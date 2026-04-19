"use client";

import { useState, useCallback, useEffect } from "react";
import { Activity, TrendingUp } from "lucide-react";
import clsx from "clsx";
import { useChain, useUnusual } from "@/hooks/useOptions";
import { useStockFundamentals } from "@/hooks/useStockFundamentals";
import TickerInput from "@/components/ui/TickerInput";
import SummaryCards from "@/components/options/SummaryCards";
import UnusualTable from "@/components/options/UnusualTable";
import OIBarChart from "@/components/charts/OIBarChart";
import CallPutChart from "@/components/charts/CallPutChart";
import VerdictBanner from "@/components/fundamentals/VerdictBanner";
import ScoreCard from "@/components/fundamentals/ScoreCard";
import DCFCard from "@/components/fundamentals/DCFCard";
import FinancialsGrid from "@/components/fundamentals/FinancialsGrid";
import ValuationGrid from "@/components/fundamentals/ValuationGrid";
import FCFChart from "@/components/fundamentals/FCFChart";
import ReasonsAndWarnings from "@/components/fundamentals/ReasonsAndWarnings";
import { fmtTimestamp, fmtNotional } from "@/lib/formatters";
import { ErrorState } from "@/components/ui/Badge";

type Tab = "flow" | "fundamentals";

const TABS = [
  { id: "flow"         as Tab, label: "Options Flow",  icon: Activity    },
  { id: "fundamentals" as Tab, label: "Fundamentals",  icon: TrendingUp  },
];

// ── Fundamentals skeleton ─────────────────────────────────────────────────────
function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse bg-bg-raised rounded-lg", className)} />;
}
function FundamentalsLoading() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-28 w-full" />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Skeleton className="h-40" /><Skeleton className="h-40" />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Skeleton className="h-52" /><Skeleton className="h-52" /><Skeleton className="h-52" />
      </div>
      <Skeleton className="h-44 w-full" />
    </div>
  );
}

// ── Main dashboard ────────────────────────────────────────────────────────────
export default function Dashboard() {
  const [tab, setTab] = useState<Tab>("flow");
  const [ticker, setTicker] = useState("SPY");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshMs, setRefreshMs] = useState(60_000);
  const [dcfMethod, setDcfMethod] = useState("conservative");

  useEffect(() => {
    try {
      const stored = localStorage.getItem("options_settings");
      if (stored) {
        const s = JSON.parse(stored);
        if (s.defaultTicker) setTicker(s.defaultTicker);
        if (s.autoRefreshInterval) setRefreshMs(s.autoRefreshInterval * 1000);
      }
    } catch {}
  }, []);

  // Options flow data
  const { data: chain, error: chainErr, isLoading: chainLoading, mutate: refreshChain } =
    useChain(ticker, { refreshInterval: autoRefresh && tab === "flow" ? refreshMs : 0 });
  const { data: unusual, error: unusualErr, isLoading: unusualLoading, mutate: refreshUnusual } =
    useUnusual(ticker, { refreshInterval: autoRefresh && tab === "flow" ? refreshMs : 0 });

  // Fundamentals — only fetch when that tab is active
  const { data: fundamentals, error: fundErr, isLoading: fundLoading, mutate: refreshFund } =
    useStockFundamentals(tab === "fundamentals" ? ticker : null, dcfMethod);

  const refreshFlow = useCallback(() => { refreshChain(); refreshUnusual(); }, [refreshChain, refreshUnusual]);

  const flowLoading = chainLoading || unusualLoading;
  const flowError = chainErr || unusualErr;

  return (
    <div className="space-y-5 animate-fade-in">

      {/* ── Header row ── */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-1 bg-bg-raised border border-bg-border rounded-lg p-0.5">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={clsx(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all",
                tab === id
                  ? "bg-accent text-white shadow-sm"
                  : "text-text-secondary hover:text-text-primary"
              )}
            >
              <Icon className="w-3.5 h-3.5" />
              {label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3">
          {/* DCF method — only shown on fundamentals tab */}
          {tab === "fundamentals" && (
            <div className="flex items-center gap-1 bg-bg-raised border border-bg-border rounded-lg p-0.5">
              {(["conservative", "historical_average", "capped_growth"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setDcfMethod(m)}
                  className={clsx(
                    "text-xs px-2.5 py-1 rounded-md transition-colors",
                    dcfMethod === m ? "bg-accent text-white font-medium" : "text-text-secondary hover:text-text-primary"
                  )}
                >
                  {m === "conservative" ? "Conservative" : m === "historical_average" ? "Hist. Avg" : "Capped"}
                </button>
              ))}
            </div>
          )}

          <TickerInput
            value={ticker}
            onChange={setTicker}
            onRefresh={tab === "flow" ? refreshFlow : refreshFund}
            loading={tab === "flow" ? flowLoading : fundLoading}
            autoRefresh={tab === "flow" ? autoRefresh : false}
            onAutoRefreshChange={tab === "flow" ? setAutoRefresh : undefined}
            refreshInterval={refreshMs / 1000}
            lastRefresh={
              tab === "flow" && chain?.timestamp ? fmtTimestamp(chain.timestamp) : undefined
            }
          />
        </div>
      </div>

      {/* ── OPTIONS FLOW TAB ── */}
      {tab === "flow" && (
        <div className="space-y-6">
          {flowError && <ErrorState message={`Failed to load data: ${(flowError as Error).message}`} />}

          <SummaryCards
            data={unusual}
            loading={unusualLoading}
            underlying={chain?.underlying_price}
            callPutRatio={chain?.call_put_ratio}
            totalCallOI={chain?.total_call_oi}
            totalPutOI={chain?.total_put_oi}
          />

          {!flowError && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <OIBarChart contracts={chain?.contracts ?? []} loading={chainLoading} underlying={chain?.underlying_price} />
              <CallPutChart
                callVolume={chain?.total_call_volume ?? 0}
                putVolume={chain?.total_put_volume ?? 0}
                callOI={chain?.total_call_oi ?? 0}
                putOI={chain?.total_put_oi ?? 0}
                loading={chainLoading}
              />
            </div>
          )}

          {!flowError && (
            <div>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-text-primary">Top Unusual Activity</h2>
                <a href="/unusual" className="text-xs text-accent hover:underline">View all →</a>
              </div>
              <UnusualTable contracts={unusual?.combined ?? []} loading={unusualLoading} limit={10} />
            </div>
          )}
        </div>
      )}

      {/* ── FUNDAMENTALS TAB ── */}
      {tab === "fundamentals" && (
        <div className="space-y-4">
          {fundLoading && <FundamentalsLoading />}

          {fundErr && !fundLoading && (
            <div className="card border-put/30 bg-put/5 p-5">
              <p className="text-sm font-medium text-put">Failed to analyze {ticker}</p>
              <p className="text-xs text-text-muted mt-1">{(fundErr as Error).message}</p>
              <button onClick={() => refreshFund()} className="btn-ghost mt-3 text-xs">Retry</button>
            </div>
          )}

          {fundamentals && !fundLoading && (
            <>
              {/* Company line */}
              <div className="flex flex-wrap items-baseline gap-3">
                <span className="text-lg font-bold text-text-primary">{fundamentals.ticker}</span>
                <span className="text-text-secondary text-sm">{fundamentals.company_name}</span>
                {fundamentals.sector && (
                  <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
                    {fundamentals.sector}
                  </span>
                )}
                <div className="ml-auto flex items-center gap-4 text-sm">
                  <span className="text-text-muted">Price <span className="font-bold text-text-primary tabular-nums">${fundamentals.current_price.toFixed(2)}</span></span>
                  {fundamentals.market_cap && (
                    <span className="text-text-muted">MCap <span className="font-bold text-text-primary">{fmtNotional(fundamentals.market_cap)}</span></span>
                  )}
                </div>
              </div>

              <VerdictBanner analysis={fundamentals} />

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <DCFCard dcf={fundamentals.dcf} />
                <ScoreCard score={fundamentals.score.score} />
              </div>

              <FinancialsGrid
                growth={fundamentals.growth_metrics}
                margins={fundamentals.margin_metrics}
                health={fundamentals.financial_health}
              />

              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <ValuationGrid valuation={fundamentals.valuation_metrics} />
                <div className="lg:col-span-2">
                  <FCFChart fcf={fundamentals.fcf_profile} />
                </div>
              </div>

              <ReasonsAndWarnings
                reasons={fundamentals.verdict_reasons}
                warnings={fundamentals.warnings}
              />
            </>
          )}

          {!fundamentals && !fundLoading && !fundErr && (
            <div className="card flex flex-col items-center justify-center py-16 text-center gap-3">
              <TrendingUp className="w-10 h-10 text-text-muted" />
              <p className="text-text-secondary font-medium">Enter a ticker above and hit Refresh</p>
              <p className="text-sm text-text-muted">DCF · scoring · financial quality</p>
            </div>
          )}
        </div>
      )}

    </div>
  );
}
