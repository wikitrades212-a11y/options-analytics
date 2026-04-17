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
from app.services.csv_logger import log_alerts_to_csv

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
        logger.warning("Telegram disabled (TELEGRAM_ENABLED=false) — skipping send")
        return False
    token   = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False

    url = _SEND_URL.format(token=token)
    logger.info("Telegram POST → chat_id=%s text_len=%d", chat_id, len(text))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id":                  chat_id,
                "text":                     text[:4096],
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            })
            resp.raise_for_status()
        logger.info("Telegram POST OK")
        return True
    except Exception as exc:
        logger.error(f"Telegram send error: {exc}")
        return False


async def send_alert(contract: OptionContract, bias: str, underlying_price: float) -> bool:
    return await _post(format_alert(contract, bias, underlying_price))


async def send_scan_summary(scan_result: dict) -> None:
    """
    Build one AI summary message from all scan alerts and send it.

    Happy path:
      1. Convert OptionContract alert dicts → scorer format
      2. build_summary_message() → single Markdown summary
      3. POST to Telegram

    Early exits (no send):
      - No alerts from scanner
      - Scorer returns empty string (all flow below SPECULATIVE threshold)

    Fallback:
      If the scorer raises for any reason, fall back to the original
      per-alert format so Telegram never goes silent due to scorer bugs.
    """
    from app.services.flow_scorer import build_summary_message, contracts_to_scorer_alerts

    alerts = scan_result["alerts"]
    logger.info(
        "send_scan_summary called: %d alerts, telegram_enabled=%s",
        len(alerts), settings.telegram_enabled,
    )

    if not alerts:
        logger.info("send_scan_summary: no alerts — skipping Telegram send.")
        return

    # ── Happy path: AI summary ────────────────────────────────────────────────
    try:
        scorer_alerts = contracts_to_scorer_alerts(alerts)
        summary = build_summary_message(scorer_alerts)

        if not summary:
            # Normal operating condition — flow existed but all scored WEAK.
            # INFO, not WARNING. This is expected during quiet tape.
            logger.info(
                "send_scan_summary: scorer returned empty (all flow WEAK or below threshold) "
                "— no Telegram send. alerts_in=%d",
                len(alerts),
            )
            return

        sent = await _post(summary)
        log_alerts_to_csv(alerts, telegram_sent=sent)
        return

    except Exception as exc:
        # ERROR — scorer raised unexpectedly. The [SCORER FALLBACK] tag makes
        # this instantly greppable in Railway logs: `grep "SCORER FALLBACK"`.
        logger.error(
            "[SCORER FALLBACK] flow_scorer raised — sending raw alerts instead. "
            "alerts=%d  error=%s",
            len(alerts),
            exc,
            exc_info=True,
        )

    # ── Fallback: original per-alert format ───────────────────────────────────
    # Reached only when the scorer raised above. Sends up to 10 individual
    # alerts so Telegram is never silenced by a scorer bug.
    logger.warning(
        "[SCORER FALLBACK] sending %d raw alert(s) — fix scorer to restore AI summaries.",
        len(alerts),
    )
    for alert in alerts[:10]:
        if alert.get("cluster_count", 1) > 1:
            text = format_cluster_alert(alert)
        else:
            text = format_alert(
                alert["contract"],
                alert["bias"],
                alert["underlying_price"],
            )
        sent = await _post(text)
        log_alerts_to_csv([alert], telegram_sent=sent)


# ── Credit Spread + LHF alert formatting (HTML) ───────────────────────────────

def _notional_str(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


def format_spread_alert(spread) -> str:
    """
    Format a CreditSpreadResult (with optional LHF result) as an HTML
    Telegram message. Switches header style based on LHF classification.
    """
    from app.models.credit_spread import CreditSpreadResult
    s: CreditSpreadResult = spread

    is_put        = "Put" in s.spread_type
    suffix        = "P" if is_put else "C"
    lhf           = s.lhf
    classification = lhf.classification if lhf else "UNKNOWN"

    # ── Header ────────────────────────────────────────────────────────────────
    if classification == "LOW_HANGING_FRUIT":
        header = "🍒 <b>LOW HANGING FRUIT</b>"
    elif classification == "VALID_BUT_NOT_EASY":
        header = "📊 <b>VALID SETUP</b> (Not Easy)"
    else:
        header = "📋 <b>CREDIT SPREAD</b>"

    # ── Strike line ───────────────────────────────────────────────────────────
    exp_pretty = _fmt_expiry(s.expiration)
    strike_line = (
        f"Sell <b>{s.sell_strike:.0f}{suffix}</b> / Buy {s.buy_strike:.0f}{suffix}"
        f"  •  Exp: {exp_pretty} ({s.dte}d)  •  Δ{s.structure.delta_at_sell:.2f}"
    )

    # ── Premium block ─────────────────────────────────────────────────────────
    rr = s.max_risk / s.premium if s.premium > 0 else 0
    prem_line = (
        f"💰 Credit: <b>${s.premium:.2f}</b>  |  Risk: ${s.max_risk:.2f}  |  RR: {rr:.1f}:1\n"
        f"🎯 Win Probability: <b>{s.win_probability:.0f}%</b>"
    )

    # ── Score block ───────────────────────────────────────────────────────────
    if lhf:
        sc = lhf.score
        score_block = (
            f"📊 <b>LHF Score: {sc.total}/100</b>\n"
            f"├ Flow:       {sc.flow_clarity}/25\n"
            f"├ Structure:  {sc.structure_safety}/25\n"
            f"├ Regime:     {sc.regime}/20\n"
            f"├ Premium:    {sc.premium_quality}/10\n"
            f"└ Edge:       {sc.historical_edge}/20"
        )
    else:
        sc2 = s.score
        score_block = (
            f"📊 <b>Score: {sc2.total}/100</b>\n"
            f"Flow: {sc2.flow_score}/30  |  Structure: {sc2.structure_score}/30\n"
            f"Probability: {sc2.probability_score}/20  |  Edge: {sc2.historical_score}/20"
        )

    # ── Flow confirmation ─────────────────────────────────────────────────────
    voi_str = f"{s.flow.vol_oi_ratio:.1f}x"
    inst    = any("Big Premium" in t or "Institutional" in t for t in s.flow.tags)
    flow_block = (
        f"<b>Flow Confirmation:</b>\n"
        f"• {s.flow.description}\n"
        f"• Vol/OI: {voi_str}  ({_notional_str(s.flow.vol_notional)} notional)\n"
        f"• Grade <b>{s.flow.conviction_grade}</b> conviction"
        + ("  •  Institutional" if inst else "")
    )

    # ── Structure context ─────────────────────────────────────────────────────
    struct_bullets = "\n".join(f"• {n}" for n in s.structure.notes)
    struct_block   = f"<b>Structure:</b>\n{struct_bullets}"

    # ── Why easy / landmines ──────────────────────────────────────────────────
    extra = ""
    if lhf and classification == "LOW_HANGING_FRUIT" and lhf.why_easy:
        bullets = "\n".join(f"• {w}" for w in lhf.why_easy[:4])
        extra += f"\n✅ <b>Why it's easy:</b>\n{bullets}"

    if lhf and lhf.landmines:
        mines = "\n".join(f"• {m}" for m in lhf.landmines[:3])
        extra += f"\n⚠️ <b>Landmine Check:</b>\n{mines}"
    else:
        extra += "\n⚠️ <b>Landmine Check:</b> None flagged ✅"

    if lhf and classification == "VALID_BUT_NOT_EASY" and lhf.reject_reasons:
        reasons = "\n".join(f"• {r}" for r in lhf.reject_reasons[:3])
        extra += f"\n\n<i>Why not easy:</i>\n{reasons}"

    # ── Verdict ───────────────────────────────────────────────────────────────
    if classification == "LOW_HANGING_FRUIT":
        verdict_line = "<b>VERDICT: ✅ TAKE</b>"
    elif classification == "VALID_BUT_NOT_EASY":
        verdict_line = "<b>VERDICT: ⚠️ TAKE (cautious sizing)</b>"
    else:
        verdict_line = f"<b>VERDICT: ✅ TAKE</b>"

    parts = [
        header,
        "",
        f"<b>{s.ticker}</b> — {s.spread_type}",
        strike_line,
        "",
        prem_line,
        "",
        score_block,
        "",
        flow_block,
        "",
        struct_block,
        extra,
        "",
        verdict_line,
    ]

    return "\n".join(parts)


async def send_spread_alerts(spreads: list) -> None:
    """
    Send Telegram alerts for all TAKE spreads.
    LHF spreads are sent first. Persists each to spread_tracker.
    """
    if not settings.telegram_enabled:
        logger.info("send_spread_alerts: Telegram disabled — skipping.")
        return

    valid = [s for s in spreads if s.verdict == "TAKE"]
    logger.info("send_spread_alerts: %d spread(s) to send", len(valid))

    try:
        from app.services.spread_tracker import record_spread
        _tracker_ok = True
    except Exception:
        _tracker_ok = False

    for spread in valid:
        text = format_spread_alert(spread)
        await _post(text)
        if _tracker_ok:
            try:
                record_spread(spread)
            except Exception as exc:
                logger.warning("spread_tracker.record_spread failed: %s", exc)
