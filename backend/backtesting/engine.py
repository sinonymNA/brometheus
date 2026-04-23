"""APEX CRUSHER backtesting engine.

Day-by-day simulation over historical OHLCV bars.  No database calls —
all strategy logic is inlined; options pricing via Black-Scholes.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.backtesting.data_loader import HistoricalDataLoader
from backend.backtesting.metrics import MetricsCalculator
from backend.core.greeks_engine import calculate
from backend.utils.logger import get_logger

logger = get_logger(__name__)

STARTING_BALANCE: float = 50_000.0
MAX_CONTRACTS: int = 10


@dataclass
class BacktestParams:
    """Tunable parameters for a single isolated backtest run.

    All values are per-run only — they never touch live-bot constants.
    """
    rsi_bull_threshold: float = 62.0    # RSI above this → overbought
    rsi_bear_threshold: float = 38.0    # RSI below this → oversold
    volume_ratio_min: float = 1.5       # call/put volume ratio min for flow strategy
    iv_rank_max: float = 60.0           # momentum: skip when IV rank % exceeds this
    iv_rank_min: float = 70.0           # iv_rank strategy: trigger above this %
    signal_strength_min: float = 0.55   # discard signals below this
    stop_loss_pct: float = 0.08         # long: exit when premium loses this fraction
    profit_target_pct: float = 0.30     # exit when gain reaches this fraction
    min_dte: int = 5                    # close position when DTE ≤ this
    max_dte: int = 45                   # only enter options with ≤ this DTE
    max_positions: int = 3              # max concurrent open positions
    position_size_pct: float = 0.02     # max portfolio fraction risked per trade

    @classmethod
    def from_dict(cls, d: dict) -> "BacktestParams":
        valid = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dc_fields(self)}

# ── Internal trade dataclass ──────────────────────────────────────────────────


@dataclass
class _BacktestTrade:
    id: str
    symbol: str
    strategy: str           # "momentum" | "iv_rank" | "flow"
    signal_type: str
    action: str             # "buy" | "sell"
    option_type: str        # "call" | "put" | "condor"
    strike: float           # 0.0 for condors
    expiry: date
    quantity: int
    entry_price: float      # per-share credit/debit
    entry_time: datetime
    legs: list[dict] | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None
    pnl: float | None = None


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_dollars: float
    avg_loss_dollars: float
    profit_factor: float
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
    max_drawdown_dollars: float
    sharpe_ratio: float
    trades_by_strategy: dict[str, int]
    pnl_by_strategy: dict[str, float]
    equity_curve: list[float]
    daily_pnl_history: list[float]
    best_trade: dict
    worst_trade: dict
    best_day: dict
    worst_day: dict
    signals_generated: int
    signals_acted_on: int
    start_date: date
    end_date: date
    duration_days: int
    starting_balance: float
    ending_balance: float

    def to_dict(self) -> dict:
        def _cvt(v: Any) -> Any:
            if isinstance(v, date) and not isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        return {
            k: _cvt(v) if not isinstance(v, (dict, list)) else v
            for k, v in self.__dict__.items()
        }


# ── Helper functions ──────────────────────────────────────────────────────────


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI for a list of closes.  Returns None if insufficient data."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _compute_sma(closes: list[float], period: int = 20) -> float | None:
    """Simple moving average of the last `period` values.  None if insufficient data."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _next_expiry(from_date: date, target_dte: int = 30) -> date:
    """Find the next Friday on or after from_date + target_dte."""
    target = from_date + timedelta(days=target_dte)
    while target.weekday() != 4:  # 4 == Friday
        target += timedelta(days=1)
    return target


def _option_price(
    S: float,
    K: float,
    T_days: int,
    sigma: float,
    option_type: str,
    r: float = 0.05,
) -> float:
    """Return mid-price for a single leg."""
    T = T_days / 365.0
    if T <= 0:
        return max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
    try:
        res = calculate(S=S, K=K, T=T, r=r, sigma=sigma)
        return res.call_price if option_type == "call" else res.put_price
    except Exception:
        return 0.0


def _kelly_contracts(
    balance: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    strength: float,
    option_price: float,
    params: BacktestParams,
    recent_trades: list | None = None,
) -> int:
    """Half-Kelly sizing capped at params.position_size_pct and MAX_CONTRACTS.

    If avg_loss > 2× avg_win from the last 20 closed trades, position
    size is halved until the ratio improves.
    """
    if avg_loss <= 0 or option_price <= 0:
        return 1
    edge = win_rate - (1 - win_rate) * (avg_loss / max(avg_win, 0.01))
    kelly_raw = edge / (avg_loss / max(avg_win, 0.01)) if avg_win > 0 else 0.0
    kelly_f = max(kelly_raw * 0.5 * strength, 0.0)
    risk_dollars = balance * min(kelly_f, params.position_size_pct)
    contracts = int(risk_dollars / (option_price * 100))
    contracts = max(1, min(contracts, MAX_CONTRACTS))

    # Ratio check: reduce size when recent loss-to-win ratio is adverse
    if recent_trades and len(recent_trades) >= 10:
        recent_wins   = [t.pnl for t in recent_trades[-20:] if (t.pnl or 0) > 0]
        recent_losses = [t.pnl for t in recent_trades[-20:] if (t.pnl or 0) < 0]
        if recent_wins and recent_losses:
            r_avg_win  = sum(recent_wins) / len(recent_wins)
            r_avg_loss = abs(sum(recent_losses) / len(recent_losses))
            if r_avg_loss > 2.0 * r_avg_win:
                contracts = max(1, contracts // 2)

    return contracts


def _trade_pnl(trade: _BacktestTrade, exit_price: float) -> float:
    """Net PnL in dollars for 100-share multiplier."""
    multiplier = 100 * trade.quantity
    if trade.action == "sell":
        # Credit received upfront; profit = entry_price - exit_price
        return (trade.entry_price - exit_price) * multiplier
    else:
        return (exit_price - trade.entry_price) * multiplier


# ── Strategy evaluators ───────────────────────────────────────────────────────


def _eval_momentum(
    symbol: str,
    closes: list[float],
    spot: float,
    sigma: float,
    balance: float,
    today: date,
    params: BacktestParams,
    spy_spot: float | None = None,
    spy_ma20: float | None = None,
    iv_rank: float | None = None,
) -> list[dict] | None:
    """RSI-based signals with SPY 20d MA market-regime filter.

    Bull regime (SPY > 20d MA): calls only.
    Bear regime (SPY < 20d MA): puts only.
    Skips when IV rank exceeds params.iv_rank_max (vol crush risk).
    """
    rsi = _compute_rsi(closes)
    if rsi is None:
        return None

    # Skip momentum when implied vol is already expensive
    if iv_rank is not None and iv_rank > params.iv_rank_max:
        return None

    # Determine regime: True=bull, False=bear, None=unknown
    if spy_spot is not None and spy_ma20 is not None:
        bull_regime = spy_spot > spy_ma20
    else:
        bull_regime = None

    target_dte = min(21, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    if rsi > params.rsi_bull_threshold:
        strength = min((rsi - params.rsi_bull_threshold) / (100 - params.rsi_bull_threshold), 1.0)
        if bull_regime is False:
            strike = round(spot * 0.98, 0)
            price = _option_price(spot, strike, dte, sigma, "put")
            if price < 0.05:
                return None
            return [{"symbol": symbol, "strategy": "momentum",
                     "signal_type": "overbought_sell_put", "action": "sell",
                     "option_type": "put", "strike": strike,
                     "expiry": expiry, "price": price, "strength": strength}]
        else:
            strike = round(spot * 1.02, 0)
            price = _option_price(spot, strike, dte, sigma, "call")
            if price < 0.05:
                return None
            return [{"symbol": symbol, "strategy": "momentum",
                     "signal_type": "overbought_sell_call", "action": "sell",
                     "option_type": "call", "strike": strike,
                     "expiry": expiry, "price": price, "strength": strength}]

    if rsi < params.rsi_bear_threshold:
        strength = min((params.rsi_bear_threshold - rsi) / params.rsi_bear_threshold, 1.0)
        if bull_regime is False:
            strike = round(spot * 0.98, 0)
            price = _option_price(spot, strike, dte, sigma, "put")
            if price < 0.05:
                return None
            return [{"symbol": symbol, "strategy": "momentum",
                     "signal_type": "oversold_buy_put", "action": "buy",
                     "option_type": "put", "strike": strike,
                     "expiry": expiry, "price": price, "strength": strength}]
        else:
            strike = round(spot * 0.98, 0)
            price = _option_price(spot, strike, dte, sigma, "call")
            if price < 0.05:
                return None
            return [{"symbol": symbol, "strategy": "momentum",
                     "signal_type": "oversold_buy_call", "action": "buy",
                     "option_type": "call", "strike": strike,
                     "expiry": expiry, "price": price, "strength": strength}]

    return None


def _eval_iv_rank(
    symbol: str,
    spot: float,
    current_vix: float,
    vix_history: list[float],
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """IV rank: high IV → iron condor (sell). Threshold from params.iv_rank_min."""
    if len(vix_history) < 10:
        return None
    lo, hi = min(vix_history), max(vix_history)
    if hi <= lo:
        return None
    iv_rank = (current_vix - lo) / (hi - lo)
    if iv_rank * 100 < params.iv_rank_min:
        return None

    sigma = current_vix / 100.0
    target_dte = min(30, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    # Short call spread: sell ATM call, buy OTM call
    short_call_k = round(spot * 1.005, 0)
    long_call_k = round(spot * 1.05, 0)
    short_put_k = round(spot * 0.995, 0)
    long_put_k = round(spot * 0.95, 0)

    sc = _option_price(spot, short_call_k, dte, sigma, "call")
    lc = _option_price(spot, long_call_k, dte, sigma, "call")
    sp = _option_price(spot, short_put_k, dte, sigma, "put")
    lp = _option_price(spot, long_put_k, dte, sigma, "put")
    net_credit = sc - lc + sp - lp

    if net_credit < 0.10:
        return None

    return [{
        "symbol": symbol, "strategy": "iv_rank", "signal_type": "condor_sell",
        "action": "sell", "option_type": "condor", "strike": 0.0,
        "expiry": expiry, "price": net_credit, "strength": min(iv_rank, 1.0),
        "legs": [
            {"type": "call", "strike": short_call_k, "action": "sell", "price": sc},
            {"type": "call", "strike": long_call_k, "action": "buy", "price": lc},
            {"type": "put", "strike": short_put_k, "action": "sell", "price": sp},
            {"type": "put", "strike": long_put_k, "action": "buy", "price": lp},
        ],
    }]


def _eval_flow(
    symbol: str,
    spot: float,
    sigma: float,
    chain: list[dict],
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """Simulated unusual flow: call/put volume ratio above params.volume_ratio_min → buy call."""
    if not chain:
        return None
    call_vol = sum(c["volume"] for c in chain if c["option_type"] == "call")
    put_vol = sum(c["volume"] for c in chain if c["option_type"] == "put")
    if put_vol == 0:
        return None
    ratio = call_vol / put_vol
    if ratio < params.volume_ratio_min:
        return None

    target_dte = min(14, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days
    strike = round(spot * 1.01, 0)
    price = _option_price(spot, strike, dte, sigma, "call")
    if price < 0.05:
        return None

    strength = min((ratio - 1.5) / 1.5, 1.0)
    return [{
        "symbol": symbol, "strategy": "flow", "signal_type": "unusual_call_flow",
        "action": "buy", "option_type": "call", "strike": strike,
        "expiry": expiry, "price": price, "strength": strength,
    }]


def _check_exit(
    trade: _BacktestTrade,
    spot: float,
    sigma: float,
    today: date,
    params: BacktestParams,
) -> tuple[bool, str, float]:
    """Returns (should_exit, reason, exit_price)."""
    dte = (trade.expiry - today).days

    if dte <= 0:
        ep = _current_price(trade, spot, sigma, 0)
        return True, "expiry", ep

    if dte <= params.min_dte:
        ep = _current_price(trade, spot, sigma, dte)
        return True, "dte_close", ep

    ep = _current_price(trade, spot, sigma, dte)
    pnl = _trade_pnl(trade, ep)
    entry_value = trade.entry_price * 100 * trade.quantity

    if trade.action == "sell":
        # Profit target: collect params.profit_target_pct of credit
        if pnl >= entry_value * params.profit_target_pct:
            return True, "profit_target", ep
        # Stop: short options stop at 10× the buy stop (credit spread convention)
        if pnl <= -entry_value * (params.stop_loss_pct * 10):
            return True, "stop_loss", ep
    else:
        # Profit target: long options — 2.5× of profit_target_pct for higher upside
        if pnl >= entry_value * (params.profit_target_pct * 2.5):
            return True, "profit_target", ep
        if pnl <= -entry_value * params.stop_loss_pct:
            return True, "stop_loss", ep

    return False, "", 0.0


def _current_price(
    trade: _BacktestTrade,
    spot: float,
    sigma: float,
    dte: int,
) -> float:
    """Mark-to-market price for the trade."""
    if trade.option_type == "condor":
        # Reprice all four legs
        if not trade.legs:
            return 0.0
        total = 0.0
        for leg in trade.legs:
            p = _option_price(spot, leg["strike"], dte, sigma, leg["type"])
            total += p if leg["action"] == "sell" else -p
        return max(total, 0.0)
    return _option_price(spot, trade.strike, dte, sigma, trade.option_type)


# ── Main engine ───────────────────────────────────────────────────────────────


class BacktestEngine:
    def __init__(
        self,
        loader: HistoricalDataLoader,
        symbols: list[str],
        strategies: list[str] | None = None,
    ) -> None:
        self._loader = loader
        self._symbols = symbols
        self._strategies = strategies or ["momentum", "iv_rank", "flow"]

    async def run(
        self,
        start_date: date,
        end_date: date,
        progress_cb: Any = None,
        params: BacktestParams | None = None,
    ) -> BacktestResult:
        """Execute day-by-day simulation and return BacktestResult."""
        if params is None:
            params = BacktestParams()
        logger.info("Backtest run: %s → %s  symbols=%s  params=%s",
                    start_date, end_date, self._symbols, params.to_dict())

        # ── Load data ────────────────────────────────────────────────────────
        # Always include SPY for the market-regime filter even if the user
        # didn't explicitly request it; deduplicate to avoid double-loading.
        load_symbols = list(dict.fromkeys(["SPY"] + self._symbols))
        bars_map = await self._loader.load_stock_data(load_symbols, start_date, end_date)
        vix_map = await self._loader.load_vix_data(start_date, end_date)

        # Build sorted list of trading days (intersection across strategy symbols)
        strategy_symbols = [s for s in self._symbols if s in bars_map]
        day_sets = [
            {b.timestamp.date() for b in bars_map[s]}
            for s in strategy_symbols
        ]
        if not day_sets:
            raise ValueError("No bar data loaded")
        trading_days = sorted(set.intersection(*day_sets) if len(day_sets) > 1 else day_sets[0])
        trading_days = [d for d in trading_days if start_date <= d <= end_date]

        if not trading_days:
            raise ValueError("No trading days in range")

        # ── Simulation state ─────────────────────────────────────────────────
        balance = STARTING_BALANCE
        open_trades: list[_BacktestTrade] = []
        closed_trades: list[_BacktestTrade] = []
        equity_curve: list[float] = [balance]
        daily_pnl_list: list[float] = []
        signals_generated = 0
        signals_acted_on = 0

        # Build per-symbol close-price arrays keyed by date
        closes_by_symbol: dict[str, dict[date, float]] = {}
        for sym, bars in bars_map.items():
            closes_by_symbol[sym] = {b.timestamp.date(): b.close for b in bars}

        # SPY close list ordered by trading_days for rolling MA computation
        spy_close_list: list[float] = []

        vix_history_window: list[float] = []
        total_days = len(trading_days)

        for day_idx, today in enumerate(trading_days):
            day_open_pnl = 0.0

            # ── Progress callback ────────────────────────────────────────────
            if progress_cb and day_idx % 10 == 0:
                pct = int(day_idx / total_days * 100)
                try:
                    await progress_cb(pct, f"Simulating {today}")
                except Exception:
                    pass

            # VIX for today
            current_vix = vix_map.get(today, 20.0)
            vix_history_window.append(current_vix)
            if len(vix_history_window) > 252:
                vix_history_window.pop(0)

            sigma = max(current_vix / 100.0, 0.05)

            # SPY regime: update rolling close list and compute 20d MA
            spy_today = closes_by_symbol.get("SPY", {}).get(today)
            if spy_today:
                spy_close_list.append(spy_today)
            spy_ma20 = _compute_sma(spy_close_list, 20)

            # IV rank for today (0–100 scale, passed to momentum filter)
            if len(vix_history_window) >= 10:
                lo_v, hi_v = min(vix_history_window), max(vix_history_window)
                iv_rank_today = ((current_vix - lo_v) / (hi_v - lo_v) * 100) if hi_v > lo_v else 0.0
            else:
                iv_rank_today = None

            # ── Exit checks for open positions ───────────────────────────────
            still_open: list[_BacktestTrade] = []
            for trade in open_trades:
                spot = closes_by_symbol.get(trade.symbol, {}).get(today, 0.0)
                if spot <= 0:
                    still_open.append(trade)
                    continue
                t_sigma = sigma
                should_exit, reason, exit_price = _check_exit(trade, spot, t_sigma, today, params)
                if should_exit:
                    pnl = _trade_pnl(trade, exit_price)
                    trade.exit_price = exit_price
                    trade.exit_time = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
                    trade.exit_reason = reason
                    trade.pnl = pnl
                    balance += pnl
                    day_open_pnl += pnl
                    closed_trades.append(trade)
                else:
                    still_open.append(trade)
            open_trades = still_open

            # ── Signal generation + entry ────────────────────────────────────
            if len(open_trades) < MAX_CONCURRENT_POSITIONS:
                for sym in self._symbols:
                    spot = closes_by_symbol.get(sym, {}).get(today, 0.0)
                    if spot <= 0:
                        continue

                    # Rolling close array up to today
                    sym_closes = [
                        closes_by_symbol[sym][d]
                        for d in trading_days[:day_idx + 1]
                        if d in closes_by_symbol[sym]
                    ]

                    # Build lightweight chain for flow strategy
                    chain: list[dict] = []
                    if "flow" in self._strategies:
                        chain = self._loader.reconstruct_options_chain(sym, spot, current_vix,
                                                                        datetime.combine(today, datetime.min.time()))

                    candidates: list[dict] = []
                    if "momentum" in self._strategies:
                        sigs = _eval_momentum(
                            sym, sym_closes, spot, sigma, balance, today, params,
                            spy_spot=spy_today, spy_ma20=spy_ma20,
                            iv_rank=iv_rank_today,
                        )
                        if sigs:
                            candidates.extend(sigs)
                    if "iv_rank" in self._strategies:
                        sigs = _eval_iv_rank(sym, spot, current_vix, vix_history_window, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)
                    if "flow" in self._strategies:
                        sigs = _eval_flow(sym, spot, sigma, chain, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)

                    signals_generated += len(candidates)

                    # Quality filter: discard low-conviction signals
                    candidates = [s for s in candidates if s.get("strength", 0) > params.signal_strength_min]

                    for sig in candidates:
                        if len(open_trades) >= params.max_positions:
                            break
                        # Avoid duplicate symbol+strategy positions
                        existing = [(t.symbol, t.strategy) for t in open_trades]
                        if (sig["symbol"], sig["strategy"]) in existing:
                            continue

                        qty = _kelly_contracts(
                            balance,
                            win_rate=0.55,
                            avg_win=200.0,
                            avg_loss=120.0,
                            strength=sig["strength"],
                            option_price=sig["price"],
                            params=params,
                            recent_trades=closed_trades,
                        )
                        cost = sig["price"] * 100 * qty
                        if sig["action"] == "buy" and cost > balance * params.position_size_pct * 2:
                            qty = max(1, int(balance * params.position_size_pct / (sig["price"] * 100)))

                        trade = _BacktestTrade(
                            id=uuid.uuid4().hex,
                            symbol=sig["symbol"],
                            strategy=sig["strategy"],
                            signal_type=sig["signal_type"],
                            action=sig["action"],
                            option_type=sig["option_type"],
                            strike=sig.get("strike", 0.0),
                            expiry=sig["expiry"],
                            quantity=qty,
                            entry_price=sig["price"],
                            entry_time=datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc),
                            legs=sig.get("legs"),
                        )
                        open_trades.append(trade)
                        signals_acted_on += 1

            equity_curve.append(balance)
            daily_pnl_list.append(day_open_pnl)

        # ── Force-close any remaining positions at last day's price ──────────
        last_day = trading_days[-1] if trading_days else end_date
        for trade in open_trades:
            spot = closes_by_symbol.get(trade.symbol, {}).get(last_day, 0.0)
            dte = max((trade.expiry - last_day).days, 0)
            ep = _current_price(trade, spot, sigma, dte)
            pnl = _trade_pnl(trade, ep)
            trade.exit_price = ep
            trade.exit_time = datetime.combine(last_day, datetime.min.time()).replace(tzinfo=timezone.utc)
            trade.exit_reason = "end_of_backtest"
            trade.pnl = pnl
            balance += pnl
            closed_trades.append(trade)
        open_trades = []

        # ── Compute metrics ──────────────────────────────────────────────────
        pnls = [t.pnl for t in closed_trades if t.pnl is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_rate = MetricsCalculator.calculate_win_rate(pnls)
        profit_factor = MetricsCalculator.calculate_profit_factor(wins, losses)
        max_dd_pct, _ = MetricsCalculator.calculate_max_drawdown(equity_curve)
        sharpe = MetricsCalculator.calculate_sharpe(daily_pnl_list)

        total_pnl = sum(pnls)
        total_return_pct = total_pnl / STARTING_BALANCE

        trades_by_strategy: dict[str, int] = {}
        pnl_by_strategy: dict[str, float] = {}
        for t in closed_trades:
            trades_by_strategy[t.strategy] = trades_by_strategy.get(t.strategy, 0) + 1
            pnl_by_strategy[t.strategy] = pnl_by_strategy.get(t.strategy, 0.0) + (t.pnl or 0.0)

        best = max(closed_trades, key=lambda t: t.pnl or 0.0, default=None)
        worst = min(closed_trades, key=lambda t: t.pnl or 0.0, default=None)

        def _trade_dict(t: _BacktestTrade | None) -> dict:
            if t is None:
                return {}
            return {
                "id": t.id, "symbol": t.symbol, "strategy": t.strategy,
                "option_type": t.option_type, "pnl": t.pnl,
                "entry_time": t.entry_time.isoformat(), "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            }

        daily_pnl_by_date = MetricsCalculator.build_daily_pnl(closed_trades)
        best_day_key = max(daily_pnl_by_date, key=daily_pnl_by_date.get, default=None) if daily_pnl_by_date else None
        worst_day_key = min(daily_pnl_by_date, key=daily_pnl_by_date.get, default=None) if daily_pnl_by_date else None

        return BacktestResult(
            total_trades=len(closed_trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=win_rate,
            avg_win_dollars=sum(wins) / len(wins) if wins else 0.0,
            avg_loss_dollars=sum(losses) / len(losses) if losses else 0.0,
            profit_factor=profit_factor,
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
            max_drawdown_pct=max_dd_pct,
            max_drawdown_dollars=max_dd_pct * STARTING_BALANCE,
            sharpe_ratio=sharpe,
            trades_by_strategy=trades_by_strategy,
            pnl_by_strategy=pnl_by_strategy,
            equity_curve=equity_curve,
            daily_pnl_history=daily_pnl_list,
            best_trade=_trade_dict(best),
            worst_trade=_trade_dict(worst),
            best_day={"date": best_day_key, "pnl": daily_pnl_by_date.get(best_day_key, 0.0)} if best_day_key else {},
            worst_day={"date": worst_day_key, "pnl": daily_pnl_by_date.get(worst_day_key, 0.0)} if worst_day_key else {},
            signals_generated=signals_generated,
            signals_acted_on=signals_acted_on,
            start_date=start_date,
            end_date=end_date,
            duration_days=(end_date - start_date).days,
            starting_balance=STARTING_BALANCE,
            ending_balance=balance,
        )

    async def run_walk_forward(
        self,
        train_start: date,
        train_end: date,
        test_start: date,
        test_end: date,
        params: BacktestParams | None = None,
    ) -> dict:
        """Run train window then test window; return comparison dict."""
        logger.info(
            "Walk-forward: train=%s→%s  test=%s→%s",
            train_start, train_end, test_start, test_end,
        )
        train_result = await self.run(train_start, train_end, params=params)
        test_result = await self.run(test_start, test_end, params=params)

        return {
            "train": train_result.to_dict(),
            "test": test_result.to_dict(),
            "degradation": {
                "win_rate_delta": test_result.win_rate - train_result.win_rate,
                "sharpe_delta": test_result.sharpe_ratio - train_result.sharpe_ratio,
                "return_delta": test_result.total_return_pct - train_result.total_return_pct,
                "max_dd_delta": test_result.max_drawdown_pct - train_result.max_drawdown_pct,
            },
        }
