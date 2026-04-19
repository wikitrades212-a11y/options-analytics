"use client";

import clsx from "clsx";
import type { StockAnalysis } from "@/lib/types";

const VERDICT_CONFIG: Record<string, { bg: string; border: string; dot: string; label: string }> = {
  "Strong Candidate":           { bg: "bg-success/10",  border: "border-success/30",  dot: "bg-success",  label: "🟢" },
  "Watchlist":                  { bg: "bg-accent/10",   border: "border-accent/30",   dot: "bg-accent",   label: "🟡" },
  "Good Business, Too Expensive":{ bg: "bg-warn/10",   border: "border-warn/30",     dot: "bg-warn",     label: "🟠" },
  "Speculative":                { bg: "bg-warn/10",     border: "border-warn/30",     dot: "bg-warn",     label: "🟠" },
  "Avoid":                      { bg: "bg-put/10",      border: "border-put/30",      dot: "bg-put",      label: "🔴" },
};

function ScoreBar({ total }: { total: number }) {
  const pct = Math.min(100, Math.max(0, total));
  const color =
    pct >= 70 ? "bg-success" :
    pct >= 50 ? "bg-accent" :
    pct >= 35 ? "bg-warn" :
    "bg-put";

  return (
    <div className="flex items-center gap-3">
      <div className="flex-1 h-2 bg-bg-raised rounded-full overflow-hidden">
        <div
          className={clsx("h-full rounded-full transition-all duration-700", color)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-sm font-bold tabular-nums text-text-primary w-14 text-right">
        {pct.toFixed(0)}<span className="text-text-muted font-normal">/100</span>
      </span>
    </div>
  );
}

export default function VerdictBanner({ analysis }: { analysis: StockAnalysis }) {
  const cfg = VERDICT_CONFIG[analysis.verdict] ?? VERDICT_CONFIG["Watchlist"];
  const dcf = analysis.dcf;
  const upside = dcf.upside_downside_pct;

  return (
    <div className={clsx("rounded-xl border p-4 space-y-3", cfg.bg, cfg.border)}>
      {/* Top row */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 mb-0.5">
            <span className={clsx("w-2.5 h-2.5 rounded-full flex-shrink-0", cfg.dot)} />
            <span className="text-lg font-bold text-text-primary">{analysis.verdict}</span>
          </div>
          <p className="text-sm text-text-secondary max-w-xl">{analysis.summary}</p>
        </div>

        {/* DCF callout */}
        {dcf.is_reliable && upside !== null && (
          <div className="text-right shrink-0">
            <div className="text-2xs text-text-muted uppercase tracking-widest mb-0.5">
              DCF {upside >= 0 ? "Upside" : "Downside"}
            </div>
            <div className={clsx(
              "text-2xl font-bold tabular-nums",
              upside >= 0 ? "text-success" : "text-put"
            )}>
              {upside >= 0 ? "+" : ""}{(upside * 100).toFixed(1)}%
            </div>
            <div className="text-2xs text-text-muted">
              Fair Value ${dcf.intrinsic_value_per_share?.toFixed(2)}
            </div>
          </div>
        )}
      </div>

      {/* Score bar */}
      <ScoreBar total={analysis.score.score.total} />

      {/* Confidence + data quality badges */}
      <div className="flex flex-wrap gap-2">
        <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
          Score confidence: <span className="text-text-secondary capitalize">{analysis.score.confidence}</span>
        </span>
        <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
          DCF confidence: <span className="text-text-secondary capitalize">{dcf.confidence}</span>
        </span>
        <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
          Data: <span className="text-text-secondary">{analysis.data_quality}</span>
        </span>
      </div>
    </div>
  );
}
