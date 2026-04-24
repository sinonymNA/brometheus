"""Kelly criterion position sizing engine for APEX CRUSHER.

Sizes each trade using a half-Kelly fraction derived from historical win rate
and average win/loss, then applies hard caps so no single trade can breach the
per-trade risk limit.

Usage::

    from backend.core.position_sizer import PositionSizer

    sizer = PositionSizer()
    result = await sizer.calculate(
        account_equity=51_000.0,
        option_price=2.50,
    )
    print(f"Contracts: {result.contracts}  Risk: ${result.risk_amount:.2f}")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RISK_PER_TRADE_PCT: float = 0.02  # never risk more than 2 % of equity
MAX_CONTRACTS: int = 10               # hard cap regardless of Kelly output

# Defaults used when fewer than 10 closed trades exist in the database.
_DEFAULT_WIN_RATE: float = 0.55
_DEFAULT_AVG_WIN: float = 400.0   # dollars
_DEFAULT_AVG_LOSS: float = 250.0  # dollars


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class SizeResult:
    """Output of a position-sizing calculation.

    Attributes:
        contracts: Recommended number of option contracts (≥ 0).
        risk_amount: Dollar cost of the recommended position.
        risk_pct: ``risk_amount / account_equity``.
        kelly_fraction: Half-Kelly fraction applied (after capping).
        win_rate: Win rate used for the Kelly calculation.
        avg_win: Average winning trade size (dollars) used for Kelly.
        avg_loss: Average losing trade size (dollars, positive) used for Kelly.
        n_trades: Number of closed trades in the history sample.
        reason: Short description of the sizing logic applied.
    """

    contracts: int
    risk_amount: float
    risk_pct: float
    kelly_fraction: float
    win_rate: float
    avg_win: float
    avg_loss: float
    n_trades: int
    reason: str


# ── Position sizer ────────────────────────────────────────────────────────────


class PositionSizer:
    """Kelly criterion-based position sizer with hard risk caps."""

    async def calculate(
        self,
        strength: float,
        option_price: float,
        account_equity: float,
        daily_pnl: float = 0.0,
        atr_ratio: float = 1.0,
    ) -> SizeResult:
        """Size a position in five steps using the half-Kelly criterion.

        **Step 1** — Retrieve trade statistics (win rate, avg win/loss).
        **Step 2** — Compute the raw Kelly fraction f* = (b·p − q) / b,
                     where b = avg_win/avg_loss, p = win_rate, q = 1 − p.
        **Step 3** — Apply half-Kelly × *strength*, cap at
                     :data:`MAX_RISK_PER_TRADE_PCT`.
        **Step 4** — Dollar risk = equity × capped Kelly fraction.
        **Step 5** — Contracts = floor(dollar_risk / (option_price × 100)),
                     capped at :data:`MAX_CONTRACTS`.

        Args:
            strength: Signal strength ∈ [0, 1]; scales the Kelly fraction.
            option_price: Per-share mid-price of the option contract.
            account_equity: Current account equity in dollars.
            daily_pnl: Today's realised + unrealised PnL (informational).

        Returns:
            :class:`SizeResult` with the recommended contract count and
            supporting details.  Returns 0 contracts when *option_price* is
            non-positive or Kelly fraction is zero/negative.
        """
        if account_equity <= 0:
            return SizeResult(
                contracts=0,
                risk_amount=0.0,
                risk_pct=0.0,
                kelly_fraction=0.0,
                win_rate=_DEFAULT_WIN_RATE,
                avg_win=_DEFAULT_AVG_WIN,
                avg_loss=_DEFAULT_AVG_LOSS,
                n_trades=0,
                reason="Invalid account equity",
            )

        if option_price <= 0:
            return SizeResult(
                contracts=0,
                risk_amount=0.0,
                risk_pct=0.0,
                kelly_fraction=0.0,
                win_rate=_DEFAULT_WIN_RATE,
                avg_win=_DEFAULT_AVG_WIN,
                avg_loss=_DEFAULT_AVG_LOSS,
                n_trades=0,
                reason="Invalid option price",
            )

        # Step 1: Historical trade stats.
        stats = await self.get_trade_stats()
        win_rate = stats["win_rate"]
        avg_win = stats["avg_win"]
        avg_loss = stats["avg_loss"]
        n_trades = stats["n_trades"]

        # Step 2: Kelly fraction  f* = (b·p − q) / b  where b = avg_win / avg_loss
        lose_rate = 1.0 - win_rate
        b = avg_win / avg_loss if avg_loss > 0 else 1.0
        kelly_raw = (b * win_rate - lose_rate) / b

        # Step 3: Half-Kelly × signal strength, floored at 0, capped at max risk.
        signal_strength = max(0.0, min(float(strength), 1.0))
        kelly_fraction = max(0.0, min(kelly_raw * 0.5 * signal_strength, MAX_RISK_PER_TRADE_PCT))

        # ATR volatility scaling: high ATR → smaller position.
        # atr_ratio = current_TR / avg_TR_14; ratio > 1 means above-average volatility.
        # Multiplier is clamped so we never go below half-size or above double.
        atr_mult = max(0.5, min(1.0 / max(float(atr_ratio), 0.25), 2.0))
        kelly_fraction = min(kelly_fraction * atr_mult, MAX_RISK_PER_TRADE_PCT)

        if kelly_fraction == 0.0:
            return SizeResult(
                contracts=0,
                risk_amount=0.0,
                risk_pct=0.0,
                kelly_fraction=kelly_fraction,
                win_rate=win_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                n_trades=n_trades,
                reason=f"Kelly fraction is non-positive (f*={kelly_raw:.4f})",
            )

        # Step 4: Dollar risk budget.
        risk_budget = account_equity * kelly_fraction

        # Step 5: Convert to contracts (1 contract = 100 shares × option_price).
        cost_per_contract = option_price * 100.0
        contracts = int(risk_budget / cost_per_contract)
        contracts = min(max(contracts, 0), MAX_CONTRACTS)

        actual_cost = contracts * cost_per_contract
        actual_pct = actual_cost / account_equity

        return SizeResult(
            contracts=contracts,
            risk_amount=actual_cost,
            risk_pct=actual_pct,
            kelly_fraction=kelly_fraction,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            n_trades=n_trades,
            reason=(
                f"half-Kelly={kelly_fraction:.4f}  "
                f"(raw f*={kelly_raw:.4f})  "
                f"atr_mult={atr_mult:.2f}  "
                f"budget=${risk_budget:.2f}"
            ),
        )

    async def get_trade_stats(self, n: int = 50) -> dict[str, Any]:
        """Return win rate, average win, and average loss from recent closed trades.

        Queries the last *n* closed trades from the database.  Falls back to
        conservative defaults when fewer than 10 trades exist (not enough
        history to estimate the distribution reliably).

        Args:
            n: Maximum number of recent closed trades to include. Default 50.

        Returns:
            Dict with keys ``win_rate``, ``avg_win``, ``avg_loss``,
            and ``n_trades``.
        """
        rows: list[Any] = []
        try:
            from backend.data.storage import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT pnl
                    FROM   trades
                    WHERE  status = 'closed'
                      AND  pnl IS NOT NULL
                    ORDER BY closed_at DESC
                    LIMIT  $1
                    """,
                    n,
                )
        except Exception as exc:
            logger.warning("Failed to fetch trade history for Kelly sizing: %s", exc)

        if len(rows) < 10:
            logger.debug(
                "Fewer than 10 closed trades (%d found) — using default Kelly params.",
                len(rows),
            )
            return {
                "win_rate": _DEFAULT_WIN_RATE,
                "avg_win": _DEFAULT_AVG_WIN,
                "avg_loss": _DEFAULT_AVG_LOSS,
                "n_trades": len(rows),
            }

        pnls = [float(r["pnl"]) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls)
        avg_win = sum(wins) / len(wins) if wins else _DEFAULT_AVG_WIN
        avg_loss = abs(sum(losses) / len(losses)) if losses else _DEFAULT_AVG_LOSS

        logger.debug(
            "Trade stats: n=%d  win_rate=%.3f  avg_win=%.2f  avg_loss=%.2f",
            len(pnls), win_rate, avg_win, avg_loss,
        )
        return {
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "n_trades": len(pnls),
        }
