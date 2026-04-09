"use client";

import { useState, useMemo, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from "recharts";
import clsx from "clsx";
import { ChevronDown, ChevronUp, TrendingUp, Activity } from "lucide-react";
import { bsPrice, dteToYears, RISK_FREE_RATE } from "@/lib/blackScholes";

// ── Strategy types ────────────────────────────────────────────────────────────

type StrategyType =
  | "long_call"
  | "long_put"
  | "call_debit_spread"
  | "put_debit_spread"
  | "call_credit_spread"
  | "put_credit_spread"
  | "covered_call"
  | "cash_secured_put";

const STRATEGY_LABELS: Record<StrategyType, string> = {
  long_call:           "Long Call",
  long_put:            "Long Put",
  call_debit_spread:   "Call Debit Spread",
  put_debit_spread:    "Put Debit Spread",
  call_credit_spread:  "Call Credit Spread",
  put_credit_spread:   "Put Credit Spread",
  covered_call:        "Covered Call",
  cash_secured_put:    "Cash-Secured Put",
};

type LegStructure = "single" | "spread" | "covered";

const STRATEGY_LEGS: Record<StrategyType, LegStructure> = {
  long_call:           "single",
  long_put:            "single",
  call_debit_spread:   "spread",
  put_debit_spread:    "spread",
  call_credit_spread:  "spread",
  put_credit_spread:   "spread",
  covered_call:        "covered",
  cash_secured_put:    "single",
};

// ── Strategy params ───────────────────────────────────────────────────────────

interface StrategyParams {
  strike: number;
  premium: number;
  longStrike: number;
  longPremium: number;
  shortStrike: number;
  shortPremium: number;
  stockEntry: number;
}

const DEFAULT_PARAMS: StrategyParams = {
  strike: 0, premium: 0,
  longStrike: 0, longPremium: 0,
  shortStrike: 0, shortPremium: 0,
  stockEntry: 0,
};

// ── Leg Greeks / IV ───────────────────────────────────────────────────────────
// iv = implied volatility in percent (e.g. 45 for 45%)
// When iv > 0: Black-Scholes pricing is used (more accurate)
// When iv = 0 but delta set: Taylor delta/gamma approximation used
// When both zero: live estimate not available

interface LegGreeks {
  delta: number;
  gamma: number;
  iv:    number; // IV as % (e.g. 45), 0 = use Taylor fallback
}

const DEFAULT_GREEKS: LegGreeks = { delta: 0, gamma: 0, iv: 0 };

type EstimateMode = "bs" | "taylor" | "none";

function getEstimateMode(
  legs: LegStructure,
  gA: LegGreeks,
  _gB: LegGreeks,
): EstimateMode {
  if (gA.iv > 0) return "bs";
  if (legs === "single" || legs === "covered") return gA.delta !== 0 ? "taylor" : "none";
  return gA.delta !== 0 || _gB.delta !== 0 ? "taylor" : "none";
}

// ── Payoff at expiration (per-share, exact) ────────────────────────────────────

function payoffAtExpiry(strategy: StrategyType, p: StrategyParams, S: number): number {
  switch (strategy) {
    case "long_call":      return Math.max(S - p.strike, 0) - p.premium;
    case "long_put":       return Math.max(p.strike - S, 0) - p.premium;
    case "call_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return Math.max(S - p.longStrike, 0) - Math.max(S - p.shortStrike, 0) - nd;
    }
    case "put_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return Math.max(p.longStrike - S, 0) - Math.max(p.shortStrike - S, 0) - nd;
    }
    case "call_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      return nc - (Math.max(S - p.shortStrike, 0) - Math.max(S - p.longStrike, 0));
    }
    case "put_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      return nc - (Math.max(p.shortStrike - S, 0) - Math.max(p.longStrike - S, 0));
    }
    case "covered_call":
      return (S - p.stockEntry) + (p.premium - Math.max(S - p.strike, 0));
    case "cash_secured_put":
      return p.premium - Math.max(p.strike - S, 0);
    default: return 0;
  }
}

// ── Black-Scholes live estimate (per-share) ───────────────────────────────────
// Uses option's IV and DTE to price each leg at stock price S.
// gA = Greeks for longStrike/longPremium or single-leg; gB = shortStrike/shortPremium

function liveEstimateBS(
  strategy: StrategyType,
  p: StrategyParams,
  gA: LegGreeks,
  gB: LegGreeks,
  S: number,
  T: number, // years to expiration
): number {
  const r = RISK_FREE_RATE;

  function bsVal(K: number, ivPct: number, type: "call" | "put"): number {
    const sigma = ivPct / 100;
    if (sigma <= 0 || T <= 0) {
      return type === "call" ? Math.max(S - K, 0) : Math.max(K - S, 0);
    }
    return bsPrice({ S, K, T, r, sigma, type }).price;
  }

  const ivB = gB.iv > 0 ? gB.iv : gA.iv; // fall back to gA.iv for second leg if not set

  switch (strategy) {
    case "long_call":
      return bsVal(p.strike, gA.iv, "call") - p.premium;
    case "long_put":
      return bsVal(p.strike, gA.iv, "put") - p.premium;
    case "cash_secured_put":
      return p.premium - bsVal(p.strike, gA.iv, "put");
    case "call_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return bsVal(p.longStrike, gA.iv, "call") - bsVal(p.shortStrike, ivB, "call") - nd;
    }
    case "put_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return bsVal(p.longStrike, gA.iv, "put") - bsVal(p.shortStrike, ivB, "put") - nd;
    }
    case "call_credit_spread": {
      // shortStrike/shortPremium = SOLD (gA); longStrike/longPremium = HEDGE (gB)
      const nc = p.shortPremium - p.longPremium;
      return nc - (bsVal(p.shortStrike, gA.iv, "call") - bsVal(p.longStrike, ivB, "call"));
    }
    case "put_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      return nc - (bsVal(p.shortStrike, gA.iv, "put") - bsVal(p.longStrike, ivB, "put"));
    }
    case "covered_call": {
      const stockPnL = S - p.stockEntry;
      const callPnL  = p.premium - bsVal(p.strike, gA.iv, "call");
      return stockPnL + callPnL;
    }
    default: return 0;
  }
}

// ── Taylor delta/gamma approximation (fallback, per-share) ───────────────────

function liveEstimateTaylor(
  strategy: StrategyType,
  p: StrategyParams,
  gA: LegGreeks,
  gB: LegGreeks,
  S: number,
  S0: number,
): number {
  const dS = S - S0;
  const optVal = (prem: number, d: number, g: number) =>
    Math.max(prem + d * dS + 0.5 * g * dS * dS, 0);

  switch (strategy) {
    case "long_call":
    case "long_put":
      return optVal(p.premium, gA.delta, gA.gamma) - p.premium;
    case "cash_secured_put":
      return p.premium - optVal(p.premium, gA.delta, gA.gamma);
    case "call_debit_spread":
    case "put_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return optVal(p.longPremium, gA.delta, gA.gamma) - optVal(p.shortPremium, gB.delta, gB.gamma) - nd;
    }
    case "call_credit_spread":
    case "put_credit_spread": {
      // shortPremium = SOLD (gA); longPremium = HEDGE (gB) — mirrors StrategyParams convention
      const nc = p.shortPremium - p.longPremium;
      return nc - (optVal(p.shortPremium, gA.delta, gA.gamma) - optVal(p.longPremium, gB.delta, gB.gamma));
    }
    case "covered_call": {
      const stockPnL = S - p.stockEntry;
      const callPnL  = p.premium - optVal(p.premium, gA.delta, gA.gamma);
      return stockPnL + callPnL;
    }
    default: return 0;
  }
}

// ── Analytic summary (expiry mode) ────────────────────────────────────────────

interface Summary {
  maxProfit: number | null;
  maxLoss:   number;
  breakevens: number[];
  cost:      number;
}

function computeSummary(strategy: StrategyType, p: StrategyParams): Summary {
  switch (strategy) {
    case "long_call":
      return { maxProfit: null, maxLoss: -p.premium, breakevens: [p.strike + p.premium], cost: p.premium };
    case "long_put":
      return { maxProfit: p.strike - p.premium, maxLoss: -p.premium, breakevens: [p.strike - p.premium], cost: p.premium };
    case "call_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return { maxProfit: p.shortStrike - p.longStrike - nd, maxLoss: -nd, breakevens: [p.longStrike + nd], cost: nd };
    }
    case "put_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      return { maxProfit: p.longStrike - p.shortStrike - nd, maxLoss: -nd, breakevens: [p.longStrike - nd], cost: nd };
    }
    case "call_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      return { maxProfit: nc, maxLoss: -(p.longStrike - p.shortStrike - nc), breakevens: [p.shortStrike + nc], cost: -nc };
    }
    case "put_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      return { maxProfit: nc, maxLoss: -(p.shortStrike - p.longStrike - nc), breakevens: [p.shortStrike - nc], cost: -nc };
    }
    case "covered_call": {
      const ec = p.stockEntry - p.premium;
      return { maxProfit: p.strike - p.stockEntry + p.premium, maxLoss: -ec, breakevens: [ec], cost: ec };
    }
    case "cash_secured_put":
      return { maxProfit: p.premium, maxLoss: -(p.strike - p.premium), breakevens: [p.strike - p.premium], cost: -p.premium };
    default:
      return { maxProfit: 0, maxLoss: 0, breakevens: [], cost: 0 };
  }
}

// ── Chart range ───────────────────────────────────────────────────────────────

function chartRange(strategy: StrategyType, p: StrategyParams, cp: number) {
  const legs = STRATEGY_LEGS[strategy];
  const strikes: number[] = [];
  if (legs === "single" || legs === "covered") {
    if (p.strike > 0) strikes.push(p.strike);
  } else {
    if (p.longStrike > 0)  strikes.push(p.longStrike);
    if (p.shortStrike > 0) strikes.push(p.shortStrike);
  }
  if (p.stockEntry > 0) strikes.push(p.stockEntry);
  if (cp > 0)           strikes.push(cp);
  const arr = strikes.length ? strikes : [cp || 100];
  const lo  = Math.min(...arr);
  const hi  = Math.max(...arr);
  const ctr = (lo + hi) / 2;
  const span = Math.max((hi - lo) * 1.5, ctr * 0.4, 20);
  return { lo: Math.max(ctr - span, 0.01), hi: ctr + span };
}

function formatExpiry(exp: string): string {
  if (!exp) return "";
  try {
    return new Date(`${exp}T12:00:00Z`).toLocaleDateString("en-US", {
      month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
    });
  } catch { return exp; }
}

// ── Tooltip ───────────────────────────────────────────────────────────────────

function PayoffTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  const val: number = payload[0]?.value ?? 0;
  return (
    <div className="bg-bg-surface border border-bg-border rounded-xl p-3 text-xs shadow-2xl space-y-1">
      <p className="text-text-muted font-mono">
        Stock @ <span className="text-text-primary font-semibold">${Number(label).toFixed(2)}</span>
      </p>
      <p className={clsx("font-semibold font-mono", val >= 0 ? "text-call" : "text-put")}>
        P/L: {val >= 0 ? "+" : ""}${val.toFixed(2)} / share
      </p>
      <p className="text-text-muted">
        Contract: {val >= 0 ? "+" : ""}${(val * 100).toFixed(2)}
      </p>
    </div>
  );
}

// ── Small inputs ──────────────────────────────────────────────────────────────

function NumInput({ label, value, onChange, placeholder, step = "0.01" }: {
  label: string; value: number; onChange: (v: number) => void;
  placeholder?: string; step?: string;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-text-muted font-medium">{label}</label>
      <input
        type="number" step={step} className="input w-full font-mono"
        value={value || ""}
        onChange={e => onChange(parseFloat(e.target.value) || 0)}
        placeholder={placeholder}
      />
    </div>
  );
}

function GreekInput({ label, value, onChange, placeholder }: {
  label: string; value: number; onChange: (v: number) => void; placeholder?: string;
}) {
  return (
    <div className="space-y-0.5">
      <label className="text-2xs text-text-muted">{label}</label>
      <input
        type="number" step="0.001"
        className="input w-full font-mono text-xs py-1"
        value={value || ""}
        onChange={e => onChange(parseFloat(e.target.value) || 0)}
        placeholder={placeholder ?? "0.000"}
      />
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  currentPrice: number;
  targetPrice:  number;
  expiration?:  string;
  dte?:         number;
}

export default function StrategyVisualizer({ currentPrice, targetPrice, expiration, dte }: Props) {
  const [open, setOpen]           = useState(false);
  const [strategy, setStrategy]   = useState<StrategyType>("long_call");
  const [params, setParams]       = useState<StrategyParams>({ ...DEFAULT_PARAMS });
  const [expectedPrice, setExpectedPrice] = useState(targetPrice || 0);
  const [viewMode, setViewMode]   = useState<"expiry" | "live">("expiry");
  const [greeksA, setGreeksA]     = useState<LegGreeks>({ ...DEFAULT_GREEKS });
  const [greeksB, setGreeksB]     = useState<LegGreeks>({ ...DEFAULT_GREEKS });
  const [dteInput, setDteInput]   = useState(dte ?? 30);

  // Sync DTE when prop changes (user runs calculator)
  useEffect(() => {
    if (dte != null) setDteInput(dte);
  }, [dte]);

  const legs     = STRATEGY_LEGS[strategy];
  const isLive   = viewMode === "live";
  const estMode  = getEstimateMode(legs, greeksA, greeksB);
  const canEst   = estMode !== "none";
  const T        = dteToYears(dteInput);

  function set(k: keyof StrategyParams, v: number) {
    setParams(prev => ({ ...prev, [k]: v }));
  }

  function changeStrategy(s: StrategyType) {
    setStrategy(s);
    setParams({ ...DEFAULT_PARAMS });
    setGreeksA({ ...DEFAULT_GREEKS });
    setGreeksB({ ...DEFAULT_GREEKS });
  }

  // Chart data
  const chartData = useMemo(() => {
    const { lo, hi } = chartRange(strategy, params, currentPrice || 100);
    const step = (hi - lo) / 80;
    return Array.from({ length: 81 }, (_, i) => {
      const S = lo + i * step;
      let pnl: number;
      if (isLive && canEst) {
        pnl = estMode === "bs"
          ? liveEstimateBS(strategy, params, greeksA, greeksB, S, T)
          : liveEstimateTaylor(strategy, params, greeksA, greeksB, S, currentPrice);
      } else {
        pnl = payoffAtExpiry(strategy, params, S);
      }
      return { price: parseFloat(S.toFixed(2)), pnl: parseFloat(pnl.toFixed(4)) };
    });
  }, [strategy, params, currentPrice, isLive, canEst, estMode, greeksA, greeksB, T]);

  // Numerical breakevens from sign changes in chart
  const chartBreakevens = useMemo(() => {
    const bks: number[] = [];
    for (let i = 1; i < chartData.length; i++) {
      const a = chartData[i - 1], b = chartData[i];
      if (a.pnl * b.pnl < 0) {
        const t = -a.pnl / (b.pnl - a.pnl);
        bks.push(parseFloat((a.price + t * (b.price - a.price)).toFixed(2)));
      }
    }
    return bks;
  }, [chartData]);

  const summary   = useMemo(() => computeSummary(strategy, params), [strategy, params]);
  const dispBEs   = isLive ? chartBreakevens : summary.breakevens;
  const chartVals = chartData.map(d => d.pnl);
  const chartMax  = Math.max(...chartVals);
  const chartMin  = Math.min(...chartVals);

  const pnlAtExpected = useMemo(() => {
    if (expectedPrice <= 0) return null;
    if (isLive && estMode === "bs")
      return liveEstimateBS(strategy, params, greeksA, greeksB, expectedPrice, T);
    if (isLive && estMode === "taylor")
      return liveEstimateTaylor(strategy, params, greeksA, greeksB, expectedPrice, currentPrice);
    if (isLive) return null; // no Greeks entered
    return payoffAtExpiry(strategy, params, expectedPrice);
  }, [expectedPrice, isLive, estMode, strategy, params, greeksA, greeksB, T, currentPrice]);

  const pnlAtCurrent = currentPrice > 0
    ? payoffAtExpiry(strategy, params, currentPrice) : null;
  const pnlAtTarget  = targetPrice > 0 && targetPrice !== expectedPrice
    ? payoffAtExpiry(strategy, params, targetPrice) : null;

  const costBasis = Math.abs(summary.cost);
  const roiPct = (pnl: number) =>
    costBasis === 0 ? "—" : ((pnl / costBasis) * 100).toFixed(1) + "%";

  const chartColor =
    !isLive || !canEst ? "#6366f1"   // expiry: accent
    : estMode === "bs" ? "#22c55e"   // BS: green
    :                    "#f59e0b";  // Taylor: amber

  const modeBadge =
    !isLive           ? { text: "Expiration payoff",           cls: "text-accent border-accent/40 bg-accent/10" }
    : estMode === "bs"? { text: "Black-Scholes estimate",       cls: "text-call border-call/30 bg-call/10" }
    : estMode === "taylor" ? { text: "Delta/gamma approx",      cls: "text-warn border-warn/30 bg-warn/10" }
    :                   { text: "Enter IV or delta below",      cls: "text-text-muted border-bg-border bg-bg-raised" };

  return (
    <div className="card space-y-4">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex items-center justify-between w-full"
      >
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-text-primary text-left">
              Strategy Visualizer
            </h2>
            {!open && (
              <p className="text-xs text-text-muted text-left mt-0.5">
                Model any strategy · expiration payoff or live BS estimate
              </p>
            )}
          </div>
        </div>
        {open
          ? <ChevronUp className="w-4 h-4 text-text-muted shrink-0" />
          : <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />}
      </button>

      {open && (
        <div className="space-y-5 pt-1">

          {/* ── MODE TOGGLE + EXPIRY ──────────────────────────────────────── */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex rounded-lg border border-bg-border overflow-hidden text-xs font-medium">
              <button type="button" onClick={() => setViewMode("expiry")}
                className={clsx("px-3 py-1.5 transition-colors",
                  !isLive ? "bg-accent text-white" : "text-text-muted hover:text-text-secondary")}>
                Expiration payoff
              </button>
              <button type="button" onClick={() => setViewMode("live")}
                className={clsx("px-3 py-1.5 border-l border-bg-border transition-colors",
                  isLive
                    ? estMode === "bs" ? "bg-call text-white"
                    : estMode === "taylor" ? "bg-warn text-bg-surface"
                    : "bg-bg-hover text-text-primary"
                    : "text-text-muted hover:text-text-secondary")}>
                Live estimate
              </button>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <span className={clsx("text-2xs font-medium px-2 py-0.5 rounded border", modeBadge.cls)}>
                {modeBadge.text}
              </span>
              {expiration && (
                <span className="text-2xs font-mono text-text-muted bg-bg-raised border border-bg-border rounded px-2 py-0.5">
                  {isLive
                    ? `${dteInput}d to ${formatExpiry(expiration)}`
                    : `Payoff at ${formatExpiry(expiration)}`}
                </span>
              )}
            </div>
          </div>

          {/* Mode context */}
          {isLive ? (
            <div className="rounded-md bg-bg-raised border border-bg-border px-3 py-2 text-2xs text-text-muted leading-relaxed">
              {estMode === "bs"
                ? <><span className="text-call font-medium">Black-Scholes mode</span> — accurate pre-expiry estimate using your option&apos;s IV and DTE.</>
                : estMode === "taylor"
                ? <><span className="text-warn font-medium">Taylor approximation</span> — delta + ½γ(ΔS)². Enter IV% for Black-Scholes (more accurate).</>
                : <><span className="text-text-secondary font-medium">Live estimate disabled</span> — enter IV% (or delta) below to enable.</>}
            </div>
          ) : (
            <div className="rounded-md bg-bg-raised border border-bg-border px-3 py-2 text-2xs text-text-muted">
              <span className="text-accent font-medium">Exact payoff at expiration</span> — intrinsic value only.
              Switch to{" "}
              <button type="button" onClick={() => setViewMode("live")} className="text-accent underline underline-offset-2">
                Live estimate
              </button>{" "}
              for pre-expiry BS pricing.
            </div>
          )}

          {/* ── TOP CONTROLS ──────────────────────────────────────────────── */}
          <div className="space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-xs text-text-muted font-medium">Strategy</label>
                <select className="input w-full" value={strategy}
                  onChange={e => changeStrategy(e.target.value as StrategyType)}>
                  {(Object.keys(STRATEGY_LABELS) as StrategyType[]).map(s => (
                    <option key={s} value={s}>{STRATEGY_LABELS[s]}</option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-accent">Expected stock price</label>
                <input
                  type="number" step="0.01"
                  className="input w-full font-mono ring-1 ring-accent/40 focus:ring-accent"
                  value={expectedPrice || ""}
                  onChange={e => setExpectedPrice(parseFloat(e.target.value) || 0)}
                  placeholder="Where do I expect price to go?"
                />
              </div>
            </div>

            {/* Leg inputs */}
            {legs === "single" && (
              <div className="grid grid-cols-2 gap-3">
                <NumInput
                  label={strategy === "cash_secured_put" ? "Strike (put)" : "Strike"}
                  value={params.strike} onChange={v => set("strike", v)}
                  placeholder="e.g. 500" step="0.5"
                />
                <NumInput
                  label={strategy === "cash_secured_put" ? "Premium Received ($)" : "Premium Paid ($)"}
                  value={params.premium} onChange={v => set("premium", v)}
                  placeholder="e.g. 3.50"
                />
              </div>
            )}

            {legs === "spread" && (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <NumInput
                    label={strategy === "call_debit_spread"  ? "Long Call Strike"  :
                           strategy === "put_debit_spread"   ? "Long Put Strike"   :
                           strategy === "call_credit_spread" ? "Short Call Strike" : "Short Put Strike"}
                    value={params.longStrike} onChange={v => set("longStrike", v)}
                    placeholder="Lower strike" step="0.5"
                  />
                  <NumInput label="Premium" value={params.longPremium}
                    onChange={v => set("longPremium", v)} placeholder="e.g. 4.00" />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <NumInput
                    label={strategy === "call_debit_spread"  ? "Short Call Strike"  :
                           strategy === "put_debit_spread"   ? "Short Put Strike"   :
                           strategy === "call_credit_spread" ? "Long Call Strike"   : "Long Put Strike"}
                    value={params.shortStrike} onChange={v => set("shortStrike", v)}
                    placeholder="Higher strike" step="0.5"
                  />
                  <NumInput label="Premium" value={params.shortPremium}
                    onChange={v => set("shortPremium", v)} placeholder="e.g. 2.00" />
                </div>
                {(strategy === "call_debit_spread" || strategy === "put_debit_spread") && (
                  <p className="text-2xs text-text-muted">
                    Net debit: <span className="font-mono text-text-secondary">
                      ${Math.max(params.longPremium - params.shortPremium, 0).toFixed(2)}
                    </span>
                  </p>
                )}
                {(strategy === "call_credit_spread" || strategy === "put_credit_spread") && (
                  <p className="text-2xs text-text-muted">
                    Net credit: <span className="font-mono text-call">
                      ${Math.max(params.shortPremium - params.longPremium, 0).toFixed(2)}
                    </span>
                  </p>
                )}
              </div>
            )}

            {legs === "covered" && (
              <div className="grid grid-cols-3 gap-3">
                <NumInput label="Stock Entry ($)" value={params.stockEntry}
                  onChange={v => set("stockEntry", v)} placeholder="e.g. 185.00" step="0.01" />
                <NumInput label="Call Strike" value={params.strike}
                  onChange={v => set("strike", v)} placeholder="e.g. 190" step="0.5" />
                <NumInput label="Call Premium ($)" value={params.premium}
                  onChange={v => set("premium", v)} placeholder="e.g. 2.50" />
              </div>
            )}
          </div>

          {/* ── LIVE MODE: IV / GREEKS INPUTS ─────────────────────────────── */}
          {isLive && (
            <div className="rounded-lg border border-bg-border bg-bg-raised p-3 space-y-3">
              <p className="text-2xs font-semibold text-text-muted uppercase tracking-wide">
                Pricing inputs{" "}
                <span className="font-normal normal-case">
                  (IV → Black-Scholes · delta/gamma → Taylor)
                </span>
              </p>

              {/* DTE */}
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-0.5">
                  <label className="text-2xs text-text-muted">DTE (days)</label>
                  <input
                    type="number" step="1" min="0"
                    className="input w-full font-mono text-xs py-1"
                    value={dteInput || ""}
                    onChange={e => setDteInput(parseInt(e.target.value) || 0)}
                    placeholder={dte != null ? String(dte) : "30"}
                  />
                </div>
                <div className="space-y-0.5 self-end">
                  <p className="text-2xs text-text-muted">
                    T = <span className="font-mono">{T.toFixed(4)}</span> yrs
                    {dte != null && dteInput !== dte && (
                      <button
                        type="button"
                        onClick={() => setDteInput(dte)}
                        className="ml-2 text-accent underline underline-offset-1"
                      >
                        reset ({dte}d)
                      </button>
                    )}
                  </p>
                </div>
              </div>

              {/* Single-leg */}
              {legs === "single" && (
                <>
                  <p className="text-2xs text-text-muted font-medium">
                    {strategy === "cash_secured_put" ? "Short put" : "Option"} inputs
                  </p>
                  <div className="grid grid-cols-3 gap-2">
                    <GreekInput label="IV %" value={greeksA.iv}
                      onChange={v => setGreeksA(g => ({ ...g, iv: v }))} placeholder="e.g. 35" />
                    <GreekInput label="Delta" value={greeksA.delta}
                      onChange={v => setGreeksA(g => ({ ...g, delta: v }))} />
                    <GreekInput label="Gamma" value={greeksA.gamma}
                      onChange={v => setGreeksA(g => ({ ...g, gamma: v }))} />
                  </div>
                </>
              )}

              {/* Spread */}
              {legs === "spread" && (
                <div className="space-y-2">
                  <p className="text-2xs text-text-muted font-medium">
                    {strategy === "call_debit_spread"  ? "Long Call"        :
                     strategy === "put_debit_spread"   ? "Long Put"         :
                     strategy === "call_credit_spread" ? "Short Call (sold)":
                     "Short Put (sold)"} inputs
                  </p>
                  <div className="grid grid-cols-3 gap-2">
                    <GreekInput label="IV %" value={greeksA.iv}
                      onChange={v => setGreeksA(g => ({ ...g, iv: v }))} placeholder="e.g. 35" />
                    <GreekInput label="Delta" value={greeksA.delta}
                      onChange={v => setGreeksA(g => ({ ...g, delta: v }))} />
                    <GreekInput label="Gamma" value={greeksA.gamma}
                      onChange={v => setGreeksA(g => ({ ...g, gamma: v }))} />
                  </div>
                  <p className="text-2xs text-text-muted font-medium mt-1">
                    {strategy === "call_debit_spread"  ? "Short Call"         :
                     strategy === "put_debit_spread"   ? "Short Put"          :
                     strategy === "call_credit_spread" ? "Long Call (hedge)"  :
                     "Long Put (hedge)"} inputs{" "}
                    <span className="font-normal">(leave IV 0 to use same IV as first leg)</span>
                  </p>
                  <div className="grid grid-cols-3 gap-2">
                    <GreekInput label="IV %" value={greeksB.iv}
                      onChange={v => setGreeksB(g => ({ ...g, iv: v }))} placeholder="0 = same" />
                    <GreekInput label="Delta" value={greeksB.delta}
                      onChange={v => setGreeksB(g => ({ ...g, delta: v }))} />
                    <GreekInput label="Gamma" value={greeksB.gamma}
                      onChange={v => setGreeksB(g => ({ ...g, gamma: v }))} />
                  </div>
                </div>
              )}

              {/* Covered */}
              {legs === "covered" && (
                <>
                  <p className="text-2xs text-text-muted">
                    Short call inputs (stock portion is always exact)
                  </p>
                  <div className="grid grid-cols-3 gap-2">
                    <GreekInput label="IV %" value={greeksA.iv}
                      onChange={v => setGreeksA(g => ({ ...g, iv: v }))} placeholder="e.g. 28" />
                    <GreekInput label="Delta" value={greeksA.delta}
                      onChange={v => setGreeksA(g => ({ ...g, delta: v }))} />
                    <GreekInput label="Gamma" value={greeksA.gamma}
                      onChange={v => setGreeksA(g => ({ ...g, gamma: v }))} />
                  </div>
                </>
              )}

              {!canEst && (
                <p className="text-2xs text-warn">
                  Enter IV% for Black-Scholes, or delta for Taylor approximation
                </p>
              )}
            </div>
          )}

          {/* ── OUTCOME SUMMARY ───────────────────────────────────────────── */}
          <div className="rounded-xl border border-accent/20 bg-bg-raised p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-semibold text-text-primary">
                {isLive ? "Live estimate" : "Outcome at expiration"}
                {pnlAtExpected !== null && expectedPrice > 0 && (
                  <span className="font-normal text-text-muted">
                    {` — if price reaches $${expectedPrice.toFixed(2)}`}
                  </span>
                )}
              </p>
              {expiration && (
                <span className="text-2xs text-text-muted font-mono">
                  {isLive ? `~${dteInput}d DTE` : formatExpiry(expiration)}
                </span>
              )}
            </div>

            {pnlAtExpected !== null && expectedPrice > 0 ? (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">P/L / share</p>
                  <p className={clsx("text-sm font-bold font-mono", pnlAtExpected >= 0 ? "text-call" : "text-put")}>
                    {pnlAtExpected >= 0 ? "+" : ""}${pnlAtExpected.toFixed(2)}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">P/L / contract</p>
                  <p className={clsx("text-sm font-bold font-mono", pnlAtExpected >= 0 ? "text-call" : "text-put")}>
                    {pnlAtExpected >= 0 ? "+" : ""}${(pnlAtExpected * 100).toFixed(2)}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">ROI</p>
                  <p className={clsx("text-sm font-bold font-mono", pnlAtExpected >= 0 ? "text-call" : "text-put")}>
                    {roiPct(pnlAtExpected)}
                  </p>
                </div>

                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">Breakeven(s)</p>
                  {dispBEs.length === 0
                    ? <p className="text-sm font-semibold text-text-muted">—</p>
                    : dispBEs.map((be, i) => (
                        <p key={i} className="text-sm font-semibold font-mono text-warn">${be.toFixed(2)}</p>
                      ))}
                </div>

                {isLive ? (
                  <>
                    <div className="space-y-0.5">
                      <p className="text-2xs text-text-muted">Chart max</p>
                      <p className={clsx("text-sm font-semibold font-mono", chartMax > 0 ? "text-call" : "text-text-muted")}>
                        {isFinite(chartMax) ? `${chartMax >= 0 ? "+" : ""}$${chartMax.toFixed(2)}` : "—"}
                      </p>
                    </div>
                    <div className="space-y-0.5">
                      <p className="text-2xs text-text-muted">Chart min</p>
                      <p className={clsx("text-sm font-semibold font-mono", chartMin < 0 ? "text-put" : "text-text-muted")}>
                        {isFinite(chartMin) ? `$${chartMin.toFixed(2)}` : "—"}
                      </p>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="space-y-0.5">
                      <p className="text-2xs text-text-muted">Max Profit</p>
                      <p className="text-sm font-semibold font-mono text-call">
                        {summary.maxProfit === null ? "Unlimited"
                          : summary.maxProfit <= 0 ? "—"
                          : `$${summary.maxProfit.toFixed(2)}`}
                      </p>
                      {summary.maxProfit !== null && summary.maxProfit > 0 && (
                        <p className="text-2xs text-text-muted">
                          ${(summary.maxProfit * 100).toFixed(0)} / contract
                        </p>
                      )}
                    </div>
                    <div className="space-y-0.5">
                      <p className="text-2xs text-text-muted">Max Loss</p>
                      <p className="text-sm font-semibold font-mono text-put">
                        {summary.maxLoss >= 0 ? "—" : `$${summary.maxLoss.toFixed(2)}`}
                      </p>
                      {summary.maxLoss < 0 && (
                        <p className="text-2xs text-text-muted">
                          ${(summary.maxLoss * 100).toFixed(0)} / contract
                        </p>
                      )}
                    </div>
                  </>
                )}
              </div>
            ) : (
              <p className="text-xs text-text-muted">
                {isLive && !canEst && expectedPrice > 0
                  ? "Enter IV% above to enable Black-Scholes pricing."
                  : "Enter an expected stock price above to see your outcome."}
              </p>
            )}

            {isLive && canEst && (
              <p className="text-2xs text-text-muted border-t border-bg-border pt-2">
                {estMode === "bs"
                  ? `Black-Scholes · IV from inputs · T = ${T.toFixed(4)} yrs · r = ${(RISK_FREE_RATE * 100).toFixed(1)}%`
                  : "Taylor approx · delta + ½γ(ΔS)² · no time decay · add IV% for BS"}
              </p>
            )}
          </div>

          {/* ── PAYOFF CHART ──────────────────────────────────────────────── */}
          <div>
            <p className="text-xs text-text-muted mb-3">
              {isLive && estMode === "bs"
                ? `Black-Scholes · per share · ${dteInput}d DTE`
                : isLive && estMode === "taylor"
                ? "Delta/gamma approx · per share"
                : isLive
                ? "Enter IV or delta above to enable live estimate"
                : "Exact payoff at expiration · per share · 100× for contract"}
            </p>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={chartData} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#242830" vertical={false} />
                <XAxis dataKey="price" tick={{ fontSize: 10, fill: "#555b6a" }}
                  axisLine={false} tickLine={false} tickFormatter={v => `$${v}`}
                  interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 10, fill: "#555b6a" }} axisLine={false}
                  tickLine={false} tickFormatter={v => `$${v}`} />
                <Tooltip content={<PayoffTooltip />} />
                <ReferenceLine y={0} stroke="#555b6a" strokeDasharray="4 2" />
                {currentPrice > 0 && (
                  <ReferenceLine x={currentPrice} stroke="#6366f1" strokeDasharray="4 2"
                    label={{ value: "Now", position: "top", fontSize: 9, fill: "#6366f1" }} />
                )}
                {expectedPrice > 0 && (
                  <ReferenceLine x={expectedPrice} stroke="#22c55e" strokeDasharray="4 2"
                    label={{ value: "Expected", position: "top", fontSize: 9, fill: "#22c55e" }} />
                )}
                {dispBEs.map((be, i) => (
                  <ReferenceLine key={i} x={be} stroke="#f59e0b" strokeDasharray="4 2"
                    label={{ value: "BE", position: "insideTopRight", fontSize: 9, fill: "#f59e0b" }} />
                ))}
                <Line type="monotone" dataKey="pnl" stroke={chartColor}
                  strokeWidth={2} dot={false}
                  activeDot={{ r: 4, fill: chartColor }} />
              </LineChart>
            </ResponsiveContainer>
            <div className="flex flex-wrap gap-4 text-2xs text-text-muted mt-2">
              <span className="flex items-center gap-1.5">
                <span className="w-3 h-0.5 rounded" style={{ background: chartColor }} />
                {isLive && estMode === "bs" ? "BS P&L" : isLive && estMode === "taylor" ? "Est. P&L" : "P&L at expiry"}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 border-t-2 border-dashed border-[#6366f1] opacity-60" />Current
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 border-t-2 border-dashed border-call" />Expected
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 border-t-2 border-dashed border-warn" />Breakeven
              </span>
            </div>
          </div>

          {/* ── REFERENCE PRICES ──────────────────────────────────────────── */}
          {(pnlAtCurrent !== null || pnlAtTarget !== null) && (
            <div className="border-t border-bg-border pt-4 space-y-2">
              <p className="text-2xs font-semibold text-text-muted uppercase tracking-wide">
                Reference prices (expiration payoff)
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {pnlAtCurrent !== null && currentPrice > 0 && (
                  <div className="bg-bg-raised rounded-lg p-3 space-y-1">
                    <p className="text-2xs text-text-muted">At Current (${currentPrice.toFixed(2)})</p>
                    <p className={clsx("text-sm font-semibold font-mono", pnlAtCurrent >= 0 ? "text-call" : "text-put")}>
                      {pnlAtCurrent >= 0 ? "+" : ""}${pnlAtCurrent.toFixed(2)} / share
                      &nbsp;·&nbsp;
                      {pnlAtCurrent >= 0 ? "+" : ""}${(pnlAtCurrent * 100).toFixed(2)} / contract
                    </p>
                    <p className="text-2xs text-text-muted">ROI: {roiPct(pnlAtCurrent)}</p>
                  </div>
                )}
                {pnlAtTarget !== null && targetPrice > 0 && (
                  <div className="bg-bg-raised rounded-lg p-3 space-y-1">
                    <p className="text-2xs text-text-muted">At Expected (${targetPrice.toFixed(2)})</p>
                    <p className={clsx("text-sm font-semibold font-mono", pnlAtTarget >= 0 ? "text-call" : "text-put")}>
                      {pnlAtTarget >= 0 ? "+" : ""}${pnlAtTarget.toFixed(2)} / share
                      &nbsp;·&nbsp;
                      {pnlAtTarget >= 0 ? "+" : ""}${(pnlAtTarget * 100).toFixed(2)} / contract
                    </p>
                    <p className="text-2xs text-text-muted">ROI: {roiPct(pnlAtTarget)}</p>
                  </div>
                )}
              </div>
              <p className="text-2xs text-text-muted">Exact intrinsic payoff at expiration.</p>
            </div>
          )}

        </div>
      )}
    </div>
  );
}
