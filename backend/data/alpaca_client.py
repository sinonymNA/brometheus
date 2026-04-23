"""Alpaca API client wrapper for APEX CRUSHER.

Wraps the alpaca-py SDK with async support (via asyncio.to_thread),
exponential-backoff retry on rate limits, and structured logging.

Usage::

    from backend.data.alpaca_client import alpaca

    alpaca.connect()
    quotes = await alpaca.get_quotes(["SPY", "QQQ"])
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.models import Bar, OptionsSnapshot, Quote
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order, Position, TradeAccount
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from backend.utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 2  # wait = base ** attempt  →  2s, 4s, 8s

_TIMEFRAMES: dict[str, TimeFrame] = {
    "1min": TimeFrame.Minute,
    "5min": TimeFrame(5, TimeFrameUnit.Minute),
    "1day": TimeFrame.Day,
}


# ── Client ────────────────────────────────────────────────────────────────────


class AlpacaClient:
    """Async wrapper around the alpaca-py SDK for paper trading.

    Call :meth:`connect` once before using any other method (e.g. in the
    FastAPI startup handler).  All data / trading methods are coroutines and
    safe to use concurrently.
    """

    def __init__(self) -> None:
        self._trading: TradingClient | None = None
        self._stock_data: StockHistoricalDataClient | None = None
        self._option_data: OptionHistoricalDataClient | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Initialise all Alpaca SDK clients from config.

        Determines paper vs. live mode by checking whether
        ``ALPACA_BASE_URL`` contains the string ``"paper"``.

        Raises:
            RuntimeError: If called more than once without an intervening
                :meth:`disconnect`.
        """
        if self._trading is not None:
            logger.warning("AlpacaClient.connect() called while already connected; ignoring.")
            return

        from backend.utils.config import settings
        paper: bool = "paper" in settings.alpaca_base_url.lower()

        self._trading = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=paper,
        )
        self._stock_data = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
        self._option_data = OptionHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
        logger.info(
            "Alpaca clients initialised (paper=%s, base_url=%s)",
            paper,
            settings.alpaca_base_url,
        )

    def disconnect(self) -> None:
        """Release client references (no persistent connections to close)."""
        self._trading = None
        self._stock_data = None
        self._option_data = None
        logger.info("Alpaca clients disconnected.")

    async def health_check(self) -> bool:
        """Verify the Alpaca API is reachable by fetching account info.

        Returns:
            ``True`` if the trading account endpoint responds successfully.
            ``False`` if the client is not connected or the request fails for
            any reason (network error, bad credentials, etc.).
        """
        if self._trading is None:
            return False
        try:
            await self._call(self._trading_client.get_account)
            return True
        except Exception as exc:
            logger.warning("Alpaca health check failed: %s", exc)
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    @property
    def _trading_client(self) -> TradingClient:
        if self._trading is None:
            raise RuntimeError("AlpacaClient is not connected. Call connect() first.")
        return self._trading

    @property
    def _stock_client(self) -> StockHistoricalDataClient:
        if self._stock_data is None:
            raise RuntimeError("AlpacaClient is not connected. Call connect() first.")
        return self._stock_data

    @property
    def _option_client(self) -> OptionHistoricalDataClient:
        if self._option_data is None:
            raise RuntimeError("AlpacaClient is not connected. Call connect() first.")
        return self._option_data

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous alpaca-py call in a thread with retry on rate limits.

        Uses ``asyncio.to_thread`` so the event loop is never blocked.
        Retries up to :data:`_MAX_RETRIES` times with exponential backoff
        (2 s, 4 s, 8 s) when the server responds with HTTP 429.  All other
        :class:`~alpaca.common.exceptions.APIError` variants are re-raised
        immediately after logging.

        Args:
            fn: Bound method of an alpaca-py client.
            *args: Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            Whatever *fn* returns.

        Raises:
            APIError: On non-429 API errors or after exhausting retries.
        """
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except APIError as exc:
                status: int | None = getattr(exc, "status_code", None)
                if status == 429 and attempt < _MAX_RETRIES:
                    wait = _RETRY_BASE_SECONDS ** (attempt + 1)
                    logger.warning(
                        "Rate limit hit (attempt %d/%d); retrying in %ds…",
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Alpaca API error [status=%s] in %s: %s",
                        status,
                        getattr(fn, "__name__", fn),
                        exc,
                    )
                    raise

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch the latest NBBO quote for one or more equity symbols.

        Args:
            symbols: One or more ticker symbols, e.g. ``["SPY", "QQQ"]``.

        Returns:
            Dict mapping each symbol to its latest :class:`~alpaca.data.models.Quote`.
            A symbol may be absent from the dict if no quote is available.
        """
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        result: dict[str, Quote] = await self._call(
            self._stock_client.get_stock_latest_quote, req
        )
        logger.debug("Fetched quotes for %s", symbols)
        return result

    async def get_option_chain(
        self,
        symbol: str,
        expiry_start: date | None = None,
        expiry_end: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> dict[str, OptionsSnapshot]:
        """Fetch the options chain snapshot for an underlying equity.

        All filter parameters are optional — omitting them returns the full
        chain (which can be large; always apply at least one filter in
        production).

        Args:
            symbol: Underlying ticker, e.g. ``"SPY"``.
            expiry_start: Earliest expiration date to include (inclusive).
            expiry_end: Latest expiration date to include (inclusive).
            strike_min: Minimum strike price filter.
            strike_max: Maximum strike price filter.

        Returns:
            Dict mapping each OCC option symbol to its
            :class:`~alpaca.data.models.OptionsSnapshot` (bid/ask, greeks, IV).

        Note:
            Options data requires an Alpaca Options subscription tier.
        """
        req = OptionChainRequest(
            underlying_symbol=symbol,
            expiration_date_gte=expiry_start,
            expiration_date_lte=expiry_end,
            strike_price_gte=strike_min,
            strike_price_lte=strike_max,
        )
        result: dict[str, OptionsSnapshot] = await self._call(
            self._option_client.get_option_chain, req
        )
        logger.debug(
            "Fetched option chain for %s: %d contracts",
            symbol,
            len(result),
        )
        return result

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "1day",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        """Download historical OHLCV bar data.

        Args:
            symbols: One or more ticker symbols.
            timeframe: Aggregation period — one of ``"1min"``, ``"5min"``,
                or ``"1day"``.  Defaults to ``"1day"``.
            start: Start of the date range (UTC). Defaults to 30 days ago.
            end: End of the date range (UTC). Defaults to now.

        Returns:
            Dict mapping each symbol to a list of :class:`~alpaca.data.models.Bar`
            objects ordered oldest-first.

        Raises:
            ValueError: If *timeframe* is not one of the recognised strings.
        """
        tf = _TIMEFRAMES.get(timeframe)
        if tf is None:
            raise ValueError(
                f"Unknown timeframe {timeframe!r}. "
                f"Valid options: {sorted(_TIMEFRAMES)}"
            )

        now = datetime.now(timezone.utc)
        if start is None:
            start = now - timedelta(days=30)
        if end is None:
            end = now

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end,
        )
        raw = await self._call(self._stock_client.get_stock_bars, req)
        # BarSet is dict-like: symbol -> list[Bar]
        result: dict[str, list[Bar]] = {sym: list(bars) for sym, bars in raw.items()}
        logger.debug("Fetched %s bars for %s", timeframe, symbols)
        return result

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        """Submit a market order.

        Args:
            symbol: Ticker symbol, e.g. ``"SPY"``.
            qty: Number of shares (fractional shares supported).
            side: :attr:`~alpaca.trading.enums.OrderSide.BUY` or
                :attr:`~alpaca.trading.enums.OrderSide.SELL`.
            time_in_force: Order duration policy. Defaults to ``DAY``.

        Returns:
            The submitted :class:`~alpaca.trading.models.Order`.
        """
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        order: Order = await self._call(self._trading_client.submit_order, req)
        logger.info(
            "Market order submitted: %s %s x%.4f → order_id=%s",
            side.value,
            symbol,
            qty,
            order.id,
        )
        return order

    async def place_limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        """Submit a limit order.

        Args:
            symbol: Ticker symbol.
            qty: Number of shares.
            side: :attr:`~alpaca.trading.enums.OrderSide.BUY` or
                :attr:`~alpaca.trading.enums.OrderSide.SELL`.
            limit_price: Maximum price (buy) or minimum price (sell) for
                execution.
            time_in_force: Order duration policy. Defaults to ``DAY``.

        Returns:
            The submitted :class:`~alpaca.trading.models.Order`.
        """
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )
        order: Order = await self._call(self._trading_client.submit_order, req)
        logger.info(
            "Limit order submitted: %s %s x%.4f @ %.4f → order_id=%s",
            side.value,
            symbol,
            qty,
            limit_price,
            order.id,
        )
        return order

    async def place_stop_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        stop_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        """Submit a stop order.

        Once the market touches *stop_price* the order converts to a market
        order and executes at the next available price.

        Args:
            symbol: Ticker symbol.
            qty: Number of shares.
            side: :attr:`~alpaca.trading.enums.OrderSide.BUY` or
                :attr:`~alpaca.trading.enums.OrderSide.SELL`.
            stop_price: Trigger price.
            time_in_force: Order duration policy. Defaults to ``DAY``.

        Returns:
            The submitted :class:`~alpaca.trading.models.Order`.
        """
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            stop_price=stop_price,
            time_in_force=time_in_force,
        )
        order: Order = await self._call(self._trading_client.submit_order, req)
        logger.info(
            "Stop order submitted: %s %s x%.4f stop=%.4f → order_id=%s",
            side.value,
            symbol,
            qty,
            stop_price,
            order.id,
        )
        return order

    # ── Account & positions ───────────────────────────────────────────────────

    async def get_account(self) -> TradeAccount:
        """Fetch current account details (cash, equity, buying power, status).

        Returns:
            :class:`~alpaca.trading.models.TradeAccount` for the configured
            account.
        """
        account: TradeAccount = await self._call(self._trading_client.get_account)
        logger.debug(
            "Account fetched: equity=%s cash=%s buying_power=%s",
            account.equity,
            account.cash,
            account.buying_power,
        )
        return account

    async def get_positions(self) -> list[Position]:
        """Fetch all currently open positions.

        Returns:
            List of :class:`~alpaca.trading.models.Position` objects.
            Returns an empty list when the account is flat.
        """
        positions: list[Position] = await self._call(
            self._trading_client.get_all_positions
        )
        logger.debug("Fetched %d open position(s)", len(positions))
        return positions


# ── Module-level singleton ────────────────────────────────────────────────────

alpaca = AlpacaClient()
"""Pre-built client instance.

Call ``alpaca.connect()`` once at application startup before issuing any
requests — typically inside the FastAPI ``@app.on_event("startup")`` handler.
"""


# ── Connection smoke test ─────────────────────────────────────────────────────


async def test_connection() -> None:
    """Smoke test: connect, fetch a SPY quote, print account balance.

    Run directly::

        python -m backend.data.alpaca_client
    """
    alpaca.connect()

    print("\n── SPY quote ──────────────────────────────────────────")
    quotes = await alpaca.get_quotes(["SPY"])
    spy = quotes.get("SPY")
    if spy:
        print(f"  Bid:       ${spy.bid_price}")
        print(f"  Ask:       ${spy.ask_price}")
        print(f"  Timestamp: {spy.timestamp}")
    else:
        print("  No quote returned for SPY.")

    print("\n── Account info ───────────────────────────────────────")
    account = await alpaca.get_account()
    print(f"  Cash:         ${account.cash}")
    print(f"  Equity:       ${account.equity}")
    print(f"  Buying power: ${account.buying_power}")
    print(f"  Status:       {account.status}")

    print("\n✓ Alpaca paper trading connection confirmed.\n")


if __name__ == "__main__":
    asyncio.run(test_connection())
