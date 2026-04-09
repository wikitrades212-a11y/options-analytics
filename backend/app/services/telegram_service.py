"""
Telegram alert dispatcher.

Formats unusual option alerts and sends them via the Telegram Bot API.
Uses httpx (already in requirements) — no extra dependency needed.
"""
import logging

import httpx

from app.config import settings
from app.models.options import OptionContract

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt_premium(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _moneyness_label(moneyness: float | None) -> str:
    if moneyness is None:
        return ""
    pct = (moneyness - 1.0) * 100
    if abs(pct) < 1.0:
        return " \\[ATM]"
    if pct > 0:
        return f" \\[+{pct:.1f}% OTM]"
    return f" \\[{pct:.1f}% ITM]"


def format_alert(contract: OptionContract, bias: str, underlying_price: float) -> str:
    is_call = contract.option_type == "call"
    emoji   = "🟢" if is_call else "🔴"
    otype   = "CALL" if is_call else "PUT"

    iv_str    = f"{contract.implied_volatility * 100:.1f}%" if contract.implied_volatility else "N/A"
    delta_str = f"{contract.delta:.2f}" if contract.delta is not None else "N/A"
    tags_str  = ", ".join(contract.reason_tags) if contract.reason_tags else "—"
    money_lbl = _moneyness_label(contract.moneyness)

    lines = [
        f"{emoji} *UNUSUAL OPTIONS ALERT*",
        "─" * 26,
        f"📌 *{contract.ticker}*  ${contract.strike:.0f} {otype}  exp `{contract.expiration}`{money_lbl}",
        f"💰 Premium Flow: *{_fmt_premium(contract.vol_notional)}*",
        f"📊 Vol: `{contract.volume:,}`  OI: `{contract.open_interest:,}`  Vol/OI: `{contract.vol_oi_ratio:.1f}x`",
        f"⚡ Delta: `{delta_str}`  IV: `{iv_str}`",
        f"🎯 Score: *{contract.unusual_score:.0f}/100*",
        f"🏷 {tags_str}",
        f"📈 Bias: *{bias}*",
    ]
    return "\n".join(lines)


def format_summary(scan_result: dict) -> str:
    n       = len(scan_result["alerts"])
    flow    = scan_result["total_unusual_flow"]
    tickers = ", ".join(scan_result["tickers_scanned"]) or "—"
    failed  = ", ".join(scan_result["tickers_failed"]) if scan_result["tickers_failed"] else "none"
    ts      = scan_result["scanned_at"].strftime("%H:%M UTC")

    return (
        f"🔍 *Unusual Options Scan*  `{ts}`\n"
        "─" * 26 + "\n"
        f"Tickers scanned: `{tickers}`\n"
        f"Failed: `{failed}`\n"
        f"Alerts found: *{n}*\n"
        f"Total flow: *{_fmt_premium(flow)}*"
    )


# ── Sender ────────────────────────────────────────────────────────────────────

async def _post(text: str) -> bool:
    if not settings.telegram_enabled:
        return False
    token    = settings.telegram_bot_token
    chat_id  = settings.telegram_chat_id
    if not token or not chat_id:
        logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False

    url = _SEND_URL.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
            })
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error(f"Telegram send error: {exc}")
        return False


async def send_alert(contract: OptionContract, bias: str, underlying_price: float) -> bool:
    text = format_alert(contract, bias, underlying_price)
    return await _post(text)


async def send_scan_summary(scan_result: dict) -> None:
    """Send a summary header then one message per alert (capped at 10)."""
    alerts = scan_result["alerts"]
    if not alerts:
        logger.info("No alerts to send.")
        return

    await _post(format_summary(scan_result))

    for alert in alerts[:10]:
        await send_alert(
            alert["contract"],
            alert["bias"],
            alert["underlying_price"],
        )
