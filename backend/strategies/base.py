"""Abstract base class for all APEX CRUSHER trading strategies.

Usage::

    class MyStrategy(BaseStrategy):
        async def scan(self) -> list[dict]:
            ...
"""

from __future__ import annotations

import abc
from datetime import date, timedelta
from typing import Any

from backend.data.alpaca_client import AlpacaClient
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class BaseStrategy(abc.ABC):
    """Abstract strategy base.  Subclasses must implement :meth:`scan`."""

    def __init__(self, alpaca_client: AlpacaClient, storage: Any = None) -> None:
        self.alpaca = alpaca_client
        self.storage = storage  # module reference; each method imports what it needs

    @abc.abstractmethod
    async def scan(self) -> list[dict[str, Any]]:
        """Scan the market and return a list of trade-signal dicts."""
        ...

    async def get_open_trade_symbols(self) -> list[str]:
        """Return the symbols that already have an open trade in the database."""
        from backend.data.storage import get_open_trades
        trades = await get_open_trades()
        return [t["symbol"] for t in trades]

    async def find_best_option(
        self,
        symbol: str,
        direction: str,
        spot_price: float,
        min_dte: int = 5,
        max_dte: int = 45,
        target_delta: float = 0.40,
    ) -> dict[str, Any] | None:
        """Find the best-matching option contract from the most recent snapshot.

        Queries ``options_data`` joined with ``calculated_greeks`` to rank
        contracts by how close their stored (call-side) delta is to the
        effective target delta.  For put options the target is converted so
        that ``|put_delta| ≈ target_delta`` (i.e. call delta ≈ 1 − target).

        Args:
            symbol: Underlying ticker (e.g. ``"SPY"``).
            direction: ``"call"`` or ``"put"``.
            spot_price: Current underlying mid-price (informational; not
                used to filter contracts).
            min_dte: Minimum days to expiry. Default 5.
            max_dte: Maximum days to expiry. Default 45.
            target_delta: Desired absolute delta magnitude. Default 0.40.

        Returns:
            Dict with ``strike``, ``expiry``, ``bid``, ``ask``, ``delta``,
            ``iv``, or ``None`` if no qualifying contract exists.
        """
        from backend.data.storage import get_pool

        today = date.today()
        min_expiry = today + timedelta(days=min_dte)
        max_expiry = today + timedelta(days=max_dte)

        # GreeksResult stores call delta ∈ (0,1).  For puts we want
        # |put_delta| ≈ target_delta  →  call_delta ≈ 1 − target_delta.
        effective_delta = (
            target_delta if direction == "call" else (1.0 - target_delta)
        )

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH latest_snap AS (
                    SELECT MAX(timestamp) AS ts
                    FROM   options_data
                    WHERE  symbol = $1
                ),
                opts AS (
                    SELECT DISTINCT ON (od.id)
                        od.strike,
                        od.expiry,
                        od.bid,
                        od.ask,
                        od.iv,
                        cg.delta
                    FROM   options_data od
                    CROSS JOIN latest_snap ls
                    JOIN   calculated_greeks cg ON cg.option_id = od.id
                    WHERE  od.symbol      = $1
                      AND  od.option_type = $2
                      AND  od.expiry      BETWEEN $3 AND $4
                      AND  od.bid         > 0.10
                      AND  od.timestamp   = ls.ts
                      AND  cg.delta       IS NOT NULL
                    ORDER BY od.id, cg.timestamp DESC
                )
                SELECT strike, expiry, bid, ask, iv, delta
                FROM   opts
                ORDER BY ABS(delta - $5) ASC
                LIMIT  1
                """,
                symbol,
                direction,
                min_expiry,
                max_expiry,
                effective_delta,
            )

        if not rows:
            return None

        r = rows[0]
        return {
            "strike": float(r["strike"]),
            "expiry": r["expiry"],
            "bid":    float(r["bid"]),
            "ask":    float(r["ask"]),
            "delta":  float(r["delta"]),
            "iv":     float(r["iv"]) if r["iv"] is not None else None,
        }
