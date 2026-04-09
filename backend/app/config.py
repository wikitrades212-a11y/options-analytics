from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    rh_username: str = ""
    rh_password: str = ""
    rh_mfa_secret: str = ""
    # Base64-encoded robinhood.pickle for cloud deployments (no persistent disk)
    rh_pickle_b64: str = ""

    tradier_token: str = ""
    tradier_sandbox: bool = False

    cache_ttl: int = 60
    data_provider: str = "tradier"
    cors_origins: str = "http://localhost:3000"
    rate_limit: int = 30

    # ── Scanner ───────────────────────────────────────────────────────────────
    scan_tickers: str = "SPY,QQQ,AAPL,TSLA,NVDA,AMZN,MSFT,META,AMD,GOOGL"
    scan_interval_minutes: int = 15      # how often the scheduler fires
    scan_min_score: float = 60.0          # minimum unusual_score (0–100)
    scan_min_premium: float = 100_000.0  # minimum vol_notional in USD
    scan_min_volume: int = 250           # minimum contract volume
    scan_top_n: int = 5                  # top N contracts kept per ticker

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
        case_sensitive = False

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
