"use client";

import { useState, useRef } from "react";
import { Search, TrendingUp, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { useStockFundamentals } from "@/hooks/useStockFundamentals";
import VerdictBanner from "@/components/fundamentals/VerdictBanner";
import ScoreCard from "@/components/fundamentals/ScoreCard";
import DCFCard from "@/components/fundamentals/DCFCard";
import FinancialsGrid from "@/components/fundamentals/FinancialsGrid";
import ValuationGrid from "@/components/fundamentals/ValuationGrid";
import FCFChart from "@/components/fundamentals/FCFChart";
import ReasonsAndWarnings from "@/components/fundamentals/ReasonsAndWarnings";
import { fmtNotional } from "@/lib/formatters";

const QUICK_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B", "JPM", "V"];
const DCF_METHODS = [
  { id: "conservative",       label: "Conservative" },
  { id: "historical_average", label: "Historical Avg" },
  { id: "capped_growth",      label: "Capped (25%)" },
];

function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse bg-bg-raised rounded-lg", className)} />;
}

function LoadingState() {
  return (
    <div className="space-y-4 animate-fade-in">
      <Skeleton className="h-28 w-full" />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Skeleton className="h-52" />
        <Skeleton className="h-52" />
        <Skeleton className="h-52" />
      </div>
      <Skeleton className="h-48 w-full" />
    </div>
  );
}

export default function FundamentalsPage() {
  const [inputVal, setInputVal] = useState("");
  const [activeTicker, setActiveTicker] = useState<string | null>(null);
  const [dcfMethod, setDcfMethod] = useState("conservative");
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data, error, isLoading, mutate } = useStockFundamentals(activeTicker, dcfMethod);

  const submit = (raw: string) => {
    const t = raw.trim().toUpperCase();
    if (!t) return;
    setInputVal(t);
    setActiveTicker(t);
    setDropdownOpen(false);
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") submit(inputVal);
    if (e.key === "Escape") setDropdownOpen(false);
  };

  return (
    <div className="space-y-6 animate-fade-in">

      {/* ── Page header ── */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-text-primary flex items-center gap-2">
            <TrendingUp className="w-5 h-5 text-accent" />
            Stock Fundamentals
          </h1>
          <p className="text-sm text-text-muted">DCF valuation · scoring · financial quality</p>
        </div>

        {/* Controls */}
        <div className="flex flex-wrap items-center gap-3">
          {/* DCF method selector */}
          <div className="flex items-center gap-1 bg-bg-raised border border-bg-border rounded-lg p-0.5">
            {DCF_METHODS.map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  setDcfMethod(m.id);
                  if (activeTicker) mutate();
                }}
                className={clsx(
                  "text-xs px-2.5 py-1 rounded-md transition-colors",
                  dcfMethod === m.id
                    ? "bg-accent text-white font-medium"
                    : "text-text-secondary hover:text-text-primary"
                )}
              >
                {m.label}
              </button>
            ))}
          </div>

          {/* Ticker input */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-muted pointer-events-none" />
            <input
              ref={inputRef}
              className="input pl-9 pr-3 w-44 uppercase font-mono tracking-widest"
              placeholder="AAPL"
              value={inputVal}
              maxLength={8}
              onChange={(e) => {
                setInputVal(e.target.value.toUpperCase());
                setDropdownOpen(true);
              }}
              onKeyDown={handleKey}
              onFocus={() => setDropdownOpen(true)}
              onBlur={() => setTimeout(() => setDropdownOpen(false), 150)}
            />
            {dropdownOpen && (
              <div className="absolute top-full mt-1 left-0 z-50 bg-bg-surface border border-bg-border rounded-xl shadow-2xl p-2 grid grid-cols-5 gap-1 w-56">
                {QUICK_TICKERS.map((t) => (
                  <button
                    key={t}
                    onMouseDown={() => submit(t)}
                    className={clsx(
                      "text-xs font-mono px-2 py-1 rounded-lg transition-colors",
                      t === activeTicker
                        ? "bg-accent text-white"
                        : "text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                    )}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}
          </div>

          <button
            onClick={() => submit(inputVal)}
            disabled={isLoading}
            className="btn-primary flex items-center gap-1.5"
          >
            <RefreshCw className={clsx("w-3.5 h-3.5", isLoading && "animate-spin")} />
            Analyze
          </button>
        </div>
      </div>

      {/* ── Empty state ── */}
      {!activeTicker && !isLoading && (
        <div className="card flex flex-col items-center justify-center py-16 text-center gap-4">
          <TrendingUp className="w-10 h-10 text-text-muted" />
          <div>
            <p className="text-text-secondary font-medium">Enter a ticker to analyze</p>
            <p className="text-sm text-text-muted mt-1">DCF valuation, scoring, and financial health</p>
          </div>
          <div className="flex flex-wrap gap-2 justify-center">
            {QUICK_TICKERS.slice(0, 5).map((t) => (
              <button
                key={t}
                onClick={() => submit(t)}
                className="text-xs font-mono px-3 py-1.5 rounded-lg bg-bg-raised border border-bg-border text-text-secondary hover:text-text-primary hover:border-accent/40 transition-colors"
              >
                {t}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Loading ── */}
      {isLoading && <LoadingState />}

      {/* ── Error ── */}
      {error && !isLoading && (
        <div className="card border-put/30 bg-put/5 p-5">
          <p className="text-sm font-medium text-put">Failed to analyze {activeTicker}</p>
          <p className="text-xs text-text-muted mt-1">{(error as Error).message}</p>
          <button onClick={() => mutate()} className="btn-ghost mt-3 text-xs">Retry</button>
        </div>
      )}

      {/* ── Results ── */}
      {data && !isLoading && (
        <div className="space-y-4">

          {/* Company header */}
          <div className="flex flex-wrap items-baseline gap-3">
            <h2 className="text-2xl font-bold text-text-primary">{data.ticker}</h2>
            <span className="text-text-secondary">{data.company_name}</span>
            {data.sector && (
              <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
                {data.sector}
              </span>
            )}
            <div className="ml-auto flex items-center gap-4 text-sm">
              <div className="flex items-baseline gap-1.5">
                <span className="text-text-muted">Price </span>
                <span className="font-bold tabular-nums text-text-primary">${data.current_price.toFixed(2)}</span>
                {data.price_source && (
                  <span className={clsx(
                    "text-2xs px-1.5 py-0.5 rounded font-semibold uppercase tracking-wide",
                    data.price_source === "tradier"   ? "bg-green-500/10 text-green-600" :
                    data.price_source === "alpaca"    ? "bg-blue-500/10 text-blue-600" :
                    data.price_source === "robinhood" ? "bg-emerald-500/10 text-emerald-600" :
                    "bg-bg-raised text-text-muted"
                  )}>
                    {data.price_source}
                  </span>
                )}
              </div>
              {data.market_cap && (
                <div>
                  <span className="text-text-muted">MCap </span>
                  <span className="font-bold text-text-primary">{fmtNotional(data.market_cap)}</span>
                </div>
              )}
            </div>
          </div>

          {/* Verdict banner */}
          <VerdictBanner analysis={data} />

          {/* DCF + Score cards */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <DCFCard dcf={data.dcf} />
            <ScoreCard score={data.score.score} />
          </div>

          {/* Fundamentals grid (Growth, Margins, Balance Sheet) */}
          <FinancialsGrid
            growth={data.growth_metrics}
            margins={data.margin_metrics}
            health={data.financial_health}
          />

          {/* Valuation + FCF chart */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <ValuationGrid valuation={data.valuation_metrics} />
            <div className="lg:col-span-2">
              <FCFChart fcf={data.fcf_profile} />
            </div>
          </div>

          {/* Reasons + Warnings */}
          <ReasonsAndWarnings
            reasons={data.verdict_reasons}
            warnings={data.warnings}
          />

        </div>
      )}
    </div>
  );
}
