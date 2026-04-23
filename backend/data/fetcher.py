"""Real-time market data fetcher for APEX CRUSHER.

Runs as a long-lived asyncio background task during market hours.  Each
60-second cycle fetches NBBO quotes, caches prices in Redis, saves raw
market data, fetches option chains filtered to the nearest three expiries
and strikes within ±10 % of spot, solves implied volatility, computes
full Black-Scholes Greeks, and persists everything to PostgreSQL.

Lifecycle (call from FastAPI startup / shutdown handlers)::

    from backend.data.fetcher import start, stop

    await start()   # on_startup
    await stop()    # on_shutdown

One-shot usage (testing, scripts)::

    from backend.data.fetcher import run_cycle
    n = await run_cycle()
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from backend.core.greeks_engine import calculate, calculate_iv
from backend.data.alpaca_client import alpaca
from backend.data.storage import save_greeks, save_market_data, save_options_snapshot
from backend.utils.config import settings
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

SYMBOLS: list[str] = [
    "SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD", "META",
]

_CYCLE_SECS: int = 60          # interval between fetch cycles (market hours)
_DOWN_WAIT_SECS: int = 300     # pause when Alpaca is unreachable (5 min)
_REDIS_TTL: int = 90           # price cache TTL in seconds
_RISK_FREE_RATE: float = 0.05  # annualised, used for all Greeks calculations
_STRIKE_PCT: float = 0.10      # ±10 % of spot for option chain filter
_EXPIRY_DAYS: int = 30         # forward window when requesting chains
_N_EXPIRIES: int = 3           # keep only the nearest N unique expiry dates

_ET = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)

# OCC symbol: e.g. SPY240620C00500000
_OCC_RE = re.compile(r"^([A-Z]{1,5})(\d{6})([CP])(\d{8})$")

# ── Module-level state ────────────────────────────────────────────────────────

_task: asyncio.Task[None] | None = None
_redis: aioredis.Redis | None = None  # type: ignore[type-arg]
_last_cycle_at: datetime | None = None
_total_options_priced: int = 0


# ── Public lifecycle ──────────────────────────────────────────────────────────


async def start() -> "asyncio.Task[None]":
    """Launch the fetch loop as a named asyncio background task.

    No-op if the task is already running.  Intended to be called from the
    FastAPI ``on_startup`` handler after :func:`~backend.data.storage.init_db`
    and :meth:`~backend.data.alpaca_client.AlpacaClient.connect` have
    completed.

    Returns:
        The background :class:`asyncio.Task` (already running).
    """
    global _task
    if _task is not None and not _task.done():
        logger.warning("Fetcher is already running; ignoring duplicate start().")
        return _task
    _task = asyncio.create_task(_loop(), name="apex-fetcher")
    logger.info("Fetcher started (cycle=%ds, symbols=%s).", _CYCLE_SECS, SYMBOLS)
    return _task


async def stop() -> None:
    """Cancel the background task and close the Redis connection.

    Awaits task completion so callers (e.g. shutdown handlers) can be sure
    resources are released before the process exits.
    """
    global _task, _redis

    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        logger.info("Fetcher stopped.")
    _task = None

    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.debug("Redis connection closed by fetcher.")


# ── Main loop ─────────────────────────────────────────────────────────────────


async def _loop() -> None:
    """Infinite fetch loop.  Runs :func:`run_cycle` every :data:`_CYCLE_SECS`
    during market hours; sleeps 60 s when the market is closed.

    If an unhandled exception escapes :func:`run_cycle` (indicating Alpaca is
    unreachable or returning fatal errors), the loop pauses for
    :data:`_DOWN_WAIT_SECS` before retrying rather than spinning.
    """
    while True:
        if not is_market_open():
            logger.info("Market closed — sleeping 60s.")
            await asyncio.sleep(60)
            continue

        try:
            n = await run_cycle()
            global _last_cycle_at, _total_options_priced
            _last_cycle_at = datetime.now(timezone.utc)
            _total_options_priced += n
            logger.info("Cycle complete — %d options priced.", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Alpaca appears to be down (%s). Pausing %ds before retry.",
                exc,
                _DOWN_WAIT_SECS,
            )
            await asyncio.sleep(_DOWN_WAIT_SECS)
            continue

        await asyncio.sleep(_CYCLE_SECS)


# ── Core cycle (public for testing) ──────────────────────────────────────────


async def run_cycle() -> int:
    """Run one complete fetch-price-save cycle for all :data:`SYMBOLS`.

    Steps:

    1. Fetch NBBO quotes for all symbols in one API call.
    2. For each symbol (errors isolated per-symbol):

       a. Cache spot price in Redis (TTL :data:`_REDIS_TTL`).
       b. Save the quote row to ``market_data``.
       c. Fetch the option chain (nearest :data:`_EXPIRY_DAYS` days,
          strikes within :data:`_STRIKE_PCT` of spot).
       d. Keep only contracts from the :data:`_N_EXPIRIES` nearest expiries.
       e. Solve implied volatility and compute Greeks for each contract.
       f. Batch-save options and Greeks to PostgreSQL.

    Returns:
        Total number of option contracts that were successfully priced and
        saved across all symbols.

    Raises:
        Any exception from the bulk quote fetch (signals Alpaca is down and
        causes the loop to back off).  Per-symbol errors are caught internally
        and do **not** propagate.
    """
    # A failure here propagates → treated as "Alpaca down" by the loop.
    quotes = await alpaca.get_quotes(SYMBOLS)

    total = 0
    for symbol in SYMBOLS:
        try:
            total += await _process_symbol(symbol, quotes)
        except Exception as exc:
            logger.error("Symbol %s failed in cycle: %s", symbol, exc)

    return total


# ── Per-symbol processing ─────────────────────────────────────────────────────


async def _process_symbol(
    symbol: str,
    quotes: dict[str, Any],
) -> int:
    """Cache, persist, and price the option chain for one underlying symbol.

    Args:
        symbol: Ticker to process (e.g. ``"SPY"``).
        quotes: Full quote map returned by
            :meth:`~backend.data.alpaca_client.AlpacaClient.get_quotes`.

    Returns:
        Number of option contracts priced for this symbol.
    """
    quote = quotes.get(symbol)
    if quote is None:
        logger.warning("No quote for %s in this cycle; skipping.", symbol)
        return 0

    bid = float(getattr(quote, "bid_price", None) or 0)
    ask = float(getattr(quote, "ask_price", None) or 0)

    if bid > 0 and ask > 0:
        spot = (bid + ask) / 2.0
    elif ask > 0:
        spot = ask
    elif bid > 0:
        spot = bid
    else:
        logger.warning("Zero bid/ask for %s; skipping.", symbol)
        return 0

    # ── Redis price cache ─────────────────────────────────────────────────────
    redis_client = await _get_redis()
    cached_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    await redis_client.set(f"price:{symbol}", str(spot), ex=_REDIS_TTL)
    await redis_client.set(f"price_ts:{symbol}", cached_at, ex=_REDIS_TTL)

    # ── Persist market data row ───────────────────────────────────────────────
    ask_size = int(getattr(quote, "ask_size", 0) or 0)
    await save_market_data(
        symbol=symbol,
        price=spot,
        volume=ask_size,
        bid=bid,
        ask=ask,
    )

    # ── Fetch option chain ────────────────────────────────────────────────────
    today = date.today()
    chain = await alpaca.get_option_chain(
        symbol=symbol,
        expiry_start=today,
        expiry_end=today + timedelta(days=_EXPIRY_DAYS),
        strike_min=round(spot * (1.0 - _STRIKE_PCT), 2),
        strike_max=round(spot * (1.0 + _STRIKE_PCT), 2),
    )

    if not chain:
        logger.debug("Empty option chain returned for %s.", symbol)
        return 0

    nearest = _nearest_n_expiries(chain, _N_EXPIRIES)
    contracts = {
        occ: snap
        for occ, snap in chain.items()
        if _expiry_from_occ(occ) in nearest
    }

    if not contracts:
        return 0

    return await _price_and_save(symbol, spot, contracts)


async def _price_and_save(
    symbol: str,
    spot: float,
    contracts: dict[str, Any],
) -> int:
    """Solve IV, compute Greeks, and persist options + Greeks for one symbol.

    Only contracts with a non-zero bid or ask are processed.  Contracts
    whose IV cannot be solved (deep ITM/OTM, zero vega) are saved to
    ``options_data`` with a NULL IV and skipped for Greeks.

    Args:
        symbol: Underlying ticker.
        spot: Mid-price of the underlying at the time of the cycle.
        contracts: OCC-symbol → ``OptionSnapshot`` mapping, pre-filtered
            to the desired expiries.

    Returns:
        Number of contracts for which Greeks were successfully computed and
        saved.
    """
    option_rows: list[dict[str, Any]] = []
    iv_per_row: list[float | None] = []

    for occ, snapshot in contracts.items():
        try:
            _, exp_date, opt_type, strike = _parse_occ_symbol(occ)
        except ValueError as exc:
            logger.debug("Skipping unrecognised OCC symbol %r: %s", occ, exc)
            continue

        latest_quote = getattr(snapshot, "latest_quote", None)
        if latest_quote is None:
            continue

        bid = float(getattr(latest_quote, "bid_price", None) or 0)
        ask = float(getattr(latest_quote, "ask_price", None) or 0)

        if bid <= 0 and ask <= 0:
            continue  # no market; can't solve IV

        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (ask or bid)

        T = _years_to_expiry(exp_date)
        iv = calculate_iv(mid, spot, strike, T, _RISK_FREE_RATE, opt_type)

        volume = int(getattr(snapshot, "volume", 0) or 0)
        oi = int(getattr(snapshot, "open_interest", 0) or 0)

        option_rows.append({
            "symbol": symbol,
            "strike": strike,
            "expiry": exp_date,
            "option_type": opt_type,
            "bid": bid if bid > 0 else None,
            "ask": ask if ask > 0 else None,
            "volume": volume,
            "open_interest": oi,
            "iv": iv,
            "timestamp": datetime.now(timezone.utc),
        })
        iv_per_row.append(iv)

    if not option_rows:
        return 0

    # Batch-insert options; get back DB IDs in the same order.
    ids = await save_options_snapshot(option_rows)

    priced = 0
    for option_id, row, iv in zip(ids, option_rows, iv_per_row):
        if iv is None or iv <= 0:
            continue
        try:
            T = _years_to_expiry(row["expiry"])
            greeks = calculate(
                S=spot,
                K=float(row["strike"]),
                T=T,
                r=_RISK_FREE_RATE,
                sigma=iv,
                symbol=row["symbol"],
                expiry=str(row["expiry"]),
            )
            await save_greeks(option_id, greeks, underlying_price=spot)
            priced += 1
        except Exception as exc:
            logger.debug(
                "Greeks calculation failed for option_id=%d (%s K=%.2f): %s",
                option_id, row["option_type"], row["strike"], exc,
            )

    return priced


# ── Public helpers ────────────────────────────────────────────────────────────


async def get_cached_market_data(symbol: str) -> "dict | None":
    """Read the cached spot price and timestamp for *symbol* from Redis.

    Returns:
        Dict with ``symbol``, ``price`` (float), and ``cached_at`` (ISO string),
        or ``None`` if the cache entry is absent or expired.
    """
    try:
        client = await _get_redis()
        price_val, ts_val = await asyncio.gather(
            client.get(f"price:{symbol}"),
            client.get(f"price_ts:{symbol}"),
        )
        if price_val is None:
            return None
        return {
            "symbol": symbol,
            "price": float(price_val),
            "cached_at": ts_val,
        }
    except Exception as exc:
        logger.warning("Redis read failed for %s: %s", symbol, exc)
        return None


def get_pipeline_state() -> "dict":
    """Return a snapshot of the fetcher's internal bookkeeping state.

    Returns:
        Dict with ``last_cycle_at`` (ISO string or ``None``),
        ``total_options_priced`` (int), and ``is_running`` (bool).
    """
    running = _task is not None and not _task.done()
    last = (
        _last_cycle_at.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        if _last_cycle_at is not None
        else None
    )
    return {
        "last_cycle_at": last,
        "total_options_priced": _total_options_priced,
        "is_running": running,
    }


async def get_cached_price(symbol: str) -> float | None:
    """Read the latest spot price for *symbol* from the Redis cache.

    The cache entry expires after :data:`_REDIS_TTL` seconds; a ``None``
    return means either the key has expired or no cycle has run yet.

    Args:
        symbol: Underlying ticker (e.g. ``"SPY"``).

    Returns:
        Cached spot price as a ``float``, or ``None`` if absent / expired.
    """
    try:
        client = await _get_redis()
        value = await client.get(f"price:{symbol}")
        return float(value) if value is not None else None
    except Exception as exc:
        logger.warning("Redis read failed for %s: %s", symbol, exc)
        return None


def is_market_open() -> bool:
    """Return ``True`` if the US equity market is currently open.

    Checks against regular session hours (09:30–16:00 ET, Monday–Friday).
    Does **not** account for market holidays or early-close days.

    Returns:
        ``True`` if within regular trading hours, ``False`` otherwise.
    """
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    t = now_et.time()
    return _OPEN <= t < _CLOSE


# ── Private helpers ───────────────────────────────────────────────────────────


async def _get_redis() -> aioredis.Redis:  # type: ignore[type-arg]
    """Return the module-level Redis client, initialising it on first call."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return _redis


def _parse_occ_symbol(occ: str) -> tuple[str, date, str, float]:
    """Decompose an OCC option symbol into its four components.

    OCC format: ``{UNDERLYING}{YYMMDD}{C|P}{STRIKE_8_DIGITS}``

    The strike is encoded as the price × 1000 with leading zeros, so
    ``00500000`` → $500.00.

    Args:
        occ: OCC option ticker, e.g. ``"SPY240620C00500000"``.

    Returns:
        ``(underlying, expiry_date, option_type, strike)`` where
        *option_type* is ``"call"`` or ``"put"``.

    Raises:
        ValueError: If *occ* does not match the expected pattern.
    """
    m = _OCC_RE.match(occ)
    if not m:
        raise ValueError(f"Unrecognised OCC symbol: {occ!r}")
    underlying, date_str, cp, strike_str = m.groups()
    expiry = datetime.strptime(date_str, "%y%m%d").date()
    option_type = "call" if cp == "C" else "put"
    strike = int(strike_str) / 1000.0
    return underlying, expiry, option_type, strike


def _expiry_from_occ(occ: str) -> date | None:
    """Return the expiry date parsed from *occ*, or ``None`` on failure."""
    try:
        _, expiry, _, _ = _parse_occ_symbol(occ)
        return expiry
    except ValueError:
        return None


def _nearest_n_expiries(chain: dict[str, Any], n: int) -> frozenset[date]:
    """Collect all unique expiry dates from *chain* and return the *n* nearest.

    Args:
        chain: OCC-symbol → ``OptionSnapshot`` mapping.
        n: Maximum number of expiry dates to return.

    Returns:
        Frozenset of the *n* chronologically nearest expiry dates found in
        the chain.  May contain fewer than *n* entries if the chain is small.
    """
    expiries: set[date] = set()
    for occ in chain:
        d = _expiry_from_occ(occ)
        if d is not None:
            expiries.add(d)
    return frozenset(sorted(expiries)[:n])


def _years_to_expiry(expiry: date) -> float:
    """Convert an expiry date to a fraction of a year from today.

    Applies a floor of one calendar day so that same-day expiries (0DTE)
    produce a small but positive T rather than zero (which would cause a
    division-by-zero in Black-Scholes).

    Args:
        expiry: Option expiry date.

    Returns:
        Time to expiry in years (minimum ``1 / 365``).
    """
    days = (expiry - date.today()).days
    return max(days, 1) / 365.0
