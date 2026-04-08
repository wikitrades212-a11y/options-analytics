"""
Tradier options data provider.
Uses Tradier Brokerage API v1 for market data.
Configure via: DATA_PROVIDER=tradier, TRADIER_TOKEN, TRADIER_SANDBOX
"""
import asyncio
import logging
import math
from typing import List, Optional

import httpx

from app.config import settings
from .base import OptionsDataProvider

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://sandbox.tradier.com"
LIVE_BASE    = "https://api.tradier.com"


def _safe_float(val, default: Optional[float] = None) -> Optional[float]:
    """Convert val to float; return default on failure, NaN, or Inf."""
    try:
        if val is None:
            return default
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


class TradierProvider(OptionsDataProvider):
    """
    Tradier Brokerage API implementation.
    Docs: https://documentation.tradier.com/brokerage-api/markets/get-options-chains
    """

    def __init__(self):
        self._token = settings.tradier_token
        self._sandbox = settings.tradier_sandbox
        self._base = SANDBOX_BASE if self._sandbox else LIVE_BASE
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        self._health_ok: Optional[bool] = None
        logger.info(
            f"Using Tradier provider ({'sandbox' if self._sandbox else 'live'})"
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=15.0,
        )

    async def get_underlying_price(self, ticker: str) -> float:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/v1/markets/quotes",
                    params={"symbols": ticker.upper()},
                )
            if resp.status_code == 401:
                raise RuntimeError("Tradier authentication failed (401)")
            if resp.status_code != 200:
                raise RuntimeError(f"Tradier returned {resp.status_code} for quotes")
            data = resp.json()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Tradier request failed: {exc}") from exc

        quote = data.get("quotes", {}).get("quote") or {}
        if isinstance(quote, list):
            quote = quote[0] if quote else {}

        price = _safe_float(quote.get("last")) or _safe_float(quote.get("prevclose"))
        if not price:
            raise ValueError(f"No price data returned for {ticker}")
        return price

    async def get_expirations(self, ticker: str) -> List[str]:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/v1/markets/options/expirations",
                    params={"symbol": ticker.upper(), "includeAllRoots": "true"},
                )
            if resp.status_code == 401:
                raise RuntimeError("Tradier authentication failed (401)")
            if resp.status_code != 200:
                raise RuntimeError(f"Tradier returned {resp.status_code} for expirations")
            data = resp.json()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Tradier request failed: {exc}") from exc

        raw = (data.get("expirations") or {}).get("date") or []
        if isinstance(raw, str):
            raw = [raw]
        return sorted(raw)

    async def get_option_chain(self, ticker: str, expiration: str) -> List[dict]:
        if not self._token:
            logger.error("Tradier auth failure: no TRADIER_TOKEN configured")
            raise RuntimeError("Tradier provider is misconfigured: missing token")

        try:
            async with self._client() as client:
                resp = await client.get(
                    "/v1/markets/options/chains",
                    params={
                        "symbol": ticker.upper(),
                        "expiration": expiration,
                        "greeks": "true",
                    },
                )
        except Exception as exc:
            logger.error(f"Tradier network error fetching chain {ticker} {expiration}: {exc}")
            raise RuntimeError(f"Tradier request failed: {exc}") from exc

        if resp.status_code == 401:
            logger.error("Tradier auth failure: 401 Unauthorized")
            raise RuntimeError("Tradier authentication failed")
        if resp.status_code != 200:
            logger.error(f"Tradier request failed: {resp.status_code}")
            raise RuntimeError(f"Tradier returned {resp.status_code}")

        data = resp.json()
        raw_list = (data.get("options") or {}).get("option") or []
        if isinstance(raw_list, dict):
            raw_list = [raw_list]

        return [self._normalize(opt, ticker) for opt in raw_list if opt]

    async def get_option_chain_bulk(
        self, ticker: str, expirations: List[str]
    ) -> List[dict]:
        """Fetch multiple expirations concurrently."""
        if not expirations:
            return []
        tasks = [self.get_option_chain(ticker, exp) for exp in expirations]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        contracts: List[dict] = []
        for exp, result in zip(expirations, results):
            if isinstance(result, Exception):
                logger.warning(f"Tradier chain fetch failed for {ticker} {exp}: {result}")
            else:
                contracts.extend(result)
        return contracts

    def _normalize(self, opt: dict, ticker: str) -> dict:
        bid = _safe_float(opt.get("bid"), 0.0)
        ask = _safe_float(opt.get("ask"), 0.0)
        mid = round((bid + ask) / 2, 4)

        greeks = opt.get("greeks") or {}

        option_type = (opt.get("option_type") or "").lower()
        if option_type not in ("call", "put"):
            option_type = "call" if "C" in opt.get("symbol", "") else "put"

        # Prefer mid_iv from greeks; fall back to root-level implied_volatility
        iv = _safe_float(greeks.get("mid_iv"))
        if iv is None:
            iv = _safe_float(opt.get("implied_volatility"))
        if iv is None:
            iv = 0.0

        return {
            "ticker":             ticker.upper(),
            "strike":             _safe_float(opt.get("strike"), 0.0),
            "expiration":         opt.get("expiration_date", ""),
            "option_type":        option_type,
            "bid":                bid,
            "ask":                ask,
            "mid":                mid,
            "last":               _safe_float(opt.get("last"), 0.0),
            "mark":               mid,
            "volume":             _safe_int(opt.get("volume"), 0),
            "open_interest":      _safe_int(opt.get("open_interest"), 0),
            "implied_volatility": iv,
            "delta":              _safe_float(greeks.get("delta")),
            "gamma":              _safe_float(greeks.get("gamma")),
            "theta":              _safe_float(greeks.get("theta")),
            "vega":               _safe_float(greeks.get("vega")),
            "rho":                _safe_float(greeks.get("rho")),
        }

    async def health_check(self) -> bool:
        if not self._token:
            logger.warning("Tradier auth failure: TRADIER_TOKEN not set — misconfigured")
            self._health_ok = False
            return False
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/v1/markets/quotes",
                    params={"symbols": "SPY"},
                )
            if resp.status_code == 200:
                logger.info("Tradier health check passed")
                self._health_ok = True
                return True
            if resp.status_code == 401:
                logger.error("Tradier auth failure: 401 Unauthorized")
            else:
                logger.error(f"Tradier request failed: HTTP {resp.status_code}")
            self._health_ok = False
            return False
        except Exception as exc:
            logger.error(f"Tradier request failed: {exc}")
            self._health_ok = False
            return False
