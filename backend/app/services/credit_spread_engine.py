"""
Credit Spread Engine — generates high-probability credit spread trades
from unusual options flow data + live chain data.

Pipeline per ticker:
  1. Aggregate flow alerts → determine directional bias
  2. Fetch options chain → find best expiration (7-21 DTE)
  3. Select short strike at sell_delta 0.10-0.30, outside key levels
  4. Build spread with defined width (3-10 pts depending on price)
  5. Validate: premium >= $0.30, risk:reward <= 1:3
  6. Score 0-100 across four dimensions; output only if score >= 70
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

# ── Thresholds ─────────────────────────────────────────────────────────────────
SELL_DELTA_MIN   = 0.10
SELL_DELTA_MAX   = 0.30
MIN_DTE          = 3
MAX_DTE          = 21
MIN_PREMIUM      = 0.30   # minimum net credit (per share)
MAX_RISK_RATIO   = 3.0    # max (max_risk / premium) ratio
MIN_SCORE        = 70     # minimum composite score to output TAKE

_CSV_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "telegram_alerts_log.csv"


# ── Bias extraction ────────────────────────────────────────────────────────────

def _aggregate_bias(alerts: list[dict]) -> tuple[str, dict]:
    """
    Returns (bias, best_alert) from a list of scanner alerts for one ticker.

    Bias rules:
      Majority bullish flow → BULLISH (sell puts → Bull Put Spread)
      Majority bearish flow → BEARISH (sell calls → Bear Call Spread)
      Mixed or hedge-only   → MIXED (no trade)

    The 'best_alert' is the highest-scoring contract among those that
    match the dominant bias.
    """
    bullish_flow = 0.0
    bearish_flow = 0.0
    bullish_alerts: list[dict] = []
    bearish_alerts: list[dict] = []

    for a in alerts:
        bias  = a.get("bias", "").upper()
        notional = a["contract"].vol_notional
        if "BULLISH" in bias:
            bullish_flow += notional
            bullish_alerts.append(a)
        elif "BEARISH" in bias:
            bearish_flow += notional
            bearish_alerts.append(a)
        # HEDGE / SPECULATIVE don't count toward a directional spread

    total = bullish_flow + bearish_flow
    if total == 0:
        return "MIXED", {}

    bull_pct = bullish_flow / total
    bear_pct = bearish_flow / total

    # Require at least 65% dominance for a clean bias
    if bull_pct >= 0.65:
        best = max(bullish_alerts, key=lambda a: a["contract"].unusual_score)
        return "BULLISH", best
    if bear_pct >= 0.65:
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
    """Pick the nearest expiration within [MIN_DTE, MAX_DTE]."""
    exps = sorted({c.expiration for c in contracts})
    candidates = [(e, _dte(e)) for e in exps if MIN_DTE <= _dte(e) <= MAX_DTE]
    if not candidates:
        # Fallback: nearest with dte >= MIN_DTE
        candidates = [(e, _dte(e)) for e in exps if _dte(e) >= MIN_DTE]
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[1])[0]


# ── Strike selection ───────────────────────────────────────────────────────────

def _spread_width(underlying_price: float, ticker: str) -> float:
    """Return the spread width in points based on price / ticker type."""
    etfs = {"SPY", "QQQ", "IWM", "DIA"}
    if ticker in etfs:
        return 5.0
    if underlying_price >= 500:
        return 10.0
    if underlying_price >= 200:
        return 5.0
    return 3.0


def _nearest_strike(
    strikes: list[float],
    target: float,
    prefer_below: bool = True,
) -> Optional[float]:
    """Find the tradeable strike nearest to target (OTM side)."""
    candidates = [s for s in strikes if (s <= target if prefer_below else s >= target)]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s - target)) if prefer_below \
        else min(candidates, key=lambda s: abs(s - target))


def _select_put_spread(
    contracts: list[OptionContract],
    expiration: str,
    underlying_price: float,
    ticker: str,
) -> Optional[tuple[OptionContract, float]]:
    """
    Bull Put Spread: sell OTM put at delta 0.10-0.30, buy put below.
    Returns (short_contract, buy_strike) or None.
    """
    puts = [
        c for c in contracts
        if c.expiration == expiration
        and c.option_type == "put"
        and c.delta is not None
        and SELL_DELTA_MIN <= abs(c.delta) <= SELL_DELTA_MAX
        and c.strike < underlying_price          # must be OTM
        and c.bid > 0                            # must have a real bid
    ]
    if not puts:
        return None

    # Choose the put with delta closest to 0.20 (center of sweet spot)
    best = min(puts, key=lambda c: abs(abs(c.delta) - 0.20))

    width = _spread_width(underlying_price, ticker)
    buy_strike = best.strike - width

    return best, buy_strike


def _select_call_spread(
    contracts: list[OptionContract],
    expiration: str,
    underlying_price: float,
    ticker: str,
) -> Optional[tuple[OptionContract, float]]:
    """
    Bear Call Spread: sell OTM call at delta 0.10-0.30, buy call above.
    Returns (short_contract, buy_strike) or None.
    """
    calls = [
        c for c in contracts
        if c.expiration == expiration
        and c.option_type == "call"
        and c.delta is not None
        and SELL_DELTA_MIN <= abs(c.delta) <= SELL_DELTA_MAX
        and c.strike > underlying_price          # must be OTM
        and c.bid > 0
    ]
    if not calls:
        return None

    best = min(calls, key=lambda c: abs(abs(c.delta) - 0.20))

    width = _spread_width(underlying_price, ticker)
    buy_strike = best.strike + width

    return best, buy_strike


# ── Premium calculation ────────────────────────────────────────────────────────

def _net_credit(short: OptionContract, contracts: list[OptionContract], buy_strike: float) -> float:
    """
    Net credit = short premium - long premium.
    Uses mid price; falls back to mark, then bid.
    """
    is_put = short.option_type == "put"
    exp    = short.expiration

    long_candidates = [
        c for c in contracts
        if c.expiration == exp
        and c.option_type == short.option_type
        and c.strike == buy_strike
    ]

    def _mid(c: OptionContract) -> float:
        if c.mid > 0:
            return c.mid
        if c.mark and c.mark > 0:
            return c.mark
        return c.bid

    short_mid = _mid(short)
    long_mid  = _mid(long_candidates[0]) if long_candidates else 0.0

    return round(short_mid - long_mid, 2)


# ── Historical edge ────────────────────────────────────────────────────────────

def _historical_score(ticker: str, option_type: str) -> int:
    """
    Load signals CSV and compute a historical quality score (0-20) for
    this ticker + direction based on conviction grade distribution.

    Higher score = more Grade-A signals in history for this setup.
    """
    if not _CSV_PATH.exists():
        return 10  # neutral fallback

    try:
        rows = []
        with open(_CSV_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("ticker", "").upper() == ticker.upper():
                    if row.get("option_type", "").lower() == option_type.lower():
                        rows.append(row)

        if not rows:
            # Ticker exists but not this direction — slight discount
            return 8

        total = len(rows)
        grade_a = sum(1 for r in rows if r.get("conviction_grade", "") == "A")
        grade_b = sum(1 for r in rows if r.get("conviction_grade", "") == "B")

        quality_rate = (grade_a + grade_b * 0.6) / total

        # Map quality_rate [0,1] → score [5,20]
        return round(5 + quality_rate * 15)

    except Exception as exc:
        logger.warning("Historical score error for %s: %s", ticker, exc)
        return 10


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score_trade(
    short: OptionContract,
    net_credit: float,
    dte: int,
    flow_alert: dict,
    underlying_price: float,
    ticker: str,
) -> SpreadScoreBreakdown:
    contract = flow_alert["contract"]

    # ── Flow score (0-30) ─────────────────────────────────────────────────────
    vol_not   = contract.vol_notional
    vol_oi    = contract.vol_oi_ratio
    grade     = contract.conviction_grade

    # Notional quality: $100k=10, $250k=16, $500k=22, $1M+=28
    if vol_not >= 1_000_000:
        notional_pts = 28
    elif vol_not >= 500_000:
        notional_pts = 22
    elif vol_not >= 250_000:
        notional_pts = 16
    else:
        notional_pts = 10

    # Vol/OI ratio: 5x=2, 10x=4, 20x+=6
    voi_pts = min(6, round(math.log10(max(vol_oi, 1)) * 4))

    # Conviction grade bonus
    grade_pts = {"A": 4, "B": 2, "C": 0}.get(grade, 0)

    flow_score = min(30, notional_pts + voi_pts + grade_pts)

    # ── Structure score (0-30) ────────────────────────────────────────────────
    sell_delta   = abs(short.delta or 0)
    otm_pct      = abs(short.strike - underlying_price) / underlying_price * 100

    # OTM distance: 5%+ OTM is safe territory
    if otm_pct >= 7:
        otm_pts = 12
    elif otm_pct >= 5:
        otm_pts = 10
    elif otm_pct >= 3:
        otm_pts = 7
    else:
        otm_pts = 3

    # DTE quality: 7-14 DTE is ideal for theta capture + defined risk
    if 7 <= dte <= 14:
        dte_pts = 10
    elif 5 <= dte <= 21:
        dte_pts = 7
    else:
        dte_pts = 3

    # Delta positioning: closer to 0.15 = better structure
    delta_dist  = abs(sell_delta - 0.15)
    delta_pts   = round(max(0, 8 - delta_dist * 40))

    structure_score = min(30, otm_pts + dte_pts + delta_pts)

    # ── Probability score (0-20) ──────────────────────────────────────────────
    # Win prob ≈ 1 - delta. Delta 0.10 → 90% → 20pts; 0.30 → 70% → 10pts
    win_prob    = 1.0 - sell_delta
    prob_score  = round(win_prob * 22)   # 0.90 → ~20, 0.70 → ~15
    prob_score  = min(20, prob_score)

    # ── Historical score (0-20) ───────────────────────────────────────────────
    hist_score = _historical_score(ticker, short.option_type)

    total = flow_score + structure_score + prob_score + hist_score

    return SpreadScoreBreakdown(
        flow_score        = flow_score,
        structure_score   = structure_score,
        probability_score = prob_score,
        historical_score  = hist_score,
        total             = total,
    )


# ── Structure notes ────────────────────────────────────────────────────────────

def _structure_notes(
    spread_type: str,
    sell_strike: float,
    underlying_price: float,
    dte: int,
) -> list[str]:
    notes = []
    otm_pct = abs(sell_strike - underlying_price) / underlying_price * 100

    if spread_type == "Bull Put Spread":
        notes.append(f"Sell strike {otm_pct:.1f}% below current price")
        notes.append("Bullish flow dominates — put selling detected")
        if otm_pct >= 5:
            notes.append("Wide buffer above support level")
    else:
        notes.append(f"Sell strike {otm_pct:.1f}% above current price")
        notes.append("Bearish flow dominates — call selling detected")
        if otm_pct >= 5:
            notes.append("Wide buffer below resistance level")

    if dte <= 7:
        notes.append(f"Short DTE ({dte}d) — fast theta burn")
    elif dte <= 14:
        notes.append(f"Ideal DTE ({dte}d) — balanced theta + time buffer")
    else:
        notes.append(f"DTE {dte}d — more time for spread to decay")

    return notes


# ── Main entry point ───────────────────────────────────────────────────────────

async def generate_credit_spread(
    ticker: str,
    flow_alerts: list[dict],
) -> Optional[CreditSpreadResult]:
    """
    Generate a credit spread recommendation for a ticker.

    flow_alerts: list of scanner alert dicts for this ticker
                 Each has keys: contract (OptionContract), bias (str),
                 underlying_price (float)

    Returns CreditSpreadResult with verdict="TAKE" / "SKIP", or None if
    no spread could be built at all (data unavailable).
    """
    if not flow_alerts:
        return None

    # Step 1: Aggregate bias
    bias, best_alert = _aggregate_bias(flow_alerts)
    if bias == "MIXED" or not best_alert:
        return CreditSpreadResult(
            ticker=ticker, spread_type="N/A", bias="MIXED",
            sell_strike=0, buy_strike=0, expiration="", dte=0,
            premium=0, max_risk=0, win_probability=0,
            flow=FlowConfirmation(
                description="Mixed flow — no directional edge",
                vol_oi_ratio=0, vol_notional=0, conviction_grade="N/A", tags=[],
            ),
            structure=StructureContext(
                sell_strike_otm_pct=0, dte=0, expiration="",
                delta_at_sell=0, notes=["Mixed flow — no trade"],
            ),
            score=SpreadScoreBreakdown(
                flow_score=0, structure_score=0,
                probability_score=0, historical_score=0, total=0,
            ),
            verdict="SKIP",
            reject_reason="No clear directional bias (flow is mixed)",
        )

    underlying_price = best_alert.get("underlying_price", 0) or 0

    # Step 2: Fetch live options chain
    try:
        price, _, contracts = await _fetch_chain(ticker)
        if price and price > 0:
            underlying_price = price
    except Exception as exc:
        logger.warning("Credit spread: chain fetch failed for %s: %s", ticker, exc)
        return None

    if not contracts:
        return None

    # Step 3: Find best expiration
    expiration = _best_expiration(contracts)
    if not expiration:
        return CreditSpreadResult(
            ticker=ticker, spread_type="N/A", bias=bias,
            sell_strike=0, buy_strike=0, expiration="", dte=0,
            premium=0, max_risk=0, win_probability=0,
            flow=FlowConfirmation(
                description="No suitable expiration found",
                vol_oi_ratio=0, vol_notional=0, conviction_grade="N/A", tags=[],
            ),
            structure=StructureContext(
                sell_strike_otm_pct=0, dte=0, expiration="",
                delta_at_sell=0, notes=[f"No expiration in {MIN_DTE}-{MAX_DTE} DTE range"],
            ),
            score=SpreadScoreBreakdown(
                flow_score=0, structure_score=0,
                probability_score=0, historical_score=0, total=0,
            ),
            verdict="SKIP",
            reject_reason=f"No expiration found within {MIN_DTE}-{MAX_DTE} DTE",
        )

    dte = _dte(expiration)

    # Step 4: Select strikes
    if bias == "BULLISH":
        spread_type = "Bull Put Spread"
        result = _select_put_spread(contracts, expiration, underlying_price, ticker)
        option_type_sold = "put"
    else:
        spread_type = "Bear Call Spread"
        result = _select_call_spread(contracts, expiration, underlying_price, ticker)
        option_type_sold = "call"

    if not result:
        return CreditSpreadResult(
            ticker=ticker, spread_type=spread_type, bias=bias,
            sell_strike=0, buy_strike=0, expiration=expiration, dte=dte,
            premium=0, max_risk=0, win_probability=0,
            flow=FlowConfirmation(
                description="No qualifying strike found",
                vol_oi_ratio=0, vol_notional=0, conviction_grade="N/A", tags=[],
            ),
            structure=StructureContext(
                sell_strike_otm_pct=0, dte=dte, expiration=expiration,
                delta_at_sell=0,
                notes=[f"No strike with delta {SELL_DELTA_MIN}-{SELL_DELTA_MAX} available"],
            ),
            score=SpreadScoreBreakdown(
                flow_score=0, structure_score=0,
                probability_score=0, historical_score=0, total=0,
            ),
            verdict="SKIP",
            reject_reason=f"No {option_type_sold} strike at delta {SELL_DELTA_MIN}-{SELL_DELTA_MAX}",
        )

    short_contract, buy_strike = result
    sell_strike  = short_contract.strike
    sell_delta   = abs(short_contract.delta or 0)
    win_prob_pct = round((1.0 - sell_delta) * 100, 1)

    # Step 5: Premium validation
    width      = _spread_width(underlying_price, ticker)
    net_credit = _net_credit(short_contract, contracts, buy_strike)

    def _skip(reason: str, premium: float = 0) -> CreditSpreadResult:
        contract = best_alert["contract"]
        return CreditSpreadResult(
            ticker=ticker, spread_type=spread_type, bias=bias,
            sell_strike=sell_strike, buy_strike=buy_strike,
            expiration=expiration, dte=dte,
            premium=round(premium, 2),
            max_risk=round(width - premium, 2),
            win_probability=win_prob_pct,
            flow=FlowConfirmation(
                description=f"{bias.title()} flow detected",
                vol_oi_ratio=round(contract.vol_oi_ratio, 2),
                vol_notional=round(contract.vol_notional, 0),
                conviction_grade=contract.conviction_grade,
                tags=contract.reason_tags,
            ),
            structure=StructureContext(
                sell_strike_otm_pct=round(
                    abs(sell_strike - underlying_price) / underlying_price * 100, 2
                ),
                dte=dte, expiration=expiration,
                delta_at_sell=round(sell_delta, 3),
                notes=_structure_notes(spread_type, sell_strike, underlying_price, dte),
            ),
            score=SpreadScoreBreakdown(
                flow_score=0, structure_score=0,
                probability_score=0, historical_score=0, total=0,
            ),
            verdict="SKIP",
            reject_reason=reason,
        )

    if net_credit <= 0:
        return _skip("Could not determine net credit (pricing data missing)", net_credit)

    if net_credit < MIN_PREMIUM:
        return _skip(
            f"Premium too low: ${net_credit:.2f} (min ${MIN_PREMIUM:.2f})",
            net_credit,
        )

    max_risk = round(width - net_credit, 2)
    if max_risk <= 0:
        return _skip("Invalid spread: max risk is non-positive", net_credit)

    risk_ratio = max_risk / net_credit
    if risk_ratio > MAX_RISK_RATIO:
        return _skip(
            f"Risk:reward unfavorable ({risk_ratio:.1f}x, max {MAX_RISK_RATIO}x)",
            net_credit,
        )

    # Step 6: Score
    score = _score_trade(
        short=short_contract,
        net_credit=net_credit,
        dte=dte,
        flow_alert=best_alert,
        underlying_price=underlying_price,
        ticker=ticker,
    )

    verdict = "TAKE" if score.total >= MIN_SCORE else "SKIP"
    reject_reason = None if verdict == "TAKE" else f"Score {score.total}/100 below threshold ({MIN_SCORE})"

    contract = best_alert["contract"]
    otm_pct  = abs(sell_strike - underlying_price) / underlying_price * 100

    # Flow confirmation
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
        flow=FlowConfirmation(
            description=flow_desc,
            vol_oi_ratio=round(contract.vol_oi_ratio, 2),
            vol_notional=round(contract.vol_notional, 0),
            conviction_grade=contract.conviction_grade,
            tags=contract.reason_tags,
        ),
        structure=StructureContext(
            sell_strike_otm_pct=round(otm_pct, 2),
            dte=dte,
            expiration=expiration,
            delta_at_sell=round(sell_delta, 3),
            notes=_structure_notes(spread_type, sell_strike, underlying_price, dte),
        ),
        score=score,
        verdict=verdict,
        reject_reason=reject_reason,
    )


async def run_spread_scan(scan_result: dict) -> dict:
    """
    Run credit spread engine + LHF classifier across all tickers in a scan result.
    Returns spreads sorted by LHF score (LOW_HANGING_FRUIT first).
    """
    import asyncio

    all_alerts = scan_result.get("alerts", [])
    alerts_by_ticker: dict[str, list[dict]] = {}
    for a in all_alerts:
        t = a["contract"].ticker
        alerts_by_ticker.setdefault(t, []).append(a)

    tasks = {
        ticker: generate_credit_spread(ticker, ticker_alerts)
        for ticker, ticker_alerts in alerts_by_ticker.items()
    }

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    spreads: list[CreditSpreadResult] = []
    rejected: list[dict] = []

    for ticker, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            logger.warning("Spread engine error for %s: %s", ticker, res)
            rejected.append({"ticker": ticker, "reason": str(res)})
            continue
        if res is None:
            rejected.append({"ticker": ticker, "reason": "No data"})
            continue
        if res.verdict == "TAKE":
            # Second-stage LHF filter
            lhf = classify_lhf(res, all_alerts)
            updated = res.model_copy(update={"lhf": lhf})
            spreads.append(updated)
        else:
            rejected.append({
                "ticker": ticker,
                "reason": res.reject_reason or "Score too low",
            })

    # Sort: LOW_HANGING_FRUIT first, then by LHF score
    def _sort_key(s: CreditSpreadResult):
        lhf_score = s.lhf.score.total if s.lhf else 0
        lhf_class = 0 if (s.lhf and s.lhf.classification == "LOW_HANGING_FRUIT") else 1
        return (lhf_class, -lhf_score)

    spreads.sort(key=_sort_key)
    lhf_count = sum(1 for s in spreads if s.lhf and s.lhf.classification == "LOW_HANGING_FRUIT")

    return {
        "spreads":     spreads,
        "rejected":    rejected,
        "total_valid": len(spreads),
        "total_lhf":   lhf_count,
    }


# ── LHF Second-Stage Classifier ───────────────────────────────────────────────
#
# Runs after the base engine produces a TAKE verdict.
# Scores the setup on a stricter 0-100 scale across 5 dimensions:
#   flow_clarity (0-25) — how clean and strong is the directional flow
#   structure_safety (0-25) — how far OTM, DTE window, delta position
#   regime (0-20) — are other tickers confirming the same direction
#   premium_quality (0-10) — credit size and risk:reward
#   historical_edge (0-20) — grade-A rate in signals DB for this setup
#
# Classification:
#   LOW_HANGING_FRUIT   ≥ 80 — high-probability, boring, repeatable
#   VALID_BUT_NOT_EASY  ≥ 60 — passes base filter but has at least one weakness
#   REJECT              < 60 — too much noise or risk for a clean spread

def _lhf_flow_clarity(s: CreditSpreadResult) -> tuple[int, list[str], list[str]]:
    """0-25: directional conviction, Vol/OI, grade quality."""
    flow  = s.flow
    notes: list[str] = []
    mines: list[str] = []

    # Scale existing flow_score (0-30) down to 0-22 base
    base = min(22, round(s.score.flow_score / 30 * 22))

    grade_bonus = {"A": 3, "B": 1, "C": 0}.get(flow.conviction_grade, 0)
    voi_bonus   = 2 if flow.vol_oi_ratio >= 10 else (1 if flow.vol_oi_ratio >= 5 else 0)
    total       = min(25, base + grade_bonus + voi_bonus)

    if total >= 20:
        notes.append(
            f"Grade {flow.conviction_grade} conviction, {flow.vol_oi_ratio:.1f}x Vol/OI"
            + (f" — {_fmt_notional(flow.vol_notional)} flow" if flow.vol_notional >= 100_000 else "")
        )
    elif total >= 14:
        notes.append(f"Decent flow: Grade {flow.conviction_grade}, {flow.vol_oi_ratio:.1f}x Vol/OI")

    if flow.vol_oi_ratio < 2.5:
        mines.append(f"Low Vol/OI ({flow.vol_oi_ratio:.1f}x) — weak conviction signal")
    if flow.conviction_grade not in ("A", "B"):
        mines.append("Grade C or lower conviction — setup is speculative")

    return total, notes, mines


def _lhf_structure_safety(s: CreditSpreadResult) -> tuple[int, list[str], list[str]]:
    """0-25: OTM distance, DTE quality, delta position."""
    struct = s.structure
    otm    = struct.sell_strike_otm_pct
    dte    = struct.dte
    delta  = struct.delta_at_sell
    notes: list[str] = []
    mines: list[str] = []

    # OTM distance (0-15)
    if otm >= 8:   otm_pts = 15
    elif otm >= 6: otm_pts = 12
    elif otm >= 4: otm_pts = 8
    elif otm >= 2.5: otm_pts = 5
    else:          otm_pts = 2

    # DTE quality (0-7)
    if 7 <= dte <= 14:   dte_pts = 7
    elif 5 <= dte <= 21: dte_pts = 5
    else:                dte_pts = 2

    # Delta safety (0-3)
    if delta <= 0.15:   delta_pts = 3
    elif delta <= 0.20: delta_pts = 2
    elif delta <= 0.25: delta_pts = 1
    else:               delta_pts = 0

    total = min(25, otm_pts + dte_pts + delta_pts)

    if otm >= 6:
        notes.append(f"Sell strike {otm:.1f}% OTM — safely outside key levels")
    elif otm >= 4:
        notes.append(f"Sell strike {otm:.1f}% OTM — reasonable buffer")

    if 7 <= dte <= 14:
        notes.append(f"{dte}d DTE — ideal theta decay window")

    if otm < 3.0:
        mines.append(f"Strike only {otm:.1f}% OTM — breakout risk")
    if delta > 0.25:
        mines.append(f"Sell delta {delta:.2f} — too close to ATM")
    if dte < 4:
        mines.append(f"Only {dte}d to expiry — elevated gamma risk")
    if dte > 21:
        mines.append(f"{dte}d DTE — too much time, higher exposure")

    return total, notes, mines


def _lhf_regime(
    s: CreditSpreadResult,
    all_alerts: list[dict],
) -> tuple[int, list[str], list[str]]:
    """0-20: direction consensus across the full scan."""
    notes: list[str] = []
    mines: list[str] = []

    if not all_alerts:
        return 10, ["Regime: no scan data (neutral)"], []

    target = "BULLISH" if "Put" in s.spread_type else "BEARISH"
    opposite = "BEARISH" if target == "BULLISH" else "BULLISH"

    same_tickers: set[str] = set()
    opp_tickers: set[str]  = set()

    for a in all_alerts:
        ticker = a["contract"].ticker
        bias   = a.get("bias", "").upper()
        if target in bias:
            same_tickers.add(ticker)
        elif opposite in bias:
            opp_tickers.add(ticker)

    n_same = len(same_tickers)
    n_opp  = len(opp_tickers)

    if n_same >= 5:
        score = 19
        notes.append(f"Strong regime: {n_same} tickers confirming {target} direction")
    elif n_same >= 3:
        score = 15
        notes.append(f"Moderate regime: {n_same} tickers in {target} direction")
    elif n_same == 2:
        score = 11
        notes.append(f"Weak regime: only {n_same} tickers in {target} direction")
    else:
        score = 7
        notes.append("Isolated signal — no regime confirmation")

    if n_opp >= n_same and n_opp >= 2:
        mines.append(f"Conflicting flow: {n_opp} tickers pointing {opposite}")
        score = max(score - 6, 4)

    return min(20, score), notes, mines


def _lhf_premium_quality(s: CreditSpreadResult) -> tuple[int, list[str], list[str]]:
    """0-10: credit size and risk:reward ratio."""
    credit = s.premium
    risk   = s.max_risk
    width  = credit + risk
    notes: list[str] = []
    mines: list[str] = []

    if credit >= 1.00:   credit_pts = 7
    elif credit >= 0.70: credit_pts = 6
    elif credit >= 0.50: credit_pts = 5
    elif credit >= 0.35: credit_pts = 3
    else:                credit_pts = 1

    rr = risk / credit if credit > 0 else 99
    if rr <= 2.0:   rr_pts = 3
    elif rr <= 2.5: rr_pts = 2
    elif rr <= 3.0: rr_pts = 1
    else:           rr_pts = 0

    total = min(10, credit_pts + rr_pts)

    if credit >= 0.50:
        notes.append(f"${credit:.2f} credit on ${width:.0f}-wide spread — acceptable premium")

    if credit < 0.35:
        mines.append(f"Premium trap: ${credit:.2f} credit is too thin for the risk taken")
    if rr > 3.0:
        mines.append(f"Risk:reward {rr:.1f}:1 — unfavorable")

    return total, notes, mines


def _fmt_notional(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


def classify_lhf(
    spread: CreditSpreadResult,
    all_alerts: list[dict],
) -> LHFResult:
    """
    Second-stage Low Hanging Fruit classifier.
    Only call on spreads with verdict='TAKE'.

    Returns an LHFResult with classification, score breakdown,
    why_easy bullets, landmine flags, and reject_reasons.
    """
    from app.config import settings

    flow_score,   flow_notes,   flow_mines   = _lhf_flow_clarity(spread)
    struct_score, struct_notes, struct_mines = _lhf_structure_safety(spread)
    regime_score, regime_notes, regime_mines = _lhf_regime(spread, all_alerts)
    prem_score,   prem_notes,   prem_mines   = _lhf_premium_quality(spread)

    opt_type = "put" if "Put" in spread.spread_type else "call"
    hist_score = _historical_score(spread.ticker, opt_type)

    total = flow_score + struct_score + regime_score + prem_score + hist_score

    all_mines = flow_mines + struct_mines + regime_mines + prem_mines
    why_easy   = [n for n in (flow_notes + struct_notes + regime_notes + prem_notes) if n]

    lhf_threshold = settings.lhf_min_score

    if total >= lhf_threshold:
        classification = "LOW_HANGING_FRUIT"
        reject_reasons: list[str] = []
    elif total >= 60:
        classification = "VALID_BUT_NOT_EASY"
        reject_reasons = all_mines[:3]  # top reasons it's not easy
    else:
        classification = "REJECT"
        reject_reasons = all_mines or ["Overall score too low for a clean setup"]

    return LHFResult(
        classification = classification,
        score = LHFScoreBreakdown(
            flow_clarity    = flow_score,
            structure_safety = struct_score,
            regime          = regime_score,
            premium_quality = prem_score,
            historical_edge = hist_score,
            total           = total,
        ),
        why_easy       = why_easy,
        landmines      = all_mines,
        reject_reasons = reject_reasons,
    )
