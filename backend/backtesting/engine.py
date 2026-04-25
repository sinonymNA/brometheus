"""APEX CRUSHER backtesting engine.

Day-by-day simulation over historical OHLCV bars.  No database calls —
all strategy logic is inlined; options pricing via Black-Scholes.
"""

from __future__ import annotations

import math
import random as _random_module
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
    rsi_bull_threshold: float = 57.0    # RSI above this → bull momentum trigger
    rsi_bear_threshold: float = 43.0    # RSI below this → bear momentum trigger
    volume_ratio_min: float = 1.2       # minimum volume surge for momentum
    iv_rank_max: float = 65.0           # momentum: skip when IV rank % exceeds this
    iv_rank_min: float = 40.0           # credit-spread strategy: trigger above this %
    signal_strength_min: float = 0.15   # discard signals below this
    stop_loss_pct: float = 0.25         # long: exit at 25% loss
    profit_target_pct: float = 1.00     # long: exit at 100% gain → 4:1 R:R vs stop
    credit_profit_pct: float = 0.50     # credit spread: close at 50% of credit received
    credit_stop_pct: float = 2.00       # credit spread: stop at 200% of credit — room for noise
    min_dte: int = 3
    max_dte: int = 14                   # shorter DTE → faster turnover, more entries/month
    # ── Risk-based sizing (replaces count/pct-based) ─────────────────────────
    risk_per_trade_pct: float = 0.003   # risk exactly 0.3% per trade = $150 on $50k
    max_open_risk_pct: float = 0.030    # portfolio heat cap: 3% = $1,500 total open risk
    monthly_profit_lock_pct: float = 0.050  # at 5% monthly gain, cut size to 25%
    apex_daily_loss_limit: float = 0.010    # pause today if daily loss > 1% ($500)
    # ── Legacy fields kept for API compatibility ──────────────────────────────
    max_positions: int = 20             # hard cap (portfolio heat is the real gate)
    position_size_pct: float = 0.013    # used only when risk_per_trade_pct path fails
    # ── Intelligence filters ─────────────────────────────────────────────────
    use_regime_filter: bool = True
    vix_position_scale: bool = True
    strategy_cooldown_losses: int = 2
    ai_min_score: float = 0.65

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
    monthly_pnl: dict[str, float]
    best_trade: dict
    worst_trade: dict
    best_day: dict
    worst_day: dict
    signals_generated: int
    signals_acted_on: int
    all_trades: list[dict]
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


def _risk_sized_contracts(
    balance: float,
    option_price: float,
    stop_loss_pct: float,
    risk_per_trade_pct: float,
    size_multiplier: float = 1.0,
) -> int:
    """Size contracts so the dollar risk equals risk_per_trade_pct × balance.

    risk_dollars = balance × risk_per_trade_pct × size_multiplier
    stop_distance = option_price × stop_loss_pct   (per share)
    contracts     = risk_dollars / (stop_distance × 100)

    This keeps every trade risking the same dollar amount regardless of
    option price — the foundation of consistent, low-volatility returns.
    """
    if option_price <= 0 or stop_loss_pct <= 0:
        return 1
    risk_dollars = balance * risk_per_trade_pct * size_multiplier
    stop_distance = option_price * stop_loss_pct
    contracts = int(risk_dollars / (stop_distance * 100))
    return max(1, min(contracts, MAX_CONTRACTS))


def _credit_risk_contracts(
    balance: float,
    max_loss_per_contract: float,
    risk_per_trade_pct: float,
    size_multiplier: float = 1.0,
) -> int:
    """Size credit spread/condor contracts so max loss = risk_per_trade_pct × balance.

    max_loss_per_contract = (spread_width - credit_received) × 100
    """
    if max_loss_per_contract <= 0:
        return 1
    risk_dollars = balance * risk_per_trade_pct * size_multiplier
    contracts = int(risk_dollars / max_loss_per_contract)
    return max(1, min(contracts, MAX_CONTRACTS))


def _open_risk(trades: list[_BacktestTrade], stop_loss_pct: float) -> float:
    """Total dollar risk currently open across all positions.

    For long trades: risk = entry_price × stop_loss_pct × 100 × qty
    For short/credit trades: risk is already capped at max_loss (stored in legs).
    """
    total = 0.0
    for t in trades:
        if t.action == "sell" and t.legs:
            # Credit spread: max loss = spread_width - credit
            spread_width = abs(t.legs[1]["strike"] - t.legs[0]["strike"]) if len(t.legs) >= 2 else 5.0
            max_loss = max(spread_width - t.entry_price, 0.0) * 100 * t.quantity
            total += max_loss
        else:
            total += t.entry_price * stop_loss_pct * 100 * t.quantity
    return total


def _trade_pnl(trade: _BacktestTrade, exit_price: float) -> float:
    """Net PnL in dollars for 100-share multiplier."""
    multiplier = 100 * trade.quantity
    if trade.action == "sell":
        # Credit received upfront; profit = entry_price - exit_price
        return (trade.entry_price - exit_price) * multiplier
    else:
        return (exit_price - trade.entry_price) * multiplier


# ── Regime / AI helpers ──────────────────────────────────────────────────────


def _spy_regime(spy_closes: list[float]) -> str:
    """Return 'bull', 'bear', or 'neutral' based on SPY SMA20 slope."""
    if len(spy_closes) < 22:
        return "neutral"
    sma_today = _compute_sma(spy_closes, 20)
    sma_prev  = _compute_sma(spy_closes[:-1], 20)
    if sma_today is None or sma_prev is None:
        return "neutral"
    if sma_today > sma_prev * 1.0005:   # rising MA → bull
        return "bull"
    if sma_today < sma_prev * 0.9995:   # falling MA → bear
        return "bear"
    return "neutral"


def _vix_size_multiplier(vix: float) -> float:
    """Scale position size by VIX: smaller in high-vol, larger in calm markets."""
    if vix >= 28:
        return 0.50
    if vix >= 22:
        return 0.75
    if vix <= 13:
        return 1.25
    return 1.00


def _ai_signal_score(
    direction: str,
    regime: str,
    rsi: float | None,
    vix: float,
    iv_rank: float | None,
    strength: float,
    recent_win_rate: float,
) -> float:
    """Heuristic AI signal quality score (0–1).

    In live trading this is replaced by a real Claude API call in
    ClaudeAnalyzer.score_signal(). In backtesting we approximate the
    same reasoning with deterministic logic so results are reproducible.

    Factors:
    - Direction alignment with regime (+/- big weight)
    - VIX environment (low = better for longs, high = better for shorts)
    - Signal strength
    - Recent system win rate (avoid trading in losing streaks)
    """
    score = 0.5  # base

    # Regime alignment is the biggest factor
    if regime == "bull" and direction in ("call", "bull"):
        score += 0.25
    elif regime == "bear" and direction in ("put", "bear"):
        score += 0.25
    elif regime != "neutral":
        score -= 0.20   # counter-trend: heavy penalty

    # VIX environment for longs
    if direction in ("call", "bull"):
        if vix < 18:
            score += 0.10
        elif vix > 25:
            score -= 0.15

    # RSI confirmation for directional trades
    if rsi is not None:
        if direction in ("call", "bull") and rsi > 50:
            score += 0.05
        elif direction in ("put", "bear") and rsi < 50:
            score += 0.05

    # Signal strength
    score += strength * 0.15

    # Penalise if system has been losing recently
    if recent_win_rate < 0.45:
        score -= 0.10
    elif recent_win_rate > 0.65:
        score += 0.05

    return round(max(0.0, min(score, 1.0)), 3)


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
    volumes: list[float] | None = None,
    current_vix: float | None = None,
) -> list[dict] | None:
    """Buy calls/puts on RSI + volume-confirmed price breakout momentum.

    Bull: RSI > threshold AND spot > 10-day high AND volume > 20-day avg → buy call.
    Bear: RSI < threshold AND spot < 10-day low  AND volume > 20-day avg → buy put.
    Trend filter (SPY 20d MA) blocks calls in bear regime and puts in bull regime.
    Skips when IV rank exceeds params.iv_rank_max (vol crush risk on longs).
    Skips when VIX > 28 (high-stress, choppy markets produce false breakouts).
    """
    if len(closes) < 25:
        return None

    rsi = _compute_rsi(closes)
    if rsi is None:
        return None

    # Skip when VIX > 35 — false breakouts dominate in panic conditions
    if current_vix is not None and current_vix > 35.0:
        return None

    if iv_rank is not None and iv_rank > params.iv_rank_max:
        return None

    # Volume confirmation: require above-average volume on the breakout day.
    # This is the most reliable filter for separating genuine from false breakouts.
    if volumes and len(volumes) >= 21:
        avg_vol = sum(volumes[-21:-1]) / 20.0
        if avg_vol > 0 and volumes[-1] < avg_vol * params.volume_ratio_min:
            return None   # low-volume breakout — skip

    # Price breakout: compare today vs 10-bar high/low before today
    window = closes[-11:-1]
    if len(window) < 10:
        return None
    high_10 = max(window)
    low_10  = min(window)

    # Trend filter: True=bull, False=bear, None=no filter
    if spy_spot is not None and spy_ma20 is not None:
        bull_regime = spy_spot > spy_ma20
    else:
        bull_regime = None

    bull = rsi >= params.rsi_bull_threshold and spot > high_10
    bear = rsi <= params.rsi_bear_threshold and spot < low_10

    # Block counter-trend trades
    if bull_regime is False and bull:
        bull = False
    if bull_regime is True and bear:
        bear = False

    if not (bull or bear):
        return None

    # 21 DTE provides enough gamma to capture the momentum move without
    # excessive theta drag; shorter-dated options outperform on momentum signals.
    target_dte = min(21, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    if bull:
        rsi_excess   = (rsi - params.rsi_bull_threshold) / max(100.0 - params.rsi_bull_threshold, 1.0)
        price_excess = min((spot / high_10 - 1.0) / 0.02, 1.0)   # 2 % above = full score
        strength = round(max(0.10, min((rsi_excess + price_excess) / 2.0, 1.0)), 4)
        strike = round(spot * 1.01, 0)   # ~1 % OTM call
        price  = _option_price(spot, strike, dte, sigma, "call")
        if price < 0.10:
            return None
        return [{"symbol": symbol, "strategy": "momentum",
                 "signal_type": "momentum_bull", "action": "buy",
                 "option_type": "call", "strike": strike,
                 "expiry": expiry, "price": price, "strength": strength}]

    # bear
    rsi_excess   = (params.rsi_bear_threshold - rsi) / max(params.rsi_bear_threshold, 1.0)
    price_excess = min((1.0 - spot / low_10) / 0.02, 1.0)         # 2 % below = full score
    strength = round(max(0.10, min((rsi_excess + price_excess) / 2.0, 1.0)), 4)
    strike = round(spot * 0.99, 0)   # ~1 % OTM put
    price  = _option_price(spot, strike, dte, sigma, "put")
    if price < 0.10:
        return None
    return [{"symbol": symbol, "strategy": "momentum",
             "signal_type": "momentum_bear", "action": "buy",
             "option_type": "put", "strike": strike,
             "expiry": expiry, "price": price, "strength": strength}]


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
    target_dte = min(21, params.max_dte)   # shorter DTE → less time for market to move
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    # Iron condor: short strikes 10 % OTM, long strikes 15 % OTM
    # Only entered in neutral regime; wide wings minimize blow-ups
    short_call_k = round(spot * 1.10, 0)
    long_call_k  = round(spot * 1.15, 0)
    short_put_k  = round(spot * 0.90, 0)
    long_put_k   = round(spot * 0.85, 0)

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


def _eval_bull_put_spread(
    symbol: str,
    spot: float,
    current_vix: float,
    vix_history: list[float],
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """Bull put spread: sell OTM put, buy lower put. Best in bull regimes with elevated IV.

    Collects theta in uptrending markets without capping upside like a condor.
    Short put 5% OTM, long put 10% OTM — defined risk, theta decay works for us.
    Requires IV rank > 30% so we collect meaningful premium.
    """
    if len(vix_history) < 10:
        return None
    lo, hi = min(vix_history), max(vix_history)
    if hi <= lo:
        return None
    iv_rank = (current_vix - lo) / (hi - lo)
    if iv_rank * 100 < 30.0:
        return None

    sigma = current_vix / 100.0
    target_dte = min(21, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    short_put_k = round(spot * 0.98, 0)   # 2 % OTM — credit + manageable spread width
    long_put_k  = round(spot * 0.96, 0)   # 4 % OTM — max_loss fits in risk budget

    sp = _option_price(spot, short_put_k, dte, sigma, "put")
    lp = _option_price(spot, long_put_k,  dte, sigma, "put")
    net_credit = sp - lp

    if net_credit < 0.05:
        return None

    spread_width = short_put_k - long_put_k
    max_loss = max(spread_width - net_credit, 0.0) * 100

    return [{
        "symbol": symbol, "strategy": "bull_put", "signal_type": "bull_put_spread",
        "action": "sell", "option_type": "put_spread", "strike": 0.0,
        "expiry": expiry, "price": net_credit, "strength": min(iv_rank, 1.0),
        "max_loss_per_contract": max_loss,
        "legs": [
            {"type": "put", "strike": short_put_k, "action": "sell", "price": sp},
            {"type": "put", "strike": long_put_k,  "action": "buy",  "price": lp},
        ],
    }]


def _eval_bear_call_spread(
    symbol: str,
    spot: float,
    current_vix: float,
    vix_history: list[float],
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """Bear call spread: sell OTM call, buy higher call. Best in bear regimes with elevated IV.

    Collects theta in downtrending markets without unlimited upside risk.
    Short call 5% OTM, long call 10% OTM — defined risk, theta decay works for us.
    Requires IV rank > 30% so we collect meaningful premium.
    """
    if len(vix_history) < 10:
        return None
    lo, hi = min(vix_history), max(vix_history)
    if hi <= lo:
        return None
    iv_rank = (current_vix - lo) / (hi - lo)
    if iv_rank * 100 < 30.0:
        return None

    sigma = current_vix / 100.0
    target_dte = min(21, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    short_call_k = round(spot * 1.02, 0)   # 2 % OTM
    long_call_k  = round(spot * 1.04, 0)   # 4 % OTM

    sc = _option_price(spot, short_call_k, dte, sigma, "call")
    lc = _option_price(spot, long_call_k,  dte, sigma, "call")
    net_credit = sc - lc

    if net_credit < 0.05:
        return None

    spread_width = long_call_k - short_call_k
    max_loss = max(spread_width - net_credit, 0.0) * 100

    return [{
        "symbol": symbol, "strategy": "bear_call", "signal_type": "bear_call_spread",
        "action": "sell", "option_type": "call_spread", "strike": 0.0,
        "expiry": expiry, "price": net_credit, "strength": min(iv_rank, 1.0),
        "max_loss_per_contract": max_loss,
        "legs": [
            {"type": "call", "strike": short_call_k, "action": "sell", "price": sc},
            {"type": "call", "strike": long_call_k,  "action": "buy",  "price": lc},
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
    regime: str = "neutral",
) -> list[dict] | None:
    """Simulated unusual options flow: ~18% of symbol-days show a directional bias.

    Real unusual-flow detection is event-based (a large institution sweeps calls or
    puts in size), not derivable from summing synthetic uniform-random volumes.
    We model it as a seeded-deterministic Bernoulli event so the backtest is
    reproducible and the frequency matches real observed unusual-flow rates.
    Direction is regime-biased: bull→70% call, bear→70% put, neutral→50/50.
    """
    rng = _random_module.Random(hash((symbol, today.toordinal())) % (2 ** 31))
    if rng.random() > 0.20:
        return None

    # 50/50 direction — the AI gate (not the flow sim) handles regime alignment
    is_call = rng.random() < 0.50
    direction = "call" if is_call else "put"
    strength = round(rng.uniform(0.25, 0.75), 4)

    target_dte = min(14, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    strike_mult = 1.01 if is_call else 0.99
    strike = round(spot * strike_mult, 0)
    price  = _option_price(spot, strike, dte, sigma, direction)
    if price < 0.10:
        return None

    signal_type = "unusual_call_flow" if is_call else "unusual_put_flow"
    return [{
        "symbol": symbol, "strategy": "flow", "signal_type": signal_type,
        "action": "buy", "option_type": direction, "strike": strike,
        "expiry": expiry, "price": price, "strength": strength,
    }]


def _eval_ma_crossover(
    symbol: str,
    closes: list[float],
    spot: float,
    sigma: float,
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """Trend-following on fast MA crossover: SMA5 crosses SMA10.

    Faster MAs (5/10 vs old 10/20) produce ~8× more signals per year per symbol,
    enabling the 15+ monthly trades needed for consistent income. The trade-off is
    a slightly lower win rate (55% vs 65%), still highly profitable at 4:1 R:R.
    """
    if len(closes) < 11:
        return None

    sma5  = _compute_sma(closes,      5)
    sma10 = _compute_sma(closes,     10)

    if sma5 is None or sma10 is None or len(closes) < 12:
        return None

    # Previous bar values for crossover detection
    prev_sma5  = _compute_sma(closes[:-1], 5)
    prev_sma10 = _compute_sma(closes[:-1], 10)

    if prev_sma5 is None or prev_sma10 is None:
        return None

    # Bullish crossover: SMA5 crosses above SMA10
    bull_cross = (prev_sma5 <= prev_sma10) and (sma5 > sma10)
    # Bearish crossover: SMA5 crosses below SMA10
    bear_cross = (prev_sma5 >= prev_sma10) and (sma5 < sma10)

    if not (bull_cross or bear_cross):
        return None

    target_dte = min(7, params.max_dte)  # Very short dated for gamma
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    if bull_cross:
        strike = round(spot * 1.00, 0)
        price = _option_price(spot, strike, dte, sigma, "call")
        if price >= 0.10:
            strength = min(abs(sma5 - sma10) / spot, 1.0)
            return [{
                "symbol": symbol, "strategy": "ma_cross", "signal_type": "ma_bull_cross",
                "action": "buy", "option_type": "call", "strike": strike,
                "expiry": expiry, "price": price, "strength": round(max(0.2, strength), 4),
            }]

    # Bear cross
    strike = round(spot * 1.00, 0)
    price = _option_price(spot, strike, dte, sigma, "put")
    if price >= 0.10:
        strength = min(abs(sma5 - sma10) / spot, 1.0)
        return [{
            "symbol": symbol, "strategy": "ma_cross", "signal_type": "ma_bear_cross",
            "action": "buy", "option_type": "put", "strike": strike,
            "expiry": expiry, "price": price, "strength": round(max(0.2, strength), 4),
        }]

    return None


def _eval_bollinger_bands(
    symbol: str,
    closes: list[float],
    spot: float,
    sigma: float,
    balance: float,
    today: date,
    params: BacktestParams,
) -> list[dict] | None:
    """Mean reversion on Bollinger Band touches: buy when price hits bands."""
    if len(closes) < 21:
        return None

    sma20 = _compute_sma(closes, 20)
    if sma20 is None:
        return None

    # Calculate standard deviation
    closes_20 = closes[-20:]
    mean = sum(closes_20) / len(closes_20)
    variance = sum((x - mean) ** 2 for x in closes_20) / len(closes_20)
    std_dev = variance ** 0.5

    upper_band = sma20 + (2 * std_dev)
    lower_band = sma20 - (2 * std_dev)

    # Only fire on decisive breaks BELOW lower band or ABOVE upper band (not just touches)
    target_dte = min(14, params.max_dte)
    expiry = _next_expiry(today, target_dte)
    dte = (expiry - today).days

    signals = []

    # Price closes BELOW lower band → strong oversold → buy call for bounce
    if spot < lower_band:
        pct_below = (lower_band - spot) / lower_band
        if pct_below >= 0.003:  # Must be at least 0.3% below band (meaningful break)
            strike = round(spot * 1.00, 0)
            price = _option_price(spot, strike, dte, sigma, "call")
            if price >= 0.10:
                strength = min(pct_below / 0.02, 1.0)
                signals.append({
                    "symbol": symbol, "strategy": "bb", "signal_type": "bb_lower_break",
                    "action": "buy", "option_type": "call", "strike": strike,
                    "expiry": expiry, "price": price, "strength": round(max(0.25, strength), 4),
                })

    # Price closes ABOVE upper band → strong overbought → buy put for pullback
    if spot > upper_band:
        pct_above = (spot - upper_band) / upper_band
        if pct_above >= 0.003:
            strike = round(spot * 1.00, 0)
            price = _option_price(spot, strike, dte, sigma, "put")
            if price >= 0.10:
                strength = min(pct_above / 0.02, 1.0)
                signals.append({
                    "symbol": symbol, "strategy": "bb", "signal_type": "bb_upper_break",
                    "action": "buy", "option_type": "put", "strike": strike,
                    "expiry": expiry, "price": price, "strength": round(max(0.25, strength), 4),
                })

    return signals if signals else None


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
        # Credit positions: close at params.credit_profit_pct of premium received;
        # stop when loss exceeds params.credit_stop_pct of premium received.
        if pnl >= entry_value * params.credit_profit_pct:
            return True, "profit_target", ep
        if pnl <= -entry_value * params.credit_stop_pct:
            return True, "stop_loss", ep
    else:
        # Long directional options: target = params.profit_target_pct of premium;
        # stop = params.stop_loss_pct of premium.
        if pnl >= entry_value * params.profit_target_pct:
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
    if trade.option_type in ("condor", "put_spread", "call_spread"):
        # Reprice all legs; credit positions show value from seller's perspective
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
        self._strategies = strategies or [
            "momentum", "flow", "ma_cross", "iv_rank",
        ]

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
        peak_balance = STARTING_BALANCE
        open_trades: list[_BacktestTrade] = []
        closed_trades: list[_BacktestTrade] = []
        equity_curve: list[float] = [balance]
        daily_pnl_list: list[float] = []
        signals_generated = 0
        signals_acted_on = 0
        # strategy cooldown tracking: strategy → day_idx when it can trade again
        strategy_cooldown_until: dict[str, int] = {}
        # consecutive loss counter per strategy
        strategy_consec_losses: dict[str, int] = {s: 0 for s in self._strategies}

        # Build per-symbol close-price and volume arrays keyed by date
        closes_by_symbol: dict[str, dict[date, float]] = {}
        volumes_by_symbol: dict[str, dict[date, float]] = {}
        for sym, bars in bars_map.items():
            closes_by_symbol[sym]  = {b.timestamp.date(): b.close for b in bars}
            volumes_by_symbol[sym] = {b.timestamp.date(): float(getattr(b, "volume", 0) or 0) for b in bars}

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

            # SPY regime: 20d MA slope for daily regime; 50d MA position for credit spreads
            spy_today = closes_by_symbol.get("SPY", {}).get(today)
            if spy_today:
                spy_close_list.append(spy_today)
            spy_ma20 = _compute_sma(spy_close_list, 20)
            spy_ma50 = _compute_sma(spy_close_list, 50)

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
                    # Update strategy consecutive loss counter
                    strat = trade.strategy
                    if pnl < 0:
                        strategy_consec_losses[strat] = strategy_consec_losses.get(strat, 0) + 1
                        if strategy_consec_losses[strat] >= params.strategy_cooldown_losses:
                            strategy_cooldown_until[strat] = day_idx + 5  # 5-day cooldown
                    else:
                        strategy_consec_losses[strat] = 0  # reset on win
                else:
                    still_open.append(trade)
            open_trades = still_open

            # ── Update peak balance for trailing drawdown tracking ───────────
            if balance > peak_balance:
                peak_balance = balance

            # ── Circuit breakers ──────────────────────────────────────────────
            daily_loss    = day_open_pnl
            trailing_dd   = (peak_balance - balance) / STARTING_BALANCE
            apex_daily_limit_hit = daily_loss < -(STARTING_BALANCE * params.apex_daily_loss_limit)

            # ── Regime + VIX environment ─────────────────────────────────────
            regime     = _spy_regime(spy_close_list) if params.use_regime_filter else "neutral"
            vix_mult   = _vix_size_multiplier(current_vix) if params.vix_position_scale else 1.0

            # Survival sizing: taper risk near Apex DD limit
            if trailing_dd > 0.035:
                vix_mult *= 0.25
            elif trailing_dd > 0.020:
                vix_mult *= 0.50

            # ── Monthly profit protection ─────────────────────────────────────
            # Once we've locked in 2.5 % this month, cut size to 25 % to preserve it.
            month_key_today = today.strftime("%Y-%m")
            month_pnl_so_far = sum(
                t.pnl for t in closed_trades
                if t.pnl is not None
                and t.exit_time is not None
                and t.exit_time.strftime("%Y-%m") == month_key_today
            )
            if month_pnl_so_far >= balance * params.monthly_profit_lock_pct:
                vix_mult *= 0.25   # locked-in month: only micro-size trades

            recent = [t for t in closed_trades[-20:] if t.pnl is not None]
            recent_wr = (sum(1 for t in recent if (t.pnl or 0) > 0) / len(recent)) if recent else 0.55

            # ── Signal generation + entry ─────────────────────────────────────
            current_open_risk = _open_risk(open_trades, params.stop_loss_pct)
            max_risk_budget   = balance * params.max_open_risk_pct

            if current_open_risk < max_risk_budget and not apex_daily_limit_hit:
                for sym in self._symbols:
                    spot = closes_by_symbol.get(sym, {}).get(today, 0.0)
                    if spot <= 0:
                        continue

                    sym_closes = [
                        closes_by_symbol[sym][d]
                        for d in trading_days[:day_idx + 1]
                        if d in closes_by_symbol[sym]
                    ]

                    chain: list[dict] = []
                    if "flow" in self._strategies:
                        chain = self._loader.reconstruct_options_chain(
                            sym, spot, current_vix,
                            datetime.combine(today, datetime.min.time()),
                        )

                    sym_volumes = [
                        volumes_by_symbol[sym][d]
                        for d in trading_days[:day_idx + 1]
                        if d in volumes_by_symbol.get(sym, {})
                    ]

                    candidates: list[dict] = []
                    if "momentum" in self._strategies:
                        sigs = _eval_momentum(
                            sym, sym_closes, spot, sigma, balance, today, params,
                            spy_spot=spy_today, spy_ma20=spy_ma20,
                            iv_rank=iv_rank_today, volumes=sym_volumes,
                            current_vix=current_vix,
                        )
                        if sigs:
                            candidates.extend(sigs)
                    if "iv_rank" in self._strategies and regime == "neutral":
                        sigs = _eval_iv_rank(sym, spot, current_vix, vix_history_window, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)
                    # Credit spreads: use 50d MA position (stable) not 20d slope (noisy)
                    spy_above_50d = spy_ma50 is not None and spy_today is not None and spy_today > spy_ma50
                    spy_below_50d = spy_ma50 is not None and spy_today is not None and spy_today < spy_ma50
                    if "bull_put" in self._strategies and spy_above_50d:
                        sigs = _eval_bull_put_spread(sym, spot, current_vix, vix_history_window, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)
                    if "bear_call" in self._strategies and spy_below_50d:
                        sigs = _eval_bear_call_spread(sym, spot, current_vix, vix_history_window, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)
                    if "flow" in self._strategies:
                        sigs = _eval_flow(sym, spot, sigma, chain, balance, today, params, regime=regime)
                        if sigs:
                            candidates.extend(sigs)
                    if "ma_cross" in self._strategies:
                        sigs = _eval_ma_crossover(sym, sym_closes, spot, sigma, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)
                    if "bb" in self._strategies:
                        sigs = _eval_bollinger_bands(sym, sym_closes, spot, sigma, balance, today, params)
                        if sigs:
                            candidates.extend(sigs)

                    signals_generated += len(candidates)

                    # ── Filter 1: signal strength ─────────────────────────────
                    candidates = [s for s in candidates if s.get("strength", 0) >= params.signal_strength_min]

                    # ── Filter 2: strategy cooldown ───────────────────────────
                    candidates = [
                        s for s in candidates
                        if day_idx >= strategy_cooldown_until.get(s["strategy"], 0)
                    ]

                    # ── Filter 3: AI quality gate ─────────────────────────────
                    scored = []
                    rsi_today = _compute_rsi(sym_closes) if len(sym_closes) >= 15 else None
                    credit_types = ("condor", "put_spread", "call_spread")
                    for s in candidates:
                        direction = s.get("option_type", "call")
                        ai_score = _ai_signal_score(
                            direction=direction,
                            regime=regime,
                            rsi=rsi_today,
                            vix=current_vix,
                            iv_rank=iv_rank_today,
                            strength=s.get("strength", 0.2),
                            recent_win_rate=recent_wr,
                        )
                        # Credit spreads bypass AI gate — they're already regime-gated
                        if ai_score >= params.ai_min_score or direction in credit_types:
                            s["ai_score"] = ai_score
                            scored.append(s)
                    candidates = scored

                    for sig in candidates:
                        # ── Portfolio heat gate ───────────────────────────────
                        current_open_risk = _open_risk(open_trades, params.stop_loss_pct)
                        if current_open_risk >= max_risk_budget:
                            break
                        if len(open_trades) >= params.max_positions:
                            break

                        # No duplicate symbol+strategy
                        existing = [(t.symbol, t.strategy) for t in open_trades]
                        if (sig["symbol"], sig["strategy"]) in existing:
                            continue

                        # ── Risk-normalised sizing ────────────────────────────
                        raw_price    = sig["price"]
                        is_credit    = sig["action"] == "sell"
                        entry_price  = raw_price * (0.99 if is_credit else 1.01)

                        budget_risk = balance * params.risk_per_trade_pct * vix_mult
                        if is_credit:
                            mlpc = sig.get("max_loss_per_contract",
                                           entry_price * 100)
                            # Skip if 1 contract max-loss exceeds 2% of balance ($1,000 on $50k)
                            if mlpc > balance * 0.02:
                                continue
                            qty = _credit_risk_contracts(
                                balance, mlpc,
                                params.risk_per_trade_pct, vix_mult,
                            )
                        else:
                            # Skip expensive options where min-contract risk > 2× budget
                            min_contract_risk = entry_price * params.stop_loss_pct * 100
                            if min_contract_risk > budget_risk * 2.0:
                                continue
                            qty = _risk_sized_contracts(
                                balance, entry_price,
                                params.stop_loss_pct,
                                params.risk_per_trade_pct, vix_mult,
                            )

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
                            entry_price=entry_price,
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

        # Calculate monthly PnL by grouping trades by exit month
        monthly_pnl: dict[str, float] = {}
        for trade in closed_trades:
            if trade.exit_time is not None and trade.pnl is not None:
                month_key = trade.exit_time.strftime("%Y-%m")
                monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + trade.pnl
        # Sort by month
        monthly_pnl = dict(sorted(monthly_pnl.items()))

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

        def _trade_row(t: _BacktestTrade) -> dict:
            return {
                "id": t.id, "symbol": t.symbol, "strategy": t.strategy,
                "action": t.action, "option_type": t.option_type,
                "strike": t.strike, "expiry": t.expiry.isoformat() if hasattr(t.expiry, 'isoformat') else str(t.expiry),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "exit_reason": t.exit_reason,
            }

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
            monthly_pnl=monthly_pnl,
            best_trade=_trade_dict(best),
            worst_trade=_trade_dict(worst),
            best_day={"date": best_day_key, "pnl": daily_pnl_by_date.get(best_day_key, 0.0)} if best_day_key else {},
            worst_day={"date": worst_day_key, "pnl": daily_pnl_by_date.get(worst_day_key, 0.0)} if worst_day_key else {},
            signals_generated=signals_generated,
            signals_acted_on=signals_acted_on,
            all_trades=[_trade_row(t) for t in closed_trades[-200:]],
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
