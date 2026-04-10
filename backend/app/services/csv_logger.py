"""
CSV logger for Telegram alerts.

Appends every alert dispatched (or attempted) to data/telegram_alerts_log.csv
relative to the project root. Safe to call from async context — all I/O is
synchronous but wrapped so failures never block or crash the main flow.
"""
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve project root as the parent of the `backend/` directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CSV_PATH = _PROJECT_ROOT / "data" / "telegram_alerts_log.csv"

_FIELDNAMES = [
    "sent_at",
    "ticker",
    "strike",
    "expiration",
    "option_type",
    "bias",
    "conviction_score",
    "conviction_grade",
    "unusual_score",
    "volume",
    "open_interest",
    "vol_oi_ratio",
    "mark",
    "underlying_price",
    "telegram_sent",
    "message_id",
    "chat_id",
    "scan_timestamp",
]


def _ensure_file(path: Path) -> None:
    """Create parent dirs and write CSV header if the file does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
            writer.writeheader()


def log_alerts_to_csv(
    alerts: list[dict],
    telegram_sent: bool,
    *,
    message_id: int | None = None,
    chat_id: str | int | None = None,
) -> None:
    """
    Append *alerts* to the CSV log.

    Parameters
    ----------
    alerts:
        List of alert dicts as produced by the scanner. Each dict is expected
        to contain a ``"contract"`` key holding an ``OptionContract`` instance
        plus ``"bias"``, ``"underlying_price"``, and optionally
        ``"scan_timestamp"``.
    telegram_sent:
        True if the Telegram POST succeeded; False otherwise.
    message_id:
        Optional Telegram message_id returned by the API.
    chat_id:
        Optional Telegram chat_id used for the send.
    """
    if not alerts:
        return

    try:
        _ensure_file(_CSV_PATH)
        sent_at = datetime.now(timezone.utc).isoformat()

        with _CSV_PATH.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES, extrasaction="ignore")
            for alert in alerts:
                contract = alert.get("contract", alert)

                # Support both Pydantic model and plain dict for contract.
                def _get(field: str, default=None):
                    if hasattr(contract, field):
                        return getattr(contract, field, default)
                    if isinstance(contract, dict):
                        return contract.get(field, default)
                    return default

                row = {
                    "sent_at":          sent_at,
                    "ticker":           _get("ticker", ""),
                    "strike":           _get("strike", ""),
                    "expiration":       _get("expiration", ""),
                    "option_type":      _get("option_type", ""),
                    "bias":             alert.get("bias", ""),
                    "conviction_score": _get("conviction_score", ""),
                    "conviction_grade": _get("conviction_grade", ""),
                    "unusual_score":    _get("unusual_score", ""),
                    "volume":           _get("volume", ""),
                    "open_interest":    _get("open_interest", ""),
                    "vol_oi_ratio":     _get("vol_oi_ratio", ""),
                    "mark":             _get("mark", ""),
                    "underlying_price": alert.get("underlying_price") or _get("underlying_price", ""),
                    "telegram_sent":    telegram_sent,
                    "message_id":       message_id if message_id is not None else "",
                    "chat_id":          chat_id if chat_id is not None else "",
                    "scan_timestamp":   alert.get("scan_timestamp", ""),
                }
                writer.writerow(row)

        logger.debug(
            "csv_logger: wrote %d row(s) to %s (telegram_sent=%s)",
            len(alerts),
            _CSV_PATH,
            telegram_sent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("csv_logger: failed to write alerts log — %s", exc)
