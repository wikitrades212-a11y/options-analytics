"""
Stock Fundamentals Router
Plugs into the existing FastAPI app.

Routes:
  GET  /api/stock/{ticker}               — fetch data + run analysis (live, via yfinance)
  POST /api/stock/{ticker}/analyze       — full analysis with supplied raw data
  POST /api/stock/{ticker}/telegram      — analyze + push to Telegram
  POST /api/stock/screen                 — batch screen a list of stocks
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.models.stock_fundamentals import DCFConfig, RawStockData, StockAnalysis
from app.providers.yfinance_provider import fetch_raw_stock_data
from app.screener.low_hanging_fruit import ScreenerConfig, ScreenerResult, screen_batch, screen_stock
from app.services.stock_analysis_service import analyze_stock
from app.services.telegram_service import _post_flow as _send_markdown
from app.services.telegram_stock_formatter import format_for_telegram

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["Stock Fundamentals"])


# ── Request / Response helpers ────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    data: RawStockData
    dcf_config: Optional[DCFConfig] = None


class BatchScreenRequest(BaseModel):
    stocks: List[RawStockData]
    screener_config: Optional[ScreenerConfig] = None
    dcf_config: Optional[DCFConfig] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/{ticker}", response_model=StockAnalysis, summary="Live fundamental analysis via yfinance")
async def get_stock_fundamentals(
    ticker: str,
    dcf_method: str = Query("conservative", description="conservative | historical_average | capped_growth"),
    send_telegram: bool = Query(False),
):
    """
    Fetch financial data from Yahoo Finance, run the full DCF + scoring
    pipeline, and return a StockAnalysis object. Called directly by the
    dashboard — no manual data input needed.
    """
    t = ticker.upper()
    try:
        raw = await fetch_raw_stock_data(t)
        dcf_config = DCFConfig(growth_method=dcf_method)
        result = analyze_stock(raw, dcf_config)

        if send_telegram:
            message = format_for_telegram(result)
            await _send_markdown(message)

        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Live fundamentals failed for %s: %s", t, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")


@router.post("/{ticker}/analyze", response_model=StockAnalysis, summary="Full fundamental analysis")
async def analyze_stock_route(
    ticker: str,
    body: AnalyzeRequest,
    send_telegram: bool = Query(False, description="Push result to Telegram after analysis"),
):
    """
    Run full DCF + scoring analysis on a stock.
    Optionally push formatted result to Telegram.
    """
    try:
        body.data.ticker = ticker.upper()
        result = analyze_stock(body.data, body.dcf_config)

        if send_telegram:
            message = format_for_telegram(result)
            await _send_markdown(message)

        return result
    except Exception as exc:
        logger.error("Stock analysis failed for %s: %s", ticker, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/{ticker}/telegram",
    response_model=dict,
    summary="Analyze stock and push to Telegram",
)
async def push_stock_to_telegram(ticker: str, body: AnalyzeRequest):
    """Analyze + immediately push formatted message to Telegram."""
    try:
        body.data.ticker = ticker.upper()
        result = analyze_stock(body.data, body.dcf_config)
        message = format_for_telegram(result)
        sent = await _send_markdown(message)
        return {
            "status": "sent" if sent else "telegram_error",
            "ticker": ticker.upper(),
            "verdict": result.verdict,
            "score": result.score.score.total,
        }
    except Exception as exc:
        logger.error("Telegram push failed for %s: %s", ticker, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/screen",
    response_model=List[ScreenerResult],
    summary="Screen a list of stocks for low-hanging-fruit setups",
)
async def screen_stocks(body: BatchScreenRequest):
    """
    Analyze a batch of stocks and return only those passing all screener rules.
    Results are sorted by DCF upside descending.
    """
    try:
        analyses = [
            analyze_stock(stock_data, body.dcf_config)
            for stock_data in body.stocks
        ]
        passing = screen_batch(analyses, body.screener_config)
        return passing
    except Exception as exc:
        logger.error("Batch screener error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
