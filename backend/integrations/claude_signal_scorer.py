"""Claude-powered live signal scoring for APEX CRUSHER.

In live trading every candidate signal is sent to Claude for a confidence
score (0-10) before we commit capital.  The heuristic in engine.py mirrors
this logic for backtesting so results stay reproducible without API calls.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

SCORE_PROMPT = """\
You are a professional options trader reviewing a potential trade for an Apex \
prop-firm evaluation account ($50,000, $3,000 profit target, $2,500 max drawdown).

Signal context:
{context}

Rate this trade opportunity on a scale of 0.0–1.0 where:
- 1.0 = extremely high confidence, strong edge, aligned with all conditions
- 0.6 = acceptable, take it
- 0.4 = marginal, skip
- 0.0 = do not trade

Reply with ONLY a JSON object: {{"score": <float>, "reason": "<one sentence>"}}
"""


class ClaudeSignalScorer:
    """Score live trade signals using Claude before execution."""

    def __init__(self, api_key: str | None = None) -> None:
        if not _ANTHROPIC_AVAILABLE:
            logger.warning("anthropic package not installed — signal scorer disabled")
            self._client = None
            return
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def score(self, signal: dict[str, Any], market_ctx: dict[str, Any]) -> tuple[float, str]:
        """Return (score 0–1, reason string).  Falls back to heuristic if unavailable."""
        if self._client is None:
            return self._heuristic_score(signal, market_ctx)

        context = json.dumps({
            "strategy": signal.get("strategy"),
            "direction": signal.get("option_type"),
            "symbol": signal.get("symbol"),
            "signal_strength": signal.get("strength"),
            "signal_type": signal.get("signal_type"),
            "spy_regime": market_ctx.get("regime", "unknown"),
            "vix": market_ctx.get("vix"),
            "iv_rank": market_ctx.get("iv_rank"),
            "rsi": market_ctx.get("rsi"),
            "recent_win_rate_last20": market_ctx.get("recent_win_rate"),
            "open_positions": market_ctx.get("open_positions"),
            "daily_pnl_so_far": market_ctx.get("daily_pnl"),
            "trailing_drawdown_pct": market_ctx.get("trailing_dd"),
        }, indent=2)

        try:
            msg = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": SCORE_PROMPT.format(context=context)}],
            )
            raw = msg.content[0].text.strip()
            data = json.loads(raw)
            score = float(data.get("score", 0.5))
            reason = data.get("reason", "")
            logger.info("Claude scored %s %s: %.2f — %s",
                        signal.get("symbol"), signal.get("strategy"), score, reason)
            return max(0.0, min(score, 1.0)), reason
        except Exception as exc:
            logger.warning("Claude scoring failed (%s) — using heuristic", exc)
            return self._heuristic_score(signal, market_ctx)

    @staticmethod
    def _heuristic_score(signal: dict, ctx: dict) -> tuple[float, str]:
        """Mirror of _ai_signal_score() in engine.py — keeps live/backtest consistent."""
        score = 0.5
        direction = signal.get("option_type", "call")
        regime = ctx.get("regime", "neutral")
        vix = ctx.get("vix", 18.0)
        strength = signal.get("strength", 0.2)
        recent_wr = ctx.get("recent_win_rate", 0.55)

        if regime == "bull" and direction == "call":
            score += 0.25
        elif regime == "bear" and direction == "put":
            score += 0.25
        elif regime != "neutral" and direction != "condor":
            score -= 0.20

        if direction == "call" and vix < 18:
            score += 0.10
        elif direction == "call" and vix > 25:
            score -= 0.15

        score += strength * 0.15
        if recent_wr < 0.45:
            score -= 0.10
        elif recent_wr > 0.65:
            score += 0.05

        score = round(max(0.0, min(score, 1.0)), 3)
        return score, f"heuristic: regime={regime} vix={vix:.1f} strength={strength:.2f}"
