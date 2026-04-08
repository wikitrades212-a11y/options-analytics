"""
Trade quality scoring engine.

Computes per-contract trade quality scores, confidence breakdown,
execution plan, thesis validation, IV context, and alert fields
for the Target Move Calculator.

All inputs are plain Python values (no model dependencies).
All outputs are a flat dict ready to merge into StrikeAnalysis.
"""
import math
import logging
from typing import List

logger = logging.getLogger(__name__)


# ── Strategy mode presets ─────────────────────────────────────────────────────

STRATEGY_PRESETS: dict = {
    "Scalp": {
        # Delta preference: reward contracts near 0.50, tolerate ±0.18
        "delta_center": 0.50,
        "delta_half": 0.18,
        # Execution multipliers
        "chase_multiplier": 1.03,
        "soft_stop_pct": 0.15,
        "hard_stop_pct": 0.22,
        "tp1_realization_pct": 0.45,
        # Composite weights (must sum to ~1.0)
        "weights": {
            "target_fit_score":       0.18,
            "expiry_fit_score":       0.08,
            "liquidity_score":        0.15,
            "spread_score":           0.15,
            "delta_quality_score":    0.10,
            "gamma_quality_score":    0.12,
            "theta_quality_score":    0.05,
            "roi_score":              0.08,
            "premium_efficiency_score": 0.05,
            "iv_fairness_score":      0.02,
            "realism_score":          0.02,
        },
    },
    "Intraday": {
        "delta_center": 0.45,
        "delta_half": 0.18,
        "chase_multiplier": 1.05,
        "soft_stop_pct": 0.18,
        "hard_stop_pct": 0.28,
        "tp1_realization_pct": 0.50,
        "weights": {
            "target_fit_score":       0.16,
            "expiry_fit_score":       0.12,
            "liquidity_score":        0.12,
            "spread_score":           0.10,
            "delta_quality_score":    0.10,
            "gamma_quality_score":    0.07,
            "theta_quality_score":    0.07,
            "roi_score":              0.10,
            "premium_efficiency_score": 0.06,
            "iv_fairness_score":      0.05,
            "realism_score":          0.05,
        },
    },
    "Swing": {
        "delta_center": 0.45,
        "delta_half": 0.20,
        "chase_multiplier": 1.08,
        "soft_stop_pct": 0.22,
        "hard_stop_pct": 0.35,
        "tp1_realization_pct": 0.55,
        "weights": {
            "target_fit_score":       0.15,
            "expiry_fit_score":       0.18,
            "liquidity_score":        0.10,
            "spread_score":           0.08,
            "delta_quality_score":    0.10,
            "gamma_quality_score":    0.04,
            "theta_quality_score":    0.12,
            "roi_score":              0.10,
            "premium_efficiency_score": 0.06,
            "iv_fairness_score":      0.05,
            "realism_score":          0.02,
        },
    },
    "Lottery": {
        "delta_center": 0.25,
        "delta_half": 0.15,
        "chase_multiplier": 1.12,
        "soft_stop_pct": 0.30,
        "hard_stop_pct": 0.45,
        "tp1_realization_pct": 0.40,
        "weights": {
            "target_fit_score":       0.14,
            "expiry_fit_score":       0.08,
            "liquidity_score":        0.08,
            "spread_score":           0.12,
            "delta_quality_score":    0.08,
            "gamma_quality_score":    0.10,
            "theta_quality_score":    0.04,
            "roi_score":              0.22,
            "premium_efficiency_score": 0.08,
            "iv_fairness_score":      0.04,
            "realism_score":          0.02,
        },
    },
    "Conservative": {
        "delta_center": 0.60,
        "delta_half": 0.15,
        "chase_multiplier": 1.02,
        "soft_stop_pct": 0.12,
        "hard_stop_pct": 0.20,
        "tp1_realization_pct": 0.50,
        "weights": {
            "target_fit_score":       0.14,
            "expiry_fit_score":       0.14,
            "liquidity_score":        0.16,
            "spread_score":           0.14,
            "delta_quality_score":    0.12,
            "gamma_quality_score":    0.04,
            "theta_quality_score":    0.10,
            "roi_score":              0.06,
            "premium_efficiency_score": 0.05,
            "iv_fairness_score":      0.03,
            "realism_score":          0.02,
        },
    },
}

VALID_MODES = set(STRATEGY_PRESETS.keys())


# ── Utility helpers ──────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _safe(v, default: float = 0.0) -> float:
    try:
        r = float(v)
        if math.isnan(r) or math.isinf(r):
            return default
        return r
    except (TypeError, ValueError):
        return default


def _round2(v: float) -> float:
    return round(v, 2)


def _round3(v: float) -> float:
    return round(v, 3)


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# ── IV context ────────────────────────────────────────────────────────────────

def _iv_context(contract_iv: float, chain_ivs: List[float]) -> tuple[str, float]:
    """
    Return (label, score[0-1]) based on contract IV vs chain median.

    Thresholds:
      < 0.90× median  → Cheap  (score 0.90)
      0.90–1.10×      → Fair   (score 0.70)
      1.10–1.30×      → Rich   (score 0.45)
      > 1.30×         → Extreme (score 0.15)
    Falls back to Fair when data is insufficient.
    """
    valid_ivs = [iv for iv in chain_ivs if iv and iv > 0]
    if not valid_ivs or contract_iv <= 0:
        return "IV Fair", 0.70

    median_iv = _median(valid_ivs)
    if median_iv <= 0:
        return "IV Fair", 0.70

    ratio = contract_iv / median_iv

    if ratio < 0.90:
        return "IV Cheap", 0.90
    elif ratio <= 1.10:
        return "IV Fair", 0.70
    elif ratio <= 1.30:
        return "IV Rich", 0.45
    else:
        return "IV Extreme", 0.15


# ── Sub-score functions (all return [0, 1]) ───────────────────────────────────

def _target_fit(strike: float, target_price: float) -> float:
    """How close is the strike to the target price?"""
    window = max(target_price * 0.08, 1.0)
    return _clamp(1.0 - abs(strike - target_price) / window)


def _spread_score(spread_pct: float) -> float:
    """Tight spread = good. 0% = 1.0, 50%+ = 0.0."""
    return _clamp(1.0 - spread_pct / 50.0)


def _delta_quality(abs_delta: float, preset: dict) -> float:
    """
    Reward deltas near the strategy's preferred center.
    Uses a triangular decay: full score at center, zero at ±(1.5×half_width).
    """
    center = preset["delta_center"]
    half   = max(preset["delta_half"], 0.05)
    dist   = abs(abs_delta - center)
    return _clamp(1.0 - dist / (half * 1.5))


def _gamma_quality(gamma: float, gamma_ceil: float) -> float:
    """Chain-relative gamma: higher = better (more leverage per point)."""
    if gamma_ceil <= 0:
        return 0.5
    return _clamp(abs(gamma) / gamma_ceil)


def _theta_quality(theta: float, mid: float) -> float:
    """
    Lower theta-per-dollar-of-premium = better.
    Daily theta > 10 % of premium = poor. > 5 % = moderate concern.
    """
    if mid <= 0:
        return 0.5
    theta_pct = abs(theta) / mid   # fraction of premium lost per day
    return _clamp(1.0 - theta_pct / 0.10)


def _roi_score_fn(roi_pct: float, strategy_mode: str) -> float:
    """
    Normalized ROI [0–300%] → [0, 1].
    Lottery rewards higher ROI; Conservative caps at moderate ROI.
    """
    roi = _safe(roi_pct)
    if strategy_mode == "Conservative":
        # Sweet-spot 20–80 %; penalise extreme lottery-style ROI
        if roi < 0:
            return 0.0
        if roi <= 80:
            return _clamp(roi / 80.0)
        return _clamp(1.0 - (roi - 80.0) / 200.0)
    elif strategy_mode == "Lottery":
        return _clamp(roi / 400.0)
    else:
        return _clamp(roi / 300.0)


def _premium_efficiency(estimated_value: float, mid: float) -> float:
    """
    Ratio of estimated target value to current premium.
    1× = no gain → 0.0; 4× = 300% gain → 1.0.
    """
    if mid <= 0:
        return 0.0
    ratio = _safe(estimated_value / mid)
    return _clamp((ratio - 1.0) / 3.0)


def _realism_score(
    move_pct_abs: float, implied_volatility: float, dte: int
) -> float:
    """
    How realistic is the required move given IV and DTE?

    Expected 1-sigma move ≈ IV × sqrt(DTE/365) × 100%.
    z < 0.75: easy → 1.0
    z 0.75–1.5: normal → declining
    z > 2.0: aggressive → near 0
    """
    iv  = _safe(implied_volatility)
    dte = max(dte, 1)

    if iv <= 0:
        # No IV data: use DTE heuristic only (short DTE + big move = bad)
        days_needed = move_pct_abs * 5    # rough days per % move
        return _clamp(1.0 - days_needed / max(dte * 2.0, 1.0))

    expected_sigma_pct = iv * (dte / 365.0) ** 0.5 * 100.0
    z = move_pct_abs / max(expected_sigma_pct, 0.01)

    if z <= 0.75:
        return 1.0
    elif z <= 1.5:
        return _clamp(1.0 - (z - 0.75) / 0.75 * 0.5)
    else:
        return _clamp(0.5 - (z - 1.5) / 1.0 * 0.4)


# ── Composite score ───────────────────────────────────────────────────────────

def _composite_score(sub_scores: dict, weights: dict) -> float:
    """Weighted sum of [0,1] sub-scores → [0, 100] final score."""
    raw = sum(sub_scores[k] * weights[k] for k in weights if k in sub_scores)
    return _clamp(raw * 100.0, 0.0, 100.0)


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    elif score >= 65:
        return "B"
    elif score >= 50:
        return "C"
    return "D"


# ── Confidence breakdown ──────────────────────────────────────────────────────

def _confidence_breakdown(sub: dict) -> tuple[float, float, float, float]:
    """
    Returns (direction, contract, execution, risk) each 0–100.

    direction_confidence:  target_fit, delta_quality, realism, expiry_fit
    contract_quality:      liquidity, spread, greeks avg, iv_fairness
    execution_quality:     spread, premium_efficiency, fill tolerance (spread inverse)
    risk_quality:          theta_quality, realism, expiry_fit, not-overleveraged
    """
    greeks_avg = (
        sub["delta_quality_score"]
        + sub["gamma_quality_score"]
        + sub["theta_quality_score"]
    ) / 3.0

    direction = (
        sub["target_fit_score"]
        + sub["delta_quality_score"]
        + sub["realism_score"]
        + sub["expiry_fit_score"]
    ) / 4.0

    contract = (
        sub["liquidity_score"]
        + sub["spread_score"]
        + greeks_avg
        + sub["iv_fairness_score"]
    ) / 4.0

    execution = (
        sub["spread_score"] * 0.40
        + sub["premium_efficiency_score"] * 0.35
        + sub["spread_score"] * 0.25    # fill tolerance proxy
    )

    # Penalise extreme ROI (lottery risk); reward theta quality
    roi_penalty = max(0.0, sub["roi_score"] - 0.80) * 0.5
    risk = (
        sub["theta_quality_score"] * 0.35
        + sub["realism_score"] * 0.30
        + sub["expiry_fit_score"] * 0.25
        + max(0.0, 1.0 - roi_penalty) * 0.10
    )

    return (
        _clamp(direction * 100.0, 0.0, 100.0),
        _clamp(contract  * 100.0, 0.0, 100.0),
        _clamp(execution * 100.0, 0.0, 100.0),
        _clamp(risk      * 100.0, 0.0, 100.0),
    )


# ── Execution plan ────────────────────────────────────────────────────────────

def _execution_plan(
    bid: float,
    ask: float,
    mid: float,
    mark: float,
    estimated_value_at_target: float,
    preset: dict,
) -> tuple[float, float, float, float, float, float]:
    """
    Returns (ideal_entry, chase_limit, soft_stop, hard_stop, tp1, tp2).

    Formulas are strategy-mode-aware via preset multipliers.
    Safety clamping ensures ordering invariants:
      hard_stop < soft_stop < ideal_entry ≤ chase_limit
      tp1 > ideal_entry, tp2 ≥ tp1
    """
    current_mark = _safe(mark) or _safe(mid) or 0.01
    spread       = max(_safe(ask) - _safe(bid), 0.0)
    spread_buf   = max(spread * 0.5, 0.02)

    # Ideal entry: try to get filled slightly below mark
    ideal_entry = min(
        current_mark,
        max(_safe(bid) + spread_buf, current_mark * 0.97),
    )
    ideal_entry = max(0.01, _round2(ideal_entry))

    chase_limit  = _round2(ideal_entry * preset["chase_multiplier"])
    soft_stop    = _round2(ideal_entry * (1.0 - preset["soft_stop_pct"]))
    hard_stop    = _round2(ideal_entry * (1.0 - preset["hard_stop_pct"]))

    exp_val = max(_safe(estimated_value_at_target), current_mark * 1.01)
    tp1     = _round2(ideal_entry + (exp_val - ideal_entry) * preset["tp1_realization_pct"])
    tp2     = _round2(exp_val)

    # Ordering clamps
    hard_stop = max(0.01, min(hard_stop, soft_stop - 0.01))
    soft_stop = max(hard_stop + 0.01, soft_stop)
    tp1       = max(ideal_entry + 0.01, tp1)
    tp2       = max(tp1 + 0.01, tp2)

    return ideal_entry, chase_limit, soft_stop, hard_stop, tp1, tp2


# ── Thesis validation ─────────────────────────────────────────────────────────

def _thesis(
    trade_quality_score: float,
    realism_score: float,
    premium_efficiency_score: float,
    spread_score: float,
) -> str:
    if (
        trade_quality_score >= 75
        and realism_score >= 0.50
        and premium_efficiency_score >= 0.35
    ):
        return "Worth It"
    elif trade_quality_score >= 55 and spread_score >= 0.30:
        return "Borderline"
    return "Not Worth It"


def _move_to_option_target(
    option_target: float, mid: float, abs_delta: float, current_price: float
) -> float:
    """
    Estimate % stock move needed to bring option from mid to option_target.
    Uses first-order delta approximation.
    """
    if abs_delta <= 0 or current_price <= 0:
        return 0.0
    option_gain    = max(option_target - mid, 0.0)
    stock_points   = option_gain / max(abs_delta, 0.01)
    return _round3(stock_points / current_price * 100.0)


# ── Alert fields ──────────────────────────────────────────────────────────────

def _alerts(
    mid: float,
    ideal_entry: float,
    spread_pct: float,
    trade_quality_score: float,
    current_price: float,
    target_price: float,
    tp1: float,
    tp2: float,
) -> dict:
    return {
        "alert_entry_ready":         mid <= ideal_entry * 1.02,
        "alert_below_ideal_entry":   mid < ideal_entry,
        "alert_spread_improving":    spread_pct < 10.0,
        "alert_score_above_threshold": trade_quality_score >= 65,
        "alert_stock_near_target": (
            target_price > 0
            and abs(current_price - target_price) / max(current_price, 0.01) < 0.02
        ),
        "alert_tp1_hit": mid >= tp1,
        "alert_tp2_hit": mid >= tp2,
    }


# ── Explanation builder ───────────────────────────────────────────────────────

def _explanation(
    strategy_mode: str,
    trade_quality_score: float,
    trade_quality_grade: str,
    sub: dict,
    iv_context_label: str,
    thesis_verdict: str,
) -> str:
    """Build a human-readable explanation from the score drivers."""
    parts: List[str] = []

    # Header
    grade_word = {"A": "strong", "B": "solid", "C": "moderate", "D": "weak"}.get(
        trade_quality_grade, "moderate"
    )
    parts.append(
        f"This {grade_word} {strategy_mode.lower()} setup "
        f"(Grade {trade_quality_grade}, score {trade_quality_score:.0f}/100)"
    )

    # Target / expiry fit
    tf = sub["target_fit_score"]
    ef = sub["expiry_fit_score"]
    if tf >= 0.70 and ef >= 0.70:
        parts.append("aligns well with your price target and expiry timeline")
    elif tf >= 0.50 and ef >= 0.50:
        parts.append("has a reasonable target and expiry fit")
    elif tf < 0.40:
        parts.append("is a stretch from your price target")
    else:
        parts.append("matches the target reasonably but the expiry timing is imperfect")

    # Liquidity and spread
    liq_n   = sub["liquidity_score"]
    spr     = sub["spread_score"]
    if liq_n >= 0.70 and spr >= 0.70:
        parts.append("has good liquidity and tight spreads")
    elif liq_n < 0.35:
        parts.append("suffers from thin liquidity — fills may be difficult")
    elif spr < 0.35:
        parts.append("has wide spreads that will hurt entry and exit pricing")

    # Delta / gamma
    dq = sub["delta_quality_score"]
    gq = sub["gamma_quality_score"]
    tq = sub["theta_quality_score"]
    if dq >= 0.70 and gq >= 0.70:
        parts.append("Delta and gamma are both favorable for this strategy")
    elif dq >= 0.60:
        parts.append("Delta responsiveness is solid")
    elif dq < 0.35:
        parts.append("Delta sensitivity is below the ideal range for this approach")

    # Theta
    if tq < 0.35:
        parts.append("Theta decay is a meaningful headwind")
    elif tq >= 0.70:
        parts.append("Theta pressure is manageable")

    # IV context
    if iv_context_label == "IV Cheap":
        parts.append("IV is relatively cheap — favorable premium entry")
    elif iv_context_label == "IV Rich":
        parts.append("IV is elevated — moderate overpayment risk exists")
    elif iv_context_label == "IV Extreme":
        parts.append("IV is extremely elevated — significant overpayment risk")

    # Realism
    real = sub["realism_score"]
    if real < 0.35:
        parts.append("The required stock move is aggressive relative to time available")
    elif real >= 0.75:
        parts.append("The required move looks realistic given the timeframe")

    # Thesis verdict
    if thesis_verdict == "Worth It":
        parts.append("Overall, this trade is worth considering at current pricing")
    elif thesis_verdict == "Borderline":
        parts.append("Overall, proceed cautiously — the thesis is borderline")
    else:
        parts.append("Overall, the risk/reward does not justify this setup at current pricing")

    return ". ".join(parts) + "."


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_trade_scores(
    *,
    # Contract data
    strike: float,
    bid: float,
    ask: float,
    mid: float,
    mark: float,
    volume: int,
    open_interest: int,
    implied_volatility: float,
    delta: float,
    gamma: float,
    theta: float,
    spread_pct: float,
    liquidity_score_raw: float,     # 0–100 existing liquidity score
    estimated_value_at_target: float,
    estimated_roi_pct: float,
    breakeven_move_pct: float,
    # Chain / move context
    current_price: float,
    target_price: float,
    move_pct_abs: float,
    dte: int,
    expiry_fit: float,              # [0, 1] already computed
    gamma_ceil: float,
    chain_ivs: List[float],         # all same-expiry contract IVs
    # Strategy
    strategy_mode: str,
) -> dict:
    """
    Compute all scoring fields for one contract.
    Returns a flat dict of new fields to merge into StrikeAnalysis.
    Never raises — all errors produce safe fallback values.
    """
    # Normalise / validate strategy mode
    mode = strategy_mode if strategy_mode in VALID_MODES else "Intraday"
    preset = STRATEGY_PRESETS[mode]

    try:
        # Safe input values
        s_bid   = _safe(bid)
        s_ask   = _safe(ask)
        s_mid   = _safe(mid) or 0.01
        s_mark  = _safe(mark) or s_mid
        s_iv    = _safe(implied_volatility)
        s_delta = _safe(delta)
        s_gamma = _safe(gamma)
        s_theta = _safe(theta)
        abs_delta = abs(s_delta)

        # ── IV context ────────────────────────────────────────────────────────
        iv_label, iv_score = _iv_context(s_iv, chain_ivs)

        # ── Sub-scores (all [0, 1]) ───────────────────────────────────────────
        tf   = _target_fit(strike, target_price)
        ef   = _clamp(expiry_fit)
        liq  = _clamp(liquidity_score_raw / 100.0)
        spr  = _spread_score(spread_pct)
        dq   = _delta_quality(abs_delta, preset)
        gq   = _gamma_quality(s_gamma, gamma_ceil)
        tq   = _theta_quality(s_theta, s_mid)
        roi  = _roi_score_fn(estimated_roi_pct, mode)
        pe   = _premium_efficiency(estimated_value_at_target, s_mid)
        ivf  = iv_score
        real = _realism_score(move_pct_abs, s_iv, dte)

        sub = {
            "target_fit_score":         tf,
            "expiry_fit_score":         ef,
            "liquidity_score":          liq,
            "spread_score":             spr,
            "delta_quality_score":      dq,
            "gamma_quality_score":      gq,
            "theta_quality_score":      tq,
            "roi_score":                roi,
            "premium_efficiency_score": pe,
            "iv_fairness_score":        ivf,
            "realism_score":            real,
        }

        # ── Composite score ───────────────────────────────────────────────────
        tqs   = _composite_score(sub, preset["weights"])
        grade = _grade(tqs)

        # ── Confidence breakdown ──────────────────────────────────────────────
        dir_conf, con_qual, exe_qual, rsk_qual = _confidence_breakdown(sub)

        # ── Execution plan ────────────────────────────────────────────────────
        ideal_entry, chase_limit, soft_stop, hard_stop, tp1, tp2 = _execution_plan(
            s_bid, s_ask, s_mid, s_mark, estimated_value_at_target, preset
        )

        # ── Thesis validation ─────────────────────────────────────────────────
        exp_move_pct = _round3(
            abs(target_price - current_price) / max(current_price, 0.01) * 100.0
        )
        thesis = _thesis(tqs, real, pe, spr)

        move_to_tp1_pct = _move_to_option_target(tp1, s_mid, abs_delta, current_price)
        move_to_tp2_pct = _move_to_option_target(tp2, s_mid, abs_delta, current_price)

        # ── Alert fields ──────────────────────────────────────────────────────
        alert_fields = _alerts(
            s_mid, ideal_entry, spread_pct, tqs,
            current_price, target_price, tp1, tp2,
        )

        # ── Explanation ───────────────────────────────────────────────────────
        expl = _explanation(mode, tqs, grade, sub, iv_label, thesis)

        # Scale sub-scores to 0–100 for output
        return {
            # Composite
            "trade_quality_score": _round2(tqs),
            "trade_quality_grade": grade,
            # Sub-scores 0–100
            "target_fit_score":         _round2(tf   * 100),
            "expiry_fit_score":         _round2(ef   * 100),
            "spread_score":             _round2(spr  * 100),
            "delta_quality_score":      _round2(dq   * 100),
            "gamma_quality_score":      _round2(gq   * 100),
            "theta_quality_score":      _round2(tq   * 100),
            "roi_score":                _round2(roi  * 100),
            "premium_efficiency_score": _round2(pe   * 100),
            "iv_fairness_score":        _round2(ivf  * 100),
            "realism_score":            _round2(real * 100),
            # Confidence breakdown
            "direction_confidence_score": _round2(dir_conf),
            "contract_quality_score":     _round2(con_qual),
            "execution_quality_score":    _round2(exe_qual),
            "risk_quality_score":         _round2(rsk_qual),
            # Execution plan
            "ideal_entry":  ideal_entry,
            "chase_limit":  chase_limit,
            "soft_stop":    soft_stop,
            "hard_stop":    hard_stop,
            "tp1":          tp1,
            "tp2":          tp2,
            # Thesis validation
            "current_stock_price":          _round2(current_price),
            "target_stock_price":           _round2(target_price),
            "expected_stock_move_pct":      exp_move_pct,
            "move_required_to_breakeven_pct": _round3(breakeven_move_pct),
            "move_required_to_tp1_pct":     move_to_tp1_pct,
            "move_required_to_tp2_pct":     move_to_tp2_pct,
            "thesis_verdict":               thesis,
            # IV context
            "iv_context_label": iv_label,
            "iv_context_score": _round2(iv_score * 100),
            # Strategy mode
            "strategy_mode": mode,
            # Alert fields
            **alert_fields,
            # Explanation
            "explanation": expl,
        }

    except Exception as exc:
        logger.error(f"Scoring engine error for strike={strike}: {exc}", exc_info=True)
        # Return safe neutral fallback — never propagate
        return _safe_fallback(strategy_mode)


def _safe_fallback(strategy_mode: str = "Intraday") -> dict:
    """All-zero fallback returned when scoring crashes."""
    return {
        "trade_quality_score": 0.0, "trade_quality_grade": "D",
        "target_fit_score": 0.0, "expiry_fit_score": 0.0,
        "spread_score": 0.0, "delta_quality_score": 0.0,
        "gamma_quality_score": 0.0, "theta_quality_score": 0.0,
        "roi_score": 0.0, "premium_efficiency_score": 0.0,
        "iv_fairness_score": 0.0, "realism_score": 0.0,
        "direction_confidence_score": 0.0, "contract_quality_score": 0.0,
        "execution_quality_score": 0.0, "risk_quality_score": 0.0,
        "ideal_entry": 0.0, "chase_limit": 0.0,
        "soft_stop": 0.0, "hard_stop": 0.0, "tp1": 0.0, "tp2": 0.0,
        "current_stock_price": 0.0, "target_stock_price": 0.0,
        "expected_stock_move_pct": 0.0, "move_required_to_breakeven_pct": 0.0,
        "move_required_to_tp1_pct": 0.0, "move_required_to_tp2_pct": 0.0,
        "thesis_verdict": "Not Worth It",
        "iv_context_label": "IV Fair", "iv_context_score": 0.0,
        "strategy_mode": strategy_mode,
        "alert_entry_ready": False, "alert_below_ideal_entry": False,
        "alert_spread_improving": False, "alert_score_above_threshold": False,
        "alert_stock_near_target": False, "alert_tp1_hit": False, "alert_tp2_hit": False,
        "explanation": "Scoring unavailable for this contract.",
    }
