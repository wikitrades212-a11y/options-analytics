"use client";

import useSWR from "swr";
import type { StockAnalysis } from "@/lib/types";

const fetcher = async <T,>(url: string): Promise<T> => {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.detail ?? `Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
};

export function useStockFundamentals(ticker: string | null, dcfMethod = "conservative") {
  const key = ticker
    ? `/api/stock/${encodeURIComponent(ticker.toUpperCase())}?dcf_method=${dcfMethod}`
    : null;

  return useSWR<StockAnalysis>(key, fetcher<StockAnalysis>, {
    revalidateOnFocus: false,
    keepPreviousData: false,
  });
}
