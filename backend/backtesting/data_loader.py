"""Historical data loader for APEX CRUSHER backtesting.

Downloads OHLCV bars from Alpaca and VIX from Yahoo Finance,
caches both to disk (pickle) so re-runs don't re-fetch.
"""

from __future__ import annotations

import asyncio
import hashlib
import pickle
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.greeks_engine import calculate
from backend.utils.logger import get_logger

logger = get_logger(__name__)


def _upcoming_fridays(from_date: date, max_days: int = 45) -> list[date]:
    """Return all Fridays between from_date+2 and from_date+max_days."""
    start = from_date + timedelta(days=2)
    end = from_date + timedelta(days=max_days)
    fridays: list[date] = []
    current = start
    while current <= end:
        # weekday() == 4 is Friday
        if current.weekday() == 4:
            fridays.append(current)
        current += timedelta(days=1)
    return fridays


class HistoricalDataLoader:
    def __init__(self, alpaca_client: Any) -> None:
        self._alpaca = alpaca_client
        self._cache_dir = Path("backtest_cache")
        self._cache_dir.mkdir(exist_ok=True)

    def clear_cache(self) -> None:
        """Delete all cached data files."""
        import shutil
        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir)
            self._cache_dir.mkdir(exist_ok=True)
            logger.info("Cleared backtest cache")

    async def load_stock_data(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
        timeframe: str = "1day",
    ) -> dict[str, list[Any]]:
        """Load OHLCV bars from Alpaca with disk cache.

        Returns {symbol: [Bar objects oldest-first]}.
        AlpacaClient.get_bars(symbols, timeframe, start, end) returns dict[str, list[Bar]].
        Bar has: .timestamp (datetime), .open, .high, .low, .close (float), .volume (int).

        Cache file: backtest_cache/{symbols_hash}_{start}_{end}_{timeframe}.pkl
        """
        symbols_key = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:8]
        cache_file = (
            self._cache_dir
            / f"{symbols_key}_{start_date}_{end_date}_{timeframe}.pkl"
        )

        if cache_file.exists():
            logger.info("Loading stock data from cache: %s", cache_file)
            with cache_file.open("rb") as fh:
                return pickle.load(fh)

        logger.info(
            "Fetching stock bars from Alpaca: symbols=%s start=%s end=%s tf=%s",
            symbols,
            start_date,
            end_date,
            timeframe,
        )
        # alpaca_client.get_bars returns dict[str, list[Bar]] already
        bars: dict[str, list[Any]] = await self._alpaca.get_bars(
            symbols, timeframe, start_date, end_date
        )

        logger.info("Fetched %d symbols, %d total bars",
                   len(bars), sum(len(b) for b in bars.values()))

        with cache_file.open("wb") as fh:
            pickle.dump(bars, fh)

        return bars

    async def load_vix_data(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, float]:
        """Load daily VIX levels from Yahoo Finance with disk cache.

        Returns {date: vix_close}.
        Uses yfinance: yf.download("^VIX", start=start_date, end=end_date, auto_adjust=False)
        yfinance is synchronous — run via asyncio.get_event_loop().run_in_executor(None, fn).
        Cache file: backtest_cache/vix_{start}_{end}.pkl
        Falls back to {} on error (engine will use default VIX of 20).
        """
        cache_file = self._cache_dir / f"vix_{start_date}_{end_date}.pkl"

        if cache_file.exists():
            logger.info("Loading VIX data from cache: %s", cache_file)
            with cache_file.open("rb") as fh:
                return pickle.load(fh)

        logger.info("Fetching VIX data from Yahoo Finance: %s to %s", start_date, end_date)

        def _fetch_vix() -> dict[date, float]:
            import yfinance as yf  # local import — optional dependency

            df = yf.download(
                "^VIX",
                start=start_date,
                end=end_date,
                auto_adjust=False,
                progress=False,
            )
            result: dict[date, float] = {}
            if df.empty:
                return result
            close_col = "Close"
            for idx, row in df.iterrows():
                row_date = idx.date() if hasattr(idx, "date") else idx
                val = row[close_col]
                # yfinance may return a Series for multi-level columns
                if hasattr(val, "iloc"):
                    val = float(val.iloc[0])
                else:
                    val = float(val)
                result[row_date] = val
            return result

        try:
            loop = asyncio.get_event_loop()
            vix_data = await loop.run_in_executor(None, _fetch_vix)
        except Exception as exc:
            logger.warning("Failed to fetch VIX data: %s — returning empty dict", exc)
            return {}

        with cache_file.open("wb") as fh:
            pickle.dump(vix_data, fh)

        return vix_data

    def reconstruct_options_chain(
        self,
        symbol: str,
        spot_price: float,
        vix_level: float,
        timestamp: datetime,
    ) -> list[dict]:
        """Reconstruct a synthetic options chain using Black-Scholes.

        Strikes: spot ± 10% in 1% increments (20 strikes).
        Expiries: upcoming Fridays from timestamp+2 to timestamp+45 days.
        For each strike/expiry/type:
            T = (expiry - timestamp.date()).days / 365
            sigma = max(vix_level / 100, 0.05)
            result = calculate(S=spot_price, K=strike, T=T, r=0.05, sigma=sigma)
            bid = price * 0.98, ask = price * 1.02
            volume = random.randint(100, 5000)
            open_interest = random.randint(500, 50000)
        Returns list of option contract dicts with all fields.
        Skip if price < 0.01 (too far OTM).
        """
        sigma = max(vix_level / 100.0, 0.05)
        today = timestamp.date()
        expiries = _upcoming_fridays(today, max_days=45)

        # 20 strikes: spot * (0.90, 0.91, ..., 0.99, 1.00, 1.01, ..., 1.09, 1.10)
        # That's 21 values; spec says ±10% in 1% increments = 20 strikes
        # Interpret as -9% to +10% (20 steps) or -10% to +9%; use -10% to +9% exclusive of
        # one endpoint to land on exactly 20. Simpler: use range(-10, 10) for 20 strikes.
        pct_offsets = [i / 100.0 for i in range(-10, 10)]  # -0.10 to +0.09, 20 values
        strikes = [round(spot_price * (1.0 + p), 2) for p in pct_offsets]

        contracts: list[dict] = []

        for expiry in expiries:
            T = (expiry - today).days / 365.0
            if T <= 0:
                continue

            for strike in strikes:
                if strike <= 0:
                    continue

                try:
                    result = calculate(
                        S=spot_price, K=strike, T=T, r=0.05, sigma=sigma
                    )
                except Exception as exc:
                    logger.debug("BS calc failed S=%s K=%s T=%s: %s", spot_price, strike, T, exc)
                    continue

                for option_type, raw_price in (
                    ("call", result.call_price),
                    ("put", result.put_price),
                ):
                    if raw_price < 0.01:
                        continue

                    bid = round(raw_price * 0.98, 4)
                    ask = round(raw_price * 1.02, 4)
                    mid = round(raw_price, 4)

                    contracts.append(
                        {
                            "symbol": symbol,
                            "strike": strike,
                            "expiry": expiry,
                            "option_type": option_type,
                            "underlying_price": spot_price,
                            "timestamp": timestamp,
                            "T": T,
                            "sigma": sigma,
                            "price": mid,
                            "bid": bid,
                            "ask": ask,
                            "delta": result.delta if option_type == "call" else result.delta - 1.0,
                            "gamma": result.gamma,
                            "vega": result.vega,
                            "theta": result.theta,
                            "rho": result.rho,
                            "iv": result.iv,
                            "volume": random.randint(100, 5000),
                            "open_interest": random.randint(500, 50000),
                        }
                    )

        logger.debug(
            "Reconstructed options chain for %s @ %.2f: %d contracts",
            symbol,
            spot_price,
            len(contracts),
        )
        return contracts
