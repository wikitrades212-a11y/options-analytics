"use client";

import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell, ReferenceLine } from "recharts";
import type { FCFProfile } from "@/lib/types";

function fmtB(v: number): string {
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
  return `$${v.toFixed(0)}`;
}

const CONSISTENCY_COLOR: Record<string, string> = {
  Strong:   "#22c55e",
  Moderate: "#38bdf8",
  Weak:     "#f59e0b",
  Unstable: "#f97316",
  Negative: "#ef4444",
  Unknown:  "#6b7280",
};

export default function FCFChart({ fcf }: { fcf: FCFProfile }) {
  if (!fcf.years.length) return null;

  const data = fcf.years.map((year, i) => ({
    year: String(year),
    fcf: fcf.values[i],
  }));

  const barColor = CONSISTENCY_COLOR[fcf.consistency] ?? "#38bdf8";

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted">
          Free Cash Flow History
        </h3>
        <span className="text-2xs px-2 py-0.5 rounded-full bg-bg-raised border border-bg-border text-text-muted">
          FCF profile:{" "}
          <span className="font-semibold" style={{ color: barColor }}>
            {fcf.consistency}
          </span>
        </span>
      </div>

      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={data} barCategoryGap="35%" margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
          <XAxis
            dataKey="year"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tickFormatter={fmtB}
            tick={{ fill: "var(--text-muted)", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            width={56}
          />
          <Tooltip
            formatter={(value: number) => [fmtB(value), "FCF"]}
            contentStyle={{
              background: "var(--bg-surface)",
              border: "1px solid var(--bg-border)",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelStyle={{ color: "var(--text-secondary)" }}
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
          />
          <ReferenceLine y={0} stroke="var(--bg-border)" />
          <Bar dataKey="fcf" radius={[3, 3, 0, 0]}>
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={d.fcf >= 0 ? barColor : "#ef4444"}
                fillOpacity={0.85}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {fcf.avg_fcf_3y !== null && (
        <div className="mt-2 text-xs text-text-muted">
          3y avg FCF: <span className="text-text-secondary font-mono">{fmtB(fcf.avg_fcf_3y)}</span>
          {fcf.latest_fcf !== null && (
            <span className="ml-3">
              Latest: <span className="text-text-secondary font-mono">{fmtB(fcf.latest_fcf)}</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
