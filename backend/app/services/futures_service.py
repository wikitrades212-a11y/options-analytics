"""
Sunday ES/NQ futures gap report.

Runs once every Sunday at 6:00 PM ET.
Fetches ES and NQ reopen prices, compares them to Friday's close/high/low,
and sends a concise Telegram summary with a Monday bias interpretation.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_REPORT_TIME = dtime(18, 0)  # 6:00 PM ET

_futures_scheduler_task: Optional[asyncio.Task] = None


# ── Schedule helper ───────────────────────────────────────────────────────────

def _next_sunday_report_dt() -> datetime:
    """Return the next Sunday 6:00 PM ET as a timezone-aware datetime."""
    now = datetime.now(_ET)
    today = now.date()

    # days until Sunday (weekday 6); 0 if already Sunday
    days_until = (6 - today.weekday()) % 7
    candidate_date = today + timedelta(days=days_until)
    candidate = datetime(
        candidate_date.year, candidate_date.month, candidate_date.day,
        _REPORT_TIME.hour, _REPORT_TIME.minute,
        tzinfo=_ET,
    )

    # If that time has already passed today (or we're past 6 PM on a Sunday), go +7 days
    if candidate <= now:
        candidate_date = candidate_date + timedelta(days=7)
        candidate = datetime(
            candidate_date.year, candidate_date.month, candidate_date.day,
            _REPORT_TIME.hour, _REPORT_TIME.minute,
            tzinfo=_ET,
        )

    return candidate


# ── Data fetching ─────────────────────────────────────────────────────────────

def _sync_fetch_gap_data(symbol: str) -> Optional[dict]:
    """
    Synchronous fetch (runs in thread executor).
    Returns Friday OHLC + Sunday reopen for the given continuous futures symbol.
    """
    import yfinance as yf  # noqa: PLC0415

    ticker = yf.Ticker(symbol)

    # Daily bars to find last Friday's OHLC
    daily = ticker.history(period="7d", interval="1d")
    if daily.empty:
        return None

    fridays = daily[daily.index.dayofweek == 4]
    if fridays.empty:
        logger.warning(f"{symbol}: no Friday bar found in last 7 days")
        return None

    friday_row = fridays.iloc[-1]

    # Current-session intraday (Sunday reopen)
    intraday = ticker.history(period="1d", interval="1m")
    if intraday.empty:
        logger.warning(f"{symbol}: no intraday data for today")
        return None

    return {
        "symbol":       symbol,
        "friday_close": float(friday_row["Close"]),
        "friday_high":  float(friday_row["High"]),
        "friday_low":   float(friday_row["Low"]),
        "reopen":       float(intraday.iloc[0]["Open"]),
        "current":      float(intraday.iloc[-1]["Close"]),
    }


async def _fetch_gap_data(symbol: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, _sync_fetch_gap_data, symbol)


# ── Gap analysis ──────────────────────────────────────────────────────────────

def _analyze_gap(data: dict) -> dict:
    gap = data["reopen"] - data["friday_close"]
    gap_pct = gap / data["friday_close"] * 100

    if gap > 0.5:
        direction, arrow = "UP", "↑"
    elif gap < -0.5:
        direction, arrow = "DOWN", "↓"
    else:
        direction, arrow = "FLAT", "→"

    inside = data["friday_low"] <= data["reopen"] <= data["friday_high"]

    return {
        **data,
        "gap":          gap,
        "gap_pct":      gap_pct,
        "direction":    direction,
        "arrow":        arrow,
        "inside_range": inside,
        "range_label":  "inside Friday range" if inside else "outside Friday range",
    }


def _bias_line(es: dict, nq: dict) -> str:
    """One-sentence Monday bias based on gap direction and range position."""
    both_up   = es["direction"] == "UP"   and nq["direction"] == "UP"
    both_down = es["direction"] == "DOWN" and nq["direction"] == "DOWN"
    es_out    = not es["inside_range"]
    nq_out    = not nq["inside_range"]

    if both_up:
        if es_out and nq_out:
            return "Both gap up outside Friday range → strong overnight demand, bullish lean for Monday open."
        return "Both gap up inside Friday range → modest overnight lift, watch for gap fill attempt at open."
    if both_down:
        if es_out and nq_out:
            return "Both gap down outside Friday range → strong overnight selling, bearish lean for Monday open."
        return "Both gap down inside Friday range → mild overnight selling, watch for gap fill attempt at open."
    return "Mixed signals — ES and NQ diverging, expect choppy / uncertain Monday open."


# ── Message formatting ────────────────────────────────────────────────────────

def format_futures_gap_message(es: dict, nq: dict) -> str:
    date_str = datetime.now(_ET).strftime("%a %-d %b")

    def _block(d: dict, label: str) -> str:
        sign = "+" if d["gap"] >= 0 else ""
        return (
            f"*{label}*\n"
            f"Fri Close: `{d['friday_close']:,.2f}`  "
            f"Hi: `{d['friday_high']:,.2f}`  Lo: `{d['friday_low']:,.2f}`\n"
            f"Reopen: `{d['reopen']:,.2f}`  "
            f"({sign}{d['gap']:,.2f} / {sign}{d['gap_pct']:.2f}%)\n"
            f"Gap: {d['arrow']} *{d['direction']}* · {d['range_label']}"
        )

    return (
        f"📊 *ES/NQ Futures Gap Report — {date_str} 6 PM ET*\n"
        + "─" * 30 + "\n"
        + _block(es, "ES  (/ES)") + "\n\n"
        + _block(nq, "NQ  (/NQ)") + "\n\n"
        + f"📌 *Monday Bias*\n{_bias_line(es, nq)}"
    )


# ── Report runner ─────────────────────────────────────────────────────────────

async def run_futures_gap_report() -> None:
    from app.services.telegram_service import _post  # noqa: PLC0415

    logger.info("Running Sunday futures gap report…")

    es_raw, nq_raw = await asyncio.gather(
        _fetch_gap_data("ES=F"),
        _fetch_gap_data("NQ=F"),
    )

    if es_raw is None or nq_raw is None:
        logger.warning("Futures data unavailable — skipping report")
        await _post("⚠️ *ES/NQ Futures Gap Report* — data unavailable at 6 PM ET.")
        return

    es  = _analyze_gap(es_raw)
    nq  = _analyze_gap(nq_raw)
    msg = format_futures_gap_message(es, nq)
    await _post(msg)
    logger.info("Futures gap report sent.")

    # Social automation — additive, does not affect Telegram flow
    from app.services.social_service import queue_futures_result  # noqa: PLC0415
    queue_futures_result(es, nq)


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _futures_scheduler_loop() -> None:
    logger.info("Futures scheduler started — Sundays 6:00 PM ET")

    while True:
        next_dt   = _next_sunday_report_dt()
        sleep_secs = (next_dt - datetime.now(_ET)).total_seconds()
        logger.info(
            "Next futures report at %s ET (in %.1f h)",
            next_dt.strftime("%Y-%m-%d %H:%M"),
            sleep_secs / 3600,
        )

        try:
            await asyncio.sleep(max(sleep_secs, 0))
        except asyncio.CancelledError:
            raise

        try:
            await run_futures_gap_report()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Futures gap report error: {exc}")


def start_futures_scheduler() -> None:
    global _futures_scheduler_task
    if _futures_scheduler_task is None or _futures_scheduler_task.done():
        _futures_scheduler_task = asyncio.create_task(_futures_scheduler_loop())
        logger.info("Futures scheduler task created.")


def stop_futures_scheduler() -> None:
    global _futures_scheduler_task
    if _futures_scheduler_task and not _futures_scheduler_task.done():
        _futures_scheduler_task.cancel()
        logger.info("Futures scheduler task cancelled.")
