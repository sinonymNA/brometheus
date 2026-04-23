"""Strategy orchestration loop for APEX CRUSHER.

Runs all three trading strategies every 60 seconds during market hours,
deduplicates and prioritises signals, executes approved trades, and monitors
open positions.

Usage (from FastAPI startup)::

    runner = StrategyRunner(alpaca_client, storage)
    task = await runner.start()

    # On shutdown:
    await runner.stop()
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from backend.core.executor import TradeExecutor
from backend.core.position_sizer import PositionSizer
from backend.core.risk_manager import RiskManager
from backend.data.alpaca_client import AlpacaClient
from backend.strategies.flow import FlowStrategy
from backend.strategies.iv_rank import IVRankStrategy
from backend.strategies.momentum import MomentumStrategy
from backend.utils.logger import get_logger

logger = get_logger(__name__)

_CYCLE_SECONDS = 60


class StrategyRunner:
    """Orchestrates all strategies and drives the trading loop."""

    def __init__(self, alpaca_client: AlpacaClient, storage: Any = None) -> None:
        risk_manager   = RiskManager()
        position_sizer = PositionSizer()

        self._executor = TradeExecutor(
            alpaca_client=alpaca_client,
            storage=storage,
            risk_manager=risk_manager,
            position_sizer=position_sizer,
        )
        self._momentum = MomentumStrategy(alpaca_client, storage)
        self._iv_rank  = IVRankStrategy(alpaca_client, storage)
        self._flow     = FlowStrategy(alpaca_client, storage)

        self._task: asyncio.Task[None] | None = None

    # ── Public lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> asyncio.Task[None]:
        """Launch the strategy loop as a named background task.

        No-op if the loop is already running.

        Returns:
            The running :class:`asyncio.Task`.
        """
        if self._task is not None and not self._task.done():
            logger.warning("StrategyRunner already running; ignoring duplicate start().")
            return self._task
        self._task = asyncio.create_task(self._loop(), name="apex-strategy-runner")
        logger.info("StrategyRunner started (cycle=%ds).", _CYCLE_SECONDS)
        return self._task

    async def stop(self) -> None:
        """Cancel the loop task and wait for it to finish."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("StrategyRunner stopped.")

    @property
    def is_running(self) -> bool:
        """``True`` when the background task is alive."""
        return self._task is not None and not self._task.done()

    # ── Cycle ─────────────────────────────────────────────────────────────────

    async def run_cycle(self) -> None:
        """Execute one full scan-execute-monitor cycle.

        Steps:

        1. Skip if market is closed.
        2. Run all three strategy scans concurrently.
        3. Flatten, sort by strength descending, deduplicate by symbol.
        4. Save each signal to DB, then execute it.
        5. Monitor all open trades for exit conditions.
        6. Log cycle summary.
        """
        if not self._executor._risk_manager.is_market_open():
            logger.debug("StrategyRunner: market closed — skipping cycle.")
            return

        # Step 2: concurrent scans
        scan_results = await asyncio.gather(
            self._momentum.scan(),
            self._iv_rank.scan(),
            self._flow.scan(),
            return_exceptions=True,
        )

        signals: list[dict[str, Any]] = []
        for i, result in enumerate(scan_results):
            if isinstance(result, Exception):
                names = ("momentum", "iv_rank", "flow")
                logger.error("Strategy %s scan error: %s", names[i], result)
            elif isinstance(result, list):
                signals.extend(result)

        # Step 3: sort + deduplicate
        signals.sort(key=lambda s: float(s.get("strength", 0)), reverse=True)
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for sig in signals:
            sym = sig.get("symbol", "")
            if sym and sym not in seen:
                unique.append(sig)
                seen.add(sym)

        logger.info("StrategyRunner: %d signals found (%d unique).", len(signals), len(unique))

        # Step 4: save + execute
        trades_opened = 0
        from backend.data.storage import save_signal
        for sig in unique:
            try:
                db_dir = "short" if sig.get("direction") == "short" else "long"
                await save_signal(
                    symbol=sig["symbol"],
                    signal_type=sig.get("signal_type", "unknown"),
                    direction=db_dir,
                    strength=float(sig.get("strength", 0)),
                    strategy=sig.get("strategy_name", "unknown"),
                    metadata=_safe_metadata(sig),
                )
            except Exception as exc:
                logger.warning("save_signal failed for %s: %s", sig.get("symbol"), exc)

            result = await self._executor.execute(sig)
            if result.approved:
                trades_opened += 1
                logger.info(
                    "Trade opened: %s  trade_id=%s  order_id=%s",
                    sig["symbol"], result.trade_id, result.order_id,
                )
            else:
                logger.info("Signal rejected: %s — %s", sig.get("symbol"), result.reason)

        # Step 5: monitor open positions
        await self._executor.monitor_open_trades()

        logger.info(
            "StrategyRunner cycle complete: %d signals, %d trades opened.",
            len(unique), trades_opened,
        )

    # ── Private ───────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Infinite loop: run_cycle every _CYCLE_SECONDS. Never crashes."""
        while True:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("StrategyRunner unhandled cycle error: %s", exc, exc_info=True)
            await asyncio.sleep(_CYCLE_SECONDS)


def _safe_metadata(sig: dict[str, Any]) -> dict[str, Any]:
    """Strip non-serialisable fields and return a JSON-safe subset."""
    skip = {"symbol", "direction", "strength", "strategy_name", "signal_type",
            "bid", "ask", "delta", "iv", "strike", "expiry", "option_type", "legs"}
    out: dict[str, Any] = {}
    for k, v in sig.items():
        if k in skip:
            continue
        if isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, float):
            out[k] = round(v, 4)
        elif isinstance(v, (int, str, bool)):
            out[k] = v
    return out
