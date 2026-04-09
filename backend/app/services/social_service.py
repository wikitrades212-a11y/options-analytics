"""
Social automation service — delayed public distribution layer.

Sits downstream of the scanner and futures services. Receives scan results,
stores them in a delay queue, and publishes social-ready summaries on schedule.

Content tiers:
  Telegram (existing, unchanged) — real-time, full alerts
  Social media                   — delayed 15–30 min, summaries only

Post types:
  PREMARKET       — 8:30 AM ET weekdays, top 3 names
  LIVE_UPDATE     — intraday, only when strong cluster exists, top 1–2 names
  EOD_RECAP       — 4:30 PM ET weekdays, top 3 names + tomorrow bias
  SUNDAY_FUTURES  — Sunday 6 PM ET, ES/NQ gap summary

Platform abstraction:
  publish_post(platform, text)        — publish to one named platform
  publish_summary(post_type, content) — format + dispatch to all platforms

Platforms supported today:
  "log"     — dry-run, writes post to app log only
  "webhook" — generic HTTP POST (Buffer, Zapier, Make, X API proxy, etc.)

To add a new platform, register an async publisher in _PUBLISHERS.
"""
import asyncio
import logging
from datetime import datetime, date, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


# ── Post types ────────────────────────────────────────────────────────────────

class PostType(str, Enum):
    PREMARKET      = "premarket"
    LIVE_UPDATE    = "live_update"
    EOD_RECAP      = "eod_recap"
    SUNDAY_FUTURES = "sunday_futures"


# ── In-memory state ───────────────────────────────────────────────────────────

# Pending posts waiting for their delay window to elapse.
# Structure: list of {post_type, content, queued_at (UTC), delay_minutes, published}
_pending: list[dict] = []

# Published history for same-day deduplication. Pruned to last 24 h.
# Structure: list of {post_type, published_at (UTC), date_et, text}
_history: list[dict] = []

_social_scheduler_task: Optional[asyncio.Task] = None


# ── Config helpers ────────────────────────────────────────────────────────────

def _delay() -> int:
    """Clamp configured delay to [15, 60] minutes."""
    return max(15, min(settings.social_delay_minutes, 60))


def _platforms() -> list[str]:
    return [p.strip() for p in settings.social_platforms.split(",") if p.strip()]


def _max_names() -> int:
    return max(1, settings.social_max_names_per_post)


# ── Dedup helpers ─────────────────────────────────────────────────────────────

def _already_posted_today(post_type: PostType) -> bool:
    """True if this post_type was already published today (ET calendar day)."""
    today_et = datetime.now(_ET).date()
    return any(
        h["post_type"] == post_type and h["date_et"] == today_et
        for h in _history
    )


def _record_published(post_type: PostType, text: str) -> None:
    now_utc  = datetime.utcnow()
    today_et = datetime.now(_ET).date()
    _history.append({
        "post_type":    post_type,
        "published_at": now_utc,
        "date_et":      today_et,
        "text":         text,
    })
    # Keep only last 24 h
    cutoff = now_utc - timedelta(hours=24)
    _history[:] = [h for h in _history if h["published_at"] >= cutoff]


# ── Platform publishers ───────────────────────────────────────────────────────

async def _publish_log(text: str) -> bool:
    """Dry-run publisher: logs the formatted post without sending anywhere."""
    logger.info("SOCIAL [dry-run] ──────────────────────\n%s\n──────────────────────────────", text)
    return True


async def _publish_webhook(text: str) -> bool:
    """
    Generic webhook publisher.
    POST {"text": "..."} to social_webhook_url.
    Plug in Buffer / Zapier / Make / X API proxy by pointing this URL at them.
    """
    url = getattr(settings, "social_webhook_url", "")
    if not url:
        logger.warning("SOCIAL webhook URL not configured — skipping")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"text": text})
            r.raise_for_status()
            logger.info("SOCIAL webhook OK (%s)", r.status_code)
            return True
    except Exception as exc:
        logger.error("SOCIAL webhook error: %s", exc)
        return False


# Registry — add new platforms here without touching callers
_PUBLISHERS: dict[str, object] = {
    "log":     _publish_log,
    "webhook": _publish_webhook,
}


async def publish_post(platform: str, text: str) -> bool:
    """Publish text to a single named platform."""
    fn = _PUBLISHERS.get(platform)
    if fn is None:
        logger.warning("SOCIAL unknown platform '%s'", platform)
        return False
    return await fn(text)  # type: ignore[call-arg]


async def publish_summary(post_type: PostType, content: dict) -> None:
    """
    Format content into a social post and publish to all configured platforms.
    Records to history for same-day dedup.
    """
    if not settings.social_enabled:
        return

    text = _format(post_type, content)
    if not text:
        logger.debug("SOCIAL %s produced empty post — skipping", post_type)
        return

    for platform in _platforms():
        await publish_post(platform, text)

    _record_published(post_type, text)


# ── Formatters ────────────────────────────────────────────────────────────────

def _format(post_type: PostType, content: dict) -> str:
    if post_type == PostType.PREMARKET:
        return format_premarket_post(content["alerts"])
    if post_type == PostType.LIVE_UPDATE:
        return format_live_update(content["alerts"])
    if post_type == PostType.EOD_RECAP:
        return format_eod_recap(content["alerts"])
    if post_type == PostType.SUNDAY_FUTURES:
        return format_sunday_futures_post(content["es"], content["nq"])
    return ""


def _pick_top(alerts: list[dict], n: int) -> list[dict]:
    """Select top N alerts ranked by conviction_score then unusual_score."""
    return sorted(
        alerts,
        key=lambda a: (a["contract"].conviction_score, a["contract"].unusual_score),
        reverse=True,
    )[:n]


def _direction_phrase(bias: str, option_type: str) -> str:
    """Convert internal bias tag to a short, natural social phrase."""
    b = bias.upper()
    if "BULLISH AGGRESSIVE" in b:
        return "aggressive call flow"
    if "BULLISH" in b or "SPECULATIVE" in b:
        return "calls building" if option_type == "call" else "bullish positioning"
    if "BEARISH AGGRESSIVE" in b:
        return "aggressive put flow"
    if "BEARISH" in b:
        return "bearish positioning"
    if "HEDGE" in b or "PROTECTION" in b:
        return "put protection"
    return "notable flow"


def _overall_lean(alerts: list[dict]) -> str:
    """Infer dominant direction from a set of alerts."""
    calls = sum(1 for a in alerts if a["contract"].option_type == "call")
    puts  = len(alerts) - calls
    if calls > puts * 1.5:
        return "bullish"
    if puts > calls * 1.5:
        return "bearish"
    return "mixed"


def format_premarket_post(alerts: list[dict]) -> str:
    """
    8:30 AM ET weekday post.
    Top 3 actionable names, Twitter/X length, market commentary tone.

    Example:
        Premarket flow:
        NVDA calls building
        AMZN accumulation
        AMD bearish positioning
        Opening with a bullish lean.
    """
    top = _pick_top(alerts, _max_names())
    if not top:
        return ""

    lines = ["Premarket flow:"]
    for a in top:
        ticker = a["contract"].ticker
        phrase = _direction_phrase(a["bias"], a["contract"].option_type)
        lines.append(f"{ticker} {phrase}")

    lean = _overall_lean(top)
    lines.append(f"Opening with a {lean} lean.")
    return "\n".join(lines)


def format_live_update(alerts: list[dict]) -> str:
    """
    Intraday post — only when strong cluster qualifies.
    Top 1–2 names, brief and confident.

    Example:
        Flow still pressing into NVDA calls.
        This is where smart money is leaning today.
        Also watching AMD.
    """
    top = _pick_top(alerts, min(2, _max_names()))
    if not top:
        return ""

    best   = top[0]
    ticker = best["contract"].ticker
    otype  = best["contract"].option_type

    bias_line = _bias_sentence(best["bias"], ticker)

    lines = [
        f"Flow still pressing into {ticker} {otype}s.",
        bias_line,
    ]
    if len(top) > 1:
        lines.append(f"Also watching {top[1]['contract'].ticker}.")
    return "\n".join(lines)


def _bias_sentence(bias: str, ticker: str) -> str:
    b = bias.upper()
    if "BULLISH AGGRESSIVE" in b:
        return "This is where smart money is leaning today."
    if "BULLISH" in b or "SPECULATIVE" in b:
        return f"Upside bias holding in {ticker}."
    if "BEARISH AGGRESSIVE" in b:
        return "Aggressive put flow — protection or directional bet."
    if "BEARISH" in b:
        return f"Downside pressure building in {ticker}."
    return f"Watching {ticker} closely."


def format_eod_recap(alerts: list[dict]) -> str:
    """
    4:30 PM ET weekday post.
    Top 3 names, day summary, tomorrow bias.

    Example:
        Flow recap 9 Apr:
        NVDA — calls building
        AMZN — bullish positioning
        AMD — bearish positioning
        Bias into tomorrow: bullish.
    """
    top = _pick_top(alerts, _max_names())
    if not top:
        return ""

    date_str = datetime.now(_ET).strftime("%-d %b")
    lines = [f"Flow recap {date_str}:"]
    for a in top:
        ticker = a["contract"].ticker
        phrase = _direction_phrase(a["bias"], a["contract"].option_type)
        lines.append(f"{ticker} — {phrase}")

    lean = _overall_lean(top)
    lines.append(f"Bias into tomorrow: {lean}.")
    return "\n".join(lines)


def format_sunday_futures_post(es: dict, nq: dict) -> str:
    """
    Sunday 6 PM ET post.
    ES/NQ gap summary, social-friendly, no jargon.

    Example:
        ES ↑ +0.2%
        NQ ↑ +0.4%
        Both gapping up strong. Bullish Monday lean.
        Watching Sunday open into Monday.
    """
    es_sign = "+" if es["gap_pct"] >= 0 else ""
    nq_sign = "+" if nq["gap_pct"] >= 0 else ""
    bias    = _futures_bias_short(es, nq)

    return "\n".join([
        f"ES {es['arrow']} {es_sign}{es['gap_pct']:.1f}%",
        f"NQ {nq['arrow']} {nq_sign}{nq['gap_pct']:.1f}%",
        bias,
        "Watching Sunday open into Monday.",
    ])


def _futures_bias_short(es: dict, nq: dict) -> str:
    both_up   = es["direction"] == "UP"   and nq["direction"] == "UP"
    both_down = es["direction"] == "DOWN" and nq["direction"] == "DOWN"
    es_out    = not es["inside_range"]
    nq_out    = not nq["inside_range"]

    if both_up:
        if es_out and nq_out:
            return "Both gapping up strong. Bullish Monday lean."
        return "Modest gap up. Gap fill possible at open."
    if both_down:
        if es_out and nq_out:
            return "Both gapping down hard. Bearish Monday lean."
        return "Mild gap down. Watch for gap fill at open."
    return "Mixed signals. ES and NQ diverging — choppy open expected."


# ── Qualifying check for live updates ─────────────────────────────────────────

def _qualifies_for_live_update(alerts: list[dict]) -> bool:
    """
    Only post a live update on genuine cluster strength:
      - 3+ alerts in the scan, OR
      - at least one A-grade conviction alert with conviction_score >= 80
    """
    if len(alerts) >= 3:
        return True
    return any(
        a["contract"].conviction_grade == "A"
        and a["contract"].conviction_score >= 80
        for a in alerts
    )


# ── Queue management ──────────────────────────────────────────────────────────

def queue_scan_result(scan_result: dict, post_type: PostType) -> None:
    """
    Called from scanner_service after each scheduled scan.
    Enqueues the result for delayed social publication.

    post_type is determined by the caller based on scan schedule:
      8:30 AM scan   → PostType.PREMARKET
      4:30 PM scan   → PostType.EOD_RECAP
      Other scans    → PostType.LIVE_UPDATE (only if qualifying)
    """
    if not settings.social_enabled:
        return

    alerts = scan_result.get("alerts", [])
    if not alerts:
        logger.debug("SOCIAL queue_scan_result: no alerts — nothing to queue")
        return

    # Config gates
    if post_type == PostType.PREMARKET and not settings.social_premarket_enabled:
        return
    if post_type == PostType.EOD_RECAP and not settings.social_eod_enabled:
        return
    if post_type == PostType.LIVE_UPDATE:
        if not settings.social_live_update_enabled:
            return
        if not _qualifies_for_live_update(alerts):
            logger.debug("SOCIAL live update: cluster too weak — skipping")
            return

    # Same-day dedup for scheduled posts
    if post_type in (PostType.PREMARKET, PostType.EOD_RECAP):
        if _already_posted_today(post_type):
            logger.info("SOCIAL %s already posted today — skipping duplicate", post_type)
            return

    delay = _delay()
    _pending.append({
        "post_type":     post_type,
        "content":       {"alerts": alerts},
        "queued_at":     datetime.utcnow(),
        "delay_minutes": delay,
        "published":     False,
    })
    logger.info(
        "SOCIAL queued %s — %d alerts, publish after %d min delay",
        post_type, len(alerts), delay,
    )


def queue_futures_result(es: dict, nq: dict) -> None:
    """
    Called from futures_service after the Sunday gap report runs.
    Enqueues for delayed social publication.
    """
    if not settings.social_enabled or not settings.social_sunday_enabled:
        return

    if _already_posted_today(PostType.SUNDAY_FUTURES):
        logger.info("SOCIAL sunday_futures already posted today — skipping")
        return

    delay = _delay()
    _pending.append({
        "post_type":     PostType.SUNDAY_FUTURES,
        "content":       {"es": es, "nq": nq},
        "queued_at":     datetime.utcnow(),
        "delay_minutes": delay,
        "published":     False,
    })
    logger.info("SOCIAL queued sunday_futures — publish after %d min delay", delay)


# ── Publish pending ───────────────────────────────────────────────────────────

async def _publish_pending() -> None:
    """Publish any queued posts whose delay window has elapsed."""
    now = datetime.utcnow()
    for item in _pending:
        if item["published"]:
            continue
        elapsed_min = (now - item["queued_at"]).total_seconds() / 60
        if elapsed_min >= item["delay_minutes"]:
            logger.info(
                "SOCIAL publishing %s (queued %.1f min ago)",
                item["post_type"], elapsed_min,
            )
            await publish_summary(item["post_type"], item["content"])
            item["published"] = True


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _social_scheduler_loop() -> None:
    """
    Runs every 60 seconds.
    Publishes queued posts once their delay window elapses.
    Prunes stale entries from the pending list.
    """
    logger.info("Social scheduler started — checking queue every 60 s")

    while True:
        try:
            await asyncio.sleep(60)
            await _publish_pending()

            # Prune published entries older than 2 h to keep memory bounded
            cutoff = datetime.utcnow() - timedelta(hours=2)
            _pending[:] = [
                p for p in _pending
                if not p["published"] or p["queued_at"] >= cutoff
            ]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Social scheduler loop error: %s", exc)


def start_social_scheduler() -> None:
    global _social_scheduler_task
    if _social_scheduler_task is None or _social_scheduler_task.done():
        _social_scheduler_task = asyncio.create_task(_social_scheduler_loop())
        logger.info("Social scheduler task created.")


def stop_social_scheduler() -> None:
    global _social_scheduler_task
    if _social_scheduler_task and not _social_scheduler_task.done():
        _social_scheduler_task.cancel()
        logger.info("Social scheduler task cancelled.")
