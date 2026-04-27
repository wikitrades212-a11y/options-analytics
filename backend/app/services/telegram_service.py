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

    prem_at_sig = contract.premium_at_signal
    prem_at_sig_str = f"${prem_at_sig:.2f}" if prem_at_sig else "N/A"

    lines = [
        f"{emoji} *{contract.ticker} ${contract.strike:.0f}{otype}  {exp_str}*{money_lbl}",
        (
            f"💰 Notional: {_fmt_premium(contract.vol_notional)}  ·  "
            f"Vol `{contract.volume:,}` / OI `{contract.open_interest:,}` "
            f"(`{contract.vol_oi_ratio:.1f}x`)"
        ),
        f"📌 Premium at signal: `{prem_at_sig_str}`",
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

    prem_at_sig = contract.premium_at_signal
    prem_at_sig_str = f"${prem_at_sig:.2f}" if prem_at_sig else "N/A"

    lines = [
        f"{emoji} *{contract.ticker} {otype}  {exp_str}*  — {cluster_count} strikes",
        f"Strikes: _{strikes_str}_",
        (
            f"Best: ${contract.strike:.0f}  Score `{contract.unusual_score:.0f}`  "
            f"Conviction `{grade}` ({conv:.0f})  _{cls}_"
        ),
        (
            f"💰 Notional: {_fmt_premium(contract.vol_notional)}  ·  "
            f"Vol `{contract.volume:,}` / OI `{contract.open_interest:,}` "
            f"(`{contract.vol_oi_ratio:.1f}x`)"
        ),
        f"📌 Premium at signal: `{prem_at_sig_str}`",
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
    """Send HTML-formatted message. Used by the spread/LHF rail only."""
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


async def _post_flow(text: str) -> bool:
    """Send Markdown-formatted message. Used by the normal flow rail only.
    Flow scorer (flow_scorer.py) explicitly outputs Markdown — do not change this to HTML.
    """
    if not settings.telegram_enabled:
        return False
    token   = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return False

    url = _SEND_URL.format(token=token)
    logger.info("Telegram FLOW POST → chat_id=%s text_len=%d", chat_id, len(text))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id":                  chat_id,
                "text":                     text[:4096],
                "parse_mode":               "Markdown",
                "disable_web_page_preview": True,
            })
            resp.raise_for_status()
        logger.info("Telegram FLOW POST OK")
        return True
    except Exception as exc:
        logger.error(f"Telegram flow send error: {exc}")
        return False


async def send_alert(contract: OptionContract, bias: str, underlying_price: float) -> bool:
    return await _post_flow(format_alert(contract, bias, underlying_price))


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

        sent = await _post_flow(summary)
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
        sent = await _post_flow(text)
        log_alerts_to_csv([alert], telegram_sent=sent)


# ── Credit Spread + LHF alert formatting (HTML) ───────────────────────────────

# Internal classification → user-facing verdict label
_DISPLAY_VERDICT: dict[str, str] = {
    "LOW_HANGING_FRUIT":   "✅ TAKE",
    "VALID_SETUP":         "⚠️ CONDITIONAL TAKE",
    "ACTIVE_TRADER_SETUP": "⚡ ACTIVE TRADE ONLY",
    "REJECT":              "❌ PASS",
}

# Only these classifications are sent as live alerts.
# REJECT (❌ PASS) is stored in history only; surfaced via /rejects,
# debug mode (LHF_DEBUG_ALERTS=true), or /scan all. Never auto-pushed.
_ALERTABLE: frozenset[str] = frozenset({"LOW_HANGING_FRUIT", "VALID_SETUP", "ACTIVE_TRADER_SETUP"})


def _notional_str(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


def _format_pass_alert(s) -> str:
    """
    Compact format for a REJECT (❌ PASS) spread — shows block reason and bullets.
    Called when LHF classification == REJECT.
    """
    lhf     = s.lhf
    blocked = (
        lhf.lhf_blocked_by
        if lhf and lhf.lhf_blocked_by
        else (lhf.reject_reasons[0] if lhf and lhf.reject_reasons else "Hard block")
    )

    lines = [
        "❌ <b>PASS</b>",
        "",
        f"<b>{s.ticker}</b> — {s.spread_type}",
        "",
        "<b>VERDICT: ❌ PASS</b>",
        f"Reason: {blocked}",
    ]

    if lhf and lhf.reject_reasons:
        lines.append("")
        for r in lhf.reject_reasons[:4]:
            lines.append(f"• {r}")

    return "\n".join(lines)


def format_spread_alert(spread) -> str:
    """
    Format a CreditSpreadResult (with LHF result) as an HTML Telegram message.

    Classification → display verdict:
      LOW_HANGING_FRUIT   → ✅ TAKE          (passive, normal size)
      VALID_SETUP         → ⚠️ CONDITIONAL TAKE (active, small size)
      ACTIVE_TRADER_SETUP → ⚡ ACTIVE TRADE ONLY (active, small, must manage)
      REJECT              → ❌ PASS           (compact format, no trade details)
    """
    from app.models.credit_spread import CreditSpreadResult
    s: CreditSpreadResult = spread

    is_put         = "Put" in s.spread_type
    suffix         = "P" if is_put else "C"
    lhf            = s.lhf
    classification = lhf.classification if lhf else "UNKNOWN"

    # REJECT — use compact PASS format (no strike/score details)
    if classification == "REJECT":
        return _format_pass_alert(s)

    # ── Header ────────────────────────────────────────────────────────────────
    if classification == "LOW_HANGING_FRUIT":
        header = "🍒 <b>LOW HANGING FRUIT</b>"
    elif classification == "ACTIVE_TRADER_SETUP":
        header = "⚡ <b>ACTIVE TRADER SETUP</b>"
    elif classification == "VALID_SETUP":
        header = "✅ <b>VALID SETUP</b>"
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
        pen_line = f"\n└ Penalties: -{sc.penalties}  (raw {sc.raw_total})" if sc.penalties else ""
        score_block = (
            f"📊 <b>LHF Score: {sc.total}/100</b>\n"
            f"├ Flow:       {sc.flow_clarity}/25\n"
            f"├ Structure:  {sc.structure_safety}/25\n"
            f"├ Regime:     {sc.regime}/20\n"
            f"├ Premium:    {sc.premium_quality}/10\n"
            f"{'├' if pen_line else '└'} Edge:       {sc.historical_edge}/20"
            + pen_line
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

    if lhf and lhf.warnings:
        for w in lhf.warnings[:3]:
            extra += f"\n⚠️ {w}"

    if lhf and lhf.lhf_blocked_by and classification != "LOW_HANGING_FRUIT":
        extra += f"\n🚫 <b>Hard block:</b> {lhf.lhf_blocked_by}"

    # WHY NOT LHF — shown for VALID_SETUP and ACTIVE_TRADER_SETUP
    if lhf and classification in ("VALID_SETUP", "ACTIVE_TRADER_SETUP") and lhf.why_not_lhf:
        reasons = "\n".join(f"• {r}" for r in lhf.why_not_lhf[:4])
        extra += f"\n\n<b>WHY NOT LHF:</b>\n{reasons}"
    elif lhf and classification == "VALID_SETUP" and lhf.reject_reasons:
        reasons = "\n".join(f"• {r}" for r in lhf.reject_reasons[:3])
        extra += f"\n\n<i>Why not easy:</i>\n{reasons}"

    # ── Verdict + trade characterization ─────────────────────────────────────
    display      = _DISPLAY_VERDICT.get(classification, "⚠️ CONDITIONAL TAKE")
    verdict_parts = [f"<b>VERDICT: {display}</b>"]

    if lhf:
        verdict_parts.append(f"TRADE_STYLE: {lhf.trade_style}")
        verdict_parts.append(f"SIZE: {lhf.size_recommendation}")
        if lhf.gamma_risk != "LOW":
            verdict_parts.append(f"GAMMA_RISK: <b>{lhf.gamma_risk}</b>")
        if lhf.do_not_hold_blindly:
            verdict_parts.append("DO_NOT_HOLD_BLINDLY: <b>TRUE ⚠️</b>")

        # Management — only for non-LHF tiers where active management matters
        if lhf.management and classification != "LOW_HANGING_FRUIT":
            m = lhf.management
            verdict_parts.append("")
            verdict_parts.append("<b>MANAGEMENT:</b>")
            if m.get("entry"):
                verdict_parts.append(f"• Entry: {m['entry']}")
            if m.get("stop"):
                verdict_parts.append(f"• Stop: {m['stop']}")
            if m.get("profit_taking"):
                verdict_parts.append(f"• Profit: {m['profit_taking']}")
            if m.get("invalidation"):
                verdict_parts.append(f"• Invalidation: {m['invalidation']}")
        elif lhf.management and classification == "LOW_HANGING_FRUIT":
            m = lhf.management
            verdict_parts.append(f"<i>Stop: {m.get('stop','50% of max risk')}</i>")

    verdict_block = "\n".join(verdict_parts)

    # ── BOT_DATA (machine-readable) ───────────────────────────────────────────
    if lhf:
        sc = lhf.score
        flow_q   = "STRONG" if sc.flow_clarity >= 18 else ("MODERATE" if sc.flow_clarity >= 12 else "WEAK")
        struct_q = "STRONG" if sc.structure_safety >= 18 else ("MODERATE" if sc.structure_safety >= 12 else "WEAK")
        regime_q = "STRONG" if sc.regime >= 16 else ("MODERATE" if sc.regime >= 10 else "WEAK")
        bot_data = (
            f"\n<pre>BOT_DATA\n"
            f"CLASSIFICATION={classification}\n"
            f"VERDICT={display}\n"
            f"TRADE_STYLE={lhf.trade_style}\n"
            f"SIZE={lhf.size_recommendation}\n"
            f"GAMMA_RISK={lhf.gamma_risk}\n"
            f"FLOW_QUALITY={flow_q}\n"
            f"STRUCTURE_QUALITY={struct_q}\n"
            f"REGIME_QUALITY={regime_q}</pre>"
        )
    else:
        bot_data = ""

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
        verdict_block,
        bot_data,
    ]

    return "\n".join(parts)


async def send_system_alert(text: str) -> bool:
    """Send a plain-text ops/system alert (no parse_mode markup)."""
    if not settings.telegram_enabled:
        return False
    token   = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return False
    url = _SEND_URL.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id":                  chat_id,
                "text":                     text[:4096],
                "disable_web_page_preview": True,
            })
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error(f"Telegram system alert error: {exc}")
        return False


async def send_spread_alerts(spreads: list) -> None:
    """
    Send Telegram alerts for ✅ TAKE and ⚠️ WATCH spreads only.

    ❌ PASS (REJECT) classifications are never sent here — they go to
    history/rejects only. Use send_pass_alerts() for the explicit paths
    (debug mode, /rejects, /scan all).
    """
    if not settings.telegram_enabled:
        logger.info("send_spread_alerts: Telegram disabled — skipping.")
        return

    # Explicit guard: only TAKE/WATCH reach the channel — PASS is never alertable
    valid = [
        s for s in spreads
        if s.lhf and s.lhf.classification in _ALERTABLE
        or (not s.lhf and s.verdict == "TAKE")
    ]
    logger.info(
        "send_spread_alerts: %d/%d spread(s) alertable (PASS suppressed)",
        len(valid), len(spreads),
    )

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


async def send_pass_alerts(rejected: list) -> None:
    """
    Send ❌ PASS alerts for LHF-blocked spreads.

    Only called in three explicit paths:
      1. /rejects command (user-requested)
      2. LHF_DEBUG_ALERTS=true in env
      3. /scan all (manual scan requesting full output)

    Never called from automated scan flow.
    """
    if not settings.telegram_enabled:
        return

    # Only items that have a full spread object (LHF-blocked, not base-engine SKIPs)
    pass_items = [r for r in rejected if r.get("spread")]
    if not pass_items:
        return

    logger.info("send_pass_alerts: sending %d PASS alert(s)", len(pass_items))
    await _post(f"🔍 <b>PASS Alerts ({len(pass_items)} blocked setup(s))</b>")
    for item in pass_items:
        await _post(format_spread_alert(item["spread"]))
