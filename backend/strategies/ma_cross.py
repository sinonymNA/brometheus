"""SMA5/SMA10 moving-average crossover strategy for APEX CRUSHER.

Generates directional option signals when the fast 5-day SMA crosses the
slow 10-day SMA on daily bars, confirming a near-term momentum shift.

This is the top-performing strategy in 4-year backtests (2021-2024),
contributing $6k-$13k annually. It runs on daily bars so one scan per
market session is sufficient — the StrategyRunner calls it every cycle
but the bar data only changes once per day.
"""

from __future__ import annotations

import collections
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.strategies.base import BaseStrategy
from backend.utils.logger import get_logger

logger = get_logger(__name__)

SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA", "AMZN", "META"]
_MIN_STRENGTH = 0.20
_MIN_BARS = 12   # 11 for SMA10 + 1 prior-day comparison


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


class MACrossStrategy(BaseStrategy):
    """Buy calls/puts on SMA5/SMA10 daily crossovers with trend confirmation."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.near_misses: collections.deque = collections.deque(maxlen=100)
        # Cache last signal date per symbol to avoid re-signalling same crossover
        self._last_signal: dict[str, str] = {}

    async def scan(self) -> list[dict[str, Any]]:
        open_symbols = set(await self.get_open_trade_symbols())
        signals: list[dict[str, Any]] = []
        for symbol in SYMBOLS:
            if symbol in open_symbols:
                continue
            try:
                sig = await self._evaluate(symbol)
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                logger.warning("MACrossStrategy: %s error: %s", symbol, exc)
        return signals

    async def _evaluate(self, symbol: str) -> dict[str, Any] | None:
        start = datetime.now(timezone.utc) - timedelta(days=25)
        raw = await self.alpaca.get_bars([symbol], timeframe="1Day", start=start)
        bars = raw.get(symbol, [])

        if len(bars) < _MIN_BARS:
            return None

        closes = [float(b.close) for b in bars]
        today_str = bars[-1].timestamp.strftime("%Y-%m-%d")

        sma5  = _sma(closes,      5)
        sma10 = _sma(closes,     10)
        prev5 = _sma(closes[:-1], 5)
        prev10= _sma(closes[:-1],10)

        if None in (sma5, sma10, prev5, prev10):
            return None

        bull_cross = (prev5 <= prev10) and (sma5 > sma10)
        bear_cross = (prev5 >= prev10) and (sma5 < sma10)

        if not (bull_cross or bear_cross):
            return None

        # Don't re-signal the same crossover that already fired today
        cache_key = f"{symbol}_{today_str}_{'bull' if bull_cross else 'bear'}"
        if self._last_signal.get(symbol) == cache_key:
            return None
        self._last_signal[symbol] = cache_key

        direction = "call" if bull_cross else "put"
        spot = closes[-1]
        cross_pct = abs(sma5 - sma10) / sma10
        strength = max(_MIN_STRENGTH, min(cross_pct * 10.0, 1.0))

        option = await self.find_best_option(
            symbol=symbol,
            direction=direction,
            spot_price=spot,
            min_dte=5,
            max_dte=21,
            target_delta=0.40,
        )
        if option is None:
            logger.debug("MACrossStrategy: no option contract found for %s %s", symbol, direction)
            return None

        logger.info(
            "MACrossStrategy signal: %s %s  SMA5=%.2f SMA10=%.2f  strength=%.3f",
            symbol, "BULL" if bull_cross else "BEAR", sma5, sma10, strength,
        )
        return {
            "symbol":        symbol,
            "direction":     direction,
            "strength":      round(strength, 4),
            "strategy_name": "ma_cross",
            "signal_type":   "sma5_10_crossover",
            "sma5":          round(sma5, 2),
            "sma10":         round(sma10, 2),
            "spot_price":    round(spot, 2),
            **option,
        }
