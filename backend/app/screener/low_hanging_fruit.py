"""
Low Hanging Fruit Screener
Filters a list of analyzed stocks against configurable fundamental thresholds.
Designed for: DCF upside, FCF quality, growth, debt, valuation score.

Future extension point: add bullish_flow_required=True once options flow
data can be joined here (ticker → flow signal lookup).
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from app.models.stock_fundamentals import StockAnalysis


# ── Config ────────────────────────────────────────────────────────────────────

class ScreenerConfig(BaseModel):
    min_dcf_upside: float = 0.20           # 20% upside (ratio)
    require_positive_fcf: bool = True       # FCF positive every year on record
    min_revenue_growth: float = 0.05        # 5% (ratio)
    max_debt_level: str = "Moderate"        # "Low" | "Moderate" | "High"
    min_valuation_score: float = 15.0       # out of 30
    min_total_score: float = 55.0
    require_reliable_dcf: bool = True
    # Future: bullish_flow_required: bool = False


# ── Result ────────────────────────────────────────────────────────────────────

class ScreenerResult(BaseModel):
    ticker: str
    verdict: str
    total_score: float
    dcf_upside_pct: Optional[float] = None  # ratio
    fcf_consistency: str
    revenue_growth: Optional[float] = None  # ratio
    debt_level: str
    passed: bool
    fail_reasons: List[str]


# ── Debt rank for comparison ──────────────────────────────────────────────────

_DEBT_RANK = {"Low": 0, "Moderate": 1, "High": 2, "Extreme": 3, "Unknown": 4}


# ── Core screening logic ──────────────────────────────────────────────────────

def screen_stock(
    analysis: StockAnalysis,
    config: Optional[ScreenerConfig] = None,
) -> ScreenerResult:
    if config is None:
        config = ScreenerConfig()

    fails: List[str] = []

    # ── DCF reliability & upside ──
    if config.require_reliable_dcf and not analysis.dcf.is_reliable:
        fails.append("DCF is unreliable or missing")
    else:
        upside = analysis.dcf.upside_downside_pct
        if upside is None or upside < config.min_dcf_upside:
            pct = f"{upside*100:.0f}%" if upside is not None else "N/A"
            fails.append(
                f"DCF upside {pct} < {config.min_dcf_upside*100:.0f}% threshold"
            )

    # ── FCF positivity ──
    if config.require_positive_fcf and not analysis.fcf_profile.is_positive_all_years:
        fails.append("FCF was negative in at least one year")

    # ── Revenue growth ──
    rev_g = (
        analysis.growth_metrics.revenue_cagr_3y
        or analysis.growth_metrics.revenue_growth_yoy
    )
    if rev_g is None or rev_g < config.min_revenue_growth:
        pct = f"{rev_g*100:.1f}%" if rev_g is not None else "N/A"
        fails.append(
            f"Revenue growth {pct} below {config.min_revenue_growth*100:.0f}% threshold"
        )

    # ── Debt level ──
    debt_level = analysis.financial_health.debt_level or "Unknown"
    if _DEBT_RANK.get(debt_level, 4) > _DEBT_RANK.get(config.max_debt_level, 1):
        fails.append(f"Debt level '{debt_level}' exceeds max '{config.max_debt_level}'")

    # ── Score gates ──
    if analysis.score.score.valuation < config.min_valuation_score:
        fails.append(
            f"Valuation score {analysis.score.score.valuation:.0f} < {config.min_valuation_score:.0f}"
        )
    if analysis.score.score.total < config.min_total_score:
        fails.append(
            f"Total score {analysis.score.score.total:.0f} < {config.min_total_score:.0f}"
        )

    return ScreenerResult(
        ticker=analysis.ticker,
        verdict=analysis.verdict,
        total_score=analysis.score.score.total,
        dcf_upside_pct=analysis.dcf.upside_downside_pct,
        fcf_consistency=analysis.fcf_profile.consistency,
        revenue_growth=rev_g,
        debt_level=debt_level,
        passed=len(fails) == 0,
        fail_reasons=fails,
    )


def screen_batch(
    analyses: List[StockAnalysis],
    config: Optional[ScreenerConfig] = None,
) -> List[ScreenerResult]:
    """
    Screen a list of analyses, return only passing results sorted by DCF upside.
    Call this for the /stock/screen endpoint or a nightly batch job.
    """
    results = [screen_stock(a, config) for a in analyses]
    passing = [r for r in results if r.passed]
    return sorted(passing, key=lambda r: r.dcf_upside_pct or 0.0, reverse=True)
