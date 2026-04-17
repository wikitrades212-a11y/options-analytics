"""
Credit Spread endpoints.

GET /spreads/scan    — run a full spread scan (triggers scanner + spread engine)
GET /spreads/status  — return the last completed spread scan result
GET /spreads/ticker/{ticker} — generate a spread for a single ticker on demand
"""
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.models.credit_spread import SpreadScanResult
from app.services.scanner_service import run_scan, _store_result
from app.services.credit_spread_engine import run_spread_scan, generate_credit_spread
from app.services.telegram_service import send_spread_alerts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/spreads", tags=["credit-spreads"])

_last_spread_result: dict | None = None


def get_last_spread_result() -> dict | None:
    return _last_spread_result


def _store_spread_result(result: dict) -> None:
    global _last_spread_result
    _last_spread_result = result


@router.get("/scan", summary="Run full scanner + generate credit spread recommendations")
async def scan_spreads(
    notify: bool = Query(True, description="Send Telegram alerts for valid setups"),
    force: bool = Query(False, description="Force a new scan even if recent data exists"),
):
    from app.services.scanner_service import get_last_result
    from datetime import timezone
    import math

    # Reuse the last scan result if it's less than 10 minutes old and not forced.
    # This avoids the cooldown double-scan problem when calling /spreads/scan
    # shortly after /scanner/run.
    cached = get_last_result()
    if cached and not force:
        age_minutes = (
            datetime.now(timezone.utc) - cached["scanned_at"].replace(tzinfo=timezone.utc)
        ).total_seconds() / 60 if cached["scanned_at"].tzinfo is None else (
            datetime.now(timezone.utc) - cached["scanned_at"]
        ).total_seconds() / 60
        if age_minutes < 10 and cached.get("alerts"):
            scan = cached
        else:
            scan = await run_scan()
            _store_result(scan)
    else:
        scan = await run_scan()
        _store_result(scan)

    spread_result = await run_spread_scan(scan)
    spread_result["scanned_at"] = scan["scanned_at"].isoformat() if hasattr(scan["scanned_at"], "isoformat") else scan["scanned_at"]
    spread_result["tickers_scanned"] = scan["tickers_scanned"]
    _store_spread_result(spread_result)

    if notify and spread_result["spreads"]:
        await send_spread_alerts(spread_result["spreads"])

    return {
        "scanned_at":      spread_result["scanned_at"],
        "tickers_scanned": spread_result["tickers_scanned"],
        "total_valid":     spread_result["total_valid"],
        "total_lhf":       spread_result.get("total_lhf", 0),
        "spreads":         [s.model_dump() for s in spread_result["spreads"]],
        "rejected":        spread_result["rejected"],
    }


@router.get("/status", summary="Last completed spread scan result")
async def spread_status():
    result = get_last_spread_result()
    if result is None:
        return {"status": "no spread scan run yet"}

    return {
        "scanned_at":     result.get("scanned_at"),
        "tickers_scanned": result.get("tickers_scanned", []),
        "total_valid":    result.get("total_valid", 0),
        "spreads":        [s.model_dump() for s in result.get("spreads", [])],
        "rejected":       result.get("rejected", []),
    }


@router.get("/ticker/{ticker}", summary="Generate credit spread for a single ticker")
async def ticker_spread(
    ticker: str,
    notify: bool = Query(False, description="Send Telegram alert if valid"),
):
    from app.services.scanner_service import get_last_result
    from app.services.options_service import get_unusual_options

    ticker = ticker.upper().strip()

    # Pull existing scan alerts for this ticker (skip a full re-scan)
    last = get_last_result()
    alerts = []
    if last:
        alerts = [a for a in last.get("alerts", []) if a["contract"].ticker == ticker]

    # If no cached alerts, do a live unusual fetch to get underlying price at least
    underlying_price = 0.0
    if not alerts:
        try:
            unusual = await get_unusual_options(ticker)
            underlying_price = unusual.underlying_price
            alerts = [
                {"contract": c, "bias": "BULLISH" if c.option_type == "call" else "BEARISH",
                 "underlying_price": unusual.underlying_price}
                for c in unusual.combined[:3]
            ]
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch data for {ticker}: {exc}")

    if not alerts:
        return {"ticker": ticker, "verdict": "SKIP", "reason": "No unusual flow detected"}

    spread = await generate_credit_spread(ticker, alerts)
    if spread is None:
        return {"ticker": ticker, "verdict": "SKIP", "reason": "Chain data unavailable"}

    if notify and spread.verdict == "TAKE":
        await send_spread_alerts([spread])

    return spread.model_dump()
