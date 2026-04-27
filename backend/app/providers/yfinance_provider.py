"""
Yahoo Finance Provider
Fetches annual financial statement data and maps it to RawStockData.
Price: Tradier → Alpaca → Robinhood → yfinance (first live source wins).
Fundamentals (income, balance, cashflow): yfinance.

Field mapping is defensive: we try multiple known column names and fall
back gracefully when Yahoo changes its schema between library versions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx
import pandas as pd
import yfinance as yf

from app.models.stock_fundamentals import (
    BalanceSheetRow,
    CashFlowRow,
    IncomeStatementRow,
    RawStockData,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(df: pd.DataFrame, *keys: str) -> Optional[pd.Series]:
    """Return the first matching row from a DataFrame by trying multiple key names."""
    for key in keys:
        if key in df.index:
            return df.loc[key]
    return None


def _val(series: Optional[pd.Series], col_idx: int = 0) -> Optional[float]:
    """Extract a float from a pandas Series column, or None on any failure."""
    if series is None:
        return None
    try:
        v = series.iloc[col_idx]
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _info(info: dict, *keys: str) -> Optional[float]:
    """Pull a value from the yf.info dict, trying multiple key names."""
    for k in keys:
        v = info.get(k)
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# ── Per-statement parsers ─────────────────────────────────────────────────────

def _parse_income(df: pd.DataFrame) -> list[IncomeStatementRow]:
    # Columns are DatetimeIndex, most-recent first
    rows = []
    for i, col in enumerate(df.columns):
        year = col.year
        revenue   = _val(_get(df, "Total Revenue"), i)
        gross     = _val(_get(df, "Gross Profit"), i)
        op_inc    = _val(_get(df, "Operating Income", "EBIT"), i)
        net_inc   = _val(_get(df, "Net Income", "Net Income Common Stockholders"), i)
        eps       = _val(_get(df, "Diluted EPS", "Basic EPS"), i)
        ebitda    = _val(_get(df, "EBITDA", "Normalized EBITDA"), i)
        int_exp   = _val(_get(df, "Interest Expense", "Interest Expense Non Operating"), i)

        rows.append(IncomeStatementRow(
            year=year,
            revenue=revenue,
            gross_profit=gross,
            operating_income=op_inc,
            net_income=net_inc,
            eps=eps,
            ebitda=ebitda,
            interest_expense=int_exp,
        ))
    return sorted(rows, key=lambda r: r.year)


def _parse_balance(df: pd.DataFrame) -> list[BalanceSheetRow]:
    rows = []
    for i, col in enumerate(df.columns):
        year = col.year
        rows.append(BalanceSheetRow(
            year=year,
            total_assets=_val(_get(df, "Total Assets"), i),
            total_liabilities=_val(_get(df, "Total Liabilities Net Minority Interest", "Total Liab"), i),
            total_equity=_val(_get(df, "Stockholders Equity", "Total Stockholder Equity"), i),
            total_debt=_val(_get(df, "Total Debt", "Long Term Debt And Capital Lease Obligation"), i),
            cash_and_equivalents=_val(_get(df, "Cash And Cash Equivalents", "Cash"), i),
            current_assets=_val(_get(df, "Current Assets"), i),
            current_liabilities=_val(_get(df, "Current Liabilities"), i),
        ))
    return sorted(rows, key=lambda r: r.year)


def _parse_cashflow(df: pd.DataFrame) -> list[CashFlowRow]:
    rows = []
    for i, col in enumerate(df.columns):
        year = col.year
        ocf   = _val(_get(df, "Operating Cash Flow", "Total Cash From Operating Activities"), i)
        capex = _val(_get(df, "Capital Expenditure", "Capital Expenditures"), i)
        fcf   = _val(_get(df, "Free Cash Flow"), i)
        rows.append(CashFlowRow(
            year=year,
            operating_cash_flow=ocf,
            capital_expenditures=capex,
            free_cash_flow=fcf,
        ))
    return sorted(rows, key=lambda r: r.year)


# ── Main fetch function (sync, run in executor) ───────────────────────────────

def _fetch_sync(ticker: str) -> RawStockData:
    yf_ticker = yf.Ticker(ticker)
    info: dict[str, Any] = yf_ticker.info or {}

    # Don't fail here — live price sources (Tradier/Alpaca/RH) will supply the
    # price even if Yahoo Finance's info dict is empty. Use 0.0 as placeholder.
    current_price = (
        _info(info, "currentPrice", "regularMarketPrice", "previousClose") or 0.0
    )

    company_name = info.get("longName") or info.get("shortName") or ticker
    market_cap   = _info(info, "marketCap")
    shares       = _info(info, "sharesOutstanding", "impliedSharesOutstanding")
    sector       = info.get("sector")
    beta         = _info(info, "beta")
    forward_pe   = _info(info, "forwardPE")
    analyst_tp   = _info(info, "targetMeanPrice")
    ev           = _info(info, "enterpriseValue")
    ttm_rev      = _info(info, "totalRevenue")
    ttm_ebitda   = _info(info, "ebitda")

    # Annual statements (up to 4 years)
    try:
        income_rows = _parse_income(yf_ticker.income_stmt)
    except Exception as e:
        logger.warning("%s income_stmt parse failed: %s", ticker, e)
        income_rows = []

    try:
        bs_rows = _parse_balance(yf_ticker.balance_sheet)
    except Exception as e:
        logger.warning("%s balance_sheet parse failed: %s", ticker, e)
        bs_rows = []

    try:
        cf_rows = _parse_cashflow(yf_ticker.cashflow)
    except Exception as e:
        logger.warning("%s cashflow parse failed: %s", ticker, e)
        cf_rows = []

    return RawStockData(
        ticker=ticker.upper(),
        company_name=company_name,
        current_price=current_price,
        market_cap=market_cap,
        shares_outstanding=shares,
        sector=sector,
        beta=beta,
        forward_pe=forward_pe,
        analyst_target_price=analyst_tp,
        enterprise_value=ev,
        ttm_revenue=ttm_rev,
        ttm_ebitda=ttm_ebitda,
        income_statements=income_rows,
        balance_sheets=bs_rows,
        cash_flows=cf_rows,
    )


async def _fetch_live_price(ticker: str) -> tuple[Optional[float], str]:
    """Try Tradier → Alpaca → Robinhood for a real-time price.
    Returns (price, source_name) or (None, 'yfinance') if all fail."""
    from app.config import settings

    # Tradier
    if settings.tradier_token:
        try:
            async with httpx.AsyncClient(timeout=4.0) as c:
                r = await c.get(
                    "https://api.tradier.com/v1/markets/quotes",
                    headers={"Authorization": f"Bearer {settings.tradier_token}", "Accept": "application/json"},
                    params={"symbols": ticker, "greeks": "false"},
                )
            if r.status_code == 200:
                quote = r.json().get("quotes", {}).get("quote", {})
                price = quote.get("last") or quote.get("bid")
                if price and float(price) > 0:
                    return float(price), "tradier"
        except Exception:
            pass

    # Alpaca
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            async with httpx.AsyncClient(timeout=4.0) as c:
                r = await c.get(
                    f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
                    headers={"APCA-API-KEY-ID": settings.alpaca_api_key, "APCA-API-SECRET-KEY": settings.alpaca_api_secret},
                    params={"feed": "sip"},
                )
            if r.status_code == 200:
                price = r.json().get("trade", {}).get("p")
                if price and float(price) > 0:
                    return float(price), "alpaca"
        except Exception:
            pass

    # Robinhood
    if settings.robinhood_token:
        try:
            async with httpx.AsyncClient(timeout=4.0) as c:
                r = await c.get(
                    f"https://api.robinhood.com/quotes/{ticker}/",
                    headers={"Authorization": f"Bearer {settings.robinhood_token}"},
                )
            if r.status_code == 200:
                price = r.json().get("last_trade_price")
                if price and float(price) > 0:
                    return float(price), "robinhood"
        except Exception:
            pass

    return None, "yfinance"


async def fetch_raw_stock_data(ticker: str) -> RawStockData:
    """Async wrapper — runs yfinance (for fundamentals) and live price sources
    concurrently. Live price wins over yfinance. Raises only if no price at all."""
    loop = asyncio.get_event_loop()

    raw, (live_price, price_source) = await asyncio.gather(
        loop.run_in_executor(None, _fetch_sync, ticker.upper()),
        _fetch_live_price(ticker.upper()),
    )

    final_price = live_price or raw.current_price
    if not final_price:
        raise ValueError(f"No price data found for '{ticker}'. Check the symbol.")

    return raw.model_copy(update={
        "current_price": final_price,
        "price_source":  price_source,
    })
