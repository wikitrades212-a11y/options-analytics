"use client";

/**
 * ExpectationPanel — "If price reaches $X, here's what each trade returns"
 *
 * Core decision engine UI. Shows Black-Scholes-priced outcomes for every
 * recommended strategy at the user's expected price.
 */

import clsx from "clsx";
import { Target, TrendingUp, TrendingDown, AlertTriangle, Info } from "lucide-react";
import {
  buildExpectationResult,
  type StrategyOutcome,
  type ExpectationResult,
} from "@/lib/strategyEngine";
import type { CalculatorResponse } from "@/lib/types";

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt$(n: number, decimals = 2): string {
  return (n >= 0 ? "+" : "") + "$" + Math.abs(n).toFixed(decimals);
}

function fmtPct(n: number, decimals = 1): string {
  return (n >= 0 ? "+" : "") + n.toFixed(decimals) + "%";
}

function formatExpiry(exp: string): string {
  if (!exp) return "";
  try {
    return new Date(`${exp}T12:00:00Z`).toLocaleDateString("en-US", {
      month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
    });
  } catch { return exp; }
}

// ── IV context banner ─────────────────────────────────────────────────────────

function IVBanner({ result }: { result: ExpectationResult }) {
  const { ivContext, ivContextMsg } = result;

  const cls =
    ivContext === "low"      ? "border-call/30 bg-call/5 text-call" :
    ivContext === "normal"   ? "border-bg-border bg-bg-raised text-text-muted" :
    ivContext === "elevated" ? "border-warn/30 bg-warn/5 text-warn" :
                               "border-put/30 bg-put/5 text-put";

  return (
    <div className={clsx("flex items-center gap-2 rounded-lg border px-3 py-2 text-xs", cls)}>
      <Info className="w-3.5 h-3.5 shrink-0" />
      <span>{ivContextMsg}</span>
    </div>
  );
}

// ── Score ring ────────────────────────────────────────────────────────────────

function ScoreBadge({ score }: { score: number }) {
  const cls =
    score >= 70 ? "bg-call/15 text-call border-call/30" :
    score >= 45 ? "bg-warn/15 text-warn border-warn/30" :
                  "bg-put/15 text-put border-put/30";
  return (
    <span className={clsx(
      "inline-flex items-center justify-center w-9 h-9 rounded-full border-2 font-bold text-xs shrink-0",
      cls,
    )}>
      {score}
    </span>
  );
}

// ── Single outcome card ───────────────────────────────────────────────────────

function OutcomeCard({
  outcome,
  isTop,
}: {
  outcome: StrategyOutcome;
  isTop: boolean;
}) {
  const pnlColor = outcome.livePnlPerShare >= 0 ? "text-call" : "text-put";
  const roiColor = outcome.liveRoi >= 0 ? "text-call" : "text-put";

  return (
    <div className={clsx(
      "rounded-xl border p-4 space-y-3 transition-all",
      isTop
        ? "border-accent/40 bg-accent/5 ring-1 ring-accent/20"
        : "border-bg-border bg-bg-raised"
    )}>
      {/* Card header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <ScoreBadge score={outcome.score} />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              {isTop && (
                <span className="text-2xs font-bold px-1.5 py-0.5 rounded bg-accent text-white shrink-0">
                  BEST
                </span>
              )}
              <span className="text-2xs font-bold px-1.5 py-0.5 rounded bg-bg-hover text-text-muted border border-bg-border shrink-0">
                {outcome.tier_label.toUpperCase()}
              </span>
              <span className="text-sm font-semibold font-mono text-text-primary">
                ${outcome.strike.strike} {outcome.strike.option_type.toUpperCase()}
              </span>
            </div>
            <p className="text-2xs text-text-muted mt-0.5">
              Entry mid: <span className="font-mono">${outcome.entryPremium.toFixed(2)}</span>
              {" · "}
              IV: <span className="font-mono">{(outcome.strike.implied_volatility * 100).toFixed(1)}%</span>
              {" · "}
              Δ: <span className="font-mono">{outcome.strike.delta?.toFixed(2) ?? "—"}</span>
            </p>
          </div>
        </div>

        {/* P/L at expected price */}
        <div className="text-right shrink-0">
          <p className={clsx("text-lg font-bold font-mono leading-none", roiColor)}>
            {fmtPct(outcome.liveRoi, 0)}
          </p>
          <p className="text-2xs text-text-muted">ROI (live)</p>
        </div>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-3 gap-2">
        <Stat
          label="Est. P/L / share"
          value={fmt$(outcome.livePnlPerShare)}
          color={pnlColor}
        />
        <Stat
          label="Est. P/L / contract"
          value={fmt$(outcome.livePnlPerContract)}
          color={pnlColor}
        />
        <Stat
          label="P/L at expiry"
          value={fmt$(outcome.expiryPnlPerShare)}
          color={outcome.expiryPnlPerShare >= 0 ? "text-call" : "text-put"}
          sub={`${fmtPct(outcome.expiryRoi, 0)} ROI`}
        />
        <Stat label="Breakeven" value={`$${outcome.breakeven.toFixed(2)}`} />
        <Stat label="Max loss / share" value={`$${outcome.maxLoss.toFixed(2)}`} color="text-put" />
        <Stat label="Prob. ITM" value={`${(outcome.probITM * 100).toFixed(0)}%`} />
      </div>

      {/* Explanation */}
      <p className="text-xs text-text-secondary leading-relaxed">
        {outcome.explanation}
      </p>

      {/* Warnings */}
      {outcome.warnings.length > 0 && (
        <div className="space-y-1">
          {outcome.warnings.map((w, i) => (
            <div key={i} className="flex items-center gap-1.5 text-2xs text-warn">
              <AlertTriangle className="w-3 h-3 shrink-0" />
              {w}
            </div>
          ))}
        </div>
      )}

      {/* BS footnote */}
      <p className="text-2xs text-text-muted opacity-70">
        Est. via Black-Scholes · IV: {(outcome.ivUsed * 100).toFixed(1)}%
        {outcome.daysToTarget > 0
          ? ` · T reduced by ${outcome.daysToTarget}d (${outcome.dteAtScore - outcome.daysToTarget}d remaining)`
          : ` · DTE: ${outcome.dteAtScore}d`}
        {" · "}r = {(outcome.riskFreeRate * 100).toFixed(1)}%
      </p>
    </div>
  );
}

function Stat({
  label, value, color, sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div className="space-y-0.5">
      <p className="text-2xs text-text-muted">{label}</p>
      <p className={clsx("text-xs font-semibold font-mono", color ?? "text-text-primary")}>
        {value}
      </p>
      {sub && <p className="text-2xs text-text-muted">{sub}</p>}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  data: CalculatorResponse;
  expectedPrice: number;
  daysToTarget?: number;  // 0 = "right now at Se"; N = T reduced by N days
}

export default function ExpectationPanel({ data, expectedPrice, daysToTarget = 0 }: Props) {
  if (!data || expectedPrice <= 0) return null;

  const result: ExpectationResult = buildExpectationResult(data, expectedPrice, daysToTarget);

  if (result.outcomes.length === 0) {
    return (
      <div className="card text-center text-sm text-text-muted py-8">
        No recommended strikes to score. Adjust your parameters.
      </div>
    );
  }

  const moveDir   = expectedPrice >= result.currentPrice ? "up" : "down";
  const movePct   = ((expectedPrice - result.currentPrice) / result.currentPrice * 100);
  const movePctFmt = (movePct >= 0 ? "+" : "") + movePct.toFixed(2) + "%";

  return (
    <div className="space-y-4">
      {/* ── CONTEXT HEADER ─────────────────────────────────────────────────── */}
      <div className="card flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
        <div className="flex items-center gap-2">
          <Target className="w-4 h-4 text-accent shrink-0" />
          <span className="font-semibold text-text-primary text-sm">
            If {data.ticker} reaches{" "}
            <span className="text-accent">${expectedPrice.toFixed(2)}</span>
          </span>
        </div>

        <span className="text-text-muted">
          From{" "}
          <span className="font-mono text-text-secondary">${result.currentPrice.toFixed(2)}</span>
          {" → "}
          <span className={clsx(
            "font-mono font-semibold",
            moveDir === "up" ? "text-call" : "text-put"
          )}>
            {movePctFmt}
          </span>
        </span>

        <span className="text-text-muted flex items-center gap-1">
          <span>Expiry:</span>
          <span className="font-mono text-text-secondary">
            {formatExpiry(result.expiration)}
          </span>
          <span className="ml-1 text-2xs text-text-muted">({result.dte}d)</span>
        </span>

        <span className="ml-auto text-2xs text-text-muted">
          Black-Scholes · live estimate
        </span>
      </div>

      {/* ── IV CONTEXT ─────────────────────────────────────────────────────── */}
      <IVBanner result={result} />

      {/* ── OUTCOME CARDS ──────────────────────────────────────────────────── */}
      <div className="space-y-3">
        {result.outcomes.map((outcome, i) => (
          <OutcomeCard
            key={outcome.strike.strike + outcome.tier}
            outcome={outcome}
            isTop={i === 0}
          />
        ))}
      </div>

      {/* ── MOVE CONTEXT ───────────────────────────────────────────────────── */}
      <div className="rounded-lg border border-bg-border px-3 py-2 flex items-start gap-2 text-2xs text-text-muted">
        {moveDir === "up"
          ? <TrendingUp className="w-3.5 h-3.5 text-call shrink-0 mt-0.5" />
          : <TrendingDown className="w-3.5 h-3.5 text-put shrink-0 mt-0.5" />}
        <span>
          Scores use Black-Scholes at your expected price with the option&apos;s IV.
          {daysToTarget > 0
            ? ` T is reduced by ${daysToTarget}d (stock reaches target in ${daysToTarget} days).`
            : " T is unchanged (stock at target right now scenario)."}
          {" "}Assumes constant IV and risk-free rate. Expiry P/L is exact intrinsic value.
        </span>
      </div>
    </div>
  );
}
