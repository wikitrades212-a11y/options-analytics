"use client";

import { useState, useMemo, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from "recharts";
import clsx from "clsx";
import { ChevronDown, ChevronUp, Activity } from "lucide-react";
import { bsPrice, dteToYears, RISK_FREE_RATE } from "@/lib/blackScholes";

// ── Strategy types ────────────────────────────────────────────────────────────

type StrategyType = "long_call" | "long_put";

// ── Strategy params ───────────────────────────────────────────────────────────

interface StrategyParams {
  strike:  number;
  premium: number;
}

const DEFAULT_PARAMS: StrategyParams = { strike: 0, premium: 0 };

// ── Leg Greeks / IV ───────────────────────────────────────────────────────────

interface LegGreeks {
  delta: number;
  gamma: number;
  iv:    number; // IV as % (e.g. 45), 0 = use Taylor fallback
}

const DEFAULT_GREEKS: LegGreeks = { delta: 0, gamma: 0, iv: 0 };

type EstimateMode = "bs" | "taylor" | "none";

function getEstimateMode(g: LegGreeks): EstimateMode {
  if (g.iv > 0) return "bs";
  if (g.delta !== 0) return "taylor";
  return "none";
}

// ── Payoff at expiration (per-share, exact) ────────────────────────────────────

function payoffAtExpiry(strategy: StrategyType, p: StrategyParams, S: number): number {
  if (strategy === "long_call") return Math.max(S - p.strike, 0) - p.premium;
  if (strategy === "long_put")  return Math.max(p.strike - S, 0) - p.premium;
  return 0;
}

// ── Black-Scholes live estimate (per-share) ───────────────────────────────────

function liveEstimateBS(
  strategy: StrategyType,
  p: StrategyParams,
  g: LegGreeks,
  S: number,
  T: number,
): number {
  const sigma = g.iv / 100;
  const r = RISK_FREE_RATE;
  function bsVal(type: "call" | "put"): number {
    if (sigma <= 0 || T <= 0) return type === "call" ? Math.max(S - p.strike, 0) : Math.max(p.strike - S, 0);
    return bsPrice({ S, K: p.strike, T, r, sigma, type }).price;
  }
  if (strategy === "long_call") return bsVal("call") - p.premium;
  if (strategy === "long_put")  return bsVal("put")  - p.premium;
  return 0;
}

// ── Taylor delta/gamma approximation (fallback, per-share) ───────────────────

function liveEstimateTaylor(
  strategy: StrategyType,
  p: StrategyParams,
  g: LegGreeks,
  S: number,
  S0: number,
): number {
  const dS = S - S0;
  const optVal = Math.max(p.premium + g.delta * dS + 0.5 * g.gamma * dS * dS, 0);
  if (strategy === "long_call") return optVal - p.premium;
  if (strategy === "long_put")  return optVal - p.premium;
  return 0;
}

// ── Analytic summary (expiry mode) ────────────────────────────────────────────

interface Summary {
  maxProfit: number | null;
  maxLoss:   number;
  breakevens: number[];
  cost:      number;
}

function computeSummary(strategy: StrategyType, p: StrategyParams): Summary {
  if (strategy === "long_call") {
    return { maxProfit: null, maxLoss: -p.premium, breakevens: [p.strike + p.premium], cost: p.premium };
  }
  return { maxProfit: p.strike - p.premium, maxLoss: -p.premium, breakevens: [p.strike - p.premium], cost: p.premium };
}

// ── Chart range ───────────────────────────────────────────────────────────────

function chartRange(p: StrategyParams, cp: number) {
  const refs = [cp || 100];
  if (p.strike > 0) refs.push(p.strike);
  const lo  = Math.min(...refs);
  const hi  = Math.max(...refs);
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
  const [greeks, setGreeks]       = useState<LegGreeks>({ ...DEFAULT_GREEKS });
  const [dteInput, setDteInput]   = useState(dte ?? 30);

  // Sync DTE when prop changes
  useEffect(() => {
    if (dte != null) setDteInput(dte);
  }, [dte]);

  const isLive  = viewMode === "live";
  const estMode = getEstimateMode(greeks);
  const canEst  = estMode !== "none";
  const T       = dteToYears(dteInput);

  function changeStrategy(s: StrategyType) {
    setStrategy(s);
    setParams({ ...DEFAULT_PARAMS });
    setGreeks({ ...DEFAULT_GREEKS });
  }

  // Chart data
  const chartData = useMemo(() => {
    const { lo, hi } = chartRange(params, currentPrice || 100);
    const step = (hi - lo) / 80;
    return Array.from({ length: 81 }, (_, i) => {
      const S = lo + i * step;
      let pnl: number;
      if (isLive && canEst) {
        pnl = estMode === "bs"
          ? liveEstimateBS(strategy, params, greeks, S, T)
          : liveEstimateTaylor(strategy, params, greeks, S, currentPrice);
      } else {
        pnl = payoffAtExpiry(strategy, params, S);
      }
      return { price: parseFloat(S.toFixed(2)), pnl: parseFloat(pnl.toFixed(4)) };
    });
  }, [strategy, params, currentPrice, isLive, canEst, estMode, greeks, T]);

  // Numerical breakevens from sign changes
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
      return liveEstimateBS(strategy, params, greeks, expectedPrice, T);
    if (isLive && estMode === "taylor")
      return liveEstimateTaylor(strategy, params, greeks, expectedPrice, currentPrice);
    if (isLive) return null;
    return payoffAtExpiry(strategy, params, expectedPrice);
  }, [expectedPrice, isLive, estMode, strategy, params, greeks, T, currentPrice]);

  const pnlAtCurrent = currentPrice > 0
    ? payoffAtExpiry(strategy, params, currentPrice) : null;
  const pnlAtTarget  = targetPrice > 0 && targetPrice !== expectedPrice
    ? payoffAtExpiry(strategy, params, targetPrice) : null;

  const costBasis = Math.abs(summary.cost);
  const roiPct = (pnl: number) =>
    costBasis === 0 ? "—" : ((pnl / costBasis) * 100).toFixed(1) + "%";

  const chartColor =
    !isLive || !canEst ? "#6366f1"
    : estMode === "bs" ? "#22c55e"
    :                    "#f59e0b";

  const modeBadge =
    !isLive           ? { text: "Expiration payoff",      cls: "text-accent border-accent/40 bg-accent/10" }
    : estMode === "bs"? { text: "Black-Scholes estimate",  cls: "text-call border-call/30 bg-call/10" }
    : estMode === "taylor" ? { text: "Delta/gamma approx", cls: "text-warn border-warn/30 bg-warn/10" }
    :                   { text: "Enter IV or delta below", cls: "text-text-muted border-bg-border bg-bg-raised" };

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
                {strategy === "long_call" ? "Call — price goes UP" : "Put — price goes DOWN"} · expiration payoff or live BS estimate
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

          {/* ── STRATEGY SELECTOR ─────────────────────────────────────────── */}
          <div className="space-y-2">
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => changeStrategy("long_call")}
                className={clsx(
                  "flex-1 py-2.5 rounded-xl border text-sm font-semibold transition-colors",
                  strategy === "long_call"
                    ? "bg-call/15 border-call/50 text-call"
                    : "border-bg-border text-text-muted hover:text-text-primary hover:border-call/30"
                )}
              >
                Call
              </button>
              <button
                type="button"
                onClick={() => changeStrategy("long_put")}
                className={clsx(
                  "flex-1 py-2.5 rounded-xl border text-sm font-semibold transition-colors",
                  strategy === "long_put"
                    ? "bg-put/15 border-put/50 text-put"
                    : "border-bg-border text-text-muted hover:text-text-primary hover:border-put/30"
                )}
              >
                Put
              </button>
            </div>
            <p className="text-xs text-text-muted text-center">
              {strategy === "long_call"
                ? <span className="text-call font-medium">Price goes UP ↑</span>
                : <span className="text-put font-medium">Price goes DOWN ↓</span>}
            </p>
          </div>

          {/* ── TOP CONTROLS ──────────────────────────────────────────────── */}
          <div className="space-y-3">
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

            <div className="grid grid-cols-2 gap-3">
              <NumInput
                label="Strike"
                value={params.strike}
                onChange={v => setParams(p => ({ ...p, strike: v }))}
                placeholder="e.g. 500"
                step="0.5"
              />
              <NumInput
                label="Premium Paid ($)"
                value={params.premium}
                onChange={v => setParams(p => ({ ...p, premium: v }))}
                placeholder="e.g. 3.50"
              />
            </div>
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

              <div className="grid grid-cols-3 gap-2">
                <GreekInput label="IV %" value={greeks.iv}
                  onChange={v => setGreeks(g => ({ ...g, iv: v }))} placeholder="e.g. 35" />
                <GreekInput label="Delta" value={greeks.delta}
                  onChange={v => setGreeks(g => ({ ...g, delta: v }))} />
                <GreekInput label="Gamma" value={greeks.gamma}
                  onChange={v => setGreeks(g => ({ ...g, gamma: v }))} />
              </div>

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
            </div>
          )}

        </div>
      )}
    </div>
  );
}
