"""
Stock Scoring Service
Produces a 0–100 weighted score across four pillars and maps it to a verdict.

Weights:  Business Quality 35 | Financial Strength 20 | Valuation 30 | Risk/Stability 15
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from app.models.stock_fundamentals import (
    DCFResult,
    FCFProfile,
    FinancialHealthMetrics,
    GrowthMetrics,
    MarginMetrics,
    ScoreBreakdown,
    StockScore,
    ValuationMetrics,
)

logger = logging.getLogger(__name__)


# ── Pillar 1: Business Quality (0–35) ─────────────────────────────────────────

def _score_business_quality(
    growth: GrowthMetrics,
    margins: MarginMetrics,
    fcf: FCFProfile,
) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []

    # Revenue growth (0–10)
    rev_g = growth.revenue_cagr_3y if growth.revenue_cagr_3y is not None else growth.revenue_growth_yoy
    if rev_g is not None:
        if rev_g >= 0.20:
            score += 10; reasons.append(f"Strong revenue growth ({rev_g*100:.0f}%+)")
        elif rev_g >= 0.10:
            score += 7; reasons.append(f"Solid revenue growth ({rev_g*100:.0f}%)")
        elif rev_g >= 0.05:
            score += 4
        elif rev_g >= 0:
            score += 2
        else:
            reasons.append(f"Revenue declining ({rev_g*100:.0f}%)")

    # Net income growth (0–8)
    ni_g = growth.net_income_growth_yoy
    if ni_g is not None:
        if ni_g >= 0.20:
            score += 8; reasons.append("Strong net income growth")
        elif ni_g >= 0.10:
            score += 5
        elif ni_g >= 0:
            score += 2
        else:
            reasons.append("Net income declining")

    # Operating margin quality (0–10)
    op_m = margins.operating_margin
    if op_m is not None:
        if op_m >= 0.25:
            score += 10; reasons.append(f"Excellent operating margin ({op_m*100:.0f}%)")
        elif op_m >= 0.15:
            score += 7
        elif op_m >= 0.08:
            score += 4
        elif op_m >= 0:
            score += 1
        else:
            reasons.append("Negative operating margin")

    # FCF consistency (0–7)
    if fcf.consistency == "Strong":
        score += 7; reasons.append("Strong, consistent FCF generation")
    elif fcf.consistency == "Moderate":
        score += 4
    elif fcf.consistency == "Weak":
        score += 1
    elif fcf.consistency in ("Negative", "Unstable"):
        reasons.append(f"FCF is {fcf.consistency.lower()}")

    return min(score, 35.0), reasons


# ── Pillar 2: Financial Strength (0–20) ───────────────────────────────────────

def _score_financial_strength(
    health: FinancialHealthMetrics,
) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []

    # Debt load (0–10)
    if health.debt_level == "Low":
        score += 10; reasons.append("Low debt load")
    elif health.debt_level == "Moderate":
        score += 7
    elif health.debt_level == "High":
        score += 3; reasons.append("High debt load")
    elif health.debt_level == "Extreme":
        score += 0; reasons.append("Extreme debt — serious balance sheet risk")

    # Liquidity (0–6)
    if health.liquidity == "Strong":
        score += 6; reasons.append("Strong current ratio")
    elif health.liquidity == "Adequate":
        score += 3
    elif health.liquidity == "Weak":
        score += 0; reasons.append("Weak current ratio")
    else:
        score += 2  # unknown — neutral

    # Interest coverage (0–4)
    ic = health.interest_coverage
    if ic is not None:
        if ic >= 10:
            score += 4
        elif ic >= 5:
            score += 2
        elif ic >= 2:
            score += 1
        else:
            reasons.append(f"Low interest coverage ({ic:.1f}x) — debt service risk")
    else:
        score += 2  # no interest expense → no leverage risk from that angle

    return min(score, 20.0), reasons


# ── Pillar 3: Valuation (0–30) ────────────────────────────────────────────────

def _score_valuation(
    valuation: ValuationMetrics,
    dcf: DCFResult,
    growth: GrowthMetrics,
) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []

    # P/E reasonableness (0–10)
    pe = valuation.pe_ratio
    if pe is not None:
        if pe < 0:
            reasons.append("Negative earnings — P/E not meaningful")
        elif pe < 15:
            score += 10; reasons.append(f"Attractive P/E ({pe:.1f}x)")
        elif pe < 22:
            score += 7
        elif pe < 30:
            score += 4
        elif pe < 40:
            score += 2; reasons.append(f"Elevated P/E ({pe:.1f}x)")
        else:
            reasons.append(f"Very high P/E ({pe:.1f}x)")

    # FCF yield (0–10)
    fcf_y = valuation.fcf_yield
    if fcf_y is not None:
        if fcf_y >= 0.06:
            score += 10; reasons.append(f"High FCF yield ({fcf_y*100:.1f}%)")
        elif fcf_y >= 0.04:
            score += 7
        elif fcf_y >= 0.02:
            score += 4
        elif fcf_y >= 0.01:
            score += 2
        else:
            reasons.append("Very low FCF yield")

    # DCF discount/premium to market (0–10)
    if dcf.is_reliable and dcf.upside_downside_pct is not None:
        upside = dcf.upside_downside_pct
        if upside >= 0.40:
            score += 10; reasons.append(f"Deep DCF discount ({upside*100:.0f}% upside)")
        elif upside >= 0.20:
            score += 8; reasons.append(f"DCF shows meaningful upside ({upside*100:.0f}%)")
        elif upside >= 0:
            score += 5
        elif upside >= -0.15:
            score += 2
        else:
            reasons.append(f"DCF implies overvaluation ({upside*100:.0f}% downside)")

    return min(score, 30.0), reasons


# ── Pillar 4: Risk / Stability (0–15) ────────────────────────────────────────

def _score_risk_stability(
    growth: GrowthMetrics,
    health: FinancialHealthMetrics,
    fcf: FCFProfile,
) -> Tuple[float, List[str]]:
    score = 15.0  # start at max; deduct for risks
    reasons: List[str] = []

    # FCF instability
    if fcf.consistency in ("Unstable", "Negative"):
        score -= 5; reasons.append("Unstable or negative FCF trend")
    elif fcf.consistency == "Weak":
        score -= 2; reasons.append("Volatile FCF")

    # Leverage
    dte = health.debt_to_equity
    if dte is not None:
        if dte > 3.0:
            score -= 4; reasons.append("Very high leverage (D/E > 3x)")
        elif dte > 1.5:
            score -= 2; reasons.append("Elevated leverage (D/E > 1.5x)")

    # Revenue trend
    rev_g = growth.revenue_growth_yoy
    if rev_g is not None and rev_g < -0.10:
        score -= 3; reasons.append("Revenue shrinking >10% YoY")

    # Earnings trend
    ni_g = growth.net_income_growth_yoy
    if ni_g is not None and ni_g < -0.20:
        score -= 2; reasons.append("Net income collapsing")

    return max(score, 0.0), reasons


# ── Verdict logic ─────────────────────────────────────────────────────────────

def _determine_verdict(
    total: float,
    dcf: DCFResult,
    valuation: ValuationMetrics,
    health: FinancialHealthMetrics,
) -> str:
    upside = dcf.upside_downside_pct if dcf.is_reliable else None
    pe = valuation.pe_ratio

    if total >= 75:
        return "Strong Candidate"

    if total >= 60:
        if upside is not None and upside < -0.10:
            return "Good Business, Too Expensive"
        return "Watchlist"

    if total >= 45:
        if pe and pe > 40:
            return "Good Business, Too Expensive"
        return "Watchlist"

    if total >= 30:
        return "Speculative"

    return "Avoid"


# ── Main entry ────────────────────────────────────────────────────────────────

def score_stock(
    growth: GrowthMetrics,
    margins: MarginMetrics,
    health: FinancialHealthMetrics,
    fcf: FCFProfile,
    valuation: ValuationMetrics,
    dcf: DCFResult,
    missing_fields: List[str],
) -> StockScore:
    bq, bq_r = _score_business_quality(growth, margins, fcf)
    fs, fs_r = _score_financial_strength(health)
    v, v_r = _score_valuation(valuation, dcf, growth)
    r, r_r = _score_risk_stability(growth, health, fcf)

    total = bq + fs + v + r
    all_reasons = bq_r + fs_r + v_r + r_r

    n_missing = len(missing_fields)
    confidence = "high" if n_missing == 0 else ("medium" if n_missing <= 3 else "low")

    verdict = _determine_verdict(total, dcf, valuation, health)

    return StockScore(
        score=ScoreBreakdown(
            business_quality=bq,
            financial_strength=fs,
            valuation=v,
            risk_stability=r,
            total=total,
        ),
        confidence=confidence,
        verdict=verdict,
        reasons=all_reasons,
    )
