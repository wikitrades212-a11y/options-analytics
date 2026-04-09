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
import ExpectationPanel from "@/components/calculator/ExpectationPanel";
import { ErrorState } from "@/components/ui/Badge";
import {
  BookmarkPlus, Trash2, Trash, BookOpen,
  ChevronUp, ChevronDown, RotateCcw,
} from "lucide-react";
import {
  buildJournalEntry, addJournalEntry, deleteJournalEntry,
  clearJournal as clearJournalStorage, loadJournal, formatScenarioDate,
  type JournalScenario,
} from "@/lib/journal";
import { buildExpectationResult } from "@/lib/strategyEngine";

// ── Usage counter (Phase 8: monetization foundation) ──────────────────────────

const USAGE_KEY  = "oa_session_count";
const IS_PREMIUM = false; // flip to enable gated features

function incrementUsage(): number {
  if (typeof window === "undefined") return 0;
  const n = (parseInt(localStorage.getItem(USAGE_KEY) || "0", 10) || 0) + 1;
  localStorage.setItem(USAGE_KEY, String(n));
  return n;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const DEFAULT_PARAMS: CalculatorParams = {
  ticker: "SPY",
  current_price: 0,
  target_price: 0,
  option_type: "call",
  expiration: "",
};

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

// ── Summary bar ───────────────────────────────────────────────────────────────

function SummaryBar({ data }: { data: CalculatorResponse }) {
  return (
    <div className="card flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-text-muted">
      <span>
        <span className="text-text-primary font-semibold font-mono">{data.ticker}</span>
        <span className="ml-2">@ <span className="font-mono text-text-primary">${data.current_price.toFixed(2)}</span></span>
      </span>
      <span>
        Expected{" "}
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
        {(data.all_strikes ?? []).length} strikes analyzed
      </span>
    </div>
  );
}

// ── Full result block ─────────────────────────────────────────────────────────

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
      <StrikeTable strikes={data.all_strikes ?? []} currentPrice={data.current_price} targetPrice={data.target_price} />
      {(data.avoid_list ?? []).length > 0 && <AvoidList strikes={data.avoid_list ?? []} />}
    </div>
  );
}

// ── Journal card ──────────────────────────────────────────────────────────────

function JournalCard({
  entry,
  onDelete,
  onLoad,
}: {
  entry: JournalScenario;
  onDelete: () => void;
  onLoad: () => void;
}) {
  const top = entry.topOutcome;
  const tz  = entry.timezone || "UTC";
  const date = formatScenarioDate(entry.timestamp, tz);

  return (
    <div className="rounded-xl border border-bg-border bg-bg-raised px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono font-semibold text-text-primary text-sm">{entry.ticker}</span>
          <span className={clsx(
            "text-2xs font-semibold px-1.5 py-0.5 rounded border font-mono",
            entry.optionType === "call" ? "text-call bg-call/10 border-call/30"
              : entry.optionType === "put" ? "text-put bg-put/10 border-put/30"
              : "text-accent bg-accent/10 border-accent/30"
          )}>
            {entry.optionType.toUpperCase()}
          </span>
          {entry.movePct != null && (
            <span className={clsx("text-2xs font-mono font-semibold",
              entry.movePct >= 0 ? "text-call" : "text-put")}>
              {entry.movePct > 0 ? "+" : ""}{entry.movePct.toFixed(2)}%
            </span>
          )}
          {entry.expiryFitScore != null && (
            <span className={clsx("text-2xs px-1.5 py-0.5 rounded border",
              entry.expiryFitScore >= 0.75 ? "text-call bg-call/10 border-call/30"
                : entry.expiryFitScore >= 0.50 ? "text-warn bg-warn/10 border-warn/30"
                : "text-put bg-put/10 border-put/30")}>
              fit {Math.round(entry.expiryFitScore * 100)}%
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={onLoad}
            className="flex items-center gap-1 text-2xs text-accent hover:underline p-1 rounded hover:bg-accent/10 transition-colors"
            title="Reload this scenario"
          >
            <RotateCcw className="w-3 h-3" />
            Load
          </button>
          <button
            onClick={onDelete}
            className="text-text-muted hover:text-put transition-colors p-1 rounded"
            aria-label="Delete entry"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
        <span>
          <span className="text-text-secondary font-mono">${entry.currentPrice.toFixed(2)}</span>
          {" → "}
          <span className="text-text-secondary font-mono">${entry.expectedPrice.toFixed(2)}</span>
        </span>
        <span>exp <span className="font-mono text-text-secondary">{entry.expiration}</span></span>
        {entry.dte != null && <span className="font-mono">{entry.dte}d DTE</span>}
      </div>

      {top && (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-text-muted">
          <span>Best pick:</span>
          <span className="font-mono text-text-secondary">${top.strike} {top.tier}</span>
          <span className={clsx("font-semibold",
            top.liveRoi >= 0 ? "text-call" : "text-put")}>
            {top.liveRoi > 0 ? "+" : ""}{top.liveRoi.toFixed(0)}% ROI (live BS)
          </span>
          {top.iv > 0 && (
            <span className="text-text-muted">IV {(top.iv * 100).toFixed(0)}%</span>
          )}
          {top.breakeven > 0 && (
            <span>BE ${top.breakeven.toFixed(2)}</span>
          )}
        </div>
      )}

      <div className="text-2xs text-text-muted">{date}</div>
    </div>
  );
}

// ── Journal section ───────────────────────────────────────────────────────────

function JournalSection({
  journal,
  onDelete,
  onClear,
  onLoad,
}: {
  journal: JournalScenario[];
  onDelete: (id: string) => void;
  onClear: () => void;
  onLoad: (entry: JournalScenario) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <button
          onClick={() => setCollapsed(c => !c)}
          className="flex items-center gap-2 text-sm font-semibold text-text-primary hover:text-accent transition-colors"
        >
          <BookOpen className="w-4 h-4" />
          Saved Scenarios
          <span className="text-xs font-normal text-text-muted">({journal.length})</span>
          {collapsed
            ? <ChevronDown className="w-3.5 h-3.5" />
            : <ChevronUp className="w-3.5 h-3.5" />}
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
            No saved scenarios yet. Hit{" "}
            <span className="text-text-secondary font-medium">Save Scenario</span> after analyzing.
          </div>
        ) : (
          <div className="space-y-2">
            {journal.map(entry => (
              <JournalCard
                key={entry.id}
                entry={entry}
                onDelete={() => onDelete(entry.id)}
                onLoad={() => onLoad(entry)}
              />
            ))}
          </div>
        )
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function TargetPage() {
  const [params, setParams]                 = useState<CalculatorParams>(DEFAULT_PARAMS);
  const [submitted, setSubmitted]           = useState<CalculatorParams | null>(null);
  const [compareMode, setCompareMode]       = useState(false);
  const [compareExpiry, setCompareExpiry]   = useState("");
  const [compareSubmitted, setCompareSubmitted] = useState<CalculatorParams | null>(null);
  const [journal, setJournal]               = useState<JournalScenario[]>([]);
  const [justSaved, setJustSaved]           = useState(false);
  const [loadedScenario, setLoadedScenario] = useState<string | null>(null); // id of loaded entry
  const [daysToTarget, setDaysToTarget]     = useState(0); // days until stock reaches expected price

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
      setLoadedScenario(null);
    }
  }, [params.ticker]);

  useEffect(() => {
    if (!compareMode) {
      setCompareExpiry("");
      setCompareSubmitted(null);
    }
  }, [compareMode]);

  const { data, error, isLoading }                                     = useCalculator(submitted);
  const { data: compareData, error: compareError, isLoading: cmpLoad } = useCalculator(compareSubmitted);

  const loading = isLoading || cmpLoad;

  const handleChange = useCallback((partial: Partial<CalculatorParams>) => {
    setParams(p => ({ ...p, ...partial }));
  }, []);

  const handleSubmit = useCallback(() => {
    incrementUsage();
    const snap = { ...params };
    setSubmitted(snap);
    setLoadedScenario(null);
    if (compareMode && compareExpiry) {
      setCompareSubmitted({ ...snap, expiration: compareExpiry });
    } else {
      setCompareSubmitted(null);
    }
  }, [params, compareMode, compareExpiry]);

  // ── Save scenario ──────────────────────────────────────────────────────────
  const saveToJournal = useCallback(() => {
    if (!data || !submitted) return;
    const outcomes = buildExpectationResult(data, data.target_price, daysToTarget).outcomes;
    const entry = buildJournalEntry(
      submitted,
      data,
      outcomes,
      compareMode ? "compare" : "single",
      compareMode ? compareExpiry : undefined,
    );
    setJournal(prev => addJournalEntry(prev, entry));
    setJustSaved(true);
    setTimeout(() => setJustSaved(false), 2000);
  }, [data, submitted, compareMode, compareExpiry]);

  // ── Load scenario ──────────────────────────────────────────────────────────
  const loadScenario = useCallback((entry: JournalScenario) => {
    setParams(entry.calParams);
    setSubmitted(entry.calParams);
    setLoadedScenario(entry.id);
    if (entry.strategyMode === "compare" && entry.compareExpiry) {
      setCompareMode(true);
      setCompareExpiry(entry.compareExpiry);
      setCompareSubmitted({ ...entry.calParams, expiration: entry.compareExpiry });
    } else {
      setCompareMode(false);
      setCompareExpiry("");
      setCompareSubmitted(null);
    }
    // Scroll back to top
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const deleteEntry = useCallback((id: string) => {
    setJournal(prev => deleteJournalEntry(prev, id));
  }, []);

  const onClearJournal = useCallback(() => {
    setJournal(clearJournalStorage());
  }, []);

  const hasResults = data && !error;
  const hasCompare = compareData && !compareError && compareMode;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Page header */}
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Target Move Calculator</h1>
        <p className="text-sm text-text-muted">
          Set your expected price and expiration — get the best options strategy, priced with Black-Scholes
        </p>
      </div>

      {/* Loaded scenario banner */}
      {loadedScenario && (
        <div className="rounded-lg border border-accent/30 bg-accent/5 px-3 py-2 text-xs text-accent flex items-center gap-2">
          <RotateCcw className="w-3.5 h-3.5" />
          Loaded saved scenario · hit <span className="font-semibold ml-1">Analyze Strikes</span> to refresh
        </div>
      )}

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
      {compareError && <ErrorState message={`Compare analysis failed: ${(compareError as Error).message}`} />}

      {/* ── EXPECTATION ENGINE (if calculator results available) ─────────── */}
      {hasResults && data.target_price > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-sm font-semibold text-text-primary">
              Expected Outcome
            </h2>
            <div className="flex-1 h-px bg-bg-border" />
            <label className="flex items-center gap-1.5 text-2xs text-text-muted whitespace-nowrap">
              Days until move:
              <input
                type="number" min="0" max={data.dte} step="1"
                className="input w-14 font-mono text-xs py-0.5 px-1.5"
                value={daysToTarget || ""}
                onChange={e => setDaysToTarget(Math.min(Math.max(parseInt(e.target.value) || 0, 0), data.dte))}
                placeholder="0"
              />
            </label>
            <span className="text-2xs text-text-muted">Black-Scholes · {data.dte}d DTE</span>
          </div>
          <ExpectationPanel
            data={data}
            expectedPrice={data.target_price}
            daysToTarget={daysToTarget}
          />
        </div>
      )}

      {/* ── STRATEGY VISUALIZER ──────────────────────────────────────────── */}
      {params.current_price > 0 && (
        <StrategyVisualizer
          currentPrice={params.current_price}
          targetPrice={params.target_price}
          expiration={params.expiration || data?.expiration}
          dte={data?.dte}
        />
      )}

      {/* ── RESULTS ──────────────────────────────────────────────────────── */}
      {hasResults && (
        <div className="space-y-8">
          {/* Save to Journal */}
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
              {justSaved ? "Scenario Saved!" : "Save Scenario"}
            </button>
            {!justSaved && (
              <span className="text-xs text-text-muted">Saves full scenario with BS outcomes · click to reload later</span>
            )}
          </div>

          {hasCompare ? (
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
            <span className="text-text-secondary">Weekly</span> quick-select for fast expiration picks.
          </p>
        </div>
      )}

      {/* ── AD PLACEHOLDER (Phase 8 — AdSense ready) ─────────────────────── */}
      {/* Uncomment and replace with actual AdSense unit when monetizing */}
      {/* <div className="rounded-xl border border-bg-border border-dashed h-20 flex items-center justify-center text-2xs text-text-muted">
        Ad placeholder — 728×90
      </div> */}

      {/* ── JOURNAL ───────────────────────────────────────────────────────── */}
      <div className="border-t border-bg-border pt-6">
        <JournalSection
          journal={journal}
          onDelete={deleteEntry}
          onClear={onClearJournal}
          onLoad={loadScenario}
        />
      </div>
    </div>
  );
}
