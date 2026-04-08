from pydantic import BaseModel
from typing import Optional, List, Literal


class CalculatorRequest(BaseModel):
    ticker: str
    current_price: float
    target_price: float
    option_type: Literal["call", "put", "auto"]  # auto = infer from direction
    expiration: str                               # YYYY-MM-DD
    max_premium: Optional[float] = None
    preferred_strike: Optional[float] = None
    account_size: Optional[float] = None
    risk_per_trade: Optional[float] = None        # dollars at risk
    strategy_mode: str = "Intraday"


class StrikeAnalysis(BaseModel):
    # Identity
    strike: float
    expiration: str
    option_type: Literal["call", "put"]

    # Pricing
    bid: float
    ask: float
    mid: float
    mark: float

    # Activity
    volume: int
    open_interest: int
    implied_volatility: float

    # Greeks
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]

    # Derived
    moneyness_pct: float          # (strike - current) / current * 100
    intrinsic_at_target: float    # max(target - strike, 0) for calls
    estimated_value_at_target: float
    estimated_roi_pct: float
    breakeven: float              # strike ± premium
    breakeven_move_pct: float     # % move needed from current to breakeven

    # Existing quality scores (0–100)
    liquidity_score: float
    spread_pct: float             # (ask - bid) / mid * 100

    # Classification
    tier: Literal["aggressive", "balanced", "safer", "avoid"]
    avoid_reasons: List[str]
    badges: List[str]

    # Max entry (existing field)
    ideal_max_entry: float        # don't pay more than this
    contracts_for_risk: Optional[int]   # how many contracts given risk $ input

    # ── Trade quality composite ───────────────────────────────────────────────
    trade_quality_score: float = 0.0   # 0–100
    trade_quality_grade: str = "D"     # A / B / C / D

    # ── Sub-scores (0–100) ────────────────────────────────────────────────────
    target_fit_score: float = 0.0
    expiry_fit_score: float = 0.0
    spread_score: float = 0.0
    delta_quality_score: float = 0.0
    gamma_quality_score: float = 0.0
    theta_quality_score: float = 0.0
    roi_score: float = 0.0
    premium_efficiency_score: float = 0.0
    iv_fairness_score: float = 0.0
    realism_score: float = 0.0

    # ── Confidence breakdown (0–100) ──────────────────────────────────────────
    direction_confidence_score: float = 0.0
    contract_quality_score: float = 0.0
    execution_quality_score: float = 0.0
    risk_quality_score: float = 0.0

    # ── Execution plan ────────────────────────────────────────────────────────
    ideal_entry: float = 0.0
    chase_limit: float = 0.0
    soft_stop: float = 0.0
    hard_stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0

    # ── Thesis validation ─────────────────────────────────────────────────────
    current_stock_price: float = 0.0
    target_stock_price: float = 0.0
    expected_stock_move_pct: float = 0.0
    move_required_to_breakeven_pct: float = 0.0
    move_required_to_tp1_pct: float = 0.0
    move_required_to_tp2_pct: float = 0.0
    thesis_verdict: str = "Not Worth It"

    # ── IV context ────────────────────────────────────────────────────────────
    iv_context_label: str = "IV Fair"
    iv_context_score: float = 0.0     # 0–100

    # ── Strategy mode ─────────────────────────────────────────────────────────
    strategy_mode: str = "Intraday"

    # ── Alert-ready fields ────────────────────────────────────────────────────
    alert_entry_ready: bool = False
    alert_below_ideal_entry: bool = False
    alert_spread_improving: bool = False
    alert_score_above_threshold: bool = False
    alert_stock_near_target: bool = False
    alert_tp1_hit: bool = False
    alert_tp2_hit: bool = False

    # ── Explanation ───────────────────────────────────────────────────────────
    explanation: str = ""


class CalculatorResponse(BaseModel):
    ticker: str
    current_price: float
    target_price: float
    move_pct: float               # (target - current) / current * 100
    option_type: Literal["call", "put"]
    expiration: str
    dte: int                      # days to expiration from today
    expiry_fit_score: float       # [0, 1] — how well DTE matches move magnitude
    strategy_mode: str = "Intraday"

    recommended_aggressive: Optional[StrikeAnalysis]
    recommended_balanced: Optional[StrikeAnalysis]
    recommended_safer: Optional[StrikeAnalysis]
    avoid_list: List[StrikeAnalysis]
    all_strikes: List[StrikeAnalysis]
