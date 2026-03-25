"""Black-Scholes pricing with configurable spread.

Adapted from backend/src/pricing/black_scholes.py — standalone, no backend imports.
"""

import logging
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


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes gamma (same for puts and calls)."""
    if sigma <= 0 or T <= 0 or S <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes vega (per 1.0 vol change, same for puts and calls)."""
    if sigma <= 0 or T <= 0 or S <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return S * norm.pdf(d1) * math.sqrt(T)


def bs_theta(
    is_put: bool, S: float, K: float, T: float, r: float, sigma: float
) -> float:
    """Black-Scholes theta (daily decay in USD)."""
    if sigma <= 0 or T <= 0 or S <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    term1 = -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
    if is_put:
        term2 = r * K * math.exp(-r * T) * norm.cdf(-d2)
        return (term1 + term2) / 365
    term2 = -r * K * math.exp(-r * T) * norm.cdf(d2)
    return (term1 + term2) / 365


IV_DIVERGENCE_WARN = 0.30
IV_MIN_VALID = 0.05
IV_MAX_VALID = 3.0


def validate_iv(iv: float, label: str = "") -> bool:
    """Check that IV is within a sane range. Returns True if valid."""
    _log = logging.getLogger(__name__)
    if iv <= 0:
        _log.warning("[IV CHECK] %s IV=0, skipping quotes", label)
        return False
    if iv < IV_MIN_VALID:
        _log.warning("[IV CHECK] %s IV=%.4f below min %.2f", label, iv, IV_MIN_VALID)
        return False
    if iv > IV_MAX_VALID:
        _log.warning("[IV CHECK] %s IV=%.4f above max %.2f", label, iv, IV_MAX_VALID)
        return False
    return True


def check_iv_divergence(
    iv: float, spot_history: list[float], label: str = ""
) -> float | None:
    """Compare implied vol against realized vol from spot history.

    Args:
        iv: Current implied volatility (annualized).
        spot_history: Recent spot prices (chronological order).
        label: Label for log messages.

    Returns:
        Realized vol if computed, None if insufficient data.
    """
    _log = logging.getLogger(__name__)
    if len(spot_history) < 2:
        return None

    returns = []
    for i in range(1, len(spot_history)):
        if spot_history[i - 1] > 0 and spot_history[i] > 0:
            returns.append(math.log(spot_history[i] / spot_history[i - 1]))

    if not returns:
        return None

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    realized_vol = math.sqrt(variance * 365)

    if realized_vol > 0:
        divergence = abs(iv - realized_vol) / realized_vol
        if divergence > IV_DIVERGENCE_WARN:
            _log.warning(
                "[IV CHECK] %s IV=%.4f vs realized=%.4f (%.0f%% divergence)",
                label,
                iv,
                realized_vol,
                divergence * 100,
            )

    return realized_vol


VOL_SKEW_SLOPE = 0.15
VOL_SKEW_PUT_BIAS = 0.05
VOL_SKEW_MIN_MULT = 0.8
VOL_SKEW_MAX_MULT = 1.5

SKEW_MAX_BPS = 200
GAMMA_NEAR_DAYS = 3
GAMMA_NEAR_BPS = 50
GAMMA_VERY_NEAR_DAYS = 1
GAMMA_VERY_NEAR_BPS = 100


def apply_vol_skew(
    sigma: float,
    S: float,
    K: float,
    is_put: bool,
) -> float:
    """Adjust IV by moneyness to approximate a volatility smile.

    OTM puts get higher IV (demand for downside protection).
    OTM calls get slightly higher IV (tail risk).
    ATM options are unchanged.

    Args:
        sigma: Base implied volatility.
        S: Spot price.
        K: Strike price.
        is_put: True for puts.

    Returns:
        Adjusted IV.
    """
    if S <= 0 or K <= 0 or sigma <= 0:
        return sigma

    moneyness = math.log(K / S)
    # moneyness < 0: OTM put / ITM call (strike below spot)
    # moneyness > 0: ITM put / OTM call (strike above spot)

    # Symmetric component: both tails get higher vol
    adjustment = VOL_SKEW_SLOPE * moneyness**2

    # Asymmetric component: OTM puts get extra vol (put skew)
    if is_put and moneyness < 0:
        adjustment += VOL_SKEW_PUT_BIAS * abs(moneyness)
    elif not is_put and moneyness > 0:
        adjustment += VOL_SKEW_PUT_BIAS * abs(moneyness) * 0.5

    multiplier = 1.0 + adjustment
    multiplier = max(VOL_SKEW_MIN_MULT, min(VOL_SKEW_MAX_MULT, multiplier))

    return sigma * multiplier


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
    # same_side: option type matches imbalance direction
    same_side = (is_put and inventory_imbalance > 0) or (
        not is_put and inventory_imbalance < 0
    )
    if inventory_imbalance != 0:
        magnitude = abs(inventory_imbalance) * SKEW_MAX_BPS
        spread += magnitude if same_side else -magnitude * 0.5

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
