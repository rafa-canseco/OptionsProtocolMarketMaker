"""Black-Scholes pricing with configurable spread.

Adapted from backend/src/pricing/black_scholes.py — standalone, no backend imports.
"""

import math

from scipy.stats import norm


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (
        (math.log(S / K) + (r + 0.5 * sigma**2) * T)
        / (sigma * math.sqrt(T))
    )


def _d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return _d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def bs_price(
    is_put: bool,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> float:
    """Black-Scholes option price.

    Args:
        is_put: True for put, False for call.
        S: Spot price.
        K: Strike price.
        T: Time to expiry in years (e.g. 7/365).
        r: Risk-free rate (annualized).
        sigma: Implied volatility (annualized).

    Returns:
        Option premium in USD.
    """
    if T <= 0:
        if is_put:
            return max(K - S, 0.0)
        return max(S - K, 0.0)

    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)

    if is_put:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def price_with_spread(
    is_put: bool,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    spread_bps: int,
) -> float:
    """BS price minus spread. The MM bids below theoretical to capture edge.

    Returns:
        Bid price in USD (floored at a tiny positive value).
    """
    theo = bs_price(is_put, S, K, T, r, sigma)
    bid = theo * (1 - spread_bps / 10_000)
    return max(bid, 1e-6)
