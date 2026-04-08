"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import clsx from "clsx";
import type { CalculatorParams, CalculatorResponse } from "@/lib/types";
import { useExpirations } from "@/hooks/useOptions";
import { useCalculator } from "@/hooks/useCalculator";
import InputPanel from "@/components/calculator/InputPanel";
import RecommendationCards from "@/components/calculator/RecommendationCards";
import ROIChart from "@/components/calculator/ROIChart";
import StrikeTable from "@/components/calculator/StrikeTable";
import AvoidList from "@/components/calculator/AvoidList";
import StrategyVisualizer from "@/components/calculator/StrategyVisualizer";
import { ErrorState } from "@/components/ui/Badge";
import { BookmarkPlus, Trash2, Trash, BookOpen, ChevronUp, ChevronDown } from "lucide-react";

const DEFAULT_PARAMS: CalculatorParams = {
  ticker: "SPY",
  current_price: 0,
  target_price: 0,
  option_type: "auto",
  expiration: "",
};

// ── Journal types ─────────────────────────────────────────────────────────────
interface JournalEntry {
  id: string;
  timestamp: string;
  ticker: string;
  current_price: number;
  target_price: number;
  expiration: string;
  option_type: string;
  strategy_mode: string;
  move_pct?: number;
  expiry_fit_score?: number;
  top_contract?: {
    strike: number;
    tier: string;
    estimated_roi_pct: number;
    badges: string[];
  };
}

const JOURNAL_KEY = "calc_journal";

function loadJournal(): JournalEntry[] {
  if (typeof window === "undefined") return [];
  try { return JSON.parse(localStorage.getItem(JOURNAL_KEY) || "[]"); } catch { return []; }
}

function saveJournalToStorage(entries: JournalEntry[]) {
  localStorage.setItem(JOURNAL_KEY, JSON.stringify(entries));
}

// ── Journal card ──────────────────────────────────────────────────────────────
function JournalCard({ entry, onDelete }: { entry: JournalEntry; onDelete: () => void }) {
  const movePct = entry.move_pct;
  const fitPct = entry.expiry_fit_score != null ? Math.round(entry.expiry_fit_score * 100) : null;
  const date = new Date(entry.timestamp).toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
  return (
    <div className="rounded-xl border border-bg-border bg-bg-raised px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono font-semibold text-text-primary text-sm">{entry.ticker}</span>
          <span className={clsx(
            "text-2xs font-semibold px-1.5 py-0.5 rounded border font-mono",
            entry.option_type === "call"
              ? "text-call bg-call/10 border-call/30"
              : entry.option_type === "put"
              ? "text-put bg-put/10 border-put/30"
              : "text-accent bg-accent/10 border-accent/30"
          )}>{entry.option_type.toUpperCase()}</span>
          {movePct != null && (
            <span className={clsx(
              "text-2xs font-mono font-semibold",
              movePct >= 0 ? "text-call" : "text-put"
            )}>
              {movePct > 0 ? "+" : ""}{movePct.toFixed(2)}%
            </span>
          )}
          {fitPct != null && (
            <span className={clsx(
              "text-2xs px-1.5 py-0.5 rounded border",
              fitPct >= 75 ? "text-call bg-call/10 border-call/30"
              : fitPct >= 50 ? "text-warn bg-warn/10 border-warn/30"
              : "text-put bg-put/10 border-put/30"
            )}>fit {fitPct}%</span>
          )}
        </div>
        <button
          onClick={onDelete}
          className="text-text-muted hover:text-put transition-colors shrink-0 p-1"
          aria-label="Delete entry"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
        <span>
          <span className="text-text-secondary font-mono">${entry.current_price.toFixed(2)}</span>
          {" → "}
          <span className="text-text-secondary font-mono">${entry.target_price.toFixed(2)}</span>
        </span>
        <span>exp <span className="font-mono text-text-secondary">{entry.expiration}</span></span>
        {entry.strategy_mode === "compare" && (
          <span className="text-accent">compare mode</span>
        )}
      </div>

      {entry.top_contract && (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-text-muted">
          <span>Top pick:</span>
          <span className="font-mono text-text-secondary">${entry.top_contract.strike} {entry.top_contract.tier}</span>
          <span className={clsx(
            "font-semibold",
            entry.top_contract.estimated_roi_pct >= 0 ? "text-call" : "text-put"
          )}>
            {entry.top_contract.estimated_roi_pct > 0 ? "+" : ""}{entry.top_contract.estimated_roi_pct.toFixed(0)}% ROI
          </span>
          {entry.top_contract.badges.slice(0, 2).map(b => (
            <span key={b} className="px-1.5 py-0.5 rounded bg-bg-hover text-text-muted">{b}</span>
          ))}
        </div>
      )}

      <div className="text-2xs text-text-muted">{date}</div>
    </div>
  );
}

// ── Journal section ───────────────────────────────────────────────────────────
function JournalSection({
  journal, onDelete, onClear,
}: { journal: JournalEntry[]; onDelete: (id: string) => void; onClear: () => void }) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <button
          onClick={() => setCollapsed(c => !c)}
          className="flex items-center gap-2 text-sm font-semibold text-text-primary hover:text-accent transition-colors"
        >
          <BookOpen className="w-4 h-4" />
          Journal
          <span className="text-xs font-normal text-text-muted">({journal.length})</span>
          {collapsed ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronUp className="w-3.5 h-3.5" />}
        </button>
        {journal.length > 0 && (
          <button
            onClick={onClear}
            className="flex items-center gap-1.5 text-2xs text-text-muted hover:text-put transition-colors px-2 py-1 rounded-md border border-bg-border hover:border-put/40"
          >
            <Trash className="w-3 h-3" />
            Clear all
          </button>
        )}
      </div>

      {!collapsed && (
        journal.length === 0 ? (
          <div className="rounded-xl border border-bg-border border-dashed py-8 text-center text-xs text-text-muted">
            No saved moves yet. Hit <span className="text-text-secondary font-medium">Save to Journal</span> after analyzing.
          </div>
        ) : (
          <div className="space-y-2">
            {journal.map(entry => (
              <JournalCard key={entry.id} entry={entry} onDelete={() => onDelete(entry.id)} />
            ))}
          </div>
        )
      )}
    </div>
  );
}

// ── Expiry-fit badge ──────────────────────────────────────────────────────────
function ExpiryFitBadge({ score, dte }: { score: number; dte: number }) {
  const pct = Math.round(score * 100);
  const { label, cls } = score >= 0.75
    ? { label: "Good fit", cls: "text-call bg-call/10 border-call/30" }
    : score >= 0.50
    ? { label: "Decent fit", cls: "text-warn bg-warn/10 border-warn/30" }
    : { label: "Poor fit", cls: "text-put bg-put/10 border-put/30" };
  return (
    <span className={clsx("inline-flex items-center gap-1 text-2xs font-medium px-1.5 py-0.5 rounded border", cls)}>
      {label} {pct}% · {dte}d
    </span>
  );
}

// ── Summary bar for one result ────────────────────────────────────────────────
function SummaryBar({ data }: { data: CalculatorResponse }) {
  return (
    <div className="card flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-text-muted">
      <span>
        <span className="text-text-primary font-semibold font-mono">{data.ticker}</span>
        <span className="ml-2">@ <span className="font-mono text-text-primary">${data.current_price.toFixed(2)}</span></span>
      </span>
      <span>
        Target{" "}
        <span className={clsx("font-mono font-semibold", data.move_pct >= 0 ? "text-call" : "text-put")}>
          ${data.target_price.toFixed(2)} ({data.move_pct > 0 ? "+" : ""}{data.move_pct.toFixed(2)}%)
        </span>
      </span>
      <span>
        Type <span className="text-text-primary font-semibold uppercase">{data.option_type}</span>
      </span>
      <span>
        Expiry <span className="text-text-primary font-semibold font-mono">{data.expiration}</span>
      </span>
      <ExpiryFitBadge score={data.expiry_fit_score} dte={data.dte} />
      <span className="ml-auto text-2xs">
        {data.all_strikes.length} strikes analyzed
      </span>
    </div>
  );
}

// ── Full result block for one expiry ─────────────────────────────────────────
function ResultBlock({ data, label }: { data: CalculatorResponse; label?: string }) {
  return (
    <div className="space-y-5">
      {label && (
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold text-accent border border-accent/40 bg-accent/10 px-2 py-0.5 rounded-md">
            {label}
          </span>
          <div className="flex-1 h-px bg-bg-border" />
        </div>
      )}
      <SummaryBar data={data} />
      <RecommendationCards data={data} />
      <ROIChart data={data} />
      <StrikeTable strikes={data.all_strikes} currentPrice={data.current_price} targetPrice={data.target_price} />
      {data.avoid_list.length > 0 && <AvoidList strikes={data.avoid_list} />}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function TargetPage() {
  const [params, setParams]           = useState<CalculatorParams>(DEFAULT_PARAMS);
  const [submitted, setSubmitted]     = useState<CalculatorParams | null>(null);
  const [compareMode, setCompareMode] = useState(false);
  const [compareExpiry, setCompareExpiry]     = useState("");
  const [compareSubmitted, setCompareSubmitted] = useState<CalculatorParams | null>(null);
  const [journal, setJournal]         = useState<JournalEntry[]>([]);
  const [justSaved, setJustSaved]     = useState(false);

  // Load journal from localStorage on mount
  useEffect(() => {
    setJournal(loadJournal());
  }, []);

  const { data: expirations } = useExpirations(params.ticker || null);
  const expirationList = expirations?.expirations ?? [];

  // Reset on ticker change
  const prevTickerRef = useRef(params.ticker);
  useEffect(() => {
    if (params.ticker !== prevTickerRef.current) {
      prevTickerRef.current = params.ticker;
      setParams(p => ({ ...p, expiration: "", current_price: 0, target_price: 0 }));
      setSubmitted(null);
      setCompareExpiry("");
      setCompareSubmitted(null);
    }
  }, [params.ticker]);

  // Reset compare when mode is toggled off
  useEffect(() => {
    if (!compareMode) {
      setCompareExpiry("");
      setCompareSubmitted(null);
    }
  }, [compareMode]);

  const { data, error, isLoading }             = useCalculator(submitted);
  const { data: compareData, error: compareError, isLoading: compareLoading } =
    useCalculator(compareSubmitted);

  const loading = isLoading || compareLoading;

  const handleChange = useCallback((partial: Partial<CalculatorParams>) => {
    setParams(p => ({ ...p, ...partial }));
  }, []);

  const handleSubmit = useCallback(() => {
    const snap = { ...params };
    setSubmitted(snap);
    if (compareMode && compareExpiry) {
      setCompareSubmitted({ ...snap, expiration: compareExpiry });
    } else {
      setCompareSubmitted(null);
    }
  }, [params, compareMode, compareExpiry]);

  const saveToJournal = useCallback(() => {
    if (!data) return;
    const top = data.recommended_balanced ?? data.recommended_aggressive ?? data.recommended_safer;
    const entry: JournalEntry = {
      id: Date.now().toString(),
      timestamp: new Date().toISOString(),
      ticker: data.ticker,
      current_price: data.current_price,
      target_price: data.target_price,
      expiration: data.expiration,
      option_type: data.option_type,
      strategy_mode: compareMode ? "compare" : "single",
      move_pct: data.move_pct,
      expiry_fit_score: data.expiry_fit_score,
      top_contract: top ? {
        strike: top.strike,
        tier: top.tier,
        estimated_roi_pct: top.estimated_roi_pct,
        badges: top.badges,
      } : undefined,
    };
    const updated = [entry, ...journal];
    setJournal(updated);
    saveJournalToStorage(updated);
    setJustSaved(true);
    setTimeout(() => setJustSaved(false), 2000);
  }, [data, journal, compareMode]);

  const deleteEntry = useCallback((id: string) => {
    const updated = journal.filter(e => e.id !== id);
    setJournal(updated);
    saveJournalToStorage(updated);
  }, [journal]);

  const clearJournal = useCallback(() => {
    setJournal([]);
    localStorage.removeItem(JOURNAL_KEY);
  }, []);

  const hasResults = data && !error;
  const hasCompare = compareData && !compareError && compareMode;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Page header */}
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Target Move Calculator</h1>
        <p className="text-sm text-text-muted">
          Enter a price target to find the best option strikes for your expected move
        </p>
      </div>

      {/* Input panel */}
      <InputPanel
        params={params}
        expirations={expirationList}
        onChange={handleChange}
        onSubmit={handleSubmit}
        loading={loading}
        compareMode={compareMode}
        onCompareModeChange={setCompareMode}
        compareExpiry={compareExpiry}
        onCompareExpiryChange={setCompareExpiry}
      />

      {/* Errors */}
      {error && <ErrorState message={`Analysis failed: ${(error as Error).message}`} />}
      {compareError && (
        <ErrorState message={`Compare analysis failed: ${(compareError as Error).message}`} />
      )}

      {/* Strategy Visualizer — shown as soon as price is known */}
      {params.current_price > 0 && (
        <StrategyVisualizer
          currentPrice={params.current_price}
          targetPrice={params.target_price}
        />
      )}

      {/* Results */}
      {hasResults && (
        <div className="space-y-8">
          {/* Save to Journal — prominent, above results */}
          <div className="flex items-center gap-3">
            <button
              onClick={saveToJournal}
              className={clsx(
                "flex items-center gap-2 text-sm font-semibold px-4 py-2 rounded-lg border transition-all",
                justSaved
                  ? "bg-success/15 border-success/50 text-success"
                  : "bg-accent/10 border-accent/40 text-accent hover:bg-accent/20 hover:border-accent/60"
              )}
            >
              <BookmarkPlus className="w-4 h-4" />
              {justSaved ? "Saved to Journal!" : "Save to Journal"}
            </button>
            {!justSaved && (
              <span className="text-xs text-text-muted">Log this setup to review later</span>
            )}
          </div>

          {hasCompare ? (
            /* Side-by-side compare layout */
            <div className="space-y-8">
              <ResultBlock data={data} label={`Expiry A · ${data.expiration}`} />
              <ResultBlock data={compareData} label={`Expiry B · ${compareData.expiration}`} />
            </div>
          ) : (
            <ResultBlock data={data} />
          )}
        </div>
      )}

      {/* Empty state */}
      {!hasResults && !isLoading && !error && (
        <div className="card flex flex-col items-center justify-center py-16 text-center space-y-2">
          <p className="text-text-muted text-sm">
            Fill in the form above and click{" "}
            <span className="text-text-primary font-medium">Analyze Strikes</span> to see recommendations.
          </p>
          <p className="text-text-muted text-xs">
            Use <span className="text-text-secondary">Nearest</span> or{" "}
            <span className="text-text-secondary">Weekly</span> quick-select to pick an expiration fast.
          </p>
        </div>
      )}

      {/* Journal */}
      <div className="border-t border-bg-border pt-6">
        <JournalSection
          journal={journal}
          onDelete={deleteEntry}
          onClear={clearJournal}
        />
      </div>
    </div>
  );
}
