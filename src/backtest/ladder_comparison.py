"""Reproducible apples-to-apples ETH CSP ladder research for B1N-450."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.backtest.config import BacktestSettings, load_settings
from src.backtest.data import MarketSeries, utc_timestamp_ms
from src.backtest.engine import binary_bid_premium, select_strike
from src.pricer import apply_vol_skew, bs_delta, bs_price


class ComparisonConfigError(ValueError):
    """Raised when a pinned input or comparison invariant has drifted."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ComparisonConfigError(message)


@dataclass(frozen=True)
class Policy:
    policy_id: str
    description: str
    put_delta: float
    put_tenor_hours: int
    put_lane_offsets_hours: tuple[int, ...]
    aggregate_utilization: float
    minimum_net_premium_bps: int
    strike_tick_usd: float
    hold_assigned_eth: bool
    laddered_covered_calls: bool = False


@dataclass
class Lot:
    lot_id: int
    amount: float
    assignment_strike: float
    assigned_at: datetime


@dataclass(frozen=True)
class Position:
    position_id: int
    kind: str
    lane: int
    opened_at: datetime
    expiry: datetime
    strike: float
    amount: float
    collateral_usdc: float
    premium_gross_usdc: float
    premium_net_usdc: float
    lot_id: int | None = None


@dataclass
class Ledger:
    cash_usdc: float
    lots: list[Lot] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    premium_gross_usdc: float = 0.0
    premium_net_usdc: float = 0.0
    protocol_premium_fee_usdc: float = 0.0
    execution_cost_usdc: float = 0.0
    turnover_usdc: float = 0.0
    puts_opened: int = 0
    puts_settled: int = 0
    assignments: int = 0
    calls_opened: int = 0
    calls_settled: int = 0
    calls_called: int = 0
    complete_wheel_cycles: int = 0
    skipped_premium_floor: int = 0
    skipped_liquidity: int = 0
    missing_market_events: int = 0
    decision_events: int = 0
    opened_put_absolute_deltas: list[float] = field(default_factory=list)

    @property
    def eth_amount(self) -> float:
        return sum(lot.amount for lot in self.lots)

    @property
    def locked_put_collateral(self) -> float:
        return sum(
            position.collateral_usdc
            for position in self.positions
            if position.kind == "put"
        )


def _load_pinned_json(root: Path, source: dict[str, str]) -> dict[str, Any]:
    path = root / source["path"]
    actual = sha256_file(path)
    if actual != source["sha256"]:
        raise ComparisonConfigError(
            f"Pinned input digest mismatch for {source['path']}: "
            f"expected={source['sha256']} actual={actual}"
        )
    return json.loads(path.read_text())


def _policy(raw: dict[str, Any]) -> Policy:
    return Policy(
        policy_id=str(raw["policy_id"]),
        description=str(raw["description"]),
        put_delta=float(raw["put_delta"]),
        put_tenor_hours=int(raw["put_tenor_hours"]),
        put_lane_offsets_hours=tuple(
            int(value) for value in raw["put_lane_offsets_hours"]
        ),
        aggregate_utilization=float(raw["aggregate_utilization"]),
        minimum_net_premium_bps=int(raw["minimum_net_premium_bps"]),
        strike_tick_usd=float(raw["strike_tick_usd"]),
        hold_assigned_eth=bool(raw["hold_assigned_eth"]),
        laddered_covered_calls=bool(raw.get("laddered_covered_calls", False)),
    )


def load_inputs(
    root: Path,
) -> tuple[dict[str, Any], BacktestSettings, MarketSeries, list[Policy]]:
    config_path = root / "backtests" / "b1n_450" / "config.json"
    config = json.loads(config_path.read_text())
    source = config["sources"]
    settings_raw = _load_pinned_json(root, source["settings"])
    market_raw = _load_pinned_json(root, source["market"])
    current = _load_pinned_json(root, source["current_policy_snapshot"])
    settings = load_settings(root / source["settings"]["path"])

    _require(config["issue"] == "B1N-450", "issue id drifted")
    _require(config["window_days"] == [30, 60, 90], "windows must remain 30/60/90")
    _require(market_raw.get("asset") == "ETH", "market asset must be ETH")
    _require(
        market_raw["cutoff"] == config["splits"]["validation"]["end"], "cutoff drifted"
    )
    _require(settings_raw["risk_free_rate"] == 0.05, "risk-free rate drifted")
    selection = current["selection"]
    expected_current = {
        "target_duration_hours": 48,
        "target_put_delta_bps": 900,
        "minimum_net_premium_bps": 20,
        "target_utilization_bps": 8000,
        "liquid_usdc_reserve_bps": 2000,
        "strike_tick_usd": 25,
    }
    for name, expected in expected_current.items():
        _require(selection.get(name) == expected, f"current policy {name} drifted")

    policies = [_policy(item) for item in config["policies"]]
    _require(len(policies) == 6, "comparison must contain exactly six policies")
    _require(
        len({item.policy_id for item in policies}) == 6, "policy ids must be unique"
    )
    baseline = policies[0]
    _require(baseline.policy_id == "current_48h", "current policy must be first")
    _require(
        (
            baseline.put_delta,
            baseline.put_tenor_hours,
            baseline.aggregate_utilization,
            baseline.minimum_net_premium_bps,
            baseline.strike_tick_usd,
        )
        == (0.09, 48, 0.8, 20, 25.0),
        "baseline does not reproduce B1N-438",
    )
    low_weekly = policies[2]
    _require(
        len(low_weekly.put_lane_offsets_hours) == 4
        and 0.25 <= low_weekly.aggregate_utilization <= 0.35
        and low_weekly.put_tenor_hours == 168,
        "low weekly ladder must use four weekly lanes at 25-35% utilization",
    )
    _require(policies[4].put_delta == 0.5, "ATM stress/control must target 0.50 delta")
    _require(policies[5].laddered_covered_calls, "hybrid must ladder covered calls")
    return config, settings, MarketSeries(root / source["market"]["path"]), policies


def non_overlapping_windows(
    start: datetime, end: datetime, window_days: int
) -> list[tuple[datetime, datetime]]:
    windows = []
    cursor = start
    width = timedelta(days=window_days)
    while cursor + width <= end:
        windows.append((cursor, cursor + width))
        cursor += width
    return windows


def _market(
    series: MarketSeries,
    settings: BacktestSettings,
    at: datetime,
) -> tuple[float, float] | None:
    timestamp_ms = utc_timestamp_ms(at)
    spot = series.spot_at(timestamp_ms, settings.coverage_gate.maximum_spot_age_hours)
    iv = series.iv_at(timestamp_ms, settings.coverage_gate.maximum_iv_age_hours)
    if spot is None or iv is None:
        return None
    return spot.value, iv.value


def _liability(
    position: Position,
    at: datetime,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    time_years = max((position.expiry - at).total_seconds(), 0.0) / (365 * 86_400)
    if time_years <= 0:
        intrinsic = (
            max(position.strike - spot, 0.0)
            if position.kind == "put"
            else max(spot - position.strike, 0.0)
        )
        return intrinsic * position.amount
    is_put = position.kind == "put"
    skewed_iv = apply_vol_skew(iv, spot, position.strike, is_put)
    price = bs_price(
        is_put,
        spot,
        position.strike,
        time_years,
        risk_free_rate,
        skewed_iv,
    )
    return max(price, 0.0) * position.amount


def _nav(
    ledger: Ledger,
    at: datetime,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    liabilities = sum(
        _liability(position, at, spot, iv, risk_free_rate)
        for position in ledger.positions
    )
    return ledger.cash_usdc + ledger.eth_amount * spot - liabilities


def _nav_after_parent_fees(
    nav_before_parent_fees: float,
    initial_nav: float,
    elapsed_days: float,
    fee_config: dict[str, Any],
) -> tuple[float, float, float]:
    """Mark NAV after accrued management and hypothetical HWM performance fees."""
    management_fee = (
        max(initial_nav, nav_before_parent_fees, 0.0)
        * int(fee_config["management_fee_bps_annual"])
        / 10_000
        * elapsed_days
        / 365
    )
    pre_performance = nav_before_parent_fees - management_fee
    performance_fee = (
        max(pre_performance - initial_nav, 0.0)
        * int(fee_config["performance_fee_bps"])
        / 10_000
    )
    return pre_performance - performance_fee, management_fee, performance_fee


def _lane_due(elapsed_hours: int, offset: int, tenor_hours: int) -> bool:
    return elapsed_hours >= offset and (elapsed_hours - offset) % tenor_hours == 0


def _open_put(
    *,
    ledger: Ledger,
    policy: Policy,
    lane: int,
    at: datetime,
    window_end: datetime,
    spot: float,
    iv: float,
    settings: BacktestSettings,
    costs: Any,
    protocol_fee_bps: int,
    next_position_id: int,
) -> Position | None:
    expiry = at + timedelta(hours=policy.put_tenor_hours)
    if expiry > window_end:
        return None
    if any(
        position.kind == "put" and position.lane == lane
        for position in ledger.positions
    ):
        raise AssertionError("put lane overlap")
    tenor_years = policy.put_tenor_hours / (365 * 24)
    strike = select_strike(
        is_put=True,
        spot=spot,
        iv=iv,
        time_years=tenor_years,
        target_delta=policy.put_delta,
        risk_free_rate=settings.risk_free_rate,
        strike_increment=policy.strike_tick_usd,
    )
    if strike is None:
        ledger.missing_market_events += 1
        return None
    # B1N-438 sizes each new position from current liquid/idle USDC. Assigned ETH
    # remains held inventory and must not inflate or reserve against the CSP budget.
    available = max(ledger.cash_usdc - ledger.locked_put_collateral, 0.0)
    target = (
        ledger.cash_usdc
        * policy.aggregate_utilization
        / len(policy.put_lane_offsets_hours)
    )
    collateral = min(target, available)
    if collateral <= 0:
        ledger.skipped_liquidity += 1
        return None
    amount = collateral / strike
    gross_per_eth, _, _ = binary_bid_premium(
        is_put=True,
        spot=spot,
        strike=strike,
        time_years=tenor_years,
        iv=iv,
        risk_free_rate=settings.risk_free_rate,
        base_spread_bps=costs.base_spread_bps,
        utilization=policy.aggregate_utilization,
    )
    gross = gross_per_eth * amount
    protocol_fee = gross * protocol_fee_bps / 10_000
    net_before_cost = gross - protocol_fee
    if net_before_cost * 10_000 < collateral * policy.minimum_net_premium_bps:
        ledger.skipped_premium_floor += 1
        return None
    execution_cost = (
        collateral * costs.fee_bps_notional / 10_000
        + costs.gas_usdc
        + gross * costs.execution_slippage_bps / 10_000
    )
    net = net_before_cost - execution_cost
    ledger.cash_usdc += net
    ledger.premium_gross_usdc += gross
    ledger.premium_net_usdc += net
    ledger.protocol_premium_fee_usdc += protocol_fee
    ledger.execution_cost_usdc += execution_cost
    ledger.turnover_usdc += collateral
    ledger.puts_opened += 1
    ledger.opened_put_absolute_deltas.append(
        abs(
            bs_delta(
                True,
                spot,
                strike,
                tenor_years,
                settings.risk_free_rate,
                iv,
            )
        )
    )
    return Position(
        position_id=next_position_id,
        kind="put",
        lane=lane,
        opened_at=at,
        expiry=expiry,
        strike=strike,
        amount=amount,
        collateral_usdc=collateral,
        premium_gross_usdc=gross,
        premium_net_usdc=net,
    )


def _uncovered_lot_amount(ledger: Ledger, lot_id: int) -> float:
    covered = sum(
        position.amount
        for position in ledger.positions
        if position.kind == "call" and position.lot_id == lot_id
    )
    lot = next((item for item in ledger.lots if item.lot_id == lot_id), None)
    return 0.0 if lot is None else max(lot.amount - covered, 0.0)


def _open_calls(
    *,
    ledger: Ledger,
    policy: Policy,
    lane: int,
    at: datetime,
    window_end: datetime,
    spot: float,
    iv: float,
    settings: BacktestSettings,
    costs: Any,
    protocol_fee_bps: int,
    call_config: dict[str, Any],
    first_position_id: int,
) -> list[Position]:
    tenor_hours = int(call_config["tenor_hours"])
    expiry = at + timedelta(hours=tenor_hours)
    if expiry > window_end:
        return []
    if any(
        position.kind == "call" and position.lane == lane
        for position in ledger.positions
    ):
        raise AssertionError("call lane overlap")
    total_eth = ledger.eth_amount
    lane_target = total_eth / int(call_config["lanes"])
    remaining = lane_target
    positions: list[Position] = []
    for lot in sorted(ledger.lots, key=lambda item: (item.assigned_at, item.lot_id)):
        amount = min(_uncovered_lot_amount(ledger, lot.lot_id), remaining)
        if amount <= 1e-12:
            continue
        tenor_years = tenor_hours / (365 * 24)
        delta_strike = select_strike(
            is_put=False,
            spot=spot,
            iv=iv,
            time_years=tenor_years,
            target_delta=float(call_config["target_delta"]),
            risk_free_rate=settings.risk_free_rate,
            strike_increment=float(call_config["strike_tick_usd"]),
        )
        literal_floor = lot.assignment_strike + float(
            call_config["assignment_buffer_usd"]
        )
        strict_floor_strike = (
            math.floor(literal_floor / float(call_config["strike_tick_usd"])) + 1
        ) * float(call_config["strike_tick_usd"])
        strike = max(delta_strike or 0.0, strict_floor_strike)
        gross_per_eth, _, _ = binary_bid_premium(
            is_put=False,
            spot=spot,
            strike=strike,
            time_years=tenor_years,
            iv=iv,
            risk_free_rate=settings.risk_free_rate,
            base_spread_bps=costs.base_spread_bps,
            utilization=policy.aggregate_utilization,
        )
        gross = gross_per_eth * amount
        protocol_fee = gross * protocol_fee_bps / 10_000
        net_before_cost = gross - protocol_fee
        notional = strike * amount
        if net_before_cost * 10_000 < notional * int(
            call_config["minimum_net_premium_bps"]
        ):
            ledger.skipped_premium_floor += 1
            continue
        execution_cost = (
            notional * costs.fee_bps_notional / 10_000
            + costs.gas_usdc
            + gross * costs.execution_slippage_bps / 10_000
        )
        net = net_before_cost - execution_cost
        if net <= 0:
            ledger.skipped_premium_floor += 1
            continue
        ledger.cash_usdc += net
        ledger.premium_gross_usdc += gross
        ledger.premium_net_usdc += net
        ledger.protocol_premium_fee_usdc += protocol_fee
        ledger.execution_cost_usdc += execution_cost
        ledger.turnover_usdc += notional
        ledger.calls_opened += 1
        positions.append(
            Position(
                position_id=first_position_id + len(positions),
                kind="call",
                lane=lane,
                opened_at=at,
                expiry=expiry,
                strike=strike,
                amount=amount,
                collateral_usdc=0.0,
                premium_gross_usdc=gross,
                premium_net_usdc=net,
                lot_id=lot.lot_id,
            )
        )
        remaining -= amount
        if remaining <= 1e-12:
            break
    return positions


def _settle(ledger: Ledger, at: datetime, spot: float, hold_assigned_eth: bool) -> None:
    expired = [position for position in ledger.positions if position.expiry <= at]
    for position in expired:
        if position.kind == "put":
            ledger.puts_settled += 1
            if spot < position.strike:
                ledger.assignments += 1
                intrinsic = (position.strike - spot) * position.amount
                if hold_assigned_eth:
                    ledger.cash_usdc -= position.strike * position.amount
                    ledger.lots.append(
                        Lot(
                            lot_id=position.position_id,
                            amount=position.amount,
                            assignment_strike=position.strike,
                            assigned_at=at,
                        )
                    )
                else:
                    ledger.cash_usdc -= intrinsic
        else:
            ledger.calls_settled += 1
            if spot > position.strike:
                lot = next(
                    item for item in ledger.lots if item.lot_id == position.lot_id
                )
                _require(
                    position.strike > lot.assignment_strike,
                    "covered call breached literal assignment floor",
                )
                ledger.cash_usdc += position.strike * position.amount
                lot.amount -= position.amount
                ledger.calls_called += 1
                ledger.turnover_usdc += position.strike * position.amount
                if lot.amount <= 1e-12:
                    ledger.complete_wheel_cycles += 1
                    ledger.lots.remove(lot)
        ledger.positions.remove(position)


def _drawdown(values: Iterable[float]) -> float:
    peak = -math.inf
    result = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            result = min(result, value / peak - 1)
    return result


def _quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _cvar(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return statistics.fmean(ordered[: max(1, math.ceil(len(ordered) * probability))])


def _regime(
    series: MarketSeries,
    settings: BacktestSettings,
    start: datetime,
    end: datetime,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    spots, ivs = series.observed_range(utc_timestamp_ms(start), utc_timestamp_ms(end))
    if len(spots) < 2 or not ivs:
        return {"market_regime": "missing", "volatility_crash": False}
    underlying_return = spots[-1] / spots[0] - 1
    threshold = float(thresholds[str((end - start).days)])
    underlying_drawdown = _drawdown(spots)
    iv_spike = max(ivs) - statistics.median(ivs)
    if underlying_return >= threshold:
        regime = "bull"
    elif underlying_return <= -threshold:
        regime = "bear"
    else:
        regime = "sideways"
    return {
        "market_regime": regime,
        "volatility_crash": (
            underlying_drawdown <= float(thresholds["crash_drawdown"])
            and iv_spike >= float(thresholds["crash_iv_spike"])
        ),
        "underlying_period_return": underlying_return,
        "underlying_maximum_drawdown": underlying_drawdown,
        "iv_spike": iv_spike,
    }


def run_window(
    *,
    config: dict[str, Any],
    settings: BacktestSettings,
    series: MarketSeries,
    policy: Policy,
    cost_name: str,
    split: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    costs = next(item for item in settings.cost_scenarios if item.name == cost_name)
    fee_config = config["fees"]
    call_config = config["covered_call"]
    ledger = Ledger(cash_usdc=float(config["initial_usdc"]))
    nav_marks: list[float] = [ledger.cash_usdc]
    daily_values: list[float] = [ledger.cash_usdc]
    idle_fractions: list[float] = []
    liquidity_fractions: list[float] = []
    eth_amount_hours = 0.0
    eth_inventory_hours = 0.0
    peak_eth = 0.0
    next_position_id = 1
    elapsed_hours = 0
    current = start
    last_valid_market: tuple[float, float] | None = None

    while current <= end:
        market = _market(series, settings, current)
        if market is None:
            ledger.missing_market_events += 1
            current += timedelta(hours=1)
            elapsed_hours += 1
            continue
        spot, iv = market
        last_valid_market = market
        valuation_at = current
        valuation_spot, valuation_iv = spot, iv
        _settle(ledger, current, spot, policy.hold_assigned_eth)

        if current < end:
            for lane, offset in enumerate(policy.put_lane_offsets_hours):
                if _lane_due(elapsed_hours, offset, policy.put_tenor_hours):
                    ledger.decision_events += 1
                    execution = current + timedelta(
                        minutes=costs.operational_delay_minutes
                    )
                    execution_market = _market(series, settings, execution)
                    if execution_market is None:
                        ledger.missing_market_events += 1
                        continue
                    execution_spot, execution_iv = execution_market
                    _settle(
                        ledger,
                        execution,
                        execution_spot,
                        policy.hold_assigned_eth,
                    )
                    valuation_at = execution
                    valuation_spot, valuation_iv = execution_spot, execution_iv
                    last_valid_market = execution_market
                    position = _open_put(
                        ledger=ledger,
                        policy=policy,
                        lane=lane,
                        at=execution,
                        window_end=end,
                        spot=execution_spot,
                        iv=execution_iv,
                        settings=settings,
                        costs=costs,
                        protocol_fee_bps=int(fee_config["protocol_premium_fee_bps"]),
                        next_position_id=next_position_id,
                    )
                    if position is not None:
                        ledger.positions.append(position)
                        next_position_id += 1
            if policy.laddered_covered_calls and ledger.lots:
                for lane, offset in enumerate(call_config["lane_offsets_hours"]):
                    if _lane_due(
                        elapsed_hours, int(offset), int(call_config["tenor_hours"])
                    ):
                        ledger.decision_events += 1
                        execution = current + timedelta(
                            minutes=costs.operational_delay_minutes
                        )
                        execution_market = _market(series, settings, execution)
                        if execution_market is None:
                            ledger.missing_market_events += 1
                            continue
                        execution_spot, execution_iv = execution_market
                        _settle(
                            ledger,
                            execution,
                            execution_spot,
                            policy.hold_assigned_eth,
                        )
                        valuation_at = execution
                        valuation_spot, valuation_iv = execution_spot, execution_iv
                        last_valid_market = execution_market
                        calls = _open_calls(
                            ledger=ledger,
                            policy=policy,
                            lane=lane,
                            at=execution,
                            window_end=end,
                            spot=execution_spot,
                            iv=execution_iv,
                            settings=settings,
                            costs=costs,
                            protocol_fee_bps=int(
                                fee_config["protocol_premium_fee_bps"]
                            ),
                            call_config=call_config,
                            first_position_id=next_position_id,
                        )
                        ledger.positions.extend(calls)
                        next_position_id += len(calls)

        nav_before_parent_fees = _nav(
            ledger,
            valuation_at,
            valuation_spot,
            valuation_iv,
            settings.risk_free_rate,
        )
        nav, _, _ = _nav_after_parent_fees(
            nav_before_parent_fees,
            float(config["initial_usdc"]),
            max((valuation_at - start).total_seconds() / 86_400, 0.0),
            fee_config,
        )
        nav_marks.append(nav)
        liquid = max(ledger.cash_usdc - ledger.locked_put_collateral, 0.0)
        if nav > 0:
            idle_fractions.append(liquid / nav)
            liquidity_fractions.append(liquid / nav)
        eth_amount_hours += ledger.eth_amount
        eth_inventory_hours += float(ledger.eth_amount > 1e-12)
        peak_eth = max(peak_eth, ledger.eth_amount)
        if elapsed_hours > 0 and elapsed_hours % 24 == 0:
            daily_values.append(nav)

        current += timedelta(hours=1)
        elapsed_hours += 1

    if last_valid_market is None:
        raise RuntimeError("window has no causal market observations")
    final_spot, final_iv = last_valid_market
    if ledger.positions:
        raise AssertionError(
            "comparison opened a position that did not settle in-window"
        )
    final_nav_before_parent_fees = _nav(
        ledger, end, final_spot, final_iv, settings.risk_free_rate
    )
    final_nav, management_fee, performance_fee = _nav_after_parent_fees(
        final_nav_before_parent_fees,
        float(config["initial_usdc"]),
        (end - start).total_seconds() / 86_400,
        fee_config,
    )
    period_return = final_nav / float(config["initial_usdc"]) - 1
    daily_returns = [
        daily_values[index] / daily_values[index - 1] - 1
        for index in range(1, len(daily_values))
        if daily_values[index - 1] > 0
    ]
    hours = max((end - start).total_seconds() / 3600, 1.0)
    regime = _regime(series, settings, start, end, config["market_regime_thresholds"])
    return {
        "schema_version": 1,
        "issue": "B1N-450",
        "split": split,
        "policy_id": policy.policy_id,
        "cost_scenario": cost_name,
        "window_days": (end - start).days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "put_target_delta": policy.put_delta,
        "median_opened_absolute_put_delta": (
            statistics.median(ledger.opened_put_absolute_deltas)
            if ledger.opened_put_absolute_deltas
            else None
        ),
        "put_tenor_hours": policy.put_tenor_hours,
        "put_lane_count": len(policy.put_lane_offsets_hours),
        "aggregate_utilization": policy.aggregate_utilization,
        "minimum_net_premium_bps": policy.minimum_net_premium_bps,
        "strike_tick_usd": policy.strike_tick_usd,
        "period_return": period_return,
        "final_nav_usdc": final_nav,
        "management_fee_usdc": management_fee,
        "performance_fee_usdc": performance_fee,
        "modeled_gross_premium_usdc": ledger.premium_gross_usdc,
        "modeled_net_premium_usdc": ledger.premium_net_usdc,
        "modeled_net_premium_yield": ledger.premium_net_usdc
        / float(config["initial_usdc"]),
        "observed_executable_premium_usdc": 0.0,
        "observed_executable_premium_events": 0,
        "premium_observation_class": "modeled_binary_bid_on_observed_spot_and_dvol",
        "protocol_premium_fee_usdc": ledger.protocol_premium_fee_usdc,
        "execution_cost_usdc": ledger.execution_cost_usdc,
        "assignment_frequency_per_settled_put": (
            ledger.assignments / ledger.puts_settled if ledger.puts_settled else 0.0
        ),
        "assignments_per_30_days": ledger.assignments * 30 / (end - start).days,
        "assignments": ledger.assignments,
        "puts_opened": ledger.puts_opened,
        "puts_settled": ledger.puts_settled,
        "ending_eth": ledger.eth_amount,
        "peak_eth": peak_eth,
        "eth_inventory_time_fraction": eth_inventory_hours / hours,
        "average_eth_inventory": eth_amount_hours / hours,
        "maximum_drawdown_after_parent_fees": _drawdown(nav_marks),
        "daily_return_cvar_5_after_parent_fees": _cvar(daily_returns, 0.05),
        "risk_metric_fee_basis": "after_accrued_management_and_hypothetical_hwm_performance_fees",
        "idle_capital_fraction_mean": statistics.fmean(idle_fractions)
        if idle_fractions
        else 0.0,
        "turnover_usdc": ledger.turnover_usdc,
        "complete_wheel_cycles": ledger.complete_wheel_cycles,
        "redemption_liquidity_fraction_mean": (
            statistics.fmean(liquidity_fractions) if liquidity_fractions else 0.0
        ),
        "redemption_liquidity_fraction_p5": _quantile(liquidity_fractions, 0.05),
        "redemption_liquidity_fraction_minimum": min(liquidity_fractions, default=0.0),
        "redemption_liquidity_definition": "unencumbered_usdc_divided_by_marked_nav_proxy",
        "operational_decisions": ledger.decision_events,
        "operational_actions": (
            ledger.puts_opened
            + ledger.puts_settled
            + ledger.calls_opened
            + ledger.calls_settled
        ),
        "operational_actions_per_30_days": (
            (
                ledger.puts_opened
                + ledger.puts_settled
                + ledger.calls_opened
                + ledger.calls_settled
            )
            * 30
            / (end - start).days
        ),
        "covered_calls_opened": ledger.calls_opened,
        "covered_calls_called": ledger.calls_called,
        "skipped_premium_floor": ledger.skipped_premium_floor,
        "skipped_liquidity": ledger.skipped_liquidity,
        "missing_market_events": ledger.missing_market_events,
        "causal_observations_only": True,
        **regime,
    }


def build_rows(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config, settings, series, policies = load_inputs(root)
    rows = []
    for split, bounds in config["splits"].items():
        split_start = datetime.fromisoformat(bounds["start"])
        split_end = datetime.fromisoformat(bounds["end"])
        for window_days in config["window_days"]:
            for start, end in non_overlapping_windows(
                split_start, split_end, window_days
            ):
                for cost_name in config["cost_scenarios"]:
                    for policy in policies:
                        rows.append(
                            run_window(
                                config=config,
                                settings=settings,
                                series=series,
                                policy=policy,
                                cost_name=cost_name,
                                split=split,
                                start=start,
                                end=end,
                            )
                        )
    return config, rows


def _distribution(values: Iterable[float]) -> dict[str, float]:
    values = list(values)
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "p5": _quantile(values, 0.05),
        "median": _quantile(values, 0.5),
        "p95": _quantile(values, 0.95),
        "worst": min(values, default=0.0),
        "cvar_10": _cvar(values, 0.10),
    }


def build_summary(config: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row["split"],
                row["policy_id"],
                int(row["window_days"]),
                row["cost_scenario"],
            )
        ].append(row)
    groups = []
    for (split, policy_id, window_days, cost_name), group in sorted(grouped.items()):
        regime_metrics = []
        for regime in ("bull", "bear", "sideways", "volatility_crash"):
            regime_group = [
                row
                for row in group
                if (
                    row["volatility_crash"]
                    if regime == "volatility_crash"
                    else row["market_regime"] == regime
                )
            ]
            regime_metrics.append(
                {
                    "regime": regime,
                    "sample_count": len(regime_group),
                    "period_return": _distribution(
                        row["period_return"] for row in regime_group
                    ),
                    "assignment_frequency_per_settled_put_pooled": (
                        sum(row["assignments"] for row in regime_group)
                        / sum(row["puts_settled"] for row in regime_group)
                        if sum(row["puts_settled"] for row in regime_group)
                        else 0.0
                    ),
                    "assignments_per_30_days_mean": (
                        statistics.fmean(
                            row["assignments_per_30_days"] for row in regime_group
                        )
                        if regime_group
                        else 0.0
                    ),
                    "maximum_drawdown_after_parent_fees_worst": min(
                        (
                            row["maximum_drawdown_after_parent_fees"]
                            for row in regime_group
                        ),
                        default=0.0,
                    ),
                }
            )
        groups.append(
            {
                "split": split,
                "policy_id": policy_id,
                "window_days": window_days,
                "cost_scenario": cost_name,
                "sample_count": len(group),
                "period_return": _distribution(row["period_return"] for row in group),
                "modeled_net_premium_yield": _distribution(
                    row["modeled_net_premium_yield"] for row in group
                ),
                "assignment_frequency_per_settled_put_pooled": (
                    sum(row["assignments"] for row in group)
                    / sum(row["puts_settled"] for row in group)
                ),
                "assignments_per_30_days_mean": statistics.fmean(
                    row["assignments_per_30_days"] for row in group
                ),
                "maximum_drawdown_after_parent_fees_worst": min(
                    row["maximum_drawdown_after_parent_fees"] for row in group
                ),
                "daily_return_cvar_5_after_parent_fees_worst": min(
                    row["daily_return_cvar_5_after_parent_fees"] for row in group
                ),
                "eth_inventory_time_fraction_mean": statistics.fmean(
                    row["eth_inventory_time_fraction"] for row in group
                ),
                "average_eth_inventory_mean": statistics.fmean(
                    row["average_eth_inventory"] for row in group
                ),
                "idle_capital_fraction_mean": statistics.fmean(
                    row["idle_capital_fraction_mean"] for row in group
                ),
                "turnover_usdc_mean": statistics.fmean(
                    row["turnover_usdc"] for row in group
                ),
                "complete_wheel_cycles_total": sum(
                    row["complete_wheel_cycles"] for row in group
                ),
                "redemption_liquidity_fraction_p5_worst": min(
                    row["redemption_liquidity_fraction_p5"] for row in group
                ),
                "operational_actions_per_30_days_mean": statistics.fmean(
                    row["operational_actions_per_30_days"] for row in group
                ),
                "market_regime_counts": {
                    regime: sum(row["market_regime"] == regime for row in group)
                    for regime in ("bull", "bear", "sideways")
                },
                "volatility_crash_samples": sum(
                    row["volatility_crash"] for row in group
                ),
                "regime_metrics": regime_metrics,
                "observed_executable_premium_events": 0,
            }
        )

    validation_base = [
        group
        for group in groups
        if group["split"] == "validation" and group["cost_scenario"] == "base"
    ]
    atm_checks = []
    for window_days in config["window_days"]:
        current = next(
            group
            for group in validation_base
            if group["policy_id"] == "current_48h"
            and group["window_days"] == window_days
        )
        atm = next(
            group
            for group in validation_base
            if group["policy_id"] == "atm_48h_control"
            and group["window_days"] == window_days
        )
        atm_checks.append(
            {
                "window_days": window_days,
                "current_assignment_frequency_per_settled_put": current[
                    "assignment_frequency_per_settled_put_pooled"
                ],
                "atm_assignment_frequency_per_settled_put": atm[
                    "assignment_frequency_per_settled_put_pooled"
                ],
                "current_assignments_per_30_days": current[
                    "assignments_per_30_days_mean"
                ],
                "atm_assignments_per_30_days": atm["assignments_per_30_days_mean"],
                "current_worst_drawdown_after_parent_fees": current[
                    "maximum_drawdown_after_parent_fees_worst"
                ],
                "atm_worst_drawdown_after_parent_fees": atm[
                    "maximum_drawdown_after_parent_fees_worst"
                ],
                "assignment_frequency_higher": bool(
                    atm["assignment_frequency_per_settled_put_pooled"]
                    > current["assignment_frequency_per_settled_put_pooled"]
                ),
                "assignment_intensity_higher": bool(
                    atm["assignments_per_30_days_mean"]
                    > current["assignments_per_30_days_mean"]
                ),
                "drawdown_worse": bool(
                    atm["maximum_drawdown_after_parent_fees_worst"]
                    < current["maximum_drawdown_after_parent_fees_worst"]
                ),
            }
        )
    atm_rows = [row for row in rows if row["policy_id"] == "atm_48h_control"]
    opened_atm_deltas = [
        float(row["median_opened_absolute_put_delta"])
        for row in atm_rows
        if row["median_opened_absolute_put_delta"] is not None
    ]
    atm_hypothesis = {
        "target_delta": 0.5,
        "median_opened_absolute_put_delta": _quantile(opened_atm_deltas, 0.5),
        "interpretation": "Black-Scholes absolute put delta target; approximately 50-delta, not a BlackRock CSP rule",
        "materially_riskier": all(
            item["assignment_frequency_higher"]
            and item["assignment_intensity_higher"]
            and item["drawdown_worse"]
            for item in atm_checks
        ),
        "checks": atm_checks,
        "opened_positions": sum(row["puts_opened"] for row in atm_rows),
    }
    return {
        "schema_version": 1,
        "issue": "B1N-450",
        "warning": (
            "Research only. Observed Deribit spot/DVOL drive modeled Binary bids; "
            "no premium is an observed fill or executable quote. No live policy change is authorized."
        ),
        "splits": config["splits"],
        "window_days": config["window_days"],
        "group_metrics": groups,
        "atm_hypothesis": atm_hypothesis,
        "premium_evidence": {
            "modeled_rows": len(rows),
            "observed_executable_rows": 0,
            "observed_underlying_and_iv": True,
        },
    }


def build_recommendation(
    config: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    evidence = summary["premium_evidence"]
    rule = config["recommendation_rule"]
    observed_rows = int(evidence["observed_executable_rows"])
    evidence_required = bool(
        rule["live_change_requires_observed_executable_premium_evidence"]
    )
    change_gate_passed = not evidence_required or observed_rows > 0
    decision = str(
        rule[
            "met_change_gate_result"
            if change_gate_passed
            else "unmet_change_gate_result"
        ]
    )
    validation_base = {
        (group["policy_id"], group["window_days"]): group
        for group in summary["group_metrics"]
        if group["split"] == "validation" and group["cost_scenario"] == "base"
    }
    current_90 = validation_base[("current_48h", 90)]
    staggered_90 = validation_base[("current_48h_staggered_4", 90)]
    weekly_low_90 = validation_base[("weekly_4_low_30pct", 90)]
    hybrid_90 = validation_base[("hybrid_current_csp_weekly_cc", 90)]
    reasons = [
        "No observed executable Binary premium or fill evidence exists for any arm.",
        (
            "The 30% weekly ladder improved 90-day worst drawdown and redemption "
            f"liquidity but had {weekly_low_90['period_return']['median']:.2%} median "
            f"return versus {current_90['period_return']['median']:.2%} for current."
        ),
        (
            "The staggered 48h arm improved 90-day worst drawdown but reduced median "
            f"return to {staggered_90['period_return']['median']:.2%} and raised modeled "
            f"operational load to {staggered_90['operational_actions_per_30_days_mean']:.1f} "
            f"actions/30d versus {current_90['operational_actions_per_30_days_mean']:.1f}."
        ),
        (
            f"The hybrid completed {hybrid_90['complete_wheel_cycles_total']} full "
            "assignment-lot Wheel cycles and did not dominate current 90-day return "
            f"({hybrid_90['period_return']['median']:.2%} versus "
            f"{current_90['period_return']['median']:.2%}); it also increased "
            "turnover/operations."
        ),
        "ATM is a high-assignment stress/control, not a candidate and not a rule attributed to BlackRock.",
    ]
    payload = {
        "schema_version": 1,
        "issue": "B1N-450",
        "decision": decision,
        "attested_at": config["generated_at"],
        "attestation_scope": "content_integrity_only_not_signer_authentication",
        "live_policy_action": (
            "none_keep_b1n_438_unchanged"
            if decision == "keep"
            else "none_research_gate_only"
        ),
        "recommended_follow_up": (
            "If product owners choose to pursue laddering after independent review and "
            "executable-premium collection, create a new implementation ticket; any candidate "
            "must remain disabled by default until separately approved."
        ),
        "reasons": reasons,
        "confidence": "low_to_moderate",
        "confidence_limitations": [
            "Premiums and capacity are modeled rather than executable observations.",
            "The redemption metric is an unencumbered-USDC proxy without subscriptions or queued redemptions.",
            "Non-overlapping samples are limited: four 90-day validation windows make CVaR10 equal the single worst observation.",
            "Historical Deribit perpetual/DVOL inputs may not represent Base execution or future regimes.",
        ],
        "evidence_digest_sha256": canonical_sha256(summary),
        "decision_gate": {
            "observed_executable_rows_required_for_change": evidence_required,
            "observed_executable_rows": observed_rows,
            "change_gate_passed": change_gate_passed,
        },
    }
    payload["content_attestation_method"] = "sha256_canonical_json"
    payload["content_attestation_sha256"] = canonical_sha256(payload)
    return payload


def verify_recommendation_attestation(recommendation: dict[str, Any]) -> bool:
    unattested = dict(recommendation)
    attestation = unattested.pop("content_attestation_sha256", None)
    return attestation == canonical_sha256(unattested)


def write_report(
    path: Path,
    config: dict[str, Any],
    summary: dict[str, Any],
    recommendation: dict[str, Any],
) -> None:
    validation_base = [
        group
        for group in summary["group_metrics"]
        if group["split"] == "validation" and group["cost_scenario"] == "base"
    ]
    validation_stressed = [
        group
        for group in summary["group_metrics"]
        if group["split"] == "validation" and group["cost_scenario"] == "stressed"
    ]
    descriptions = {
        item["policy_id"]: item["description"] for item in config["policies"]
    }
    lines = [
        "# B1N-450 — ETH CSP partial-utilization and weekly-ladder comparison",
        "",
        "> Research only. This package does not deploy, activate, or change live ETH policy/config.",
        "",
        f"Gate-derived recommendation: **{recommendation['decision'].upper()}** the current B1N-438 live policy.",
        f"Content-integrity attestation (not signer authentication): `{recommendation['content_attestation_sha256']}`.",
        "",
        "Observed Deribit ETH perpetual closes and DVOL are causal inputs. Every option premium is a modeled Binary bid; observed executable premium evidence is zero for every arm.",
        "",
        "## Frozen arms",
        "",
    ]
    for item in config["policies"]:
        lines.append(f"- `{item['policy_id']}`: {item['description']}")
    lines.extend(
        [
            "",
            "All arms share the pinned market snapshot, $100,000 opening NAV, 5% risk-free rate, current fee semantics, causal observation-age gates, and base/stressed cost definitions. Development and validation periods are disjoint; windows are non-overlapping within each split.",
            "",
            "Assignment frequency is per settled put; assignments/30d is the time-normalized cross-tenor intensity. Drawdown and daily NAV CVaR marks include accrued management fees and hypothetical HWM performance fees at each mark. With four 90-day validation windows, CVaR10 of period return equals the single worst window.",
            "",
            "## Validation — base costs",
            "",
            "| Policy | Window | Median return | Premium yield | Assign/settled put | Assignments/30d | Worst DD after fees | Return CVaR10 | ETH time | Idle capital | Redemption liquidity p5 | Turnover | Complete Wheel cycles | Ops/30d |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group in validation_base:
        lines.append(
            f"| {group['policy_id']} | {group['window_days']}d | "
            f"{group['period_return']['median']:.2%} | "
            f"{group['modeled_net_premium_yield']['median']:.2%} | "
            f"{group['assignment_frequency_per_settled_put_pooled']:.1%} | "
            f"{group['assignments_per_30_days_mean']:.2f} | "
            f"{group['maximum_drawdown_after_parent_fees_worst']:.2%} | "
            f"{group['period_return']['cvar_10']:.2%} | "
            f"{group['eth_inventory_time_fraction_mean']:.1%} | "
            f"{group['idle_capital_fraction_mean']:.1%} | "
            f"{group['redemption_liquidity_fraction_p5_worst']:.1%} | "
            f"${group['turnover_usdc_mean']:,.0f} | "
            f"{group['complete_wheel_cycles_total']} | "
            f"{group['operational_actions_per_30_days_mean']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Validation — stressed modeled costs",
            "",
            "| Policy | Window | Median return | Premium yield | Assign/settled put | Assignments/30d | Worst DD after fees | Return CVaR10 | Redemption liquidity p5 | Ops/30d |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group in validation_stressed:
        lines.append(
            f"| {group['policy_id']} | {group['window_days']}d | "
            f"{group['period_return']['median']:.2%} | "
            f"{group['modeled_net_premium_yield']['median']:.2%} | "
            f"{group['assignment_frequency_per_settled_put_pooled']:.1%} | "
            f"{group['assignments_per_30_days_mean']:.2f} | "
            f"{group['maximum_drawdown_after_parent_fees_worst']:.2%} | "
            f"{group['period_return']['cvar_10']:.2%} | "
            f"{group['redemption_liquidity_fraction_p5_worst']:.1%} | "
            f"{group['operational_actions_per_30_days_mean']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## ATM hypothesis",
            "",
            "The ATM control targets absolute put delta 0.50 (approximately 50-delta under the model). It is a deliberately high-assignment control, not a candidate. No CSP rule in this package is attributed to BlackRock.",
            f" Median opened absolute put delta across raw windows: `{summary['atm_hypothesis']['median_opened_absolute_put_delta']:.4f}`.",
            "",
            f"Materially riskier on the predeclared assignment-frequency and drawdown checks: `{summary['atm_hypothesis']['materially_riskier']}`.",
            "",
            "## Causal and regime evidence",
            "",
            "Rows use only the latest spot/DVOL observations available at each decision. Market regimes are classified independently as bull, bear, sideways and volatility-crash overlays. Raw normalized rows retain regime labels, causal-coverage failures, 30/60/90-day metrics, base/stressed costs, and operational counts.",
            "",
            "## Recommendation and limitations",
            "",
        ]
    )
    lines.extend(f"- {reason}" for reason in recommendation["reasons"])
    lines.append("")
    lines.extend(
        f"- Limitation: {item}" for item in recommendation["confidence_limitations"]
    )
    lines.extend(
        [
            "",
            "A future ladder or hybrid change must remain disabled, require a new implementation ticket, and pass independent pricing review plus executable-premium evidence gates. This ticket authorizes no runtime action.",
            "",
            "## Policy descriptions",
            "",
        ]
    )
    lines.extend(f"- `{key}` — {value}" for key, value in descriptions.items())
    path.write_text("\n".join(lines) + "\n")


def write_outputs(
    root: Path, config: dict[str, Any], rows: list[dict[str, Any]]
) -> Path:
    output = root / "backtests" / "b1n_450" / "results"
    output.mkdir(parents=True, exist_ok=True)
    summary = build_summary(config, rows)
    recommendation = build_recommendation(config, summary)
    _require(
        verify_recommendation_attestation(recommendation),
        "recommendation content attestation failed",
    )

    with (output / "results.jsonl").open("w") as target:
        for row in rows:
            target.write(json.dumps(row, sort_keys=True) + "\n")
    fields = sorted({key for row in rows for key in row})
    with (output / "results.csv").open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    (output / "recommendation.json").write_text(
        json.dumps(recommendation, indent=2, sort_keys=True) + "\n"
    )
    write_report(output / "REPORT.md", config, summary, recommendation)
    checksum_paths = (
        (output / "results.jsonl", "results.jsonl"),
        (output / "results.csv", "results.csv"),
        (output / "summary.json", "summary.json"),
        (output / "recommendation.json", "recommendation.json"),
        (output / "REPORT.md", "REPORT.md"),
        (output.parent / "config.json", "../config.json"),
        (
            output.parent / "inputs/csp_fund_policy.v4.base-sepolia.snapshot.json",
            "../inputs/csp_fund_policy.v4.base-sepolia.snapshot.json",
        ),
        (
            root / "src/backtest/ladder_comparison.py",
            "../../../src/backtest/ladder_comparison.py",
        ),
        (
            root / "scripts/run_b1n_450_ladder_comparison.py",
            "../../../scripts/run_b1n_450_ladder_comparison.py",
        ),
        (
            root / "tests/test_ladder_comparison.py",
            "../../../tests/test_ladder_comparison.py",
        ),
    )
    (output / "checksums.sha256").write_text(
        "".join(
            f"{sha256_file(path)}  {relative}\n" for path, relative in checksum_paths
        )
    )
    return output
