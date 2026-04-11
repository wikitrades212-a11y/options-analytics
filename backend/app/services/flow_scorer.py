"""
Flow Scorer — integrated with the Options Analytics pipeline.

Pure scoring and formatting module. No I/O, no dedup, no Telegram calls.
Deduplication lives upstream in scanner_service (persistent SQLite cooldown).
Called by telegram_service.send_scan_summary to build the single summary message.

Input:  list of scanner alert dicts  {"contract": OptionContract, "bias": str, ...}
Output: formatted Markdown string ready for Telegram
"""

import math
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

# Macro/index instruments — weighted differently in bias calculation
INDEX_TICKERS: Set[str] = {
    "SPY", "QQQ", "IWM", "DIA", "VXX", "SQQQ", "TQQQ", "SH", "PSQ",
}

# Sector groupings — used for leadership detection and Quick Read.
# Rule: put tickers where they TRADE, not where they technically belong.
# TSLA trades like momentum/high-beta, not stable big-tech.
SECTOR_MAP: Dict[str, Set[str]] = {
    "semis":      {"NVDA", "AMD", "INTC", "AVGO", "QCOM", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "TSM", "SMCI"},
    "big_tech":   {"MSFT", "AAPL", "GOOGL", "META", "AMZN", "NFLX", "CRM", "ORCL", "ADBE", "NOW"},
    "momentum":   {"PLTR", "TSLA", "SNOW", "COIN", "MSTR", "DKNG", "HOOD", "RBLX", "UBER", "RDDT", "APP"},
    "financials": {"JPM", "GS", "MS", "BAC", "C", "WFC", "V", "MA", "AXP", "BLK"},
    "energy":     {"XOM", "CVX", "COP", "OXY", "SLB", "HAL"},
    "healthcare": {"UNH", "JNJ", "PFE", "MRNA", "ABBV", "LLY", "BMY", "AMGN"},
}

SECTOR_LABELS: Dict[str, str] = {
    "semis":      "Semis",
    "big_tech":   "Big Tech",
    "momentum":   "Momentum",
    "financials": "Financials",
    "energy":     "Energy",
    "healthcare": "Healthcare",
}


# ──────────────────────────────────────────────────────────────────────────────
# ADAPTER — OptionContract → scorer alert dict
# ──────────────────────────────────────────────────────────────────────────────

def _compute_dte(expiration: str) -> int:
    try:
        return max(0, (date.fromisoformat(expiration) - date.today()).days)
    except (ValueError, TypeError):
        return 0


def _moneyness_str(moneyness_ratio: float | None, option_type: str) -> str:
    """Convert moneyness ratio (strike/spot) to a readable string."""
    if moneyness_ratio is None:
        return ""
    pct = (moneyness_ratio - 1.0) * 100
    if abs(pct) < 1.0:
        return "ATM"
    if option_type == "call":
        return f"+{pct:.1f}% OTM" if pct > 0 else f"{abs(pct):.1f}% ITM"
    # put: OTM when strike < spot (ratio < 1 → pct < 0)
    return f"+{abs(pct):.1f}% OTM" if pct < 0 else f"{pct:.1f}% ITM"


def contracts_to_scorer_alerts(scanner_alerts: list[dict]) -> list[dict]:
    """
    Convert a list of scanner alert dicts (each with an OptionContract) into
    the flat dict format expected by analyze_alert().

    Field mapping
    -------------
    contract.vol_notional      → premium   (dollar flow)
    contract.implied_volatility→ iv        (×100 → percentage)
    contract.unusual_score     → score     (0–100)
    contract.conviction_score  → conviction(0–100)
    alert["bias"]              → sentiment (e.g. "BULLISH AGGRESSIVE")
    contract.reason_tags       → tags
    """
    result = []
    for alert in scanner_alerts:
        c = alert.get("contract")
        if c is None:
            continue
        bias = alert.get("bias", "")
        strike_str = f"{c.strike:.0f}{'C' if c.option_type == 'call' else 'P'}"
        dte = _compute_dte(c.expiration)
        iv  = (c.implied_volatility or 0.0) * 100.0

        result.append({
            "ticker":     c.ticker,
            "strike":     strike_str,
            "premium":    c.vol_notional,
            "volume":     c.volume,
            "oi":         c.open_interest,
            "delta":      c.delta if c.delta is not None else 0.0,
            "iv":         iv,
            "dte":        dte,
            "score":      c.unusual_score,
            "conviction": c.conviction_score,
            "sentiment":  bias,
            "tags":       list(c.reason_tags),
            "moneyness":  _moneyness_str(c.moneyness, c.option_type),
        })
    return result


# ──────────────────────────────────────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def format_money(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


# ──────────────────────────────────────────────────────────────────────────────
# SCORING COMPONENTS
# ──────────────────────────────────────────────────────────────────────────────

def _premium_score(premium: float) -> float:
    """
    Log-scaled, anchored at $100K = 0. Cap at $30M+.
    Differentiates $1M (~15 pts) from $30M (~40 pts) — the old version
    capped out at $1M.

      $100K →  0    $500K → ~10    $1M → ~15
      $5M   → ~26   $10M  → ~32   $30M+ → 40
    """
    if premium <= 0:
        return 0.0
    return max(0.0, min(40.0, (math.log10(premium) - 5.0) * 19.0))


def _vol_oi_score(volume: float, oi: float) -> float:
    """High ratio = fresh positioning (no prior OI absorbing volume)."""
    if oi <= 0:
        return 20.0 if volume > 0 else 0.0
    r = volume / oi
    if r >= 200: return 32.0
    if r >= 100: return 28.0
    if r >= 50:  return 24.0
    if r >= 25:  return 20.0
    if r >= 10:  return 14.0
    if r >= 5:   return 8.0
    if r >= 2:   return 4.0
    return 1.0


def _delta_score(delta: float) -> float:
    """
    Sweet spot 0.40–0.65: near-ATM directional conviction.
    Deep ITM penalized (stock replacement, not a bet).
    Deep OTM penalized (speculation).
    """
    ad = abs(delta)
    if 0.40 <= ad <= 0.65: return 14.0
    if 0.30 <= ad < 0.40:  return 8.0
    if 0.65 < ad <= 0.80:  return 7.0
    if 0.25 <= ad < 0.30:  return 4.0
    if ad > 0.80:          return 4.0
    return 1.0


def _dte_score(dte: int) -> float:
    """Short DTE = urgency. LEAPS (>90d) also rewarded for commitment."""
    if dte <= 2:   return 12.0
    if dte <= 5:   return 10.0
    if dte <= 10:  return 7.0
    if dte <= 21:  return 4.0
    if dte <= 45:  return 2.0
    if dte <= 90:  return 1.0
    return 4.0


def _iv_context_score(iv: float) -> float:
    """
    KEY signal missing from prior version.
    Cheap IV + large premium = institutional conviction in quiet market.
    Expensive IV = momentum / panic — discount accordingly.

      < 15%  → +6    15–24% → +4    25–49% → +2
      50–79% →  0    80%+   → -3
    """
    if iv <= 0:  return 0.0
    if iv < 15:  return 6.0
    if iv < 25:  return 4.0
    if iv < 50:  return 2.0
    if iv < 80:  return 0.0
    return -3.0


def _tag_bonus(tags: List[str]) -> float:
    t = {str(x).strip().lower() for x in tags}
    bonus = 0.0
    if "high vol/oi" in t:             bonus += 8.0
    if "big premium" in t:             bonus += 5.0   # reduced: premium_score handles size
    if "expiry concentration" in t:    bonus += 5.0
    if "call dominance" in t:          bonus += 5.0
    if "put dominance" in t:           bonus += 5.0
    if "near atm aggression" in t:     bonus += 8.0
    if "sweep" in t:                   bonus += 6.0
    if "dark pool" in t:               bonus += 4.0
    if "repeat flow" in t:             bonus += 5.0
    return bonus


def _classify_strength(model_score: float) -> str:
    """
    Recalibrated thresholds.
    Old version (80/60/40) was inflated because external score/conviction
    contributed ~20 fixed points to every alert. Fixed by capping ext at 8pts.
    """
    if model_score >= 70: return "INSTITUTIONAL"
    if model_score >= 50: return "STRONG"
    if model_score >= 30: return "SPECULATIVE"
    return "WEAK"


# ──────────────────────────────────────────────────────────────────────────────
# HEDGE DETECTION
# ──────────────────────────────────────────────────────────────────────────────

def _is_likely_hedge(
    ticker: str, trade_type: str, delta: float, dte: int, premium: float,
    vol_oi: float = 0.0,
) -> bool:
    """
    Index ETF put + large premium + near-ATM + short-dated = portfolio protection.
    Exception: high Vol/OI (> 20x) means fresh aggressive positioning, not a hedge —
    a real hedge rides existing OI, it doesn't blast 20-100x over it.
    """
    if ticker not in INDEX_TICKERS:
        return False
    if trade_type != "PUT":
        return False
    if vol_oi > 20.0:
        return False
    return premium >= 5_000_000 and abs(delta) >= 0.35 and dte <= 21


# ──────────────────────────────────────────────────────────────────────────────
# SECTOR DETECTION
# ──────────────────────────────────────────────────────────────────────────────

def _get_sector(ticker: str) -> Optional[str]:
    for sector, members in SECTOR_MAP.items():
        if ticker in members:
            return sector
    return None


def _detect_sector_dynamics(analyzed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    For non-index tickers with non-WEAK flow, aggregate scores by sector.
    Returns dominant bear/bull sectors and per-sector ticker lists.
    """
    bear_score: Dict[str, float] = defaultdict(float)
    bull_score: Dict[str, float] = defaultdict(float)
    bear_tickers: Dict[str, List[str]] = defaultdict(list)
    bull_tickers: Dict[str, List[str]] = defaultdict(list)

    for a in analyzed:
        if a["strength"] == "WEAK" or a["is_index"]:
            continue
        sector = _get_sector(a["ticker"])
        if sector is None:
            continue
        if a["sentiment"] == "BEARISH":
            bear_score[sector] += a["model_score"]
            if a["ticker"] not in bear_tickers[sector]:
                bear_tickers[sector].append(a["ticker"])
        elif a["sentiment"] == "BULLISH":
            bull_score[sector] += a["model_score"]
            if a["ticker"] not in bull_tickers[sector]:
                bull_tickers[sector].append(a["ticker"])

    top_bear = max(bear_score, key=bear_score.__getitem__) if bear_score else None
    top_bull = max(bull_score, key=bull_score.__getitem__) if bull_score else None

    return {
        "top_bear_sector":      top_bear,
        "top_bull_sector":      top_bull,
        "bear_tickers":         dict(bear_tickers),
        "bull_tickers":         dict(bull_tickers),
        "bear_score_by_sector": dict(bear_score),
        "bull_score_by_sector": dict(bull_score),
    }


# ──────────────────────────────────────────────────────────────────────────────
# ALERT ANALYSIS
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_sentiment(raw: str) -> Tuple[str, bool]:
    s = str(raw).upper().strip()
    aggressive = "AGGRESSIVE" in s
    if "BULLISH" in s:   return "BULLISH", aggressive
    if "BEARISH" in s:   return "BEARISH", aggressive
    return "NEUTRAL", aggressive


def analyze_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    ticker    = str(alert.get("ticker", "")).upper().strip()
    strike    = str(alert.get("strike", "")).strip()
    premium   = _safe_float(alert.get("premium"))
    volume    = _safe_float(alert.get("volume", alert.get("vol", 0)))
    oi        = _safe_float(alert.get("oi"))
    delta     = _safe_float(alert.get("delta"))
    iv        = _safe_float(alert.get("iv"))
    dte       = _safe_int(alert.get("dte"))
    tags      = alert.get("tags", []) or []
    moneyness = str(alert.get("moneyness", "")).strip()

    # External scores are often maxed at 100 — cap contribution to prevent
    # inflating everything to INSTITUTIONAL regardless of actual signal quality.
    ext_score      = min(_safe_float(alert.get("score")), 100.0) / 25.0      # max 4 pts
    ext_conviction = min(_safe_float(alert.get("conviction")), 100.0) / 25.0 # max 4 pts

    sentiment, aggressive = _normalize_sentiment(alert.get("sentiment", ""))

    trade_type = "CALL" if delta > 0 else "PUT"
    if strike.upper().endswith("C"):
        trade_type = "CALL"
    elif strike.upper().endswith("P"):
        trade_type = "PUT"

    vol_oi = (volume / oi) if oi > 0 else (volume if volume > 0 else 0.0)

    p_sc  = _premium_score(premium)
    vo_sc = _vol_oi_score(volume, oi)
    d_sc  = _delta_score(delta)
    t_sc  = _dte_score(dte)
    iv_sc = _iv_context_score(iv)
    tb_sc = _tag_bonus(tags)
    ag_sc = 5.0 if aggressive else 0.0

    model_score = round(max(0.0,
        p_sc + vo_sc + d_sc + t_sc + iv_sc + tb_sc + ag_sc
        + ext_score + ext_conviction
    ), 2)

    strength = _classify_strength(model_score)
    is_hedge = _is_likely_hedge(ticker, trade_type, delta, dte, premium, vol_oi)
    is_index = ticker in INDEX_TICKERS

    emoji = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(sentiment, "⚪")

    note_parts = []
    if premium >= 10_000_000:          note_parts.append("huge premium")
    elif premium >= 1_000_000:         note_parts.append("large premium")
    if vol_oi >= 25:                   note_parts.append("fresh positioning")
    elif vol_oi >= 10:                 note_parts.append("strong vol/OI")
    if 0.40 <= abs(delta) <= 0.65:    note_parts.append("conviction delta")
    elif abs(delta) >= 0.25:          note_parts.append("tradable delta")
    if dte <= 3:                       note_parts.append("urgent expiry")
    elif dte <= 7:                     note_parts.append("short-dated")
    if 0 < iv < 20 and premium >= 500_000:
                                       note_parts.append("cheap-IV conviction")
    elif iv >= 80:                     note_parts.append("high-IV caution")
    if aggressive:                     note_parts.append("aggressive")
    if is_hedge:                       note_parts.append("likely hedge")
    summary_note = ", ".join(note_parts) if note_parts else "flow detected"

    return {
        "ticker":      ticker,
        "strike":      strike,
        "premium":     premium,
        "volume":      volume,
        "oi":          oi,
        "vol_oi":      vol_oi,
        "delta":       delta,
        "iv":          iv,
        "dte":         dte,
        "sentiment":   sentiment,
        "aggressive":  aggressive,
        "trade_type":  trade_type,
        "moneyness":   moneyness,
        "tags":        tags,
        "model_score": model_score,
        "strength":    strength,
        "emoji":       emoji,
        "note":        summary_note,
        "is_hedge":    is_hedge,
        "is_index":    is_index,
    }


# ──────────────────────────────────────────────────────────────────────────────
# RANKING
# ──────────────────────────────────────────────────────────────────────────────

def _rank(alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        alerts,
        key=lambda x: (
            x["model_score"], x["premium"], x["vol_oi"],
            abs(x["delta"]), -x["dte"],
        ),
        reverse=True,
    )


def _strength_order(s: str) -> int:
    return {"INSTITUTIONAL": 3, "STRONG": 2, "SPECULATIVE": 1, "WEAK": 0}.get(s, 0)


def _top_overall(
    analyzed: List[Dict[str, Any]], limit: int = 5, min_strength: str = "SPECULATIVE"
) -> List[Dict[str, Any]]:
    threshold = _strength_order(min_strength)
    return _rank([a for a in analyzed if _strength_order(a["strength"]) >= threshold])[:limit]


def _top_bulls(analyzed: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    return _rank([
        a for a in analyzed
        if a["sentiment"] == "BULLISH" and a["strength"] != "WEAK"
    ])[:limit]


def _top_bears(analyzed: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    return _rank([
        a for a in analyzed
        if a["sentiment"] == "BEARISH" and a["strength"] != "WEAK"
    ])[:limit]


# ──────────────────────────────────────────────────────────────────────────────
# MARKET BIAS
# ──────────────────────────────────────────────────────────────────────────────

def _build_bias(analyzed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Improvements over the original:
    - Hedges: 0.25× weight (portfolio protection, not directional)
    - Index non-hedge: 1.2× (broad market signal)
    - Single-name INSTITUTIONAL: 1.5× (high-conviction directional)
    - WEAK alerts excluded
    - Confidence: clean 0–100 split (no artificial +50 floor)
    - Divergent tape detection: macro bears vs single-name bulls (or vice versa)
    """
    bull_score, bear_score = 0.0, 0.0
    bull_names: List[str] = []
    bear_names: List[str] = []
    hedge_names: List[str] = []

    for a in analyzed:
        if a["strength"] == "WEAK":
            continue

        w = a["model_score"]
        if a["is_hedge"]:
            w *= 0.25
            if a["ticker"] not in hedge_names:
                hedge_names.append(a["ticker"])
        elif a["is_index"]:
            w *= 1.2
        elif a["strength"] == "INSTITUTIONAL":
            w *= 1.5

        if a["sentiment"] == "BULLISH":
            bull_score += w
            if a["ticker"] not in bull_names:
                bull_names.append(a["ticker"])
        elif a["sentiment"] == "BEARISH":
            bear_score += w
            if a["ticker"] not in bear_names:
                bear_names.append(a["ticker"])

    total = bull_score + bear_score
    if total == 0:
        return {
            "label": "MIXED", "bull_score": 0.0, "bear_score": 0.0,
            "bull_pct": 50, "bear_pct": 50, "confidence": 0,
            "bull_names": [], "bear_names": [], "hedge_names": [],
            "divergent": False,
        }

    diff       = bull_score - bear_score
    confidence = int(abs(diff) / total * 100)
    bull_pct   = int(bull_score / total * 100)

    if diff > total * 0.25:
        label = "BULLISH"
    elif diff < -total * 0.25:
        label = "BEARISH"
    else:
        label = "MIXED"

    macro_bears = [
        a for a in analyzed
        if a["is_index"] and a["sentiment"] == "BEARISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG") and not a["is_hedge"]
    ]
    macro_bulls = [
        a for a in analyzed
        if a["is_index"] and a["sentiment"] == "BULLISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
    ]
    single_bulls = [
        a for a in analyzed
        if not a["is_index"] and a["sentiment"] == "BULLISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
    ]
    single_bears = [
        a for a in analyzed
        if not a["is_index"] and a["sentiment"] == "BEARISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
    ]
    divergent = bool(
        (macro_bears and single_bulls) or (macro_bulls and single_bears)
    )

    # Macro override: only fires when BOTH primary index (SPY/QQQ) AND a major
    # sector (semis) are STRONG/INSTITUTIONAL in the same direction.
    # Weak or speculative signals do not qualify — they stay as rotational/divergent.
    _PRIMARY = {"SPY", "QQQ"}
    _primary_bear = any(
        a["ticker"] in _PRIMARY and a["sentiment"] == "BEARISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG") and not a["is_hedge"]
        for a in analyzed
    )
    _primary_bull = any(
        a["ticker"] in _PRIMARY and a["sentiment"] == "BULLISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
        for a in analyzed
    )
    _semis_bear = any(
        a["ticker"] in SECTOR_MAP["semis"] and a["sentiment"] == "BEARISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
        for a in analyzed
    )
    _semis_bull = any(
        a["ticker"] in SECTOR_MAP["semis"] and a["sentiment"] == "BULLISH"
        and a["strength"] in ("INSTITUTIONAL", "STRONG")
        for a in analyzed
    )
    macro_override_bear = _primary_bear and _semis_bear
    macro_override_bull = _primary_bull and _semis_bull
    macro_override = macro_override_bear or macro_override_bull

    if macro_override_bear and label in ("MIXED", "BULLISH"):
        label = "BEARISH"
    elif macro_override_bull and label in ("MIXED", "BEARISH"):
        label = "BULLISH"

    # Refined second-layer bias label
    index_bearish = bool(macro_bears)
    index_bullish = bool(macro_bulls)
    has_strong_single_bulls = bool(single_bulls)
    has_strong_puts = bool(hedge_names) or bool(single_bears)

    if label == "BEARISH":
        if has_strong_single_bulls:
            refined_label = "BEARISH WITH ROTATIONAL DIVERGENCE"
        else:
            refined_label = "BEARISH"
    elif label == "BULLISH":
        if has_strong_puts:
            refined_label = "BULLISH WITH HEDGING"
        else:
            refined_label = "BULLISH"
    else:  # MIXED
        if index_bearish and has_strong_single_bulls:
            refined_label = "BEARISH WITH ROTATIONAL DIVERGENCE"
        elif index_bullish and has_strong_puts:
            refined_label = "BULLISH WITH HEDGING"
        else:
            refined_label = "RANGE / CHOP"

    return {
        "label":          label,
        "refined_label":  refined_label,
        "bull_score":     round(bull_score, 1),
        "bear_score":     round(bear_score, 1),
        "bull_pct":       bull_pct,
        "bear_pct":       100 - bull_pct,
        "confidence":     confidence,
        "bull_names":     bull_names,
        "bear_names":     bear_names,
        "hedge_names":    hedge_names,
        "divergent":      divergent,
        "index_bearish":  index_bearish,
        "index_bullish":  index_bullish,
        "macro_override": macro_override,
    }


# ──────────────────────────────────────────────────────────────────────────────
# MARKET STRUCTURE — propagation / rotation classification
# ──────────────────────────────────────────────────────────────────────────────

def _classify_market_structure(
    actionable: List[Dict[str, Any]],
    bias: Dict[str, Any],
    sector_dynamics: Dict[str, Any],
) -> Dict[str, List[str]]:
    """
    Assign each non-index STRONG/INSTITUTIONAL ticker a propagation role:

    Bearish context
      first_to_move  — bearish names in the dominant weak sector (drop first)
      holders        — bullish names in neutral/strong sectors (resist early)
      late_movers    — bullish names in split/weak sectors (hold then roll)

    Bullish context mirrors the logic.
    RANGE/CHOP: just split bears vs bulls, no late-mover signal.
    """
    refined       = bias.get("refined_label", bias["label"])
    macro_bearish = "BEARISH" in refined
    macro_bullish = "BULLISH" in refined and not macro_bearish

    top_bear_sector = sector_dynamics.get("top_bear_sector")
    top_bull_sector = sector_dynamics.get("top_bull_sector")

    # Sectors with meaningful flow in BOTH directions
    split_sectors: Set[str] = {
        s for s in sector_dynamics.get("bear_tickers", {})
        if s in sector_dynamics.get("bull_tickers", {})
        and sector_dynamics["bear_score_by_sector"].get(s, 0) > 0
        and sector_dynamics["bull_score_by_sector"].get(s, 0) > 0
    }

    candidates = sorted(
        [
            a for a in actionable
            if not a["is_index"] and a["strength"] in ("INSTITUTIONAL", "STRONG")
        ],
        key=lambda x: x["model_score"],
        reverse=True,
    )

    first_to_move: List[str] = []
    holders:       List[str] = []
    late_movers:   List[str] = []
    seen:          Set[str]  = set()

    if macro_bearish:
        # First to drop — bearish names, weak sector first
        bear_cands = sorted(
            [a for a in candidates if a["sentiment"] == "BEARISH"],
            key=lambda a: (0 if _get_sector(a["ticker"]) == top_bear_sector else 1,
                           -a["model_score"]),
        )
        for a in bear_cands:
            if a["ticker"] not in seen and len(first_to_move) < 3:
                first_to_move.append(a["ticker"])
                seen.add(a["ticker"])

        # Bullish names: split into clean holders vs late-weakness candidates
        for a in candidates:
            if a["sentiment"] != "BULLISH" or a["ticker"] in seen:
                continue
            sector = _get_sector(a["ticker"])
            if sector in split_sectors or sector == top_bear_sector:
                # Holding now, but sector is already cracking → will roll later
                if len(late_movers) < 3:
                    late_movers.append(a["ticker"])
                    seen.add(a["ticker"])
            else:
                # Genuinely neutral/strong sector — clean relative strength hold
                if len(holders) < 3:
                    holders.append(a["ticker"])
                    seen.add(a["ticker"])

    elif macro_bullish:
        # First to pop — bullish names, strong sector first
        bull_cands = sorted(
            [a for a in candidates if a["sentiment"] == "BULLISH"],
            key=lambda a: (0 if _get_sector(a["ticker"]) == top_bull_sector else 1,
                           -a["model_score"]),
        )
        for a in bull_cands:
            if a["ticker"] not in seen and len(first_to_move) < 3:
                first_to_move.append(a["ticker"])
                seen.add(a["ticker"])

        # Bearish names: split into persistent shorts vs late-fade candidates
        for a in candidates:
            if a["sentiment"] != "BEARISH" or a["ticker"] in seen:
                continue
            sector = _get_sector(a["ticker"])
            if sector in split_sectors or sector == top_bull_sector:
                if len(late_movers) < 3:
                    late_movers.append(a["ticker"])
                    seen.add(a["ticker"])
            else:
                if len(holders) < 3:
                    holders.append(a["ticker"])
                    seen.add(a["ticker"])

    else:  # RANGE / CHOP — no propagation thesis, just split the tape
        for a in candidates:
            t = a["ticker"]
            if t in seen:
                continue
            if a["sentiment"] == "BEARISH" and len(first_to_move) < 2:
                first_to_move.append(t)
                seen.add(t)
            elif a["sentiment"] == "BULLISH" and len(holders) < 2:
                holders.append(t)
                seen.add(t)

    return {
        "first_to_move": first_to_move,
        "holders":       holders,
        "late_movers":   late_movers,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GAME PLAN
# ──────────────────────────────────────────────────────────────────────────────

def _build_gameplan(
    bias: Dict[str, Any],
    bulls: List[Dict[str, Any]],
    bears: List[Dict[str, Any]],
    sector_dynamics: Dict[str, Any],
    market_structure: Dict[str, List[str]],
) -> List[str]:
    plan: List[str] = []
    top_bull = bulls[0]["ticker"] if bulls else None
    top_bear = bears[0]["ticker"] if bears else None
    refined  = bias.get("refined_label", bias["label"])

    top_bear_sector   = sector_dynamics.get("top_bear_sector")
    top_bull_sector   = sector_dynamics.get("top_bull_sector")
    bear_sector_label = SECTOR_LABELS.get(top_bear_sector, "") if top_bear_sector else ""
    bull_sector_label = SECTOR_LABELS.get(top_bull_sector, "") if top_bull_sector else ""

    first_to_move = market_structure.get("first_to_move", [])
    holders       = market_structure.get("holders", [])
    late_movers   = market_structure.get("late_movers", [])

    def _tickers(lst: List[str]) -> str:
        return "/".join(f"*{t}*" for t in lst)

    # ── PRIMARY ──────────────────────────────────────────────────────────────
    if "BEARISH" in refined:
        if bear_sector_label and top_bear:
            macro_ctx = f"{bear_sector_label} + index pressure"
        elif top_bear:
            macro_ctx = f"macro pressure via *{top_bear}*"
        else:
            macro_ctx = "dominant put flow"
        plan.append(f"▸ *Primary:* Sell strength — {macro_ctx}")

    elif "BULLISH" in refined:
        if bull_sector_label and top_bull:
            macro_ctx = f"{bull_sector_label} + index bid"
        elif top_bull:
            macro_ctx = f"upside momentum via *{top_bull}*"
        else:
            macro_ctx = "dominant call flow"
        plan.append(f"▸ *Primary:* Buy dips — {macro_ctx}")

    else:
        plan.append("▸ *Primary:* No macro conviction — wait for directional confirmation")

    # ── SECONDARY ────────────────────────────────────────────────────────────
    if "ROTATIONAL DIVERGENCE" in refined and holders:
        # Only show sector label if the holders actually belong to that sector
        holders_in_sector = [t for t in holders if _get_sector(t) == top_bull_sector]
        bl = f" ({bull_sector_label})" if bull_sector_label and holders_in_sector else ""
        plan.append(
            f"▸ *Secondary:* Relative strength in {_tickers(holders[:2])}{bl} — "
            "quick longs only, not a macro confirmation"
        )
    elif "ROTATIONAL DIVERGENCE" in refined and top_bull:
        plan.append(
            f"▸ *Secondary:* Relative strength in *{top_bull}* — "
            "quick longs only, not a macro confirmation"
        )
    elif "HEDGING" in refined and top_bear:
        plan.append(
            f"▸ *Secondary:* Hedge pressure via *{top_bear}* — "
            "confirm index hold before chasing upside"
        )
    elif refined == "RANGE / CHOP":
        if top_bull and top_bear:
            plan.append(
                f"▸ *Secondary:* *{top_bull}* = upside leader, *{top_bear}* = downside leader — "
                "trade the spread, not direction"
            )

    # ── EXECUTION (timing-aware) ──────────────────────────────────────────────
    exec_steps: List[str] = []

    if "BEARISH" in refined:
        if first_to_move:
            exec_steps.append(
                f"Short {_tickers(first_to_move)} on breakdown — weak sector leads"
            )
        elif top_bear:
            exec_steps.append(f"Wait for breakdown confirmation on *{top_bear}*")

        if holders:
            exec_steps.append(
                f"Do NOT short {_tickers(holders)} early — let relative strength exhaust first"
            )
        else:
            exec_steps.append("Avoid chasing index longs while put flow dominates")

        if late_movers:
            exec_steps.append(
                f"Fade {_tickers(late_movers)} after they lose relative strength vs index"
            )

    elif "BULLISH" in refined:
        if first_to_move:
            exec_steps.append(
                f"Enter {_tickers(first_to_move)} on first pullback — strong sector leads"
            )
        elif top_bull:
            exec_steps.append(f"Enter *{top_bull}* on pullback — not extended move")

        if holders:
            exec_steps.append(
                f"Do NOT short {_tickers(holders)} — they resist until macro confirms turn"
            )
        elif "HEDGING" in refined:
            exec_steps.append("Stay nimble — institutional hedges signal risk awareness")
        else:
            exec_steps.append("Lean with call flow. Don't force shorts into strength")

        if late_movers:
            exec_steps.append(
                f"{_tickers(late_movers)} may catch bid later — wait for index to confirm first"
            )

    else:  # RANGE / CHOP
        if first_to_move and holders:
            exec_steps.append(
                f"Trade the spread: short {_tickers(first_to_move)}, long {_tickers(holders)}"
            )
        exec_steps.append("Wait for opening range before committing size")

    if exec_steps:
        plan.append("▸ *Execution:*")
        for step in exec_steps:
            plan.append(f"  — {step}")

    return plan


# ──────────────────────────────────────────────────────────────────────────────
# FORMATTING
# ──────────────────────────────────────────────────────────────────────────────

def _one_line(a: Dict[str, Any]) -> str:
    aggr  = " AGGR" if a["aggressive"] else ""
    hedge = " HDG"  if a["is_hedge"]   else ""
    iv_s  = f" IV:{a['iv']:.0f}%" if a["iv"] > 0 else ""
    return (
        f"{a['emoji']} *{a['ticker']}* {a['strike']}{aggr}{hedge}  "
        f"| {format_money(a['premium'])}{iv_s}  "
        f"| Vol/OI {a['vol_oi']:.1f}x  "
        f"| Δ {a['delta']:.2f}  "
        f"| DTE {a['dte']}  "
        f"| {a['strength']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# QUICK READ ONE-LINER
# ──────────────────────────────────────────────────────────────────────────────

def _build_quick_read_summary(
    bias: Dict[str, Any],
    sector_dynamics: Dict[str, Any],
) -> str:
    """
    Generate a one-line human-readable tape description.
    Example: "Index hedging + semiconductor weakness + selective tech strength"
    """
    parts: List[str] = []

    if bias.get("hedge_names"):
        parts.append("index hedging")

    top_bear = sector_dynamics.get("top_bear_sector")
    top_bull = sector_dynamics.get("top_bull_sector")

    if top_bear:
        parts.append(f"{SECTOR_LABELS.get(top_bear, top_bear).lower()} weakness")

    if top_bull and top_bull != top_bear:
        parts.append(f"selective {SECTOR_LABELS.get(top_bull, top_bull).lower()} strength")
    elif not top_bull and bias.get("bull_names"):
        parts.append("isolated long-side flow")

    if not parts:
        # Fall back to a plain description of the refined label
        return bias.get("refined_label", bias["label"]).replace("_", " ").lower()

    return " + ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def build_summary_message(scorer_alerts: List[Dict[str, Any]]) -> str:
    """
    Build the full Telegram summary from a list of scorer-format alert dicts.

    Returns an empty string if there is nothing actionable to send.
    Deduplication is NOT performed here — the scanner's persistent cooldown
    (scanner_service.py) already gates what reaches this function.
    """
    analyzed = [analyze_alert(a) for a in scorer_alerts if a.get("ticker")]
    if not analyzed:
        return ""

    # Drop WEAK — not worth surfacing in the summary
    actionable = [a for a in analyzed if a["strength"] != "WEAK"]
    if not actionable:
        return ""

    bias             = _build_bias(actionable)
    sector_dynamics  = _detect_sector_dynamics(actionable)
    market_structure = _classify_market_structure(actionable, bias, sector_dynamics)
    top_overall      = _top_overall(actionable, limit=5)
    top_bulls        = _top_bulls(actionable, limit=3)
    top_bears        = _top_bears(actionable, limit=3)
    plan             = _build_gameplan(bias, top_bulls, top_bears, sector_dynamics, market_structure)
    quick_read_line  = _build_quick_read_summary(bias, sector_dynamics)

    refined_label = bias.get("refined_label", bias["label"])
    bias_emoji    = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(bias["label"], "🟡")
    override_tag  = " ⚠️MACRO OVERRIDE" if bias.get("macro_override") else ""

    # Leader tickers for header emphasis
    downside_leader = top_bears[0]["ticker"] if top_bears else None
    upside_leader   = top_bulls[0]["ticker"] if top_bulls else None

    lines: List[str] = []

    # ── Header ───────────────────────────────────────────────────────────────
    lines.append(f"{bias_emoji} *MARKET BIAS: {refined_label}{override_tag}*")
    lines.append(
        f"Bear {bias['bear_pct']}% vs Bull {bias['bull_pct']}%  "
        f"|  Confidence: {bias['confidence']}/100"
    )
    leader_parts = []
    if downside_leader:
        leader_parts.append(f"📉 *{downside_leader}*")
    if upside_leader:
        leader_parts.append(f"📈 *{upside_leader}*")
    if leader_parts:
        lines.append("  ".join(leader_parts))
    lines.append("")

    # ── Top Overall ──────────────────────────────────────────────────────────
    if top_overall:
        lines.append("*Top Overall Flow*")
        for i, play in enumerate(top_overall, start=1):
            lines.append(f"{i}. {_one_line(play)}")
        lines.append("")

    # ── Top Bears ────────────────────────────────────────────────────────────
    if top_bears:
        lines.append("*Top Bears*")
        for play in top_bears:
            lines.append(f"• {_one_line(play)}")
        lines.append("")

    # ── Top Bulls ────────────────────────────────────────────────────────────
    if top_bulls:
        lines.append("*Top Bulls*")
        for play in top_bulls:
            lines.append(f"• {_one_line(play)}")
        lines.append("")

    # ── Market Structure ──────────────────────────────────────────────────────
    macro_bearish_ms = "BEARISH" in refined_label
    ms = market_structure
    if ms["first_to_move"] or ms["holders"] or ms["late_movers"]:
        lines.append("*Market Structure*")
        if ms["first_to_move"]:
            names = ", ".join(f"*{t}*" for t in ms["first_to_move"])
            icon  = "📉" if macro_bearish_ms else "📈"
            lbl   = "First to drop" if macro_bearish_ms else "First to pop"
            lines.append(f"• {icon} *{lbl}:* {names}")
        if ms["holders"]:
            names = ", ".join(f"*{t}*" for t in ms["holders"])
            lbl   = "Holding strength" if macro_bearish_ms else "Lagging shorts"
            lines.append(f"• 📈 *{lbl}:* {names}")
        if ms["late_movers"]:
            names = ", ".join(f"*{t}*" for t in ms["late_movers"])
            lbl   = "Likely to roll later" if macro_bearish_ms else "Likely to catch bid later"
            lines.append(f"• ⚠️ *{lbl}:* {names}")
        lines.append("")

    # ── Sector Leadership ─────────────────────────────────────────────────────
    sector_lines: List[str] = []
    top_bear_s = sector_dynamics.get("top_bear_sector")
    top_bull_s = sector_dynamics.get("top_bull_sector")
    if top_bear_s:
        tickers_str = ", ".join(sector_dynamics["bear_tickers"].get(top_bear_s, []))
        lbl = SECTOR_LABELS.get(top_bear_s, top_bear_s)
        sector_lines.append(f"📉 *{lbl} weak* — {tickers_str}")
    if top_bull_s and top_bull_s != top_bear_s:
        tickers_str = ", ".join(sector_dynamics["bull_tickers"].get(top_bull_s, []))
        lbl = SECTOR_LABELS.get(top_bull_s, top_bull_s)
        sector_lines.append(f"📈 *{lbl} strong* — {tickers_str}")
    elif top_bull_s == top_bear_s and top_bull_s:
        sector_lines.append(
            f"⚡ *{SECTOR_LABELS.get(top_bull_s, top_bull_s)} split* — mixed flow within sector"
        )
    if sector_lines:
        lines.append("*Sector Leadership*")
        for sl in sector_lines:
            lines.append(f"• {sl}")
        lines.append("")

    # ── Game Plan ─────────────────────────────────────────────────────────────
    if plan:
        lines.append("*Game Plan*")
        for step in plan:
            lines.append(step)
        lines.append("")

    # ── Quick Read ────────────────────────────────────────────────────────────
    if top_overall:
        lines.append("*Quick Read*")
        lines.append(f'"{quick_read_line}"')
        for play in top_overall[:3]:
            lines.append(f"• *{play['ticker']}* → {play['note']}")

    return "\n".join(lines)
