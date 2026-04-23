"""Async PostgreSQL storage layer for APEX CRUSHER.

All public functions require :func:`init_db` to have been awaited first —
typically from the FastAPI startup handler.  A module-level connection pool
is shared across the process; never create per-request pools.

Usage::

    from backend.data.storage import init_db, save_market_data, get_open_trades

    # In FastAPI startup:
    await init_db()

    # In route / task handlers:
    await save_market_data("SPY", price=520.10, volume=1_000_000, bid=520.08, ask=520.12)
    trades = await get_open_trades()
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from backend.core.greeks_engine import GreeksResult
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# Absolute path to the SQL schema file, resolved relative to this file's
# location so it works regardless of the process working directory.
_SCHEMA_PATH = Path(__file__).parent.parent.parent / "scripts" / "setup_db.sql"

_MAX_INIT_RETRIES = 5
_INIT_RETRY_BASE = 2  # seconds; wait = base ** attempt → 2, 4, 8, 16, 32

_pool: asyncpg.Pool | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_db() -> None:
    """Create the connection pool and apply the schema (idempotent).

    Retries up to :data:`_MAX_INIT_RETRIES` times with exponential backoff so
    that Railway's PostgreSQL service has time to become ready on a cold start.

    Raises:
        asyncpg.PostgresError: If the pool cannot be created after all retries.
    """
    global _pool

    from backend.utils.config import settings
    for attempt in range(_MAX_INIT_RETRIES):
        try:
            _pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("Database pool created (min=2, max=10).")
            break
        except Exception as exc:
            if attempt < _MAX_INIT_RETRIES - 1:
                wait = _INIT_RETRY_BASE ** (attempt + 1)
                logger.warning(
                    "Database connection failed (attempt %d/%d): %s — retrying in %ds…",
                    attempt + 1,
                    _MAX_INIT_RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("Database connection failed after %d attempts.", _MAX_INIT_RETRIES)
                raise

    schema_sql = _SCHEMA_PATH.read_text()
    async with _pool.acquire() as conn:  # type: ignore[union-attr]
        await conn.execute(schema_sql)
    logger.info("Database schema applied from %s.", _SCHEMA_PATH.name)


async def close_db() -> None:
    """Gracefully close the connection pool on application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed.")


def get_pool() -> asyncpg.Pool:
    """Return the active connection pool.

    Raises:
        RuntimeError: If :func:`init_db` has not been called yet.
    """
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call init_db() first.")
    return _pool


# ── Market data ───────────────────────────────────────────────────────────────


async def save_market_data(
    symbol: str,
    price: float,
    volume: int,
    bid: float,
    ask: float,
    timestamp: datetime | None = None,
) -> int:
    """Insert one real-time market data row.

    Args:
        symbol: Underlying ticker (e.g. ``"SPY"``).
        price: Last trade price.
        volume: Cumulative volume for the period.
        bid: Best bid price.
        ask: Best ask price.
        timestamp: Observation time (UTC). Defaults to now.

    Returns:
        The newly inserted row ID.
    """
    ts = timestamp or datetime.now(timezone.utc)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO market_data (symbol, timestamp, price, volume, bid, ask)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            symbol, ts, price, volume, bid, ask,
        )
    logger.debug("Saved market_data id=%d for %s @ %.2f", row["id"], symbol, price)
    return row["id"]


# ── Options data ──────────────────────────────────────────────────────────────


async def save_options_snapshot(options: list[dict[str, Any]]) -> list[int]:
    """Batch-insert an options chain snapshot in a single transaction.

    Each dict in *options* must contain:
    ``symbol``, ``strike``, ``expiry``, ``option_type``, and ``timestamp``.
    Optional keys: ``bid``, ``ask``, ``volume``, ``open_interest``, ``iv``.

    Args:
        options: List of option contract dicts (one per contract).

    Returns:
        List of inserted row IDs in the same order as *options*.
    """
    if not options:
        return []

    ids: list[int] = []
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            stmt = await conn.prepare(
                """
                INSERT INTO options_data
                    (symbol, strike, expiry, option_type, bid, ask,
                     volume, open_interest, iv, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """
            )
            for opt in options:
                row = await stmt.fetchrow(
                    opt["symbol"],
                    opt["strike"],
                    opt["expiry"],
                    opt["option_type"],
                    opt.get("bid"),
                    opt.get("ask"),
                    opt.get("volume"),
                    opt.get("open_interest"),
                    opt.get("iv"),
                    opt.get("timestamp", datetime.now(timezone.utc)),
                )
                ids.append(row["id"])

    logger.debug("Saved %d options contracts for %s.", len(ids), options[0]["symbol"])
    return ids


# ── Greeks ────────────────────────────────────────────────────────────────────


async def save_greeks(
    option_id: int,
    greeks: GreeksResult,
    underlying_price: float,
) -> int:
    """Insert a Black-Scholes Greeks row linked to an options_data record.

    Args:
        option_id: Foreign key into ``options_data.id``.
        greeks: Computed :class:`~backend.core.greeks_engine.GreeksResult`.
        underlying_price: Underlying price (S) used for the calculation.

    Returns:
        The newly inserted row ID.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO calculated_greeks
                (option_id, delta, gamma, vega, theta, rho, underlying_price, timestamp)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            option_id,
            greeks.delta,
            greeks.gamma,
            greeks.vega,
            greeks.theta,
            greeks.rho,
            underlying_price,
            greeks.calculated_at,
        )
    logger.debug("Saved greeks id=%d for option_id=%d.", row["id"], option_id)
    return row["id"]


# ── Signals ───────────────────────────────────────────────────────────────────


async def save_signal(
    symbol: str,
    signal_type: str,
    direction: str,
    strength: float,
    strategy: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert a trading signal.

    Args:
        symbol: Underlying ticker.
        signal_type: Category label (e.g. ``"momentum_breakout"``).
        direction: ``"long"`` or ``"short"``.
        strength: Confidence score in [0, 1].
        strategy: Strategy name (e.g. ``"0DTE_scalp"``).
        metadata: Arbitrary JSON payload for extra context.

    Returns:
        The newly inserted signal ID.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO signals
                (symbol, signal_type, direction, strength, strategy_name, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            symbol,
            signal_type,
            direction,
            strength,
            strategy,
            json.dumps(metadata or {}),
        )
    logger.info(
        "Signal saved: id=%d  %s %s %s  strength=%.3f",
        row["id"], strategy, symbol, direction, strength,
    )
    return row["id"]


# ── Trades ────────────────────────────────────────────────────────────────────


async def save_trade(trade: dict[str, Any]) -> int:
    """Insert a new trade record (status defaults to ``'open'``).

    *trade* must contain: ``symbol``, ``action``, ``quantity``,
    ``entry_price``.  Optional keys: ``strike``, ``expiry``, ``option_type``,
    ``strategy``.

    Args:
        trade: Dict of trade fields.

    Returns:
        The newly inserted trade ID.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO trades
                (symbol, strike, expiry, option_type, action,
                 quantity, entry_price, strategy)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            trade["symbol"],
            trade.get("strike"),
            trade.get("expiry"),
            trade.get("option_type"),
            trade["action"],
            trade["quantity"],
            trade["entry_price"],
            trade.get("strategy", ""),
        )
    logger.info(
        "Trade opened: id=%d  %s %s x%d @ %.2f",
        row["id"], trade["action"], trade["symbol"],
        trade["quantity"], trade["entry_price"],
    )
    return row["id"]


async def update_trade_closed(trade_id: int, exit_price: float, pnl: float) -> None:
    """Mark a trade as closed and record its exit price and PnL.

    Args:
        trade_id: Primary key of the trade to close.
        exit_price: Fill price of the closing order.
        pnl: Realised profit/loss in dollars.
    """
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE trades
            SET exit_price = $1,
                pnl        = $2,
                status     = 'closed',
                closed_at  = NOW()
            WHERE id = $3
            """,
            exit_price, pnl, trade_id,
        )
    logger.info(
        "Trade closed: id=%d  exit=%.2f  pnl=%+.2f",
        trade_id, exit_price, pnl,
    )


# ── Queries ───────────────────────────────────────────────────────────────────


async def get_open_trades() -> list[dict[str, Any]]:
    """Return all trades with ``status = 'open'``, newest first.

    Returns:
        List of trade dicts (all columns from ``trades``).
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC"
        )
    return [dict(r) for r in rows]


async def get_recent_signals(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent signals, newest first.

    Args:
        limit: Maximum number of rows to return. Default 20.

    Returns:
        List of signal dicts (all columns from ``signals``).
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


async def get_latest_greeks(
    symbol: str,
    strike: float,
    expiry: date,
) -> dict[str, Any] | None:
    """Return the most recent Greeks row for a specific option contract.

    Joins ``calculated_greeks`` with ``options_data`` to filter by the
    underlying symbol, strike, and expiry.

    Args:
        symbol: Underlying ticker (e.g. ``"SPY"``).
        strike: Strike price.
        expiry: Option expiry date.

    Returns:
        Dict of all ``calculated_greeks`` columns plus ``option_type``,
        ``strike``, ``expiry``, and ``symbol`` from ``options_data``,
        or ``None`` if no matching row exists.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cg.*, od.option_type, od.strike, od.expiry, od.symbol
            FROM   calculated_greeks cg
            JOIN   options_data od ON od.id = cg.option_id
            WHERE  od.symbol = $1
              AND  od.strike = $2
              AND  od.expiry = $3
            ORDER BY cg.timestamp DESC
            LIMIT 1
            """,
            symbol,
            strike,
            expiry,
        )
    return dict(row) if row is not None else None


async def get_latest_options(symbol: str) -> list[dict[str, Any]]:
    """Return the most recent options chain snapshot for *symbol*.

    Finds the latest ``timestamp`` value for the given symbol and returns
    all contracts from that snapshot, ordered by expiry then strike.

    Args:
        symbol: Underlying ticker (e.g. ``"SPY"``).

    Returns:
        List of option contract dicts, or an empty list if none exist.
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM   options_data
            WHERE  symbol    = $1
              AND  timestamp = (
                      SELECT MAX(timestamp)
                      FROM   options_data
                      WHERE  symbol = $1
                  )
            ORDER BY expiry, strike
            """,
            symbol,
        )
    return [dict(r) for r in rows]
