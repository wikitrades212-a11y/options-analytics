"use client";

import { AlertTriangle, CheckCircle } from "lucide-react";

interface Props {
  reasons: string[];
  warnings: string[];
}

export default function ReasonsAndWarnings({ reasons, warnings }: Props) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {/* Why */}
      {reasons.length > 0 && (
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <CheckCircle className="w-3.5 h-3.5 text-accent" />
            <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted">Why This Verdict</h3>
          </div>
          <ul className="space-y-1.5">
            {reasons.map((r, i) => (
              <li key={i} className="text-xs text-text-secondary flex gap-2">
                <span className="text-text-muted mt-px shrink-0">•</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Warnings */}
      {warnings.length > 0 && (
        <div className="card border-warn/20">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle className="w-3.5 h-3.5 text-warn" />
            <h3 className="text-xs font-semibold uppercase tracking-widest text-text-muted">Warnings</h3>
          </div>
          <ul className="space-y-1.5">
            {warnings.map((w, i) => (
              <li key={i} className="text-xs text-warn/80 flex gap-2">
                <span className="mt-px shrink-0">⚠</span>
                <span>{w}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
