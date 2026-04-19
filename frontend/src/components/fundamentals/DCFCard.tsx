"use client";

import clsx from "clsx";
import type { DCFResult } from "@/lib/types";

const CONFIDENCE_COLOR: Record<string, string> = {
  high:   "text-success",
  medium: "text-warn",
  low:    "text-put",
};

export default function DCFCard({ dcf }: { dcf: DCFResult }) {
  const upside = dcf.upside_downside_pct;
  const upsideColor = upside !== null && upside >= 0 ? "text-success" : "text-put";

  return (
    <div className="card space-y-3">
      <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted">DCF Model</h3>

      {dcf.is_reliable && dcf.intrinsic_value_per_share !== null ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="text-2xs text-text-muted mb-0.5">Fair Value</div>
              <div className="text-xl font-bold text-text-primary tabular-nums">
                ${dcf.intrinsic_value_per_share.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-2xs text-text-muted mb-0.5">vs Market</div>
              <div className={clsx("text-xl font-bold tabular-nums", upsideColor)}>
                {upside !== null ? `${upside >= 0 ? "+" : ""}${(upside * 100).toFixed(1)}%` : "—"}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 pt-1 border-t border-bg-border text-xs">
            {dcf.projected_growth_rate !== null && (
              <div>
                <span className="text-text-muted">Proj. Growth</span>
                <span className="ml-2 font-mono text-text-primary">
                  {(dcf.projected_growth_rate * 100).toFixed(1)}%
                </span>
              </div>
            )}
            <div>
              <span className="text-text-muted">Confidence</span>
              <span className={clsx("ml-2 font-semibold capitalize", CONFIDENCE_COLOR[dcf.confidence])}>
                {dcf.confidence}
              </span>
            </div>
          </div>

          {dcf.explanation && (
            <p className="text-2xs text-text-muted leading-relaxed border-t border-bg-border pt-2">
              {dcf.explanation}
            </p>
          )}
        </>
      ) : (
        <div className="space-y-2">
          <p className="text-sm text-put font-medium">DCF Unreliable</p>
          {dcf.confidence_reasons.map((r, i) => (
            <p key={i} className="text-xs text-text-muted">• {r}</p>
          ))}
        </div>
      )}
    </div>
  );
}
