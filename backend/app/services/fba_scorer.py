"""
FBA Product Scorer — 0-100 composite score across 4 dimensions.

Dimensions (25 pts each):
  1. DEMAND      — BSR rank velocity + Google Trends momentum
  2. COMPETITION — review count / seller concentration (low = better)
  3. MARGIN      — estimated net margin after COGS + FBA fees
  4. LOGISTICS   — weight/size tier (light, small = better)

Classification:
  HIGH_OPPORTUNITY  ≥ 70
  MEDIUM_OPPORTUNITY 50-69
  LOW_OPPORTUNITY   < 50
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# ── FBA fee table (simplified tier model) ─────────────────────────────────────
# Real FBA fees depend on size tier + weight. These are 2024 approximations.
_FBA_FEES: list[tuple[float, float, float]] = [
    # (max_price, min_fee, per_lb_rate)  — price used as proxy for size
    (10.0,  3.22, 0.0),
    (20.0,  4.75, 0.0),
    (35.0,  5.40, 0.0),
    (50.0,  6.50, 0.0),
    (75.0,  8.00, 0.5),
    (150.0, 10.50, 0.8),
    (999.0, 14.00, 1.2),
]

_REFERRAL_PCT = 0.15   # Amazon referral fee (most categories)
_COGS_PCT     = 0.30   # Conservative COGS estimate as % of price


def _fba_fee(price: float) -> float:
    for max_p, base, _ in _FBA_FEES:
        if price <= max_p:
            return base
    return 14.00


def _estimated_margin(price: float) -> float:
    """Net margin fraction after referral + FBA fee + COGS."""
    if price <= 0:
        return 0.0
    cogs     = price * _COGS_PCT
    referral = price * _REFERRAL_PCT
    fba      = _fba_fee(price)
    net      = price - cogs - referral - fba
    return net / price


# ── Score breakdown model ──────────────────────────────────────────────────────

@dataclass
class FBAScoreBreakdown:
    demand:      float = 0.0   # 0-25
    competition: float = 0.0   # 0-25
    margin:      float = 0.0   # 0-25
    logistics:   float = 0.0   # 0-25

    @property
    def total(self) -> float:
        return round(self.demand + self.competition + self.margin + self.logistics, 1)


@dataclass
class FBAProduct:
    asin:            str
    title:           str
    category:        str
    price:           Optional[float]
    bsr_rank:        int
    bsr_gain_pct:    int        = 0
    is_mover:        bool       = False
    source:          str        = "bsr"
    url:             str        = ""

    # Enriched by scorer
    keyword:         str        = ""
    interest_score:  int        = 0    # Google Trends 0-100
    trend:           str        = "unknown"

    score:           FBAScoreBreakdown = field(default_factory=FBAScoreBreakdown)
    classification:  str        = "LOW_OPPORTUNITY"
    why:             list[str]  = field(default_factory=list)
    flags:           list[str]  = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "asin":           self.asin,
            "title":          self.title,
            "category":       self.category,
            "price":          self.price,
            "bsr_rank":       self.bsr_rank,
            "bsr_gain_pct":   self.bsr_gain_pct,
            "is_mover":       self.is_mover,
            "keyword":        self.keyword,
            "interest_score": self.interest_score,
            "trend":          self.trend,
            "score": {
                "demand":      self.score.demand,
                "competition": self.score.competition,
                "margin":      self.score.margin,
                "logistics":   self.score.logistics,
                "total":       self.score.total,
            },
            "classification": self.classification,
            "why":   self.why,
            "flags": self.flags,
            "url":   self.url,
        }


# ── Dimension scorers ──────────────────────────────────────────────────────────

def _score_demand(bsr_rank: int, bsr_gain_pct: int, interest_score: int, trend: str) -> tuple[float, list[str]]:
    """
    0-25 pts.
    BSR rank ≤ 25  → 15 pts, ≤ 100 → 12, ≤ 300 → 9, ≤ 500 → 6, else → 3
    Mover gain bonus: >500% → +8, >200% → +6, >100% → +4, >50% → +2
    Trends interest: ≥70 → +5, ≥40 → +3, rising → +2
    """
    pts: float = 0.0
    why: list[str] = []

    # BSR rank — higher base so products score without needing price/trends data
    if bsr_rank <= 25:
        pts += 15; why.append(f"Top-25 BSR (#{bsr_rank})")
    elif bsr_rank <= 100:
        pts += 12; why.append(f"Top-100 BSR (#{bsr_rank})")
    elif bsr_rank <= 300:
        pts += 9;  why.append(f"Top-300 BSR (#{bsr_rank})")
    elif bsr_rank <= 500:
        pts += 6;  why.append(f"Top-500 BSR (#{bsr_rank})")
    else:
        pts += 3

    # Mover velocity
    if bsr_gain_pct > 500:
        pts += 8; why.append(f"Explosive mover +{bsr_gain_pct}%")
    elif bsr_gain_pct > 200:
        pts += 6; why.append(f"Strong mover +{bsr_gain_pct}%")
    elif bsr_gain_pct > 100:
        pts += 4; why.append(f"Mover +{bsr_gain_pct}%")
    elif bsr_gain_pct > 50:
        pts += 2; why.append(f"Mild mover +{bsr_gain_pct}%")

    # Google Trends
    if interest_score >= 70:
        pts += 5; why.append(f"High search interest ({interest_score})")
    elif interest_score >= 40:
        pts += 3; why.append(f"Moderate search interest ({interest_score})")

    if trend == "rising":
        pts += 2; why.append("Trending up on Google")
    elif trend == "declining":
        pts -= 2

    return min(pts, 25.0), why


def _score_competition(bsr_rank: int, review_count: Optional[int] = None) -> tuple[float, list[str]]:
    """
    0-25 pts.
    Without review data (scraper limitation) we proxy on BSR rank band:
    - Top-100 = high competition = lower score
    - Top-200 = moderate
    - BSR 200-500 = lower competition = higher score
    Mover bonus already captured in demand; here we penalize deep competition.
    """
    pts: float = 0.0
    why: list[str] = []

    if review_count is not None:
        # Real data path (not used by current scraper but future-proofed)
        if review_count < 100:
            pts = 25; why.append(f"Low reviews ({review_count}) — weak competition")
        elif review_count < 300:
            pts = 18; why.append(f"Moderate reviews ({review_count})")
        elif review_count < 1000:
            pts = 10; why.append(f"High reviews ({review_count}) — competitive")
        else:
            pts = 3;  why.append(f"Very high reviews ({review_count}) — saturated")
    else:
        # Proxy via BSR rank band
        if bsr_rank <= 25:
            pts = 10; why.append("Top-25 — very competitive, needs strong differentiation")
        elif bsr_rank <= 100:
            pts = 14; why.append("Top-100 — proven market, manageable competition")
        elif bsr_rank <= 300:
            pts = 18; why.append("Top-300 — good demand/competition balance")
        else:
            pts = 22; why.append("Rank 300-500 — lower competition zone")

    return min(pts, 25.0), why


def _score_margin(price: Optional[float]) -> tuple[float, list[str]]:
    """
    0-25 pts.
    Ideal FBA price: $20-$50. Margin estimate drives the score.
    """
    pts: float = 0.0
    why: list[str] = []
    flags: list[str] = []

    if price is None or price <= 0:
        return 15.0, ["Price unknown — assuming typical FBA margin"]

    if price < 12:
        flags.append(f"Price ${price:.2f} too low — FBA fees will crush margin")
        return 2.0, flags
    if price > 100:
        flags.append(f"Price ${price:.2f} high — capital-intensive inventory")
        pts = 8; why.append("High-price item: good margin % but high MOQ risk")
        return pts, why + flags

    margin = _estimated_margin(price)
    margin_pct = round(margin * 100, 1)

    if margin >= 0.40:
        pts = 25; why.append(f"Excellent margin ~{margin_pct}%")
    elif margin >= 0.30:
        pts = 20; why.append(f"Strong margin ~{margin_pct}%")
    elif margin >= 0.20:
        pts = 13; why.append(f"Thin margin ~{margin_pct}% — needs volume")
    elif margin >= 0.10:
        pts = 6;  why.append(f"Very thin margin ~{margin_pct}%")
        flags.append("Margin below 20% — risky")
    else:
        pts = 0;  flags.append(f"Negative or near-zero margin ~{margin_pct}%")

    # Sweet-spot bonus ($20-$50)
    if 20 <= price <= 50:
        pts = min(pts + 3, 25); why.append("In sweet-spot price range $20-$50")

    return pts, why + flags


def _score_logistics(price: Optional[float], is_mover: bool) -> tuple[float, list[str]]:
    """
    0-25 pts.
    Without weight data we proxy on price (higher price often = larger/heavier).
    Movers get a slight bonus (proven demand velocity = easier inventory turns).
    """
    pts: float = 0.0
    why: list[str] = []

    if price is None:
        pts = 15; why.append("No price data — assuming standard size/weight")
    elif price <= 25:
        pts = 22; why.append("Light/small item likely — low FBA fee tier")
    elif price <= 50:
        pts = 18; why.append("Mid-size item — standard FBA tier")
    elif price <= 100:
        pts = 12; why.append("Larger item — watch FBA storage fees")
    else:
        pts = 6;  why.append("Likely bulky/heavy — high FBA fees + storage risk")

    if is_mover:
        pts = min(pts + 3, 25); why.append("Fast-moving inventory — lower storage risk")

    return pts, why


# ── Main scorer ────────────────────────────────────────────────────────────────

def score_product(raw: dict, trends: dict[str, dict] | None = None) -> FBAProduct:
    """
    Convert a raw scraper dict into a scored FBAProduct.
    trends: output from fba_scraper.fetch_trends_batch keyed by keyword.
    """
    from app.services.fba_scraper import _extract_keywords

    price        = raw.get("price")
    bsr_rank     = raw.get("bsr_rank", 999)
    bsr_gain_pct = raw.get("bsr_gain_pct", 0)
    is_mover     = raw.get("is_mover", False)
    title        = raw.get("title", "")

    keyword = _extract_keywords(title)
    trend_data = (trends or {}).get(keyword, {})
    interest_score = trend_data.get("interest_score", 0)
    trend          = trend_data.get("trend", "unknown")

    d_pts, d_why = _score_demand(bsr_rank, bsr_gain_pct, interest_score, trend)
    c_pts, c_why = _score_competition(bsr_rank)
    m_pts, m_why = _score_margin(price)
    l_pts, l_why = _score_logistics(price, is_mover)

    breakdown = FBAScoreBreakdown(
        demand=round(d_pts, 1),
        competition=round(c_pts, 1),
        margin=round(m_pts, 1),
        logistics=round(l_pts, 1),
    )

    all_why   = [w for w in (d_why + c_why + m_why + l_why) if not _is_flag(w)]
    all_flags = [w for w in (d_why + c_why + m_why + l_why) if _is_flag(w)]

    total = breakdown.total
    if total >= 65:
        classification = "HIGH_OPPORTUNITY"
    elif total >= 45:
        classification = "MEDIUM_OPPORTUNITY"
    else:
        classification = "LOW_OPPORTUNITY"

    return FBAProduct(
        asin=raw.get("asin", ""),
        title=title,
        category=raw.get("category", ""),
        price=price,
        bsr_rank=bsr_rank,
        bsr_gain_pct=bsr_gain_pct,
        is_mover=is_mover,
        source=raw.get("source", "bsr"),
        url=raw.get("url", f"https://www.amazon.com/dp/{raw.get('asin','')}"),
        keyword=keyword,
        interest_score=interest_score,
        trend=trend,
        score=breakdown,
        classification=classification,
        why=all_why[:6],
        flags=all_flags[:3],
    )


def _is_flag(text: str) -> bool:
    """Heuristic: flags are negative/warning statements."""
    triggers = ["risk", "crush", "thin", "high fees", "negative", "below", "capital", "saturated", "watch"]
    return any(t in text.lower() for t in triggers)


def score_all(
    products: list[dict],
    trends: dict[str, dict] | None = None,
    min_score: float = 0.0,
) -> list[FBAProduct]:
    """
    Score a list of raw product dicts.
    Returns scored products sorted by total score DESC, filtered by min_score.
    """
    scored = []
    for raw in products:
        try:
            p = score_product(raw, trends)
            if p.score.total >= min_score:
                scored.append(p)
        except Exception as exc:
            logger.warning("score_product failed for %s: %s", raw.get("asin"), exc)

    scored.sort(key=lambda p: p.score.total, reverse=True)
    logger.info(
        "score_all: %d products scored, %d above min_score=%.0f",
        len(products), len(scored), min_score
    )
    return scored
