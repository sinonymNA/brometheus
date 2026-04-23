"""APEX CRUSHER — FastAPI application entry point."""

from __future__ import annotations

import traceback
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.data.alpaca_client import AlpacaClient
from backend.data.fetcher import (
    SYMBOLS,
    get_cached_market_data,
    get_pipeline_state,
    is_market_open,
    start as start_fetcher,
    stop as stop_fetcher,
)
from backend.data.storage import (
    close_db,
    get_latest_greeks,
    get_latest_options,
    init_db,
)
from backend.utils.logger import get_logger

logger = get_logger(__name__)

_VERSION = "0.1.0"

# Mutable bot state — toggled by future start/stop endpoints.
_bot_status: str = "stopped"

app = FastAPI(
    title="APEX CRUSHER",
    description="Automated options trading bot API.",
    version=_VERSION,
)

# ── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8000",
        "https://*.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Error handling ────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that returns a structured JSON error instead of a 500 HTML page."""
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    logger.debug(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc),
            "type": type(exc).__name__,
        },
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise the Alpaca client, database pool, and background fetcher."""
    from backend.utils.config import settings

    app.state.ready = False

    logger.info("APEX CRUSHER starting… version=%s", _VERSION)
    logger.info("Alpaca base URL: %s", settings.alpaca_base_url)

    client = AlpacaClient()
    connected = False

    try:
        client.connect()
        connected = await client.health_check()
        if connected:
            logger.info("Alpaca connection verified.")
        else:
            logger.warning("Alpaca client initialised but health check returned False.")
    except Exception as exc:
        logger.error("Alpaca failed to initialise: %s", exc)

    app.state.alpaca = client
    app.state.alpaca_connected = connected

    # ── Database ──────────────────────────────────────────────────────────────
    try:
        await init_db()
        logger.info("Database initialised.")
    except Exception as exc:
        logger.error("Database failed to initialise: %s", exc)
        return  # don't mark ready if DB failed

    # ── Data fetcher ──────────────────────────────────────────────────────────
    app.state.fetcher_task = await start_fetcher()

    app.state.ready = True
    logger.info("APEX CRUSHER ready.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Disconnect the Alpaca client, stop the fetcher, and close the DB pool."""
    logger.info("APEX CRUSHER shutting down…")
    client: AlpacaClient | None = getattr(app.state, "alpaca", None)
    if client is not None:
        client.disconnect()
    await stop_fetcher()
    await close_db()


# ── Dependencies ─────────────────────────────────────────────────────────────


async def require_db(request: Request) -> None:
    """Raise 503 if the database pool is not yet initialised."""
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(
            status_code=503,
            detail="Service starting up, try again in a moment.",
        )


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str | bool]:
    """Return service liveness and dependency health.

    Returns:
        A dict with ``status``, ``timestamp`` (ISO 8601 UTC), ``version``,
        and ``alpaca_connected`` (``bool``).
    """
    return {
        "status": "ok",
        "timestamp": _utc_now(),
        "version": _VERSION,
        "alpaca_connected": getattr(app.state, "alpaca_connected", False),
    }


@app.get("/api/status", tags=["bot"])
async def api_status() -> dict[str, str]:
    """Return the current operational status of the trading bot.

    Returns:
        A dict containing ``bot_status`` (``"running"`` or ``"stopped"``)
        and ``timestamp`` (ISO 8601 UTC).
    """
    return {
        "bot_status": _bot_status,
        "timestamp": _utc_now(),
    }


@app.get("/api/market-data/{symbol}", tags=["market"], dependencies=[Depends(require_db)])
async def market_data(symbol: str) -> dict[str, Any]:
    """Return the latest cached spot price for *symbol*.

    The price is read from Redis (TTL 90 s).  A 404 is returned if no cycle
    has run yet or the cache entry has expired.

    Args:
        symbol: Underlying ticker, e.g. ``SPY``.
    """
    upper = symbol.upper()
    data = await get_cached_market_data(upper)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached price for {upper}. The fetcher may not have run yet.",
        )
    return data


@app.get("/api/options/{symbol}", tags=["market"], dependencies=[Depends(require_db)])
async def options_chain(symbol: str) -> dict[str, Any]:
    """Return the most recent options chain snapshot for *symbol*.

    Fetches up to 50 contracts from the latest snapshot timestamp, ordered
    by expiry then strike.

    Args:
        symbol: Underlying ticker, e.g. ``SPY``.
    """
    upper = symbol.upper()
    rows = await get_latest_options(upper)
    serialized = [_serialize_row(r) for r in rows[:50]]
    return {
        "symbol": upper,
        "count": len(serialized),
        "contracts": serialized,
        "timestamp": _utc_now(),
    }


@app.get("/api/greeks/{symbol}/{strike}/{expiry}", tags=["market"], dependencies=[Depends(require_db)])
async def greeks(symbol: str, strike: float, expiry: date) -> dict[str, Any]:
    """Return the most recently computed Greeks for a specific option contract.

    FastAPI parses *expiry* as ``YYYY-MM-DD`` natively.  Returns 404 if no
    matching record exists in the database.

    Args:
        symbol: Underlying ticker, e.g. ``SPY``.
        strike: Strike price, e.g. ``500.0``.
        expiry: Expiry date in ``YYYY-MM-DD`` format.
    """
    upper = symbol.upper()
    row = await get_latest_greeks(upper, strike, expiry)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Greeks found for {upper} K={strike} exp={expiry}.",
        )
    return _serialize_row(row)


@app.get("/api/pipeline/status", tags=["bot"], dependencies=[Depends(require_db)])
async def pipeline_status() -> dict[str, Any]:
    """Return the current state of the background data pipeline.

    Includes the last successful cycle timestamp, total options priced since
    startup, monitored symbols, market-open status, and Redis connectivity.
    """
    state = get_pipeline_state()
    redis_ok = await _redis_ping()
    return {
        **state,
        "symbols": SYMBOLS,
        "market_open": is_market_open(),
        "redis_connected": redis_ok,
        "timestamp": _utc_now(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert asyncpg-specific types to JSON-serialisable Python primitives.

    Converts ``Decimal`` → ``float`` and ``date``/``datetime`` → ISO string.
    """
    out: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, Decimal):
            out[key] = float(val)
        elif isinstance(val, datetime):
            out[key] = val.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        elif isinstance(val, date):
            out[key] = val.isoformat()
        else:
            out[key] = val
    return out


async def _redis_ping() -> bool:
    """Return ``True`` if Redis is reachable, ``False`` otherwise."""
    try:
        from backend.data.fetcher import _get_redis
        client = await _get_redis()
        await client.ping()
        return True
    except Exception:
        return False
