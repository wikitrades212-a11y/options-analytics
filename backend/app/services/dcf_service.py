"""
DCF Service
Discounted Cash Flow model with configurable growth methods and honest confidence
flagging. Does not produce fake precision on bad input data.

Growth is linearly decayed from projected_rate → terminal_rate over the
projection window, which avoids the discontinuity of a two-stage step model
while still being intuitive.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from app.models.stock_fundamentals import DCFConfig, DCFResult, FCFProfile, RawStockData

logger = logging.getLogger(__name__)


# ── Confidence assessment ─────────────────────────────────────────────────────

def _assess_confidence(
    fcf_profile: FCFProfile,
    data: RawStockData,
) -> Tuple[str, List[str], bool]:
    """Returns (confidence_level, reasons, is_reliable)."""
    reasons: List[str] = []
    demerits = 0

    if not fcf_profile.values:
        return "low", ["No FCF data available — DCF cannot be computed"], False

    if not fcf_profile.is_positive_all_years:
        reasons.append("FCF has been negative in at least one year")
        demerits += 2

    if fcf_profile.consistency in ("Negative", "Unstable"):
        reasons.append(f"FCF is {fcf_profile.consistency.lower()} — projections unreliable")
        demerits += 2
    elif fcf_profile.consistency == "Weak":
        reasons.append("FCF shows high variability across years")
        demerits += 1

    if len(fcf_profile.values) < 3:
        reasons.append("Fewer than 3 years of FCF history")
        demerits += 1

    if not data.shares_outstanding:
        reasons.append("Shares outstanding missing — per-share value cannot be derived")
        demerits += 1

    # Decide tier
    is_reliable = demerits < 4
    if demerits == 0:
        return "high", ["FCF is positive, consistent, and multi-year"], True
    if demerits <= 2:
        return "medium", reasons, True
    if demerits <= 3:
        return "low", reasons, True
    return "low", reasons, False


# ── Growth rate projection ────────────────────────────────────────────────────

def _project_growth_rate(fcf_values: List[float], config: DCFConfig) -> float:
    method = config.growth_method
    min_g, max_g = config.min_growth, config.growth_cap

    hist_cagr: Optional[float] = None
    if len(fcf_values) >= 2 and fcf_values[0] > 0 and fcf_values[-1] > 0:
        n = len(fcf_values) - 1
        hist_cagr = (fcf_values[-1] / fcf_values[0]) ** (1.0 / n) - 1.0

    if method == "conservative":
        base = (hist_cagr * 0.5) if hist_cagr is not None else 0.05
        return max(min_g, min(base, 0.15))

    if method == "historical_average":
        if hist_cagr is None:
            return 0.05
        return max(min_g, min(hist_cagr, 0.30))

    if method == "capped_growth":
        if hist_cagr is None:
            return 0.05
        return max(min_g, min(hist_cagr, max_g))

    return 0.05  # safe fallback for unknown method


# ── Main DCF runner ───────────────────────────────────────────────────────────

def run_dcf(
    data: RawStockData,
    fcf_profile: FCFProfile,
    config: Optional[DCFConfig] = None,
) -> DCFResult:
    if config is None:
        config = DCFConfig()

    confidence, conf_reasons, is_reliable = _assess_confidence(fcf_profile, data)

    if not is_reliable or not fcf_profile.values:
        return DCFResult(
            current_price=data.current_price,
            confidence=confidence,
            confidence_reasons=conf_reasons,
            is_reliable=False,
            explanation="DCF skipped — FCF data is insufficient or unreliable.",
            config=config,
        )

    vals = fcf_profile.values
    # Base FCF: average of latest 1–3 years for stability against one-off anomalies
    n_base = min(3, len(vals))
    base_fcf = sum(vals[-n_base:]) / n_base

    growth_rate = _project_growth_rate(vals, config)
    terminal_rate = config.terminal_growth_rate
    discount_rate = config.discount_rate

    # Guard: discount rate must exceed terminal growth rate
    if discount_rate <= terminal_rate:
        logger.warning(
            "discount_rate (%.2f) <= terminal_growth_rate (%.2f); clamping terminal rate",
            discount_rate, terminal_rate,
        )
        terminal_rate = discount_rate - 0.01

    # Project cash flows with linear growth decay from growth_rate → terminal_rate
    pv_sum = 0.0
    for year in range(1, config.projection_years + 1):
        weight = (config.projection_years - year) / config.projection_years
        effective_growth = terminal_rate + (growth_rate - terminal_rate) * weight
        fcf_yr = base_fcf * ((1 + effective_growth) ** year)
        pv_sum += fcf_yr / ((1 + discount_rate) ** year)

    # Terminal value (Gordon Growth, applied to final projected FCF)
    final_fcf = base_fcf * ((1 + growth_rate) ** config.projection_years)
    terminal_fcf_next = final_fcf * (1 + terminal_rate)
    terminal_value_raw = terminal_fcf_next / (discount_rate - terminal_rate)
    terminal_value_pv = terminal_value_raw / ((1 + discount_rate) ** config.projection_years)

    total_equity_value = pv_sum + terminal_value_pv

    intrinsic_per_share: Optional[float] = None
    upside: Optional[float] = None
    if data.shares_outstanding and data.shares_outstanding > 0:
        intrinsic_per_share = total_equity_value / data.shares_outstanding
        upside = (intrinsic_per_share - data.current_price) / data.current_price

    explanation = (
        f"Base FCF averaged over {n_base} year(s). "
        f"{config.growth_method.replace('_', ' ').title()} growth of "
        f"{growth_rate * 100:.1f}% decays to {terminal_rate * 100:.1f}% terminal "
        f"over {config.projection_years} years, discounted at {discount_rate * 100:.1f}%."
    )

    return DCFResult(
        intrinsic_value_per_share=intrinsic_per_share,
        current_price=data.current_price,
        upside_downside_pct=upside,
        terminal_value=terminal_value_pv,
        pv_of_cash_flows=pv_sum,
        projected_growth_rate=growth_rate,
        confidence=confidence,
        confidence_reasons=conf_reasons,
        is_reliable=True,
        explanation=explanation,
        config=config,
    )
