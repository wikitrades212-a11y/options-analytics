"use client";

import { useState, useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import clsx from "clsx";
import { ChevronDown, ChevronUp, TrendingUp } from "lucide-react";

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
  // Single leg
  strike: number;
  premium: number;
  // Spread legs
  longStrike: number;
  longPremium: number;
  shortStrike: number;
  shortPremium: number;
  // Covered call stock entry price
  stockEntry: number;
}

const DEFAULT_PARAMS: StrategyParams = {
  strike: 0,
  premium: 0,
  longStrike: 0,
  longPremium: 0,
  shortStrike: 0,
  shortPremium: 0,
  stockEntry: 0,
};

// ── Payoff math (per-share at expiration) ──────────────────────────────────────

function payoffAtExpiry(
  strategy: StrategyType,
  p: StrategyParams,
  S: number
): number {
  switch (strategy) {
    case "long_call":
      return Math.max(S - p.strike, 0) - p.premium;

    case "long_put":
      return Math.max(p.strike - S, 0) - p.premium;

    case "call_debit_spread": {
      // Long lower-strike call, short higher-strike call
      const netDebit = p.longPremium - p.shortPremium;
      return (
        Math.max(S - p.longStrike, 0) -
        Math.max(S - p.shortStrike, 0) -
        netDebit
      );
    }

    case "put_debit_spread": {
      // Long higher-strike put, short lower-strike put
      const netDebit = p.longPremium - p.shortPremium;
      return (
        Math.max(p.longStrike - S, 0) -
        Math.max(p.shortStrike - S, 0) -
        netDebit
      );
    }

    case "call_credit_spread": {
      // Short lower-strike call, long higher-strike call
      const netCredit = p.shortPremium - p.longPremium;
      return (
        netCredit -
        (Math.max(S - p.shortStrike, 0) - Math.max(S - p.longStrike, 0))
      );
    }

    case "put_credit_spread": {
      // Short higher-strike put, long lower-strike put
      const netCredit = p.shortPremium - p.longPremium;
      return (
        netCredit -
        (Math.max(p.shortStrike - S, 0) - Math.max(p.longStrike - S, 0))
      );
    }

    case "covered_call": {
      // Own 100 shares at stockEntry + sold call at strike for premium
      const stockPnL = S - p.stockEntry;
      const callPnL = p.premium - Math.max(S - p.strike, 0);
      return stockPnL + callPnL;
    }

    case "cash_secured_put":
      // Sell put at strike for premium
      return p.premium - Math.max(p.strike - S, 0);

    default:
      return 0;
  }
}

// ── Analytic summary ──────────────────────────────────────────────────────────

interface Summary {
  maxProfit: number | null; // null = unlimited
  maxLoss: number;
  breakevens: number[];
  cost: number; // net debit (positive) or credit (negative) per share
}

function computeSummary(strategy: StrategyType, p: StrategyParams): Summary {
  switch (strategy) {
    case "long_call":
      return {
        maxProfit: null, // unlimited
        maxLoss: -p.premium,
        breakevens: [p.strike + p.premium],
        cost: p.premium,
      };

    case "long_put":
      return {
        maxProfit: p.strike - p.premium,
        maxLoss: -p.premium,
        breakevens: [p.strike - p.premium],
        cost: p.premium,
      };

    case "call_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      const width = p.shortStrike - p.longStrike;
      return {
        maxProfit: width - nd,
        maxLoss: -nd,
        breakevens: [p.longStrike + nd],
        cost: nd,
      };
    }

    case "put_debit_spread": {
      const nd = p.longPremium - p.shortPremium;
      const width = p.longStrike - p.shortStrike;
      return {
        maxProfit: width - nd,
        maxLoss: -nd,
        breakevens: [p.longStrike - nd],
        cost: nd,
      };
    }

    case "call_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      const width = p.longStrike - p.shortStrike;
      return {
        maxProfit: nc,
        maxLoss: -(width - nc),
        breakevens: [p.shortStrike + nc],
        cost: -nc, // received credit
      };
    }

    case "put_credit_spread": {
      const nc = p.shortPremium - p.longPremium;
      const width = p.shortStrike - p.longStrike;
      return {
        maxProfit: nc,
        maxLoss: -(width - nc),
        breakevens: [p.shortStrike - nc],
        cost: -nc,
      };
    }

    case "covered_call": {
      const effectiveCost = p.stockEntry - p.premium;
      return {
        maxProfit: p.strike - p.stockEntry + p.premium,
        maxLoss: -effectiveCost, // stock → 0
        breakevens: [effectiveCost],
        cost: effectiveCost,
      };
    }

    case "cash_secured_put":
      return {
        maxProfit: p.premium,
        maxLoss: -(p.strike - p.premium),
        breakevens: [p.strike - p.premium],
        cost: -p.premium,
      };

    default:
      return { maxProfit: 0, maxLoss: 0, breakevens: [], cost: 0 };
  }
}

// ── Chart range helper ────────────────────────────────────────────────────────

function chartRange(
  strategy: StrategyType,
  p: StrategyParams,
  currentPrice: number
): { lo: number; hi: number } {
  const legs = STRATEGY_LEGS[strategy];
  let center = currentPrice;
  let strikes: number[] = [];

  if (legs === "single" || legs === "covered") {
    if (p.strike > 0) strikes = [p.strike];
  } else {
    if (p.longStrike > 0) strikes.push(p.longStrike);
    if (p.shortStrike > 0) strikes.push(p.shortStrike);
  }
  if (p.stockEntry > 0) strikes.push(p.stockEntry);
  if (currentPrice > 0) strikes.push(currentPrice);

  const min = Math.min(...(strikes.length ? strikes : [center]));
  const max = Math.max(...(strikes.length ? strikes : [center]));
  center = (min + max) / 2;

  const span = Math.max((max - min) * 1.5, center * 0.4, 20);
  return { lo: Math.max(center - span, 0.01), hi: center + span };
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
        Per contract: {val >= 0 ? "+" : ""}${(val * 100).toFixed(2)}
      </p>
    </div>
  );
}

// ── Numeric input helper ──────────────────────────────────────────────────────

function NumInput({
  label, value, onChange, placeholder, step = "0.01",
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  placeholder?: string;
  step?: string;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-text-muted font-medium">{label}</label>
      <input
        type="number"
        step={step}
        className="input w-full font-mono"
        value={value || ""}
        onChange={e => onChange(parseFloat(e.target.value) || 0)}
        placeholder={placeholder}
      />
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  currentPrice: number;
  targetPrice: number;
}

export default function StrategyVisualizer({ currentPrice, targetPrice }: Props) {
  const [open, setOpen] = useState(false);
  const [strategy, setStrategy] = useState<StrategyType>("long_call");
  const [params, setParams] = useState<StrategyParams>({ ...DEFAULT_PARAMS });
  const [expectedPrice, setExpectedPrice] = useState(targetPrice || 0);

  const legs = STRATEGY_LEGS[strategy];

  function set(k: keyof StrategyParams, v: number) {
    setParams(prev => ({ ...prev, [k]: v }));
  }

  // When strategy changes, reset params
  function changeStrategy(s: StrategyType) {
    setStrategy(s);
    setParams({ ...DEFAULT_PARAMS });
  }

  // Build chart data
  const chartData = useMemo(() => {
    const { lo, hi } = chartRange(strategy, params, currentPrice || 100);
    const points = 80;
    const step = (hi - lo) / points;
    return Array.from({ length: points + 1 }, (_, i) => {
      const S = lo + i * step;
      return {
        price: parseFloat(S.toFixed(2)),
        pnl: parseFloat(payoffAtExpiry(strategy, params, S).toFixed(4)),
      };
    });
  }, [strategy, params, currentPrice]);

  const summary = useMemo(() => computeSummary(strategy, params), [strategy, params]);

  const pnlAtExpected = expectedPrice > 0
    ? payoffAtExpiry(strategy, params, expectedPrice)
    : null;

  const pnlAtCurrent = currentPrice > 0
    ? payoffAtExpiry(strategy, params, currentPrice)
    : null;

  const pnlAtTarget = targetPrice > 0 && targetPrice !== expectedPrice
    ? payoffAtExpiry(strategy, params, targetPrice)
    : null;

  // Cost basis for ROI calculation
  const costBasis = Math.abs(summary.cost);

  function roiPct(pnl: number): string {
    if (costBasis === 0) return "—";
    return ((pnl / costBasis) * 100).toFixed(1) + "%";
  }

  return (
    <div className="card space-y-4">
      {/* Header / toggle */}
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex items-center justify-between w-full"
      >
        <div className="flex items-center gap-2">
          <TrendingUp className="w-4 h-4 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-text-primary text-left">
              If price goes here — what happens to my strategy?
            </h2>
            {!open && (
              <p className="text-xs text-text-muted text-left mt-0.5">
                Set your expected stock price and see the payoff.
              </p>
            )}
          </div>
        </div>
        {open ? (
          <ChevronUp className="w-4 h-4 text-text-muted shrink-0" />
        ) : (
          <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />
        )}
      </button>

      {open && (
        <div className="space-y-5 pt-1">

          {/* ── TOP CONTROL ROW ───────────────────────────────────────────── */}
          <div className="space-y-3">
            {/* Strategy + Expected Price — first row, both prominent */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-xs text-text-muted font-medium">Strategy</label>
                <select
                  className="input w-full"
                  value={strategy}
                  onChange={e => changeStrategy(e.target.value as StrategyType)}
                >
                  {(Object.keys(STRATEGY_LABELS) as StrategyType[]).map(s => (
                    <option key={s} value={s}>{STRATEGY_LABELS[s]}</option>
                  ))}
                </select>
              </div>

              {/* Expected price — highlighted as primary input */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-accent">
                  Expected stock price
                </label>
                <input
                  type="number"
                  step="0.01"
                  className="input w-full font-mono ring-1 ring-accent/40 focus:ring-accent"
                  value={expectedPrice || ""}
                  onChange={e => setExpectedPrice(parseFloat(e.target.value) || 0)}
                  placeholder="Where do I expect price to go?"
                />
              </div>
            </div>

            {/* Leg inputs — strike(s) + premium(s) */}
            {legs === "single" && (
              <div className="grid grid-cols-2 gap-3">
                <NumInput
                  label={strategy === "cash_secured_put" ? "Strike (put)" : "Strike"}
                  value={params.strike}
                  onChange={v => set("strike", v)}
                  placeholder="e.g. 500"
                  step="0.5"
                />
                <NumInput
                  label={strategy === "cash_secured_put" ? "Premium Received ($)" : "Premium Paid ($)"}
                  value={params.premium}
                  onChange={v => set("premium", v)}
                  placeholder="e.g. 3.50"
                />
              </div>
            )}

            {legs === "spread" && (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <NumInput
                    label={
                      strategy === "call_debit_spread" ? "Long Call Strike" :
                      strategy === "put_debit_spread"  ? "Long Put Strike"  :
                      strategy === "call_credit_spread" ? "Short Call Strike" :
                      "Short Put Strike"
                    }
                    value={params.longStrike}
                    onChange={v => set("longStrike", v)}
                    placeholder="Lower strike"
                    step="0.5"
                  />
                  <NumInput
                    label="Premium"
                    value={params.longPremium}
                    onChange={v => set("longPremium", v)}
                    placeholder="e.g. 4.00"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <NumInput
                    label={
                      strategy === "call_debit_spread" ? "Short Call Strike" :
                      strategy === "put_debit_spread"  ? "Short Put Strike"  :
                      strategy === "call_credit_spread" ? "Long Call Strike"  :
                      "Long Put Strike"
                    }
                    value={params.shortStrike}
                    onChange={v => set("shortStrike", v)}
                    placeholder="Higher strike"
                    step="0.5"
                  />
                  <NumInput
                    label="Premium"
                    value={params.shortPremium}
                    onChange={v => set("shortPremium", v)}
                    placeholder="e.g. 2.00"
                  />
                </div>
                {(strategy === "call_debit_spread" || strategy === "put_debit_spread") && (
                  <p className="text-2xs text-text-muted">
                    Net debit: <span className="text-text-secondary font-mono">
                      ${Math.max(params.longPremium - params.shortPremium, 0).toFixed(2)}
                    </span>
                  </p>
                )}
                {(strategy === "call_credit_spread" || strategy === "put_credit_spread") && (
                  <p className="text-2xs text-text-muted">
                    Net credit: <span className="text-call font-mono">
                      ${Math.max(params.shortPremium - params.longPremium, 0).toFixed(2)}
                    </span>
                  </p>
                )}
              </div>
            )}

            {legs === "covered" && (
              <div className="grid grid-cols-3 gap-3">
                <NumInput
                  label="Stock Entry ($)"
                  value={params.stockEntry}
                  onChange={v => set("stockEntry", v)}
                  placeholder="e.g. 185.00"
                  step="0.01"
                />
                <NumInput
                  label="Call Strike"
                  value={params.strike}
                  onChange={v => set("strike", v)}
                  placeholder="e.g. 190"
                  step="0.5"
                />
                <NumInput
                  label="Call Premium ($)"
                  value={params.premium}
                  onChange={v => set("premium", v)}
                  placeholder="e.g. 2.50"
                />
              </div>
            )}
          </div>

          {/* ── OUTCOME SUMMARY AT EXPECTED PRICE ────────────────────────── */}
          <div className="rounded-xl border border-accent/20 bg-bg-raised p-4 space-y-3">
            <p className="text-xs font-semibold text-text-primary">
              {expectedPrice > 0
                ? `Outcome if price reaches $${expectedPrice.toFixed(2)}`
                : "Outcome at expected price"}
            </p>
            {pnlAtExpected !== null && expectedPrice > 0 ? (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">P/L per share</p>
                  <p className={clsx("text-sm font-bold font-mono", pnlAtExpected >= 0 ? "text-call" : "text-put")}>
                    {pnlAtExpected >= 0 ? "+" : ""}${pnlAtExpected.toFixed(2)}
                  </p>
                </div>
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">P/L per contract</p>
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
                  <p className="text-2xs text-text-muted">Breakeven</p>
                  {summary.breakevens.length === 0 ? (
                    <p className="text-sm font-semibold text-text-muted">—</p>
                  ) : (
                    summary.breakevens.map((be, i) => (
                      <p key={i} className="text-sm font-semibold font-mono text-warn">${be.toFixed(2)}</p>
                    ))
                  )}
                </div>
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">Max Profit</p>
                  <p className="text-sm font-semibold font-mono text-call">
                    {summary.maxProfit === null
                      ? "Unlimited"
                      : summary.maxProfit <= 0
                      ? "—"
                      : `$${summary.maxProfit.toFixed(2)}`}
                  </p>
                  {summary.maxProfit !== null && summary.maxProfit > 0 && (
                    <p className="text-2xs text-text-muted">${(summary.maxProfit * 100).toFixed(0)} / contract</p>
                  )}
                </div>
                <div className="space-y-0.5">
                  <p className="text-2xs text-text-muted">Max Loss</p>
                  <p className="text-sm font-semibold font-mono text-put">
                    {summary.maxLoss >= 0 ? "—" : `$${summary.maxLoss.toFixed(2)}`}
                  </p>
                  {summary.maxLoss < 0 && (
                    <p className="text-2xs text-text-muted">${(summary.maxLoss * 100).toFixed(0)} / contract</p>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-xs text-text-muted">
                Enter an expected stock price above to see your outcome instantly.
              </p>
            )}
          </div>

          {/* ── PAYOFF CHART ──────────────────────────────────────────────── */}
          <div>
            <p className="text-xs text-text-muted mb-3">
              Payoff at expiration · per share · 100× multiplier for contract value
            </p>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={chartData} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#242830" vertical={false} />
                <XAxis
                  dataKey="price"
                  tick={{ fontSize: 10, fill: "#555b6a" }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={v => `$${v}`}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "#555b6a" }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={v => `$${v}`}
                />
                <Tooltip content={<PayoffTooltip />} />
                <ReferenceLine y={0} stroke="#555b6a" strokeDasharray="4 2" />
                {currentPrice > 0 && (
                  <ReferenceLine
                    x={currentPrice}
                    stroke="#6366f1"
                    strokeDasharray="4 2"
                    label={{ value: "Now", position: "top", fontSize: 9, fill: "#6366f1" }}
                  />
                )}
                {expectedPrice > 0 && (
                  <ReferenceLine
                    x={expectedPrice}
                    stroke="#22c55e"
                    strokeDasharray="4 2"
                    label={{ value: "Expected", position: "top", fontSize: 9, fill: "#22c55e" }}
                  />
                )}
                {summary.breakevens.map((be, i) => (
                  <ReferenceLine
                    key={i}
                    x={be}
                    stroke="#f59e0b"
                    strokeDasharray="4 2"
                    label={{ value: "BE", position: "insideTopRight", fontSize: 9, fill: "#f59e0b" }}
                  />
                ))}
                <Line
                  type="monotone"
                  dataKey="pnl"
                  stroke="#6366f1"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: "#6366f1" }}
                />
              </LineChart>
            </ResponsiveContainer>
            {/* Chart legend */}
            <div className="flex flex-wrap gap-4 text-2xs text-text-muted mt-2">
              <span className="flex items-center gap-1.5">
                <span className="w-3 h-0.5 bg-accent rounded" />P&L
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 h-0.5 bg-[#6366f1] rounded opacity-60" style={{ borderTop: "2px dashed #6366f1", background: "none" }} />
                Current
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 border-t-2 border-dashed border-call" />Expected
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-3 border-t-2 border-dashed border-warn" />Breakeven
              </span>
            </div>
          </div>

          {/* ── SECONDARY DETAILS ─────────────────────────────────────────── */}
          {(pnlAtCurrent !== null || pnlAtTarget !== null) && (
            <div className="border-t border-bg-border pt-4 space-y-2">
              <p className="text-2xs font-semibold text-text-muted uppercase tracking-wide">Reference prices</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {pnlAtCurrent !== null && currentPrice > 0 && (
                  <div className="bg-bg-raised rounded-lg p-3 space-y-1">
                    <p className="text-2xs text-text-muted">At Current Price (${currentPrice.toFixed(2)})</p>
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
                    <p className="text-2xs text-text-muted">At Calculator Target (${targetPrice.toFixed(2)})</p>
                    <p className={clsx("text-sm font-semibold font-mono", pnlAtTarget >= 0 ? "text-call" : "text-put")}>
                      {pnlAtTarget >= 0 ? "+" : ""}${pnlAtTarget.toFixed(2)} / share
                      &nbsp;·&nbsp;
                      {pnlAtTarget >= 0 ? "+" : ""}${(pnlAtTarget * 100).toFixed(2)} / contract
                    </p>
                    <p className="text-2xs text-text-muted">ROI: {roiPct(pnlAtTarget)}</p>
                  </div>
                )}
              </div>
              <p className="text-2xs text-text-muted">
                Shows exact expiration payoff. Pre-expiration estimates require Greeks/IV and are not modeled here.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
