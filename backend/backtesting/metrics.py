"""Performance metrics calculator for APEX CRUSHER backtesting."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


class MetricsCalculator:
    """All methods are static — instantiation is optional."""

    @staticmethod
    def calculate_sharpe(
        daily_returns: list[float], risk_free_rate: float = 0.05
    ) -> float:
        """Annualised Sharpe ratio from daily PnL values (not percentages).

        daily_excess = daily_returns - daily_rf (rf/252 per day)
        sharpe = mean(daily_excess) / std(daily_excess) * sqrt(252)
        Returns 0.0 if fewer than 2 data points or std == 0.
        """
        if len(daily_returns) < 2:
            return 0.0

        daily_rf = risk_free_rate / 252.0
        excess = [r - daily_rf for r in daily_returns]

        n = len(excess)
        mean_excess = sum(excess) / n
        variance = sum((x - mean_excess) ** 2 for x in excess) / (n - 1)
        std_excess = math.sqrt(variance)

        if std_excess == 0.0:
            return 0.0

        return (mean_excess / std_excess) * math.sqrt(252.0)

    @staticmethod
    def calculate_max_drawdown(equity_curve: list[float]) -> tuple[float, int]:
        """Maximum drawdown pct and recovery days.

        Track running peak. When equity < peak, we're in drawdown.
        max_dd_pct = (peak - trough) / peak
        recovery_days = days from trough until equity returns to previous peak.
        Returns (max_drawdown_pct, recovery_days).
        Returns (0.0, 0) if equity_curve has fewer than 2 points.
        """
        if len(equity_curve) < 2:
            return (0.0, 0)

        peak = equity_curve[0]
        peak_idx = 0
        trough = equity_curve[0]
        trough_idx = 0

        max_dd_pct = 0.0
        best_peak = equity_curve[0]
        best_peak_idx = 0
        best_trough_idx = 0

        for i, value in enumerate(equity_curve):
            if value >= peak:
                peak = value
                peak_idx = i
                trough = value
                trough_idx = i
            elif value < trough:
                trough = value
                trough_idx = i
                dd_pct = (peak - trough) / peak if peak > 0 else 0.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
                    best_peak = peak
                    best_peak_idx = peak_idx
                    best_trough_idx = trough_idx

        # Count recovery days: from trough, find first index where equity >= best_peak
        recovery_days = 0
        if max_dd_pct > 0.0:
            for i in range(best_trough_idx + 1, len(equity_curve)):
                if equity_curve[i] >= best_peak:
                    recovery_days = i - best_trough_idx
                    break
            # If never recovered, recovery_days remains 0

        return (max_dd_pct, recovery_days)

    @staticmethod
    def calculate_profit_factor(wins: list[float], losses: list[float]) -> float:
        """sum(wins) / abs(sum(losses)). Returns 0.0 if no losses."""
        total_loss = sum(losses)
        if not losses or total_loss == 0.0:
            return 0.0
        return sum(wins) / abs(total_loss)

    @staticmethod
    def calculate_win_rate(pnls: list[float]) -> float:
        """Fraction of trades with pnl > 0."""
        if not pnls:
            return 0.0
        winners = sum(1 for p in pnls if p > 0)
        return winners / len(pnls)

    @staticmethod
    def calculate_consecutive_streaks(pnls: list[float]) -> tuple[int, int]:
        """Return (max_consecutive_wins, max_consecutive_losses)."""
        if not pnls:
            return (0, 0)

        max_wins = 0
        max_losses = 0
        cur_wins = 0
        cur_losses = 0

        for p in pnls:
            if p > 0:
                cur_wins += 1
                cur_losses = 0
                max_wins = max(max_wins, cur_wins)
            elif p < 0:
                cur_losses += 1
                cur_wins = 0
                max_losses = max(max_losses, cur_losses)
            else:
                # Breakeven resets both streaks
                cur_wins = 0
                cur_losses = 0

        return (max_wins, max_losses)

    @staticmethod
    def calculate_avg_hold_time(trades: list[Any]) -> float:
        """Average duration in minutes across closed trades.

        Each trade has .entry_time (datetime) and .exit_time (datetime | None).
        Skip trades without exit_time.
        """
        durations: list[float] = []
        for trade in trades:
            exit_time: datetime | None = getattr(trade, "exit_time", None)
            if exit_time is None:
                continue
            entry_time: datetime = trade.entry_time
            duration_minutes = (exit_time - entry_time).total_seconds() / 60.0
            durations.append(duration_minutes)

        if not durations:
            return 0.0
        return sum(durations) / len(durations)

    @staticmethod
    def build_monthly_pnl(closed_trades: list[Any]) -> dict[str, float]:
        """Return {YYYY-MM: total_pnl} for all months in the trade history."""
        monthly: dict[str, float] = {}
        for trade in closed_trades:
            exit_time: datetime | None = getattr(trade, "exit_time", None)
            if exit_time is None:
                continue
            key = exit_time.strftime("%Y-%m")
            pnl: float = getattr(trade, "pnl", 0.0) or 0.0
            monthly[key] = monthly.get(key, 0.0) + pnl
        return monthly

    @staticmethod
    def build_daily_pnl(closed_trades: list[Any]) -> dict[str, float]:
        """Return {YYYY-MM-DD: total_pnl} keyed by exit date."""
        daily: dict[str, float] = {}
        for trade in closed_trades:
            exit_time: datetime | None = getattr(trade, "exit_time", None)
            if exit_time is None:
                continue
            key = exit_time.strftime("%Y-%m-%d")
            pnl: float = getattr(trade, "pnl", 0.0) or 0.0
            daily[key] = daily.get(key, 0.0) + pnl
        return daily
