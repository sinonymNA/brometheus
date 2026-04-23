"""APEX CRUSHER — FastAPI application entry point."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    """Validate configuration and log startup confirmation."""
    # Import here so a missing env var surfaces immediately on startup with a clear error.
    from backend.utils.config import settings  # noqa: F401

    logger.info("APEX CRUSHER starting… version=%s", _VERSION)
    logger.info("Alpaca base URL: %s", settings.alpaca_base_url)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Log shutdown and release any held resources."""
    logger.info("APEX CRUSHER shutting down…")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Return service liveness information.

    Returns:
        A dict containing ``status``, ``timestamp`` (ISO 8601 UTC), and ``version``.
    """
    return {
        "status": "ok",
        "timestamp": _utc_now(),
        "version": _VERSION,
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
