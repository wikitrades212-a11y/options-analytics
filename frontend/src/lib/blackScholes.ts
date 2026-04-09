/**
 * Black-Scholes pricing engine.
 * Single source of truth for all option pricing in this app.
 *
 * Assumptions:
 *   - European-style exercise
 *   - Constant risk-free rate (RISK_FREE_RATE below)
 *   - Constant IV (no vol surface / skew)
 *   - Continuous dividends not modeled
 */

export const RISK_FREE_RATE = 0.045; // ~current fed funds rate, update as needed

// ── Normal distribution helpers ───────────────────────────────────────────────

/**
 * CDF of the standard normal distribution.
 * Rational approximation from Abramowitz & Stegun §26.2.17, error < 1.5×10⁻⁷.
 */
export function normCDF(x: number): number {
  const sign = x >= 0 ? 1 : -1;
  const z = Math.abs(x) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * z);
  const poly =
    t * (0.254829592 +
    t * (-0.284496736 +
    t * (1.421413741 +
    t * (-1.453152027 +
    t * 1.061405429))));
  return 0.5 * (1 + sign * (1 - poly * Math.exp(-z * z)));
}

/** PDF of the standard normal distribution. */
export function normPDF(x: number): number {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

// ── Core pricing ──────────────────────────────────────────────────────────────

export interface BSInputs {
  S: number;      // spot price
  K: number;      // strike price
  T: number;      // time to expiration in years (0 = expired)
  r: number;      // risk-free rate, decimal (e.g. 0.045)
  sigma: number;  // implied volatility, decimal (e.g. 0.25 for 25%)
  type: "call" | "put";
}

export interface BSResult {
  price: number;   // fair value
  delta: number;   // ∂V/∂S
  gamma: number;   // ∂²V/∂S²
  theta: number;   // ∂V/∂t per calendar day (negative = decays)
  vega:  number;   // ∂V/∂σ per 1-point IV change (i.e. per 0.01 sigma)
  rho:   number;   // ∂V/∂r per 1-point rate change
  d1:    number;
  d2:    number;
}

export function bsPrice(inputs: BSInputs): BSResult {
  const { S, K, T, r, sigma, type } = inputs;

  // ── Edge: at or past expiration / degenerate inputs ──────────────────────
  if (T <= 0 || sigma <= 0 || S <= 0 || K <= 0) {
    const intrinsic =
      type === "call" ? Math.max(S - K, 0) : Math.max(K - S, 0);
    return {
      price: intrinsic,
      delta: type === "call" ? (S >= K ? 1 : 0) : (S <= K ? -1 : 0),
      gamma: 0, theta: 0, vega: 0, rho: 0, d1: 0, d2: 0,
    };
  }

  const sqrtT  = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT);
  const d2 = d1 - sigma * sqrtT;
  const Kdf   = K * Math.exp(-r * T); // discounted strike

  const Nd1 = normCDF(d1);
  const Nd2 = normCDF(d2);
  const nd1 = normPDF(d1);

  let price: number;
  let delta: number;
  let theta: number;
  let rho:   number;

  if (type === "call") {
    price = S * Nd1 - Kdf * Nd2;
    delta = Nd1;
    theta = (-S * nd1 * sigma / (2 * sqrtT) - r * Kdf * Nd2) / 365;
    rho   =  K * T * Math.exp(-r * T) * Nd2 / 100;
  } else {
    price = Kdf * (1 - Nd2) - S * (1 - Nd1);
    delta = Nd1 - 1;
    theta = (-S * nd1 * sigma / (2 * sqrtT) + r * Kdf * (1 - Nd2)) / 365;
    rho   = -K * T * Math.exp(-r * T) * (1 - Nd2) / 100;
  }

  const gamma = nd1 / (S * sigma * sqrtT);
  const vega  = S * nd1 * sqrtT / 100; // per 1 percentage-point of IV

  return {
    price: Math.max(price, 0),
    delta, gamma, theta, vega, rho, d1, d2,
  };
}

// ── Convenience wrappers ──────────────────────────────────────────────────────

/** DTE in calendar days → time in years. */
export function dteToYears(dte: number): number {
  return Math.max(dte, 0) / 365;
}

/**
 * Price a single option at a DIFFERENT stock price (same T, r, sigma).
 * Useful for charting P/L across stock-price scenarios.
 */
export function priceAt(
  S_new: number,
  K: number,
  T: number,
  r: number,
  sigma: number,
  type: "call" | "put",
): number {
  return bsPrice({ S: S_new, K, T, r, sigma, type }).price;
}

/**
 * Net P/L for a long single-leg option if the underlying moves to S_new.
 * entry = premium paid.
 */
export function longOptionPnL(
  S_new: number,
  K: number,
  T: number,
  sigma: number,
  entry: number,
  type: "call" | "put",
): number {
  return priceAt(S_new, K, T, RISK_FREE_RATE, sigma, type) - entry;
}

/**
 * Net P/L for a vertical spread (debit or credit) at S_new.
 *
 * @param longK    Strike of the long (bought) leg
 * @param longIV   IV of the long leg (decimal)
 * @param longPrem Entry premium of the long leg
 * @param shortK   Strike of the short (sold) leg
 * @param shortIV  IV of the short leg (decimal)
 * @param shortPrem Entry premium of the short leg
 * @param type     "call" or "put"
 */
export function spreadPnL(
  S_new: number,
  T: number,
  longK: number,  longIV: number,  longPrem: number,
  shortK: number, shortIV: number, shortPrem: number,
  type: "call" | "put",
): number {
  const longVal  = priceAt(S_new, longK,  T, RISK_FREE_RATE, longIV,  type);
  const shortVal = priceAt(S_new, shortK, T, RISK_FREE_RATE, shortIV, type);
  const netCost  = longPrem - shortPrem; // positive = debit, negative = credit
  return (longVal - shortVal) - netCost;
}

// ── Probability helpers ───────────────────────────────────────────────────────

/**
 * Probability of expiring ITM (risk-neutral), approximated by |delta|.
 * More precise: normCDF(d2) for calls, normCDF(-d2) for puts.
 */
export function probITM(
  S: number,
  K: number,
  T: number,
  sigma: number,
  type: "call" | "put",
): number {
  const { d2 } = bsPrice({ S, K, T, r: RISK_FREE_RATE, sigma, type });
  return type === "call" ? normCDF(d2) : normCDF(-d2);
}

// ── IV context classifier ─────────────────────────────────────────────────────

export type IVContext = "low" | "normal" | "elevated" | "high";

export function classifyIV(ivDecimal: number): IVContext {
  const pct = ivDecimal * 100;
  if (pct < 20) return "low";
  if (pct < 35) return "normal";
  if (pct < 55) return "elevated";
  return "high";
}

export function ivContextLabel(ctx: IVContext): string {
  switch (ctx) {
    case "low":      return "IV is low — options are cheap";
    case "normal":   return "IV is normal";
    case "elevated": return "IV is elevated — options are pricey";
    case "high":     return "IV is high — options are expensive; consider selling premium";
  }
}
