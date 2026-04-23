"""APEX CRUSHER — FastAPI application entry point."""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import traceback
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

# ── WebSocket manager ─────────────────────────────────────────────────────────

_MAX_WS = 10


class _WSManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> bool:
        if len(self._clients) >= _MAX_WS:
            await ws.close(code=1008, reason="Max connections reached")
            return False
        await ws.accept()
        self._clients.add(ws)
        logger.debug("WebSocket client connected (%d total).", len(self._clients))
        return True

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.debug("WebSocket client disconnected (%d remaining).", len(self._clients))

    @property
    def count(self) -> int:
        return len(self._clients)


_ws_manager = _WSManager()

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
    app.state.ready = False
    app.state.strategy_runner = None
    app.state.risk_manager = None
    app.state.alpaca = None
    app.state.alpaca_connected = False

    from backend.utils.config import settings

    logger.info("APEX CRUSHER starting… version=%s", _VERSION)
    logger.info("Alpaca base URL: %s", settings.alpaca_base_url)

    # Database — hard dependency, raise if it fails
    try:
        await init_db()
        logger.info("Database ready.")
    except Exception as exc:
        logger.error("Database failed to initialise: %s", exc)
        logger.error(traceback.format_exc())
        raise

    # Alpaca client
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

    # Data fetcher
    try:
        app.state.fetcher_task = await start_fetcher()
        logger.info("Fetcher started.")
    except Exception as exc:
        logger.error("Fetcher failed to start: %s", exc)

    # Strategy runner + risk manager
    try:
        from backend.core.risk_manager import RiskManager
        from backend.core.strategy_runner import StrategyRunner
        runner = StrategyRunner(alpaca_client=client)
        app.state.strategy_runner = runner
        await runner.start()
        app.state.risk_manager = RiskManager()
        logger.info("StrategyRunner started.")
    except Exception as exc:
        logger.error("StrategyRunner failed to start: %s", exc, exc_info=True)
        try:
            from backend.core.risk_manager import RiskManager as _RM
            if app.state.risk_manager is None:
                app.state.risk_manager = _RM()
        except Exception:
            pass

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
        raise HTTPException(status_code=503, detail="Service starting up, try again in a moment.")


# ── HTTP Routes ───────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
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
    upper = symbol.upper()
    data = await get_cached_market_data(upper)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No cached price for {upper}.")
    return data


@app.get("/api/options/{symbol}", tags=["market"], dependencies=[Depends(require_ready)])
async def options_chain(symbol: str) -> dict[str, Any]:
    upper = symbol.upper()
    rows = await get_latest_options(upper)
    return {"symbol": upper, "count": len(rows[:100]), "contracts": [_ser(dict(r)) for r in rows[:100]], "timestamp": _utc_now()}


@app.get("/api/greeks/{symbol}/{strike}/{expiry}", tags=["market"], dependencies=[Depends(require_ready)])
async def greeks(symbol: str, strike: float, expiry: date) -> dict[str, Any]:
    upper = symbol.upper()
    row = await get_latest_greeks(upper, strike, expiry)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No Greeks for {upper} K={strike} exp={expiry}.")
    return _ser(dict(row))


@app.get("/api/signals/recent", tags=["trading"], dependencies=[Depends(require_ready)])
async def signals_recent() -> dict[str, Any]:
    from backend.data.storage import get_pool
    rows: list[dict[str, Any]] = []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT id, symbol, signal_type, direction, strength, strategy_name,
                       created_at, acted_on, metadata
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
    from backend.data.storage import get_open_trades
    from backend.data.fetcher import get_cached_price
    raw = await get_open_trades()
    enriched: list[dict[str, Any]] = []
    today = date.today()
    for trade in raw:
        t = _ser(dict(trade))
        cached = await get_cached_price(t["symbol"])
        entry = trade.get("entry_price")
        qty = trade.get("quantity", 0)
        if cached is not None and entry is not None and qty:
            direction = 1 if str(trade.get("action", "buy")).lower() == "buy" else -1
            t["current_price"] = cached
            t["unrealized_pnl"] = round(direction * (cached - float(entry)) * qty * 100, 2)
        else:
            t["current_price"] = None
            t["unrealized_pnl"] = None
        expiry_val = trade.get("expiry")
        t["dte"] = (expiry_val - today).days if isinstance(expiry_val, date) else None
        enriched.append(t)
    return {"count": len(enriched), "trades": enriched, "timestamp": _utc_now()}


@app.get("/api/trades/history", tags=["trading"], dependencies=[Depends(require_ready)])
async def trades_history() -> dict[str, Any]:
    from backend.data.storage import get_closed_trades
    raw = await get_closed_trades(limit=100)
    result: list[dict[str, Any]] = []
    for trade in raw:
        t = _ser(dict(trade))
        opened_at = trade.get("opened_at")
        closed_at = trade.get("closed_at")
        if isinstance(opened_at, datetime) and isinstance(closed_at, datetime):
            t["duration_minutes"] = round((closed_at - opened_at).total_seconds() / 60, 1)
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
    from backend.data.storage import get_pool
    from backend.core.risk_manager import STARTING_BALANCE

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT pnl, closed_at::date AS trade_date FROM trades "
            "WHERE status = 'closed' AND pnl IS NOT NULL ORDER BY closed_at DESC",
        )

    pnls = [float(r["pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = round(sum(pnls), 2)
    win_rate = round(len(wins) / len(pnls), 4) if pnls else 0.0
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    gross_loss = abs(sum(losses))
    profit_factor = round(sum(wins) / gross_loss, 4) if gross_loss else None
    best_trade = round(max(pnls), 2) if pnls else None
    worst_trade = round(min(pnls), 2) if pnls else None

    daily: dict[Any, float] = {}
    for r in rows:
        d = r["trade_date"]
        daily[d] = daily.get(d, 0.0) + float(r["pnl"])
    sharpe: float | None = None
    if len(daily) >= 2:
        try:
            mu = statistics.mean(daily.values())
            sigma = statistics.stdev(daily.values())
            sharpe = round(mu / sigma * math.sqrt(252), 4) if sigma else None
        except Exception:
            pass

    today = date.today()
    monthly_pnl: float = 0.0
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
                "WHERE status = 'closed' AND pnl IS NOT NULL "
                "AND DATE_TRUNC('month', closed_at) = DATE_TRUNC('month', $1::date)",
                today,
            )
        monthly_pnl = round(float(row["total"] or 0.0), 2)
    except Exception as exc:
        logger.warning("monthly_pnl query failed: %s", exc)

    equity = STARTING_BALANCE
    try:
        alpaca: AlpacaClient = app.state.alpaca
        account = await alpaca.get_account()
        equity = float(account.equity)
    except Exception:
        pass
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
    rm = app.state.risk_manager
    await rm.emergency_stop()
    return {"status": "emergency_stop_activated", "timestamp": _utc_now()}


@app.post("/api/resume", tags=["control"], dependencies=[Depends(require_ready)])
async def resume() -> dict[str, Any]:
    rm = app.state.risk_manager
    await rm.reset_emergency_stop()
    return {"status": "trading_resumed", "timestamp": _utc_now()}


# ── WebSocket ─────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_handler(ws: WebSocket) -> None:
    if not await _ws_manager.connect(ws):
        return
    ping_tick = 0
    try:
        while True:
            payload = await _build_ws_payload()
            await ws.send_text(json.dumps(payload, default=str))
            ping_tick += 1
            if ping_tick >= 15:
                await ws.send_text('{"type":"ping"}')
                ping_tick = 0
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        _ws_manager.disconnect(ws)


async def _build_ws_payload() -> dict[str, Any]:
    from backend.core.risk_manager import STARTING_BALANCE

    payload: dict[str, Any] = {
        "type": "update",
        "balance": STARTING_BALANCE,
        "daily_pnl": 0.0,
        "market_open": False,
        "bot_running": False,
        "emergency_stop": False,
        "open_positions": [],
        "recent_signals": [],
        "portfolio_greeks": {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0},
        "equity_curve": [],
        "pipeline_state": {},
        "uptime_seconds": round((datetime.now(timezone.utc) - _START_TIME).total_seconds(), 1),
        "timestamp": _utc_now(),
    }

    rm = getattr(app.state, "risk_manager", None)
    runner = getattr(app.state, "strategy_runner", None)
    if rm is not None:
        payload["market_open"] = rm.is_market_open()
        try:
            payload["emergency_stop"] = await rm.is_emergency_stopped()
        except Exception:
            pass
    if runner is not None:
        payload["bot_running"] = runner.is_running

    payload["pipeline_state"] = get_pipeline_state()

    # Account balance + daily PnL
    balance = STARTING_BALANCE
    try:
        alpaca = getattr(app.state, "alpaca", None)
        if alpaca is not None:
            account = await alpaca.get_account()
            balance = float(account.equity)
            payload["balance"] = balance
    except Exception:
        pass
    try:
        if rm is not None:
            payload["daily_pnl"] = round(await rm.get_daily_pnl(balance), 2)
    except Exception:
        pass

    # Open positions enriched with current price + DTE
    try:
        from backend.data.storage import get_open_trades
        from backend.data.fetcher import get_cached_price
        trades = await get_open_trades()
        positions = []
        today = date.today()
        for t in trades:
            current = await get_cached_price(t["symbol"])
            entry = float(t.get("entry_price") or 0)
            qty = int(t.get("quantity") or 0)
            direction = 1 if str(t.get("action", "buy")).lower() == "buy" else -1
            upnl = round(direction * (current - entry) * qty * 100, 2) if current else None
            exp = t.get("expiry")
            positions.append({
                "id": t.get("id"),
                "symbol": t["symbol"],
                "strike": float(t.get("strike") or 0),
                "option_type": t.get("option_type", ""),
                "strategy": t.get("strategy", ""),
                "action": t.get("action", ""),
                "entry_price": entry,
                "current_price": current,
                "quantity": qty,
                "unrealized_pnl": upnl,
                "dte": (exp - today).days if isinstance(exp, date) else None,
                "expiry": exp.isoformat() if isinstance(exp, date) else None,
                "opened_at": t["opened_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(t.get("opened_at"), "strftime") else None,
            })
        payload["open_positions"] = positions
    except Exception as exc:
        logger.debug("WS open_positions error: %s", exc)

    # Recent signals (last 10)
    try:
        from backend.data.storage import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, symbol, signal_type, direction, strength, strategy_name, "
                "created_at, acted_on FROM signals ORDER BY created_at DESC LIMIT 10",
            )
        payload["recent_signals"] = [
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "signal_type": r["signal_type"],
                "direction": r["direction"],
                "strength": float(r["strength"] or 0),
                "strategy": r["strategy_name"],
                "acted_on": r["acted_on"],
                "timestamp": r["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.debug("WS recent_signals error: %s", exc)

    # Portfolio Greeks (net exposure across open trades)
    try:
        from backend.data.storage import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.quantity, t.action,
                       cg.delta, cg.gamma, cg.vega, cg.theta, cg.rho
                FROM   trades t
                JOIN   LATERAL (
                    SELECT cg2.delta, cg2.gamma, cg2.vega, cg2.theta, cg2.rho
                    FROM   options_data od
                    JOIN   calculated_greeks cg2 ON cg2.option_id = od.id
                    WHERE  od.symbol      = t.symbol
                      AND  od.strike      = t.strike
                      AND  od.expiry      = t.expiry
                      AND  od.option_type = t.option_type
                    ORDER  BY cg2.timestamp DESC
                    LIMIT  1
                ) cg ON TRUE
                WHERE  t.status = 'open'
                  AND  t.strike IS NOT NULL
                  AND  t.expiry IS NOT NULL
                  AND  t.option_type IS NOT NULL
                """,
            )
        net: dict[str, float] = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
        for r in rows:
            qty = int(r["quantity"] or 0)
            sign = (1 if str(r.get("action", "buy")).lower() == "buy" else -1) * qty * 100
            for g in net:
                net[g] += float(r[g] or 0) * sign
        payload["portfolio_greeks"] = {k: round(v, 4) for k, v in net.items()}
    except Exception as exc:
        logger.debug("WS portfolio_greeks error: %s", exc)

    # Equity curve — running balance from trade history (last 60 days)
    try:
        from backend.data.storage import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH daily AS (
                    SELECT closed_at::date AS d, SUM(pnl) AS dpnl
                    FROM   trades
                    WHERE  status = 'closed' AND pnl IS NOT NULL
                    GROUP  BY 1
                    ORDER  BY 1
                )
                SELECT d, $1::float + SUM(dpnl) OVER (ORDER BY d) AS balance
                FROM   daily
                WHERE  d >= CURRENT_DATE - INTERVAL '60 days'
                ORDER  BY d
                """,
                STARTING_BALANCE,
            )
        payload["equity_curve"] = [
            {"date": r["d"].isoformat(), "balance": round(float(r["balance"]), 2)}
            for r in rows
        ]
    except Exception as exc:
        logger.debug("WS equity_curve error: %s", exc)

    return payload


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _ser(row: dict[str, Any]) -> dict[str, Any]:
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
    try:
        from backend.data.fetcher import get_cached_price
        from backend.data.storage import get_pool
        spot = await get_cached_price(symbol)
        if spot is None:
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT bid, ask FROM options_data "
                "WHERE symbol=$1 AND bid>0 AND ask>0 "
                "AND timestamp=(SELECT MAX(timestamp) FROM options_data WHERE symbol=$1) "
                "ORDER BY ABS(strike-$2) ASC LIMIT 1",
                symbol, spot,
            )
        if row is None:
            return None
        return round((float(row["bid"]) + float(row["ask"])) / 2, 4)
    except Exception:
        return None


# ── Backtesting ───────────────────────────────────────────────────────────────

_backtest_jobs: dict[str, dict] = {}   # job_id → {status, progress, result, error}


@app.get("/api/backtest/presets", tags=["backtest"])
async def backtest_presets() -> dict:
    today = date.today()
    return {
        "presets": [
            {
                "id": "2023_full",
                "label": "2023 Full Year",
                "start_date": "2023-01-03",
                "end_date": "2023-12-29",
                "description": "Bull market recovery",
            },
            {
                "id": "2022_bear",
                "label": "2022 Bear Market",
                "start_date": "2022-01-03",
                "end_date": "2022-12-30",
                "description": "High-volatility bear market",
            },
            {
                "id": "walk_forward",
                "label": "Walk-Forward (train 2022-2023, test 2024)",
                "start_date": "2022-01-03",
                "end_date": "2023-12-29",
                "test_start_date": "2024-01-02",
                "test_end_date": "2024-12-31",
                "description": "Train on 2022-2023, validate on 2024",
                "walk_forward": True,
            },
            {
                "id": "ytd",
                "label": "Year-to-Date",
                "start_date": f"{today.year}-01-02",
                "end_date": today.isoformat(),
                "description": "Current year performance",
            },
        ]
    }


class _BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    symbols: list[str] = ["SPY", "QQQ", "AAPL"]
    strategies: list[str] = ["momentum", "iv_rank", "flow"]
    walk_forward: bool = False
    test_start_date: Optional[date] = None
    test_end_date: Optional[date] = None


@app.post("/api/backtest", tags=["backtest"])
async def start_backtest(req: _BacktestRequest) -> dict:
    import uuid as _uuid
    job_id = _uuid.uuid4().hex
    _backtest_jobs[job_id] = {"status": "running", "progress": 0, "message": "Starting…", "result": None, "error": None}

    async def _run() -> None:
        try:
            from backend.backtesting.data_loader import HistoricalDataLoader
            from backend.backtesting.engine import BacktestEngine

            alpaca = app.state.alpaca
            if alpaca is None:
                raise RuntimeError("Alpaca client not initialised")

            loader = HistoricalDataLoader(alpaca)
            engine = BacktestEngine(loader, req.symbols, req.strategies)

            async def _progress(pct: int, msg: str) -> None:
                _backtest_jobs[job_id]["progress"] = pct
                _backtest_jobs[job_id]["message"] = msg

            if req.walk_forward and req.test_start_date and req.test_end_date:
                result = await engine.run_walk_forward(
                    req.start_date, req.end_date,
                    req.test_start_date, req.test_end_date,
                )
                _backtest_jobs[job_id].update({"status": "done", "progress": 100, "result": result})
            else:
                result = await engine.run(req.start_date, req.end_date, progress_cb=_progress)
                _backtest_jobs[job_id].update({"status": "done", "progress": 100, "result": result.to_dict()})
        except Exception as exc:
            logger.exception("Backtest job %s failed", job_id)
            _backtest_jobs[job_id].update({"status": "error", "error": str(exc)})

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/api/backtest/{job_id}", tags=["backtest"])
async def get_backtest(job_id: str) -> dict:
    job = _backtest_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Static frontend (must be last) ────────────────────────────────────────────

_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
