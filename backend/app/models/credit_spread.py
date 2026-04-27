from pydantic import BaseModel
from typing import List, Optional


class SpreadScoreBreakdown(BaseModel):
    flow_score: int        # 0-30
    structure_score: int   # 0-30
    probability_score: int # 0-20
    historical_score: int  # 0-20
    total: int             # 0-100


class LHFScoreBreakdown(BaseModel):
    flow_clarity: int      # 0-25: directional dominance, grade, Vol/OI
    structure_safety: int  # 0-25: IV-adjusted sigma distance, DTE, delta
    regime: int            # 0-20: market-wide direction consensus (≥70% required)
    premium_quality: int   # 0-10: credit size vs risk:reward
    historical_edge: int   # 0-20: past performance from signals DB
    raw_total: int         # sum before penalties
    penalties: int         # total deducted (leveraged ETF, regime conflict, etc.)
    total: int             # final score (raw_total - penalties), floor 0


class LHFResult(BaseModel):
    classification: str        # LOW_HANGING_FRUIT / VALID_SETUP / ACTIVE_TRADER_SETUP / REJECT
    tier: str                  # display label
    score: LHFScoreBreakdown
    why_easy: List[str]        # bullet points explaining why it qualifies
    landmines: List[str]       # risk flags that remain
    warnings: List[str]        # explicit ⚠️ flags (impossible to ignore)
    reject_reasons: List[str]  # why it's not LHF (if applicable)
    lhf_blocked_by: Optional[str] = None  # hard-block reason even if score ≥ 85

    # ── Trade characterization ─────────────────────────────────────────────────
    trade_style: str = "PASSIVE"           # PASSIVE / ACTIVE / WATCH_ONLY
    gamma_risk: str = "LOW"                # HIGH / MEDIUM / LOW
    size_recommendation: str = "NORMAL"   # NORMAL / SMALL / NO_TRADE
    why_not_lhf: List[str] = []           # explicit LHF disqualifiers
    management: dict = {}                  # entry / stop / profit_taking / invalidation
    do_not_hold_blindly: bool = False      # True for ACTIVE_TRADER_SETUP


class FlowConfirmation(BaseModel):
    description: str
    vol_oi_ratio: float
    vol_notional: float
    conviction_grade: str
    tags: List[str]


class StructureContext(BaseModel):
    sell_strike_otm_pct: float
    dte: int
    expiration: str
    delta_at_sell: float
    notes: List[str]


class CreditSpreadResult(BaseModel):
    ticker: str
    spread_type: str              # "Bull Put Spread" | "Bear Call Spread"
    bias: str

    sell_strike: float
    buy_strike: float
    expiration: str
    dte: int

    premium: float                # net credit (per share)
    max_risk: float               # spread width - premium
    win_probability: float        # % based on 1 - sell_delta

    flow: FlowConfirmation
    structure: StructureContext
    score: SpreadScoreBreakdown

    verdict: str                  # "TAKE" | "SKIP"
    reject_reason: Optional[str] = None
    iv_at_sell: float = 0.0       # implied volatility of the short leg (for expected-move calc)

    # Populated after second-stage LHF filter
    lhf: Optional[LHFResult] = None


class SpreadScanResult(BaseModel):
    scanned_at: str
    tickers_scanned: List[str]
    spreads: List[CreditSpreadResult]
    rejected: List[dict]
    total_valid: int
    total_lhf: int = 0
