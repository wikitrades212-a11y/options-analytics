"""
Telegram Stock Formatter
Renders a StockAnalysis as a tight, decision-focused Markdown message
compatible with Telegram's MarkdownV2-lite (no entity escaping needed for
the fields we output, as long as we avoid reserved chars in data).

Outputs ~40–50 lines — readable on mobile without excessive scrolling.
"""
from __future__ import annotations

from typing import Optional

from app.models.stock_fundamentals import StockAnalysis


# ── Formatting helpers ────────────────────────────────────────────────────────

def _p(val: Optional[float]) -> str:
    """Format a dollar price or large dollar amount."""
    if val is None:
        return "N/A"
    if val >= 1_000_000_000_000:
        return f"${val / 1_000_000_000_000:.2f}T"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    return f"${val:,.2f}"


def _pct(val: Optional[float], decimals: int = 1) -> str:
    """Format a ratio (0.12) as a percentage string (+12.0%)."""
    if val is None:
        return "N/A"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val * 100:.{decimals}f}%"


def _x(val: Optional[float]) -> str:
    """Format a multiplier ratio (P/E, EV/EBITDA etc.)."""
    if val is None:
        return "N/A"
    return f"{val:.1f}x"


def _bar(score: float, max_score: float = 100) -> str:
    """ASCII progress bar out of 10 blocks."""
    filled = round((score / max_score) * 10)
    return "█" * filled + "░" * (10 - filled)


_VERDICT_EMOJI = {
    "Strong Candidate": "🟢",
    "Watchlist": "🟡",
    "Good Business, Too Expensive": "🟠",
    "Speculative": "🟠",
    "Avoid": "🔴",
}


# ── Main formatter ────────────────────────────────────────────────────────────

def format_for_telegram(analysis: StockAnalysis) -> str:
    a = analysis
    v = a.valuation_metrics
    g = a.growth_metrics
    m = a.margin_metrics
    h = a.financial_health
    dcf = a.dcf
    sc = a.score.score

    lines = []

    # ── Header ──
    sector_str = f" | {a.sector}" if a.sector else ""
    lines += [
        f"📊 *STOCK ANALYSIS — {a.ticker}*",
        f"_{a.company_name}{sector_str}_",
        "",
        f"💰 *Price:* {_p(a.current_price)}",
    ]
    if a.market_cap:
        lines.append(f"📦 *Market Cap:* {_p(a.market_cap)}")

    # ── DCF block ──
    lines.append("")
    if dcf.is_reliable and dcf.intrinsic_value_per_share:
        direction_icon = "📈" if (dcf.upside_downside_pct or 0) >= 0 else "📉"
        lines += [
            f"🎯 *Fair Value (DCF):* {_p(dcf.intrinsic_value_per_share)}",
            f"{direction_icon} *Upside/Downside:* {_pct(dcf.upside_downside_pct)}",
            f"🔬 *DCF Confidence:* {dcf.confidence.capitalize()}",
        ]
    else:
        reason = dcf.confidence_reasons[0] if dcf.confidence_reasons else "insufficient data"
        lines.append(f"🎯 *DCF:* Unreliable — _{reason}_")

    # ── Fundamentals ──
    lines += [
        "",
        "📋 *Fundamentals:*",
        f"  • Revenue Growth (YoY): {_pct(g.revenue_growth_yoy)}",
        f"  • Revenue CAGR (3y):    {_pct(g.revenue_cagr_3y)}",
        f"  • Net Income Growth:    {_pct(g.net_income_growth_yoy)}",
        f"  • FCF Profile:          {a.fcf_profile.consistency}",
        f"  • Gross Margin:         {_pct(m.gross_margin)}",
        f"  • Operating Margin:     {_pct(m.operating_margin)}",
        f"  • Net Margin:           {_pct(m.net_margin)}",
        f"  • Debt Load:            {h.debt_level or 'N/A'}",
        f"  • Liquidity:            {h.liquidity or 'N/A'}",
    ]
    if h.debt_to_equity is not None:
        lines.append(f"  • D/E Ratio:            {h.debt_to_equity:.2f}x")
    if h.interest_coverage is not None:
        lines.append(f"  • Interest Coverage:    {h.interest_coverage:.1f}x")

    # ── Valuation ──
    lines += [
        "",
        "📐 *Valuation Metrics:*",
        f"  • P/E:        {_x(v.pe_ratio)}",
        f"  • Fwd P/E:    {_x(v.forward_pe)}",
        f"  • PEG:        {_x(v.peg_ratio)}",
        f"  • P/S:        {_x(v.price_to_sales)}",
        f"  • P/B:        {_x(v.price_to_book)}",
        f"  • EV/EBITDA:  {_x(v.ev_to_ebitda)}",
        f"  • FCF Yield:  {_pct(v.fcf_yield)}",
    ]

    # ── Score ──
    lines += [
        "",
        f"🧮 *Score: {sc.total:.0f}/100*  {_bar(sc.total)}",
        f"  Business Quality:   {sc.business_quality:.0f}/35",
        f"  Financial Strength: {sc.financial_strength:.0f}/20",
        f"  Valuation:          {sc.valuation:.0f}/30",
        f"  Risk/Stability:     {sc.risk_stability:.0f}/15",
    ]

    # ── Verdict ──
    emoji = _VERDICT_EMOJI.get(a.verdict, "⚪")
    lines += [
        "",
        f"🏁 *Verdict: {emoji} {a.verdict}*",
    ]

    # Why (top 5 most informative reasons)
    top_reasons = a.verdict_reasons[:5]
    if top_reasons:
        lines += ["", "*Why:*"]
        for r in top_reasons:
            lines.append(f"  • {r}")

    # Warnings
    if a.warnings:
        lines += ["", "⚠️ *Warnings:*"]
        for w in a.warnings:
            lines.append(f"  • {w}")

    # Footer
    lines += [
        "",
        f"_{a.summary}_",
        f"_Data: {a.data_quality} | {a.analysis_date}_",
    ]

    return "\n".join(lines)
