"""
Options Analytics API — FastAPI entrypoint.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.routers import options_router, calculator_router, scanner_router, credit_spread_router, fba_router
from app.providers import provider
from app.services.scanner_service import start_scheduler, stop_scheduler, start_fba_scheduler, stop_fba_scheduler
from app.services.futures_service import start_futures_scheduler, stop_futures_scheduler
from app.services.social_service import start_social_scheduler, stop_social_scheduler
from app.services.telegram_bot import start_bot, stop_bot
from app.services.spread_tracker import init_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_provider_ready: Optional[bool] = None


async def _warmup_provider():
    """Background task: check provider health without blocking startup."""
    global _provider_ready
    try:
        ok = await provider.health_check()
        _provider_ready = ok
        if ok:
            logger.info("Provider health check passed.")
        else:
            logger.warning("Provider health check failed — check credentials.")
    except Exception as exc:
        _provider_ready = False
        logger.warning(f"Provider warmup error (non-fatal): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks; provider warmup runs concurrently so uvicorn binds immediately."""
    logger.info(f"Starting with provider: {settings.data_provider}")

    # Initialise persistent stores
    init_tracker()

    asyncio.create_task(_warmup_provider())
    start_scheduler()
    start_fba_scheduler()
    start_futures_scheduler()
    start_social_scheduler()
    start_bot()          # Telegram command bot (long-polling)

    yield

    stop_bot()
    stop_scheduler()
    stop_fba_scheduler()
    stop_futures_scheduler()
    stop_social_scheduler()
    logger.info("Shutting down.")


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.rate_limit}/minute"],
)

app = FastAPI(
    title="Options Analytics API",
    description="Production-grade options flow analysis powered by Tradier.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(options_router)
app.include_router(calculator_router)
app.include_router(scanner_router)
app.include_router(credit_spread_router)
app.include_router(fba_router)


@app.get("/health", tags=["meta"])
async def health():
    if _provider_ready is None:
        readiness = "starting"
    elif _provider_ready:
        readiness = "ready"
    else:
        readiness = "unavailable"
    return {"status": "ok", "provider": settings.data_provider, "readiness": readiness}


@app.get("/", tags=["meta"])
async def root():
    return {
        "name": "Options Analytics API",
        "version": "1.0.0",
        "docs": "/docs",
    }
