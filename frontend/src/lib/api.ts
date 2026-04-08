import type {
  OptionChainResponse,
  UnusualOptionsResponse,
  TopContractsResponse,
  ExpirationResponse,
  SortMetric,
} from "./types";

const BASE = "/api/options";

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { next: { revalidate: 0 } });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail?.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  chain: (ticker: string): Promise<OptionChainResponse> =>
    fetchJSON(`${BASE}?ticker=${encodeURIComponent(ticker)}`),

  unusual: (ticker: string): Promise<UnusualOptionsResponse> =>
    fetchJSON(`${BASE}/unusual?ticker=${encodeURIComponent(ticker)}`),

  top: (ticker: string, metric: SortMetric, limit = 25): Promise<TopContractsResponse> =>
    fetchJSON(
      `${BASE}/top?ticker=${encodeURIComponent(ticker)}&metric=${metric}&limit=${limit}`
    ),

  expirations: (ticker: string): Promise<ExpirationResponse> =>
    fetchJSON(`${BASE}/expirations?ticker=${encodeURIComponent(ticker)}`),

  exportUrl: (ticker: string, optionType?: "call" | "put", minVolume = 0, minOI = 0): string => {
    const params = new URLSearchParams({ ticker });
    if (optionType) params.set("option_type", optionType);
    if (minVolume) params.set("min_volume", String(minVolume));
    if (minOI) params.set("min_oi", String(minOI));
    return `${BASE}/export?${params.toString()}`;
  },
};

// SWR fetcher compatible
export const fetcher = (url: string) => fetchJSON(url);
