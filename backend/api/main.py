"""APEX CRUSHER — FastAPI application entry point."""

from __future__ import annotations

import math
import statistics
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

_VERSION = "0.2.0"
_START_TIME: datetime = datetime.now(timezone.utc)

app = FastAPI(
    title="APEX CRUSHER",
    description="Automated options trading bot API.",
    version=_VERSION,
)

# ── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Error handling ────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    logger.debug(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "type": type(exc).__name__},
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup() -> None:
    from backend.utils.config import settings
    from backend.core.risk_manager import RiskManager
    from backend.core.strategy_runner import StrategyRunner

    app.state.ready = False

    logger.info("APEX CRUSHER starting… version=%s", _VERSION)

    # a. Config is already loaded via settings import above.
    logger.info("Alpaca base URL: %s", settings.alpaca_base_url)

    # b. Database
    try:
        await init_db()
        logger.info("Database ready.")
    except Exception as exc:
        logger.error("Database failed to initialise: %s", exc)
        logger.error(traceback.format_exc())
        raise

    # c. Alpaca client
    client = AlpacaClient()
    connected = False
    try:
        client.connect()
        connected = await client.health_check()
        if connected:
            logger.info("Alpaca connection verified.")
        else:
            logger.warning("Alpaca health check returned False.")
    except Exception as exc:
        logger.error("Alpaca failed to initialise: %s", exc)

    app.state.alpaca = client
    app.state.alpaca_connected = connected

    # d. Data fetcher
    app.state.fetcher_task = await start_fetcher()
    logger.info("Fetcher started.")

    # e. Strategy runner
    runner = StrategyRunner(alpaca_client=client)
    app.state.strategy_runner = runner
    await runner.start()
    logger.info("StrategyRunner started.")

    # f. Shared risk manager for API endpoints
    app.state.risk_manager = RiskManager()

    # g. Mark ready
    app.state.ready = True
    logger.info("APEX CRUSHER IS LIVE")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("APEX CRUSHER shutting down…")

    runner: Any = getattr(app.state, "strategy_runner", None)
    if runner is not None:
        await runner.stop()

    client: AlpacaClient | None = getattr(app.state, "alpaca", None)
    if client is not None:
        client.disconnect()

    await stop_fetcher()
    await close_db()


# ── Dependencies ─────────────────────────────────────────────────────────────


async def require_ready(request: Request) -> None:
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(
            status_code=503,
            detail="Service starting up, try again in a moment.",
        )


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    """Return service liveness and dependency health."""
    from backend.data.storage import get_pool
    db_ok = False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    uptime = (datetime.now(timezone.utc) - _START_TIME).total_seconds()
    rm = getattr(app.state, "risk_manager", None)
    market_open = rm.is_market_open() if rm is not None else is_market_open()

    return {
        "status": "ok",
        "timestamp": _utc_now(),
        "version": _VERSION,
        "alpaca_connected": getattr(app.state, "alpaca_connected", False),
        "db_connected": db_ok,
        "market_open": market_open,
        "uptime_seconds": round(uptime, 1),
    }


@app.get("/api/status", tags=["bot"])
async def api_status() -> dict[str, Any]:
    """Return the current operational status of the trading bot."""
    runner = getattr(app.state, "strategy_runner", None)
    rm = getattr(app.state, "risk_manager", None)
    emergency_stopped = False
    if rm is not None:
        try:
            emergency_stopped = await rm.is_emergency_stopped()
        except Exception:
            pass

    return {
        "bot_running": runner.is_running if runner is not None else False,
        "ready": getattr(app.state, "ready", False),
        "market_open": rm.is_market_open() if rm is not None else is_market_open(),
        "emergency_stop_active": emergency_stopped,
        "timestamp": _utc_now(),
    }


@app.get("/api/pipeline/status", tags=["bot"], dependencies=[Depends(require_ready)])
async def pipeline_status() -> dict[str, Any]:
    """Return the current state of the background data pipeline."""
    state = get_pipeline_state()
    redis_ok = await _redis_ping()
    return {
        **state,
        "symbols": SYMBOLS,
        "market_open": is_market_open(),
        "redis_connected": redis_ok,
        "timestamp": _utc_now(),
    }


@app.get("/api/market-data/{symbol}", tags=["market"], dependencies=[Depends(require_ready)])
async def market_data(symbol: str) -> dict[str, Any]:
    """Return the latest cached spot price for *symbol*."""
    upper = symbol.upper()
    data = await get_cached_market_data(upper)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached price for {upper}. The fetcher may not have run yet.",
        )
    return data


@app.get("/api/options/{symbol}", tags=["market"], dependencies=[Depends(require_ready)])
async def options_chain(symbol: str) -> dict[str, Any]:
    """Return the most recent options chain snapshot for *symbol* (max 100 contracts)."""
    upper = symbol.upper()
    rows = await get_latest_options(upper)
    serialized = [_ser(dict(r)) for r in rows[:100]]
    return {
        "symbol": upper,
        "count": len(serialized),
        "contracts": serialized,
        "timestamp": _utc_now(),
    }


@app.get("/api/greeks/{symbol}/{strike}/{expiry}", tags=["market"], dependencies=[Depends(require_ready)])
async def greeks(symbol: str, strike: float, expiry: date) -> dict[str, Any]:
    """Return the most recently computed Greeks for a specific option contract."""
    upper = symbol.upper()
    row = await get_latest_greeks(upper, strike, expiry)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Greeks found for {upper} K={strike} exp={expiry}.",
        )
    return _ser(dict(row))


@app.get("/api/signals/recent", tags=["trading"], dependencies=[Depends(require_ready)])
async def signals_recent() -> dict[str, Any]:
    """Return the last 50 signals saved to the database."""
    from backend.data.storage import get_pool
    rows: list[dict[str, Any]] = []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT id, symbol, signal_type, direction, strength, strategy,
                       created_at, metadata
                FROM   signals
                ORDER  BY created_at DESC
                LIMIT  50
                """,
            )
        rows = [_ser(dict(r)) for r in records]
    except Exception as exc:
        logger.warning("signals_recent query failed: %s", exc)

    return {"count": len(rows), "signals": rows, "timestamp": _utc_now()}


@app.get("/api/trades/open", tags=["trading"], dependencies=[Depends(require_ready)])
async def trades_open() -> dict[str, Any]:
    """Return all currently open trades enriched with unrealised PnL and DTE."""
    from backend.data.storage import get_open_trades
    from backend.data.fetcher import get_cached_price

    raw = await get_open_trades()
    enriched: list[dict[str, Any]] = []
    today = date.today()

    for trade in raw:
        t = _ser(dict(trade))

        # Unrealised PnL
        cached = await get_cached_price(t["symbol"])
        entry = trade.get("entry_price")
        qty = trade.get("quantity", 0)
        if cached is not None and entry is not None and qty:
            direction = 1 if str(trade.get("action", "buy")).lower() == "buy" else -1
            t["current_price"] = cached
            t["unrealized_pnl"] = round(
                direction * (cached - float(entry)) * qty * 100, 2
            )
        else:
            t["current_price"] = None
            t["unrealized_pnl"] = None

        # DTE
        expiry_val = trade.get("expiry")
        if isinstance(expiry_val, date):
            t["dte"] = (expiry_val - today).days
        else:
            t["dte"] = None

        enriched.append(t)

    return {"count": len(enriched), "trades": enriched, "timestamp": _utc_now()}


@app.get("/api/trades/history", tags=["trading"], dependencies=[Depends(require_ready)])
async def trades_history() -> dict[str, Any]:
    """Return the last 100 closed trades with duration, PnL%, and close reason."""
    from backend.data.storage import get_closed_trades

    raw = await get_closed_trades(limit=100)
    result: list[dict[str, Any]] = []

    for trade in raw:
        t = _ser(dict(trade))

        opened_at = trade.get("opened_at")
        closed_at = trade.get("closed_at")
        if isinstance(opened_at, datetime) and isinstance(closed_at, datetime):
            delta = closed_at - opened_at
            t["duration_minutes"] = round(delta.total_seconds() / 60, 1)
        else:
            t["duration_minutes"] = None

        entry = trade.get("entry_price")
        pnl = trade.get("pnl")
        qty = trade.get("quantity", 1)
        if entry is not None and pnl is not None and float(entry) > 0 and qty:
            cost_basis = float(entry) * qty * 100
            t["pnl_pct"] = round(float(pnl) / cost_basis * 100, 2) if cost_basis else None
        else:
            t["pnl_pct"] = None

        t.setdefault("close_reason", None)
        result.append(t)

    return {"count": len(result), "trades": result, "timestamp": _utc_now()}


@app.get("/api/performance", tags=["trading"], dependencies=[Depends(require_ready)])
async def performance() -> dict[str, Any]:
    """Return aggregate performance metrics for the bot."""
    from backend.data.storage import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pnl, closed_at::date AS trade_date
            FROM   trades
            WHERE  status = 'closed' AND pnl IS NOT NULL
            ORDER  BY closed_at DESC
            """,
        )

    pnls = [float(r["pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = round(sum(pnls), 2)
    win_rate = round(len(wins) / len(pnls), 4) if pnls else 0.0
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else None
    best_trade = round(max(pnls), 2) if pnls else None
    worst_trade = round(min(pnls), 2) if pnls else None

    # Daily PnL bucketing for Sharpe
    daily: dict[Any, float] = {}
    for r in rows:
        d = r["trade_date"]
        daily[d] = daily.get(d, 0.0) + float(r["pnl"])
    daily_pnls = list(daily.values())
    sharpe: float | None = None
    if len(daily_pnls) >= 2:
        try:
            mu = statistics.mean(daily_pnls)
            sigma = statistics.stdev(daily_pnls)
            sharpe = round(mu / sigma * math.sqrt(252), 4) if sigma else None
        except Exception:
            pass

    # Monthly PnL (current calendar month)
    from backend.data.storage import get_pool as _gp  # already imported above
    today = date.today()
    monthly_pnl: float = 0.0
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(pnl), 0) AS total
                FROM   trades
                WHERE  status = 'closed'
                  AND  pnl IS NOT NULL
                  AND  DATE_TRUNC('month', closed_at) = DATE_TRUNC('month', $1::date)
                """,
                today,
            )
        monthly_pnl = round(float(row["total"] or 0.0), 2)
    except Exception as exc:
        logger.warning("monthly_pnl query failed: %s", exc)

    # Current drawdown vs starting balance
    from backend.core.risk_manager import STARTING_BALANCE
    try:
        alpaca: AlpacaClient = app.state.alpaca
        account = await alpaca.get_account()
        equity = float(account.get("equity", STARTING_BALANCE))
    except Exception:
        equity = STARTING_BALANCE
    drawdown_pct = round(max(0.0, (STARTING_BALANCE - equity) / STARTING_BALANCE), 4)

    return {
        "total_pnl": total_pnl,
        "monthly_pnl": monthly_pnl,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "sharpe_estimate": sharpe,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "total_trades": len(pnls),
        "current_drawdown_pct": drawdown_pct,
        "timestamp": _utc_now(),
    }


@app.post("/api/emergency-stop", tags=["control"], dependencies=[Depends(require_ready)])
async def emergency_stop() -> dict[str, Any]:
    """Activate the emergency stop flag, halting all new trade execution."""
    rm = app.state.risk_manager
    await rm.emergency_stop()
    return {"status": "emergency_stop_activated", "timestamp": _utc_now()}


@app.post("/api/resume", tags=["control"], dependencies=[Depends(require_ready)])
async def resume() -> dict[str, Any]:
    """Clear the emergency stop flag, re-enabling trade execution."""
    rm = app.state.risk_manager
    await rm.reset_emergency_stop()
    return {"status": "trading_resumed", "timestamp": _utc_now()}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _ser(row: dict[str, Any]) -> dict[str, Any]:
    """Convert asyncpg types to JSON-serialisable primitives."""
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
    try:
        from backend.data.fetcher import _get_redis
        client = await _get_redis()
        await client.ping()
        return True
    except Exception:
        return False


async def _opt_mid(symbol: str) -> float | None:
    """Return the mid-price of the best ATM option for *symbol*, or None."""
    try:
        from backend.data.fetcher import get_cached_price
        from backend.data.storage import get_pool
        spot = await get_cached_price(symbol)
        if spot is None:
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT bid, ask
                FROM   options_data
                WHERE  symbol = $1 AND bid > 0 AND ask > 0
                  AND  timestamp = (SELECT MAX(timestamp) FROM options_data WHERE symbol = $1)
                ORDER  BY ABS(strike - $2) ASC
                LIMIT  1
                """,
                symbol, spot,
            )
        if row is None:
            return None
        return round((float(row["bid"]) + float(row["ask"])) / 2, 4)
    except Exception:
        return None
