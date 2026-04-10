"""
Trending ticker fetcher — zero-auth public endpoints.

Sources:
  Yahoo Finance: https://query1.finance.yahoo.com/v1/finance/trending/US
  StockTwits:    https://api.stocktwits.com/api/2/trending/symbols.json

No API keys required. Falls back to empty list on any source error.
"""
import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_YAHOO_URL      = "https://query1.finance.yahoo.com/v1/finance/trending/US"
_STOCKTWITS_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"

_TICKER_RE = re.compile(r'^[A-Z]{1,5}$')
_BLOCKLIST  = {"BTC", "ETH", "BTC-USD", "ETH-USD", "^VIX", "^GSPC", "^IXIC", "^DJI", "GC=F", "CL=F"}


def _is_valid_ticker(symbol: str) -> bool:
    return bool(_TICKER_RE.match(symbol)) and symbol not in _BLOCKLIST


async def _fetch_yahoo(client: httpx.AsyncClient) -> list[str]:
    try:
        r = await client.get(_YAHOO_URL, timeout=8)
        r.raise_for_status()
        data  = r.json()
        syms  = [q["symbol"].upper() for q in data["finance"]["result"][0]["quotes"] if "symbol" in q]
        valid = [s for s in syms if _is_valid_ticker(s)]
        logger.info("trending/yahoo: raw=%d  valid=%d  %s", len(syms), len(valid), valid)
        return valid
    except Exception as exc:
        logger.warning("trending/yahoo failed: %s", exc)
        return []


async def _fetch_stocktwits(client: httpx.AsyncClient) -> list[str]:
    try:
        r = await client.get(_STOCKTWITS_URL, timeout=8)
        r.raise_for_status()
        data  = r.json()
        syms  = [s["symbol"].upper() for s in data.get("symbols", []) if "symbol" in s]
        valid = [s for s in syms if _is_valid_ticker(s)]
        logger.info("trending/stocktwits: raw=%d  valid=%d  %s", len(syms), len(valid), valid)
        return valid
    except Exception as exc:
        logger.warning("trending/stocktwits failed: %s", exc)
        return []


async def get_trending_tickers(limit: int = 10) -> list[str]:
    """
    Fetch and merge trending tickers from Yahoo Finance and StockTwits.
    Returns up to `limit` deduplicated valid US equity tickers.
    Returns [] if all sources fail — callers must handle gracefully.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        yahoo, stocktwits = await asyncio.gather(
            _fetch_yahoo(client),
            _fetch_stocktwits(client),
        )

    seen: set[str] = set()
    merged: list[str] = []
    for ticker in yahoo + stocktwits:        # Yahoo first (higher quality signal)
        if ticker not in seen:
            seen.add(ticker)
            merged.append(ticker)

    result = merged[:limit]
    logger.info("trending/merged: final=%d  %s", len(result), result)
    return result
