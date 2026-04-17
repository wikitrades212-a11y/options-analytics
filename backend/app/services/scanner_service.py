"""
Multi-ticker unusual options scanner — Tradable Flow Edition.

Scans a configurable list of tickers concurrently, filters contracts by
threshold, applies quality suppression, deduplicates clusters, and enforces
a per-contract cooldown before dispatching Telegram alerts.

Quality gate (Telegram):
  contract_class == "actionable"
  conviction_grade A  OR  (conviction_grade B AND conviction_score >= 60)

Cooldown (persistent SQLite):
  Same contract (ticker + expiry + type + strike + direction) is suppressed for
  COOLDOWN_MINUTES unless unusual_score or premium changes materially.
  Direction is normalized (BULLISH/BEARISH/HEDGE/SPECULATIVE) so tag-driven
  intensity changes don't bypass cooldown. Stored in dedup.db — survives restarts.

Cluster grouping:
  Multiple alerts with same ticker + expiry + option_type are merged into
  one summary alert (best scorer is the representative).
"""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.models.options import OptionContract
from app.services.options_service import get_unusual_options
from app.services.trending_service import get_trending_tickers
from app.services.unusual_engine import MIN_OI as ENGINE_MIN_OI

logger = logging.getLogger(__name__)

# Module-level state
_last_result: Optional[dict] = None
_scheduler_task: Optional[asyncio.Task] = None

# ── Persistent cooldown (SQLite) ───────────────────────────────────────────────
#
# Replaces the in-memory dict so cooldown survives process restarts and
# hot-reloads. Set DEDUP_DB_PATH to an absolute path on a persistent volume
# (e.g. a Railway volume mount) for full cross-deploy persistence.
#
COOLDOWN_MINUTES     = 60     # suppress same contract for 60 min
COOLDOWN_SCORE_DELTA = 10.0   # resend if score changes by this many points
COOLDOWN_PREMIUM_PCT = 0.25   # resend if premium changes by 25%+

_DEDUP_DB_ENV = os.getenv("DEDUP_DB_PATH", "")
if _DEDUP_DB_ENV:
    _DB_PATH = Path(_DEDUP_DB_ENV).resolve()
else:
    _DB_PATH = Path("./dedup.db").resolve()
    # Emit at module-load time so it appears in startup logs. On Railway this
    # means cooldown state is lost on every redeploy. Mount a volume and set
    # DEDUP_DB_PATH=/data/dedup.db (or equivalent) for true persistence.
    import warnings
    warnings.warn(
        "DEDUP_DB_PATH is not set — using ephemeral path ./dedup.db. "
        "Cooldown state will be lost on redeploy. "
        "Set DEDUP_DB_PATH to a path on a Railway persistent volume "
        "(e.g. DEDUP_DB_PATH=/data/dedup.db) to fix this.",
        RuntimeWarning,
        stacklevel=1,
    )


def _db_connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_cooldown (
            key          TEXT PRIMARY KEY,
            last_sent_ts REAL NOT NULL,
            score        REAL NOT NULL DEFAULT 0,
            premium      REAL NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _normalize_direction(bias: str) -> str:
    """
    Collapse the raw bias string down to its core direction for use in the
    cooldown key.  We intentionally ignore intensity (AGGRESSIVE) because
    that is tag-driven and can fluctuate between scans for the same contract.
    Using the full bias string would let the same contract slip through the
    cooldown just because a tag like "High Vol/OI" appeared or disappeared.

    Direction is what matters:
      BULLISH AGGRESSIVE  → BULLISH
      BEARISH AGGRESSIVE  → BEARISH
      HEDGE / PROTECTION  → HEDGE
      SPECULATIVE         → SPECULATIVE
      anything else       → NEUTRAL
    """
    b = bias.upper()
    if "BULLISH" in b:   return "BULLISH"
    if "BEARISH" in b:   return "BEARISH"
    if "HEDGE" in b:     return "HEDGE"
    if "SPECULATIVE" in b: return "SPECULATIVE"
    return "NEUTRAL"


def _cooldown_key(c: OptionContract, sentiment: str) -> str:
    """
    Persistent dedup key.
    Direction is normalized so intensity fluctuations (BEARISH vs BEARISH
    AGGRESSIVE) do not bypass cooldown. A genuine directional flip
    (BULLISH → BEARISH) still produces a different key and will resend.
    """
    direction = _normalize_direction(sentiment)
    return f"{c.ticker}:{c.expiration}:{c.option_type}:{c.strike:.0f}:{direction}"


def _is_cooled(c: OptionContract, sentiment: str) -> bool:
    """Return True if this contract+sentiment is still within its cooldown window."""
    key    = _cooldown_key(c, sentiment)
    cutoff = time.time() - COOLDOWN_MINUTES * 60
    try:
        with _db_connect() as conn:
            row = conn.execute(
                "SELECT last_sent_ts, score, premium FROM alert_cooldown WHERE key = ?",
                (key,),
            ).fetchone()
    except Exception as exc:
        logger.warning("cooldown DB read error (treating as not cooled): %s", exc)
        return False

    if row is None:
        return False

    last_sent_ts, prev_score, prev_premium = row
    if last_sent_ts < cutoff:
        return False  # TTL expired — allow resend

    # Allow resend on material score or premium change even within TTL
    if abs(c.unusual_score - prev_score) >= COOLDOWN_SCORE_DELTA:
        return False
    if abs(c.vol_notional - prev_premium) / max(prev_premium, 1) >= COOLDOWN_PREMIUM_PCT:
        return False

    return True


def _mark_sent(c: OptionContract, sentiment: str) -> None:
    key = _cooldown_key(c, sentiment)
    try:
        with _db_connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_cooldown (key, last_sent_ts, score, premium)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    last_sent_ts = excluded.last_sent_ts,
                    score        = excluded.score,
                    premium      = excluded.premium
                """,
                (key, time.time(), c.unusual_score, c.vol_notional),
            )
            conn.commit()
            # Prune entries older than 2× TTL to keep the DB small
            conn.execute(
                "DELETE FROM alert_cooldown WHERE last_sent_ts < ?",
                (time.time() - COOLDOWN_MINUTES * 60 * 2,),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("cooldown DB write error (alert will not be throttled): %s", exc)


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
    min_score   = settings.scan_min_score
    min_premium = settings.scan_min_premium
    min_volume  = settings.scan_min_volume
    top_n       = settings.scan_top_n
    FINAL_CAP   = 15

    # ── Base tickers (always included) ───────────────────────────────────────
    base_tickers = [t.strip().upper() for t in settings.scan_tickers.split(",") if t.strip()]

    # ── Trending tickers (augment, never replace base) ────────────────────────
    TRENDING_CAP = 10
    try:
        trending_raw = await get_trending_tickers(limit=TRENDING_CAP)
    except Exception as exc:
        logger.warning("run_scan: trending fetch error: %s", exc)
        trending_raw = []

    base_set       = set(base_tickers)
    trending_added = [t for t in trending_raw if t not in base_set][:TRENDING_CAP]
    tickers        = base_tickers + trending_added

    logger.info(
        "run_scan START — base=%d  trending_added=%d  total=%d  "
        "tickers=%s  min_score=%.1f  min_premium=%.0f  "
        "min_volume=%d  top_n=%d  FINAL_CAP=%d  ENGINE_MIN_OI=%d",
        len(base_tickers), len(trending_added), len(tickers),
        tickers, min_score, min_premium, min_volume, top_n, FINAL_CAP, ENGINE_MIN_OI,
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

    # Apply cooldown filter (persistent SQLite — survives restarts)
    alerts_before_cooldown = len(alerts)
    alerts = [a for a in alerts if not _is_cooled(a["contract"], a["bias"])]
    logger.info(
        "run_scan cooldown: %d → %d (suppressed %d)",
        alerts_before_cooldown, len(alerts),
        alerts_before_cooldown - len(alerts),
    )

    # Group duplicate strike clusters
    alerts = _group_alerts(alerts)

    # Mark all surviving contracts as sent (written to SQLite)
    for a in alerts:
        _mark_sent(a["contract"], a["bias"])

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


# ── Weekday delivery schedule (America/New_York) ──────────────────────────────

_ET = ZoneInfo("America/New_York")

# One premarket run + hourly from market open through 4:30 PM ET (Mon–Fri only)
_SCAN_TIMES: list[dtime] = [
    dtime(8, 30),                                                 # premarket
    dtime(9, 30),  dtime(10, 30), dtime(11, 30), dtime(12, 30),  # morning
    dtime(13, 30), dtime(14, 30), dtime(15, 30), dtime(16, 30),  # afternoon
]


def _next_scan_dt() -> datetime:
    """Return the next scheduled scan datetime (ET-aware, weekdays only)."""
    now = datetime.now(_ET)
    today = now.date()

    # Try remaining slots today if it's a weekday
    if today.weekday() < 5:  # 0=Mon … 4=Fri
        for t in _SCAN_TIMES:
            cand = datetime(today.year, today.month, today.day, t.hour, t.minute, tzinfo=_ET)
            if cand > now:
                return cand

    # Walk forward to find the next weekday, use 8:30 AM
    for delta in range(1, 8):
        d = today + timedelta(days=delta)
        if d.weekday() < 5:
            t = _SCAN_TIMES[0]
            return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=_ET)

    raise RuntimeError("Could not compute next scan time")


# ── Background scheduler ──────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    from app.services.telegram_service import send_scan_summary  # noqa: PLC0415
    from app.services.social_service import PostType, queue_scan_result  # noqa: PLC0415

    logger.info("Scanner scheduler started — weekdays 8:30 AM + hourly 9:30–4:30 PM ET")

    while True:
        now_et = datetime.now(_ET)
        next_dt = _next_scan_dt()
        sleep_secs = (next_dt - now_et).total_seconds()

        logger.info(
            "[scheduler] now=%s ET  |  next=%s ET  (in %.1f min)",
            now_et.strftime("%Y-%m-%d %H:%M:%S"),
            next_dt.strftime("%Y-%m-%d %H:%M"),
            sleep_secs / 60,
        )

        try:
            await asyncio.sleep(max(sleep_secs, 0))
        except asyncio.CancelledError:
            raise

        wake_et = datetime.now(_ET)
        logger.info(
            "[scheduler] woke at %s ET — executing slot %s ET",
            wake_et.strftime("%Y-%m-%d %H:%M:%S"),
            next_dt.strftime("%H:%M"),
        )

        try:
            result = await run_scan()
            _store_result(result)
            logger.info(
                "[scheduler] scan done — %d alerts across %d tickers",
                len(result["alerts"]),
                len(result["tickers_scanned"]),
            )
            await send_scan_summary(result)

            # ── Credit Spread Engine (runs after flow scan) ───────────────────
            try:
                from app.services.credit_spread_engine import run_spread_scan  # noqa: PLC0415
                from app.services.telegram_service import send_spread_alerts   # noqa: PLC0415
                from app.routers.credit_spread import _store_spread_result     # noqa: PLC0415

                spread_result = await run_spread_scan(result)
                spread_result["scanned_at"] = result["scanned_at"].isoformat()
                spread_result["tickers_scanned"] = result["tickers_scanned"]
                _store_spread_result(spread_result)

                if spread_result["spreads"]:
                    logger.info(
                        "[scheduler] spread engine: %d valid trade(s) found",
                        len(spread_result["spreads"]),
                    )
                    await send_spread_alerts(spread_result["spreads"])
                else:
                    logger.info("[scheduler] spread engine: no valid setups this cycle")
            except Exception as spread_exc:
                logger.error("[scheduler] spread engine error: %s", spread_exc, exc_info=True)

            # Social automation — additive, does not affect Telegram flow
            if next_dt.hour == 8:
                social_type = PostType.PREMARKET
            elif next_dt.hour == 16:
                social_type = PostType.EOD_RECAP
            else:
                social_type = PostType.LIVE_UPDATE
            queue_scan_result(result, social_type)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[scheduler] scan error: %s", exc, exc_info=True)


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


# ── FBA daily scheduler (6 AM ET every day) ───────────────────────────────────

_fba_scheduler_task: Optional[asyncio.Task] = None


def _next_fba_dt() -> datetime:
    """Next 6:00 AM ET — runs every day (not just weekdays)."""
    now = datetime.now(_ET)
    cand = datetime(now.year, now.month, now.day, 6, 0, tzinfo=_ET)
    if cand > now:
        return cand
    return cand + timedelta(days=1)


async def _fba_scheduler_loop() -> None:
    logger.info("FBA scheduler started — daily 6:00 AM ET")

    while True:
        now_et  = datetime.now(_ET)
        next_dt = _next_fba_dt()
        sleep_secs = (next_dt - now_et).total_seconds()

        logger.info("[fba-scheduler] next run: %s ET (in %.1f min)", next_dt.strftime("%Y-%m-%d %H:%M"), sleep_secs / 60)

        try:
            await asyncio.sleep(max(sleep_secs, 0))
        except asyncio.CancelledError:
            raise

        try:
            from app.services.fba_service import run_fba_scan, send_fba_alerts  # noqa: PLC0415
            result = await run_fba_scan(include_trends=True, min_score=50.0, top_n=15)
            logger.info("[fba-scheduler] done — %d high, %d medium", result["total_high"], result["total_medium"])
            if result["high"]:
                await send_fba_alerts(result["top_products"], top_n=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[fba-scheduler] error: %s", exc, exc_info=True)


def start_fba_scheduler() -> None:
    global _fba_scheduler_task
    if _fba_scheduler_task is None or _fba_scheduler_task.done():
        _fba_scheduler_task = asyncio.create_task(_fba_scheduler_loop())
        logger.info("FBA scheduler task created.")


def stop_fba_scheduler() -> None:
    global _fba_scheduler_task
    if _fba_scheduler_task and not _fba_scheduler_task.done():
        _fba_scheduler_task.cancel()
        logger.info("FBA scheduler task cancelled.")
