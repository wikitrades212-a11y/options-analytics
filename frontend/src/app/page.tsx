"use client";

import { useState, useCallback, useEffect } from "react";
import { useChain, useUnusual } from "@/hooks/useOptions";
import TickerInput from "@/components/ui/TickerInput";
import SummaryCards from "@/components/options/SummaryCards";
import UnusualTable from "@/components/options/UnusualTable";
import OIBarChart from "@/components/charts/OIBarChart";
import CallPutChart from "@/components/charts/CallPutChart";
import { fmtTimestamp } from "@/lib/formatters";
import { ErrorState } from "@/components/ui/Badge";
import { Copy, Check, X, Heart } from "lucide-react";

// ── Donation addresses ────────────────────────────────────────────────────────
const DONATION_ITEMS = [
  { label: "BTC", address: "bc1qx6yvrptsytxxepp7n8elwkxcycs7w9pvhg7ewg" },
  { label: "ETH", address: "0xa81ded7DF812795326404619b84376abF96048f4" },
  { label: "SOL", address: "Bfy6v9PkAamUZjXXxUfepgfsxy3xyKMMdiPyR7XGAjQv" },
  { label: "Cash App", address: "$epay" },
];

function DonationBanner({ onDismiss }: { onDismiss: () => void }) {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (address: string, label: string) => {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {}
  };

  return (
    <div className="card border border-accent/20 bg-accent/5 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Heart className="w-4 h-4 text-accent" />
          <span className="text-sm font-semibold text-text-primary">Support the hard work</span>
        </div>
        <button
          onClick={onDismiss}
          className="text-text-muted hover:text-text-primary transition-colors p-0.5"
          aria-label="Dismiss"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {DONATION_ITEMS.map(({ label, address }) => (
          <div key={label} className="flex items-center gap-2 bg-bg-raised rounded-lg px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="text-2xs text-text-muted font-medium mb-0.5">{label}</div>
              <div className="text-xs font-mono text-text-secondary truncate">{address}</div>
            </div>
            <button
              onClick={() => copy(address, label)}
              className="shrink-0 p-1.5 rounded-md bg-bg-hover hover:bg-accent/20 text-text-muted hover:text-accent transition-colors"
              aria-label={`Copy ${label} address`}
            >
              {copied === label
                ? <Check className="w-3.5 h-3.5 text-success" />
                : <Copy className="w-3.5 h-3.5" />
              }
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main dashboard ────────────────────────────────────────────────────────────
export default function Dashboard() {
  const [ticker, setTicker] = useState("SPY");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshMs, setRefreshMs] = useState(60_000);
  const [showDonation, setShowDonation] = useState(false);

  // Load settings from localStorage
  useEffect(() => {
    try {
      const stored = localStorage.getItem("options_settings");
      if (stored) {
        const s = JSON.parse(stored);
        if (s.defaultTicker) setTicker(s.defaultTicker);
        if (s.autoRefreshInterval) setRefreshMs(s.autoRefreshInterval * 1000);
      }
    } catch {}

    // Show donation unless dismissed
    const dismissed = localStorage.getItem("donation_dismissed");
    if (!dismissed) setShowDonation(true);
  }, []);

  const dismissDonation = () => {
    setShowDonation(false);
    localStorage.setItem("donation_dismissed", "1");
  };

  const { data: chain, error: chainErr, isLoading: chainLoading, mutate: refreshChain } =
    useChain(ticker, { refreshInterval: autoRefresh ? refreshMs : 0 });

  const { data: unusual, error: unusualErr, isLoading: unusualLoading, mutate: refreshUnusual } =
    useUnusual(ticker, { refreshInterval: autoRefresh ? refreshMs : 0 });

  const refresh = useCallback(() => {
    refreshChain();
    refreshUnusual();
  }, [refreshChain, refreshUnusual]);

  const loading = chainLoading || unusualLoading;
  const error = chainErr || unusualErr;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Donation banner */}
      {showDonation && <DonationBanner onDismiss={dismissDonation} />}

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Dashboard</h1>
          <p className="text-sm text-text-muted">Options flow overview</p>
        </div>
        <TickerInput
          value={ticker}
          onChange={setTicker}
          onRefresh={refresh}
          loading={loading}
          autoRefresh={autoRefresh}
          onAutoRefreshChange={setAutoRefresh}
          refreshInterval={refreshMs / 1000}
          lastRefresh={
            chain?.timestamp ? fmtTimestamp(chain.timestamp) : undefined
          }
        />
      </div>

      {error && <ErrorState message={`Failed to load data: ${(error as Error).message}`} />}

      {/* Summary cards */}
      <SummaryCards
        data={unusual}
        loading={unusualLoading}
        underlying={chain?.underlying_price}
        callPutRatio={chain?.call_put_ratio}
        totalCallOI={chain?.total_call_oi}
        totalPutOI={chain?.total_put_oi}
      />

      {/* Charts row */}
      {!error && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <OIBarChart
            contracts={chain?.contracts ?? []}
            loading={chainLoading}
            underlying={chain?.underlying_price}
          />
          <CallPutChart
            callVolume={chain?.total_call_volume ?? 0}
            putVolume={chain?.total_put_volume ?? 0}
            callOI={chain?.total_call_oi ?? 0}
            putOI={chain?.total_put_oi ?? 0}
            loading={chainLoading}
          />
        </div>
      )}

      {/* Top unusual preview */}
      {!error && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-text-primary">Top Unusual Activity</h2>
            <a href="/unusual" className="text-xs text-accent hover:underline">View all →</a>
          </div>
          <UnusualTable
            contracts={unusual?.combined ?? []}
            loading={unusualLoading}
            limit={10}
          />
        </div>
      )}
    </div>
  );
}
