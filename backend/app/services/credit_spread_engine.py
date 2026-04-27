"""
Credit Spread Engine — generates high-probability credit spread trades
from unusual options flow data + live chain data.

Pipeline per ticker:
  1. Aggregate flow alerts → determine directional bias (≥70% dominance)
  2. Fetch options chain → find best expiration (5-14 DTE ideal, up to 21)
  3. Select short strike at sell_delta 0.10-0.20, outside key levels
  4. Build spread with defined width (3-10 pts depending on price)
  5. Validate: premium >= $0.30, risk:reward <= 1:3
  6. Score 0-100 across four dimensions; output only if score >= 70

Second-stage LHF classifier (runs on all TAKE verdicts):
  - Applies strict penalties for leveraged ETFs, regime conflict,
    weak flow, and strikes inside the expected move.
  - Only labels as LOW_HANGING_FRUIT if score ≥ 85 with no hard blocks.
  - Hard blocks: leveraged ETF, regime conflict, inside expected move.
"""
import csv
import logging
import math
from datetime import date
from pathlib import Path
from typing import Optional

from app.models.credit_spread import (
    CreditSpreadResult,
    FlowConfirmation,
    LHFResult,
    LHFScoreBreakdown,
    SpreadScoreBreakdown,
    StructureContext,
)
from app.models.options import OptionContract
from app.services.options_service import _fetch_chain

logger = logging.getLogger(__name__)

# ── Base spread thresholds ──────────────────────────────────────────────────────
SELL_DELTA_MIN   = 0.10
SELL_DELTA_MAX   = 0.20   # tightened from 0.30 — only lower-delta setups
MIN_DTE          = 3
MAX_DTE          = 21
MIN_PREMIUM      = 0.30
MAX_RISK_RATIO   = 3.0
MIN_SCORE        = 70

# ── LHF classifier thresholds ──────────────────────────────────────────────────
LHF_MIN_SCORE    = 85     # only the cleanest — overridden by settings.lhf_min_score
VALID_SETUP_MIN  = 70     # acceptable but not easy

# ── LHF 4-tier downgrade thresholds ────────────────────────────────────────────
LHF_VOI_MINIMUM    = 2.0   # vol/oi < 2 → cap flow, cannot be TRUE_LHF
LHF_VOI_ACTIVE     = 8.0   # vol/oi >= 8 = "flow_strong" (qualifies for ACTIVE_TRADER_SETUP)
LHF_REGIME_MIN     = 10    # regime < 10 → cannot be TRUE_LHF
LHF_REGIME_ACTIVE  = 8     # regime < 8 → cannot be VALID_SETUP (but ACTIVE_TRADER_SETUP ok)
LHF_STRUCT_MIN     = 10    # structure < 10 → cannot be TRUE_LHF
LHF_STRUCT_ACTIVE  = 7     # structure < 7 → ACTIVE_TRADER_SETUP or PASS
LHF_OTM_PASSIVE    = 2.5   # < 2.5% OTM → ACTIVE_TRADER_SETUP at best
LHF_OTM_WARNING    = 2.0   # < 2.0% OTM → must warn "ACTIVE MANAGEMENT REQUIRED"
ACTIVE_TRADER_MIN  = 45    # minimum score for ACTIVE_TRADER_SETUP (lower than VALID_SETUP_MIN=70)
                           # because dangerous structure inherently depresses scores

# ── Flow quality requirements ──────────────────────────────────────────────────
FLOW_MIN_VOI        = 5.0          # Vol/OI minimum for any credit on flow confirmation
FLOW_LHF_VOI        = 10.0         # Vol/OI minimum to earn full flow score
FLOW_LHF_NOTIONAL   = 1_000_000    # $1M minimum notional for LHF classification
FLOW_OK_NOTIONAL    = 500_000      # $500K minimum to not be penalised at all
FLOW_MIN_ALERTS     = 2            # minimum same-direction alerts to avoid isolation penalty

# ── Regime requirements ────────────────────────────────────────────────────────
REGIME_LHF_ALIGNMENT    = 0.70     # 70%+ same-direction tickers required for LHF
REGIME_BLOCK_ALIGNMENT  = 0.30     # <30% = hard-block (no TAKE allowed)
REGIME_MIN_SAME         = 2        # need at least 2 confirming tickers for any regime credit
REGIME_GATE_MIN         = 5        # regime score < 5 → AUTO PASS (truly no edge)
                                   # 5-7: allowed for ACTIVE_TRADER_SETUP with strong flow
                                   # <8: cannot be VALID_SETUP or LHF (per user spec)
ALIGNMENT_GATE_AUTO_PASS = 0.40    # alignment < 40% with conflict + isolated flow → HARD BLOCK

# ── Penalty magnitudes ─────────────────────────────────────────────────────────
_PEN_LEVERAGED_ETF    = 18   # leveraged ETFs: excessive vol invalidates distance safety
_PEN_REGIME_CONFLICT  = 20   # strong opposite-direction flow in market
_PEN_INSIDE_EXP_MOVE  = 15   # strike closer than 1 sigma from current price
_PEN_WEAK_FLOW        = 12   # Vol/OI < 5x or notional < $500K
_PEN_ISOLATED_FLOW    = 8    # only one alert confirming direction

# ── Known leveraged ETFs ───────────────────────────────────────────────────────
LEVERAGED_ETFS: frozenset[str] = frozenset({
    # 3x equity
    "TQQQ", "SQQQ", "UPRO", "SPXS", "SPXU", "SPXL",
    "UDOW", "SDOW", "TNA",  "TZA",  "URTY", "SRTY",
    "UMDD", "SMDD",
    # 3x sector
    "SOXL", "SOXS", "TECL", "TECS", "FAS",  "FAZ",
    "ERX",  "ERY",  "GUSH", "DRIP", "LABU", "LABD",
    "NAIL", "CURE", "DFEN",
    # Volatility products
    "UVXY", "SVXY",
    # 2x (still penalised, slightly less)
    "QLD",  "QID",  "SSO",  "SDS",  "DDM",  "DXD",
    "UWM",  "TWM",  "MVV",  "MZZ",
    # Commodity leveraged
    "NUGT", "DUST", "JNUG", "JDST",
})

_CSV_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "telegram_alerts_log.csv"


# ── Utility helpers ─────────────────────────────────────────────────────────────

def _is_leveraged_etf(ticker: str) -> bool:
    return ticker.upper() in LEVERAGED_ETFS


def _expected_move_1sd(price: float, iv: float, dte_days: int) -> float:
    """
    1-sigma expected move in dollars.
      E[|ΔS|] = S × IV × √(T)   where T = DTE/365
    Returns 0.0 if inputs are invalid.
    """
    if iv <= 0.0 or dte_days <= 0 or price <= 0:
        return 0.0
    return price * iv * math.sqrt(dte_days / 365.0)


def _fmt_notional(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


# ── Bias extraction ────────────────────────────────────────────────────────────

def _aggregate_bias(alerts: list[dict]) -> tuple[str, dict]:
    """
    Returns (bias, best_alert).
    Requires ≥70% notional dominance (up from 65%) for a clean directional read.
    """
    bullish_flow = 0.0
    bearish_flow = 0.0
    bullish_alerts: list[dict] = []
    bearish_alerts: list[dict] = []

    for a in alerts:
        bias     = a.get("bias", "").upper()
        notional = a["contract"].vol_notional
        if "BULLISH" in bias:
            bullish_flow += notional
            bullish_alerts.append(a)
        elif "BEARISH" in bias:
            bearish_flow += notional
            bearish_alerts.append(a)

    total = bullish_flow + bearish_flow
    if total == 0:
        return "MIXED", {}

    bull_pct = bullish_flow / total
    bear_pct = bearish_flow / total

    # Raised from 0.65 → 0.70 to require cleaner bias
    if bull_pct >= 0.70:
        best = max(bullish_alerts, key=lambda a: a["contract"].unusual_score)
        return "BULLISH", best
    if bear_pct >= 0.70:
        best = max(bearish_alerts, key=lambda a: a["contract"].unusual_score)
        return "BEARISH", best

    return "MIXED", {}


# ── DTE helpers ────────────────────────────────────────────────────────────────

def _dte(expiration: str) -> int:
    try:
        exp = date.fromisoformat(expiration)
        return max((exp - date.today()).days, 0)
    except (ValueError, TypeError):
        return 0


def _best_expiration(contracts: list[OptionContract]) -> Optional[str]:
    """
    Prefer 5-14 DTE (sweet spot for theta capture without excess exposure).
    Falls back to full MIN_DTE-MAX_DTE window if nothing ideal exists.
    """
    exps = sorted({c.expiration for c in contracts})
    # Ideal window first
    ideal = [(e, _dte(e)) for e in exps if 5 <= _dte(e) <= 14]
    if ideal:
        return min(ideal, key=lambda x: x[1])[0]
    # Fallback to full window
    candidates = [(e, _dte(e)) for e in exps if MIN_DTE <= _dte(e) <= MAX_DTE]
    if candidates:
        return min(candidates, key=lambda x: x[1])[0]
    candidates = [(e, _dte(e)) for e in exps if _dte(e) >= MIN_DTE]
    return min(candidates, key=lambda x: x[1])[0] if candidates else None


# ── Strike selection ───────────────────────────────────────────────────────────

def _spread_width(underlying_price: float, ticker: str) -> float:
    etfs = {"SPY", "QQQ", "IWM", "DIA"}
    if ticker in etfs:
        return 5.0
    if underlying_price >= 500:
        return 10.0
    if underlying_price >= 200:
        return 5.0
    return 3.0


def _select_put_spread(
    contracts: list[OptionContract],
    expiration: str,
    underlying_price: float,
    ticker: str,
) -> Optional[tuple[OptionContract, float]]:
    """
    Bull Put Spread: sell OTM put.
    Targets delta 0.10-0.20 (tightened max from 0.30).
    """
    puts = [
        c for c in contracts
        if c.expiration == expiration
        and c.option_type == "put"
        and c.delta is not None
        and SELL_DELTA_MIN <= abs(c.delta) <= SELL_DELTA_MAX
        and c.strike < underlying_price
        and c.bid > 0
    ]
    if not puts:
        return None
    # Target delta 0.15 (centre of tightened range)
    best = min(puts, key=lambda c: abs(abs(c.delta) - 0.15))
    width = _spread_width(underlying_price, ticker)
    return best, best.strike - width


def _select_call_spread(
    contracts: list[OptionContract],
    expiration: str,
    underlying_price: float,
    ticker: str,
) -> Optional[tuple[OptionContract, float]]:
    """Bear Call Spread: sell OTM call, delta 0.10-0.20."""
    calls = [
        c for c in contracts
        if c.expiration == expiration
        and c.option_type == "call"
        and c.delta is not None
        and SELL_DELTA_MIN <= abs(c.delta) <= SELL_DELTA_MAX
        and c.strike > underlying_price
        and c.bid > 0
    ]
    if not calls:
        return None
    best = min(calls, key=lambda c: abs(abs(c.delta) - 0.15))
    width = _spread_width(underlying_price, ticker)
    return best, best.strike + width


# ── Premium calculation ────────────────────────────────────────────────────────

def _net_credit(short: OptionContract, contracts: list[OptionContract], buy_strike: float) -> float:
    exp  = short.expiration
    long_candidates = [
        c for c in contracts
        if c.expiration == exp and c.option_type == short.option_type and c.strike == buy_strike
    ]
    def _mid(c: OptionContract) -> float:
        if c.mid > 0:  return c.mid
        if c.mark and c.mark > 0: return c.mark
        return c.bid
    short_mid = _mid(short)
    long_mid  = _mid(long_candidates[0]) if long_candidates else 0.0
    return round(short_mid - long_mid, 2)


# ── Historical edge ────────────────────────────────────────────────────────────

def _historical_score(ticker: str, option_type: str) -> int:
    if not _CSV_PATH.exists():
        return 10
    try:
        rows = []
        with open(_CSV_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("ticker", "").upper() == ticker.upper()
                        and row.get("option_type", "").lower() == option_type.lower()):
                    rows.append(row)
        if not rows:
            return 8
        total    = len(rows)
        grade_a  = sum(1 for r in rows if r.get("conviction_grade", "") == "A")
        grade_b  = sum(1 for r in rows if r.get("conviction_grade", "") == "B")
        quality  = (grade_a + grade_b * 0.6) / total
        return round(5 + quality * 15)
    except Exception as exc:
        logger.warning("Historical score error for %s: %s", ticker, exc)
        return 10


# ── Base spread scoring (pre-LHF filter) ──────────────────────────────────────

def _score_trade(
    short: OptionContract,
    net_credit: float,
    dte: int,
    flow_alert: dict,
    underlying_price: float,
    ticker: str,
) -> SpreadScoreBreakdown:
    contract  = flow_alert["contract"]
    vol_not   = contract.vol_notional
    vol_oi    = contract.vol_oi_ratio
    grade     = contract.conviction_grade
    sell_delta = abs(short.delta or 0)

    # ── Flow score (0-30) ─────────────────────────────────────────────────────
    if vol_not >= 2_000_000:  notional_pts = 28
    elif vol_not >= 1_000_000: notional_pts = 22
    elif vol_not >= 500_000:  notional_pts = 16
    elif vol_not >= 250_000:  notional_pts = 10
    else:                     notional_pts = 4

    voi_pts   = 6 if vol_oi >= 10 else (4 if vol_oi >= 5 else (2 if vol_oi >= 2.5 else 0))
    grade_pts = {"A": 4, "B": 2, "C": 0}.get(grade, 0)
    flow_score = min(30, notional_pts + voi_pts + grade_pts)

    # ── Structure score (0-30) ────────────────────────────────────────────────
    iv  = short.implied_volatility or 0.0
    em  = _expected_move_1sd(underlying_price, iv, dte)
    otm = abs(short.strike - underlying_price)
    if em > 0:
        sigma = otm / em
        if sigma >= 1.5:   otm_pts = 12
        elif sigma >= 1.0: otm_pts = 8
        elif sigma >= 0.7: otm_pts = 5
        else:              otm_pts = 2
    else:
        # IV unavailable — fall back to % OTM
        otm_pct = otm / underlying_price * 100
        if otm_pct >= 7:   otm_pts = 12
        elif otm_pct >= 5: otm_pts = 9
        elif otm_pct >= 3: otm_pts = 6
        else:              otm_pts = 2

    if 5 <= dte <= 14:    dte_pts = 10
    elif dte <= 21:       dte_pts = 7
    else:                 dte_pts = 3

    delta_pts = round(max(0, 8 - abs(sell_delta - 0.15) * 40))
    structure_score = min(30, otm_pts + dte_pts + delta_pts)

    # ── Probability score (0-20) ──────────────────────────────────────────────
    prob_score = min(20, round((1.0 - sell_delta) * 22))

    # ── Historical score (0-20) ───────────────────────────────────────────────
    hist_score = _historical_score(ticker, short.option_type)

    return SpreadScoreBreakdown(
        flow_score        = flow_score,
        structure_score   = structure_score,
        probability_score = prob_score,
        historical_score  = hist_score,
        total             = flow_score + structure_score + prob_score + hist_score,
    )


# ── Structure notes ────────────────────────────────────────────────────────────

def _structure_notes(spread_type: str, sell_strike: float, underlying_price: float, dte: int) -> list[str]:
    notes = []
    otm_pct = abs(sell_strike - underlying_price) / underlying_price * 100
    if "Put" in spread_type:
        notes.append(f"Sell strike {otm_pct:.1f}% below current price")
        notes.append("Bullish flow — selling put below market")
    else:
        notes.append(f"Sell strike {otm_pct:.1f}% above current price")
        notes.append("Bearish flow — selling call above market")
    if dte <= 7:
        notes.append(f"Short DTE ({dte}d) — fast theta burn, elevated gamma")
    elif dte <= 14:
        notes.append(f"Ideal DTE ({dte}d) — balanced theta + time buffer")
    else:
        notes.append(f"DTE {dte}d — more time, higher exposure window")
    return notes


# ── Main entry point ───────────────────────────────────────────────────────────

async def generate_credit_spread(
    ticker: str,
    flow_alerts: list[dict],
) -> Optional[CreditSpreadResult]:
    if not flow_alerts:
        return None

    bias, best_alert = _aggregate_bias(flow_alerts)
    if bias == "MIXED" or not best_alert:
        return CreditSpreadResult(
            ticker=ticker, spread_type="N/A", bias="MIXED",
            sell_strike=0, buy_strike=0, expiration="", dte=0,
            premium=0, max_risk=0, win_probability=0,
            flow=FlowConfirmation(description="Mixed flow — no directional edge",
                                  vol_oi_ratio=0, vol_notional=0, conviction_grade="N/A", tags=[]),
            structure=StructureContext(sell_strike_otm_pct=0, dte=0, expiration="",
                                       delta_at_sell=0, notes=["Mixed flow — no trade"]),
            score=SpreadScoreBreakdown(flow_score=0, structure_score=0,
                                       probability_score=0, historical_score=0, total=0),
            verdict="SKIP",
            reject_reason="No clear directional bias — flow dominance < 70%",
        )

    underlying_price = best_alert.get("underlying_price", 0) or 0
    try:
        price, _, contracts = await _fetch_chain(ticker)
        if price and price > 0:
            underlying_price = price
    except Exception as exc:
        logger.warning("Credit spread chain fetch failed %s: %s", ticker, exc)
        return None

    if not contracts:
        return None

    expiration = _best_expiration(contracts)
    if not expiration:
        return CreditSpreadResult(
            ticker=ticker, spread_type="N/A", bias=bias,
            sell_strike=0, buy_strike=0, expiration="", dte=0,
            premium=0, max_risk=0, win_probability=0,
            flow=FlowConfirmation(description="No suitable expiration found",
                                  vol_oi_ratio=0, vol_notional=0, conviction_grade="N/A", tags=[]),
            structure=StructureContext(sell_strike_otm_pct=0, dte=0, expiration="",
                                       delta_at_sell=0, notes=[f"No expiration in {MIN_DTE}-{MAX_DTE} DTE"]),
            score=SpreadScoreBreakdown(flow_score=0, structure_score=0,
                                       probability_score=0, historical_score=0, total=0),
            verdict="SKIP",
            reject_reason=f"No expiration within {MIN_DTE}-{MAX_DTE} DTE",
        )

    dte = _dte(expiration)

    if bias == "BULLISH":
        spread_type       = "Bull Put Spread"
        result            = _select_put_spread(contracts, expiration, underlying_price, ticker)
        option_type_sold  = "put"
    else:
        spread_type       = "Bear Call Spread"
        result            = _select_call_spread(contracts, expiration, underlying_price, ticker)
        option_type_sold  = "call"

    def _skip(reason: str, premium: float = 0.0) -> CreditSpreadResult:
        c = best_alert["contract"]
        return CreditSpreadResult(
            ticker=ticker, spread_type=spread_type, bias=bias,
            sell_strike=0, buy_strike=0, expiration=expiration, dte=dte,
            premium=round(premium, 2), max_risk=0, win_probability=0,
            flow=FlowConfirmation(description=f"{bias.title()} flow detected",
                                  vol_oi_ratio=round(c.vol_oi_ratio, 2),
                                  vol_notional=round(c.vol_notional, 0),
                                  conviction_grade=c.conviction_grade, tags=c.reason_tags),
            structure=StructureContext(sell_strike_otm_pct=0, dte=dte, expiration=expiration,
                                       delta_at_sell=0, notes=[]),
            score=SpreadScoreBreakdown(flow_score=0, structure_score=0,
                                       probability_score=0, historical_score=0, total=0),
            verdict="SKIP",
            reject_reason=reason,
        )

    if not result:
        return _skip(f"No {option_type_sold} strike at delta {SELL_DELTA_MIN}-{SELL_DELTA_MAX}")

    short_contract, buy_strike = result
    sell_strike  = short_contract.strike
    sell_delta   = abs(short_contract.delta or 0)
    win_prob_pct = round((1.0 - sell_delta) * 100, 1)

    width      = _spread_width(underlying_price, ticker)
    net_credit = _net_credit(short_contract, contracts, buy_strike)

    if net_credit <= 0:
        return _skip("Could not determine net credit", net_credit)
    if net_credit < MIN_PREMIUM:
        return _skip(f"Premium too low: ${net_credit:.2f} (min ${MIN_PREMIUM:.2f})", net_credit)

    max_risk = round(width - net_credit, 2)
    if max_risk <= 0:
        return _skip("Invalid spread: max risk non-positive", net_credit)

    risk_ratio = max_risk / net_credit
    if risk_ratio > MAX_RISK_RATIO:
        return _skip(f"Risk:reward {risk_ratio:.1f}x exceeds {MAX_RISK_RATIO}x limit", net_credit)

    score = _score_trade(
        short=short_contract, net_credit=net_credit, dte=dte,
        flow_alert=best_alert, underlying_price=underlying_price, ticker=ticker,
    )

    verdict       = "TAKE" if score.total >= MIN_SCORE else "SKIP"
    reject_reason = None if verdict == "TAKE" else f"Score {score.total}/100 below {MIN_SCORE}"

    contract  = best_alert["contract"]
    otm_pct   = abs(sell_strike - underlying_price) / underlying_price * 100
    flow_desc = "Put selling detected" if bias == "BULLISH" else "Call selling detected"
    inst_tags = [t for t in contract.reason_tags if "Institutional" in t or "Big Premium" in t]
    if inst_tags:
        flow_desc += " — Institutional activity"

    return CreditSpreadResult(
        ticker=ticker,
        spread_type=spread_type,
        bias=bias,
        sell_strike=sell_strike,
        buy_strike=buy_strike,
        expiration=expiration,
        dte=dte,
        premium=round(net_credit, 2),
        max_risk=max_risk,
        win_probability=win_prob_pct,
        iv_at_sell=round(short_contract.implied_volatility or 0.0, 4),
        flow=FlowConfirmation(
            description=flow_desc,
            vol_oi_ratio=round(contract.vol_oi_ratio, 2),
            vol_notional=round(contract.vol_notional, 0),
            conviction_grade=contract.conviction_grade,
            tags=contract.reason_tags,
        ),
        structure=StructureContext(
            sell_strike_otm_pct=round(otm_pct, 2),
            dte=dte, expiration=expiration,
            delta_at_sell=round(sell_delta, 3),
            notes=_structure_notes(spread_type, sell_strike, underlying_price, dte),
        ),
        score=score,
        verdict=verdict,
        reject_reason=reject_reason,
    )


# ── LHF Second-Stage Classifier ───────────────────────────────────────────────
#
# Only runs on spreads with verdict='TAKE'.
# Scores 0-100 across five dimensions, then applies penalties for:
#   - Leveraged ETF                        (-18 pts)
#   - Regime conflict / weak consensus     (-20 pts)
#   - Strike inside 1-sigma expected move  (-15 pts)
#   - Weak flow (Vol/OI < 5x or < $500K)  (-12 pts)
#   - Isolated flow (single alert)         (-8 pts)
#
# Hard blocks (prevent LHF even if score ≥ 85):
#   - Ticker is a leveraged ETF
#   - Regime alignment < 30% (market pointing opposite direction)
#   - Strike is inside 1-sigma expected move
#
# Tiers:
#   🍒 LOW HANGING FRUIT   score ≥ 85 AND no hard block
#   ✅ VALID SETUP          score 70-84  (or hard-blocked with score ≥ 70)
#   ❌ REJECT               score < 70

def _lhf_flow_clarity(
    s: CreditSpreadResult,
    ticker_alerts: list[dict] | None = None,
) -> tuple[int, list[str], list[str]]:
    """
    0-25: directional conviction, Vol/OI, notional size, alert clustering.

    Requires BOTH Vol/OI ≥ 5x AND notional ≥ $500K to earn positive score.
    Full score requires Vol/OI ≥ 10x AND notional ≥ $1M.
    """
    flow   = s.flow
    notes: list[str] = []
    mines: list[str] = []

    voi      = flow.vol_oi_ratio
    notional = flow.vol_notional
    grade    = flow.conviction_grade

    # ── Notional quality (0-14) ──────────────────────────────────────────────
    if notional >= 2_000_000:   notional_pts = 14
    elif notional >= 1_000_000: notional_pts = 11
    elif notional >= 500_000:   notional_pts = 7
    elif notional >= 250_000:   notional_pts = 3
    else:                       notional_pts = 0

    # ── Vol/OI quality (0-7) — BOTH must meet minimum for any score ──────────
    if voi >= 20:    voi_pts = 7
    elif voi >= 10:  voi_pts = 5
    elif voi >= 5:   voi_pts = 3
    else:            voi_pts = 0   # below minimum — no contribution

    # ── Grade bonus (0-4) ────────────────────────────────────────────────────
    grade_pts = {"A": 4, "B": 2, "C": 0}.get(grade, 0)

    # ── Alert clustering bonus (0-2) — more confirming alerts = cleaner signal
    n_confirming = 0
    if ticker_alerts:
        target = "BULLISH" if "Put" in s.spread_type else "BEARISH"
        n_confirming = sum(1 for a in ticker_alerts if target in a.get("bias", "").upper())
    cluster_pts = 2 if n_confirming >= 3 else (1 if n_confirming >= 2 else 0)

    total = min(25, notional_pts + voi_pts + grade_pts + cluster_pts)

    # ── Notes ────────────────────────────────────────────────────────────────
    if total >= 20:
        notes.append(
            f"Grade {grade} conviction · {voi:.1f}x Vol/OI · "
            f"{_fmt_notional(notional)} flow"
            + (f" ({n_confirming} alerts)" if n_confirming >= 2 else "")
        )
    elif total >= 13:
        notes.append(f"Adequate flow: Grade {grade}, {voi:.1f}x Vol/OI, {_fmt_notional(notional)}")

    # ── Mines ────────────────────────────────────────────────────────────────
    if voi < FLOW_MIN_VOI:
        mines.append(f"⚠️ Weak flow confirmation — Vol/OI only {voi:.1f}x (need ≥{FLOW_MIN_VOI:.0f}x)")
    if notional < FLOW_OK_NOTIONAL:
        mines.append(f"⚠️ Notional too thin — {_fmt_notional(notional)} (need ≥$500K)")
    elif notional < FLOW_LHF_NOTIONAL:
        mines.append(f"Low notional for LHF — {_fmt_notional(notional)} (prefer ≥$1M)")
    if grade not in ("A", "B"):
        mines.append("Grade C or lower — speculative conviction, not institutional quality")
    if n_confirming <= 1:
        mines.append("⚠️ Isolated flow — only one alert confirming direction")

    return total, notes, mines


def _lhf_structure_safety(
    s: CreditSpreadResult,
) -> tuple[int, list[str], list[str]]:
    """
    0-25: IV-adjusted sigma distance, DTE quality, delta position.

    Uses s.iv_at_sell to compute the 1-sigma expected move.
    Strike safety is measured in sigma-units, not raw % OTM,
    so leveraged ETF distance is automatically deflated.
    """
    struct     = s.structure
    otm_pct    = struct.sell_strike_otm_pct
    dte        = struct.dte
    delta      = struct.delta_at_sell
    iv         = s.iv_at_sell
    price      = s.sell_strike / (1 - otm_pct / 100) if otm_pct < 100 else 0.0
    notes: list[str] = []
    mines: list[str] = []

    # ── Sigma-distance scoring (0-15) ────────────────────────────────────────
    em = _expected_move_1sd(price, iv, dte)
    otm_dollars = price * otm_pct / 100

    if em > 0:
        sigma_dist = otm_dollars / em
        sigma_str  = f"{sigma_dist:.2f}σ"
        if sigma_dist >= 2.0:   sigma_pts = 15
        elif sigma_dist >= 1.5: sigma_pts = 12
        elif sigma_dist >= 1.2: sigma_pts = 9
        elif sigma_dist >= 1.0: sigma_pts = 6
        elif sigma_dist >= 0.7: sigma_pts = 3
        else:                   sigma_pts = 0
    else:
        # Fallback: % OTM with tighter thresholds (IV unknown)
        sigma_dist = 0.0
        sigma_str  = f"{otm_pct:.1f}% (IV unknown)"
        if otm_pct >= 8:    sigma_pts = 11
        elif otm_pct >= 6:  sigma_pts = 8
        elif otm_pct >= 4:  sigma_pts = 5
        elif otm_pct >= 2.5: sigma_pts = 3
        else:               sigma_pts = 0

    # ── DTE quality (0-7) ────────────────────────────────────────────────────
    if 5 <= dte <= 14:    dte_pts = 7
    elif dte <= 21:       dte_pts = 5
    elif dte <= 3:        dte_pts = 1   # gamma ramp — too risky
    else:                 dte_pts = 2

    # ── Delta positioning (0-3) ──────────────────────────────────────────────
    if delta <= 0.12:    delta_pts = 3
    elif delta <= 0.15:  delta_pts = 2
    elif delta <= 0.18:  delta_pts = 1
    else:                delta_pts = 0  # > 0.18 with tighter range = too close

    total = min(25, sigma_pts + dte_pts + delta_pts)

    # ── Notes ────────────────────────────────────────────────────────────────
    if sigma_pts >= 12:
        notes.append(f"Strike {sigma_str} from current price — strong safety buffer")
    elif sigma_pts >= 9:
        notes.append(f"Strike {sigma_str} — reasonable buffer, not exceptional")
    if 5 <= dte <= 14:
        notes.append(f"{dte}d DTE — ideal theta decay window")

    # ── Mines ────────────────────────────────────────────────────────────────
    if em > 0 and sigma_dist < 1.0:
        mines.append(
            f"⚠️ Within expected move — strike only {sigma_str} from price "
            f"(1SD move = ${em:.2f})"
        )
    elif em > 0 and sigma_dist < 1.2:
        mines.append(f"Marginal buffer — strike {sigma_str}, barely outside 1-sigma move")
    if delta > 0.18:
        mines.append(f"⚠️ Delta {delta:.2f} — too close to ATM after range tightening")
    if dte <= 3:
        mines.append(f"⚠️ Only {dte}d to expiry — extreme gamma risk")
    if dte > 21:
        mines.append(f"{dte}d DTE — excessive time, extended risk window")

    return total, notes, mines


def _lhf_regime(
    s: CreditSpreadResult,
    all_alerts: list[dict],
) -> tuple[int, list[str], list[str], float]:
    """
    0-20: directional consensus across full scan.

    Returns (score, notes, mines, alignment_ratio).
    Requires ≥70% same-direction for strong regime.
    Alignment < 30% = hard block.
    """
    notes: list[str] = []
    mines: list[str] = []

    if not all_alerts:
        return 8, ["Regime: no scan data (neutral)"], [], 0.5

    target   = "BULLISH" if "Put" in s.spread_type else "BEARISH"
    opposite = "BEARISH" if target == "BULLISH" else "BULLISH"

    same_tickers: set[str] = set()
    opp_tickers:  set[str] = set()

    for a in all_alerts:
        ticker = a["contract"].ticker
        bias   = a.get("bias", "").upper()
        if target in bias:
            same_tickers.add(ticker)
        elif opposite in bias:
            opp_tickers.add(ticker)

    n_same  = len(same_tickers)
    n_opp   = len(opp_tickers)
    n_total = n_same + n_opp

    # Alignment ratio: fraction of directional tickers on our side
    alignment = (n_same / n_total) if n_total > 0 else 0.5

    # ── Regime score ─────────────────────────────────────────────────────────
    if alignment >= 0.80 and n_same >= 5:
        score = 20
        notes.append(f"Strong regime — {n_same}/{n_total} tickers {target} ({alignment:.0%} aligned)")
    elif alignment >= 0.70 and n_same >= 4:
        score = 16
        notes.append(f"Good regime — {n_same}/{n_total} tickers {target} ({alignment:.0%} aligned)")
    elif alignment >= 0.70 and n_same >= 2:
        score = 12
        notes.append(f"Adequate regime — {n_same} {target} tickers ({alignment:.0%} aligned)")
    elif alignment >= 0.60 and n_same >= 3:
        score = 9
        notes.append(f"Moderate regime — {n_same} confirming, some mixed signals")
    elif n_same >= 2 and alignment >= 0.50:
        score = 6
        notes.append(f"Weak regime — {n_same} confirming but {n_opp} opposing tickers")
    elif n_same == 1:
        score = 4
        notes.append("Isolated signal — no regime confirmation from other tickers")
    else:
        score = 2
        notes.append("No same-direction tickers — completely isolated trade")

    # ── Conflict penalties ───────────────────────────────────────────────────
    if alignment < REGIME_BLOCK_ALIGNMENT and n_opp >= 2:
        mines.append(
            f"⚠️ Regime conflict — {n_opp} tickers pointing {opposite} vs "
            f"only {n_same} confirming ({alignment:.0%} alignment)"
        )
        score = 0   # wipe regime score for hard-block case
    elif n_opp > n_same and n_opp >= 2:
        mines.append(
            f"⚠️ More {opposite} tickers ({n_opp}) than {target} ({n_same}) — conflicting market bias"
        )
        score = max(0, score - 10)
    elif n_opp >= 3 and alignment < 0.60:
        mines.append(f"Mixed market regime — {n_opp} tickers opposing this trade direction")
        score = max(0, score - 5)

    return min(20, score), notes, mines, alignment


def _lhf_premium_quality(s: CreditSpreadResult) -> tuple[int, list[str], list[str]]:
    """0-10: credit size and risk:reward ratio."""
    credit = s.premium
    risk   = s.max_risk
    width  = credit + risk
    notes: list[str] = []
    mines: list[str] = []

    if credit >= 1.00:   credit_pts = 7
    elif credit >= 0.70: credit_pts = 6
    elif credit >= 0.50: credit_pts = 4
    elif credit >= 0.35: credit_pts = 2
    else:                credit_pts = 0

    rr = risk / credit if credit > 0 else 99
    if rr <= 1.5:    rr_pts = 3
    elif rr <= 2.0:  rr_pts = 2
    elif rr <= 2.5:  rr_pts = 1
    else:            rr_pts = 0

    total = min(10, credit_pts + rr_pts)

    if credit >= 0.50:
        notes.append(f"${credit:.2f} credit on ${width:.0f}-wide spread ({rr:.1f}:1 risk:reward)")

    if credit < 0.35:
        mines.append(f"⚠️ Premium trap — ${credit:.2f} credit is too thin for the risk")
    if rr > 3.0:
        mines.append(f"⚠️ Risk:reward {rr:.1f}:1 — unfavorable payoff structure")

    return total, notes, mines


# ── LHF support helpers ────────────────────────────────────────────────────────

def _compute_gamma_risk(dte: int, otm_pct: float) -> str:
    """HIGH / MEDIUM / LOW gamma risk based on DTE + distance from short strike."""
    if dte <= 3:
        return "HIGH"
    if dte <= 5 or otm_pct < LHF_OTM_PASSIVE:
        return "MEDIUM"
    return "LOW"


def _build_management_rules(
    classification: str,
    spread: "CreditSpreadResult",
    is_active_trade: bool,
) -> dict:
    """Generate entry/stop/profit/invalidation guidance based on classification."""
    premium  = spread.premium
    max_risk = spread.max_risk
    is_call  = "Call" in spread.spread_type

    if classification == "LOW_HANGING_FRUIT":
        return {
            "entry":        "Enter at open or first pullback — no special trigger needed",
            "stop":         f"Close if loss exceeds ${max_risk * 0.5:.2f} (50% of max risk)",
            "profit_taking": f"Close at 50% of credit (${premium * 0.5:.2f} target)",
            "invalidation": "Breach of short strike or regime reversal",
        }
    elif classification == "ACTIVE_TRADER_SETUP":
        trigger = "price rejection at short strike" if is_call else "price hold above short strike"
        return {
            "entry":        f"Wait for {trigger} — do not enter at open",
            "stop":         "Close immediately if short strike is touched or challenged",
            "profit_taking": f"Close at 25-40% of credit (${premium * 0.3:.2f} target)",
            "invalidation": "Any challenge of short strike — no holding through it",
        }
    else:  # VALID_SETUP
        return {
            "entry":        "Wait for intraday confirmation — not at market open",
            "stop":         f"Close if loss exceeds ${max_risk * 0.4:.2f} (40% of max risk)",
            "profit_taking": "Close at 40-50% of credit",
            "invalidation": "Regime shift or short strike challenged",
        }


# ── Main LHF classifier ────────────────────────────────────────────────────────

def classify_lhf(
    spread: CreditSpreadResult,
    all_alerts: list[dict],
    ticker_alerts: list[dict] | None = None,
) -> LHFResult:
    """
    Second-stage Low Hanging Fruit classifier.
    Only call on spreads with verdict='TAKE'.

    Decision order (non-negotiable):
      1. Hard gates  — STOP immediately if triggered, no score computed
      2. Sub-scores  — flow is capped by regime quality
      3. Auto-pass   — regime < 8 regardless of other scores
      4. Penalties   — applied to already-capped total
      5. Classification

    Hard gates (triggers → REJECT with no override):
      - Regime score = 0          (active directional conflict)
      - Market alignment < 30%    (market explicitly opposing trade)
      - Alignment < 40% AND flow isolated  (no structural OR flow confirmation)

    Flow cap by regime score (prevents flow from overpowering structure):
      regime < 5  → flow capped at 10/25
      regime < 10 → flow capped at 15/25
      regime ≥ 10 → full 25 available

    Auto-pass gate:
      regime < 8 → REJECT (insufficient market confirmation, not tradeable)

    Penalties applied after sub-scores (if gates pass):
      Leveraged ETF          -18
      Regime soft conflict   -20  (alignment 30-40% with opposing tickers)
      Inside expected move   -15  (< 1σ from price)
      Weak flow              -12  (Vol/OI < 5x or < $500K)
      Isolated flow           -8  (single confirming alert)

    Hard blocks in penalty phase (prevent LHF; VALID_SETUP still possible):
      - Leveraged ETF
      - Strike inside 1-sigma expected move

    Tiers:
      score ≥ 85 + no hard block → 🍒 LOW HANGING FRUIT
      score 70-84  → ✅ VALID SETUP
      score < 70   → ❌ REJECT
    """
    from app.config import settings

    # ── Step 1: Regime (always first — all gates depend on it) ────────────────
    regime_score, regime_notes, regime_mines, alignment = _lhf_regime(spread, all_alerts)

    # ── Step 2: Flow isolation state ──────────────────────────────────────────
    target = "BULLISH" if "Put" in spread.spread_type else "BEARISH"
    n_confirming = 0
    if ticker_alerts:
        n_confirming = sum(1 for a in ticker_alerts if target in a.get("bias", "").upper())
    flow_isolated = n_confirming <= 1

    # ── Step 3: Hard gates — STOP HERE, no scoring, no override ──────────────
    hard_block: Optional[str] = None

    if regime_score == 0:
        # Regime = 0 means _lhf_regime detected active directional conflict
        hard_block = (
            f"Regime 0/20 — {alignment:.0%} alignment indicates active directional conflict"
        )
    elif alignment < REGIME_BLOCK_ALIGNMENT:
        # Alignment < 30% — market explicitly pointing against this trade
        hard_block = (
            f"Market alignment {alignment:.0%} — below 30% minimum threshold"
        )
    elif alignment < ALIGNMENT_GATE_AUTO_PASS and flow_isolated:
        # Combined kill: weak regime + no flow confirmation = zero basis for trade
        hard_block = (
            f"Regime conflict ({alignment:.0%} alignment) combined with isolated flow — "
            f"no structural or flow confirmation"
        )

    if hard_block:
        gate_warnings = [f"⛔ Regime {regime_score}/20 — alignment {alignment:.0%}"]
        if flow_isolated:
            gate_warnings.append("⛔ Isolated flow — no confirming alerts")
        gate_reasons = [
            f"Regime conflict ({regime_score}/20)",
            f"Alignment only {alignment:.0%}",
        ]
        if flow_isolated:
            gate_reasons.append("Isolated flow (no confirmation)")
        return LHFResult(
            classification      = "REJECT",
            tier                = "❌ REJECT",
            score = LHFScoreBreakdown(
                flow_clarity=0, structure_safety=0, regime=regime_score,
                premium_quality=0, historical_edge=0,
                raw_total=regime_score, penalties=0, total=0,
            ),
            why_easy            = [],
            landmines           = regime_mines,
            warnings            = gate_warnings,
            reject_reasons      = gate_reasons,
            lhf_blocked_by      = hard_block,
            trade_style         = "WATCH_ONLY",
            gamma_risk          = _compute_gamma_risk(spread.structure.dte, spread.structure.sell_strike_otm_pct),
            size_recommendation = "NO_TRADE",
            why_not_lhf         = [hard_block],
            management          = {},
            do_not_hold_blindly = False,
        )

    # ── Step 4: Remaining sub-scores ──────────────────────────────────────────
    flow_score,   flow_notes,   flow_mines   = _lhf_flow_clarity(spread, ticker_alerts)
    struct_score, struct_notes, struct_mines = _lhf_structure_safety(spread)
    prem_score,   prem_notes,   prem_mines   = _lhf_premium_quality(spread)
    opt_type   = "put" if "Put" in spread.spread_type else "call"
    hist_score = _historical_score(spread.ticker, opt_type)

    # ── Step 5: Flow cap — regime quality limits flow weight ──────────────────
    flow_cap = 10 if regime_score < 5 else (15 if regime_score < 10 else 25)
    if flow_score > flow_cap:
        flow_mines.append(
            f"Flow capped at {flow_cap}/25 — regime {regime_score}/20 limits flow influence"
        )
        flow_score = flow_cap

    # ── Step 6: Auto-pass gate — regime too weak regardless of other scores ───
    raw_total = flow_score + struct_score + regime_score + prem_score + hist_score
    if regime_score < REGIME_GATE_MIN:
        # Bypass: strong flow + dangerous structure can still yield ACTIVE_TRADER_SETUP
        # even with a very weak regime. Flow quality IS the signal; structure danger is acknowledged.
        _bypass_voi  = spread.flow.vol_oi_ratio >= LHF_VOI_ACTIVE   # >= 8x
        _bypass_not  = spread.flow.vol_notional >= 2_000_000         # >= $2M notional
        _bypass_str  = (
            spread.structure.sell_strike_otm_pct < LHF_OTM_PASSIVE  # < 2.5% OTM
            or spread.structure.dte <= 3                              # extreme gamma
        )
        if _bypass_voi and _bypass_not and _bypass_str:
            pass  # let scoring proceed — will be ACTIVE_TRADER_SETUP in Step 8
        else:
            # Truly no-go: regime too weak for any classification
            _early_gamma  = _compute_gamma_risk(spread.structure.dte, spread.structure.sell_strike_otm_pct)
            _early_why    = [f"Regime {regime_score}/20 below minimum tradeable threshold ({REGIME_GATE_MIN})"]
            _early_mgmt   = _build_management_rules("REJECT", spread, False)
            return LHFResult(
            classification      = "REJECT",
            tier                = "❌ REJECT",
            score = LHFScoreBreakdown(
                flow_clarity=flow_score, structure_safety=struct_score, regime=regime_score,
                premium_quality=prem_score, historical_edge=hist_score,
                raw_total=raw_total, penalties=0, total=raw_total,
            ),
            why_easy            = [],
            landmines           = regime_mines + flow_mines,
            warnings            = [
                f"AUTO PASS — Regime {regime_score}/20 below minimum gate ({REGIME_GATE_MIN})"
            ],
            reject_reasons      = [
                f"Regime {regime_score}/20 — need ≥{REGIME_GATE_MIN} for any consideration"
            ],
            lhf_blocked_by      = f"Regime {regime_score}/20 below AUTO PASS threshold ({REGIME_GATE_MIN})",
            trade_style         = "WATCH_ONLY",
            gamma_risk          = _early_gamma,
            size_recommendation = "NO_TRADE",
            why_not_lhf         = _early_why,
            management          = _early_mgmt,
            do_not_hold_blindly = False,
        )

    # ── Step 7: Penalty computation ───────────────────────────────────────────
    penalties  = 0
    warnings: list[str] = []
    hard_block = None

    is_lev = _is_leveraged_etf(spread.ticker)
    if is_lev:
        penalties += _PEN_LEVERAGED_ETF
        warnings.append("⚠️ Leveraged ETF — volatility risk: OTM distance is unreliable")
        hard_block = f"{spread.ticker} is a leveraged ETF — buffer is deceptive at high IV"

    # Regime soft conflict (30-40% alignment — gates already blocked <30%)
    opp_bias  = "BEARISH" if "Put" in spread.spread_type else "BULLISH"
    opp_count = sum(1 for a in all_alerts if opp_bias in a.get("bias", "").upper())
    if alignment < ALIGNMENT_GATE_AUTO_PASS and opp_count >= 2:
        penalties += _PEN_REGIME_CONFLICT
        warnings.append(
            f"⚠️ Weak regime — {alignment:.0%} alignment, {opp_count} opposing tickers"
        )

    # Expected-move check
    iv    = spread.iv_at_sell
    dte   = spread.structure.dte
    price = (
        spread.sell_strike / (1 - spread.structure.sell_strike_otm_pct / 100)
        if spread.structure.sell_strike_otm_pct < 100 else 0.0
    )
    em    = _expected_move_1sd(price, iv, dte)
    otm_d = price * spread.structure.sell_strike_otm_pct / 100

    inside_expected = em > 0 and otm_d < em
    if inside_expected:
        sigma = otm_d / em if em > 0 else 0
        penalties += _PEN_INSIDE_EXP_MOVE
        warnings.append(
            f"⚠️ Strike within 1-sigma expected move — "
            f"only {sigma:.2f}σ OTM (1SD move = ${em:.2f})"
        )
        if hard_block is None:
            hard_block = f"Strike is inside the expected move ({sigma:.2f}σ < 1.0σ)"

    # Weak flow check
    if spread.flow.vol_oi_ratio < FLOW_MIN_VOI or spread.flow.vol_notional < FLOW_OK_NOTIONAL:
        penalties += _PEN_WEAK_FLOW
        warnings.append(
            f"⚠️ Weak flow confirmation — "
            f"Vol/OI {spread.flow.vol_oi_ratio:.1f}x, "
            f"{_fmt_notional(spread.flow.vol_notional)} notional"
        )

    # Isolated flow
    if flow_isolated:
        penalties += _PEN_ISOLATED_FLOW
        warnings.append("⚠️ Isolated flow — single alert, no sweep clustering")

    final_total = max(0, raw_total - penalties)

    # ── Step 7.5: Hard downgrade rules + score caps ───────────────────────────
    # These prevent premium/edge from rescuing bad structure, regime, or flow.
    otm_pct  = spread.structure.sell_strike_otm_pct
    voi      = spread.flow.vol_oi_ratio
    notional = spread.flow.vol_notional

    _struct_weak = struct_score < 10
    _regime_weak = regime_score < LHF_REGIME_MIN      # < 10
    _voi_weak    = voi < LHF_VOI_MINIMUM              # < 2x
    _dte_short   = dte <= 3
    _otm_close   = otm_pct < LHF_OTM_PASSIVE          # < 2.5%

    # Per-weakness score caps
    _cap = 100
    if _struct_weak:  _cap = min(_cap, 74)
    if _regime_weak:  _cap = min(_cap, 78)
    if _voi_weak:     _cap = min(_cap, 76)

    # Two or more weaknesses → cannot be LHF at all
    _weakness_count = sum([_struct_weak, _regime_weak, _voi_weak, _dte_short, _otm_close])
    if _weakness_count >= 2:
        _cap = min(_cap, 74)

    final_total = min(final_total, _cap)

    # Build explicit LHF disqualifier list
    why_not_lhf: list[str] = []
    lhf_disqualified = hard_block is not None

    if hard_block:
        why_not_lhf.append(f"Hard block: {hard_block}")

    if voi < LHF_VOI_MINIMUM:
        lhf_disqualified = True
        why_not_lhf.append(f"Low Vol/OI ({voi:.1f}x — LHF requires ≥{LHF_VOI_MINIMUM:.0f}x)")
    elif voi < FLOW_MIN_VOI:
        why_not_lhf.append(f"Moderate Vol/OI ({voi:.1f}x — strong LHF needs ≥{FLOW_MIN_VOI:.0f}x)")

    if notional < FLOW_LHF_NOTIONAL:
        lhf_disqualified = True
        why_not_lhf.append(f"Notional {_fmt_notional(notional)} below $1M LHF threshold")

    if regime_score < LHF_REGIME_MIN:
        lhf_disqualified = True
        why_not_lhf.append(f"Regime {regime_score}/20 — LHF requires ≥{LHF_REGIME_MIN}/20")

    # Market-level opposing flow disqualifier
    _lhf_target   = "BULLISH" if "Put" in spread.spread_type else "BEARISH"
    _lhf_opposite = "BEARISH" if _lhf_target == "BULLISH" else "BULLISH"
    _n_same = len({a["contract"].ticker for a in all_alerts if _lhf_target in a.get("bias","").upper()})
    _n_opp  = len({a["contract"].ticker for a in all_alerts if _lhf_opposite in a.get("bias","").upper()})

    if _n_opp >= _n_same and _n_same > 0:
        lhf_disqualified = True
        why_not_lhf.append(
            f"Conflicting flow — {_n_opp} opposing tickers cancel {_n_same} confirming"
        )
    elif _n_opp >= 3 and alignment < 0.60:
        why_not_lhf.append(f"Mixed market ({_n_opp} opposing tickers present)")

    if struct_score < LHF_STRUCT_MIN:
        lhf_disqualified = True
        why_not_lhf.append(f"Structure {struct_score}/25 — LHF requires ≥{LHF_STRUCT_MIN}/25")

    if dte <= 4 and otm_pct < 3.0:
        lhf_disqualified = True
        why_not_lhf.append(f"DTE {dte} + {otm_pct:.1f}% OTM — too close for passive trade")

    # Active trade determination: strong signal but dangerous structure
    is_active_trade = (
        otm_pct < LHF_OTM_PASSIVE    # < 2.5%: too close to money for passive hold
        or dte <= 3                   # extreme gamma exposure
        or struct_score < LHF_STRUCT_ACTIVE  # structure < 7: below minimum safe level
    )

    gamma_risk = _compute_gamma_risk(dte, otm_pct)

    # ── Step 8: 4-Tier Classification ─────────────────────────────────────────
    lhf_threshold = getattr(settings, "lhf_min_score", LHF_MIN_SCORE)

    all_mines = flow_mines + struct_mines + regime_mines + prem_mines
    why_easy  = [n for n in (flow_notes + struct_notes + regime_notes + prem_notes) if n]

    # Flow-strength flag: high vol_oi or large notional = "strong signal"
    _flow_strong = (voi >= LHF_VOI_ACTIVE or notional >= 2_000_000)

    # regime < 8 blocks VALID_SETUP but ACTIVE_TRADER_SETUP still allowed if flow strong
    _regime_blocks_valid = regime_score < LHF_REGIME_ACTIVE  # < 8

    if final_total >= lhf_threshold and not lhf_disqualified and _weakness_count < 2:
        # Only genuinely clean, passive-safe setups reach TRUE LHF
        classification = "LOW_HANGING_FRUIT"
        tier           = "🍒 LOW HANGING FRUIT"
        trade_style    = "PASSIVE"
        size_rec       = "NORMAL"
        reject_reasons: list[str] = []
    elif is_active_trade and _flow_strong and final_total >= ACTIVE_TRADER_MIN:
        # Strong flow but dangerous structure — requires active intraday management
        # Also captures regime 5-7 cases where flow is clearly strong (user spec: downgrade, not reject)
        classification = "ACTIVE_TRADER_SETUP"
        tier           = "⚡ ACTIVE TRADER SETUP"
        trade_style    = "ACTIVE"
        size_rec       = "SMALL"
        reject_reasons = why_not_lhf[:4]
    elif is_active_trade and _flow_strong and flow_score >= 8:
        # Regime bypass path: flow passed the strict bypass gate (vol_oi >= 8x, notional >= $2M)
        # but structure/inside-expected-move penalties drove final_total below ACTIVE_TRADER_MIN.
        # The flow signal is real — classify as ACTIVE_TRADER_SETUP, not REJECT.
        classification = "ACTIVE_TRADER_SETUP"
        tier           = "⚡ ACTIVE TRADER SETUP"
        trade_style    = "ACTIVE"
        size_rec       = "SMALL"
        reject_reasons = why_not_lhf[:4]
    elif final_total >= VALID_SETUP_MIN and not _regime_blocks_valid:
        # Directionally sound, regime adequate, but not clean enough to call easy
        classification = "VALID_SETUP"
        tier           = "✅ VALID SETUP"
        trade_style    = "ACTIVE"
        size_rec       = "SMALL"
        reject_reasons = why_not_lhf[:3]
    elif final_total >= VALID_SETUP_MIN and _regime_blocks_valid and _flow_strong:
        # Regime 5-7 + good overall score + strong flow → treat as ACTIVE_TRADER_SETUP
        classification = "ACTIVE_TRADER_SETUP"
        tier           = "⚡ ACTIVE TRADER SETUP"
        trade_style    = "ACTIVE"
        size_rec       = "SMALL"
        why_not_lhf    = [f"Regime {regime_score}/20 limits to active-only (need ≥8 for VALID_SETUP)"] + why_not_lhf[:3]
        reject_reasons = why_not_lhf[:4]
    else:
        classification = "REJECT"
        tier           = "❌ REJECT"
        trade_style    = "WATCH_ONLY"
        size_rec       = "NO_TRADE"
        reject_reasons = (warnings + all_mines) or ["Overall score too low for a viable spread"]

    do_not_hold = is_active_trade and classification in ("ACTIVE_TRADER_SETUP", "VALID_SETUP")
    management  = _build_management_rules(classification, spread, is_active_trade)

    return LHFResult(
        classification      = classification,
        tier                = tier,
        score = LHFScoreBreakdown(
            flow_clarity     = flow_score,
            structure_safety = struct_score,
            regime           = regime_score,
            premium_quality  = prem_score,
            historical_edge  = hist_score,
            raw_total        = raw_total,
            penalties        = penalties,
            total            = final_total,
        ),
        why_easy            = why_easy,
        landmines           = all_mines,
        warnings            = warnings,
        reject_reasons      = reject_reasons,
        lhf_blocked_by      = hard_block,
        trade_style         = trade_style,
        gamma_risk          = gamma_risk,
        size_recommendation = size_rec,
        why_not_lhf         = why_not_lhf,
        management          = management,
        do_not_hold_blindly = do_not_hold,
    )


# ── Scan runner ────────────────────────────────────────────────────────────────

async def run_spread_scan(scan_result: dict) -> dict:
    """
    Run credit spread engine + LHF classifier across all tickers in a scan result.
    Returns spreads sorted by tier: LOW_HANGING_FRUIT first, then VALID_SETUP.
    """
    import asyncio

    all_alerts       = scan_result.get("alerts", [])
    alerts_by_ticker: dict[str, list[dict]] = {}
    for a in all_alerts:
        t = a["contract"].ticker
        alerts_by_ticker.setdefault(t, []).append(a)

    tasks = {
        ticker: generate_credit_spread(ticker, ticker_alerts)
        for ticker, ticker_alerts in alerts_by_ticker.items()
    }

    results  = await asyncio.gather(*tasks.values(), return_exceptions=True)
    spreads: list[CreditSpreadResult] = []
    rejected: list[dict] = []

    for ticker, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            logger.warning("Spread engine error %s: %s", ticker, res)
            rejected.append({"ticker": ticker, "reason": str(res)})
            continue
        if res is None:
            rejected.append({"ticker": ticker, "reason": "No data"})
            continue
        if res.verdict == "TAKE":
            lhf = classify_lhf(res, all_alerts, alerts_by_ticker.get(ticker))
            if lhf.classification == "REJECT":
                # Hard gate / score rejection — not actionable; store for audit
                reason = lhf.lhf_blocked_by or (lhf.reject_reasons[0] if lhf.reject_reasons else "LHF rejected")
                full   = res.model_copy(update={"lhf": lhf})
                rejected.append({"ticker": ticker, "reason": reason, "spread": full})
            else:
                spreads.append(res.model_copy(update={"lhf": lhf}))
        else:
            rejected.append({"ticker": ticker, "reason": res.reject_reason or "Score too low", "spread": None})

    _tier_rank = {"LOW_HANGING_FRUIT": 0, "VALID_SETUP": 1, "ACTIVE_TRADER_SETUP": 2, "REJECT": 3}

    def _sort_key(s: CreditSpreadResult):
        rank  = _tier_rank.get(s.lhf.classification if s.lhf else "REJECT", 2)
        score = s.lhf.score.total if s.lhf else 0
        return (rank, -score)

    spreads.sort(key=_sort_key)
    lhf_count = sum(
        1 for s in spreads
        if s.lhf and s.lhf.classification == "LOW_HANGING_FRUIT"
    )

    return {
        "spreads":     spreads,
        "rejected":    rejected,
        "total_valid": len(spreads),
        "total_lhf":   lhf_count,
    }
