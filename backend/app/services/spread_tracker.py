"""
Spread trade result tracker.

Stores every TAKE spread in SQLite and tracks WIN/LOSS after expiration.
The DB path is set via SPREAD_DB_PATH env var (Railway: /data/spread_trades.db).
Falls back to ./spread_trades.db when unset.
"""
import logging
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_DB_PATH: Path = Path("./spread_trades.db")


def init_tracker() -> None:
    """Call once at startup. Sets DB path from settings and runs migrations."""
    global _DB_PATH
    if settings.spread_db_path:
        _DB_PATH = Path(settings.spread_db_path).resolve()
    else:
        warnings.warn(
            "SPREAD_DB_PATH is not set — using ephemeral ./spread_trades.db. "
            "Set SPREAD_DB_PATH=/data/spread_trades.db on Railway for persistence.",
            RuntimeWarning,
            stacklevel=2,
        )
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _migrate()
    logger.info("spread_tracker: DB at %s", _DB_PATH)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(str(_DB_PATH), check_same_thread=False)


def _migrate() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spread_trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at            TEXT    NOT NULL,
                ticker                TEXT    NOT NULL,
                spread_type           TEXT    NOT NULL,
                sell_strike           REAL    NOT NULL,
                buy_strike            REAL    NOT NULL,
                expiration            TEXT    NOT NULL,
                dte                   INTEGER NOT NULL,
                net_credit            REAL    NOT NULL,
                max_risk              REAL    NOT NULL,
                win_probability       REAL    NOT NULL,
                lhf_score             INTEGER DEFAULT 0,
                lhf_flow_clarity      INTEGER DEFAULT 0,
                lhf_structure         INTEGER DEFAULT 0,
                lhf_regime            INTEGER DEFAULT 0,
                lhf_premium           INTEGER DEFAULT 0,
                lhf_historical        INTEGER DEFAULT 0,
                classification        TEXT    DEFAULT 'UNKNOWN',
                bias                  TEXT    NOT NULL,
                -- Outcome (updated after expiration)
                result_at_expiry      TEXT    DEFAULT 'PENDING',
                expired_otm           INTEGER DEFAULT NULL,
                breached_short_strike INTEGER DEFAULT NULL,
                final_underlying      REAL    DEFAULT NULL,
                result_updated_at     TEXT    DEFAULT NULL
            )
        """)
        conn.commit()


def record_spread(spread) -> Optional[int]:
    """
    Persist a CreditSpreadResult to the DB.
    Returns the inserted row id, or None on failure.
    """
    from app.models.credit_spread import CreditSpreadResult
    s: CreditSpreadResult = spread

    lhf_score  = 0
    lhf_flow   = lhf_struct = lhf_regime = lhf_prem = lhf_hist = 0
    cls        = "UNKNOWN"

    if s.lhf:
        lhf_score  = s.lhf.score.total
        lhf_flow   = s.lhf.score.flow_clarity
        lhf_struct = s.lhf.score.structure_safety
        lhf_regime = s.lhf.score.regime
        lhf_prem   = s.lhf.score.premium_quality
        lhf_hist   = s.lhf.score.historical_edge
        cls        = s.lhf.classification

    try:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO spread_trades (
                    created_at, ticker, spread_type, sell_strike, buy_strike,
                    expiration, dte, net_credit, max_risk, win_probability,
                    lhf_score, lhf_flow_clarity, lhf_structure, lhf_regime,
                    lhf_premium, lhf_historical, classification, bias
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    s.ticker, s.spread_type, s.sell_strike, s.buy_strike,
                    s.expiration, s.dte, s.premium, s.max_risk, s.win_probability,
                    lhf_score, lhf_flow, lhf_struct, lhf_regime, lhf_prem, lhf_hist,
                    cls, s.bias,
                ),
            )
            conn.commit()
            return cur.lastrowid
    except Exception as exc:
        logger.error("spread_tracker.record_spread failed: %s", exc)
        return None


def update_result(
    ticker: str,
    expiration: str,
    sell_strike: float,
    *,
    expired_otm: bool,
    final_underlying: float,
) -> bool:
    """
    Update a PENDING trade's outcome after expiration.

    expired_otm=True  → short strike expired worthless → WIN
    expired_otm=False → short strike was breached       → LOSS
    """
    result   = "WIN" if expired_otm else "LOSS"
    breached = 0 if expired_otm else 1
    try:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE spread_trades
                SET result_at_expiry      = ?,
                    expired_otm           = ?,
                    breached_short_strike  = ?,
                    final_underlying      = ?,
                    result_updated_at     = ?
                WHERE ticker = ? AND expiration = ? AND sell_strike = ?
                  AND result_at_expiry = 'PENDING'
                """,
                (
                    result, int(expired_otm), breached,
                    final_underlying,
                    datetime.now(timezone.utc).isoformat(),
                    ticker, expiration, sell_strike,
                ),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.error("spread_tracker.update_result failed: %s", exc)
        return False


def win_rate_for(ticker: str, option_type: str) -> Optional[float]:
    """
    Historical win rate [0.0-1.0] for this ticker+direction.
    Returns None when fewer than 5 completed results exist.
    """
    like = "%Put%" if option_type == "put" else "%Call%"
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN result_at_expiry = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM spread_trades
                WHERE ticker = ?
                  AND spread_type LIKE ?
                  AND result_at_expiry != 'PENDING'
                """,
                (ticker, like),
            ).fetchone()
        total, wins = row
        if not total or total < 5:
            return None
        return wins / total
    except Exception:
        return None


def pending_trades() -> list[dict]:
    """Return all trades with result_at_expiry='PENDING'."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, spread_type, sell_strike, buy_strike,
                       expiration, net_credit, lhf_score, classification
                FROM spread_trades
                WHERE result_at_expiry = 'PENDING'
                ORDER BY expiration ASC
                """,
            ).fetchall()
        cols = [
            "id", "ticker", "spread_type", "sell_strike", "buy_strike",
            "expiration", "net_credit", "lhf_score", "classification",
        ]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        logger.error("spread_tracker.pending_trades failed: %s", exc)
        return []


def recent_performance(limit: int = 20) -> list[dict]:
    """Return recent completed trades for performance review."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT ticker, spread_type, sell_strike, expiration,
                       net_credit, lhf_score, classification,
                       result_at_expiry, expired_otm
                FROM spread_trades
                WHERE result_at_expiry != 'PENDING'
                ORDER BY result_updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        cols = [
            "ticker", "spread_type", "sell_strike", "expiration",
            "net_credit", "lhf_score", "classification",
            "result_at_expiry", "expired_otm",
        ]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        logger.error("spread_tracker.recent_performance failed: %s", exc)
        return []
