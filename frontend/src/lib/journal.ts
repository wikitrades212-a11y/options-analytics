/**
 * Journal — full scenario persistence.
 * Each entry stores enough to reload the exact calculator scenario.
 */

import type { CalculatorParams } from "./types";
import type { StrategyOutcome } from "./strategyEngine";

export const JOURNAL_KEY = "oa_journal_v2";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface JournalOutcomeSnapshot {
  strike:    number;
  tier:      string;
  liveRoi:   number;
  expiryRoi: number;
  iv:        number;   // decimal
  breakeven: number;
  livePnlPerShare: number;
  // Pricing metadata — for audit/transparency
  pricingMode:  string;   // "bs"
  dteAtScore:   number;
  daysToTarget: number;
  riskFreeRate: number;
}

export interface JournalScenario {
  id:        string;
  timestamp: string;   // ISO 8601
  timezone:  string;   // IANA, e.g. "America/New_York"

  // Calculator params — needed for reload
  calParams: CalculatorParams;

  // Snapshot fields (for display without re-running)
  ticker:          string;
  currentPrice:    number;
  expectedPrice:   number;
  expiration:      string;
  dte:             number;
  optionType:      string;
  movePct:         number;
  expiryFitScore:  number;
  strategyMode:    "single" | "compare";
  compareExpiry?:  string;

  // Top outcome snapshot
  topOutcome?: JournalOutcomeSnapshot;

  // Full outcomes (all ranked strategies)
  outcomes?: JournalOutcomeSnapshot[];
}

// ── CRUD helpers ──────────────────────────────────────────────────────────────

export function loadJournal(): JournalScenario[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(JOURNAL_KEY) || "[]");
  } catch {
    return [];
  }
}

export function saveJournalToStorage(entries: JournalScenario[]): void {
  localStorage.setItem(JOURNAL_KEY, JSON.stringify(entries));
}

export function addJournalEntry(
  entries: JournalScenario[],
  entry: JournalScenario,
): JournalScenario[] {
  const updated = [entry, ...entries];
  saveJournalToStorage(updated);
  return updated;
}

export function deleteJournalEntry(
  entries: JournalScenario[],
  id: string,
): JournalScenario[] {
  const updated = entries.filter(e => e.id !== id);
  saveJournalToStorage(updated);
  return updated;
}

export function clearJournal(): JournalScenario[] {
  localStorage.removeItem(JOURNAL_KEY);
  return [];
}

// ── Build entry from calc result ──────────────────────────────────────────────

export function buildJournalEntry(
  calParams: CalculatorParams,
  calcData: {
    ticker: string;
    current_price: number;
    target_price: number;   // = expectedPrice
    expiration: string;
    dte: number;
    option_type: string;
    move_pct: number;
    expiry_fit_score: number;
  },
  outcomes: StrategyOutcome[],
  strategyMode: "single" | "compare",
  compareExpiry?: string,
): JournalScenario {
  const tz = typeof Intl !== "undefined"
    ? Intl.DateTimeFormat().resolvedOptions().timeZone
    : "UTC";

  const toSnapshot = (o: StrategyOutcome): JournalOutcomeSnapshot => ({
    strike:          o.strike.strike,
    tier:            o.tier,
    liveRoi:         o.liveRoi,
    expiryRoi:       o.expiryRoi,
    iv:              o.strike.implied_volatility,
    breakeven:       o.breakeven,
    livePnlPerShare: o.livePnlPerShare,
    pricingMode:     o.pricingMode,
    dteAtScore:      o.dteAtScore,
    daysToTarget:    o.daysToTarget,
    riskFreeRate:    o.riskFreeRate,
  });

  return {
    id:             Date.now().toString(),
    timestamp:      new Date().toISOString(),
    timezone:       tz,
    calParams:      { ...calParams },
    ticker:         calcData.ticker,
    currentPrice:   calcData.current_price,
    expectedPrice:  calcData.target_price,
    expiration:     calcData.expiration,
    dte:            calcData.dte,
    optionType:     calcData.option_type,
    movePct:        calcData.move_pct,
    expiryFitScore: calcData.expiry_fit_score,
    strategyMode,
    compareExpiry,
    topOutcome:  outcomes[0] ? toSnapshot(outcomes[0]) : undefined,
    outcomes:    outcomes.map(toSnapshot),
  };
}

// ── Formatting ────────────────────────────────────────────────────────────────

export function formatScenarioDate(timestamp: string, timezone?: string): string {
  try {
    return new Date(timestamp).toLocaleString("en-US", {
      timeZone: timezone || "UTC",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return timestamp;
  }
}
