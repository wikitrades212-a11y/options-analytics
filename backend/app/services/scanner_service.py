"""
Multi-ticker unusual options scanner.

Scans a configurable list of tickers concurrently, filters contracts by
threshold, and returns the top unusual contracts across all tickers.
State is held at module level so the scheduler and HTTP endpoints share it.
"""
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.models.options import OptionContract
from app.services.options_service import get_unusual_options
from app.services.unusual_engine import MIN_OI as ENGINE_MIN_OI

logger = logging.getLogger(__name__)

# Module-level last scan result — shared by router and scheduler
_last_result: Optional[dict] = None
_scheduler_task: Optional[asyncio.Task] = None


# ── Bias inference ────────────────────────────────────────────────────────────

def _bias(contract: OptionContract) -> str:
    tags = set(contract.reason_tags)
    if contract.option_type == "call":
        if "Far OTM Lottery" in tags:
            return "SPECULATIVE"
        if "Near ATM Aggression" in tags and "Big Premium" in tags:
            return "BULLISH AGGRESSIVE"
        if "Big Premium" in tags or "Call Dominance" in tags:
            return "BULLISH"
        return "BULLISH"
    else:
        if "Put Hedge" in tags:
            return "HEDGE / PROTECTION"
        if "Big Premium" in tags and "High Vol/OI" in tags:
            return "BEARISH AGGRESSIVE"
        if "Big Premium" in tags or "High Vol/OI" in tags:
            return "BEARISH"
        return "BEARISH"


# ── Core scan ─────────────────────────────────────────────────────────────────

async def run_scan() -> dict:
    """
    Scan all configured tickers.  Returns a result dict ready for the router
    and Telegram dispatcher.
    """
    tickers = [t.strip().upper() for t in settings.scan_tickers.split(",") if t.strip()]
    min_score   = settings.scan_min_score
    min_premium = settings.scan_min_premium
    min_volume  = settings.scan_min_volume
    top_n       = settings.scan_top_n
    # Global cap across all tickers — prevents N_tickers × top_n bloat
    FINAL_CAP = 15

    logger.info(
        "run_scan START — tickers=%d  min_score=%.1f  min_premium=%.0f  "
        "min_volume=%d  top_n=%d  FINAL_CAP=%d  ENGINE_MIN_OI=%d",
        len(tickers), min_score, min_premium, min_volume, top_n, FINAL_CAP, ENGINE_MIN_OI,
    )

    scanned: List[str] = []
    failed:  List[str] = []
    alerts:  List[dict] = []

    async def _scan_one(ticker: str) -> None:
        try:
            result = await get_unusual_options(ticker)
            scanned.append(ticker)

            candidates = result.combined
            logger.info("[%s] combined candidates from cache/engine: %d", ticker, len(candidates))

            # Log OI distribution so we can verify ENGINE_MIN_OI is being respected
            oi_values = sorted(set(c.open_interest for c in candidates))
            logger.info("[%s] OI values in candidates: %s", ticker, oi_values[:20])

            # --- STAGE 1: OI floor (defense-in-depth; engine should already enforce this,
            #               but stale cache from a prior MIN_OI=1 deployment can bypass it)
            after_oi = [c for c in candidates if c.open_interest >= ENGINE_MIN_OI]
            logger.info(
                "[%s] after OI>=%d gate: %d (dropped %d low-OI)",
                ticker, ENGINE_MIN_OI, len(after_oi), len(candidates) - len(after_oi),
            )

            # --- STAGE 2: score / premium / volume gate
            after_prefilter = [
                c for c in after_oi
                if c.unusual_score >= min_score
                and c.vol_notional  >= min_premium
                and c.volume        >= min_volume
            ]
            logger.info(
                "[%s] after score/premium/volume gate: %d → taking top %d",
                ticker, len(after_prefilter), top_n,
            )

            # --- STAGE 3: per-ticker cap
            per_ticker = after_prefilter[:top_n]
            for c in per_ticker:
                alerts.append({
                    "contract":         c,
                    "bias":             _bias(c),
                    "underlying_price": result.underlying_price,
                })
        except Exception as exc:
            logger.warning(f"Scan failed for {ticker}: {exc}")
            failed.append(ticker)

    await asyncio.gather(*[_scan_one(t) for t in tickers])

    alerts.sort(key=lambda a: a["contract"].unusual_score, reverse=True)
    logger.info(
        "run_scan pre-cap: %d raw alerts across %d tickers → applying FINAL_CAP=%d",
        len(alerts), len(scanned), FINAL_CAP,
    )
    alerts = alerts[:FINAL_CAP]
    logger.info("run_scan DONE: %d alerts returned", len(alerts))

    return {
        "scanned_at":         datetime.utcnow(),
        "tickers_scanned":    scanned,
        "tickers_failed":     failed,
        "alerts":             alerts,
        "total_unusual_flow": round(sum(a["contract"].vol_notional for a in alerts), 2),
    }


# ── State accessors ───────────────────────────────────────────────────────────

def get_last_result() -> Optional[dict]:
    return _last_result


def _store_result(result: dict) -> None:
    global _last_result
    _last_result = result


# ── Background scheduler ──────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    """
    Runs indefinitely, sleeping SCAN_INTERVAL_MINUTES between scans.
    Import send_scan_summary lazily to avoid circular imports.
    """
    from app.services.telegram_service import send_scan_summary  # noqa: PLC0415

    interval = settings.scan_interval_minutes * 60
    logger.info(f"Scanner scheduler started — interval {settings.scan_interval_minutes}m")

    while True:
        await asyncio.sleep(interval)
        logger.info("Scheduled scan starting…")
        try:
            result = await run_scan()
            _store_result(result)
            logger.info(
                f"Scan complete — {len(result['alerts'])} alerts "
                f"across {len(result['tickers_scanned'])} tickers"
            )
            await send_scan_summary(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Scheduled scan error: {exc}")


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Scanner scheduler task created.")


def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        logger.info("Scanner scheduler task cancelled.")
