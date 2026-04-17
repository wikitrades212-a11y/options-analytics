"""
FBA Product Scraper — zero-auth data sources.

Sources:
  1. Amazon Best Sellers RSS  (public, no key)
  2. Amazon Movers & Shakers  (public, scrape)
  3. Google Trends via pytrends (free, no key)

Returns raw product dicts ready for fba_scorer.
"""
import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Amazon categories to scan ──────────────────────────────────────────────────
# Best Sellers RSS exists for every department node.
# These are the most FBA-friendly categories.
BSR_CATEGORIES = {
    "home-garden":    "https://www.amazon.com/gp/rss/bestsellers/garden/ref=zg_bs_garden_rsslink",
    "kitchen":        "https://www.amazon.com/gp/rss/bestsellers/kitchen/ref=zg_bs_kitchen_rsslink",
    "sports":         "https://www.amazon.com/gp/rss/bestsellers/sporting-goods/ref=zg_bs_sporting-goods_rsslink",
    "pet-supplies":   "https://www.amazon.com/gp/rss/bestsellers/pet-supplies/ref=zg_bs_pet-supplies_rsslink",
    "beauty":         "https://www.amazon.com/gp/rss/bestsellers/beauty/ref=zg_bs_beauty_rsslink",
    "office":         "https://www.amazon.com/gp/rss/bestsellers/office-products/ref=zg_bs_office-products_rsslink",
    "toys":           "https://www.amazon.com/gp/rss/bestsellers/toys-and-games/ref=zg_bs_toys-and-games_rsslink",
    "baby":           "https://www.amazon.com/gp/rss/bestsellers/baby-products/ref=zg_bs_baby-products_rsslink",
    "health":         "https://www.amazon.com/gp/rss/bestsellers/hpc/ref=zg_bs_hpc_rsslink",
    "automotive":     "https://www.amazon.com/gp/rss/bestsellers/automotive/ref=zg_bs_automotive_rsslink",
}

MOVERS_CATEGORIES = [
    "kitchen", "garden", "sporting-goods", "pet-supplies",
    "beauty", "office-products", "toys-and-games", "baby-products",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_ASIN_RE  = re.compile(r"/dp/([A-Z0-9]{10})")
_PRICE_RE = re.compile(r"\$([0-9]+\.?[0-9]*)")
_RANK_RE  = re.compile(r"#([\d,]+)\s+in\s+(.+?)(?:\s*\(|$)", re.IGNORECASE)


# ── Amazon Best Sellers RSS ────────────────────────────────────────────────────

async def fetch_bsr_category(client: httpx.AsyncClient, category: str, url: str) -> list[dict]:
    """Parse one BSR RSS feed. Returns list of raw product dicts."""
    try:
        resp = await client.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("BSR fetch failed for %s: %s", category, exc)
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        logger.warning("BSR XML parse error for %s: %s", category, exc)
        return []

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    products = []

    for i, item in enumerate(root.iter("item")):
        title_el = item.find("title")
        link_el  = item.find("link")
        desc_el  = item.find("description")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link  = link_el.text.strip()  if link_el  is not None and link_el.text  else ""
        desc  = desc_el.text or ""

        asin_match  = _ASIN_RE.search(link)
        price_match = _PRICE_RE.search(desc)

        asin  = asin_match.group(1)  if asin_match  else None
        price = float(price_match.group(1)) if price_match else None

        if not asin or not title:
            continue

        products.append({
            "asin":       asin,
            "title":      title[:120],
            "category":   category,
            "bsr_rank":   i + 1,          # position in feed = rank
            "price":      price,
            "url":        f"https://www.amazon.com/dp/{asin}",
            "source":     "bsr",
        })

    return products


async def fetch_all_bsr(max_per_category: int = 20) -> list[dict]:
    """Fetch BSR for all configured categories concurrently."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [
            fetch_bsr_category(client, cat, url)
            for cat, url in BSR_CATEGORIES.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    products = []
    for res in results:
        if isinstance(res, list):
            products.extend(res[:max_per_category])

    logger.info("fetch_all_bsr: %d products across %d categories", len(products), len(BSR_CATEGORIES))
    return products


# ── Amazon Movers & Shakers ────────────────────────────────────────────────────

async def fetch_movers(client: httpx.AsyncClient, category: str) -> list[dict]:
    """
    Scrape Movers & Shakers page for one category.
    Returns products that are gaining BSR rank fastest.
    """
    url = f"https://www.amazon.com/gp/movers-and-shakers/{category}"
    try:
        resp = await client.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("Movers fetch failed for %s: %s", category, exc)
        return []

    asins  = _ASIN_RE.findall(html)
    # Extract percentage gain from page (e.g. "↑ 2,500%")
    gains  = re.findall(r"↑\s*([\d,]+)%", html)
    titles = re.findall(r'alt="([^"]{10,80})"', html)

    products = []
    for i, asin in enumerate(dict.fromkeys(asins)):  # dedupe, preserve order
        if len(products) >= 15:
            break
        gain = int(gains[i].replace(",", "")) if i < len(gains) else 0
        title = titles[i] if i < len(titles) else f"Product {asin}"
        products.append({
            "asin":       asin,
            "title":      title[:120],
            "category":   category,
            "bsr_rank":   i + 1,
            "bsr_gain_pct": gain,
            "price":      None,
            "url":        f"https://www.amazon.com/dp/{asin}",
            "source":     "movers",
        })

    return products


async def fetch_all_movers() -> list[dict]:
    """Fetch Movers & Shakers for all configured categories."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [fetch_movers(client, cat) for cat in MOVERS_CATEGORIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    products = []
    for res in results:
        if isinstance(res, list):
            products.extend(res)

    logger.info("fetch_all_movers: %d movers products", len(products))
    return products


# ── Google Trends ──────────────────────────────────────────────────────────────

def _extract_keywords(title: str) -> str:
    """Strip common Amazon filler words, return clean search keyword."""
    noise = [
        r"\[.*?\]", r"\(.*?\)", r"\d+ (pack|count|piece|oz|lb|ml|inch)",
        r"(premium|professional|heavy duty|high quality|best|new|upgraded)",
    ]
    result = title.lower()
    for pattern in noise:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    # Take first 3-4 meaningful words
    words = [w for w in result.split() if len(w) > 3][:4]
    return " ".join(words).strip()


def fetch_trends_batch(keywords: list[str]) -> dict[str, dict]:
    """
    Fetch Google Trends data for a list of keywords.
    Returns {keyword: {"interest_score": 0-100, "trend": "rising"|"stable"|"declining"}}

    pytrends compares keywords in groups of 5. We rotate through batches.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logger.warning("pytrends not installed — trends scoring disabled")
        return {}

    results: dict[str, dict] = {}
    batch_size = 5

    try:
        pt = TrendReq(hl="en-US", tz=360, timeout=(10, 25), retries=2, backoff_factor=0.5)

        for i in range(0, min(len(keywords), 25), batch_size):
            batch = keywords[i : i + batch_size]
            try:
                pt.build_payload(batch, timeframe="today 3-m", geo="US")
                df = pt.interest_over_time()

                if df.empty:
                    for kw in batch:
                        results[kw] = {"interest_score": 0, "trend": "unknown"}
                    continue

                for kw in batch:
                    if kw not in df.columns:
                        results[kw] = {"interest_score": 0, "trend": "unknown"}
                        continue

                    series     = df[kw].dropna()
                    latest     = float(series.iloc[-4:].mean())   # last ~4 weeks
                    earlier    = float(series.iloc[:8].mean())    # first ~8 weeks
                    peak       = float(series.max())

                    # Trend direction
                    if latest > earlier * 1.20:
                        trend = "rising"
                    elif latest < earlier * 0.80:
                        trend = "declining"
                    else:
                        trend = "stable"

                    results[kw] = {
                        "interest_score": round(latest),
                        "peak_score":     round(peak),
                        "trend":          trend,
                    }

            except Exception as exc:
                logger.warning("Trends batch error for %s: %s", batch, exc)
                for kw in batch:
                    results[kw] = {"interest_score": 0, "trend": "unknown"}

    except Exception as exc:
        logger.error("Trends init failed: %s", exc)

    return results


# ── Combined scrape ────────────────────────────────────────────────────────────

async def scrape_all(include_movers: bool = True) -> list[dict]:
    """
    Full scrape: BSR + optional Movers & Shakers.
    Deduplicates by ASIN. Enriches movers data into BSR results.
    """
    bsr_task    = fetch_all_bsr()
    movers_task = fetch_all_movers() if include_movers else asyncio.sleep(0, result=[])

    bsr_products, movers_products = await asyncio.gather(bsr_task, movers_task)

    # Index movers by ASIN for enrichment
    movers_by_asin = {p["asin"]: p for p in movers_products}

    # Dedupe BSR by ASIN, enrich with mover data
    seen: set[str] = set()
    final: list[dict] = []

    for p in bsr_products:
        asin = p["asin"]
        if asin in seen:
            continue
        seen.add(asin)
        if asin in movers_by_asin:
            p["bsr_gain_pct"] = movers_by_asin[asin].get("bsr_gain_pct", 0)
            p["is_mover"]     = True
        else:
            p["bsr_gain_pct"] = 0
            p["is_mover"]     = False
        final.append(p)

    # Add movers not in BSR feed
    for p in movers_products:
        if p["asin"] not in seen:
            seen.add(p["asin"])
            p["is_mover"] = True
            final.append(p)

    logger.info("scrape_all: %d unique products", len(final))
    return final
