"use client";

import type { ValuationMetrics } from "@/lib/types";
import MetricRow from "./MetricRow";

function fmtX(v: number | null): string {
  if (v === null) return "—";
  return `${v.toFixed(1)}x`;
}

function fmtPct(v: number | null): string {
  if (v === null) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${(v * 100).toFixed(1)}%`;
}

// Rough "is this cheap?" heuristics — directional, not absolute truth
function peGood(v: number | null): boolean | null {
  if (v === null || v < 0) return null;
  return v < 20;
}
function yieldGood(v: number | null): boolean | null {
  if (v === null) return null;
  return v >= 0.04;
}
function pegGood(v: number | null): boolean | null {
  if (v === null) return null;
  return v < 1.5;
}
function evEbitdaGood(v: number | null): boolean | null {
  if (v === null) return null;
  return v < 15;
}

export default function ValuationGrid({ valuation }: { valuation: ValuationMetrics }) {
  return (
    <div className="card">
      <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted mb-3">Valuation</h3>
      <MetricRow label="P/E"         value={fmtX(valuation.pe_ratio)}        good={peGood(valuation.pe_ratio)} />
      <MetricRow label="Forward P/E" value={fmtX(valuation.forward_pe)}      good={peGood(valuation.forward_pe)} />
      <MetricRow label="PEG"         value={fmtX(valuation.peg_ratio)}       good={pegGood(valuation.peg_ratio)} />
      <MetricRow label="P/S"         value={fmtX(valuation.price_to_sales)}  good={valuation.price_to_sales !== null ? valuation.price_to_sales < 3 : null} />
      <MetricRow label="P/B"         value={fmtX(valuation.price_to_book)}   good={valuation.price_to_book !== null ? valuation.price_to_book < 3 : null} />
      <MetricRow label="EV/EBITDA"   value={fmtX(valuation.ev_to_ebitda)}   good={evEbitdaGood(valuation.ev_to_ebitda)} />
      <MetricRow label="FCF Yield"   value={fmtPct(valuation.fcf_yield)}    good={yieldGood(valuation.fcf_yield)} />
    </div>
  );
}
