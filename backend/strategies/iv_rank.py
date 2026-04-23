"""IV rank iron-condor strategy for APEX CRUSHER.

Sells iron condors when implied volatility is historically elevated,
collecting premium that is expected to decay as IV reverts to the mean.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from backend.strategies.base import BaseStrategy
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class IVRankStrategy(BaseStrategy):
    """Sell iron condors when the symbol's IV rank is ≥ 70."""

    SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT"]

    IV_RANK_MIN = 70             # minimum IV rank to enter a trade
    MIN_PREMIUM_COLLECTED = 1.00 # minimum net credit per share ($1.00)
    MAX_LOSS_MULTIPLIER = 2.0    # max loss = MAX_LOSS_MULTIPLIER × premium (risk/reward)

    _MIN_DTE = 14
    _MAX_DTE = 45

    async def scan(self) -> list[dict[str, Any]]:
        """Scan symbols for iron-condor entry opportunities."""
        open_symbols = set(await self.get_open_trade_symbols())
        signals: list[dict[str, Any]] = []

        for symbol in self.SYMBOLS:
            if symbol in open_symbols:
                continue
            try:
                sig = await self._evaluate(symbol)
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                logger.warning("IVRankStrategy: %s failed: %s", symbol, exc)

        return signals

    async def _evaluate(self, symbol: str) -> dict[str, Any] | None:
        # 1. IV rank check
        iv_rank = await self._get_iv_rank(symbol)
        if iv_rank < self.IV_RANK_MIN:
            return None

        # 2. Current spot price from Redis cache
        from backend.data.fetcher import get_cached_price
        spot = await get_cached_price(symbol)
        if spot is None or spot <= 0:
            logger.debug("IVRankStrategy: no cached price for %s", symbol)
            return None

        # 3. Target strikes for the condor
        #    Short strikes 5 % OTM, long strikes 10 % OTM
        target_short_call = round(spot * 1.05, 2)
        target_long_call  = round(spot * 1.10, 2)
        target_short_put  = round(spot * 0.95, 2)
        target_long_put   = round(spot * 0.90, 2)

        today     = date.today()
        min_exp   = today + timedelta(days=self._MIN_DTE)
        max_exp   = today + timedelta(days=self._MAX_DTE)

        # Find the short call first — its expiry anchors all remaining legs.
        short_call = await self._find_option_at_strike(
            symbol, "call", target_short_call, min_exp, max_exp
        )
        if short_call is None:
            return None

        anchor_expiry = short_call["expiry"]

        long_call = await self._find_option_at_strike(
            symbol, "call", target_long_call, min_exp, max_exp, expiry=anchor_expiry
        )
        short_put = await self._find_option_at_strike(
            symbol, "put", target_short_put, min_exp, max_exp, expiry=anchor_expiry
        )
        long_put = await self._find_option_at_strike(
            symbol, "put", target_long_put, min_exp, max_exp, expiry=anchor_expiry
        )

        if any(leg is None for leg in (long_call, short_put, long_put)):
            logger.debug("IVRankStrategy: incomplete condor legs for %s", symbol)
            return None

        # 4. Net credit: receive short bids, pay long asks
        net_premium = (
            (short_call["bid"] + short_put["bid"])
            - (long_call["ask"] + long_put["ask"])  # type: ignore[index]
        )
        if net_premium < self.MIN_PREMIUM_COLLECTED:
            logger.debug(
                "IVRankStrategy: %s net premium %.2f < %.2f",
                symbol, net_premium, self.MIN_PREMIUM_COLLECTED,
            )
            return None

        # 5. Signal strength: how far above IV_RANK_MIN we are, capped at 1.0
        strength = round(min((iv_rank - self.IV_RANK_MIN) / 30.0, 1.0), 4)

        legs = [
            {
                "action": "sell",
                "strike": short_call["strike"],
                "expiry": short_call["expiry"],
                "option_type": "call",
            },
            {
                "action": "buy",
                "strike": long_call["strike"],   # type: ignore[index]
                "expiry": long_call["expiry"],   # type: ignore[index]
                "option_type": "call",
            },
            {
                "action": "sell",
                "strike": short_put["strike"],   # type: ignore[index]
                "expiry": short_put["expiry"],   # type: ignore[index]
                "option_type": "put",
            },
            {
                "action": "buy",
                "strike": long_put["strike"],    # type: ignore[index]
                "expiry": long_put["expiry"],    # type: ignore[index]
                "option_type": "put",
            },
        ]

        return {
            "symbol": symbol,
            "direction": "short",
            "strength": strength,
            "strategy_name": "iv_rank",
            "signal_type": "iron_condor",
            "iv_rank": round(iv_rank, 1),
            "net_premium": round(net_premium, 2),
            "spot_price": spot,
            "legs": legs,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_iv_rank(self, symbol: str) -> float:
        """Return IV rank (0–100) from the past 52 weeks. Returns 50.0 if data is sparse."""
        from backend.data.storage import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    MIN(iv) AS iv_low,
                    MAX(iv) AS iv_high,
                    (
                        SELECT iv
                        FROM   options_data
                        WHERE  symbol = $1 AND iv IS NOT NULL
                        ORDER  BY timestamp DESC
                        LIMIT  1
                    ) AS iv_current
                FROM  options_data
                WHERE symbol    = $1
                  AND iv        IS NOT NULL
                  AND timestamp >= NOW() - INTERVAL '52 weeks'
                """,
                symbol,
            )

        if row is None or row["iv_low"] is None or row["iv_high"] is None:
            return 50.0

        iv_low = float(row["iv_low"])
        iv_high = float(row["iv_high"])
        iv_current = float(row["iv_current"]) if row["iv_current"] is not None else None

        if iv_current is None or iv_high == iv_low:
            return 50.0

        return max(0.0, min((iv_current - iv_low) / (iv_high - iv_low) * 100.0, 100.0))

    async def _find_option_at_strike(
        self,
        symbol: str,
        option_type: str,
        target_strike: float,
        min_expiry: date,
        max_expiry: date,
        expiry: date | None = None,
    ) -> dict[str, Any] | None:
        """Return the available option contract whose strike is closest to *target_strike*.

        When *expiry* is supplied the search is pinned to that exact date;
        otherwise the first result within [min_expiry, max_expiry] is used.

        Args:
            symbol: Underlying ticker.
            option_type: ``"call"`` or ``"put"``.
            target_strike: Desired strike price.
            min_expiry: Earliest acceptable expiry.
            max_expiry: Latest acceptable expiry.
            expiry: Pin search to this expiry date when provided.

        Returns:
            Dict with ``strike``, ``expiry``, ``bid``, ``ask``, or ``None``.
        """
        from backend.data.storage import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            if expiry is not None:
                rows = await conn.fetch(
                    """
                    SELECT strike, expiry, bid, ask
                    FROM   options_data
                    WHERE  symbol      = $1
                      AND  option_type = $2
                      AND  expiry      = $3
                      AND  bid         > 0
                      AND  ask         > 0
                      AND  timestamp   = (
                              SELECT MAX(timestamp)
                              FROM   options_data
                              WHERE  symbol = $1
                          )
                    ORDER BY ABS(strike - $4) ASC
                    LIMIT  1
                    """,
                    symbol, option_type, expiry, target_strike,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT strike, expiry, bid, ask
                    FROM   options_data
                    WHERE  symbol      = $1
                      AND  option_type = $2
                      AND  expiry      BETWEEN $3 AND $4
                      AND  bid         > 0
                      AND  ask         > 0
                      AND  timestamp   = (
                              SELECT MAX(timestamp)
                              FROM   options_data
                              WHERE  symbol = $1
                          )
                    ORDER BY ABS(strike - $5) ASC
                    LIMIT  1
                    """,
                    symbol, option_type, min_expiry, max_expiry, target_strike,
                )

        if not rows:
            return None

        r = rows[0]
        return {
            "strike": float(r["strike"]),
            "expiry": r["expiry"],
            "bid":    float(r["bid"]),
            "ask":    float(r["ask"]),
        }
