"""FastAPI application: composition root and entry point.

Startup is ordered so that failures degrade instead of aborting:

1. Load and validate settings. This is the only fatal step, because running with
   unvalidated risk ceilings or an unparseable TOTP seed is worse than not
   running.
2. Configure logging and register the credentials for redaction, before any
   component can log.
3. Load the instrument master. Failure disables F&O features only.
4. Authenticate. Failure enters degraded mode; the server still serves.
5. Start the cache updater, which retries the broker on its own cadence.

Run it:
    python -m api.server
    uvicorn api.server:app --host 127.0.0.1 --port 8000

Both must run from the ``trading_system`` directory, since imports are rooted
there.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Allow `python api/server.py` in addition to `python -m api.server`, so a
# newcomer following the README does not hit an ImportError on the first run.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from api.dependencies import AppState  # noqa: E402
from api.endpoints import (  # noqa: E402
    account,
    analysis,
    derivatives,
    health,
    instruments,
    market,
    signals,
    snapshot,
)
from config.instruments import InstrumentMaster  # noqa: E402
from config.settings import ConfigurationError, Settings, get_settings  # noqa: E402
from core.angel_client import AngelClient  # noqa: E402
from core.cache_manager import CacheUpdater, MarketCache  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from core.exceptions import TradingSystemError  # noqa: E402
from core.market_hours import now_ist  # noqa: E402
from utils.logging_config import register_secrets, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)

API_TITLE = "Trading System Data Layer"
API_VERSION = "1.0.0-phase1"
API_DESCRIPTION = """
Phase 1 data layer for an Indian equity and derivatives decision-support system.

**Read-only.** No order placement exists in this phase. Live execution is Phase 6
and is gated behind order validation, risk checks, and a kill switch.

### Guarantees

* **Never fabricates data.** Unavailable values are `null` with an explicit error
  code, never a placeholder. A missing price stays missing so that downstream
  sizing cannot act on an invented number.
* **Serves stale data, flagged.** Every response carries `cache.age_seconds` and
  `cache.is_stale`. `/data` adds `analysis_permitted`, which is false when stale.
* **Degrades rather than fails.** A broker outage leaves the server answering
  with documented shapes and null payloads. `/health` always returns 200.

### Getting oriented

`/health` for state, `/diagnostics` for actionable setup checks, `/data` for the
full JSON snapshot, `/text` for a paste-ready report.
"""


def build_state(settings: Settings) -> AppState:
    """Construct the service graph.

    Args:
        settings: Validated configuration.

    Returns:
        A fully wired :class:`AppState`. No network calls are made here.
    """
    client = AngelClient(settings)
    master = InstrumentMaster(settings.data_dir_path)
    fetcher = DataFetcher(client, master)
    cache = MarketCache(stale_threshold=settings.CACHE_STALE_THRESHOLD)
    updater = CacheUpdater(
        cache=cache,
        fetcher=fetcher,
        active_interval=settings.CACHE_UPDATE_INTERVAL,
        idle_interval=max(settings.CACHE_UPDATE_INTERVAL, 60),
        candle_bars=settings.CACHE_CANDLE_BARS,
    )
    return AppState(settings, client, master, fetcher, cache, updater)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown.

    Args:
        app: The application instance.

    Yields:
        Control while the server runs.

    Raises:
        ConfigurationError: When settings are invalid. This is the only fatal
            startup condition.
    """
    settings = get_settings()

    setup_logging(
        log_level=settings.LOG_LEVEL,
        log_dir=settings.log_dir_path,
        console=settings.LOG_CONSOLE,
    )

    # Register before any component can log. From here a credential cannot
    # reach a log file even via a third-party library or a traceback.
    register_secrets(
        settings.ANGEL_API_KEY.get_secret_value(),
        settings.ANGEL_CLIENT_ID.get_secret_value(),
        settings.ANGEL_PASSWORD.get_secret_value(),
        settings.ANGEL_TOTP_SECRET.get_secret_value(),
        settings.API_AUTH_KEY.get_secret_value(),
        settings.ANGEL_HTTPS_PROXY.get_secret_value(),
    )

    logger.info("=" * 70)
    logger.info("%s %s starting", API_TITLE, API_VERSION)
    logger.info("Mode: %s", settings.TRADING_MODE)
    logger.info(
        "Risk: capital %.2f | %.2f%%/trade (%.2f) | daily %.2f%% | weekly %.2f%% | max %d positions",
        settings.TOTAL_CAPITAL,
        settings.RISK_PER_TRADE_PCT,
        settings.risk_amount_per_trade,
        settings.MAX_DAILY_LOSS_PCT,
        settings.MAX_WEEKLY_LOSS_PCT,
        settings.MAX_OPEN_POSITIONS,
    )
    logger.info("Auth on API: %s", "enabled" if settings.auth_enabled else "DISABLED")
    logger.info("=" * 70)

    state = build_state(settings)
    app.state.services = state

    # -- Instrument master: non-fatal ----------------------------------
    try:
        if await asyncio.to_thread(state.master.load):
            lot_sizes = await asyncio.to_thread(state.master.refresh_lot_sizes)
            logger.info("Effective lot sizes: %s", lot_sizes)
            failed = [k for k, ok in state.master.verify_core_tokens().items() if not ok]
            if failed:
                logger.warning(
                    "Index tokens not found in the master: %s. Quotes for these will "
                    "be unreliable; update CORE_INSTRUMENTS.",
                    ", ".join(failed),
                )
        else:
            logger.warning(
                "Instrument master unavailable. Option chains, futures, and non-index "
                "symbol lookup are disabled for this run."
            )
    except Exception as exc:  # noqa: BLE001 - must not prevent startup
        logger.error("Instrument master load raised: %s", exc, exc_info=True)

    # -- Broker session: non-fatal -------------------------------------
    if settings.credentials_present:
        try:
            if await asyncio.to_thread(state.client.connect):
                logger.info("Broker session established")
            else:
                logger.warning(
                    "Starting in DEGRADED mode: %s", state.client.last_error or "login failed"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Broker connect raised: %s", exc, exc_info=True)
    else:
        logger.warning(
            "Starting in DEGRADED mode: credentials not configured (%s). "
            "Endpoints will respond with null data.",
            ", ".join(settings.missing_credentials),
        )

    await state.updater.start()

    logger.info("Ready on http://%s:%d  (docs at /docs)", settings.API_HOST, settings.API_PORT)

    try:
        yield
    finally:
        # Order matters: stop polling before dropping the session, so an
        # in-flight fetch cannot trigger a reconnect during shutdown.
        logger.info("Shutting down")
        await state.updater.stop()
        await asyncio.to_thread(state.client.disconnect)
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Build the application.

    Returns:
        The configured :class:`FastAPI` instance.
    """
    settings = get_settings()

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type"],
        )
        logger.debug("CORS enabled for: %s", origins)

    app.include_router(health.router)
    app.include_router(snapshot.router)
    app.include_router(market.router)
    app.include_router(account.router)
    app.include_router(derivatives.router)
    app.include_router(instruments.router)
    app.include_router(analysis.router)
    app.include_router(signals.router)

    _register_error_handlers(app)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        """Return a brief index of the useful endpoints."""
        return {
            "success": True,
            "timestamp": now_ist().isoformat(),
            "service": API_TITLE,
            "version": API_VERSION,
            "phase": "1 - data layer (read-only)",
            "endpoints": {
                "health": "/health",
                "diagnostics": "/diagnostics",
                "full_snapshot_json": "/data",
                "full_snapshot_text": "/text",
                "market": "/market",
                "account": "/account",
                "positions": "/positions",
                "option_chain": "/derivatives/chain/NIFTY",
                "instrument_search": "/instruments/search?q=RELIANCE",
                "interactive_docs": "/docs",
            },
            "notice": (
                "Read-only data layer. No order placement exists in this phase. "
                "Data only, no signals: default state is NO TRADE."
            ),
        }

    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Install exception handlers that keep the response envelope consistent.

    Args:
        app: The application instance.
    """

    @app.exception_handler(TradingSystemError)
    async def handle_domain_error(request: Request, exc: TradingSystemError) -> JSONResponse:
        """Render a domain exception using its declared code and status."""
        logger.warning("%s on %s: %s", exc.code, request.url.path, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "success": False,
                "timestamp": now_ist().isoformat(),
                "error": exc.to_dict(),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render request validation failures in the standard envelope."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "success": False,
                "timestamp": now_ist().isoformat(),
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request parameters failed validation",
                    "detail": str(exc.errors()[:5]),
                },
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Log the full traceback and return a generic 500.

        The message is deliberately generic. Internal exception text can carry
        request payloads, and those can carry credentials.
        """
        logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "timestamp": now_ist().isoformat(),
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An internal error occurred",
                    "detail": "See logs/errors.log. Details are withheld from responses.",
                },
            },
        )


try:
    app = create_app()
except ConfigurationError as exc:
    print(f"\nCONFIGURATION ERROR\n\n{exc}\n", file=sys.stderr)  # noqa: T201
    raise SystemExit(1) from exc


def main() -> None:
    """Run the development server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "api.server:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False,
        log_config=None,  # our own logging is already configured
    )


if __name__ == "__main__":
    main()
