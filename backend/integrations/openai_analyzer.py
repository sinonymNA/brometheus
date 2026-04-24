"""OpenAI-powered backtest analysis for THE LAB."""
from __future__ import annotations
import json
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

SYSTEM_PROMPT = """You are an expert algorithmic options trading analyst for APEX CRUSHER, an automated options trading bot. You analyze backtest results and help optimize parameters.

The bot trades SPY, QQQ, AAPL and other symbols using three strategies:
- Momentum: RSI + volume surge + price breakout + low IV environment
- IV Rank: Sells premium (iron condors) when implied volatility is elevated
- Flow: Follows unusual options order flow

Be concise, specific, and actionable. Use numbers from the data provided. Do not be vague."""

def _fmt_run(run: dict) -> str:
    r = run.get("result", {})
    p = run.get("params", {})
    return (
        f"[{run.get('start_date','?')}→{run.get('end_date','?')}] "
        f"WR={r.get('win_rate',0):.1%} Ret={r.get('total_return_pct',0):.1%} "
        f"PF={r.get('profit_factor',0):.2f} DD={r.get('max_drawdown_pct',0):.1%} "
        f"Sharpe={r.get('sharpe_ratio',0):.2f} Trades={r.get('total_trades',0)} | "
        f"RSI={p.get('rsi_bull_threshold','?')}/{p.get('rsi_bear_threshold','?')} "
        f"SL={p.get('stop_loss_pct',0):.0%} PT={p.get('profit_target_pct',0):.0%} "
        f"Str={p.get('signal_strength_min','?')}"
    )

def _build_context(current_run: dict | None, prev_runs: list[dict]) -> str:
    parts = ["=== APEX CRUSHER BACKTEST HISTORY ==="]
    if current_run:
        r = current_run.get("result", {})
        p = current_run.get("params", {})
        parts.append(f"\nCURRENT BACKTEST ({current_run.get('start_date')} to {current_run.get('end_date')}):")
        parts.append(f"  Results: WR={r.get('win_rate',0):.1%}, Return={r.get('total_return_pct',0):.1%}, PF={r.get('profit_factor',0):.2f}, MaxDD={r.get('max_drawdown_pct',0):.1%}, Sharpe={r.get('sharpe_ratio',0):.2f}, Trades={r.get('total_trades',0)}")
        parts.append(f"  Strategies: {r.get('trades_by_strategy',{})}, PnL by strategy: {r.get('pnl_by_strategy',{})}")
        parts.append(f"  Parameters: {json.dumps(p)}")
        parts.append(f"  Symbols: {current_run.get('symbols',[])}")
    if prev_runs:
        parts.append(f"\nPREVIOUS {min(5,len(prev_runs))} RUNS (oldest→newest):")
        for run in prev_runs[-5:]:
            parts.append(f"  {_fmt_run(run)}")
    return "\n".join(parts)


class AIAnalyzer:
    def __init__(self, api_key: str) -> None:
        if not _OPENAI_AVAILABLE:
            raise RuntimeError("openai package not installed — pip install openai")
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = "gpt-4o-mini"

    async def analyze_backtest(self, current_run: dict, prev_runs: list[dict]) -> str:
        context = _build_context(current_run, prev_runs)
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{context}\n\nAnalyze this backtest. What worked, what didn't, and what specific parameters should change next? Keep it under 200 words and be specific with numbers."},
                ],
                max_tokens=350, temperature=0.7,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("analyze_backtest failed: %s", exc)
            return f"(AI unavailable: {exc})"

    async def stream_answer(
        self, question: str, lab_runs: list[dict], current_params: dict, market_regime: str
    ) -> AsyncIterator[str]:
        context = _build_context(lab_runs[-1] if lab_runs else None, lab_runs[:-1] if len(lab_runs) > 1 else [])
        user_msg = (
            f"{context}\n\n"
            f"Current parameters under consideration: {json.dumps(current_params)}\n"
            f"Current market regime: {market_regime}\n\n"
            f"User question: {question}"
        )
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=600, temperature=0.7, stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as exc:
            logger.warning("stream_answer failed: %s", exc)
            yield f"(AI unavailable: {exc})"

    async def get_recommendations(self, lab_runs: list[dict], market_regime: str) -> dict:
        if len(lab_runs) < 2:
            return {"error": "Need at least 2 completed backtests for recommendations."}
        context = _build_context(lab_runs[-1], lab_runs[:-1])
        prompt = (
            f"{context}\n\nMarket regime: {market_regime}\n\n"
            "Based on these backtests, recommend the next parameter set to test. "
            "Return JSON only:\n"
            '{"reasoning":"2-3 sentence explanation","parameters":{'
            '"rsi_bull_threshold":62,"rsi_bear_threshold":38,'
            '"volume_ratio_min":1.5,"iv_rank_max":60,"iv_rank_min":70,'
            '"signal_strength_min":0.55,"stop_loss_pct":0.08,"profit_target_pct":0.30,'
            '"min_dte":5,"max_dte":45,"max_positions":3,"position_size_pct":0.02'
            '},"confidence":75,"expected_improvement":"description"}'
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                max_tokens=500, temperature=0.5, response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:
            logger.warning("get_recommendations failed: %s", exc)
            return {"error": str(exc)}

    async def evaluate_parameters(self, parameters: dict, lab_runs: list[dict]) -> dict:
        if not lab_runs:
            return {"error": "No historical runs to base prediction on."}
        runs_summary = "\n".join(_fmt_run(r) for r in lab_runs[-10:])
        prompt = (
            f"Historical runs:\n{runs_summary}\n\n"
            f"New parameters: {json.dumps(parameters)}\n\n"
            "Predict performance for these parameters based on historical patterns. "
            "Return JSON only:\n"
            '{"win_rate":0.57,"total_return_pct":0.12,"profit_factor":1.40,'
            '"max_drawdown_pct":0.08,"sharpe_ratio":1.1,"confidence":70,'
            '"percentile":65,"reasoning":"1-2 sentences"}'
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                max_tokens=300, temperature=0.3, response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:
            logger.warning("evaluate_parameters failed: %s", exc)
            return {"error": str(exc)}
