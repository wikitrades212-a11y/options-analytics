"""
Financial Statement Service
Processes raw annual statement data into structured growth, margin,
health, and FCF profile objects. All percentage outputs are ratios (0.12 = 12%).
"""
from __future__ import annotations

import logging
import math
from typing import List, Optional, Tuple

from app.models.stock_fundamentals import (
    BalanceSheetRow,
    CashFlowRow,
    FCFProfile,
    FinancialHealthMetrics,
    GrowthMetrics,
    IncomeStatementRow,
    MarginMetrics,
    RawStockData,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_growth(new_val: Optional[float], old_val: Optional[float]) -> Optional[float]:
    """Return ratio growth between two values. Returns None for any invalid state."""
    if new_val is None or old_val is None:
        return None
    if old_val == 0:
        return None
    # Turnaround from negative to positive is meaningful but misleading as a %;
    # we allow it only when old_val is not deeply negative (i.e., > -10% of new)
    if old_val < 0 and new_val > 0:
        return None
    return (new_val - old_val) / abs(old_val)


def _cagr(start: Optional[float], end: Optional[float], years: int) -> Optional[float]:
    if start is None or end is None or years <= 0:
        return None
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def derive_fcf(row: CashFlowRow) -> Optional[float]:
    """Prefer explicit FCF; fall back to OCF + capex (normalising capex sign)."""
    if row.free_cash_flow is not None:
        return row.free_cash_flow
    if row.operating_cash_flow is not None and row.capital_expenditures is not None:
        capex = row.capital_expenditures
        capex = -abs(capex)  # always treat as outflow
        return row.operating_cash_flow + capex
    return None


# ── Growth metrics ────────────────────────────────────────────────────────────

def compute_growth_metrics(data: RawStockData) -> GrowthMetrics:
    inc = sorted(data.income_statements, key=lambda x: x.year)
    cf = sorted(data.cash_flows, key=lambda x: x.year)

    if len(inc) < 2:
        logger.debug("%s: fewer than 2 income statement years; growth metrics empty", data.ticker)
        return GrowthMetrics()

    latest, prev = inc[-1], inc[-2]

    rev_growth_yoy = _safe_growth(latest.revenue, prev.revenue)
    ni_growth_yoy = _safe_growth(latest.net_income, prev.net_income)
    eps_growth_yoy = _safe_growth(latest.eps, prev.eps)

    # 3-year CAGR uses the record 3 years back (index -4 to -1)
    rev_cagr_3y = None
    if len(inc) >= 4:
        rev_cagr_3y = _cagr(inc[-4].revenue, inc[-1].revenue, 3)
    elif len(inc) >= 2:
        # Fall back to whatever span is available
        span = len(inc) - 1
        rev_cagr_3y = _cagr(inc[0].revenue, inc[-1].revenue, span)

    # FCF growth
    fcf_pairs: List[Tuple[int, float]] = [
        (r.year, v) for r in cf if (v := derive_fcf(r)) is not None
    ]
    fcf_growth_yoy: Optional[float] = None
    fcf_cagr_3y: Optional[float] = None
    if len(fcf_pairs) >= 2:
        fcf_growth_yoy = _safe_growth(fcf_pairs[-1][1], fcf_pairs[-2][1])
    if len(fcf_pairs) >= 4:
        fcf_cagr_3y = _cagr(fcf_pairs[-4][1], fcf_pairs[-1][1], 3)

    return GrowthMetrics(
        revenue_cagr_3y=rev_cagr_3y,
        revenue_growth_yoy=rev_growth_yoy,
        net_income_growth_yoy=ni_growth_yoy,
        eps_growth_yoy=eps_growth_yoy,
        fcf_growth_yoy=fcf_growth_yoy,
        fcf_cagr_3y=fcf_cagr_3y,
    )


# ── Margin metrics ────────────────────────────────────────────────────────────

def compute_margin_metrics(data: RawStockData) -> MarginMetrics:
    inc = sorted(data.income_statements, key=lambda x: x.year)
    cf = sorted(data.cash_flows, key=lambda x: x.year)

    if not inc:
        return MarginMetrics()

    latest_inc = inc[-1]
    revenue = latest_inc.revenue
    if not revenue:
        return MarginMetrics()

    def _margin(numerator: Optional[float]) -> Optional[float]:
        if numerator is None:
            return None
        return numerator / revenue

    fcf_margin: Optional[float] = None
    if cf:
        fcf = derive_fcf(cf[-1])
        if fcf is not None:
            fcf_margin = fcf / revenue

    return MarginMetrics(
        gross_margin=_margin(latest_inc.gross_profit),
        operating_margin=_margin(latest_inc.operating_income),
        net_margin=_margin(latest_inc.net_income),
        fcf_margin=fcf_margin,
    )


# ── Financial health ──────────────────────────────────────────────────────────

def _classify_debt_level(dte: Optional[float]) -> str:
    if dte is None:
        return "Unknown"
    if dte < 0.3:
        return "Low"
    if dte < 1.0:
        return "Moderate"
    if dte < 2.5:
        return "High"
    return "Extreme"


def _classify_liquidity(current_ratio: Optional[float]) -> str:
    if current_ratio is None:
        return "Unknown"
    if current_ratio >= 2.0:
        return "Strong"
    if current_ratio >= 1.2:
        return "Adequate"
    return "Weak"


def compute_financial_health(data: RawStockData) -> FinancialHealthMetrics:
    bs = sorted(data.balance_sheets, key=lambda x: x.year)
    inc = sorted(data.income_statements, key=lambda x: x.year)

    if not bs:
        return FinancialHealthMetrics()

    b = bs[-1]

    dte: Optional[float] = None
    if b.total_debt is not None and b.total_equity and b.total_equity != 0:
        dte = b.total_debt / abs(b.total_equity)

    current_ratio: Optional[float] = None
    if b.current_assets and b.current_liabilities and b.current_liabilities > 0:
        current_ratio = b.current_assets / b.current_liabilities

    net_debt: Optional[float] = None
    if b.total_debt is not None and b.cash_and_equivalents is not None:
        net_debt = b.total_debt - b.cash_and_equivalents

    interest_coverage: Optional[float] = None
    if inc:
        latest = inc[-1]
        if latest.operating_income and latest.interest_expense and latest.interest_expense != 0:
            interest_coverage = latest.operating_income / abs(latest.interest_expense)

    return FinancialHealthMetrics(
        debt_to_equity=dte,
        current_ratio=current_ratio,
        cash_position=b.cash_and_equivalents,
        total_debt=b.total_debt,
        net_debt=net_debt,
        interest_coverage=interest_coverage,
        debt_level=_classify_debt_level(dte),
        liquidity=_classify_liquidity(current_ratio),
    )


# ── FCF profile ───────────────────────────────────────────────────────────────

def compute_fcf_profile(data: RawStockData) -> FCFProfile:
    cf = sorted(data.cash_flows, key=lambda x: x.year)

    fcf_pairs: List[Tuple[int, float]] = [
        (r.year, v) for r in cf if (v := derive_fcf(r)) is not None
    ]

    if not fcf_pairs:
        return FCFProfile(consistency="Unknown")

    years = [p[0] for p in fcf_pairs]
    values = [p[1] for p in fcf_pairs]

    is_positive = all(v > 0 for v in values)
    latest_fcf = values[-1]
    n = min(3, len(values))
    avg_3y = sum(values[-n:]) / n

    is_growing = values[-1] > values[0] if len(values) >= 2 else False

    if not is_positive:
        consistency = "Negative" if all(v < 0 for v in values) else "Unstable"
    elif len(values) >= 3:
        avg = sum(values) / len(values)
        if avg <= 0:
            consistency = "Weak"
        else:
            # Coefficient of variation measures relative dispersion
            std_dev = math.sqrt(sum((v - avg) ** 2 for v in values) / len(values))
            cv = std_dev / avg
            if cv < 0.20 and is_growing:
                consistency = "Strong"
            elif cv < 0.40:
                consistency = "Moderate"
            else:
                consistency = "Weak"
    else:
        consistency = "Moderate" if is_positive else "Weak"

    return FCFProfile(
        years=years,
        values=values,
        is_positive_all_years=is_positive,
        is_growing=is_growing,
        consistency=consistency,
        latest_fcf=latest_fcf,
        avg_fcf_3y=avg_3y,
    )
