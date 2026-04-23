"""APEX CRUSHER — FastAPI application entry point."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.data.alpaca_client import AlpacaClient
from backend.data.fetcher import start as start_fetcher
from backend.data.fetcher import stop as stop_fetcher
from backend.data.storage import close_db, init_db
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
    """Initialise the Alpaca client and verify connectivity.

    Stores ``app.state.alpaca`` (:class:`~backend.data.alpaca_client.AlpacaClient`)
    and ``app.state.alpaca_connected`` (``bool``) so that route handlers can read
    live connection status without re-importing the client module.

    A failure to connect is logged as an error but does **not** abort startup —
    the app remains available and ``/health`` will report
    ``alpaca_connected: false``.
    """
    from backend.utils.config import settings

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

    # ── Data fetcher ──────────────────────────────────────────────────────────
    await start_fetcher()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Disconnect the Alpaca client, close the DB pool, and log shutdown."""
    logger.info("APEX CRUSHER shutting down…")
    client: AlpacaClient | None = getattr(app.state, "alpaca", None)
    if client is not None:
        client.disconnect()
    await stop_fetcher()
    await close_db()


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
