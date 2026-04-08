"use client";

import useSWR from "swr";
import type {
  OptionChainResponse,
  UnusualOptionsResponse,
  TopContractsResponse,
  ExpirationResponse,
  SortMetric,
} from "@/lib/types";

const BASE = "/api/options";

interface SWROptions {
  refreshInterval?: number;
  revalidateOnFocus?: boolean;
}

const fetcher = async <T,>(url: string): Promise<T> => {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
};

export function useChain(ticker: string | null, opts: SWROptions = {}) {
  const key: string | null = ticker
    ? `${BASE}?ticker=${encodeURIComponent(ticker)}`
    : null;

  return useSWR<OptionChainResponse>(key, fetcher<OptionChainResponse>, {
    refreshInterval: opts.refreshInterval ?? 0,
    revalidateOnFocus: opts.revalidateOnFocus ?? false,
    keepPreviousData: true,
  });
}

export function useUnusual(ticker: string | null, opts: SWROptions = {}) {
  const key: string | null = ticker
    ? `${BASE}/unusual?ticker=${encodeURIComponent(ticker)}`
    : null;

  return useSWR<UnusualOptionsResponse>(key, fetcher<UnusualOptionsResponse>, {
    refreshInterval: opts.refreshInterval ?? 0,
    revalidateOnFocus: opts.revalidateOnFocus ?? false,
    keepPreviousData: true,
  });
}

export function useTop(
  ticker: string | null,
  metric: SortMetric = "unusual_score",
  limit = 25,
  opts: SWROptions = {}
) {
  const key: string | null = ticker
    ? `${BASE}/top?ticker=${encodeURIComponent(ticker)}&metric=${encodeURIComponent(metric)}&limit=${limit}`
    : null;

  return useSWR<TopContractsResponse>(key, fetcher<TopContractsResponse>, {
    refreshInterval: opts.refreshInterval ?? 0,
    revalidateOnFocus: opts.revalidateOnFocus ?? false,
    keepPreviousData: true,
  });
}

export function useExpirations(ticker: string | null) {
  const key: string | null = ticker
    ? `${BASE}/expirations?ticker=${encodeURIComponent(ticker)}`
    : null;

  return useSWR<ExpirationResponse>(key, fetcher<ExpirationResponse>, {
    revalidateOnFocus: false,
    keepPreviousData: true,
  });
}