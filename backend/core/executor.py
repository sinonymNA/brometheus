"""Trade execution engine for APEX CRUSHER.

Handles order placement, position monitoring, and Discord notifications.
Designed to be instantiated once and shared across the application.

Usage::

    executor = TradeExecutor(alpaca_client, storage, risk_manager, position_sizer)
    result = await executor.execute(signal)
    await executor.monitor_open_trades()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderSide, TimeInForce

from backend.data.alpaca_client import AlpacaClient
from backend.utils.logger import get_logger

logger = get_logger(__name__)

_ET = ZoneInfo("America/New_York")
_EOD_CLOSE_TIME = time(15, 45)   # start closing at 3:45 PM ET

# Per-trade close thresholds for directional strategies
_PROFIT_TARGET_PCT  =  0.30   # close long at +30 %
_STOP_LOSS_PCT      = -0.15   # close long at −15 %

# iv_rank condor thresholds
_CONDOR_PROFIT_PCT  =  0.50   # close condor when 50 % of premium collected
_CONDOR_MAX_LOSS    =  2.0    # close condor when loss = 2× premium (MAX_LOSS_MULTIPLIER)


# ── Result model ──────────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    """Result of a single trade execution attempt."""

    approved: bool
    reason: str
    trade_id: int | None = None
    order_id: str | None = None
    symbol: str = ""
    pnl: float | None = None


# ── Executor ──────────────────────────────────────────────────────────────────


class TradeExecutor:
    """Stateful executor — instantiate once, reuse for all trades."""

    def __init__(
        self,
        alpaca_client: AlpacaClient,
        storage: Any,
        risk_manager: Any,
        position_sizer: Any,
    ) -> None:
        self._alpaca = alpaca_client
        self._storage = storage
        self._risk_manager = risk_manager
        self._sizer = position_sizer

    # ── Execute ───────────────────────────────────────────────────────────────

    async def execute(self, signal: dict[str, Any]) -> ExecutionResult:
        """Execute a single trade signal end-to-end.

        Steps: risk check → sizing → order → DB save → Discord alert.

        Args:
            signal: Signal dict from a strategy's ``scan()`` method.

        Returns:
            :class:`ExecutionResult` with approval status and trade details.
        """
        symbol = signal.get("symbol", "")
        is_condor = "legs" in signal

        # Determine per-share option price
        if is_condor:
            option_price = float(signal.get("net_premium", 0.0))
        else:
            bid = float(signal.get("bid", 0) or 0)
            ask = float(signal.get("ask", 0) or 0)
            option_price = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else max(bid, ask)

        if option_price <= 0:
            return ExecutionResult(approved=False, reason="No valid option price in signal", symbol=symbol)

        # Account equity
        try:
            account = await self._alpaca.get_account()
            equity = float(account.equity)
        except Exception as exc:
            logger.error("TradeExecutor: account fetch failed: %s", exc)
            return ExecutionResult(approved=False, reason=f"Account unavailable: {exc}", symbol=symbol)

        # DTE for risk check
        expiry = self._signal_expiry(signal)
        dte = (expiry - date.today()).days if isinstance(expiry, date) else 10

        # 1. Risk check
        risk = await self._risk_manager.check(
            symbol=symbol,
            option_price=option_price,
            quantity=1,
            dte=dte,
            account_equity=equity,
        )
        if not risk.allowed:
            return ExecutionResult(approved=False, reason=risk.reason, symbol=symbol)

        # 2. Position size
        size = await self._sizer.calculate(
            strength=float(signal.get("strength", 0.5)),
            option_price=option_price,
            account_equity=equity,
            daily_pnl=risk.daily_pnl,
        )
        if size.contracts == 0:
            return ExecutionResult(
                approved=False,
                reason=f"Kelly sizer returned 0 contracts ({size.reason})",
                symbol=symbol,
            )

        # 3. Place order(s) and save trade
        if is_condor:
            return await self._execute_condor(signal, size.contracts, equity, risk.daily_pnl)
        else:
            return await self._execute_single(signal, size.contracts, option_price)

    # ── Close trade ───────────────────────────────────────────────────────────

    async def close_trade(self, trade_id: int, reason: str) -> ExecutionResult:
        """Close an open trade by placing a closing limit order.

        Args:
            trade_id: DB primary key of the trade to close.
            reason: Human-readable close reason (e.g. ``"profit target"``).

        Returns:
            :class:`ExecutionResult` with PnL and order details.
        """
        from backend.data.storage import get_trade_by_id, update_trade_closed

        trade = await get_trade_by_id(trade_id)
        if trade is None:
            return ExecutionResult(approved=False, reason=f"Trade {trade_id} not found", symbol="")

        symbol = trade["symbol"]
        action = trade.get("action", "buy")

        # Current option price from DB cache
        current_price = await self._option_mid(
            symbol,
            float(trade["strike"] or 0),
            trade["expiry"],
            trade.get("option_type") or "call",
        )
        if current_price is None or current_price <= 0:
            logger.warning("close_trade %d: no current price, using entry", trade_id)
            current_price = float(trade["entry_price"])

        exit_price = round(current_price, 2)
        entry = float(trade["entry_price"])
        qty = int(trade["quantity"])

        # Closing order (reverse of opening)
        close_side = OrderSide.SELL if action == "buy" else OrderSide.BUY
        try:
            if trade.get("strike") and trade.get("expiry"):
                occ = _occ(symbol, trade["expiry"], trade.get("option_type") or "call",
                           float(trade["strike"]))
                order = await self._alpaca.place_limit_order(
                    symbol=occ,
                    qty=float(qty),
                    side=close_side,
                    limit_price=exit_price,
                    time_in_force=TimeInForce.DAY,
                )
                order_id = str(order.id)
            else:
                order_id = "condor-manual"
        except Exception as exc:
            logger.error("close_trade %d: order failed: %s", trade_id, exc)
            order_id = f"order-failed:{exc}"

        # PnL calculation
        if action == "buy":
            pnl = (exit_price - entry) * qty * 100
        else:
            pnl = (entry - exit_price) * qty * 100

        await update_trade_closed(trade_id, exit_price, pnl, close_reason=reason)

        await self.discord_alert("CLOSED", {
            "symbol": symbol,
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round(pnl / (entry * qty * 100) * 100, 1) if entry > 0 else 0,
            "reason": reason,
            "strategy": trade.get("strategy", ""),
        })

        logger.info(
            "Trade closed: id=%d  %s  pnl=%+.2f  reason=%s",
            trade_id, symbol, pnl, reason,
        )
        return ExecutionResult(
            approved=True,
            reason=reason,
            trade_id=trade_id,
            order_id=order_id,
            symbol=symbol,
            pnl=round(pnl, 2),
        )

    # ── Monitor ───────────────────────────────────────────────────────────────

    async def monitor_open_trades(self) -> None:
        """Check all open trades and close those that hit an exit condition."""
        from backend.data.storage import get_open_trades

        try:
            trades = await get_open_trades()
        except Exception as exc:
            logger.error("monitor_open_trades: fetch failed: %s", exc)
            return

        eod = self._eod_approaching()

        for trade in trades:
            try:
                await self._check_and_close(trade, eod)
            except Exception as exc:
                logger.error("monitor_open_trades: trade %s failed: %s", trade.get("id"), exc)

    # ── Discord ───────────────────────────────────────────────────────────────

    async def discord_alert(self, event_type: str, details: dict[str, Any]) -> None:
        """Post a Discord embed notification.  Fails silently if unconfigured.

        Args:
            event_type: ``"OPENED"``, ``"CLOSED"``, or ``"RISK"``.
            details: Payload dict; keys vary by *event_type*.
        """
        try:
            from backend.utils.config import settings
            webhook = settings.discord_webhook_url
        except Exception:
            webhook = None

        if not webhook:
            return

        # Build embed
        color, title = {
            "OPENED": (0x2ECC71, f"📈 OPENED  {details.get('symbol', '')}"),
            "CLOSED": (
                (0x2ECC71 if (details.get("pnl_dollars", 0) or 0) >= 0 else 0xE74C3C),
                f"{'✅' if (details.get('pnl_dollars', 0) or 0) >= 0 else '❌'} CLOSED  {details.get('symbol', '')}",
            ),
            "RISK":   (0xF39C12, "⚠️  RISK ALERT"),
        }.get(event_type, (0x95A5A6, event_type))

        fields = [
            {"name": str(k).replace("_", " ").title(), "value": str(v), "inline": True}
            for k, v in details.items()
        ]

        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }]
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status not in (200, 204):
                        logger.debug("Discord webhook returned %d", resp.status)
        except Exception as exc:
            logger.debug("Discord alert failed (non-fatal): %s", exc)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _execute_single(
        self,
        signal: dict[str, Any],
        contracts: int,
        option_price: float,
    ) -> ExecutionResult:
        """Place one limit order and save one trade record."""
        from backend.data.storage import save_trade

        symbol = signal["symbol"]
        expiry = signal["expiry"]
        direction = signal.get("direction", "call")
        strike = float(signal.get("strike", 0))
        bid = float(signal.get("bid", 0) or 0)
        ask = float(signal.get("ask", 0) or 0)
        limit_price = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else option_price

        occ = _occ(symbol, expiry, direction, strike)
        order_id: str | None = None
        try:
            order = await self._alpaca.place_limit_order(
                symbol=occ,
                qty=float(contracts),
                side=OrderSide.BUY,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
            )
            order_id = str(order.id)
        except Exception as exc:
            logger.error("Order failed for %s: %s", occ, exc)
            return ExecutionResult(approved=False, reason=f"Order failed: {exc}", symbol=symbol)

        trade_id = await save_trade({
            "symbol": symbol,
            "strike": strike,
            "expiry": expiry,
            "option_type": direction,
            "action": "buy",
            "quantity": contracts,
            "entry_price": limit_price,
            "strategy": signal.get("strategy_name", ""),
        })

        await self.discord_alert("OPENED", {
            "symbol": symbol,
            "strike": strike,
            "expiry": str(expiry),
            "direction": direction,
            "qty": contracts,
            "price": limit_price,
            "strategy": signal.get("strategy_name", ""),
        })

        return ExecutionResult(
            approved=True,
            reason="Order placed",
            trade_id=trade_id,
            order_id=order_id,
            symbol=symbol,
        )

    async def _execute_condor(
        self,
        signal: dict[str, Any],
        contracts: int,
        equity: float,
        daily_pnl: float,
    ) -> ExecutionResult:
        """Place four limit orders (iron condor) and save one umbrella trade."""
        from backend.data.storage import save_trade

        symbol = signal["symbol"]
        legs: list[dict[str, Any]] = signal["legs"]
        net_premium = float(signal.get("net_premium", 0))

        # Place all four leg orders
        order_ids: list[str] = []
        for leg in legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            limit_px = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else max(bid, ask)
            side = OrderSide.SELL if leg["action"] == "sell" else OrderSide.BUY
            occ = _occ(symbol, leg["expiry"], leg["option_type"], float(leg["strike"]))
            try:
                order = await self._alpaca.place_limit_order(
                    symbol=occ,
                    qty=float(contracts),
                    side=side,
                    limit_price=max(limit_px, 0.01),
                    time_in_force=TimeInForce.DAY,
                )
                order_ids.append(str(order.id))
            except Exception as exc:
                logger.error("Condor leg order failed %s %s: %s", leg["action"], occ, exc)
                order_ids.append(f"failed:{exc}")

        # Save umbrella trade (short call leg as representative)
        short_call = next((l for l in legs if l["action"] == "sell" and l["option_type"] == "call"), legs[0])
        trade_id = await save_trade({
            "symbol": symbol,
            "strike": float(short_call["strike"]),
            "expiry": short_call["expiry"],
            "option_type": "call",
            "action": "sell",
            "quantity": contracts,
            "entry_price": net_premium,
            "strategy": "iv_rank",
        })

        await self.discord_alert("OPENED", {
            "symbol": symbol,
            "strategy": "iv_rank (iron condor)",
            "net_premium": net_premium,
            "qty": contracts,
            "iv_rank": signal.get("iv_rank", ""),
        })

        return ExecutionResult(
            approved=True,
            reason="Iron condor orders placed",
            trade_id=trade_id,
            order_id=",".join(order_ids[:2]),
            symbol=symbol,
        )

    async def _check_and_close(self, trade: dict[str, Any], eod: bool) -> None:
        """Evaluate close conditions for a single open trade."""
        trade_id = trade["id"]
        strategy = trade.get("strategy", "")
        action = trade.get("action", "buy")
        entry = float(trade.get("entry_price", 0) or 0)
        qty = int(trade.get("quantity", 1) or 1)

        if entry <= 0:
            return

        # Expiry check
        if trade.get("expiry"):
            dte = (trade["expiry"] - date.today()).days
            if dte <= 1:
                await self.close_trade(trade_id, "expiry")
                return

        # EOD check
        if eod:
            await self.close_trade(trade_id, "EOD")
            return

        # Current option price
        if not (trade.get("strike") and trade.get("expiry")):
            return

        current = await self._option_mid(
            trade["symbol"],
            float(trade["strike"]),
            trade["expiry"],
            trade.get("option_type") or "call",
        )
        if current is None:
            return

        cost_basis = entry * qty * 100

        if strategy == "iv_rank" and action == "sell":
            # Condor: profit = premium decay; entry_price is net credit received
            unrealized_pnl = (entry - current) * qty * 100
            profit_pct = unrealized_pnl / cost_basis if cost_basis > 0 else 0.0

            if profit_pct >= _CONDOR_PROFIT_PCT:
                await self.close_trade(trade_id, "profit target")
            elif unrealized_pnl <= -(entry * _CONDOR_MAX_LOSS * qty * 100):
                await self.close_trade(trade_id, "stop loss")
        else:
            # Directional long: entry was a buy
            unrealized_pnl = (current - entry) * qty * 100
            profit_pct = unrealized_pnl / cost_basis if cost_basis > 0 else 0.0

            if profit_pct >= _PROFIT_TARGET_PCT:
                await self.close_trade(trade_id, "profit target")
            elif profit_pct <= _STOP_LOSS_PCT:
                await self.close_trade(trade_id, "stop loss")

    async def _option_mid(
        self,
        symbol: str,
        strike: float,
        expiry: date | None,
        option_type: str,
    ) -> float | None:
        """Return the latest mid-price for a specific option contract from options_data."""
        if expiry is None or strike <= 0:
            return None
        from backend.data.storage import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT bid, ask
                FROM   options_data
                WHERE  symbol      = $1
                  AND  strike      = $2
                  AND  expiry      = $3
                  AND  option_type = $4
                ORDER  BY timestamp DESC
                LIMIT  1
                """,
                symbol, strike, expiry, option_type,
            )
        if row is None:
            return None
        bid = float(row["bid"] or 0)
        ask = float(row["ask"] or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        val = bid or ask
        return round(val, 4) if val > 0 else None

    @staticmethod
    def _signal_expiry(signal: dict[str, Any]) -> date | None:
        if "legs" in signal:
            legs = signal["legs"]
            return legs[0].get("expiry") if legs else None
        return signal.get("expiry")

    @staticmethod
    def _eod_approaching() -> bool:
        """Return True at or after 3:45 PM ET on a weekday."""
        now_et = datetime.now(_ET)
        return now_et.weekday() < 5 and now_et.time() >= _EOD_CLOSE_TIME


def _occ(underlying: str, expiry: date, option_type: str, strike: float) -> str:
    """Build an OCC option symbol string.

    Format: ``{UNDERLYING}{YYMMDD}{C|P}{STRIKE×1000:08d}``
    e.g. ``SPY240620C00500000`` for SPY $500 call expiring 2024-06-20.
    """
    cp = "C" if option_type == "call" else "P"
    return f"{underlying}{expiry.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"
