from .base import OptionsDataProvider
from .tradier import TradierProvider
from app.config import settings


def get_provider() -> OptionsDataProvider:
    """Factory: return the configured provider singleton. Only Tradier is supported."""
    if settings.data_provider.lower() != "tradier":
        raise ValueError(
            f"Unsupported provider '{settings.data_provider}'. "
            "Only 'tradier' is supported. Set DATA_PROVIDER=tradier."
        )
    return TradierProvider()


# Module-level singleton — shared across all requests
provider: OptionsDataProvider = get_provider()
