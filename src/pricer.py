"""Black-Scholes pricing with configurable spread.

Adapted from backend/src/pricing/black_scholes.py — standalone, no backend imports.
"""

import math

from scipy.stats import norm


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if sigma <= 0 or T <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))


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


def bs_delta(
    is_put: bool,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> float:
    """Black-Scholes delta.

    Returns:
        Delta in range [-1, 0] for puts, [0, 1] for calls.
    """
    if T <= 0:
        if is_put:
            return -1.0 if S < K else 0.0
        return 1.0 if S > K else 0.0

    d1 = _d1(S, K, T, r, sigma)
    if is_put:
        return norm.cdf(d1) - 1.0
    return norm.cdf(d1)


SKEW_MAX_BPS = 200
GAMMA_NEAR_DAYS = 3
GAMMA_NEAR_BPS = 50
GAMMA_VERY_NEAR_DAYS = 1
GAMMA_VERY_NEAR_BPS = 100


def calculate_spread(
    base_bps: int,
    is_put: bool,
    T: float,
    inventory_imbalance: float = 0.0,
    utilization: float = 0.0,
) -> int:
    """Dynamic spread adjusted for inventory, gamma risk, and utilization.

    Args:
        base_bps: Base spread in basis points.
        is_put: True for put options.
        T: Time to expiry in years.
        inventory_imbalance: -1 (all calls) to +1 (all puts).
        utilization: 0 to 1, fraction of capacity deployed.

    Returns:
        Adjusted spread in basis points (minimum 50).
    """
    spread = float(base_bps)

    # 1. Inventory skew — widen on overweight side, narrow on underweight
    if inventory_imbalance != 0:
        if is_put and inventory_imbalance > 0:
            spread += inventory_imbalance * SKEW_MAX_BPS
        elif not is_put and inventory_imbalance < 0:
            spread += abs(inventory_imbalance) * SKEW_MAX_BPS
        elif is_put and inventory_imbalance < 0:
            spread -= abs(inventory_imbalance) * SKEW_MAX_BPS * 0.5
        elif not is_put and inventory_imbalance > 0:
            spread -= inventory_imbalance * SKEW_MAX_BPS * 0.5

    # 2. Near-expiry gamma surcharge
    days = T * 365
    if days < GAMMA_VERY_NEAR_DAYS:
        spread += GAMMA_VERY_NEAR_BPS
    elif days < GAMMA_NEAR_DAYS:
        spread += GAMMA_NEAR_BPS

    # 3. Utilization surcharge (kicks in above 80%)
    if utilization > 0.8:
        spread += (utilization - 0.8) * 500

    return max(int(spread), 50)


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
