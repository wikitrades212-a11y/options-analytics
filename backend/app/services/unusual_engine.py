"""
Unusual Options Scoring Engine — Tradable Flow Edition

Contracts are PRE-FILTERED to remove noise before any scoring.
Remaining contracts are scored with a weighted combination of signals,
each normalized to [0, 1] so no single raw metric dominates.

PRE-FILTERS (all must pass to enter scoring):
  open_interest  >= MIN_OI           (default 100)
  volume         >= MIN_VOLUME       (default 250)
  vol_notional   >= MIN_VOL_NOTIONAL (default $100k)
  spread_pct     <= MAX_SPREAD_PCT   (default 20% of mid)
  dte             in [MIN_DTE, MAX_DTE] (default 3–45 days)
  |delta|         in [MIN_DELTA_ABS, MAX_DELTA_ABS] (default 0.20–0.70)

WEIGHTS (sum to 1.0):
  vol_notional_norm  0.30   Dollar flow today (primary signal)
  vol_oi_norm        0.25   Aggression ratio (post-filter, post-cap)
  oi_notional_norm   0.15   Established position size
  expiry_pct_rank    0.15   Unusual vs same-expiry peers
  global_pct_rank    0.10   Unusual vs entire chain
  atm_proximity      0.05   Slight preference for near-ATM

CONTRACT CLASSIFICATION:
  actionable   — tradeable delta, tight spread, meaningful premium, near-term
  watchlist    — interesting but not immediately tradeable
  lottery      — far OTM, weak delta, speculative
  hedge_like   — large put position with low vol/OI (institutional hedge)

CONVICTION SCORING (conviction_score 0–100, conviction_grade A/B/C/Ignore):
  liquidity_quality  0.15   Volume absolute level
  spread_quality     0.20   Spread tightness
  delta_usefulness   0.20   Delta in 0.30–0.60 sweet spot
  oi_quality         0.20   Vol/OI participation ratio
  premium_size       0.15   Premium notional size
  near_term_rel      0.10   DTE proximity (7–21 days ideal)

REASON TAGS:
  High Vol/OI          vol_oi_ratio > 5× or top 5% per expiry
  Big Premium          vol_notional > $500k or oi_notional > $5M
  Expiry Concentration expiry_pct_rank > 0.93
  Call Dominance       call with global_pct_rank > 0.90
  Put Hedge            put with oi_notional > $5M and vol_oi_ratio < 2
  Near ATM Aggression  moneyness within 2% of spot
  Far OTM Lottery      moneyness > 10% OTM with high vol_oi_ratio
"""

import math
import logging
from datetime import date
from typing import List

import numpy as np

from app.models.options import OptionContract

logger = logging.getLogger(__name__)

# ── Weights ──────────────────────────────────────────────────────────────────
WEIGHTS = {
    "vol_notional_norm": 0.30,
    "vol_oi_norm":       0.25,
    "oi_notional_norm":  0.15,
    "expiry_pct_rank":   0.15,
    "global_pct_rank":   0.10,
    "atm_proximity":     0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"

# ── Pre-filter thresholds ─────────────────────────────────────────────────────
MIN_OI            = 100        # open_interest >= 100
MIN_VOLUME        = 250        # at least 250 contracts traded today
MIN_VOL_NOTIONAL  = 100_000.0  # at least $100k in premium-volume flow
MAX_SPREAD_PCT    = 0.20       # (ask - bid) / mid <= 20%
MIN_DTE           = 3          # exclude 0-2 DTE noise
MAX_DTE           = 45         # exclude LEAPS and far-dated contracts
MIN_DELTA_ABS     = 0.20       # exclude deep-OTM
MAX_DELTA_ABS     = 0.70       # exclude deep-ITM (delta > 0.70 is not options flow)

# ── Scoring constants ─────────────────────────────────────────────────────────
VOL_OI_CAP        = 50.0
VOL_OI_HIGH       = 5.0
BIG_PREMIUM_VOL   = 500_000
BIG_PREMIUM_OI    = 5_000_000
EXPIRY_CONC_RANK  = 0.93
CALL_DOM_RANK     = 0.90
PUT_HEDGE_OI      = 5_000_000
PUT_HEDGE_VOL_OI  = 2.0
ATM_PCT           = 0.02
OTM_PCT           = 0.10

# ── Conviction scoring weights ────────────────────────────────────────────────
_CONVICTION_W = {
    "liquidity": 0.15,
    "spread":    0.20,
    "delta":     0.20,
    "oi":        0.20,
    "premium":   0.15,
    "relevance": 0.10,
}
assert abs(sum(_CONVICTION_W.values()) - 1.0) < 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dte(expiration: str) -> int:
    """Days to expiration from today. Returns 999 on parse error."""
    try:
        exp = date.fromisoformat(expiration)
        return (exp - date.today()).days
    except (ValueError, TypeError):
        return 999


def _spread_pct(contract: OptionContract) -> float:
    """(ask - bid) / mid. Returns 0 if mid is zero (skip spread filter)."""
    mid = contract.mid
    if mid <= 0:
        return 0.0
    return (contract.ask - contract.bid) / mid


def _passes_prefilter(contract: OptionContract) -> bool:
    """Return True only if the contract meets all quality gates."""
    if contract.open_interest < MIN_OI:
        return False
    if contract.volume < MIN_VOLUME:
        return False
    if contract.vol_notional < MIN_VOL_NOTIONAL:
        return False
    if _spread_pct(contract) > MAX_SPREAD_PCT:
        return False
    dte = _dte(contract.expiration)
    if not (MIN_DTE <= dte <= MAX_DTE):
        return False
    if contract.delta is not None:
        d = abs(contract.delta)
        if d < MIN_DELTA_ABS or d > MAX_DELTA_ABS:
            return False
    return True


def _percentile_rank(value: float, arr: np.ndarray) -> float:
    """Fraction of arr that is <= value. Returns [0, 1]."""
    if len(arr) == 0:
        return 0.0
    return float(np.mean(arr <= value))


def _minmax_norm(arr: np.ndarray) -> np.ndarray:
    """Min-max normalize to [0, 1]. Returns zeros if range is zero."""
    lo, hi = arr.min(), arr.max()
    if hi - lo < 1e-9:
        return np.zeros_like(arr, dtype=float)
    return (arr - lo) / (hi - lo)


def _atm_score(moneyness: float) -> float:
    """Return [0, 1] based on proximity to ATM. 1.0 = exactly ATM."""
    distance = abs(moneyness - 1.0)
    return math.exp(-((distance / 0.07) ** 2))


# ── Contract classification ───────────────────────────────────────────────────

def classify_contract(
    contract: OptionContract,
    moneyness: float,
    dte: int,
) -> str:
    """
    Classify each contract for tradability.

    actionable   — good delta, tight spread, meaningful premium, near-term
    watchlist    — worth monitoring but not immediately tradeable
    lottery      — far OTM, speculative
    hedge_like   — institutional put protection (large OI, low vol/OI)
    """
    delta_abs = abs(contract.delta) if contract.delta is not None else 0.5
    spread    = _spread_pct(contract)
    dist_pct  = abs(moneyness - 1.0)

    # Institutional hedge: large put OI, low turnover
    if (contract.option_type == "put"
            and contract.oi_notional >= PUT_HEDGE_OI
            and contract.vol_oi_ratio < PUT_HEDGE_VOL_OI):
        return "hedge_like"

    # Lottery: far OTM with weak delta
    if dist_pct >= OTM_PCT and delta_abs < 0.25:
        return "lottery"

    # Actionable: delta in sweet spot, tight spread, real premium, near expiry
    if (0.25 <= delta_abs <= 0.65
            and spread <= 0.12
            and contract.vol_notional >= 150_000
            and dte <= 30):
        return "actionable"

    return "watchlist"


# ── Conviction scoring ────────────────────────────────────────────────────────

def score_conviction(
    contract: OptionContract,
    dte: int,
) -> tuple[float, str]:
    """
    Compute conviction_score [0–100] and grade (A / B / C / Ignore).

    Sub-scores (each 0–1):
      liquidity  — volume absolute level (scaled to 2000 contracts)
      spread     — spread quality (0% = 1.0, MAX_SPREAD_PCT = 0.0)
      delta      — 0.30–0.60 ideal range
      oi         — vol/OI participation (capped at 20x)
      premium    — premium notional (scaled to $500k)
      relevance  — DTE proximity (7–21 days = ideal)
    """
    # Liquidity
    liquidity = min(contract.volume / 2_000, 1.0)

    # Spread quality
    spread = _spread_pct(contract)
    spread_q = max(0.0, 1.0 - spread / MAX_SPREAD_PCT)

    # Delta usefulness
    if contract.delta is not None:
        d = abs(contract.delta)
        if 0.30 <= d <= 0.60:
            delta_q = 1.0
        elif 0.20 <= d < 0.30 or 0.60 < d <= 0.70:
            delta_q = 0.6
        else:
            delta_q = 0.2
    else:
        delta_q = 0.5

    # OI participation
    vol_oi = min(contract.vol_oi_ratio, 20.0)
    oi_q = min(vol_oi / 10.0, 1.0)

    # Premium size
    premium_q = min(contract.vol_notional / 500_000, 1.0)

    # Near-term relevance
    if 7 <= dte <= 21:
        rel_q = 1.0
    elif 22 <= dte <= 30:
        rel_q = 0.8
    elif 3 <= dte < 7:
        rel_q = 0.6
    elif 31 <= dte <= 45:
        rel_q = 0.4
    else:
        rel_q = 0.1

    raw = (
        _CONVICTION_W["liquidity"] * liquidity  +
        _CONVICTION_W["spread"]    * spread_q   +
        _CONVICTION_W["delta"]     * delta_q    +
        _CONVICTION_W["oi"]        * oi_q       +
        _CONVICTION_W["premium"]   * premium_q  +
        _CONVICTION_W["relevance"] * rel_q
    )
    score = round(raw * 100, 1)

    if score >= 70:
        grade = "A"
    elif score >= 50:
        grade = "B"
    elif score >= 30:
        grade = "C"
    else:
        grade = "Ignore"

    return score, grade


# ── Main entry point ──────────────────────────────────────────────────────────

def score_contracts(
    contracts: List[OptionContract],
    underlying_price: float,
) -> List[OptionContract]:
    """
    Pre-filter contracts, then annotate survivors with unusual_score,
    unusual_rank, reason_tags, conviction_score, conviction_grade,
    and contract_class. Returns survivors sorted by score desc.

    Contracts that fail pre-filtering are returned at the end with
    unusual_score=0, unusual_rank=0, reason_tags=[] so the full chain
    response still includes them.
    """
    if not contracts:
        return contracts

    eligible    = [c for c in contracts if _passes_prefilter(c)]
    filtered_out = [c for c in contracts if not _passes_prefilter(c)]

    logger.info(
        "unusual_engine [%s]: MIN_OI=%d  contracts_in=%d  eligible=%d  dropped=%d",
        contracts[0].ticker if contracts else "?",
        MIN_OI, len(contracts), len(eligible), len(filtered_out),
    )

    if not eligible:
        logger.warning(
            "unusual_engine [%s]: no eligible contracts after pre-filter",
            contracts[0].ticker if contracts else "?",
        )
        return contracts

    # Per-expiry groups for relative ranking
    expiry_groups: dict[str, list[int]] = {}
    for i, c in enumerate(eligible):
        expiry_groups.setdefault(c.expiration, []).append(i)

    vol_oi_arr  = np.array(
        [min(c.vol_oi_ratio, VOL_OI_CAP) for c in eligible], dtype=float
    )
    vol_not_arr = np.array([c.vol_notional for c in eligible], dtype=float)
    oi_not_arr  = np.array([c.oi_notional  for c in eligible], dtype=float)

    vol_oi_norm  = _minmax_norm(vol_oi_arr)
    vol_not_norm = _minmax_norm(vol_not_arr)
    oi_not_norm  = _minmax_norm(oi_not_arr)

    scores: list[float] = []

    for i, contract in enumerate(eligible):
        s_vol_not = vol_not_norm[i]
        s_vol_oi  = vol_oi_norm[i]
        s_oi_not  = oi_not_norm[i]

        expiry_idxs  = expiry_groups[contract.expiration]
        expiry_vol_oi = vol_oi_arr[expiry_idxs]
        s_expiry = _percentile_rank(vol_oi_arr[i], expiry_vol_oi)
        s_global = _percentile_rank(vol_oi_arr[i], vol_oi_arr)

        moneyness = (contract.strike / underlying_price) if underlying_price > 0 else 1.0
        contract.moneyness = round(moneyness, 4)
        contract.underlying_price = underlying_price
        s_atm = _atm_score(moneyness)

        raw = (
            WEIGHTS["vol_notional_norm"] * s_vol_not +
            WEIGHTS["vol_oi_norm"]       * s_vol_oi  +
            WEIGHTS["oi_notional_norm"]  * s_oi_not  +
            WEIGHTS["expiry_pct_rank"]   * s_expiry  +
            WEIGHTS["global_pct_rank"]   * s_global  +
            WEIGHTS["atm_proximity"]     * s_atm
        )
        scores.append(raw)

        # ── Reason tags ───────────────────────────────────────────────────────
        tags: list[str] = []
        distance_pct = abs(moneyness - 1.0)

        if contract.vol_oi_ratio >= VOL_OI_HIGH or s_expiry >= EXPIRY_CONC_RANK:
            tags.append("High Vol/OI")

        if contract.vol_notional >= BIG_PREMIUM_VOL or contract.oi_notional >= BIG_PREMIUM_OI:
            tags.append("Big Premium")

        if s_expiry >= EXPIRY_CONC_RANK:
            tags.append("Expiry Concentration")

        if contract.option_type == "call" and s_global >= CALL_DOM_RANK:
            tags.append("Call Dominance")

        if (contract.option_type == "put"
                and contract.oi_notional >= PUT_HEDGE_OI
                and contract.vol_oi_ratio < PUT_HEDGE_VOL_OI):
            tags.append("Put Hedge")

        if distance_pct <= ATM_PCT and contract.volume > 0:
            tags.append("Near ATM Aggression")

        if (distance_pct >= OTM_PCT
                and contract.vol_oi_ratio >= VOL_OI_HIGH
                and contract.option_type == "call"):
            tags.append("Far OTM Lottery")

        if not tags and raw > 0.4:
            tags.append("Unusual Activity")

        contract.reason_tags = tags

        # ── Conviction + classification ───────────────────────────────────────
        dte = _dte(contract.expiration)
        contract.conviction_score, contract.conviction_grade = score_conviction(contract, dte)
        contract.contract_class = classify_contract(contract, moneyness, dte)

    # Normalize eligible scores to 0–100 relative to their own max
    max_score = max(scores) if scores else 1.0
    for i, contract in enumerate(eligible):
        contract.unusual_score = round((scores[i] / max_score) * 100, 2)

    ranked = sorted(range(len(eligible)), key=lambda i: eligible[i].unusual_score, reverse=True)
    for rank, idx in enumerate(ranked, start=1):
        eligible[idx].unusual_rank = rank

    scored_eligible = sorted(eligible, key=lambda c: c.unusual_score, reverse=True)
    logger.info(
        "unusual_engine [%s]: scoring done — top scores: %s",
        contracts[0].ticker if contracts else "?",
        [c.unusual_score for c in scored_eligible[:5]],
    )
    return scored_eligible + filtered_out
