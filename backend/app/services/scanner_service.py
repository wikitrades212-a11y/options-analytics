"""
Multi-ticker unusual options scanner — Tradable Flow Edition.

Scans a configurable list of tickers concurrently, filters contracts by
threshold, applies quality suppression, deduplicates clusters, and enforces
a per-contract cooldown before dispatching Telegram alerts.

Quality gate (Telegram):
  contract_class == "actionable"
  conviction_grade A  OR  (conviction_grade B AND conviction_score >= 60)

Cooldown:
  Same contract (ticker + expiry + type + strike) is suppressed for
  COOLDOWN_MINUTES unless unusual_score or premium changes materially.

Cluster grouping:
  Multiple alerts with same ticker + expiry + option_type are merged into
  one summary alert (best scorer is the representative).
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

# Module-level state
_last_result: Optional[dict] = None
_scheduler_task: Optional[asyncio.Task] = None

# ── Cooldown ──────────────────────────────────────────────────────────────────
COOLDOWN_MINUTES      = 60     # suppress same contract for 60 min
COOLDOWN_SCORE_DELTA  = 10.0   # resend if score changes by this many points
COOLDOWN_PREMIUM_PCT  = 0.25   # resend if premium changes by 25%+

# key: "{ticker}:{expiry}:{type}:{strike}" → {last_sent, score, premium}
_cooldown: dict[str, dict] = {}


def _cooldown_key(c: OptionContract) -> str:
    return f"{c.ticker}:{c.expiration}:{c.option_type}:{c.strike:.0f}"


def _is_cooled(c: OptionContract) -> bool:
    """Return True if this contract is still within its cooldown window."""
    key = _cooldown_key(c)
    if key not in _cooldown:
        return False
    entry = _cooldown[key]
    elapsed_min = (datetime.utcnow() - entry["last_sent"]).total_seconds() / 60
    if elapsed_min >= COOLDOWN_MINUTES:
        return False
    # Allow resend on material score or premium change
    score_change   = abs(c.unusual_score - entry["score"])
    premium_change = abs(c.vol_notional - entry["premium"]) / max(entry["premium"], 1)
    if score_change >= COOLDOWN_SCORE_DELTA or premium_change >= COOLDOWN_PREMIUM_PCT:
        return False
    return True


def _mark_sent(c: OptionContract) -> None:
    _cooldown[_cooldown_key(c)] = {
        "last_sent": datetime.utcnow(),
        "score":     c.unusual_score,
        "premium":   c.vol_notional,
    }


# ── Quality gate ──────────────────────────────────────────────────────────────

def _is_sendable(c: OptionContract) -> bool:
    """Only send actionable contracts with A or strong-B conviction."""
    if c.contract_class != "actionable":
        return False
    if c.conviction_grade == "A":
        return True
    if c.conviction_grade == "B" and c.conviction_score >= 60:
        return True
    return False


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


# ── Cluster grouping ──────────────────────────────────────────────────────────

def _group_alerts(alerts: list[dict]) -> list[dict]:
    """
    Merge same ticker + expiry + option_type into one alert.
    The highest-scoring contract is the representative; cluster metadata
    is added when 2+ contracts share the same expiry and direction.
    """
    groups: dict[str, list[dict]] = {}
    for a in alerts:
        c = a["contract"]
        key = f"{c.ticker}:{c.expiration}:{c.option_type}"
        groups.setdefault(key, []).append(a)

    result: list[dict] = []
    for group in groups.values():
        group.sort(key=lambda a: a["contract"].unusual_score, reverse=True)
        best = dict(group[0])  # shallow copy so we don't mutate cached contract
        if len(group) > 1:
            best["cluster_count"]   = len(group)
            best["cluster_strikes"] = sorted(a["contract"].strike for a in group)
        result.append(best)

    return sorted(result, key=lambda a: a["contract"].unusual_score, reverse=True)


# ── Core scan ─────────────────────────────────────────────────────────────────

async def run_scan() -> dict:
    """
    Scan all configured tickers. Applies quality gate, cooldown, and cluster
    grouping before returning the final alert list.
    """
    tickers     = [t.strip().upper() for t in settings.scan_tickers.split(",") if t.strip()]
    min_score   = settings.scan_min_score
    min_premium = settings.scan_min_premium
    min_volume  = settings.scan_min_volume
    top_n       = settings.scan_top_n
    FINAL_CAP   = 15

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

            # Defense-in-depth OI floor (stale cache guard)
            after_oi = [c for c in candidates if c.open_interest >= ENGINE_MIN_OI]

            # Score / premium / volume gate
            after_threshold = [
                c for c in after_oi
                if c.unusual_score >= min_score
                and c.vol_notional  >= min_premium
                and c.volume        >= min_volume
            ]

            # Quality gate: actionable + conviction A/strong-B only
            after_quality = [c for c in after_threshold if _is_sendable(c)]

            logger.info(
                "[%s] candidates=%d  after_oi=%d  after_threshold=%d  "
                "after_quality=%d  taking_top=%d",
                ticker,
                len(candidates), len(after_oi), len(after_threshold),
                len(after_quality), top_n,
            )

            for c in after_quality[:top_n]:
                alerts.append({
                    "contract":         c,
                    "bias":             _bias(c),
                    "underlying_price": result.underlying_price,
                })

        except Exception as exc:
            logger.warning(f"Scan failed for {ticker}: {exc}")
            failed.append(ticker)

    await asyncio.gather(*[_scan_one(t) for t in tickers])

    # Sort by score, apply global cap
    alerts.sort(key=lambda a: a["contract"].unusual_score, reverse=True)
    alerts = alerts[:FINAL_CAP]

    # Apply cooldown filter
    alerts_before_cooldown = len(alerts)
    alerts = [a for a in alerts if not _is_cooled(a["contract"])]
    logger.info(
        "run_scan cooldown: %d → %d (suppressed %d)",
        alerts_before_cooldown, len(alerts),
        alerts_before_cooldown - len(alerts),
    )

    # Group duplicate strike clusters
    alerts = _group_alerts(alerts)

    # Mark all surviving contracts as sent
    for a in alerts:
        _mark_sent(a["contract"])

    logger.info("run_scan DONE: %d final alerts", len(alerts))

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
