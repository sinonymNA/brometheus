"""Black-Scholes options pricing engine for APEX CRUSHER.

Provides:
  - Black-Scholes European option pricing (call and put)
  - Full Greeks: delta, gamma, vega, theta, rho
  - Newton–Raphson implied-volatility solver
  - GreeksResult dataclass for structured output

Only ``numpy`` and ``scipy.stats`` are required beyond the standard library.
The module is fully standalone::

    python -m backend.core.greeks_engine   # runs self-tests
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Literal

from scipy.stats import norm

OptionType = Literal["call", "put"]

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class GreeksResult:
    """All pricing outputs for a single option evaluation.

    Attributes:
        option_type: ``"call"`` or ``"put"``.
        S: Underlying price at time of calculation.
        K: Strike price.
        T: Time to expiry in years.
        r: Annualised risk-free rate (continuous compounding).
        sigma: Annualised volatility used for this calculation.
        price: Theoretical option price (Black-Scholes fair value).
        delta: First-order sensitivity to underlying price (∂V/∂S).
        gamma: Second-order sensitivity to underlying price (∂²V/∂S²).
        vega: Sensitivity to a **1 percentage-point** rise in volatility.
        theta: Value decay per **calendar day** (typically negative).
        rho: Sensitivity to a 1-unit rise in the risk-free rate (∂V/∂r).
    """

    # Inputs
    option_type: OptionType
    S: float
    K: float
    T: float
    r: float
    sigma: float

    # Outputs
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float

    def __str__(self) -> str:
        return (
            f"GreeksResult({self.option_type.upper()}  "
            f"S={self.S}  K={self.K}  T={self.T:.4f}  "
            f"r={self.r:.4f}  σ={self.sigma:.4f})\n"
            f"  price  = {self.price:>10.4f}\n"
            f"  delta  = {self.delta:>10.4f}\n"
            f"  gamma  = {self.gamma:>10.6f}\n"
            f"  vega   = {self.vega:>10.4f}  (per 1% vol move)\n"
            f"  theta  = {self.theta:>10.4f}  (per calendar day)\n"
            f"  rho    = {self.rho:>10.4f}  (per unit r)"
        )


# ── Input validation ──────────────────────────────────────────────────────────


def _validate(S: float, K: float, T: float, r: float, sigma: float) -> None:
    """Raise ValueError listing every out-of-range input (fail-all, not fail-fast)."""
    errors: list[str] = []
    if S <= 0:
        errors.append(f"S must be > 0, got {S}")
    if K <= 0:
        errors.append(f"K must be > 0, got {K}")
    if T <= 0:
        errors.append(f"T must be > 0, got {T}")
    if sigma <= 0:
        errors.append(f"sigma must be > 0, got {sigma}")
    if errors:
        raise ValueError("Invalid Black-Scholes inputs:\n" + "\n".join(f"  {e}" for e in errors))


# ── Internal helpers ──────────────────────────────────────────────────────────


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Compute the d1 and d2 terms of the Black-Scholes formula.

    d1 = [ln(S/K) + (r + σ²/2)·T] / (σ·√T)
    d2 = d1 − σ·√T
    """
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def _price_from_d1_d2(
    S: float,
    K: float,
    T: float,
    r: float,
    d1: float,
    d2: float,
    option_type: OptionType,
) -> float:
    """Compute option price from pre-calculated d1 and d2."""
    discount = math.exp(-r * T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * discount * norm.cdf(d2)
    return K * discount * norm.cdf(-d2) - S * norm.cdf(-d1)


# ── Public API ────────────────────────────────────────────────────────────────


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: OptionType = "call",
) -> GreeksResult:
    """Price a European option and compute all first- and second-order Greeks.

    Assumes no dividends, continuous compounding, and European-style exercise
    (Black-Scholes 1973).

    Args:
        S: Current underlying price (e.g. ``100.0``).
        K: Strike price (e.g. ``105.0``).
        T: Time to expiry in **years** (e.g. ``0.25`` for three months).
        r: Annualised risk-free rate as a decimal (e.g. ``0.05`` for 5 %).
        sigma: Annualised implied volatility as a decimal (e.g. ``0.20``).
        option_type: ``"call"`` (default) or ``"put"``.

    Returns:
        :class:`GreeksResult` with all pricing and sensitivity fields populated.

    Raises:
        ValueError: If S, K, T, or sigma are non-positive.

    Reference values for S=100, K=100, T=0.25, r=0.05, σ=0.20:
        d1=0.1750, d2=0.0750  →  call≈4.61, put≈3.37, Δcall≈0.5695
    """
    _validate(S, K, T, r, sigma)

    sqrt_T = math.sqrt(T)
    discount = math.exp(-r * T)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    phi_d1: float = norm.pdf(d1)  # standard-normal PDF φ(d1)

    # ── Price ─────────────────────────────────────────────────────────────────
    price = _price_from_d1_d2(S, K, T, r, d1, d2, option_type)

    # ── Delta  ∂V/∂S ──────────────────────────────────────────────────────────
    # Call: N(d1)     Put: N(d1) − 1
    if option_type == "call":
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1.0

    # ── Gamma  ∂²V/∂S²  (identical for call and put) ─────────────────────────
    # Γ = φ(d1) / (S · σ · √T)
    gamma = phi_d1 / (S * sigma * sqrt_T)

    # ── Vega  ∂V/∂σ per 1 percentage-point (0.01) change in volatility ────────
    # Raw vega = S · φ(d1) · √T  is the change for a 1-unit (100 %) rise in σ.
    # Divide by 100 to express as sensitivity to a 1 % move (market convention).
    vega = S * phi_d1 * sqrt_T * 0.01

    # ── Theta  ∂V/∂T per calendar day ─────────────────────────────────────────
    # Annualised base term shared by call and put:
    #   −(S · φ(d1) · σ) / (2 · √T)
    # Call adds: −r · K · e^{−rT} · N(d2)
    # Put  adds: +r · K · e^{−rT} · N(−d2)
    # Divide the annualised value by 365 for a per-calendar-day figure.
    base_theta = -(S * phi_d1 * sigma) / (2.0 * sqrt_T)
    if option_type == "call":
        theta = (base_theta - r * K * discount * norm.cdf(d2)) / 365.0
    else:
        theta = (base_theta + r * K * discount * norm.cdf(-d2)) / 365.0

    # ── Rho  ∂V/∂r ────────────────────────────────────────────────────────────
    # Call: +K · T · e^{−rT} · N(d2)
    # Put:  −K · T · e^{−rT} · N(−d2)
    if option_type == "call":
        rho = K * T * discount * norm.cdf(d2)
    else:
        rho = -K * T * discount * norm.cdf(-d2)

    return GreeksResult(
        option_type=option_type,
        S=S,
        K=K,
        T=T,
        r=r,
        sigma=sigma,
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        rho=rho,
    )


def implied_volatility(
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
    """Solve for implied volatility using Newton–Raphson iteration.

    Finds σ* such that ``black_scholes(S, K, T, r, σ*, option_type).price``
    matches *market_price* within *tolerance*.

    The Newton–Raphson update is::

        σ_{n+1} = σ_n − (BS_price(σ_n) − market_price) / vega(σ_n)

    where vega is the raw ∂V/∂σ (not the per-1 % convention).

    Initial guess uses the Brenner–Subrahmanyam approximation::

        σ₀ ≈ market_price / (S · √(T / 2π))

    which is asymptotically exact for at-the-money options.

    Args:
        market_price: Observed mid-price of the option.
        S: Current underlying price.
        K: Strike price.
        T: Time to expiry in years.
        r: Annualised risk-free rate.
        option_type: ``"call"`` or ``"put"``.
        max_iterations: Hard iteration cap (default 100).
        tolerance: Convergence threshold on |BS_price − market_price|
            (default 1e-4 ≈ $0.0001).

    Returns:
        Implied volatility as a decimal (e.g. ``0.20``), or ``None`` when:

        - *market_price* is below the option's intrinsic value,
        - vega collapses to near-zero (very deep ITM/OTM),
        - the solver diverges or exhausts *max_iterations*.
    """
    # A price below intrinsic has no real volatility solution.
    discount = math.exp(-r * T)
    intrinsic = (
        max(0.0, S - K * discount)
        if option_type == "call"
        else max(0.0, K * discount - S)
    )
    if market_price < intrinsic - tolerance:
        return None

    # Brenner–Subrahmanyam initial guess; clamp to a sensible range.
    sigma = max(0.01, min(5.0, market_price / (S * math.sqrt(T / (2.0 * math.pi)))))

    sqrt_T = math.sqrt(T)

    for _ in range(max_iterations):
        try:
            d1, d2 = _d1_d2(S, K, T, r, sigma)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            return None

        price = _price_from_d1_d2(S, K, T, r, d1, d2, option_type)
        diff = price - market_price

        if abs(diff) < tolerance:
            return sigma

        # Raw vega = S · φ(d1) · √T  (the Newton denominator)
        vega_raw = S * norm.pdf(d1) * sqrt_T
        if abs(vega_raw) < 1e-10:
            return None  # vega ≈ 0 → update undefined; give up

        sigma -= diff / vega_raw

        if not (0.0 < sigma <= 10.0):
            return None  # diverged outside any reasonable vol range

    return None  # did not converge within max_iterations


# ── Self-tests ────────────────────────────────────────────────────────────────

def _check(label: str, got: float, expected: float, tol: float) -> None:
    """Assert *got* ≈ *expected* within *tol*; print result and exit on failure."""
    ok = abs(got - expected) <= tol
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}]  {label}: got {got:.8f}  expected {expected:.8f}  tol={tol}")
    if not ok:
        sys.exit(1)


def run_tests() -> None:
    """Self-test suite covering pricing, Greeks, parity, and the IV solver.

    Analytical reference (S=100, K=100, T=0.25, r=0.05, σ=0.20):
      d1 = 0.17500,  d2 = 0.07500
      N(d1) ≈ 0.56946,  N(d2) ≈ 0.52990
      e^{-rT} ≈ 0.98758
      call price ≈ 4.6106
      put  price ≈ 3.3739
    """
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20

    # ── 1. ATM call: price and delta ──────────────────────────────────────────
    print("[1] ATM call — price and delta")
    call = black_scholes(S, K, T, r, sigma, "call")
    print(call)
    _check("call price", call.price, 4.6106, tol=0.005)
    _check("call delta", call.delta, 0.56946, tol=0.0005)
    _check("call gamma", call.gamma, 0.039288, tol=0.0005)
    _check("call vega (per 1%)", call.vega, 0.19644, tol=0.0005)

    # ── 2. ATM put: price and delta ───────────────────────────────────────────
    print("\n[2] ATM put — price and delta")
    put = black_scholes(S, K, T, r, sigma, "put")
    print(put)
    _check("put price", put.price, 3.3739, tol=0.005)
    _check("put delta", put.delta, -0.43054, tol=0.0005)

    # ── 3. Put-call parity  C − P = S − K·e^{−rT} ────────────────────────────
    print("\n[3] Put-call parity")
    lhs = call.price - put.price
    rhs = S - K * math.exp(-r * T)
    _check("C − P == S − K·e^{−rT}", lhs, rhs, tol=1e-8)

    # ── 4. Gamma is identical for call and put ────────────────────────────────
    print("\n[4] Gamma symmetry (call == put)")
    _check("call.gamma == put.gamma", call.gamma, put.gamma, tol=1e-10)

    # ── 5. Vega is identical for call and put ─────────────────────────────────
    print("\n[5] Vega symmetry (call == put)")
    _check("call.vega == put.vega", call.vega, put.vega, tol=1e-10)

    # ── 6. IV solver round-trip (call and put) ────────────────────────────────
    print("\n[6] IV solver round-trip")
    for opt_type in ("call", "put"):
        ref = black_scholes(S, K, T, r, sigma, opt_type)  # type: ignore[arg-type]
        iv = implied_volatility(ref.price, S, K, T, r, opt_type)  # type: ignore[arg-type]
        assert iv is not None, f"IV solver returned None for {opt_type}"
        _check(f"{opt_type} IV", iv, sigma, tol=1e-4)

    # ── 7. IV solver: sub-intrinsic price returns None ────────────────────────
    print("\n[7] IV solver rejects sub-intrinsic price")
    deep_itm_intrinsic = S - K * math.exp(-r * T)  # deep ITM call intrinsic
    iv_none = implied_volatility(deep_itm_intrinsic - 1.0, S, K, T, r, "call")
    assert iv_none is None, f"Expected None for sub-intrinsic price, got {iv_none}"
    print("  [ok  ]  sub-intrinsic call → None")

    # ── 8. OTM call sanity checks ─────────────────────────────────────────────
    print("\n[8] OTM call (K=110) sanity")
    otm = black_scholes(100.0, 110.0, T, r, sigma, "call")
    print(otm)
    assert 0.0 < otm.price < call.price, "OTM call must be cheaper than ATM call"
    assert 0.0 < otm.delta < 0.5, "OTM call delta must be in (0, 0.5)"
    assert otm.theta < 0.0, "OTM call theta must be negative"
    print("  [ok  ]  OTM call price/delta/theta all in expected range")

    # ── 9. ValueError on invalid inputs ──────────────────────────────────────
    print("\n[9] Input validation")
    bad_cases: list[tuple[str, dict[str, float]]] = [
        ("S ≤ 0", dict(S=-1.0, K=100.0, T=0.25, r=0.05, sigma=0.2)),
        ("T = 0", dict(S=100.0, K=100.0, T=0.0, r=0.05, sigma=0.2)),
        ("sigma < 0", dict(S=100.0, K=100.0, T=0.25, r=0.05, sigma=-0.1)),
    ]
    for desc, kwargs in bad_cases:
        try:
            black_scholes(**kwargs)  # type: ignore[arg-type]
            print(f"  [FAIL]  expected ValueError for {desc}")
            sys.exit(1)
        except ValueError:
            print(f"  [ok  ]  ValueError raised for {desc}")

    print("\n✓  All tests passed.\n")


if __name__ == "__main__":
    run_tests()
