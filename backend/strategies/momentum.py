"""Momentum breakout strategy for APEX CRUSHER.

Generates directional option signals when RSI, volume surge, price breakout,
and a benign IV environment are all present simultaneously.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import ta

from backend.strategies.base import BaseStrategy
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class MomentumStrategy(BaseStrategy):
    """Buy calls/puts on confirmed momentum breakouts with low IV."""

    SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD", "META"]

    RSI_BULL_THRESHOLD = 62    # RSI must be above this for a bullish signal
    RSI_BEAR_THRESHOLD = 38    # RSI must be below this for a bearish signal
    VOLUME_RATIO_MIN = 1.5     # current bar volume / 20-bar avg
    IV_RANK_MAX = 60           # avoid expensive options when IV is already high

    async def scan(self) -> list[dict[str, Any]]:
        """Scan all symbols and return signals for untraded symbols that qualify."""
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
                logger.warning("MomentumStrategy: %s failed: %s", symbol, exc)

        return signals

    async def _evaluate(self, symbol: str) -> dict[str, Any] | None:
        # 1. Fetch last 30 5-min bars (request 2 days to cover partial trading days)
        start = datetime.now(timezone.utc) - timedelta(days=2)
        raw = await self.alpaca.get_bars([symbol], timeframe="5min", start=start)
        bars = raw.get(symbol, [])[-30:]

        if len(bars) < 21:
            logger.debug("MomentumStrategy: only %d bars for %s — skipping", len(bars), symbol)
            return None

        # 2. RSI(14)
        rsi = await self.calculate_rsi(bars)
        if math.isnan(rsi):
            return None

        # 3. Volume ratio: current bar / average of previous 20 bars
        volume_ratio = await self.calculate_volume_ratio(bars)

        # 4. Price breakout vs. the 10 bars preceding the current bar
        closes = [float(b.close) for b in bars]
        current = closes[-1]
        window = closes[-11:-1]
        if len(window) < 10:
            return None
        high_10 = max(window)
        low_10 = min(window)

        # 5. IV rank — stay out when options are already expensive
        iv_rank = await self.get_iv_rank(symbol)

        bull = (
            rsi >= self.RSI_BULL_THRESHOLD
            and volume_ratio >= self.VOLUME_RATIO_MIN
            and current > high_10
            and iv_rank <= self.IV_RANK_MAX
        )
        bear = (
            rsi <= self.RSI_BEAR_THRESHOLD
            and volume_ratio >= self.VOLUME_RATIO_MIN
            and current < low_10
            and iv_rank <= self.IV_RANK_MAX
        )

        if not (bull or bear):
            return None

        direction = "call" if bull else "put"

        # Signal strength: mean of four normalised excess values (each ∈ [0, 1])
        if bull:
            rsi_excess = min((rsi - self.RSI_BULL_THRESHOLD) / (100.0 - self.RSI_BULL_THRESHOLD), 1.0)
            price_excess = min((current - high_10) / high_10, 1.0)
        else:
            rsi_excess = min((self.RSI_BEAR_THRESHOLD - rsi) / float(self.RSI_BEAR_THRESHOLD), 1.0)
            price_excess = min((low_10 - current) / low_10, 1.0)

        vol_excess = min((volume_ratio - self.VOLUME_RATIO_MIN) / self.VOLUME_RATIO_MIN, 1.0)
        iv_excess = (self.IV_RANK_MAX - iv_rank) / float(self.IV_RANK_MAX)

        strength = round(
            max(0.0, min((rsi_excess + vol_excess + iv_excess + price_excess) / 4.0, 1.0)),
            4,
        )

        option = await self.find_best_option(
            symbol=symbol,
            direction=direction,
            spot_price=current,
        )
        if option is None:
            logger.debug("MomentumStrategy: no option found for %s %s", symbol, direction)
            return None

        return {
            "symbol": symbol,
            "direction": direction,
            "strength": strength,
            "strategy_name": "momentum",
            "signal_type": "momentum_breakout",
            "rsi": round(rsi, 2),
            "volume_ratio": round(volume_ratio, 3),
            "iv_rank": round(iv_rank, 1),
            "spot_price": current,
            **option,
        }

    async def calculate_rsi(self, bars: list, period: int = 14) -> float:
        """Compute RSI(*period*) for the last bar in *bars*.

        Args:
            bars: List of alpaca-py ``Bar`` objects, oldest first.
            period: RSI look-back period. Default 14.

        Returns:
            RSI value for the final bar, or ``float('nan')`` if insufficient data.
        """
        closes = pd.Series([float(b.close) for b in bars])
        rsi_series = ta.momentum.RSIIndicator(close=closes, window=period).rsi()
        val = rsi_series.iloc[-1]
        return float(val) if not pd.isna(val) else float("nan")

    async def calculate_volume_ratio(self, bars: list) -> float:
        """Return current bar volume divided by the average of the prior 20 bars.

        Args:
            bars: List of alpaca-py ``Bar`` objects (need ≥ 21 entries).

        Returns:
            Volume ratio, or ``0.0`` when there is insufficient data or zero average.
        """
        if len(bars) < 21:
            return 0.0
        volumes = [float(getattr(b, "volume", 0) or 0) for b in bars]
        avg = sum(volumes[-21:-1]) / 20.0
        if avg == 0.0:
            return 0.0
        return volumes[-1] / avg

    async def get_iv_rank(self, symbol: str) -> float:
        """Return IV rank (0–100) for *symbol* based on the past 52 weeks of data.

        IV rank = (current_IV − 52wk_low) / (52wk_high − 52wk_low) × 100

        Returns ``50.0`` (neutral) when the database has insufficient history.

        Args:
            symbol: Underlying ticker (e.g. ``"SPY"``).

        Returns:
            IV rank as a percentage in ``[0.0, 100.0]``.
        """
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
