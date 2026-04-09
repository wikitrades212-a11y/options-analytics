/**
 * Strategy Scoring Engine
 *
 * Takes a CalculatorResponse + expected price → scores each recommended strategy
 * using Black-Scholes pricing. Returns ranked outcomes with explanations.
 *
 * Live value = BS price at expected stock price (same T as today).
 * This answers: "If the stock were at $X right now, what would my option be worth?"
 */

import {
  bsPrice, priceAt, dteToYears, classifyIV, ivContextLabel,
  normCDF, RISK_FREE_RATE, type IVContext,
} from "./blackScholes";
import type { CalculatorResponse, StrikeAnalysis } from "./types";

export type { IVContext };
export { classifyIV, ivContextLabel };

// ── Outcome types ─────────────────────────────────────────────────────────────

export interface StrategyOutcome {
  strike: StrikeAnalysis;
  tier: "aggressive" | "balanced" | "safer";

  // Live estimate: BS at Se, same T (option value if stock were at Se today)
  liveValue:         number;  // BS price
  livePnlPerShare:   number;  // liveValue - entryPremium
  livePnlPerContract:number;
  liveRoi:           number;  // %
  liveDelta:         number;  // delta at expected price

  // Expiration: intrinsic value (exact)
  expiryValue:         number;
  expiryPnlPerShare:   number;
  expiryPnlPerContract:number;
  expiryRoi:           number;

  // Structure
  entryPremium: number;
  breakeven:    number;
  maxLoss:      number;   // per share (always negative or 0)
  probITM:      number;   // 0–1, from BS d2

  // Scoring
  score:       number;    // 0–100
  scoreBreakdown: {
    roiScore:     number;
    probScore:    number;
    tierScore:    number;
    ivFitScore:   number;
  };

  // Pricing metadata (for journal and transparency)
  pricingMode:  "bs";          // always Black-Scholes in this engine
  ivUsed:       number;        // sigma (decimal) used for BS
  dteAtScore:   number;        // full DTE at time of scoring
  daysToTarget: number;        // T reduction applied (0 = "right now" scenario)
  tAtTarget:    number;        // effective T in years used for liveBS (= (dte - daysToTarget) / 365)
  riskFreeRate: number;        // r used for BS

  // Presentation
  tier_label:   string;   // "Aggressive" / "Balanced" / "Safer"
  rank_label:   string;   // "Best" / "2nd" / "3rd"
  explanation:  string;
  warnings:     string[];
  ivContext:    IVContext;
}

export interface ExpectationResult {
  outcomes:     StrategyOutcome[];  // sorted by score desc
  best:         StrategyOutcome | null;
  ivContext:    IVContext;
  ivContextMsg: string;
  expectedPrice: number;
  currentPrice:  number;
  expiration:    string;
  dte:           number;
  movePct:       number;
}

// ── Scoring ───────────────────────────────────────────────────────────────────

const RANK_LABELS = ["Best", "2nd", "3rd"];
const TIER_LABELS: Record<string, string> = {
  aggressive: "Aggressive",
  balanced:   "Balanced",
  safer:      "Safer",
};

function scoreStrike(
  strike: StrikeAnalysis,
  tier: "aggressive" | "balanced" | "safer",
  expectedPrice: number,
  currentPrice: number,
  dte: number,
  daysToTarget: number,   // how many days until expected move; reduces T for live pricing
  ivCtx: IVContext,
): StrategyOutcome {
  const { option_type, strike: K, mid: midPrem, implied_volatility: rawIV } = strike;
  const type = option_type as "call" | "put";
  const sigma = rawIV > 0 ? rawIV : 0.30; // fallback IV 30%

  // Full T (current option state) — used for currentBS / probITM / delta display
  const T     = dteToYears(dte);
  // Reduced T — time remaining AFTER the expected move occurs
  const T_at  = dteToYears(Math.max(dte - daysToTarget, 0));

  const entry = midPrem > 0 ? midPrem : 0.01;

  // ── Live BS pricing ────────────────────────────────────────────────────────
  // Uses T_at: the remaining time to expiry once the stock reaches expectedPrice in daysToTarget days.
  const liveBS = bsPrice({ S: expectedPrice, K, T: T_at, r: RISK_FREE_RATE, sigma, type });
  const liveValue = liveBS.price;
  const livePnl   = liveValue - entry;
  const liveRoi   = (livePnl / entry) * 100;

  // ── Expiration payoff ──────────────────────────────────────────────────────
  const expiryValue = type === "call"
    ? Math.max(expectedPrice - K, 0)
    : Math.max(K - expectedPrice, 0);
  const expiryPnl = expiryValue - entry;
  const expiryRoi = (expiryPnl / entry) * 100;

  // ── Structure ──────────────────────────────────────────────────────────────
  const breakeven = type === "call" ? K + entry : K - entry;
  const maxLoss   = -entry;

  // Probability ITM at current price, using full T (current option state)
  const currentBS = bsPrice({ S: currentPrice, K, T, r: RISK_FREE_RATE, sigma, type });
  const probITMVal = type === "call" ? normCDF(currentBS.d2) : normCDF(-currentBS.d2);

  // ── Scoring (0–100) ────────────────────────────────────────────────────────
  // ROI at expected price (live BS), capped
  const roiScore = Math.min(Math.max(liveRoi / 3, 0), 40);

  // Probability / delta proxy
  const deltaAbs = Math.abs(currentBS.delta);
  const probScore = deltaAbs * 25;

  // Tier: balanced is ideal for most traders
  const tierScore =
    tier === "balanced" ? 20 :
    tier === "safer"    ? 14 : 8;

  // IV environment fit for buying options:
  // low IV → good for buying, high IV → expensive
  const ivFitScore =
    ivCtx === "low"      ? 15 :
    ivCtx === "normal"   ? 11 :
    ivCtx === "elevated" ?  6 : 3;

  const score = Math.min(100, Math.round(roiScore + probScore + tierScore + ivFitScore));

  // ── Explanation ────────────────────────────────────────────────────────────
  let explanation: string;
  if (livePnl > 0) {
    explanation = `+${liveRoi.toFixed(0)}% ROI if stock reaches $${expectedPrice.toFixed(2)} before expiry (Black-Scholes).`;
  } else if (liveRoi > -30) {
    explanation = `Small loss (${liveRoi.toFixed(0)}% ROI) — stock needs to clear $${breakeven.toFixed(2)} to profit.`;
  } else {
    explanation = `Loss territory — breakeven at $${breakeven.toFixed(2)}, ${Math.abs(liveRoi).toFixed(0)}% below expected.`;
  }

  // ── Warnings ──────────────────────────────────────────────────────────────
  const warnings: string[] = [];
  const ivPct = sigma * 100;
  if (ivPct > 55) warnings.push("IV > 55% — premium is very expensive");
  else if (ivPct > 40) warnings.push("IV is elevated — option is pricey");
  if (dte <= 5)        warnings.push("≤ 5 DTE — theta decay is severe");
  else if (dte <= 14)  warnings.push("≤ 14 DTE — watch theta carefully");
  if (deltaAbs < 0.15) warnings.push("Very low delta — high risk, low probability");
  if (deltaAbs > 0.75) warnings.push("Deep ITM — limited leverage");
  if (livePnl < 0)     warnings.push("At your expected price, this is still below breakeven");

  return {
    strike,
    tier,
    liveValue,
    livePnlPerShare:   livePnl,
    livePnlPerContract: livePnl * 100,
    liveRoi,
    liveDelta: liveBS.delta,
    expiryValue,
    expiryPnlPerShare:    expiryPnl,
    expiryPnlPerContract: expiryPnl * 100,
    expiryRoi,
    entryPremium: entry,
    breakeven,
    maxLoss,
    probITM: probITMVal,
    score,
    scoreBreakdown: { roiScore, probScore, tierScore, ivFitScore },
    pricingMode:  "bs" as const,
    ivUsed:       sigma,
    dteAtScore:   dte,
    daysToTarget,
    tAtTarget:    T_at,
    riskFreeRate: RISK_FREE_RATE,
    tier_label:  TIER_LABELS[tier] ?? tier,
    rank_label:  "—",   // set after sort
    explanation,
    warnings,
    ivContext: ivCtx,
  };
}

// ── Public API ────────────────────────────────────────────────────────────────

export function buildExpectationResult(
  data: CalculatorResponse,
  expectedPrice: number,
  daysToTarget: number = 0,
): ExpectationResult {
  const { current_price, expiration, dte, move_pct } = data;

  // IV context from average of available recommended strikes
  const ivSamples = [
    data.recommended_aggressive,
    data.recommended_balanced,
    data.recommended_safer,
  ]
    .filter(Boolean)
    .map(s => s!.implied_volatility)
    .filter(iv => iv > 0);

  const avgIV   = ivSamples.length ? ivSamples.reduce((a, b) => a + b, 0) / ivSamples.length : 0.30;
  const ivCtx   = classifyIV(avgIV);
  const ivMsg   = ivContextLabel(ivCtx);

  const outcomes: StrategyOutcome[] = [];

  const candidates: Array<[StrikeAnalysis | null, "aggressive" | "balanced" | "safer"]> = [
    [data.recommended_aggressive, "aggressive"],
    [data.recommended_balanced,   "balanced"],
    [data.recommended_safer,      "safer"],
  ];

  for (const [strike, tier] of candidates) {
    if (strike) {
      outcomes.push(scoreStrike(strike, tier, expectedPrice, current_price, dte, daysToTarget, ivCtx));
    }
  }

  // Sort by composite score
  outcomes.sort((a, b) => b.score - a.score);

  // Assign rank labels
  outcomes.forEach((o, i) => {
    o.rank_label = RANK_LABELS[i] ?? `#${i + 1}`;
  });

  return {
    outcomes,
    best:          outcomes[0] ?? null,
    ivContext:     ivCtx,
    ivContextMsg:  ivMsg,
    expectedPrice,
    currentPrice:  current_price,
    expiration,
    dte,
    movePct:       move_pct,
  };
}
