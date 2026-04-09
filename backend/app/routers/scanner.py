"""
Scanner endpoints.

GET /scanner/run     — trigger an immediate scan, optionally notify Telegram
GET /scanner/status  — return metadata from the last completed scan
"""
import logging
from fastapi import APIRouter, Query

from app.services.scanner_service import run_scan, get_last_result, _store_result


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scanner", tags=["scanner"])


@router.get("/run", summary="Trigger an immediate unusual options scan")
async def trigger_scan(notify: bool = Query(True, description="Send Telegram alerts")):
    result = await run_scan()
    _store_result(result)


    return {
        "scanned_at":         result["scanned_at"].isoformat(),
        "tickers_scanned":    result["tickers_scanned"],
        "tickers_failed":     result["tickers_failed"],
        "total_unusual_flow": result["total_unusual_flow"],
        "alert_count":        len(result["alerts"]),
        "alerts": [
            {**a["contract"].model_dump(), "bias": a["bias"]}
            for a in result["alerts"]
        ],
    }


@router.get("/status", summary="Metadata from the last completed scan")
async def scan_status():
    result = get_last_result()
    if result is None:
        return {"status": "no scan run yet"}

    return {
        "scanned_at":         result["scanned_at"].isoformat(),
        "tickers_scanned":    result["tickers_scanned"],
        "tickers_failed":     result["tickers_failed"],
        "total_unusual_flow": result["total_unusual_flow"],
        "alert_count":        len(result["alerts"]),
    }
