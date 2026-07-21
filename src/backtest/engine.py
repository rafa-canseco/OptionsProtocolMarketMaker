from __future__ import annotations

import math
import statistics
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import numpy as np
from scipy.stats import norm

from src.backtest.config import BacktestSettings
from src.backtest.data import MarketSeries, utc_timestamp_ms
from src.backtest.models import (
    AssignmentLot,
    Ledger,
    OptionPosition,
    StrategyConfig,
)
from src.backtest.probe import decision_times
from src.pricer import (
    apply_vol_skew,
    bs_delta,
    bs_price,
    calculate_spread,
    price_with_spread,
    VOL_SKEW_MAX_MULT,
    VOL_SKEW_MIN_MULT,
    VOL_SKEW_PUT_BIAS,
    VOL_SKEW_SLOPE,
)


def _strike_grid(spot: float, increment: float) -> list[float]:
    minimum = math.floor(spot * 0.5 / increment) * increment
    maximum = math.ceil(spot * 1.75 / increment) * increment
    return [
        minimum + index * increment
        for index in range(int(round((maximum - minimum) / increment)) + 1)
    ]


def select_strike(
    *,
    is_put: bool,
    spot: float,
    iv: float,
    time_years: float,
    target_delta: float,
    risk_free_rate: float,
    strike_increment: float,
    minimum_call_strike: float | None = None,
) -> float | None:
    if not 0 < target_delta < 1 or iv <= 0 or time_years <= 0:
        return None
    target_d1 = norm.ppf(1 - target_delta if is_put else target_delta)
    raw_strike = spot * math.exp(
        (risk_free_rate + 0.5 * iv**2) * time_years
        - target_d1 * iv * math.sqrt(time_years)
    )
    center = round(raw_strike / strike_increment) * strike_increment
    candidates = {center + offset * strike_increment for offset in range(-3, 4)}
    full_grid = _strike_grid(spot, strike_increment)
    grid_min, grid_max = full_grid[0], full_grid[-1]
    if minimum_call_strike is not None:
        first_above_floor = (
            math.floor(minimum_call_strike / strike_increment) + 1
        ) * strike_increment
        candidates.add(first_above_floor)
    candidates = sorted(
        strike for strike in candidates if grid_min <= strike <= grid_max
    )
    if is_put:
        candidates = [strike for strike in candidates if strike <= spot]
    else:
        floor = max(spot, minimum_call_strike or 0.0)
        candidates = [strike for strike in candidates if strike > floor]
    if not candidates:
        return None
    eligible = []
    for strike in candidates:
        delta = bs_delta(is_put, spot, strike, time_years, risk_free_rate, iv)
        if abs(delta) <= 0.90:
            eligible.append((abs(abs(delta) - target_delta), strike))
    if not eligible:
        return None
    return min(eligible)[1]


def binary_bid_premium(
    *,
    is_put: bool,
    spot: float,
    strike: float,
    time_years: float,
    iv: float,
    risk_free_rate: float,
    base_spread_bps: int,
    utilization: float,
) -> tuple[float, int, float]:
    """Replay the production Binary quote algorithm in USD per ETH."""
    spread_bps = calculate_spread(
        base_bps=base_spread_bps,
        is_put=is_put,
        T=time_years,
        inventory_imbalance=0.0,
        utilization=utilization,
    )
    skewed_iv = apply_vol_skew(iv, spot, strike, is_put)
    premium = price_with_spread(
        is_put=is_put,
        S=spot,
        K=strike,
        T=time_years,
        r=risk_free_rate,
        sigma=skewed_iv,
        spread_bps=spread_bps,
    )
    return premium, spread_bps, skewed_iv


def _weighted_basis(lots: list[AssignmentLot], attribute: str) -> float:
    amount = sum(lot.eth_amount for lot in lots)
    if amount <= 0:
        return 0.0
    return sum(getattr(lot, attribute) * lot.eth_amount for lot in lots) / amount


def protected_call_floor(
    lot: AssignmentLot,
    lots: list[AssignmentLot],
    protection_mode: str,
    margin_usd: float,
) -> float:
    """Return a strict floor that can never relax the lot's gross basis."""
    aggregate_floor = lot.gross_basis
    if protection_mode == "lot_gross_plus_weighted_gross":
        aggregate_floor = _weighted_basis(lots, "gross_basis")
    elif protection_mode == "lot_gross_plus_weighted_net":
        aggregate_floor = _weighted_basis(lots, "net_basis")
    elif protection_mode != "lot_gross":
        raise ValueError(f"Unknown protection mode: {protection_mode}")
    return max(lot.gross_basis, aggregate_floor) + margin_usd


def select_protected_call_strike(floor: float, strike_increment: float) -> float:
    """Select the nearest listed strike strictly above the protected floor."""
    return (math.floor(floor / strike_increment) + 1) * strike_increment


def _execution_cost(notional: float, config: StrategyConfig) -> float:
    return notional * config.costs.fee_bps_notional / 10_000 + config.costs.gas_usdc


def _net_premium(gross: float, config: StrategyConfig) -> float:
    return gross * (1 - config.costs.execution_slippage_bps / 10_000)


def _market_at(
    series: MarketSeries,
    timestamp_ms: int,
    settings: BacktestSettings,
) -> tuple[float, float] | None:
    gate = settings.coverage_gate
    spot = series.spot_at(timestamp_ms, gate.maximum_spot_age_hours)
    iv = series.iv_at(timestamp_ms, gate.maximum_iv_age_hours)
    if spot is None or iv is None:
        return None
    return spot.value, iv.value


def _fair_option_liability(
    position: OptionPosition,
    timestamp_ms: int,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    seconds = max((position.expiry * 1000 - timestamp_ms) / 1000, 0.0)
    time_years = seconds / (365 * 86_400)
    skewed_iv = apply_vol_skew(iv, spot, position.strike, position.is_put)
    return (
        bs_price(
            position.is_put,
            spot,
            position.strike,
            time_years,
            risk_free_rate,
            skewed_iv,
        )
        * position.amount_eth
    )


def _nav(
    ledger: Ledger,
    positions: list[OptionPosition],
    timestamp_ms: int,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    liabilities = sum(
        _fair_option_liability(position, timestamp_ms, spot, iv, risk_free_rate)
        for position in positions
    )
    return ledger.cash_usdc + ledger.eth_amount * spot - liabilities


def _vectorized_option_liability(
    position: OptionPosition,
    timestamps_ms: np.ndarray,
    spots: np.ndarray,
    ivs: np.ndarray,
    risk_free_rate: float,
) -> np.ndarray:
    time_years = np.maximum(
        (position.expiry * 1000 - timestamps_ms) / 1000 / (365 * 86_400),
        0.0,
    )
    moneyness = np.log(position.strike / spots)
    adjustment = VOL_SKEW_SLOPE * moneyness**2
    if position.is_put:
        adjustment += np.where(
            moneyness < 0,
            VOL_SKEW_PUT_BIAS * np.abs(moneyness),
            0.0,
        )
    else:
        adjustment += np.where(
            moneyness > 0,
            VOL_SKEW_PUT_BIAS * np.abs(moneyness) * 0.5,
            0.0,
        )
    multiplier = np.clip(
        1.0 + adjustment,
        VOL_SKEW_MIN_MULT,
        VOL_SKEW_MAX_MULT,
    )
    sigma = ivs * multiplier
    sqrt_time = np.sqrt(time_years)
    d1 = np.zeros_like(time_years)
    valid = (time_years > 0) & (sigma > 0)
    d1[valid] = (
        np.log(spots[valid] / position.strike)
        + (risk_free_rate + 0.5 * sigma[valid] ** 2) * time_years[valid]
    ) / (sigma[valid] * sqrt_time[valid])
    d2 = d1 - sigma * sqrt_time
    discounted_strike = position.strike * np.exp(-risk_free_rate * time_years)
    if position.is_put:
        prices = discounted_strike * norm.cdf(-d2) - spots * norm.cdf(-d1)
        prices = np.where(
            time_years <= 0, np.maximum(position.strike - spots, 0), prices
        )
    else:
        prices = spots * norm.cdf(d1) - discounted_strike * norm.cdf(d2)
        prices = np.where(
            time_years <= 0, np.maximum(spots - position.strike, 0), prices
        )
    return prices * position.amount_eth


def _record_nav_marks(
    *,
    series: MarketSeries,
    settings: BacktestSettings,
    ledger: Ledger,
    positions: list[OptionPosition],
    start: datetime,
    end: datetime,
    nav_marks: list[tuple[int, float]],
) -> None:
    timestamp = start
    timestamps_ms = []
    spots = []
    ivs = []
    while timestamp < end:
        timestamp_ms = utc_timestamp_ms(timestamp)
        spot = series.spot_at(
            timestamp_ms, settings.coverage_gate.maximum_spot_age_hours
        )
        iv = series.iv_at(timestamp_ms, settings.coverage_gate.maximum_iv_age_hours)
        if spot is not None and (iv is not None or not positions):
            timestamps_ms.append(timestamp_ms)
            spots.append(spot.value)
            ivs.append(iv.value if iv is not None else 0.0)
        timestamp += timedelta(hours=1)
    if not timestamps_ms:
        return
    timestamp_values = np.asarray(timestamps_ms, dtype=np.float64)
    spot_values = np.asarray(spots)
    iv_values = np.asarray(ivs)
    values = ledger.cash_usdc + ledger.eth_amount * spot_values
    for position in positions:
        values -= _vectorized_option_liability(
            position,
            timestamp_values,
            spot_values,
            iv_values,
            settings.risk_free_rate,
        )
    nav_marks.extend(zip(timestamps_ms, values.tolist(), strict=True))


def _record_interval_accounting(
    ledger: Ledger,
    csp: OptionPosition | None,
    covered_lots: set[int],
    hours: float,
) -> None:
    eth_amount = ledger.eth_amount
    if eth_amount > 0:
        ledger.eth_exposure_hours += hours
        ledger.eth_amount_hours += eth_amount * hours
        idle_amount = sum(
            lot.eth_amount for lot in ledger.lots if lot.lot_id not in covered_lots
        )
        if idle_amount > 0:
            ledger.eth_idle_hours += hours
            ledger.eth_idle_amount_hours += idle_amount * hours

    deployed = csp.strike * csp.amount_eth if csp is not None else 0.0
    idle_cash = max(ledger.cash_usdc - deployed, 0.0)
    ledger.idle_usdc_hours += idle_cash * hours
    ledger.total_usdc_hours += max(ledger.cash_usdc, 0.0) * hours


def _record_premium(
    ledger: Ledger,
    gross: float,
    net_before_cost: float,
    cost: float,
) -> None:
    ledger.premium_gross_usdc += gross
    ledger.premium_net_usdc += net_before_cost - cost
    ledger.premium_modeled_usdc += gross
    ledger.estimated_costs_usdc += (gross - net_before_cost) + cost
    ledger.cash_usdc += net_before_cost - cost


def _open_csp(
    *,
    ledger: Ledger,
    config: StrategyConfig,
    settings: BacktestSettings,
    spot: float,
    iv: float,
    opened_at: int,
    expiry: int,
    position_id: int,
    strike_increment: float,
) -> OptionPosition | None:
    time_years = (expiry - opened_at) / (365 * 86_400)
    strike = select_strike(
        is_put=True,
        spot=spot,
        iv=iv,
        time_years=time_years,
        target_delta=config.target_delta,
        risk_free_rate=settings.risk_free_rate,
        strike_increment=strike_increment,
    )
    if strike is None:
        return None
    deploy_budget = ledger.cash_usdc * config.utilization
    fee_rate = config.costs.fee_bps_notional / 10_000
    collateral = max(deploy_budget - config.costs.gas_usdc, 0.0) / (1 + fee_rate)
    if collateral <= 0:
        return None
    amount = collateral / strike
    premium_per_eth, _, _ = binary_bid_premium(
        is_put=True,
        spot=spot,
        strike=strike,
        time_years=time_years,
        iv=iv,
        risk_free_rate=settings.risk_free_rate,
        base_spread_bps=config.costs.base_spread_bps,
        utilization=config.utilization,
    )
    gross = premium_per_eth * amount
    premium_bps = gross / collateral * 10_000
    if premium_bps < config.minimum_premium_bps:
        ledger.skipped_minimum_premium += 1
        return None
    net_before_cost = _net_premium(gross, config)
    cost = _execution_cost(collateral, config)
    _record_premium(ledger, gross, net_before_cost, cost)
    ledger.turnover_usdc += collateral
    ledger.csp_opened += 1
    return OptionPosition(
        position_id=position_id,
        is_put=True,
        strike=strike,
        amount_eth=amount,
        opened_at=opened_at,
        expiry=expiry,
        premium_gross_usdc=gross,
        premium_net_usdc=net_before_cost - cost,
    )


def _open_calls(
    *,
    ledger: Ledger,
    config: StrategyConfig,
    settings: BacktestSettings,
    spot: float,
    iv: float,
    opened_at: int,
    expiry: int,
    first_position_id: int,
    strike_increment: float,
) -> tuple[list[OptionPosition], set[int]]:
    positions = []
    covered_lots: set[int] = set()
    time_years = (expiry - opened_at) / (365 * 86_400)
    weighted_gross = _weighted_basis(ledger.lots, "gross_basis")
    for offset, lot in enumerate(list(ledger.lots)):
        floor = protected_call_floor(
            lot, ledger.lots, config.protection_mode, config.call_margin_usd
        )
        if (
            weighted_gross + config.call_margin_usd
            < lot.gross_basis + config.call_margin_usd
        ):
            ledger.lot_floor_breach_opportunities += 1
        strike = select_protected_call_strike(floor, strike_increment)
        premium_per_eth, _, _ = binary_bid_premium(
            is_put=False,
            spot=spot,
            strike=strike,
            time_years=time_years,
            iv=iv,
            risk_free_rate=settings.risk_free_rate,
            base_spread_bps=config.costs.base_spread_bps,
            utilization=config.utilization,
        )
        gross = premium_per_eth * lot.eth_amount
        notional = strike * lot.eth_amount
        if gross / notional * 10_000 < config.minimum_premium_bps:
            ledger.skipped_minimum_premium += 1
            continue
        net_before_cost = _net_premium(gross, config)
        cost = _execution_cost(notional, config)
        if net_before_cost <= cost:
            ledger.skipped_minimum_premium += 1
            continue
        _record_premium(ledger, gross, net_before_cost, cost)
        ledger.turnover_usdc += notional
        ledger.calls_opened += 1
        covered_lots.add(lot.lot_id)
        positions.append(
            OptionPosition(
                position_id=first_position_id + offset,
                is_put=False,
                strike=strike,
                amount_eth=lot.eth_amount,
                opened_at=opened_at,
                expiry=expiry,
                premium_gross_usdc=gross,
                premium_net_usdc=net_before_cost - cost,
                lot_id=lot.lot_id,
            )
        )
    return positions, covered_lots


def _aggregate_long_option_delta(
    positions: list[OptionPosition],
    timestamp_ms: int,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    delta = 0.0
    for position in positions:
        seconds = max((position.expiry * 1000 - timestamp_ms) / 1000, 1.0)
        time_years = seconds / (365 * 86_400)
        skewed_iv = apply_vol_skew(iv, spot, position.strike, position.is_put)
        delta += (
            bs_delta(
                position.is_put,
                spot,
                position.strike,
                time_years,
                risk_free_rate,
                skewed_iv,
            )
            * position.amount_eth
        )
    return delta


def _record_mm_counterparty_economics(
    *,
    ledger: Ledger,
    positions: list[OptionPosition],
    opened_at_ms: int,
    opening_spot: float,
    opening_iv: float,
    midpoint: tuple[int, float, float] | None,
    settlement_spot: float,
    risk_free_rate: float,
    hedge_cost_bps: float,
) -> None:
    """Model Binary MM as the long-option counterparty with delta hedging.

    The option transfer reconciles exactly: vault option PnL plus unhedged MM PnL
    is zero. Hedge PnL and hedge execution costs are external market effects.
    """
    if not positions:
        return
    premium_paid = sum(position.premium_net_usdc for position in positions)
    payoff = sum(
        (
            max(position.strike - settlement_spot, 0.0)
            if position.is_put
            else max(settlement_spot - position.strike, 0.0)
        )
        * position.amount_eth
        for position in positions
    )
    long_delta = _aggregate_long_option_delta(
        positions,
        opened_at_ms,
        opening_spot,
        opening_iv,
        risk_free_rate,
    )
    hedge_units = -long_delta
    hedge_turnover = abs(hedge_units) * opening_spot
    hedge_pnl = 0.0
    previous_spot = opening_spot
    if midpoint is not None:
        midpoint_ms, midpoint_spot, midpoint_iv = midpoint
        hedge_pnl += hedge_units * (midpoint_spot - previous_spot)
        next_delta = _aggregate_long_option_delta(
            positions,
            midpoint_ms,
            midpoint_spot,
            midpoint_iv,
            risk_free_rate,
        )
        next_hedge_units = -next_delta
        hedge_turnover += abs(next_hedge_units - hedge_units) * midpoint_spot
        hedge_units = next_hedge_units
        previous_spot = midpoint_spot
    hedge_pnl += hedge_units * (settlement_spot - previous_spot)
    hedge_turnover += abs(hedge_units) * settlement_spot
    hedge_cost = hedge_turnover * hedge_cost_bps / 10_000
    ledger.mm_premium_paid_usdc += premium_paid
    ledger.mm_option_payoff_usdc += payoff
    ledger.mm_hedge_pnl_usdc += hedge_pnl
    ledger.mm_hedge_turnover_usdc += hedge_turnover
    ledger.mm_hedge_cost_usdc += hedge_cost


def _settle_positions(
    ledger: Ledger,
    positions: list[OptionPosition],
    settlement_spot: float,
    expiry: int,
    csp_only: bool,
) -> None:
    for position in positions:
        if position.is_put:
            ledger.csp_settled += 1
            if settlement_spot < position.strike:
                ledger.assignments += 1
                if csp_only:
                    intrinsic = (
                        position.strike - settlement_spot
                    ) * position.amount_eth
                    ledger.cash_usdc -= intrinsic
                else:
                    acquisition = position.strike * position.amount_eth
                    ledger.cash_usdc -= acquisition
                    net_basis = position.strike - (
                        position.premium_net_usdc / position.amount_eth
                    )
                    ledger.lots.append(
                        AssignmentLot(
                            lot_id=position.position_id,
                            eth_amount=position.amount_eth,
                            gross_basis=position.strike,
                            net_basis=net_basis,
                            assigned_at=expiry,
                            source_put_strike=position.strike,
                            source_put_premium_usdc=position.premium_net_usdc,
                        )
                    )
        else:
            if settlement_spot > position.strike:
                lot = next(
                    (item for item in ledger.lots if item.lot_id == position.lot_id),
                    None,
                )
                if lot is None:
                    raise AssertionError("covered call lost its assignment lot")
                if position.strike <= lot.gross_basis:
                    raise AssertionError(
                        "covered call materialized a gross assignment loss"
                    )
                ledger.cash_usdc += position.strike * lot.eth_amount
                ledger.realized_low_high_pnl_usdc += (
                    position.strike - lot.gross_basis
                ) * lot.eth_amount
                ledger.turnover_usdc += position.strike * lot.eth_amount
                ledger.calls_called += 1
                ledger.complete_cycles += 1
                ledger.lots.remove(lot)


def _drawdown(values: list[float]) -> float:
    peak = -math.inf
    maximum = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            maximum = min(maximum, value / peak - 1)
    return maximum


def _metrics(
    *,
    ledger: Ledger,
    nav_marks: list[tuple[int, float]],
    final_spot: float,
    initial_usdc: float,
    window_days: int,
    config: StrategyConfig,
    strategy: str,
    asset: str,
    window_start: datetime,
    window_end: datetime,
) -> dict:
    final_nav = ledger.cash_usdc + ledger.eth_amount * final_spot
    daily_closes: dict[object, float] = {}
    for timestamp_ms, value in nav_marks:
        day = datetime.fromtimestamp(timestamp_ms / 1000, UTC).date()
        daily_closes[day] = value
    daily_values = list(daily_closes.values())
    daily_returns = [
        daily_values[index] / daily_values[index - 1] - 1
        for index in range(1, len(daily_values))
        if daily_values[index - 1] > 0
    ]
    weighted_gross = _weighted_basis(ledger.lots, "gross_basis")
    unrealized = sum(
        (final_spot - lot.gross_basis) * lot.eth_amount for lot in ledger.lots
    )
    modeled_fraction = (
        ledger.premium_modeled_usdc / ledger.premium_gross_usdc
        if ledger.premium_gross_usdc
        else 0.0
    )
    mm_unhedged = ledger.mm_option_payoff_usdc - ledger.mm_premium_paid_usdc
    mm_hedged = mm_unhedged + ledger.mm_hedge_pnl_usdc - ledger.mm_hedge_cost_usdc
    result = {
        "asset": asset,
        "strategy": strategy,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "initial_usdc": initial_usdc,
        "final_nav_usdc": final_nav,
        "absolute_return": final_nav / initial_usdc - 1,
        "total_pnl_usdc": final_nav - initial_usdc,
        "maximum_drawdown": _drawdown([value for _, value in nav_marks]),
        "daily_volatility": statistics.pstdev(daily_returns) if daily_returns else 0.0,
        "annualized_volatility_secondary": (
            statistics.pstdev(daily_returns) * math.sqrt(365) if daily_returns else 0.0
        ),
        "premium_gross_usdc": ledger.premium_gross_usdc,
        "premium_net_usdc": ledger.premium_net_usdc,
        "premium_observed_usdc": 0.0,
        "premium_modeled_usdc": ledger.premium_modeled_usdc,
        "modeled_premium_fraction": modeled_fraction,
        "estimated_costs_usdc": ledger.estimated_costs_usdc,
        "realized_low_high_pnl_usdc": ledger.realized_low_high_pnl_usdc,
        "unrealized_eth_pnl_usdc": unrealized,
        "unrealized_underlying_pnl_usdc": unrealized,
        "ending_eth": ledger.eth_amount,
        "ending_underlying": ledger.eth_amount,
        "ending_weighted_gross_basis": weighted_gross,
        "csp_opened": ledger.csp_opened,
        "assignments": ledger.assignments,
        "assignment_frequency": (
            ledger.assignments / ledger.csp_settled if ledger.csp_settled else 0.0
        ),
        "covered_calls_opened": ledger.calls_opened,
        "covered_calls_called": ledger.calls_called,
        "complete_cycles": ledger.complete_cycles,
        "turnover_usdc": ledger.turnover_usdc,
        "eth_exposure_hours": ledger.eth_exposure_hours,
        "eth_idle_hours": ledger.eth_idle_hours,
        "eth_idle_share": (
            ledger.eth_idle_amount_hours / ledger.eth_amount_hours
            if ledger.eth_amount_hours
            else 0.0
        ),
        "idle_capital_share": (
            ledger.idle_usdc_hours / ledger.total_usdc_hours
            if ledger.total_usdc_hours
            else 0.0
        ),
        "lot_floor_breach_opportunities": ledger.lot_floor_breach_opportunities,
        "skipped_minimum_premium": ledger.skipped_minimum_premium,
        "missing_market_events": ledger.missing_market_events,
        "pricing_observation_class": "modeled",
        "mm_pricing_observation_class": "modeled",
        "mm_premium_paid_usdc": ledger.mm_premium_paid_usdc,
        "mm_option_payoff_usdc": ledger.mm_option_payoff_usdc,
        "mm_unhedged_pnl_usdc": mm_unhedged,
        "mm_hedge_pnl_usdc": ledger.mm_hedge_pnl_usdc,
        "mm_hedge_cost_usdc": ledger.mm_hedge_cost_usdc,
        "mm_hedge_turnover_usdc": ledger.mm_hedge_turnover_usdc,
        "mm_hedged_pnl_usdc": mm_hedged,
        "mm_hedged_return_on_initial_vault_aum": mm_hedged / initial_usdc,
        "vault_option_pnl_usdc": (
            ledger.mm_premium_paid_usdc - ledger.mm_option_payoff_usdc
        ),
        "option_transfer_reconciliation_usdc": (
            ledger.mm_premium_paid_usdc - ledger.mm_option_payoff_usdc + mm_unhedged
        ),
        **asdict(config),
    }
    result["costs"] = config.costs.name
    return result


def run_strategy(
    *,
    series: MarketSeries,
    settings: BacktestSettings,
    window_days: int,
    config: StrategyConfig,
    csp_only: bool = False,
    end: datetime | None = None,
) -> dict:
    ledger = Ledger(cash_usdc=settings.initial_usdc)
    position_counter = 1
    window_end = end or series.cutoff
    window_start = window_end - timedelta(days=window_days)
    nav_marks = [(utc_timestamp_ms(window_start), settings.initial_usdc)]
    strike_increment = settings.asset(series.asset).strike_increment_usd

    for decision in decision_times(
        series, window_days, settings.cadence_hours, end=window_end
    ):
        execution = decision + timedelta(minutes=config.costs.operational_delay_minutes)
        expiry_dt = decision + timedelta(hours=settings.cadence_hours)
        opened_at = int(execution.timestamp())
        expiry = int(expiry_dt.timestamp())
        market = _market_at(series, utc_timestamp_ms(execution), settings)
        settlement = series.spot_at(
            utc_timestamp_ms(expiry_dt), settings.coverage_gate.maximum_spot_age_hours
        )
        if market is None or settlement is None:
            ledger.missing_market_events += 1
            _record_interval_accounting(
                ledger,
                csp=None,
                covered_lots=set(),
                hours=settings.cadence_hours,
            )
            _record_nav_marks(
                series=series,
                settings=settings,
                ledger=ledger,
                positions=[],
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue
        spot, iv = market
        positions: list[OptionPosition] = []

        csp = _open_csp(
            ledger=ledger,
            config=config,
            settings=settings,
            spot=spot,
            iv=iv,
            opened_at=opened_at,
            expiry=expiry,
            position_id=position_counter,
            strike_increment=strike_increment,
        )
        position_counter += 1
        if csp is not None:
            positions.append(csp)

        covered_lots: set[int] = set()
        if not csp_only and ledger.lots:
            calls, covered_lots = _open_calls(
                ledger=ledger,
                config=config,
                settings=settings,
                spot=spot,
                iv=iv,
                opened_at=opened_at,
                expiry=expiry,
                first_position_id=position_counter,
                strike_increment=strike_increment,
            )
            position_counter += len(ledger.lots) + 1
            positions.extend(calls)

        _record_interval_accounting(
            ledger,
            csp=csp,
            covered_lots=covered_lots,
            hours=settings.cadence_hours,
        )
        _record_nav_marks(
            series=series,
            settings=settings,
            ledger=ledger,
            positions=positions,
            start=execution,
            end=expiry_dt,
            nav_marks=nav_marks,
        )
        midpoint = execution + timedelta(hours=settings.cadence_hours / 2)
        midpoint_market = _market_at(series, utc_timestamp_ms(midpoint), settings)
        midpoint_hedge = None
        if midpoint_market is not None:
            midpoint_spot, midpoint_iv = midpoint_market
            midpoint_hedge = (
                utc_timestamp_ms(midpoint),
                midpoint_spot,
                midpoint_iv,
            )
        _record_mm_counterparty_economics(
            ledger=ledger,
            positions=positions,
            opened_at_ms=utc_timestamp_ms(execution),
            opening_spot=spot,
            opening_iv=iv,
            midpoint=midpoint_hedge,
            settlement_spot=settlement.value,
            risk_free_rate=settings.risk_free_rate,
            hedge_cost_bps=config.costs.mm_hedge_cost_bps,
        )
        _settle_positions(ledger, positions, settlement.value, expiry, csp_only)
        nav_marks.append(
            (
                utc_timestamp_ms(expiry_dt),
                ledger.cash_usdc + ledger.eth_amount * settlement.value,
            )
        )
        if ledger.cash_usdc < -1e-6 or ledger.eth_amount < -1e-12:
            raise AssertionError("backtest ledger produced negative collateral")

    final_spot_value = series.spot_at(
        utc_timestamp_ms(window_end), settings.coverage_gate.maximum_spot_age_hours
    )
    if final_spot_value is None:
        raise RuntimeError("missing final spot")
    return _metrics(
        ledger=ledger,
        nav_marks=nav_marks,
        final_spot=final_spot_value.value,
        initial_usdc=settings.initial_usdc,
        window_days=window_days,
        config=config,
        strategy="csp_only" if csp_only else "wheel",
        asset=series.asset,
        window_start=window_start,
        window_end=window_end,
    )


def hold_benchmarks(
    series: MarketSeries,
    settings: BacktestSettings,
    window_days: int,
    end: datetime | None = None,
) -> list[dict]:
    window_end = end or series.cutoff
    decisions = list(
        decision_times(series, window_days, settings.cadence_hours, end=window_end)
    )
    if not decisions:
        return []
    start = decisions[0]
    start_spot = series.spot_at(
        utc_timestamp_ms(start), settings.coverage_gate.maximum_spot_age_hours
    )
    final_spot = series.spot_at(
        utc_timestamp_ms(window_end), settings.coverage_gate.maximum_spot_age_hours
    )
    if start_spot is None or final_spot is None:
        raise RuntimeError("missing benchmark spot")
    eth_return = final_spot.value / start_spot.value - 1
    rows = []
    for strategy, absolute_return in (
        ("hold_usdc", 0.0),
        ("hold_eth", eth_return),
        ("hold_50_50_no_rebalance", 0.5 * eth_return),
    ):
        rows.append(
            {
                "asset": series.asset,
                "strategy": strategy,
                "window_days": window_days,
                "window_start": start.isoformat(),
                "window_end": window_end.isoformat(),
                "initial_usdc": settings.initial_usdc,
                "final_nav_usdc": settings.initial_usdc * (1 + absolute_return),
                "absolute_return": absolute_return,
                "total_pnl_usdc": settings.initial_usdc * absolute_return,
                "pricing_observation_class": "observed",
            }
        )
    return rows
