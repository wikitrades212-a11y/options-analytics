"""
Valuation Service
Computes P/E, PEG, P/S, P/B, EV/EBITDA, and FCF yield from raw stock data.
Prefers calculating from underlying fields over trusting pre-computed API values.
All percentage outputs are ratios (0.045 = 4.5%).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.models.stock_fundamentals import FCFProfile, RawStockData, ValuationMetrics
from app.services.financial_statement_service import _cagr

logger = logging.getLogger(__name__)


def compute_valuation_metrics(data: RawStockData, fcf_profile: FCFProfile) -> ValuationMetrics:
    price = data.current_price
    market_cap = data.market_cap
    shares = data.shares_outstanding

    inc = sorted(data.income_statements, key=lambda x: x.year)
    bs = sorted(data.balance_sheets, key=lambda x: x.year)

    # ── P/E ──────────────────────────────────────────────────────────────────
    # Prefer TTM net income, fall back to latest annual
    ttm_ni = data.ttm_net_income
    if ttm_ni is None and inc:
        ttm_ni = inc[-1].net_income

    pe: Optional[float] = None
    if ttm_ni and ttm_ni > 0:
        if market_cap:
            pe = market_cap / ttm_ni
        elif shares and shares > 0:
            pe = price / (ttm_ni / shares)

    # ── PEG ──────────────────────────────────────────────────────────────────
    # Use 3-year EPS CAGR as the growth component (standard convention)
    peg: Optional[float] = None
    if pe and len(inc) >= 4:
        old_eps, new_eps = inc[-4].eps, inc[-1].eps
        if old_eps and new_eps and old_eps > 0 and new_eps > 0:
            eps_cagr = _cagr(old_eps, new_eps, 3)
            if eps_cagr and eps_cagr > 0:
                peg = pe / (eps_cagr * 100)  # PEG denominator is annualised % growth

    # ── P/S ──────────────────────────────────────────────────────────────────
    ttm_rev = data.ttm_revenue
    if ttm_rev is None and inc:
        ttm_rev = inc[-1].revenue

    ps: Optional[float] = None
    if ttm_rev and ttm_rev > 0 and market_cap:
        ps = market_cap / ttm_rev

    # ── P/B ──────────────────────────────────────────────────────────────────
    pb: Optional[float] = None
    if bs and market_cap:
        equity = bs[-1].total_equity
        if equity and equity > 0:
            pb = market_cap / equity

    # ── Enterprise Value ─────────────────────────────────────────────────────
    ev = data.enterprise_value
    if ev is None and market_cap and bs:
        b = bs[-1]
        total_debt = b.total_debt or 0.0
        cash = b.cash_and_equivalents or 0.0
        ev = market_cap + total_debt - cash

    # ── EV/EBITDA ─────────────────────────────────────────────────────────────
    ttm_ebitda = data.ttm_ebitda
    if ttm_ebitda is None and inc:
        ttm_ebitda = inc[-1].ebitda

    ev_ebitda: Optional[float] = None
    if ev and ttm_ebitda and ttm_ebitda > 0:
        ev_ebitda = ev / ttm_ebitda

    # ── FCF Yield ─────────────────────────────────────────────────────────────
    fcf_yield: Optional[float] = None
    latest_fcf = fcf_profile.latest_fcf
    if latest_fcf and latest_fcf > 0 and market_cap and market_cap > 0:
        fcf_yield = latest_fcf / market_cap  # stored as ratio

    return ValuationMetrics(
        pe_ratio=pe,
        forward_pe=data.forward_pe,
        peg_ratio=peg,
        price_to_sales=ps,
        price_to_book=pb,
        ev_to_ebitda=ev_ebitda,
        fcf_yield=fcf_yield,
    )
