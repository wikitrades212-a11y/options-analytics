"""
FBA Service — orchestrates scrape → trends → score → alert pipeline.

Daily schedule: 6:00 AM ET (configurable).
Results cached in memory; exposed via /fba endpoints and Telegram commands.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.services.fba_scraper import scrape_all, _extract_keywords, fetch_trends_batch
from app.services.fba_scorer import FBAProduct, score_all

logger = logging.getLogger(__name__)

# ── In-memory result cache ─────────────────────────────────────────────────────
_last_scan: Optional[dict] = None


def get_last_fba_scan() -> Optional[dict]:
    return _last_scan


def _store_fba_scan(result: dict) -> None:
    global _last_scan
    _last_scan = result


# ── Core pipeline ──────────────────────────────────────────────────────────────

async def run_fba_scan(
    include_movers: bool = True,
    include_trends: bool = True,
    min_score: float = 50.0,
    top_n: int = 20,
) -> dict:
    """
    Full FBA discovery pipeline.
    Returns dict with high/medium opportunity products + metadata.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("FBA scan starting")

    # Step 1: Scrape Amazon
    raw_products = await scrape_all(include_movers=include_movers)

    # Step 2: Google Trends (sync, run in executor to avoid blocking)
    trends: dict = {}
    if include_trends and raw_products:
        keywords = list({_extract_keywords(p["title"]) for p in raw_products if p.get("title")})
        keywords = [k for k in keywords if k][:50]  # cap at 50 keywords
        try:
            loop = asyncio.get_event_loop()
            trends = await loop.run_in_executor(None, fetch_trends_batch, keywords)
            logger.info("Trends fetched for %d keywords", len(trends))
        except Exception as exc:
            logger.warning("Trends fetch failed: %s", exc)

    # Step 3: Score all products
    scored = score_all(raw_products, trends=trends, min_score=min_score)

    high   = [p for p in scored if p.classification == "HIGH_OPPORTUNITY"][:top_n]
    medium = [p for p in scored if p.classification == "MEDIUM_OPPORTUNITY"][:top_n]

    result = {
        "scanned_at":     started_at.isoformat(),
        "total_scraped":  len(raw_products),
        "total_scored":   len(scored),
        "total_high":     len(high),
        "total_medium":   len(medium),
        "high":           [p.to_dict() for p in high],
        "medium":         [p.to_dict() for p in medium],
        "top_products":   [p.to_dict() for p in (high + medium)[:top_n]],
    }

    _store_fba_scan(result)
    logger.info(
        "FBA scan done: %d scraped, %d high, %d medium",
        len(raw_products), len(high), len(medium)
    )
    return result


# ── Telegram formatting ────────────────────────────────────────────────────────

def format_fba_alert(product: dict) -> str:
    """Format one FBA product as HTML for Telegram."""
    score   = product.get("score", {})
    total   = score.get("total", 0)
    title   = product.get("title", "Unknown")[:60]
    price   = product.get("price")
    rank    = product.get("bsr_rank", "?")
    cat     = product.get("category", "?").replace("-", " ").title()
    mover   = product.get("is_mover", False)
    gain    = product.get("bsr_gain_pct", 0)
    trend   = product.get("trend", "unknown")
    kw      = product.get("keyword", "")
    url     = product.get("url", "")
    cls     = product.get("classification", "")
    why     = product.get("why", [])
    flags   = product.get("flags", [])

    header = "🟢 HIGH OPPORTUNITY" if cls == "HIGH_OPPORTUNITY" else "🟡 MEDIUM OPPORTUNITY"
    price_str = f"${price:.2f}" if price else "Price N/A"
    gain_str  = f" ↑{gain}%" if gain > 0 else ""
    trend_icon = {"rising": "📈", "declining": "📉", "stable": "➡️"}.get(trend, "")
    mover_tag = " 🔥 MOVER" if mover else ""

    lines = [
        f"<b>{header}</b>{mover_tag}",
        f"<b>{title}</b>",
        f"",
        f"📦 {cat}  |  {price_str}  |  BSR #{rank}{gain_str}",
        f"🔍 Keyword: <code>{kw}</code>  {trend_icon}",
        f"",
        f"<b>Score: {total}/100</b>",
        f"  Demand: {score.get('demand',0)} | Competition: {score.get('competition',0)} | Margin: {score.get('margin',0)} | Logistics: {score.get('logistics',0)}",
    ]

    if why:
        lines.append("")
        lines.append("✅ " + " · ".join(why[:3]))

    if flags:
        lines.append("⚠️ " + " · ".join(flags[:2]))

    if url:
        lines.append(f"\n<a href=\"{url}\">View on Amazon</a>")

    return "\n".join(lines)


def format_fba_summary(result: dict) -> str:
    """Telegram summary message for a completed FBA scan."""
    top = result.get("top_products", [])[:5]
    lines = [
        f"<b>🛒 FBA PRODUCT SCAN</b>",
        f"Scraped {result['total_scraped']} products · {result['total_high']} high · {result['total_medium']} medium opportunity",
        "",
    ]
    for i, p in enumerate(top, 1):
        score = p.get("score", {}).get("total", 0)
        price = p.get("price")
        price_str = f"${price:.0f}" if price else "?"
        title = p.get("title", "?")[:45]
        cat   = p.get("category", "?")
        lines.append(f"{i}. <b>{title}</b> [{cat}] {price_str} — <b>{score}/100</b>")

    if not top:
        lines.append("No products above threshold.")

    return "\n".join(lines)


async def send_fba_alerts(products: list[dict], top_n: int = 5) -> None:
    """Send top FBA products to Telegram."""
    from app.services.telegram_service import _post

    for p in products[:top_n]:
        msg = format_fba_alert(p)
        await _post(msg)
        await asyncio.sleep(0.5)
