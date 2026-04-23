"""Pre-trade risk management engine for APEX CRUSHER.

Runs seven sequential checks before any order is placed.  All checks are
fast (Redis + in-process) so they add negligible latency to the order path.

Usage::

    from backend.core.risk_manager import RiskManager

    rm = RiskManager()
    result = await rm.check(
        symbol="SPY",
        option_price=2.50,
        quantity=2,
        dte=5,
        account_equity=51_000.0,
    )
    if not result.allowed:
        print(f"Trade blocked: {result.reason}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_DAILY_LOSS_PCT: float = 0.03      # halt if daily loss exceeds 3 % of starting balance
MAX_DRAWDOWN_PCT: float = 0.06        # halt if equity drawdown from start exceeds 6 %
MAX_CONCURRENT_POSITIONS: int = 5     # maximum simultaneously open trades
MAX_RISK_PCT: float = 0.02            # maximum cost of one trade as % of equity
MIN_DTE: int = 2                      # minimum days to expiry (avoids pin risk)
STARTING_BALANCE: float = 50_000.0   # reference balance for drawdown calculation

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 35)            # 9:35 AM ET — 5 min after open
_MARKET_CLOSE = time(15, 45)          # 3:45 PM ET — 15 min before close

_EMERGENCY_STOP_KEY = "emergency_stop"


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class RiskCheckResult:
    """Result of a full pre-trade risk evaluation.

    Attributes:
        allowed: ``True`` only when all seven checks pass.
        reason: Human-readable summary of the first failed check, or
            ``"All checks passed"`` when *allowed* is ``True``.
        daily_pnl: Estimated daily PnL (realised + unrealised) at check time.
        open_positions: Number of currently open trades.
        checks_passed: Names of checks that completed successfully.
        checks_failed: Names of the check that triggered the block (0 or 1 item).
    """

    allowed: bool
    reason: str
    daily_pnl: float = 0.0
    open_positions: int = 0
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)


# ── Risk manager ──────────────────────────────────────────────────────────────


class RiskManager:
    """Stateless risk gate — instantiate once and reuse across requests."""

    async def check(
        self,
        symbol: str,
        option_price: float,
        quantity: int,
        dte: int,
        account_equity: float,
    ) -> RiskCheckResult:
        """Run all seven pre-trade risk checks in order.

        Checks are evaluated sequentially and short-circuit on the first
        failure.  All checks that ran before the failure are recorded in
        ``checks_passed``; the failing check is recorded in ``checks_failed``.

        Args:
            symbol: Underlying ticker being traded (e.g. ``"SPY"``).
            option_price: Per-share mid-price of the option contract.
            quantity: Number of contracts (each represents 100 shares).
            dte: Days to expiry of the option being traded.
            account_equity: Current account equity in dollars.

        Returns:
            :class:`RiskCheckResult` with ``allowed=True`` iff every check passes.
        """
        passed: list[str] = []
        daily_pnl: float = 0.0
        open_pos: int = 0

        # 1. Emergency stop ────────────────────────────────────────────────────
        if await self._is_emergency_stopped():
            return RiskCheckResult(
                allowed=False,
                reason="Emergency stop is active",
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["emergency_stop"],
            )
        passed.append("emergency_stop_clear")

        # 2. Market hours ─────────────────────────────────────────────────────
        if not self.is_market_open():
            return RiskCheckResult(
                allowed=False,
                reason="Outside trading hours (09:35–15:45 ET)",
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["market_hours"],
            )
        passed.append("market_hours")

        # 3. Daily loss limit ─────────────────────────────────────────────────
        daily_pnl = await self.get_daily_pnl(account_equity)
        daily_loss_pct = -daily_pnl / STARTING_BALANCE if daily_pnl < 0 else 0.0
        if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Daily loss limit reached "
                    f"({daily_loss_pct:.1%} ≥ {MAX_DAILY_LOSS_PCT:.1%})"
                ),
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["daily_loss_limit"],
            )
        passed.append("daily_loss_limit")

        # 4. Max drawdown ─────────────────────────────────────────────────────
        drawdown_pct = max(0.0, (STARTING_BALANCE - account_equity) / STARTING_BALANCE)
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Max drawdown reached "
                    f"({drawdown_pct:.1%} ≥ {MAX_DRAWDOWN_PCT:.1%})"
                ),
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["max_drawdown"],
            )
        passed.append("max_drawdown")

        # 5. Concurrent positions ─────────────────────────────────────────────
        open_pos = await self._count_open_trades()
        if open_pos >= MAX_CONCURRENT_POSITIONS:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Max concurrent positions reached "
                    f"({open_pos} ≥ {MAX_CONCURRENT_POSITIONS})"
                ),
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["concurrent_positions"],
            )
        passed.append("concurrent_positions")

        # 6. Max risk per trade ────────────────────────────────────────────────
        trade_cost = option_price * quantity * 100  # 100 shares per contract
        risk_pct = trade_cost / account_equity if account_equity > 0 else 1.0
        if risk_pct > MAX_RISK_PCT:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Trade exceeds max risk "
                    f"({risk_pct:.1%} > {MAX_RISK_PCT:.1%})"
                ),
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["max_risk_per_trade"],
            )
        passed.append("max_risk_per_trade")

        # 7. Minimum DTE ──────────────────────────────────────────────────────
        if dte < MIN_DTE:
            return RiskCheckResult(
                allowed=False,
                reason=f"DTE too low ({dte} < {MIN_DTE})",
                daily_pnl=daily_pnl,
                open_positions=open_pos,
                checks_passed=passed,
                checks_failed=["min_dte"],
            )
        passed.append("min_dte")

        return RiskCheckResult(
            allowed=True,
            reason="All checks passed",
            daily_pnl=daily_pnl,
            open_positions=open_pos,
            checks_passed=passed,
            checks_failed=[],
        )

    async def get_daily_pnl(self, account_equity: float) -> float:
        """Estimate today's PnL as realised (DB) + unrealised (Redis prices).

        Args:
            account_equity: Current account equity (used only for logging).

        Returns:
            Combined PnL in dollars.  Returns 0.0 if data sources are
            unavailable rather than blocking the risk check.
        """
        realized: float = 0.0
        unrealized: float = 0.0

        # Realised PnL — trades closed today
        try:
            from backend.data.storage import get_pool
            pool = await get_pool()
            today = date.today()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(pnl), 0) AS total
                    FROM   trades
                    WHERE  status = 'closed'
                      AND  closed_at::date = $1
                    """,
                    today,
                )
            realized = float(row["total"] or 0.0)
        except Exception as exc:
            logger.warning("Failed to fetch realised daily PnL: %s", exc)

        # Unrealised PnL — open trades valued at cached spot prices
        try:
            from backend.data.storage import get_open_trades
            from backend.data.fetcher import get_cached_price
            open_trades = await get_open_trades()
            for trade in open_trades:
                cached = await get_cached_price(trade["symbol"])
                entry = trade.get("entry_price")
                qty = trade.get("quantity", 0)
                if cached is None or entry is None or qty == 0:
                    continue
                direction = 1 if trade.get("action", "buy").lower() == "buy" else -1
                unrealized += direction * (cached - float(entry)) * qty * 100
        except Exception as exc:
            logger.warning("Failed to compute unrealised PnL: %s", exc)

        total = realized + unrealized
        logger.debug(
            "Daily PnL: realised=%.2f  unrealised=%.2f  total=%.2f",
            realized, unrealized, total,
        )
        return total

    async def emergency_stop(self) -> None:
        """Set the Redis emergency-stop flag, halting all new trades immediately."""
        try:
            from backend.data.fetcher import _get_redis
            redis = await _get_redis()
            await redis.set(_EMERGENCY_STOP_KEY, "1")
            logger.warning("EMERGENCY STOP activated — all trading halted.")
        except Exception as exc:
            logger.error("Failed to set emergency stop in Redis: %s", exc)

    async def clear_emergency_stop(self) -> None:
        """Remove the Redis emergency-stop flag, re-enabling trading."""
        try:
            from backend.data.fetcher import _get_redis
            redis = await _get_redis()
            await redis.delete(_EMERGENCY_STOP_KEY)
            logger.info("Emergency stop cleared — trading re-enabled.")
        except Exception as exc:
            logger.error("Failed to clear emergency stop in Redis: %s", exc)

    def is_market_open(self) -> bool:
        """Return ``True`` if within active trading hours (09:35–15:45 ET, Mon–Fri).

        Uses slightly narrowed hours vs. the exchange open (09:30) and close
        (16:00) to avoid the first-minute liquidity crunch and last-minute
        settlement risk.
        """
        now_et = datetime.now(_ET)
        if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        t = now_et.time()
        return _MARKET_OPEN <= t <= _MARKET_CLOSE

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _is_emergency_stopped(self) -> bool:
        """Return ``True`` if the emergency-stop key exists in Redis."""
        try:
            from backend.data.fetcher import _get_redis
            redis = await _get_redis()
            return await redis.get(_EMERGENCY_STOP_KEY) is not None
        except Exception:
            # If Redis is unreachable, don't block trading on an unavailable flag.
            return False

    async def _count_open_trades(self) -> int:
        """Return the number of currently open trades from the database."""
        try:
            from backend.data.storage import get_open_trades
            return len(await get_open_trades())
        except Exception as exc:
            logger.warning("Failed to count open trades: %s", exc)
            return 0
