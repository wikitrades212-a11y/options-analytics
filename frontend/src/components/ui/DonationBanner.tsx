"use client";

import { useState, useEffect } from "react";
import { Copy, Check, X, Heart } from "lucide-react";

const DONATION_ITEMS = [
  { label: "BTC",      address: "bc1qx6yvrptsytxxepp7n8elwkxcycs7w9pvhg7ewg" },
  { label: "ETH",      address: "0xa81ded7DF812795326404619b84376abF96048f4" },
  { label: "SOL",      address: "Bfy6v9PkAamUZjXXxUfepgfsxy3xyKMMdiPyR7XGAjQv" },
  { label: "Cash App", address: "$epay" },
];

export default function DonationBanner() {
  const [show, setShow]     = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    const dismissed = localStorage.getItem("donation_dismissed");
    if (!dismissed) setShow(true);
  }, []);

  const dismiss = () => {
    setShow(false);
    localStorage.setItem("donation_dismissed", "1");
  };

  const copy = async (address: string, label: string) => {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {}
  };

  if (!show) return null;

  return (
    <div className="card border border-accent/20 bg-accent/5 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Heart className="w-4 h-4 text-accent" />
          <span className="text-sm font-semibold text-text-primary">Support the hard work</span>
        </div>
        <button
          onClick={dismiss}
          className="text-text-muted hover:text-text-primary transition-colors p-0.5"
          aria-label="Dismiss"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {DONATION_ITEMS.map(({ label, address }) => (
          <div key={label} className="flex items-center gap-2 bg-bg-raised rounded-lg px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="text-2xs text-text-muted font-medium mb-0.5">{label}</div>
              <div className="text-xs font-mono text-text-secondary truncate">{address}</div>
            </div>
            <button
              onClick={() => copy(address, label)}
              className="shrink-0 p-1.5 rounded-md bg-bg-hover hover:bg-accent/20 text-text-muted hover:text-accent transition-colors"
              aria-label={`Copy ${label} address`}
            >
              {copied === label
                ? <Check className="w-3.5 h-3.5 text-success" />
                : <Copy className="w-3.5 h-3.5" />
              }
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
