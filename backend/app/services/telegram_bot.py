"""
Telegram command bot — long-polling, HTML parse mode.

Commands:
  /scan     — run spread scan now + send alerts
  /status   — overview of last scan
  /ticker X — analyze one ticker on demand
  /easy     — show only LOW_HANGING_FRUIT setups
  /rejects  — recent rejected tickers + reasons
  /summary  — full scan narrative
  /perf     — recent trade performance from tracker
  /help     — command list

Security: only responds to messages from TELEGRAM_CHAT_ID.
Startup: call start_bot() inside FastAPI lifespan.
"""
import asyncio
import logging
from datetime import date
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_bot_task: Optional[asyncio.Task] = None


# ── Telegram API helpers ───────────────────────────────────────────────────────

def _base() -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def _send(chat_id: str | int, text: str) -> None:
    """Send HTML-formatted message. Silently swallows errors."""
    if not settings.telegram_bot_token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_base()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text[:4096],   # Telegram hard limit
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        logger.warning("bot._send error: %s", exc)


async def _get_updates(offset: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(
                f"{_base()}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                },
            )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as exc:
        logger.warning("bot.getUpdates error: %s", exc)
    return []


# ── Command implementations ────────────────────────────────────────────────────

async def _cmd_scan(args: list[str], chat_id: str) -> None:
    await _send(chat_id, "🔄 Running spread scan — please wait...")
    try:
        from app.services.scanner_service import run_scan, _store_result
        from app.services.credit_spread_engine import run_spread_scan
        from app.services.telegram_service import send_spread_alerts
        from app.routers.credit_spread import _store_spread_result

        scan   = await run_scan()
        _store_result(scan)

        result = await run_spread_scan(scan)
        result["scanned_at"]      = scan["scanned_at"].isoformat()
        result["tickers_scanned"] = scan["tickers_scanned"]
        _store_spread_result(result)

        n_lhf  = result.get("total_lhf", 0)
        n_tot  = result.get("total_valid", 0)
        n_scan = len(scan.get("tickers_scanned", []))

        if n_tot == 0:
            await _send(chat_id,
                f"⚪ Scan complete — {n_scan} tickers scanned.\n"
                "No valid spreads found this cycle."
            )
        else:
            await send_spread_alerts(result["spreads"])
            await _send(chat_id,
                f"✅ Scan complete — {n_scan} tickers scanned.\n"
                f"Valid spreads: {n_tot}  |  🍒 LHF: {n_lhf}\n"
                "Alerts sent above. /easy for LHF only."
            )
    except Exception as exc:
        logger.error("_cmd_scan error: %s", exc, exc_info=True)
        await _send(chat_id, f"❌ Scan failed: {exc}")


async def _cmd_status(args: list[str], chat_id: str) -> None:
    try:
        from app.routers.credit_spread import get_last_spread_result
        result = get_last_spread_result()
        if not result:
            await _send(chat_id, "⚪ No scan run yet.\n\nUse /scan to start.")
            return

        spreads    = result.get("spreads", [])
        rejected   = result.get("rejected", [])
        scanned_at = result.get("scanned_at", "unknown")
        tickers    = result.get("tickers_scanned", [])
        n_lhf      = result.get("total_lhf", 0)

        top_line = ""
        if spreads:
            best = spreads[0]
            lhf_label = ""
            if best.lhf:
                lhf_label = f" [{best.lhf.classification}]"
            top_line = (
                f"\n🏆 <b>Top Setup:</b> {best.ticker} {best.spread_type}"
                f" — {best.lhf.score.total if best.lhf else best.score.total}/100{lhf_label}"
            )

        await _send(chat_id,
            f"📊 <b>Last Scan Status</b>\n"
            f"Time: {scanned_at[:19].replace('T',' ')}\n"
            f"Tickers: {len(tickers)} scanned\n\n"
            f"Valid spreads: {len(spreads)}\n"
            f"🍒 Low Hanging Fruit: {n_lhf}\n"
            f"Rejected: {len(rejected)}"
            f"{top_line}\n\n"
            "/easy for LHF  •  /rejects for failures"
        )
    except Exception as exc:
        await _send(chat_id, f"❌ Status error: {exc}")


async def _cmd_ticker(args: list[str], chat_id: str) -> None:
    if not args:
        await _send(chat_id, "Usage: /ticker AAPL")
        return
    ticker = args[0].upper().strip()
    await _send(chat_id, f"🔍 Analyzing <b>{ticker}</b>...")

    try:
        from app.services.scanner_service import get_last_result
        from app.services.options_service import get_unusual_options
        from app.services.credit_spread_engine import generate_credit_spread, classify_lhf
        from app.services.telegram_service import format_spread_alert

        last = get_last_result()
        alerts = []
        if last:
            alerts = [a for a in last.get("alerts", []) if a["contract"].ticker == ticker]

        if not alerts:
            try:
                unusual = await get_unusual_options(ticker)
                alerts = [
                    {
                        "contract": c,
                        "bias": "BULLISH" if c.option_type == "call" else "BEARISH",
                        "underlying_price": unusual.underlying_price,
                    }
                    for c in unusual.combined[:3]
                ]
            except Exception as exc:
                await _send(chat_id, f"❌ Could not fetch data for {ticker}: {exc}")
                return

        if not alerts:
            await _send(chat_id, f"⚪ No unusual flow detected for <b>{ticker}</b>.")
            return

        spread = await generate_credit_spread(ticker, alerts)
        if spread is None:
            await _send(chat_id, f"❌ Chain data unavailable for <b>{ticker}</b>.")
            return

        if spread.verdict == "TAKE":
            all_alerts = last.get("alerts", []) if last else []
            lhf  = classify_lhf(spread, all_alerts)
            spread = spread.model_copy(update={"lhf": lhf})
            await _send(chat_id, format_spread_alert(spread))
        else:
            await _send(chat_id,
                f"❌ <b>{ticker}</b> — No valid spread\n"
                f"Reason: {spread.reject_reason or 'Score too low'}"
            )
    except Exception as exc:
        logger.error("_cmd_ticker %s error: %s", ticker, exc, exc_info=True)
        await _send(chat_id, f"❌ Error analyzing {ticker}: {exc}")


async def _cmd_easy(args: list[str], chat_id: str) -> None:
    try:
        from app.routers.credit_spread import get_last_spread_result
        from app.services.telegram_service import format_spread_alert

        result = get_last_spread_result()
        if not result:
            await _send(chat_id, "⚪ No scan data.\n\nRun /scan first.")
            return

        lhf_spreads = [
            s for s in result.get("spreads", [])
            if s.lhf and s.lhf.classification == "LOW_HANGING_FRUIT"
        ]

        if not lhf_spreads:
            valid = result.get("total_valid", 0)
            await _send(chat_id,
                "🍒 <b>No LOW HANGING FRUIT right now.</b>\n\n"
                + (f"{valid} valid (but not easy) setup(s) exist.\n" if valid else "")
                + "Run /scan to refresh."
            )
            return

        await _send(chat_id, f"🍒 <b>{len(lhf_spreads)} LOW HANGING FRUIT setup(s):</b>")
        for spread in lhf_spreads:
            await _send(chat_id, format_spread_alert(spread))
    except Exception as exc:
        await _send(chat_id, f"❌ Error: {exc}")


async def _cmd_rejects(args: list[str], chat_id: str) -> None:
    try:
        from app.routers.credit_spread import get_last_spread_result
        result = get_last_spread_result()
        if not result:
            await _send(chat_id, "⚪ No scan data.")
            return

        rejected = result.get("rejected", [])
        also_not_easy = [
            s for s in result.get("spreads", [])
            if s.lhf and s.lhf.classification != "LOW_HANGING_FRUIT"
        ]

        lines = ["❌ <b>Rejected Setups</b>\n"]
        for r in rejected[:12]:
            lines.append(f"• <b>{r['ticker']}</b>: {r['reason']}")

        if also_not_easy:
            lines.append("\n⚠️ <b>Valid but not easy:</b>")
            for s in also_not_easy[:5]:
                reasons = (s.lhf.reject_reasons or ["No clear reason"])[:1] if s.lhf else ["N/A"]
                lines.append(f"• <b>{s.ticker}</b>: {reasons[0]}")

        await _send(chat_id, "\n".join(lines))
    except Exception as exc:
        await _send(chat_id, f"❌ Error: {exc}")


async def _cmd_summary(args: list[str], chat_id: str) -> None:
    try:
        from app.routers.credit_spread import get_last_spread_result
        result = get_last_spread_result()
        if not result:
            await _send(chat_id, "⚪ No scan data. Run /scan first.")
            return

        spreads    = result.get("spreads", [])
        n_lhf      = result.get("total_lhf", 0)
        n_valid    = result.get("total_valid", 0)
        rejected   = result.get("rejected", [])
        tickers    = result.get("tickers_scanned", [])
        scanned_at = result.get("scanned_at", "unknown")[:19].replace("T", " ")

        lines = [
            f"📡 <b>Scan Summary</b>",
            f"Time: {scanned_at}",
            f"Tickers scanned: {len(tickers)}",
            "",
            f"Valid spreads:       {n_valid}",
            f"🍒 Low Hanging Fruit: {n_lhf}",
            f"Rejected:            {len(rejected)}",
        ]

        if spreads:
            lines.append("")
            if n_lhf > 0:
                best = next(s for s in spreads if s.lhf and s.lhf.classification == "LOW_HANGING_FRUIT")
                sc   = best.lhf.score.total
                lines.append(f"🏆 Best LHF: <b>{best.ticker}</b> {best.spread_type}")
                lines.append(f"   Sell {best.sell_strike:.0f} / Buy {best.buy_strike:.0f}")
                lines.append(f"   ${best.premium:.2f} credit  •  {best.win_probability:.0f}% win prob  •  Score: {sc}/100")
            else:
                best = spreads[0]
                sc   = best.score.total
                lines.append(f"Top setup: <b>{best.ticker}</b> {best.spread_type} — {sc}/100 (not easy)")
        else:
            lines.append("\n⚪ No easy setups found this cycle.")

        lines.append("\n/easy for alerts  •  /rejects for failures")
        await _send(chat_id, "\n".join(lines))
    except Exception as exc:
        await _send(chat_id, f"❌ Summary error: {exc}")


async def _cmd_perf(args: list[str], chat_id: str) -> None:
    try:
        from app.services.spread_tracker import recent_performance, pending_trades
        recent  = recent_performance(limit=10)
        pending = pending_trades()

        lines = ["📈 <b>Trade Performance</b>\n"]

        if pending:
            lines.append(f"⏳ Pending (awaiting expiry): {len(pending)}")
            for t in pending[:5]:
                lines.append(
                    f"  • {t['ticker']} {t['spread_type']} "
                    f"| Sell {t['sell_strike']:.0f} | Exp {t['expiration']} "
                    f"| ${t['net_credit']:.2f}"
                )

        if recent:
            wins   = sum(1 for r in recent if r["result_at_expiry"] == "WIN")
            losses = sum(1 for r in recent if r["result_at_expiry"] == "LOSS")
            lines.append(f"\n✅ Wins: {wins}  ❌ Losses: {losses}  (last {len(recent)})")
            for r in recent[:5]:
                icon = "✅" if r["result_at_expiry"] == "WIN" else "❌"
                lines.append(
                    f"  {icon} {r['ticker']} {r['spread_type']} "
                    f"| ${r['net_credit']:.2f} | {r['expiration']}"
                )
        else:
            lines.append("\nNo completed trades yet.")

        await _send(chat_id, "\n".join(lines))
    except Exception as exc:
        await _send(chat_id, f"❌ Perf error: {exc}")


async def _cmd_help(args: list[str], chat_id: str) -> None:
    await _send(chat_id,
        "🤖 <b>Credit Spread Bot</b>\n\n"
        "/scan — run full spread scan now\n"
        "/status — last scan overview\n"
        "/ticker AAPL — analyze one ticker\n"
        "/easy — only LOW HANGING FRUIT setups\n"
        "/rejects — rejected tickers + reasons\n"
        "/summary — full scan narrative\n"
        "/perf — recent trade performance\n"
        "/help — this message\n\n"
        "<i>Automatic scans run 8:30 AM + hourly 9:30–4:30 PM ET (weekdays)</i>"
    )


# ── Command dispatch ───────────────────────────────────────────────────────────

_HANDLERS = {
    "scan":    _cmd_scan,
    "status":  _cmd_status,
    "ticker":  _cmd_ticker,
    "easy":    _cmd_easy,
    "rejects": _cmd_rejects,
    "summary": _cmd_summary,
    "perf":    _cmd_perf,
    "help":    _cmd_help,
    "start":   _cmd_help,
}


async def _handle_update(update: dict) -> None:
    msg     = update.get("message", {})
    text    = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    if not text or not chat_id:
        return

    # Only respond to the configured chat
    if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
        logger.debug("bot: ignoring message from chat %s (not authorized)", chat_id)
        return

    if not text.startswith("/"):
        return

    parts = text.split()
    raw_cmd = parts[0].lstrip("/").split("@")[0].lower()
    cmd_args = parts[1:]

    handler = _HANDLERS.get(raw_cmd)
    if handler is None:
        return

    logger.info("bot: /%s %s from chat %s", raw_cmd, cmd_args, chat_id)
    try:
        await handler(cmd_args, chat_id)
    except Exception as exc:
        logger.error("bot: /%s handler raised: %s", raw_cmd, exc, exc_info=True)
        await _send(chat_id, f"❌ /{raw_cmd} failed: {exc}")


# ── Polling loop ───────────────────────────────────────────────────────────────

async def _poll_loop() -> None:
    if not settings.telegram_bot_token:
        logger.warning("bot: TELEGRAM_BOT_TOKEN not set — bot disabled")
        return

    logger.info("bot: long-polling started")
    offset = 0

    while True:
        try:
            updates = await _get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                await _handle_update(upd)
        except asyncio.CancelledError:
            logger.info("bot: polling cancelled")
            raise
        except Exception as exc:
            logger.error("bot: poll loop error: %s", exc)

        await asyncio.sleep(2)


def start_bot() -> None:
    global _bot_task
    if not settings.telegram_bot_token:
        logger.info("bot: TELEGRAM_BOT_TOKEN not configured — skipping bot startup")
        return
    if _bot_task is None or _bot_task.done():
        _bot_task = asyncio.create_task(_poll_loop())
        logger.info("bot: polling task started")


def stop_bot() -> None:
    global _bot_task
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        logger.info("bot: polling task cancelled")
