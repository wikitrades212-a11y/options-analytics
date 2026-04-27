"""
Stock Fundamentals Models
All percentage values are stored as ratios (0.12 = 12%).
The formatter layer multiplies by 100 for display.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


# ── Raw Input Models (from data provider) ────────────────────────────────────

class IncomeStatementRow(BaseModel):
    year: int
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_income: Optional[float] = None
    net_income: Optional[float] = None
    eps: Optional[float] = None
    ebitda: Optional[float] = None
    interest_expense: Optional[float] = None  # typically negative


class BalanceSheetRow(BaseModel):
    year: int
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    total_debt: Optional[float] = None            # long-term + short-term debt
    cash_and_equivalents: Optional[float] = None
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None


class CashFlowRow(BaseModel):
    year: int
    operating_cash_flow: Optional[float] = None
    capital_expenditures: Optional[float] = None  # may be negative or positive
    free_cash_flow: Optional[float] = None         # if None, derived from ocf + capex


class RawStockData(BaseModel):
    """Input contract. Provide as much as available; engine degrades gracefully on gaps."""
    ticker: str
    company_name: str
    current_price: float
    price_source: Optional[str] = "yfinance"  # tradier | alpaca | robinhood | yfinance
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    sector: Optional[str] = None
    beta: Optional[float] = None

    income_statements: List[IncomeStatementRow] = Field(default_factory=list)
    balance_sheets: List[BalanceSheetRow] = Field(default_factory=list)
    cash_flows: List[CashFlowRow] = Field(default_factory=list)

    # Optional pre-computed / API-provided fields
    forward_pe: Optional[float] = None
    analyst_target_price: Optional[float] = None
    ttm_revenue: Optional[float] = None
    ttm_net_income: Optional[float] = None
    ttm_fcf: Optional[float] = None
    ttm_ebitda: Optional[float] = None
    enterprise_value: Optional[float] = None


# ── Computed Output Models ────────────────────────────────────────────────────

class GrowthMetrics(BaseModel):
    revenue_cagr_3y: Optional[float] = None        # ratio, e.g. 0.12 = 12%
    revenue_growth_yoy: Optional[float] = None
    net_income_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None
    fcf_growth_yoy: Optional[float] = None
    fcf_cagr_3y: Optional[float] = None


class MarginMetrics(BaseModel):
    gross_margin: Optional[float] = None           # ratio
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    fcf_margin: Optional[float] = None             # FCF / Revenue


class FinancialHealthMetrics(BaseModel):
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    cash_position: Optional[float] = None          # absolute dollars
    total_debt: Optional[float] = None
    net_debt: Optional[float] = None               # total_debt - cash (can be negative)
    interest_coverage: Optional[float] = None      # EBIT / interest expense
    debt_level: Optional[str] = None               # "Low" | "Moderate" | "High" | "Extreme"
    liquidity: Optional[str] = None                # "Strong" | "Adequate" | "Weak"


class FCFProfile(BaseModel):
    years: List[int] = Field(default_factory=list)
    values: List[float] = Field(default_factory=list)
    is_positive_all_years: bool = False
    is_growing: bool = False
    consistency: str = "Unknown"  # "Strong" | "Moderate" | "Weak" | "Unstable" | "Negative"
    latest_fcf: Optional[float] = None
    avg_fcf_3y: Optional[float] = None


class ValuationMetrics(BaseModel):
    pe_ratio: Optional[float] = None               # absolute multiplier
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    price_to_sales: Optional[float] = None
    price_to_book: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    fcf_yield: Optional[float] = None              # ratio, e.g. 0.045 = 4.5%


class DCFConfig(BaseModel):
    projection_years: int = 10
    discount_rate: float = 0.10                    # WACC
    terminal_growth_rate: float = 0.03
    growth_method: str = "conservative"            # "conservative" | "historical_average" | "capped_growth"
    growth_cap: float = 0.25                       # ceiling for capped_growth mode
    min_growth: float = -0.05                      # floor on projected growth rate


class DCFResult(BaseModel):
    intrinsic_value_per_share: Optional[float] = None
    current_price: float
    upside_downside_pct: Optional[float] = None    # ratio, e.g. 0.25 = 25% upside
    terminal_value: Optional[float] = None         # PV of terminal value
    pv_of_cash_flows: Optional[float] = None       # PV of projection period CFs
    projected_growth_rate: Optional[float] = None  # ratio used in model
    confidence: str = "low"                        # "high" | "medium" | "low"
    confidence_reasons: List[str] = Field(default_factory=list)
    is_reliable: bool = False
    explanation: Optional[str] = None
    config: Optional[DCFConfig] = None


class ScoreBreakdown(BaseModel):
    business_quality: float    # 0–35
    financial_strength: float  # 0–20
    valuation: float           # 0–30
    risk_stability: float      # 0–15
    total: float               # 0–100


class StockScore(BaseModel):
    score: ScoreBreakdown
    confidence: str            # "high" | "medium" | "low"
    verdict: str
    reasons: List[str] = Field(default_factory=list)


class StockAnalysis(BaseModel):
    ticker: str
    company_name: str
    current_price: float
    price_source: Optional[str] = "yfinance"
    market_cap: Optional[float] = None
    sector: Optional[str] = None

    valuation_metrics: ValuationMetrics
    growth_metrics: GrowthMetrics
    margin_metrics: MarginMetrics
    financial_health: FinancialHealthMetrics
    fcf_profile: FCFProfile
    dcf: DCFResult
    score: StockScore

    verdict: str
    verdict_reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    summary: str

    analysis_date: Optional[str] = None
    data_quality: str = "good"             # "good" | "partial" | "limited"
    missing_fields: List[str] = Field(default_factory=list)
