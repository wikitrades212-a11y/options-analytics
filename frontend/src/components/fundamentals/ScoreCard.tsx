"use client";

import clsx from "clsx";
import type { ScoreBreakdown } from "@/lib/types";

interface PillarRowProps {
  label: string;
  score: number;
  max: number;
}

function PillarRow({ label, score, max }: PillarRowProps) {
  const pct = (score / max) * 100;
  const color =
    pct >= 70 ? "bg-success" :
    pct >= 45 ? "bg-accent" :
    pct >= 25 ? "bg-warn" :
    "bg-put";

  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-text-secondary">{label}</span>
        <span className="tabular-nums font-mono text-text-primary">
          {score.toFixed(0)}<span className="text-text-muted">/{max}</span>
        </span>
      </div>
      <div className="h-1.5 bg-bg-raised rounded-full overflow-hidden">
        <div
          className={clsx("h-full rounded-full transition-all duration-500", color)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function ScoreCard({ score }: { score: ScoreBreakdown }) {
  return (
    <div className="card space-y-4">
      <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted">Score Breakdown</h3>
      <PillarRow label="Business Quality"   score={score.business_quality}   max={35} />
      <PillarRow label="Financial Strength" score={score.financial_strength} max={20} />
      <PillarRow label="Valuation"          score={score.valuation}          max={30} />
      <PillarRow label="Risk / Stability"   score={score.risk_stability}     max={15} />
    </div>
  );
}
