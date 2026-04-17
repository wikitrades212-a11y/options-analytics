"""
FBA product discovery endpoints.

GET /fba/scan      — run full scrape + score pipeline
GET /fba/status    — last completed scan result
GET /fba/top       — top N high-opportunity products
GET /fba/product/{asin} — single ASIN details from last scan
"""
import logging

from fastapi import APIRouter, Query

from app.services.fba_service import run_fba_scan, get_last_fba_scan, send_fba_alerts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fba", tags=["fba"])


@router.get("/scan", summary="Run FBA product discovery scan")
async def fba_scan(
    notify:          bool  = Query(False, description="Send top results to Telegram"),
    include_trends:  bool  = Query(True,  description="Fetch Google Trends data"),
    include_movers:  bool  = Query(True,  description="Include Movers & Shakers"),
    min_score:       float = Query(50.0,  description="Minimum composite score to include"),
    top_n:           int   = Query(20,    description="Max results per tier"),
):
    result = await run_fba_scan(
        include_movers=include_movers,
        include_trends=include_trends,
        min_score=min_score,
        top_n=top_n,
    )

    if notify and result["top_products"]:
        await send_fba_alerts(result["top_products"])

    return result


@router.get("/status", summary="Last FBA scan result")
async def fba_status():
    result = get_last_fba_scan()
    if result is None:
        return {"status": "no fba scan run yet"}
    return {
        "scanned_at":   result["scanned_at"],
        "total_scraped": result["total_scraped"],
        "total_high":   result["total_high"],
        "total_medium": result["total_medium"],
    }


@router.get("/top", summary="Top high-opportunity products from last scan")
async def fba_top(n: int = Query(10, description="Number of products to return")):
    result = get_last_fba_scan()
    if result is None:
        return {"status": "no fba scan run yet", "products": []}
    return {
        "scanned_at": result["scanned_at"],
        "products":   result["top_products"][:n],
    }


@router.get("/product/{asin}", summary="Details for a specific ASIN from last scan")
async def fba_product(asin: str):
    result = get_last_fba_scan()
    if result is None:
        return {"status": "no fba scan run yet"}
    asin = asin.upper().strip()
    for p in result.get("top_products", []):
        if p.get("asin") == asin:
            return p
    return {"status": "not found", "asin": asin}
