"""
Telegram alert dispatcher — Tradable Flow Edition.

Formats unusual option alerts and sends them via the Telegram Bot API.
Supports individual contract alerts and cluster (multi-strike) summaries.
"""
import logging
from datetime import date

import httpx

from app.config import settings
from app.models.options import OptionContract

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_premium(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _fmt_expiry(expiration: str) -> str:
    """Format ISO date as '14 Jun'."""
    try:
        exp = date.fromisoformat(expiration)
        return exp.strftime("%-d %b")
    except (ValueError, TypeError):
        return expiration


def _dte_calc(expiration: str) -> int:
    try:
        exp = date.fromisoformat(expiration)
        return (exp - date.today()).days
    except (ValueError, TypeError):
        return 0


def _moneyness_label(moneyness: float | None, option_type: str) -> str:
    """
    Return a label like '[+3.2% OTM]' or '[1.8% ITM]' correct for
    both calls and puts.
    """
    if moneyness is None:
        return ""
    pct = (moneyness - 1.0) * 100
    if abs(pct) < 1.0:
        return " \\[ATM]"
    if option_type == "call":
        if pct > 0:
            return f" \\[+{pct:.1f}% OTM]"
        return f" \\[{abs(pct):.1f}% ITM]"
    else:
        if pct < 0:
            return f" \\[+{abs(pct):.1f}% OTM]"
        return f" \\[{pct:.1f}% ITM]"


# ── Alert formatters ──────────────────────────────────────────────────────────

def format_alert(contract: OptionContract, bias: str, underlying_price: float) -> str:
    """Format a single contract alert — concise, scan-ready."""
    is_call = contract.option_type == "call"
    emoji   = "🟢" if is_call else "🔴"
    otype   = "C" if is_call else "P"

    exp_str   = _fmt_expiry(contract.expiration)
    dte       = _dte_calc(contract.expiration)
    money_lbl = _moneyness_label(contract.moneyness, contract.option_type)

    delta_str = f"{contract.delta:.2f}" if contract.delta is not None else "—"
    iv_str    = f"{contract.implied_volatility * 100:.1f}%" if contract.implied_volatility else "—"
    tags_str  = ", ".join(contract.reason_tags) if contract.reason_tags else "—"

    grade  = contract.conviction_grade
    conv   = contract.conviction_score
    cls    = contract.contract_class

    lines = [
        f"{emoji} *{contract.ticker} ${contract.strike:.0f}{otype}  {exp_str}*{money_lbl}",
        (
            f"💰 {_fmt_premium(contract.vol_notional)}  ·  "
            f"Vol `{contract.volume:,}` / OI `{contract.open_interest:,}` "
            f"(`{contract.vol_oi_ratio:.1f}x`)"
        ),
        f"Δ `{delta_str}`  IV `{iv_str}`  DTE `{dte}`",
        f"Score `{contract.unusual_score:.0f}`  ·  Conviction `{grade}` ({conv:.0f})  ·  _{cls}_",
        f"📈 *{bias}*",
        f"🏷 _{tags_str}_",
    ]
    return "\n".join(lines)


def format_cluster_alert(alert: dict) -> str:
    """Format a grouped multi-strike cluster as one summary alert."""
    contract        = alert["contract"]
    bias            = alert["bias"]
    cluster_count   = alert.get("cluster_count", 1)
    cluster_strikes = alert.get("cluster_strikes", [contract.strike])

    is_call = contract.option_type == "call"
    emoji   = "🟢" if is_call else "🔴"
    otype   = "CALLS" if is_call else "PUTS"

    exp_str   = _fmt_expiry(contract.expiration)
    dte       = _dte_calc(contract.expiration)
    delta_str = f"{contract.delta:.2f}" if contract.delta is not None else "—"
    iv_str    = f"{contract.implied_volatility * 100:.1f}%" if contract.implied_volatility else "—"

    grade = contract.conviction_grade
    conv  = contract.conviction_score
    cls   = contract.contract_class

    strikes_str = ", ".join(f"${s:.0f}" for s in cluster_strikes)

    lines = [
        f"{emoji} *{contract.ticker} {otype}  {exp_str}*  — {cluster_count} strikes",
        f"Strikes: _{strikes_str}_",
        (
            f"Best: ${contract.strike:.0f}  Score `{contract.unusual_score:.0f}`  "
            f"Conviction `{grade}` ({conv:.0f})  _{cls}_"
        ),
        (
            f"💰 {_fmt_premium(contract.vol_notional)}  ·  "
            f"Vol `{contract.volume:,}` / OI `{contract.open_interest:,}` "
            f"(`{contract.vol_oi_ratio:.1f}x`)"
        ),
        f"Δ `{delta_str}`  IV `{iv_str}`  DTE `{dte}`",
        f"📈 *{bias}*",
    ]
    return "\n".join(lines)


def format_summary(scan_result: dict) -> str:
    n       = len(scan_result["alerts"])
    flow    = scan_result["total_unusual_flow"]
    tickers = ", ".join(scan_result["tickers_scanned"]) or "—"
    failed  = ", ".join(scan_result["tickers_failed"]) if scan_result["tickers_failed"] else "none"
    ts      = scan_result["scanned_at"].strftime("%H:%M UTC")

    return (
        
        "─" * 26 + "\n"
        f"Tickers: `{tickers}`\n"
        f"Failed: `{failed}`\n"
        f"Alerts: *{n}*  ·  Flow: *{_fmt_premium(flow)}*"
    )


# ── Sender ────────────────────────────────────────────────────────────────────

async def _post(text: str) -> bool:
    if not settings.telegram_enabled:
        return False
    token   = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
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
    return await _post(format_alert(contract, bias, underlying_price))


async def send_scan_summary(scan_result: dict) -> None:
    """Send one message per alert (capped at 10)."""
    alerts = scan_result["alerts"]
    if not alerts:
        logger.info("No alerts to send.")
        return

   

    for alert in alerts[:10]:
        if alert.get("cluster_count", 1) > 1:
            text = format_cluster_alert(alert)
        else:
            text = format_alert(
                alert["contract"],
                alert["bias"],
                alert["underlying_price"],
            )
        await _post(text)
