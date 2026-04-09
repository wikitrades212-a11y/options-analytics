"""
Unusual Options Scoring Engine

Contracts are PRE-FILTERED to remove noise before any scoring.
Remaining contracts are scored with a weighted combination of signals,
each normalized to [0, 1] so no single raw metric dominates.

LOW-OI HANDLING (explicit):
  Contracts with open_interest < 50 are EXCLUDED before scoring.
  Very low OI (1–49) inflates vol_oi_ratio artificially: OI=1 with volume=100
  gives a ratio of 100×, which dominates after normalization even with VOL_OI_CAP.
  These contracts have negligible established market participation and produce
  false "unusual" signals. They are dropped unconditionally.
  To override, lower MIN_OI in unusual_engine.py.

VOL/OI CAP:
  vol_oi_ratio is capped at VOL_OI_CAP (50×) before global normalization.
  This prevents a single extreme ratio from compressing all other contracts.

PRE-FILTERS (all must pass to enter scoring):
  open_interest  >= MIN_OI          (default 1 — zero OI excluded)
  volume         >= MIN_VOLUME      (default 10)
  vol_notional   >= MIN_VOL_NOTIONAL (default $5k)
  spread_pct     <= MAX_SPREAD_PCT  (default 80% of mid)
  dte             in [MIN_DTE, MAX_DTE] (default 2–90 days)
  |delta|        >= MIN_DELTA_ABS   (default 0.05, only if delta present)

WEIGHTS (sum to 1.0):
  vol_notional_norm  0.30   Dollar flow today (primary signal)
  vol_oi_norm        0.25   Aggression ratio (post-filter, post-cap)
  oi_notional_norm   0.15   Established position size
  expiry_pct_rank    0.15   Unusual vs same-expiry peers
  global_pct_rank    0.10   Unusual vs entire chain
  atm_proximity      0.05   Slight preference for near-ATM

REASON TAGS (descriptive, not mutually exclusive):
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
    "vol_notional_norm": 0.30,   # dollar flow is the primary signal
    "vol_oi_norm":       0.25,   # aggression (zero-OI excluded + capped)
    "oi_notional_norm":  0.15,   # established position
    "expiry_pct_rank":   0.15,   # concentration within expiry
    "global_pct_rank":   0.10,   # global percentile
    "atm_proximity":     0.05,   # slight preference for near-ATM
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"

# ── Pre-filter thresholds ─────────────────────────────────────────────────────
MIN_OI            = 50         # open_interest >= 50; OI=1-5 inflates vol/OI ratio artificially
MIN_VOLUME        = 10         # at least 10 contracts traded today
MIN_VOL_NOTIONAL  = 5_000.0   # at least $5k in premium-volume flow
MAX_SPREAD_PCT    = 0.80       # (ask - bid) / mid <= 80%
MIN_DTE           = 2          # exclude 0DTE and next-day expirations
MAX_DTE           = 90         # exclude LEAPS and far-dated contracts
MIN_DELTA_ABS     = 0.05       # exclude deep-OTM junk (when delta present)

# ── Scoring constants ─────────────────────────────────────────────────────────
VOL_OI_CAP        = 50.0       # cap vol/OI before normalization
VOL_OI_HIGH       = 5.0        # "High Vol/OI" tag threshold
BIG_PREMIUM_VOL   = 500_000    # $500k vol_notional → "Big Premium"
BIG_PREMIUM_OI    = 5_000_000  # $5M oi_notional → "Big Premium"
EXPIRY_CONC_RANK  = 0.93       # top 7% within expiry
CALL_DOM_RANK     = 0.90       # top 10% globally
PUT_HEDGE_OI      = 5_000_000
PUT_HEDGE_VOL_OI  = 2.0
ATM_PCT           = 0.02       # within 2% of spot
OTM_PCT           = 0.10       # more than 10% OTM


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
    """
    Return True only if the contract meets all quality gates.
    Contracts that fail are excluded from scoring entirely.
    """
    # Zero-OI exclusion: vol_oi_ratio with OI=0 is just raw volume — unreliable.
    if contract.open_interest < MIN_OI:
        return False

    # Minimum trading activity
    if contract.volume < MIN_VOLUME:
        return False

    # Minimum dollar flow: removes penny options with tiny notional
    if contract.vol_notional < MIN_VOL_NOTIONAL:
        return False

    # Spread quality: wide spreads indicate illiquid / stale quotes
    if _spread_pct(contract) > MAX_SPREAD_PCT:
        return False

    # Expiry window: exclude 0DTE noise and far-dated LEAPS
    dte = _dte(contract.expiration)
    if not (MIN_DTE <= dte <= MAX_DTE):
        return False

    # Delta filter (only when available): exclude deep-OTM lottery tickets
    if contract.delta is not None and abs(contract.delta) < MIN_DELTA_ABS:
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
    """
    Return [0, 1] based on proximity to ATM.
    1.0 = exactly ATM, decays to ~0 at >20% OTM/ITM.
    """
    distance = abs(moneyness - 1.0)
    return math.exp(-((distance / 0.07) ** 2))


# ── Main entry point ──────────────────────────────────────────────────────────

def score_contracts(
    contracts: List[OptionContract],
    underlying_price: float,
) -> List[OptionContract]:
    """
    Pre-filter contracts, then annotate survivors with unusual_score,
    unusual_rank, and reason_tags. Returns survivors sorted by score desc.

    Contracts that fail pre-filtering are returned at the end with
    unusual_score=0, unusual_rank=0, reason_tags=[] so the full chain
    response still includes them.
    """
    if not contracts:
        return contracts

    # Partition: eligible for scoring vs. filtered out
    eligible = [c for c in contracts if _passes_prefilter(c)]
    filtered_out = [c for c in contracts if not _passes_prefilter(c)]

    # Use INFO so these lines appear in Railway logs without debug mode
    logger.info(
        "unusual_engine [%s]: MIN_OI=%d  contracts_in=%d  eligible=%d  dropped=%d",
        contracts[0].ticker if contracts else "?",
        MIN_OI, len(contracts), len(eligible), len(filtered_out),
    )
    low_oi_dropped = [c for c in filtered_out if c.open_interest < MIN_OI]
    logger.info(
        "unusual_engine [%s]: low-OI dropped=%d  (OI values: %s)",
        contracts[0].ticker if contracts else "?",
        len(low_oi_dropped),
        sorted(set(c.open_interest for c in low_oi_dropped))[:10],
    )

    if not eligible:
        logger.warning(
            "unusual_engine [%s]: no eligible contracts after pre-filter — returning unsorted chain",
            contracts[0].ticker if contracts else "?",
        )
        return contracts  # nothing to score; return as-is

    # Per-expiry groups for relative ranking
    expiry_groups: dict[str, list[int]] = {}
    for i, c in enumerate(eligible):
        expiry_groups.setdefault(c.expiration, []).append(i)

    # Raw signal arrays — vol/OI capped to prevent zero-OI (or any extreme)
    # from collapsing the normalized range for the rest of the chain.
    vol_oi_arr  = np.array(
        [min(c.vol_oi_ratio, VOL_OI_CAP) for c in eligible], dtype=float
    )
    vol_not_arr = np.array([c.vol_notional  for c in eligible], dtype=float)
    oi_not_arr  = np.array([c.oi_notional   for c in eligible], dtype=float)

    # Global min-max normalization
    vol_oi_norm  = _minmax_norm(vol_oi_arr)
    vol_not_norm = _minmax_norm(vol_not_arr)
    oi_not_norm  = _minmax_norm(oi_not_arr)

    scores: list[float] = []

    for i, contract in enumerate(eligible):
        # ── Signal 1: vol notional (dollar flow) ─────────────────────────────
        s_vol_not = vol_not_norm[i]

        # ── Signal 2: vol/OI ratio (aggression, capped) ───────────────────────
        s_vol_oi = vol_oi_norm[i]

        # ── Signal 3: OI notional (established interest) ──────────────────────
        s_oi_not = oi_not_norm[i]

        # ── Signal 4: percentile rank within same expiry ──────────────────────
        expiry_idxs = expiry_groups[contract.expiration]
        expiry_vol_oi = vol_oi_arr[expiry_idxs]
        s_expiry = _percentile_rank(vol_oi_arr[i], expiry_vol_oi)

        # ── Signal 5: global percentile rank ──────────────────────────────────
        s_global = _percentile_rank(vol_oi_arr[i], vol_oi_arr)

        # ── Signal 6: ATM proximity ───────────────────────────────────────────
        moneyness = (contract.strike / underlying_price) if underlying_price > 0 else 1.0
        contract.moneyness = round(moneyness, 4)
        contract.underlying_price = underlying_price
        s_atm = _atm_score(moneyness)

        raw = (
            WEIGHTS["vol_notional_norm"] * s_vol_not  +
            WEIGHTS["vol_oi_norm"]       * s_vol_oi   +
            WEIGHTS["oi_notional_norm"]  * s_oi_not   +
            WEIGHTS["expiry_pct_rank"]   * s_expiry   +
            WEIGHTS["global_pct_rank"]   * s_global   +
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

    # Normalize eligible scores to 0–100 relative to their own max
    max_score = max(scores) if scores else 1.0
    for i, contract in enumerate(eligible):
        contract.unusual_score = round((scores[i] / max_score) * 100, 2)

    # Rank (1 = highest) across eligible only
    ranked = sorted(range(len(eligible)), key=lambda i: eligible[i].unusual_score, reverse=True)
    for rank, idx in enumerate(ranked, start=1):
        eligible[idx].unusual_rank = rank

    scored_eligible = sorted(eligible, key=lambda c: c.unusual_score, reverse=True)
    logger.info(
        "unusual_engine [%s]: scoring done — top scores: %s",
        contracts[0].ticker if contracts else "?",
        [c.unusual_score for c in scored_eligible[:5]],
    )
    # Return eligible sorted by score, then unscored contracts appended
    return scored_eligible + filtered_out
