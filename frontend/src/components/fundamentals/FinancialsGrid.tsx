"use client";

import type { GrowthMetrics, MarginMetrics, FinancialHealthMetrics } from "@/lib/types";
import MetricRow from "./MetricRow";

function fmtPct(v: number | null): string {
  if (v === null) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${(v * 100).toFixed(1)}%`;
}

function fmtX(v: number | null): string {
  if (v === null) return "—";
  return `${v.toFixed(2)}x`;
}

function fmtDollar(v: number | null): string {
  if (v === null) return "—";
  if (Math.abs(v) >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (Math.abs(v) >= 1e9)  return `$${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6)  return `$${(v / 1e6).toFixed(1)}M`;
  return `$${v.toFixed(0)}`;
}

function growthGood(v: number | null): boolean | null {
  if (v === null) return null;
  return v >= 0.05;
}

function marginGood(v: number | null, threshold: number): boolean | null {
  if (v === null) return null;
  return v >= threshold;
}

interface Props {
  growth: GrowthMetrics;
  margins: MarginMetrics;
  health: FinancialHealthMetrics;
}

export default function FinancialsGrid({ growth, margins, health }: Props) {
  const debtBad = health.debt_level === "High" || health.debt_level === "Extreme";
  const debtGood = health.debt_level === "Low";
  const liquidityGood = health.liquidity === "Strong";
  const liquidityBad = health.liquidity === "Weak";

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {/* Growth */}
      <div className="card">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted mb-3">Growth</h3>
        <MetricRow label="Revenue YoY"      value={fmtPct(growth.revenue_growth_yoy)} good={growthGood(growth.revenue_growth_yoy)} />
        <MetricRow label="Revenue CAGR 3y"  value={fmtPct(growth.revenue_cagr_3y)}    good={growthGood(growth.revenue_cagr_3y)} />
        <MetricRow label="Net Income YoY"   value={fmtPct(growth.net_income_growth_yoy)} good={growthGood(growth.net_income_growth_yoy)} />
        <MetricRow label="EPS YoY"          value={fmtPct(growth.eps_growth_yoy)}     good={growthGood(growth.eps_growth_yoy)} />
        <MetricRow label="FCF YoY"          value={fmtPct(growth.fcf_growth_yoy)}     good={growthGood(growth.fcf_growth_yoy)} />
        <MetricRow label="FCF CAGR 3y"      value={fmtPct(growth.fcf_cagr_3y)}        good={growthGood(growth.fcf_cagr_3y)} />
      </div>

      {/* Margins */}
      <div className="card">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted mb-3">Margins</h3>
        <MetricRow label="Gross Margin"     value={fmtPct(margins.gross_margin)}     good={marginGood(margins.gross_margin, 0.30)} />
        <MetricRow label="Operating Margin" value={fmtPct(margins.operating_margin)} good={marginGood(margins.operating_margin, 0.10)} />
        <MetricRow label="Net Margin"       value={fmtPct(margins.net_margin)}       good={marginGood(margins.net_margin, 0.08)} />
        <MetricRow label="FCF Margin"       value={fmtPct(margins.fcf_margin)}       good={marginGood(margins.fcf_margin, 0.08)} />
      </div>

      {/* Balance Sheet Health */}
      <div className="card">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted mb-3">Balance Sheet</h3>
        <MetricRow label="Debt Load"       value={health.debt_level ?? "—"}   mono={false} good={debtGood ? true : debtBad ? false : null} />
        <MetricRow label="Liquidity"       value={health.liquidity ?? "—"}    mono={false} good={liquidityGood ? true : liquidityBad ? false : null} />
        <MetricRow label="D/E Ratio"       value={health.debt_to_equity !== null ? fmtX(health.debt_to_equity) : "—"} good={health.debt_to_equity !== null ? health.debt_to_equity < 1 : null} />
        <MetricRow label="Current Ratio"   value={health.current_ratio !== null ? fmtX(health.current_ratio) : "—"} good={health.current_ratio !== null ? health.current_ratio >= 1.5 : null} />
        <MetricRow label="Interest Cov."   value={health.interest_coverage !== null ? fmtX(health.interest_coverage) : "—"} good={health.interest_coverage !== null ? health.interest_coverage >= 5 : null} />
        <MetricRow label="Net Debt"        value={fmtDollar(health.net_debt)} good={health.net_debt !== null ? health.net_debt < 0 : null} />
      </div>
    </div>
  );
}
