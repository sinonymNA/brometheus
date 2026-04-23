"""Black-Scholes options pricing engine for APEX CRUSHER.

Standalone module — no imports from other project files.
Requires numpy and scipy only.

Public API::

    from backend.core.greeks_engine import calculate, calculate_iv, GreeksResult

    result = calculate(S=100, K=100, T=0.25, r=0.05, sigma=0.20)
    print(result.call_price, result.delta)

    iv = calculate_iv(market_price=4.61, S=100, K=100, T=0.25, r=0.05,
                      option_type="call")

Self-tests::

    python -m backend.core.greeks_engine
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from scipy.stats import norm

OptionType = Literal["call", "put"]

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class GreeksResult:
    """Complete Black-Scholes output for a single strike / expiry.

    Greeks follow the call-side convention unless noted:

    - ``delta``  call Δ ∈ (0, 1);  put Δ = delta − 1
    - ``gamma``  identical for call and put
    - ``vega``   identical for call and put; expressed per 1 % vol move
    - ``theta``  call Θ per calendar day (typically negative)
    - ``rho``    call ρ per 1 percentage-point change in r

    ``iv`` stores the volatility used for this calculation (input sigma, or
    a solved implied vol when the result is built from :func:`calculate_iv`).
    """

    # Prices
    call_price: float
    put_price: float

    # Greeks
    delta: float        # ∂C/∂S, call delta ∈ (0, 1)
    gamma: float        # ∂²V/∂S², same for call and put
    vega: float         # ∂V/∂σ per 1 % change in σ
    theta: float        # ∂C/∂t per calendar day
    rho: float          # ∂C/∂r per 1 % change in r

    # Volatility
    iv: float           # annualised vol used for this calculation

    # Contract metadata
    symbol: str         # underlying ticker, e.g. "SPY"
    strike: float       # strike price K
    expiry: str         # expiry label, e.g. "2026-06-20"
    calculated_at: datetime

    def __str__(self) -> str:
        return (
            f"GreeksResult  {self.symbol or '?'}  "
            f"K={self.strike}  exp={self.expiry or '?'}  σ={self.iv:.4f}\n"
            f"  call={self.call_price:.4f}   put={self.put_price:.4f}\n"
            f"  Δ={self.delta:.4f}   Γ={self.gamma:.6f}   "
            f"V={self.vega:.4f}   Θ={self.theta:.4f}/day   "
            f"ρ={self.rho:.4f}/1%r"
        )


# ── Private helpers ───────────────────────────────────────────────────────────


def _validate(S: float, K: float, T: float, sigma: float) -> None:
    """Raise ValueError listing every invalid input (checks all, not fail-fast)."""
    bad: list[str] = []
    if S <= 0:
        bad.append(f"S must be > 0 (got {S})")
    if K <= 0:
        bad.append(f"K must be > 0 (got {K})")
    if T <= 0:
        bad.append(f"T must be > 0 (got {T})")
    if sigma <= 0:
        bad.append(f"sigma must be > 0 (got {sigma})")
    if bad:
        raise ValueError("Invalid inputs:\n" + "\n".join(f"  {b}" for b in bad))


def _d1_d2(
    S: float, K: float, T: float, r: float, sigma: float
) -> tuple[float, float]:
    """Return (d1, d2) for the Black-Scholes formula.

    d1 = [ln(S/K) + (r + σ²/2)·T] / (σ·√T)
    d2 = d1 − σ·√T
    """
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


# ── Public API ────────────────────────────────────────────────────────────────


def calculate(
    S: float,
    K: float,
    T: float,
    r: float = 0.05,
    sigma: float = 0.20,
    symbol: str = "",
    expiry: str = "",
) -> GreeksResult:
    """Compute Black-Scholes prices and all five Greeks for a European option.

    Assumes no dividends, continuous compounding, and European-style exercise.

    Args:
        S: Current underlying price (e.g. ``100.0``).
        K: Strike price (e.g. ``105.0``).
        T: Time to expiry in **years** (e.g. ``0.25`` for three months).
        r: Annualised risk-free rate as a decimal. Default ``0.05``.
        sigma: Annualised implied volatility as a decimal. Default ``0.20``.
        symbol: Optional underlying ticker for labelling (e.g. ``"SPY"``).
        expiry: Optional expiry label (e.g. ``"2026-06-20"``).

    Returns:
        :class:`GreeksResult` with both call/put prices and all Greeks.

    Raises:
        ValueError: If S, K, T, or sigma are non-positive.

    Reference values (S=100, K=100, T=0.25, r=0.05, σ=0.20):
        call_price≈4.61, put_price≈3.37, delta≈0.57, gamma≈0.039
    """
    _validate(S, K, T, sigma)

    sqrt_T = math.sqrt(T)
    discount = math.exp(-r * T)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    phi_d1: float = norm.pdf(d1)  # φ(d1): standard-normal PDF

    # ── Prices ────────────────────────────────────────────────────────────────
    call_price = float(S * norm.cdf(d1) - K * discount * norm.cdf(d2))
    put_price = float(K * discount * norm.cdf(-d2) - S * norm.cdf(-d1))

    # ── Delta  ∂C/∂S ∈ (0,1);  put delta = delta − 1 ─────────────────────────
    delta = float(norm.cdf(d1))

    # ── Gamma  ∂²V/∂S²  (identical for call and put) ─────────────────────────
    gamma = float(phi_d1 / (S * sigma * sqrt_T))

    # ── Vega  per 1 percentage-point (0.01) change in σ ──────────────────────
    # Raw vega = S·φ(d1)·√T for a 1-unit (100%) change; divide by 100.
    vega = float(S * phi_d1 * sqrt_T * 0.01)

    # ── Theta  call, per calendar day ─────────────────────────────────────────
    # Annualised: −(S·φ(d1)·σ)/(2·√T) − r·K·e^{−rT}·N(d2)
    # Divide by 365 for daily decay.
    theta = float(
        (-(S * phi_d1 * sigma) / (2.0 * sqrt_T) - r * K * discount * norm.cdf(d2))
        / 365.0
    )

    # ── Rho  call, per 1 percentage-point (0.01) change in r ─────────────────
    # Raw rho = K·T·e^{−rT}·N(d2) for a 1-unit change; divide by 100.
    rho = float(K * T * discount * norm.cdf(d2) * 0.01)

    return GreeksResult(
        call_price=call_price,
        put_price=put_price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        rho=rho,
        iv=sigma,
        symbol=symbol,
        strike=K,
        expiry=expiry,
        calculated_at=datetime.now(timezone.utc),
    )


def calculate_iv(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionType = "call",
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-4,
) -> float | None:
    """Solve for implied volatility using Newton-Raphson iteration.

    Finds σ* such that the Black-Scholes price equals *market_price* within
    *tolerance*.  The Newton-Raphson update is::

        σ_{n+1} = σ_n − (BS_price(σ_n) − market_price) / vega(σ_n)

    The initial guess uses the Brenner-Subrahmanyam approximation::

        σ₀ ≈ market_price / (S · √(T / 2π))

    which is exact for at-the-money options and converges in 3-5 iterations
    for most strikes.

    Args:
        market_price: Observed mid-price of the option.
        S: Current underlying price.
        K: Strike price.
        T: Time to expiry in years.
        r: Annualised risk-free rate.
        option_type: ``"call"`` (default) or ``"put"``.
        max_iterations: Hard cap on Newton steps. Default 100.
        tolerance: Convergence threshold on ``|BS_price − market_price|``.
            Default ``1e-4`` (≈ $0.0001).

    Returns:
        Implied volatility as a decimal (e.g. ``0.20``), or ``None`` if:

        - *market_price* is below the option's intrinsic value,
        - vega collapses near zero (very deep ITM/OTM),
        - the solver diverges or exhausts *max_iterations*.
    """
    discount = math.exp(-r * T)

    # Reject prices below intrinsic — no real solution exists.
    if option_type == "call":
        intrinsic = max(0.0, S - K * discount)
    else:
        intrinsic = max(0.0, K * discount - S)
    if market_price < intrinsic - tolerance:
        return None

    # Brenner-Subrahmanyam initial guess, clamped to a sane range.
    sigma = max(0.01, min(5.0, market_price / (S * math.sqrt(T / (2.0 * math.pi)))))
    sqrt_T = math.sqrt(T)

    for _ in range(max_iterations):
        try:
            d1, d2 = _d1_d2(S, K, T, r, sigma)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            return None

        if option_type == "call":
            price = float(S * norm.cdf(d1) - K * discount * norm.cdf(d2))
        else:
            price = float(K * discount * norm.cdf(-d2) - S * norm.cdf(-d1))

        diff = price - market_price
        if abs(diff) < tolerance:
            return sigma

        # Raw vega = S·φ(d1)·√T (denominator for Newton step).
        vega_raw = float(S * norm.pdf(d1) * sqrt_T)
        if abs(vega_raw) < 1e-10:
            return None  # vega ≈ 0: can't update; give up

        sigma -= diff / vega_raw
        if not (0.0 < sigma <= 10.0):
            return None  # diverged out of any meaningful vol range

    return None  # did not converge


# ── Self-tests ────────────────────────────────────────────────────────────────


def _run_tests() -> None:
    """Three self-tests that print PASS or FAIL for each assertion.

    Correct Black-Scholes reference values for S=100, K=100, T=0.25,
    r=0.05, σ=0.20:
      d1=0.17500, d2=0.07500
      call_price≈4.6150, put_price≈3.3728
      delta≈0.5695, gamma≈0.0393

    Note: the specification states call≈5.08.  The mathematically correct
    Black-Scholes value for these exact inputs is ≈4.61.  The spec figure
    corresponds to σ≈0.225 (22.5% vol).  Tests assert the correct values.
    """
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
    failures = 0

    def check(label: str, got: float, expected: float, tol: float,
              spec_ref: str = "") -> None:
        nonlocal failures
        ok = abs(got - expected) <= tol
        status = "PASS" if ok else "FAIL"
        ref = f"  [spec ref {spec_ref}]" if spec_ref else ""
        print(f"  {status}  {label}: {got:.5f}{ref}")
        if not ok:
            print(f"        expected {expected} ± {tol}, diff={got - expected:+.6f}")
            failures += 1

    # ── Test 1: ATM call pricing and key Greeks ───────────────────────────────
    print("Test 1: ATM call pricing  S=100, K=100, T=0.25, r=0.05, σ=0.20")
    r1 = calculate(S, K, T, r, sigma, symbol="TEST", expiry="2026-07-25")
    print(r1)

    check("call_price", r1.call_price, 4.6150, tol=0.01, spec_ref="≈5.08")
    check("delta",      r1.delta,      0.5695, tol=0.01, spec_ref="≈0.56")
    check("gamma",      r1.gamma,      0.0393, tol=0.002, spec_ref="≈0.040")
    check("vega",       r1.vega,       0.1964, tol=0.005)
    check("theta",      r1.theta,     -0.0287, tol=0.005)
    check("rho",        r1.rho,        0.1308, tol=0.005)

    # ── Test 2: Put-call parity  C − P = S − K·e^{−rT} ───────────────────────
    print("\nTest 2: Put-call parity  C − P = S − K·e^{-rT}")
    parity_lhs = r1.call_price - r1.put_price
    parity_rhs = S - K * math.exp(-r * T)
    diff = abs(parity_lhs - parity_rhs)
    ok = diff < 0.01
    print(f"  {'PASS' if ok else 'FAIL'}  C={r1.call_price:.4f}  "
          f"P={r1.put_price:.4f}  C−P={parity_lhs:.6f}  "
          f"S−Ke^{{-rT}}={parity_rhs:.6f}  diff={diff:.8f}")
    if not ok:
        failures += 1

    # ── Test 3: IV solver round-trip ──────────────────────────────────────────
    print("\nTest 3: IV solver round-trip")
    for opt_type in ("call", "put"):
        mkt = r1.call_price if opt_type == "call" else r1.put_price
        iv = calculate_iv(mkt, S, K, T, r, opt_type)  # type: ignore[arg-type]
        if iv is None:
            print(f"  FAIL  {opt_type} IV: solver returned None")
            failures += 1
        else:
            check(f"{opt_type} IV", iv, sigma, tol=0.001)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if failures == 0:
        print("All tests PASSED.")
    else:
        print(f"{failures} test(s) FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    _run_tests()
