"""
FBA Telegram Bot — standalone long-polling bot, separate token from options bot.

Commands:
  /scan    — run FBA product discovery now
  /top     — show top products from last scan
  /help    — command list

Set FBA_BOT_TOKEN and FBA_CHAT_ID in Railway env to enable.
If FBA_BOT_TOKEN is not set, the bot silently does nothing.
"""
import asyncio
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_fba_bot_task: Optional[asyncio.Task] = None


# ── Telegram helpers ───────────────────────────────────────────────────────────

def _base() -> str:
    return f"https://api.telegram.org/bot{settings.fba_bot_token}"


async def _send(chat_id: str | int, text: str) -> None:
    if not settings.fba_bot_token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_base()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text[:4096],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        logger.warning("fba_bot._send error: %s", exc)


async def _get_updates(offset: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(
                f"{_base()}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as exc:
        logger.warning("fba_bot.getUpdates error: %s", exc)
    return []


# ── Commands ───────────────────────────────────────────────────────────────────

async def _cmd_scan(args: list[str], chat_id: str) -> None:
    await _send(chat_id, "🔄 Running FBA product scan — please wait ~30–60 seconds...")
    try:
        from app.services.fba_service import run_fba_scan, format_fba_summary, format_fba_alert

        result = await run_fba_scan(include_trends=True, min_score=40.0, top_n=15)
        await _send(chat_id, format_fba_summary(result))

        high = result.get("high", [])
        if high:
            for p in high[:5]:
                await _send(chat_id, format_fba_alert(p))
                await asyncio.sleep(0.4)
        else:
            medium = result.get("medium", [])[:3]
            if medium:
                await _send(chat_id, "⚪ No HIGH_OPPORTUNITY found — showing top MEDIUM:")
                for p in medium:
                    await _send(chat_id, format_fba_alert(p))
                    await asyncio.sleep(0.4)
            else:
                await _send(chat_id, "⚪ No products above threshold this scan.")

    except Exception as exc:
        logger.error("fba_bot._cmd_scan error: %s", exc, exc_info=True)
        await _send(chat_id, f"❌ Scan failed: {exc}")


async def _cmd_top(args: list[str], chat_id: str) -> None:
    try:
        from app.services.fba_service import get_last_fba_scan, format_fba_summary, format_fba_alert

        result = get_last_fba_scan()
        if not result:
            await _send(chat_id, "⚪ No scan yet.\n\nRun /scan to start.")
            return

        await _send(chat_id, format_fba_summary(result))

        n = int(args[0]) if args and args[0].isdigit() else 5
        for p in result.get("top_products", [])[:n]:
            await _send(chat_id, format_fba_alert(p))
            await asyncio.sleep(0.3)

    except Exception as exc:
        await _send(chat_id, f"❌ Error: {exc}")


async def _cmd_help(args: list[str], chat_id: str) -> None:
    await _send(chat_id,
        "🛒 <b>FBA Product Bot</b>\n\n"
        "/scan — run product discovery scan now\n"
        "/top [N] — show top N results from last scan\n"
        "/help — this message\n\n"
        "<i>Scans Amazon BSR + Movers & Shakers + Google Trends.\n"
        "Run /scan whenever you want fresh data.</i>"
    )


_HANDLERS = {
    "scan": _cmd_scan,
    "top":  _cmd_top,
    "help": _cmd_help,
    "start": _cmd_help,
}


# ── Update dispatch ────────────────────────────────────────────────────────────

async def _handle_update(update: dict) -> None:
    msg     = update.get("message", {})
    text    = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    if not text.startswith("/") or not chat_id:
        return

    parts   = text.lstrip("/").split()
    command = parts[0].split("@")[0].lower()
    args    = parts[1:]

    handler = _HANDLERS.get(command)
    if handler:
        asyncio.create_task(handler(args, chat_id))


# ── Polling loop ───────────────────────────────────────────────────────────────

async def _poll_loop() -> None:
    if not settings.fba_bot_token:
        logger.info("FBA bot: FBA_BOT_TOKEN not set — bot disabled")
        return

    logger.info("FBA bot: polling started")
    offset = 0

    while True:
        try:
            updates = await _get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                await _handle_update(upd)
        except asyncio.CancelledError:
            logger.info("FBA bot: polling cancelled")
            raise
        except Exception as exc:
            logger.warning("FBA bot: poll error: %s", exc)
            await asyncio.sleep(5)


def start_fba_bot() -> None:
    global _fba_bot_task
    if not settings.fba_bot_token:
        logger.info("FBA bot: skipping start (FBA_BOT_TOKEN not configured)")
        return
    if _fba_bot_task is None or _fba_bot_task.done():
        _fba_bot_task = asyncio.create_task(_poll_loop())
        logger.info("FBA bot task created.")


def stop_fba_bot() -> None:
    global _fba_bot_task
    if _fba_bot_task and not _fba_bot_task.done():
        _fba_bot_task.cancel()
        logger.info("FBA bot task cancelled.")
