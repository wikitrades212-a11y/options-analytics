"""
Stock Analysis Service — Main Orchestrator
Calls each sub-service in sequence and assembles the final StockAnalysis object.
This is the single entry point for the pipeline.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from app.models.stock_fundamentals import DCFConfig, RawStockData, StockAnalysis
from app.services.dcf_service import run_dcf
from app.services.financial_statement_service import (
    compute_fcf_profile,
    compute_financial_health,
    compute_growth_metrics,
    compute_margin_metrics,
)
from app.services.stock_scoring_service import score_stock
from app.services.valuation_service import compute_valuation_metrics

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_missing_fields(data: RawStockData) -> List[str]:
    missing = []
    if not data.income_statements:
        missing.append("income_statements")
    if not data.balance_sheets:
        missing.append("balance_sheets")
    if not data.cash_flows:
        missing.append("cash_flows")
    if not data.shares_outstanding:
        missing.append("shares_outstanding")
    if not data.market_cap:
        missing.append("market_cap")
    return missing


def _data_quality(missing: List[str]) -> str:
    if not missing:
        return "good"
    if len(missing) <= 2:
        return "partial"
    return "limited"


def _build_warnings(data, missing, health, fcf, dcf) -> List[str]:
    warnings = []

    if missing:
        warnings.append(f"Incomplete data ({', '.join(missing)}) — some metrics unavailable")

    if not fcf.is_positive_all_years and fcf.values:
        warnings.append("FCF turned negative in at least one year")

    if health.debt_level in ("High", "Extreme"):
        warnings.append(f"Debt level is {health.debt_level.lower()} — monitor interest coverage")

    if dcf.confidence == "low":
        warnings.append("DCF confidence is low — intrinsic value estimate is speculative")

    if health.liquidity == "Weak":
        warnings.append("Weak current ratio — possible short-term liquidity risk")

    if health.interest_coverage is not None and health.interest_coverage < 2:
        warnings.append(f"Interest coverage is only {health.interest_coverage:.1f}x")

    return warnings


def _build_summary(ticker, score_total, verdict, dcf_upside, fcf_consistency, debt_level) -> str:
    parts = [f"{ticker} scores {score_total:.0f}/100 — {verdict}."]

    if dcf_upside is not None:
        direction = "upside" if dcf_upside >= 0 else "downside"
        parts.append(f"DCF model implies {abs(dcf_upside * 100):.0f}% {direction}.")

    parts.append(
        f"FCF profile is {fcf_consistency.lower()}; debt load is {debt_level.lower()}."
    )
    return " ".join(parts)


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_stock(
    data: RawStockData,
    dcf_config: Optional[DCFConfig] = None,
) -> StockAnalysis:
    """
    Full fundamental analysis pipeline.

    data        — raw financial data (see RawStockData)
    dcf_config  — optional DCF parameters; defaults to conservative 10-year model
    """
    logger.info("Starting fundamental analysis for %s", data.ticker)

    missing = _find_missing_fields(data)
    quality = _data_quality(missing)

    growth = compute_growth_metrics(data)
    margins = compute_margin_metrics(data)
    health = compute_financial_health(data)
    fcf = compute_fcf_profile(data)
    valuation = compute_valuation_metrics(data, fcf)
    dcf = run_dcf(data, fcf, dcf_config)
    stock_score = score_stock(growth, margins, health, fcf, valuation, dcf, missing)

    warnings = _build_warnings(data, missing, health, fcf, dcf)
    summary = _build_summary(
        data.ticker,
        stock_score.score.total,
        stock_score.verdict,
        dcf.upside_downside_pct,
        fcf.consistency,
        health.debt_level or "Unknown",
    )

    logger.info(
        "%s analysis complete — score=%.0f verdict=%s dcf_confidence=%s",
        data.ticker, stock_score.score.total, stock_score.verdict, dcf.confidence,
    )

    return StockAnalysis(
        ticker=data.ticker.upper(),
        company_name=data.company_name,
        current_price=data.current_price,
        price_source=getattr(data, "price_source", "yfinance"),
        market_cap=data.market_cap,
        sector=data.sector,
        valuation_metrics=valuation,
        growth_metrics=growth,
        margin_metrics=margins,
        financial_health=health,
        fcf_profile=fcf,
        dcf=dcf,
        score=stock_score,
        verdict=stock_score.verdict,
        verdict_reasons=stock_score.reasons,
        warnings=warnings,
        summary=summary,
        analysis_date=date.today().isoformat(),
        data_quality=quality,
        missing_fields=missing,
    )
