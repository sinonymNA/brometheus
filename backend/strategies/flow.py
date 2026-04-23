"""Unusual options flow strategy for APEX CRUSHER.

Detects institutional footprints by identifying option contracts with
abnormally high volume-to-open-interest ratios and large notional premium,
then trades in the same direction as the unusual flow.
"""

from __future__ import annotations

from typing import Any

from backend.strategies.base import BaseStrategy
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class FlowStrategy(BaseStrategy):
    """Follow unusual institutional options flow."""

    SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD", "META"]

    MIN_PREMIUM_THRESHOLD = 50_000   # notional = bid × volume × 100 ≥ $50 k
    VOLUME_OI_RATIO_MIN   = 3.0      # volume must be ≥ 3× open interest
    MIN_BID_ASK_FILL_PCT  = 0.70     # bid / ask ≥ 70 % (tight-spread filter)

    async def scan(self) -> list[dict[str, Any]]:
        """Identify unusual flow and return up to 3 signals, strongest first."""
        open_symbols = set(await self.get_open_trade_symbols())

        try:
            candidates = await self._query_unusual_flow()
        except Exception as exc:
            logger.error("FlowStrategy: flow query failed: %s", exc)
            return []

        signals: list[dict[str, Any]] = []
        seen: set[str] = set()

        for row in candidates:
            symbol = row["symbol"]
            if symbol not in self.SYMBOLS or symbol in open_symbols or symbol in seen:
                continue

            # Find a tradeable contract that matches the flow direction
            option = await self.find_best_option(
                symbol=symbol,
                direction=row["option_type"],
                spot_price=0.0,
                min_dte=5,
                max_dte=45,
                target_delta=0.40,
            )
            if option is None:
                continue

            voi_ratio = float(row["voi_ratio"])
            strength  = round(min(voi_ratio / 10.0, 1.0), 4)

            signals.append({
                "symbol":           symbol,
                "direction":        row["option_type"],
                "strength":         strength,
                "strategy_name":    "flow",
                "signal_type":      "unusual_flow",
                "volume_oi_ratio":  round(voi_ratio, 2),
                "notional_premium": float(row["premium"]),
                "flow_strike":      float(row["strike"]),
                "flow_expiry":      row["expiry"],
                **option,
            })
            seen.add(symbol)

            if len(signals) >= 3:
                break

        return signals

    async def _query_unusual_flow(self) -> list[dict[str, Any]]:
        """Query options_data for the last 15 minutes, returning unusual-flow rows.

        Filters by volume/OI ratio, notional premium, and bid-ask quality.
        Results are ordered by volume/OI ratio descending so the most
        aggressive flow appears first.

        Returns:
            List of dicts with ``symbol``, ``option_type``, ``strike``,
            ``expiry``, ``bid``, ``ask``, ``volume``, ``open_interest``,
            ``voi_ratio``, ``premium``.
        """
        from backend.data.storage import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    symbol,
                    option_type,
                    strike,
                    expiry,
                    bid,
                    ask,
                    volume,
                    open_interest,
                    CAST(volume AS FLOAT) / CAST(open_interest AS FLOAT)   AS voi_ratio,
                    CAST(bid AS FLOAT) * volume * 100                      AS premium
                FROM   options_data
                WHERE  timestamp      >= NOW() - INTERVAL '15 minutes'
                  AND  volume          > 0
                  AND  open_interest   > 0
                  AND  bid             > 0
                  AND  ask             > 0
                  AND  CAST(volume AS FLOAT) / CAST(open_interest AS FLOAT)
                       >= $1
                  AND  CAST(bid AS FLOAT) * volume * 100
                       >= $2
                  AND  CAST(bid AS FLOAT) / CAST(ask AS FLOAT)
                       >= $3
                ORDER BY CAST(volume AS FLOAT) / CAST(open_interest AS FLOAT) DESC
                """,
                float(self.VOLUME_OI_RATIO_MIN),
                float(self.MIN_PREMIUM_THRESHOLD),
                float(self.MIN_BID_ASK_FILL_PCT),
            )

        return [dict(r) for r in rows]
