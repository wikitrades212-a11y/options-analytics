"use client";

import clsx from "clsx";

interface Props {
  label: string;
  value: React.ReactNode;
  good?: boolean | null;   // true=green, false=red, null=neutral
  mono?: boolean;
}

export default function MetricRow({ label, value, good, mono = true }: Props) {
  const valueColor =
    good === true  ? "text-success" :
    good === false ? "text-put" :
    "text-text-primary";

  return (
    <div className="flex items-center justify-between py-1.5 border-b border-bg-border/60 last:border-0">
      <span className="text-xs text-text-secondary">{label}</span>
      <span className={clsx(
        "text-xs font-medium",
        mono && "font-mono tabular-nums",
        valueColor,
      )}>
        {value ?? "—"}
      </span>
    </div>
  );
}
