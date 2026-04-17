from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    # ── Data provider (Tradier only) ───────────────────────────────────────────
    tradier_token: str = ""
    tradier_sandbox: bool = False
    data_provider: str = "tradier"    # only supported value

    # ── App ───────────────────────────────────────────────────────────────────
    cache_ttl: int = 60
    rate_limit: int = 30
    cors_origins: str = "http://localhost:3000"

    # ── Scanner ───────────────────────────────────────────────────────────────
    scan_tickers: str = "SPY,QQQ,AAPL,TSLA,NVDA,AMZN,MSFT,META,AMD,GOOGL"
    scan_interval_minutes: int = 15
    scan_min_score: float = 60.0          # min unusual_score (0–100)
    scan_min_premium: float = 100_000.0   # min vol_notional USD
    scan_min_volume: int = 250
    scan_top_n: int = 5

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True

    # ── Credit Spread / LHF engine ────────────────────────────────────────────
    lhf_min_score: int = 80              # minimum LHF score to classify as LOW_HANGING_FRUIT
    spread_min_score: int = 70           # minimum base spread score to output a TAKE
    dedup_db_path: str = ""              # Railway: set to /data/dedup.db
    spread_db_path: str = ""            # Railway: set to /data/spread_trades.db

    # ── Social automation (optional) ──────────────────────────────────────────
    social_enabled: bool = False
    social_delay_minutes: int = 20
    social_live_update_enabled: bool = True
    social_platforms: str = "log"
    social_webhook_url: str = ""
    social_max_names_per_post: int = 3
    social_premarket_enabled: bool = True
    social_eod_enabled: bool = True
    social_sunday_enabled: bool = True

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
        case_sensitive = False
        extra = "ignore"    # silently drop unknown/legacy env vars (e.g. old RH_ keys)

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
