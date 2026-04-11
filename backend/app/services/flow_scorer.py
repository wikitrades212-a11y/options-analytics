"""
Flow Scorer — integrated with the Options Analytics pipeline.

Pure scoring and formatting module. No I/O, no dedup, no Telegram calls.
Deduplication lives upstream in scanner_service (persistent SQLite cooldown).
Called by telegram_service.send_scan_summary to build the single summary message.

Input:  list of scanner alert dicts  {"contract": OptionContract, "bias": str, ...}
Output: formatted Markdown string ready for Telegram
"""

import math
from datetime import date
from typing import Any, Dict, List, Set, Tuple

# Macro/index instruments — weighted differently in bias calculation
INDEX_TICKERS: Set[str] = {
    "SPY", "QQQ", "IWM", "DIA", "VXX", "SQQQ", "TQQQ", "SH", "PSQ",
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
    ticker: str, trade_type: str, delta: float, dte: int, premium: float
) -> bool:
    """
    Index ETF put + large premium + near-ATM + short-dated = portfolio
    protection, not a directional bet. Discounted in bias but still shown.
    """
    if ticker not in INDEX_TICKERS:
        return False
    if trade_type != "PUT":
        return False
    return premium >= 5_000_000 and abs(delta) >= 0.35 and dte <= 21


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
    is_hedge = _is_likely_hedge(ticker, trade_type, delta, dte, premium)
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

    return {
        "label":       label,
        "bull_score":  round(bull_score, 1),
        "bear_score":  round(bear_score, 1),
        "bull_pct":    bull_pct,
        "bear_pct":    100 - bull_pct,
        "confidence":  confidence,
        "bull_names":  bull_names,
        "bear_names":  bear_names,
        "hedge_names": hedge_names,
        "divergent":   divergent,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GAME PLAN
# ──────────────────────────────────────────────────────────────────────────────

def _build_gameplan(
    bias: Dict[str, Any],
    bulls: List[Dict[str, Any]],
    bears: List[Dict[str, Any]],
) -> List[str]:
    plan = []
    top_bull = bulls[0]["ticker"] if bulls else None
    top_bear = bears[0]["ticker"] if bears else None

    if bias.get("divergent"):
        plan.append(
            "DIVERGENT TAPE — index flow conflicts with single-name flow. "
            "Reduce size. Wait for price confirmation before committing."
        )
        if top_bear:
            plan.append(
                f"Downside leader: *{top_bear}* — enter on confirmed breakdown only."
            )
        if top_bull:
            plan.append(
                f"Upside leader: *{top_bull}* — watch for relative strength hold on macro dip."
            )
        return plan

    if bias["hedge_names"]:
        hedge_str = " + ".join(set(bias["hedge_names"]))
        plan.append(
            f"{hedge_str} put flow is likely portfolio protection — not a pure directional bet."
        )

    if bias["label"] == "BEARISH":
        plan.append(f"Open weak → downside continuation via *{top_bear or 'top bear'}*.")
        if top_bull:
            plan.append(
                f"Index stabilizes → *{top_bull}* as relative strength play, not macro confirmation."
            )
        plan.append("Don't chase green candles while directional put flow dominates.")

    elif bias["label"] == "BULLISH":
        plan.append(f"Open strong → upside continuation via *{top_bull or 'top bull'}*.")
        if top_bear:
            plan.append(f"Rally stalls → *{top_bear}* for reversal / failed-bounce setup.")
        plan.append("Lean with strength. Don't force shorts into dominant call flow.")

    else:
        if top_bear and top_bull:
            plan.append(
                f"Mixed tape: *{top_bear}* = downside leader, *{top_bull}* = upside leader."
            )
        plan.append("Trade relative strength vs weakness. Don't assume index direction.")
        plan.append("Wait for opening range before committing size.")

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

    bias        = _build_bias(actionable)
    top_overall = _top_overall(actionable, limit=5)
    top_bulls   = _top_bulls(actionable, limit=3)
    top_bears   = _top_bears(actionable, limit=3)
    plan        = _build_gameplan(bias, top_bulls, top_bears)

    bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(bias["label"], "🟡")
    div_tag    = " ⚡DIVERGENT" if bias["divergent"] else ""

    lines: List[str] = []

    # Header
    lines.append(f"{bias_emoji} *MARKET BIAS: {bias['label']}{div_tag}*")
    lines.append(
        f"Bull {bias['bull_pct']}% vs Bear {bias['bear_pct']}%  "
        f"|  Confidence: {bias['confidence']}/100"
    )
    lines.append("")

    # Top Overall
    if top_overall:
        lines.append("*Top Overall Flow*")
        for i, play in enumerate(top_overall, start=1):
            lines.append(f"{i}. {_one_line(play)}")
        lines.append("")

    # Top Bears
    if top_bears:
        lines.append("*Top Bears*")
        for play in top_bears:
            lines.append(f"• {_one_line(play)}")
        lines.append("")

    # Top Bulls
    if top_bulls:
        lines.append("*Top Bulls*")
        for play in top_bulls:
            lines.append(f"• {_one_line(play)}")
        lines.append("")

    # Game Plan
    if plan:
        lines.append("*Game Plan*")
        for step in plan:
            lines.append(f"• {step}")
        lines.append("")

    # Quick Read
    if top_overall:
        lines.append("*Quick Read*")
        for play in top_overall[:3]:
            lines.append(f"• *{play['ticker']}* → {play['note']}")

    return "\n".join(lines)
