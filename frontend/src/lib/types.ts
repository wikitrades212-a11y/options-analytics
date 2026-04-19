export type OptionType = "call" | "put";

// ── Calculator ────────────────────────────────────────────────────────────────

export type StrikeTier = "aggressive" | "balanced" | "safer" | "avoid";

export interface StrikeAnalysis {
  strike: number;
  expiration: string;
  option_type: OptionType;
  bid: number;
  ask: number;
  mid: number;
  mark: number;
  volume: number;
  open_interest: number;
  implied_volatility: number;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  moneyness_pct: number;
  intrinsic_at_target: number;
  estimated_value_at_target: number;
  estimated_roi_pct: number;
  breakeven: number;
  breakeven_move_pct: number;
  liquidity_score: number;
  spread_pct: number;
  tier: StrikeTier;
  avoid_reasons: string[];
  badges: string[];
  ideal_max_entry: number;
  contracts_for_risk: number | null;
}

export interface CalculatorResponse {
  ticker: string;
  current_price: number;
  target_price: number;
  move_pct: number;
  option_type: OptionType;
  expiration: string;
  dte: number;
  expiry_fit_score: number;
  recommended_aggressive: StrikeAnalysis | null;
  recommended_balanced: StrikeAnalysis | null;
  recommended_safer: StrikeAnalysis | null;
  avoid_list: StrikeAnalysis[];
  all_strikes: StrikeAnalysis[];
}

export interface CalculatorParams {
  ticker: string;
  current_price: number;
  target_price: number;
  option_type: "call" | "put" | "auto";
  expiration: string;
  max_premium?: number;
  preferred_strike?: number;
  account_size?: number;
  risk_per_trade?: number;
}

export interface OptionContract {
  ticker: string;
  strike: number;
  expiration: string;
  option_type: OptionType;
  bid: number;
  ask: number;
  mid: number;
  last: number;
  mark: number;
  volume: number;
  open_interest: number;
  implied_volatility: number;
  oi_notional: number;
  vol_notional: number;
  vol_oi_ratio: number;
  unusual_score: number;
  unusual_rank: number;
  reason_tags: string[];
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  rho: number | null;
  underlying_price: number | null;
  moneyness: number | null;
}

export interface OptionChainResponse {
  ticker: string;
  underlying_price: number;
  timestamp: string;
  expirations: string[];
  contracts: OptionContract[];
  total_call_oi: number;
  total_put_oi: number;
  total_call_volume: number;
  total_put_volume: number;
  call_put_ratio: number;
}

export interface UnusualOptionsResponse {
  ticker: string;
  underlying_price: number;
  timestamp: string;
  top_calls: OptionContract[];
  top_puts: OptionContract[];
  combined: OptionContract[];
  total_unusual_flow: number;
}

export interface TopContractsResponse {
  ticker: string;
  underlying_price: number;
  timestamp: string;
  metric: string;
  contracts: OptionContract[];
}

export interface ExpirationResponse {
  ticker: string;
  expirations: string[];
  timestamp: string;
}

export type SortMetric =
  | "open_interest"
  | "oi_notional"
  | "volume"
  | "vol_notional"
  | "unusual_score";

export interface ChainFilters {
  expiration: string;
  optionType: "call" | "put" | "both";
  minOI: number;
  minVolume: number;
  sortBy: SortMetric;
  searchTicker: string;
}

export interface UnusualFilters {
  optionType: "call" | "put" | "both";
  minScore: number;
  expiry: "all" | "nearest" | "weeklies";
  tags: string[];
}

// ── Stock Fundamentals ────────────────────────────────────────────────────────

export interface GrowthMetrics {
  revenue_cagr_3y: number | null;
  revenue_growth_yoy: number | null;
  net_income_growth_yoy: number | null;
  eps_growth_yoy: number | null;
  fcf_growth_yoy: number | null;
  fcf_cagr_3y: number | null;
}

export interface MarginMetrics {
  gross_margin: number | null;
  operating_margin: number | null;
  net_margin: number | null;
  fcf_margin: number | null;
}

export interface FinancialHealthMetrics {
  debt_to_equity: number | null;
  current_ratio: number | null;
  cash_position: number | null;
  total_debt: number | null;
  net_debt: number | null;
  interest_coverage: number | null;
  debt_level: string | null;
  liquidity: string | null;
}

export interface FCFProfile {
  years: number[];
  values: number[];
  is_positive_all_years: boolean;
  is_growing: boolean;
  consistency: string;
  latest_fcf: number | null;
  avg_fcf_3y: number | null;
}

export interface ValuationMetrics {
  pe_ratio: number | null;
  forward_pe: number | null;
  peg_ratio: number | null;
  price_to_sales: number | null;
  price_to_book: number | null;
  ev_to_ebitda: number | null;
  fcf_yield: number | null;
}

export interface DCFResult {
  intrinsic_value_per_share: number | null;
  current_price: number;
  upside_downside_pct: number | null;
  terminal_value: number | null;
  pv_of_cash_flows: number | null;
  projected_growth_rate: number | null;
  confidence: "high" | "medium" | "low";
  confidence_reasons: string[];
  is_reliable: boolean;
  explanation: string | null;
}

export interface ScoreBreakdown {
  business_quality: number;
  financial_strength: number;
  valuation: number;
  risk_stability: number;
  total: number;
}

export interface StockScore {
  score: ScoreBreakdown;
  confidence: "high" | "medium" | "low";
  verdict: string;
  reasons: string[];
}

export interface StockAnalysis {
  ticker: string;
  company_name: string;
  current_price: number;
  market_cap: number | null;
  sector: string | null;
  valuation_metrics: ValuationMetrics;
  growth_metrics: GrowthMetrics;
  margin_metrics: MarginMetrics;
  financial_health: FinancialHealthMetrics;
  fcf_profile: FCFProfile;
  dcf: DCFResult;
  score: StockScore;
  verdict: string;
  verdict_reasons: string[];
  warnings: string[];
  summary: string;
  analysis_date: string | null;
  data_quality: "good" | "partial" | "limited";
  missing_fields: string[];
}
