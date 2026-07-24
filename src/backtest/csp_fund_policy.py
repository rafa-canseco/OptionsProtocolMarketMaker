from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.backtest.config import BacktestSettings
from src.backtest.data import MarketSeries, utc_timestamp_ms
from src.backtest.engine import (
    _fair_option_liability,
    binary_bid_premium,
    select_strike,
)
from src.backtest.models import CostScenario, OptionPosition
from src.backtest.probe import decision_times
from src.pricer import bs_delta


@dataclass(frozen=True)
class PhysicalCspCandidate:
    strike_rule: str
    strike_parameter: float
    utilization: float
    minimum_net_premium_bps: int
    costs: CostScenario

    @property
    def candidate_id(self) -> str:
        parameter = f"{self.strike_parameter:.3f}".rstrip("0").rstrip(".")
        prefix = "m" if self.strike_rule == "fixed_moneyness_below_spot" else "d"
        utilization = f"{self.utilization:.2f}".rstrip("0").rstrip(".")
        return (
            f"{prefix}{parameter}-u{utilization}-p{self.minimum_net_premium_bps}"
            f"-{self.costs.name}"
        )


@dataclass(frozen=True)
class FundRiskSettings:
    maximum_weth_nav_fraction_for_new_entry: float
    minimum_deployable_collateral_usdc: float


@dataclass
class PhysicalFundLedger:
    cash_usdc: float
    weth_amount: float = 0.0
    premium_net_usdc: float = 0.0
    estimated_costs_usdc: float = 0.0
    collateral_assigned_usdc: float = 0.0
    assigned_weth_amount: float = 0.0
    positions_opened: int = 0
    positions_settled: int = 0
    assignments: int = 0
    eligible_decisions: int = 0
    skipped_minimum_premium: int = 0
    skipped_weth_inventory: int = 0
    skipped_insufficient_collateral: int = 0
    missing_market_events: int = 0
    peak_weth_nav_fraction: float = 0.0


def _quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {key: 0.0 for key in ("mean", "p5", "p25", "p50", "p75", "p95", "worst")}
    return {
        "mean": statistics.fmean(values),
        "p5": _quantile(values, 0.05),
        "p25": _quantile(values, 0.25),
        "p50": _quantile(values, 0.50),
        "p75": _quantile(values, 0.75),
        "p95": _quantile(values, 0.95),
        "worst": min(values),
    }


def _drawdown(values: list[float]) -> float:
    peak = -math.inf
    result = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            result = min(result, value / peak - 1)
    return result


def fixed_moneyness_put_strike(
    *,
    spot: float,
    moneyness_below_spot: float,
    strike_increment: float,
) -> float | None:
    """Return a put strike at least the configured percentage below spot."""
    if spot <= 0:
        raise ValueError("spot must be positive")
    if not 0 < moneyness_below_spot < 1:
        raise ValueError("moneyness_below_spot must be between zero and one")
    if strike_increment <= 0:
        raise ValueError("strike_increment must be positive")
    raw_strike = spot * (1 - moneyness_below_spot)
    strike = math.floor(raw_strike / strike_increment) * strike_increment
    if strike <= 0 or strike >= spot:
        return None
    return float(strike)


def candidate_put_strike(
    *,
    candidate: PhysicalCspCandidate,
    spot: float,
    iv: float,
    time_years: float,
    risk_free_rate: float,
    strike_increment: float,
) -> float | None:
    if candidate.strike_rule == "fixed_moneyness_below_spot":
        return fixed_moneyness_put_strike(
            spot=spot,
            moneyness_below_spot=candidate.strike_parameter,
            strike_increment=strike_increment,
        )
    if candidate.strike_rule == "target_put_delta":
        return select_strike(
            is_put=True,
            spot=spot,
            iv=iv,
            time_years=time_years,
            target_delta=candidate.strike_parameter,
            risk_free_rate=risk_free_rate,
            strike_increment=strike_increment,
        )
    raise ValueError(f"Unknown strike rule: {candidate.strike_rule}")


def _nav(
    *,
    ledger: PhysicalFundLedger,
    spot: float,
    position: OptionPosition | None = None,
    timestamp_ms: int | None = None,
    iv: float | None = None,
    risk_free_rate: float = 0.0,
) -> float:
    value = ledger.cash_usdc + ledger.weth_amount * spot
    if position is not None:
        if timestamp_ms is None or iv is None:
            raise ValueError("Open-position NAV requires timestamp and IV")
        value -= _fair_option_liability(
            position,
            timestamp_ms,
            spot,
            iv,
            risk_free_rate,
        )
    return value


def _weth_nav_fraction(ledger: PhysicalFundLedger, spot: float) -> float:
    nav = _nav(ledger=ledger, spot=spot)
    if nav <= 0:
        return 1.0 if ledger.weth_amount > 0 else 0.0
    return ledger.weth_amount * spot / nav


def _record_interval_nav(
    *,
    series: MarketSeries,
    settings: BacktestSettings,
    ledger: PhysicalFundLedger,
    position: OptionPosition | None,
    start: datetime,
    end: datetime,
    nav_marks: list[tuple[int, float]],
) -> None:
    timestamp = start
    while timestamp < end:
        timestamp_ms = utc_timestamp_ms(timestamp)
        spot = series.spot_at(
            timestamp_ms,
            settings.coverage_gate.maximum_spot_age_hours,
        )
        iv = series.iv_at(
            timestamp_ms,
            settings.coverage_gate.maximum_iv_age_hours,
        )
        if spot is not None and (position is None or iv is not None):
            nav_marks.append(
                (
                    timestamp_ms,
                    _nav(
                        ledger=ledger,
                        spot=spot.value,
                        position=position,
                        timestamp_ms=timestamp_ms,
                        iv=None if iv is None else iv.value,
                        risk_free_rate=settings.risk_free_rate,
                    ),
                )
            )
        timestamp += timedelta(hours=1)


def run_physical_csp_window(
    *,
    series: MarketSeries,
    settings: BacktestSettings,
    window_days: int,
    end: datetime,
    candidate: PhysicalCspCandidate,
    fund_risk: FundRiskSettings,
) -> dict[str, Any]:
    window_start = end - timedelta(days=window_days)
    ledger = PhysicalFundLedger(cash_usdc=settings.initial_usdc)
    nav_marks: list[tuple[int, float]] = [
        (utc_timestamp_ms(window_start), settings.initial_usdc)
    ]
    opened_strike_distances: list[float] = []
    opened_absolute_put_deltas: list[float] = []
    strike_increment = settings.asset(series.asset).strike_increment_usd
    position_counter = 1
    decisions = 0

    for decision in decision_times(
        series,
        window_days,
        settings.cadence_hours,
        end=end,
    ):
        decisions += 1
        execution = decision + timedelta(
            minutes=candidate.costs.operational_delay_minutes
        )
        expiry_dt = decision + timedelta(hours=settings.cadence_hours)
        execution_ms = utc_timestamp_ms(execution)
        expiry_ms = utc_timestamp_ms(expiry_dt)
        opening_spot = series.spot_at(
            execution_ms,
            settings.coverage_gate.maximum_spot_age_hours,
        )
        opening_iv = series.iv_at(
            execution_ms,
            settings.coverage_gate.maximum_iv_age_hours,
        )
        settlement_spot = series.spot_at(
            expiry_ms,
            settings.coverage_gate.maximum_spot_age_hours,
        )
        if opening_spot is None or opening_iv is None or settlement_spot is None:
            ledger.missing_market_events += 1
            _record_interval_nav(
                series=series,
                settings=settings,
                ledger=ledger,
                position=None,
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue

        spot = opening_spot.value
        iv = opening_iv.value
        weth_fraction = _weth_nav_fraction(ledger, spot)
        ledger.peak_weth_nav_fraction = max(
            ledger.peak_weth_nav_fraction,
            weth_fraction,
        )
        if weth_fraction >= fund_risk.maximum_weth_nav_fraction_for_new_entry:
            ledger.skipped_weth_inventory += 1
            _record_interval_nav(
                series=series,
                settings=settings,
                ledger=ledger,
                position=None,
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue

        ledger.eligible_decisions += 1
        opened_at = int(execution.timestamp())
        expiry = int(expiry_dt.timestamp())
        time_years = (expiry - opened_at) / (365 * 86_400)
        strike = candidate_put_strike(
            candidate=candidate,
            spot=spot,
            iv=iv,
            time_years=time_years,
            risk_free_rate=settings.risk_free_rate,
            strike_increment=strike_increment,
        )
        if strike is None:
            ledger.missing_market_events += 1
            _record_interval_nav(
                series=series,
                settings=settings,
                ledger=ledger,
                position=None,
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue

        fee_rate = candidate.costs.fee_bps_notional / 10_000
        deploy_budget = ledger.cash_usdc * candidate.utilization
        collateral = max(deploy_budget - candidate.costs.gas_usdc, 0.0) / (1 + fee_rate)
        if collateral < fund_risk.minimum_deployable_collateral_usdc:
            ledger.skipped_insufficient_collateral += 1
            _record_interval_nav(
                series=series,
                settings=settings,
                ledger=ledger,
                position=None,
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue

        amount = collateral / strike
        premium_per_weth, _, _ = binary_bid_premium(
            is_put=True,
            spot=spot,
            strike=strike,
            time_years=time_years,
            iv=iv,
            risk_free_rate=settings.risk_free_rate,
            base_spread_bps=candidate.costs.base_spread_bps,
            utilization=candidate.utilization,
        )
        gross_premium = premium_per_weth * amount
        net_before_cost = gross_premium * (
            1 - candidate.costs.execution_slippage_bps / 10_000
        )
        execution_cost = (
            collateral * candidate.costs.fee_bps_notional / 10_000
            + candidate.costs.gas_usdc
        )
        net_premium = net_before_cost - execution_cost
        net_premium_bps = net_premium / collateral * 10_000
        if net_premium <= 0 or (net_premium_bps < candidate.minimum_net_premium_bps):
            ledger.skipped_minimum_premium += 1
            _record_interval_nav(
                series=series,
                settings=settings,
                ledger=ledger,
                position=None,
                start=execution,
                end=expiry_dt,
                nav_marks=nav_marks,
            )
            continue

        ledger.cash_usdc += net_premium
        opened_strike_distances.append(1 - strike / spot)
        opened_absolute_put_deltas.append(
            abs(
                bs_delta(
                    True,
                    spot,
                    strike,
                    time_years,
                    settings.risk_free_rate,
                    iv,
                )
            )
        )
        ledger.premium_net_usdc += net_premium
        ledger.estimated_costs_usdc += (
            gross_premium - net_before_cost
        ) + execution_cost
        ledger.positions_opened += 1
        position = OptionPosition(
            position_id=position_counter,
            is_put=True,
            strike=strike,
            amount_eth=amount,
            opened_at=opened_at,
            expiry=expiry,
            premium_gross_usdc=gross_premium,
            premium_net_usdc=net_premium,
            pricing_source="modeled",
        )
        position_counter += 1
        _record_interval_nav(
            series=series,
            settings=settings,
            ledger=ledger,
            position=position,
            start=execution,
            end=expiry_dt,
            nav_marks=nav_marks,
        )
        ledger.positions_settled += 1
        if settlement_spot.value < strike:
            acquisition = strike * amount
            ledger.cash_usdc -= acquisition
            ledger.weth_amount += amount
            ledger.assignments += 1
            ledger.collateral_assigned_usdc += acquisition
            ledger.assigned_weth_amount += amount
        settlement_nav = _nav(
            ledger=ledger,
            spot=settlement_spot.value,
        )
        nav_marks.append((expiry_ms, settlement_nav))
        ledger.peak_weth_nav_fraction = max(
            ledger.peak_weth_nav_fraction,
            _weth_nav_fraction(ledger, settlement_spot.value),
        )
        if ledger.cash_usdc < -1e-6 or ledger.weth_amount < -1e-12:
            raise AssertionError("physical CSP ledger produced negative assets")

    final_spot = series.spot_at(
        utc_timestamp_ms(end),
        settings.coverage_gate.maximum_spot_age_hours,
    )
    if final_spot is None:
        raise RuntimeError("Missing final spot")
    final_nav = _nav(ledger=ledger, spot=final_spot.value)
    nav_marks.append((utc_timestamp_ms(end), final_nav))
    ending_weth_fraction = _weth_nav_fraction(ledger, final_spot.value)
    missing_fraction = ledger.missing_market_events / decisions if decisions else 1.0
    result = {
        "candidate_id": candidate.candidate_id,
        "asset": series.asset,
        "strategy": "standalone_physical_csp",
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": end.isoformat(),
        "initial_usdc": settings.initial_usdc,
        "final_nav_usdc": final_nav,
        "absolute_return": final_nav / settings.initial_usdc - 1,
        "maximum_drawdown": _drawdown([value for _, value in nav_marks]),
        "cash_usdc": ledger.cash_usdc,
        "ending_weth": ledger.weth_amount,
        "ending_weth_nav_fraction": ending_weth_fraction,
        "peak_weth_nav_fraction": ledger.peak_weth_nav_fraction,
        "premium_net_usdc": ledger.premium_net_usdc,
        "premium_observed_usdc": 0.0,
        "premium_modeled_usdc": ledger.premium_net_usdc,
        "estimated_costs_usdc": ledger.estimated_costs_usdc,
        "collateral_assigned_usdc": ledger.collateral_assigned_usdc,
        "assigned_weth_amount": ledger.assigned_weth_amount,
        "positions_opened": ledger.positions_opened,
        "positions_settled": ledger.positions_settled,
        "assignments": ledger.assignments,
        "assignment_frequency": (
            ledger.assignments / ledger.positions_settled
            if ledger.positions_settled
            else 0.0
        ),
        "decisions": decisions,
        "eligible_decisions": ledger.eligible_decisions,
        "open_rate": ledger.positions_opened / decisions if decisions else 0.0,
        "median_opened_strike_distance_below_spot": (
            statistics.median(opened_strike_distances)
            if opened_strike_distances
            else None
        ),
        "minimum_opened_strike_distance_below_spot": (
            min(opened_strike_distances) if opened_strike_distances else None
        ),
        "maximum_opened_strike_distance_below_spot": (
            max(opened_strike_distances) if opened_strike_distances else None
        ),
        "median_opened_absolute_put_delta": (
            statistics.median(opened_absolute_put_deltas)
            if opened_absolute_put_deltas
            else None
        ),
        "skipped_minimum_premium": ledger.skipped_minimum_premium,
        "skipped_weth_inventory": ledger.skipped_weth_inventory,
        "skipped_insufficient_collateral": ledger.skipped_insufficient_collateral,
        "missing_market_events": ledger.missing_market_events,
        "missing_market_fraction": missing_fraction,
        "strike_rule": candidate.strike_rule,
        "strike_parameter": candidate.strike_parameter,
        "utilization": candidate.utilization,
        "minimum_net_premium_bps": candidate.minimum_net_premium_bps,
        "costs": candidate.costs.name,
        "pricing_observation_class": "modeled",
        "assignment_settlement": "physical_weth_inventory",
        "cash_settlement_used": False,
        "covered_calls_used": False,
        "deallocation_used": False,
        "final_nav_reconciliation_error_usdc": (
            final_nav - (ledger.cash_usdc + ledger.weth_amount * final_spot.value)
        ),
    }
    return result


def window_end_times(
    *,
    start: datetime,
    end: datetime,
    window_days: int,
    step_days: int,
) -> list[datetime]:
    current = start + timedelta(days=window_days)
    values = []
    while current <= end:
        values.append(current)
        current += timedelta(days=step_days)
    return values


def summarize_candidate(
    *,
    rows: list[dict[str, Any]],
    decision_gates: dict[str, Any],
) -> dict[str, Any]:
    windows = []
    checks_passed = 0
    checks_total = 0
    for window_days in decision_gates["window_days"]:
        group = [row for row in rows if int(row["window_days"]) == window_days]
        returns = [float(row["absolute_return"]) for row in group]
        drawdowns = [float(row["maximum_drawdown"]) for row in group]
        open_rates = [float(row["open_rate"]) for row in group]
        assignment_frequencies = [float(row["assignment_frequency"]) for row in group]
        total_assignments = sum(int(row["assignments"]) for row in group)
        total_settled_positions = sum(int(row["positions_settled"]) for row in group)
        median_strike_distances = [
            float(row["median_opened_strike_distance_below_spot"])
            for row in group
            if row.get("median_opened_strike_distance_below_spot") is not None
        ]
        minimum_strike_distances = [
            float(row["minimum_opened_strike_distance_below_spot"])
            for row in group
            if row.get("minimum_opened_strike_distance_below_spot") is not None
        ]
        maximum_strike_distances = [
            float(row["maximum_opened_strike_distance_below_spot"])
            for row in group
            if row.get("maximum_opened_strike_distance_below_spot") is not None
        ]
        median_absolute_put_deltas = [
            float(row["median_opened_absolute_put_delta"])
            for row in group
            if row.get("median_opened_absolute_put_delta") is not None
        ]
        missing = [float(row["missing_market_fraction"]) for row in group]
        hurdle = (
            1
            + float(decision_gates["benchmark_usdc_apy"])
            + float(decision_gates["minimum_risk_premium_apy"])
        ) ** (window_days / 365) - 1
        distribution = _distribution(returns)
        loss_probability = (
            sum(value < 0 for value in returns) / len(returns) if returns else 1.0
        )
        expected_shortfall_5 = (
            statistics.fmean(sorted(returns)[: max(1, math.ceil(len(returns) * 0.05))])
            if returns
            else -1.0
        )
        worst_drawdown = min(drawdowns, default=-1.0)
        median_open_rate = statistics.median(open_rates) if open_rates else 0.0
        median_assignment_frequency = (
            statistics.median(assignment_frequencies) if assignment_frequencies else 0.0
        )
        aggregate_assignment_frequency = (
            total_assignments / total_settled_positions
            if total_settled_positions
            else 0.0
        )
        maximum_missing = max(missing, default=1.0)
        checks = {
            "median_return_above_hurdle": distribution["p50"] >= hurdle,
            "loss_probability_within_limit": (
                loss_probability <= float(decision_gates["maximum_loss_probability"])
            ),
            "worst_drawdown_within_limit": (
                worst_drawdown >= float(decision_gates["maximum_worst_drawdown"])
            ),
            "minimum_open_rate_met": (
                median_open_rate >= float(decision_gates["minimum_open_rate"])
            ),
            "market_coverage_within_limit": (
                maximum_missing
                <= float(decision_gates["maximum_missing_market_fraction"])
            ),
        }
        checks_passed += sum(checks.values())
        checks_total += len(checks)
        windows.append(
            {
                "window_days": window_days,
                "sample_count": len(group),
                "hurdle_return": hurdle,
                "return_distribution": distribution,
                "loss_probability": loss_probability,
                "expected_shortfall_5": expected_shortfall_5,
                "worst_maximum_drawdown": worst_drawdown,
                "median_open_rate": median_open_rate,
                "median_assignment_frequency": median_assignment_frequency,
                "aggregate_assignment_frequency": aggregate_assignment_frequency,
                "median_opened_strike_distance_below_spot": (
                    statistics.median(median_strike_distances)
                    if median_strike_distances
                    else None
                ),
                "minimum_opened_strike_distance_below_spot": (
                    min(minimum_strike_distances) if minimum_strike_distances else None
                ),
                "maximum_opened_strike_distance_below_spot": (
                    max(maximum_strike_distances) if maximum_strike_distances else None
                ),
                "median_opened_absolute_put_delta": (
                    statistics.median(median_absolute_put_deltas)
                    if median_absolute_put_deltas
                    else None
                ),
                "maximum_missing_market_fraction": maximum_missing,
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "candidate_id": rows[0]["candidate_id"] if rows else None,
        "candidate": (
            {
                key: rows[0][key]
                for key in (
                    "strike_rule",
                    "strike_parameter",
                    "utilization",
                    "minimum_net_premium_bps",
                    "costs",
                )
            }
            if rows
            else None
        ),
        "windows": windows,
        "economic_gate_count": checks_passed,
        "economic_gate_total": checks_total,
        "all_economic_gates_pass": bool(windows)
        and all(window["passed"] for window in windows),
        "all_activity_gates_pass": bool(windows)
        and all(window["checks"]["minimum_open_rate_met"] for window in windows),
        "worst_loss_probability": max(
            (float(window["loss_probability"]) for window in windows),
            default=1.0,
        ),
        "worst_drawdown": min(
            (float(window["worst_maximum_drawdown"]) for window in windows),
            default=-1.0,
        ),
        "minimum_median_excess_return": min(
            (
                float(window["return_distribution"]["p50"])
                - float(window["hurdle_return"])
                for window in windows
            ),
            default=-1.0,
        ),
    }


def selection_sort_key(summary: dict[str, Any]) -> tuple[Any, ...]:
    candidate = summary["candidate"]
    return (
        -int(summary["all_economic_gates_pass"]),
        -int(summary["all_activity_gates_pass"]),
        -int(summary["economic_gate_count"]),
        float(summary["worst_loss_probability"]),
        -float(summary["worst_drawdown"]),
        -float(summary["minimum_median_excess_return"]),
        float(candidate["utilization"]),
        -int(candidate["minimum_net_premium_bps"]),
        str(summary["candidate_id"]),
    )


def build_candidates(
    *,
    config: dict[str, Any],
    settings: BacktestSettings,
    cost_name: str,
) -> list[PhysicalCspCandidate]:
    family = config["candidate_family"]
    costs = next(item for item in settings.cost_scenarios if item.name == cost_name)
    strike_rules = [
        ("fixed_moneyness_below_spot", float(value))
        for value in family["fixed_moneyness_below_spot"]
    ] + [("target_put_delta", float(value)) for value in family["target_put_deltas"]]
    return [
        PhysicalCspCandidate(
            strike_rule=strike_rule,
            strike_parameter=strike_parameter,
            utilization=float(utilization),
            minimum_net_premium_bps=int(premium),
            costs=costs,
        )
        for strike_rule, strike_parameter in strike_rules
        for utilization in family["utilizations"]
        for premium in family["minimum_net_premium_bps"]
    ]


def candidate_from_summary(
    *,
    summary: dict[str, Any],
    settings: BacktestSettings,
    cost_name: str,
) -> PhysicalCspCandidate:
    raw = summary["candidate"]
    costs = next(item for item in settings.cost_scenarios if item.name == cost_name)
    return PhysicalCspCandidate(
        strike_rule=str(raw["strike_rule"]),
        strike_parameter=float(raw["strike_parameter"]),
        utilization=float(raw["utilization"]),
        minimum_net_premium_bps=int(raw["minimum_net_premium_bps"]),
        costs=costs,
    )


def _testnet_validation_passed(summary: dict[str, Any]) -> bool:
    """Require bounded operation, not investment performance, for mock testnet."""
    for window in summary["windows"]:
        checks = window["checks"]
        if not (
            checks["worst_drawdown_within_limit"]
            and checks["minimum_open_rate_met"]
            and checks["market_coverage_within_limit"]
        ):
            return False
    return True


def build_policy(
    *,
    config: dict[str, Any],
    development: dict[str, Any],
    validation_base: dict[str, Any],
    validation_stressed: dict[str, Any],
    source_digest: str,
) -> dict[str, Any]:
    evidence = config["evidence_gates"]
    every_evidence_gate = all(bool(value) for value in evidence.values())
    economic_go = (
        development["all_economic_gates_pass"]
        and validation_base["all_economic_gates_pass"]
        and validation_stressed["all_economic_gates_pass"]
        and every_evidence_gate
    )
    selected = validation_base["candidate"]
    bounds = config["base_sepolia_validation_bounds"]
    testnet_compatible = (
        float(selected["utilization"]) <= float(bounds["maximum_utilization"])
        and _testnet_validation_passed(validation_base)
        and _testnet_validation_passed(validation_stressed)
    )
    if economic_go:
        decision = "go"
    elif testnet_compatible:
        decision = "base_sepolia_validation_go"
    else:
        decision = "no_go"
    allocator_enabled = decision in {"go", "base_sepolia_validation_go"}
    return {
        "schema_version": 2,
        "policy_id": "eth_usdc_standalone_csp_fund",
        "decision": decision,
        "activation_allowed": allocator_enabled,
        "authorization_scope": (
            "base_sepolia_low_cap_validation"
            if decision == "base_sepolia_validation_go"
            else "economic"
            if decision == "go"
            else "none"
        ),
        "mainnet_authorized": decision == "go" and bool(bounds["mainnet_authorized"]),
        "economic_decision": "go" if economic_go else "no_go",
        "testnet_validation_decision": "go" if testnet_compatible else "no_go",
        "allocator_actions": {
            "open_new_positions": allocator_enabled,
            "network": (
                "base_sepolia"
                if allocator_enabled and not economic_go
                else "base"
                if economic_go
                else "none"
            ),
            "purpose": (
                "collect_observed_operational_evidence"
                if decision == "base_sepolia_validation_go"
                else "economic_operation"
                if decision == "go"
                else "none"
            ),
        },
        "selection": {
            "target_duration_hours": int(config["scope"]["cadence_hours"]),
            "reopen_cadence_hours": int(config["scope"]["cadence_hours"]),
            "strike_rule": str(selected["strike_rule"]),
            "strike_parameter": float(selected["strike_parameter"]),
            "strike_rounding": (
                "down_to_exchange_increment"
                if selected["strike_rule"] == "fixed_moneyness_below_spot"
                else "nearest_exchange_increment_from_target_delta"
            ),
            "maximum_utilization": float(selected["utilization"]),
            "minimum_net_premium_bps": int(selected["minimum_net_premium_bps"]),
            "maximum_weth_nav_fraction": float(
                config["fund_policy"]["maximum_weth_nav_fraction_for_new_entry"]
            ),
            "deallocation_enabled": False,
            "covered_calls_enabled": False,
        },
        "base_sepolia_overrides": {
            **bounds,
            "allocator_enabled": allocator_enabled,
            "maximum_collateral_per_position_usdc": (
                float(bounds["maximum_collateral_per_position_usdc"])
                if allocator_enabled
                else 0.0
            ),
            "maximum_vault_aum_usdc": (
                float(bounds["maximum_vault_aum_usdc"]) if allocator_enabled else 0.0
            ),
            "maximum_utilization": (
                float(selected["utilization"]) if allocator_enabled else 0.0
            ),
        },
        "economic_gate_results": {
            "development": development,
            "validation_base": validation_base,
            "validation_stressed": validation_stressed,
        },
        "evidence_gates": evidence,
        "unresolved_evidence": [
            key for key, value in evidence.items() if not bool(value)
        ],
        "evidence": {
            "issue": config["issue"],
            "source_market_sha256": source_digest,
            "config_path": "backtests/b1n_356/config.json",
            "summary_path": "backtests/b1n_356/results/summary.json",
            "report_path": "backtests/b1n_356/results/REPORT.md",
        },
        "runtime_fail_closed_reasons": [
            "NETWORK_SCOPE_MISMATCH",
            "STALE_NAV",
            "MISSING_OR_UNAPPROVED_QUOTE",
            "CONFIG_MISMATCH",
            "CAP_BREACH",
            "AUM_CAP_BREACH",
            "MAXIMUM_POSITIONS_REACHED",
            "VALIDATION_CYCLE_LIMIT_REACHED",
            "VALIDATION_ASSIGNMENT_LIMIT_REACHED",
            "WETH_INVENTORY_LIMIT",
            "INSUFFICIENT_DEPLOYABLE_USDC",
            "UNTRUSTED_REPORT",
            "POLICY_VERSION_MISMATCH",
        ],
        "notes": (
            "Modeled premiums cannot establish economic or mainnet readiness. "
            "A Base Sepolia validation authorization, when present, exists only "
            "with mock test assets to collect observed quote, assignment, fund-flow "
            "and NAV evidence. Failing return or loss-probability gates remain an "
            "economic no-go and are not waived by testnet authorization."
        ),
    }


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    if not rows:
        return
    with path.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=sorted(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    *,
    path: Path,
    summary: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    selected = summary["selected_development"]
    lines = [
        "# B1N-356 CSP Fund policy v2 validation",
        "",
        f"**Decision: `{policy['decision']}`.**",
        "",
        f"Economic decision: **`{policy['economic_decision']}`**. "
        f"Base Sepolia validation decision: "
        f"**`{policy['testnet_validation_decision']}`**.",
        "",
        "This research models premiums and does not claim observed Binary fills. "
        "ITM puts settle into physical WETH inventory; covered calls and automatic "
        "WETH deallocation are disabled.",
        "",
        "## Selected candidate",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        f"| Strike rule | {selected['candidate']['strike_rule']} |",
        f"| Strike parameter | {selected['candidate']['strike_parameter']:.0%} |",
        f"| Utilization | {selected['candidate']['utilization']:.0%} |",
        f"| Minimum net premium | {selected['candidate']['minimum_net_premium_bps']} bps |",
        "| Cadence | 48 hours |",
        "| WETH inventory cap for new entries | 25% NAV |",
        "",
        "## Authorization scope",
        "",
        "The selected candidate remains an economic `no_go`. The authorization is "
        "limited to Base Sepolia operational validation with mock assets:",
        "",
        "| Bound | Value |",
        "|---|---:|",
        "| Network | Base Sepolia only |",
        f"| Chain ID | {policy['base_sepolia_overrides']['chain_id']} |",
        f"| Assets | "
        f"{'Mock only' if policy['base_sepolia_overrides']['mock_assets_only'] else 'Unrestricted'} |",
        f"| Maximum vault AUM | "
        f"{policy['base_sepolia_overrides']['maximum_vault_aum_usdc']:.2f} USDC |",
        f"| Maximum collateral per position | "
        f"{policy['base_sepolia_overrides']['maximum_collateral_per_position_usdc']:.2f} USDC |",
        f"| Maximum simultaneous positions | "
        f"{policy['base_sepolia_overrides']['maximum_positions']} |",
        f"| Maximum positions before review | "
        f"{policy['base_sepolia_overrides']['maximum_opened_positions_before_review']} |",
        f"| Maximum assignments before review | "
        f"{policy['base_sepolia_overrides']['maximum_assignments_before_review']} |",
        "| Mainnet | Not authorized |",
        "",
        "## Validation comparison",
        "",
        "Each row uses the development winner for its exact strike variant.",
        "",
        "| Rule | Parameter | Actual distance | Actual delta | Cost | Window | N | Median | Loss probability | Worst DD | Open rate | Assignment | Gate |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for comparison in summary["validation_comparison"].values():
        for cost_key in ("validation_base", "validation_stressed"):
            result = comparison[cost_key]
            candidate = result["candidate"]
            for window in result["windows"]:
                distance = window["median_opened_strike_distance_below_spot"]
                distance_label = (
                    f"{distance:.1%}" if distance is not None else "No opens"
                )
                delta = window["median_opened_absolute_put_delta"]
                delta_label = f"{delta:.3f}" if delta is not None else "No opens"
                lines.append(
                    f"| {candidate['strike_rule']} | "
                    f"{candidate['strike_parameter']:.0%} | "
                    f"{distance_label} | "
                    f"{delta_label} | "
                    f"{candidate['costs']} | {window['window_days']}d | "
                    f"{window['sample_count']} | "
                    f"{window['return_distribution']['p50']:.2%} | "
                    f"{window['loss_probability']:.1%} | "
                    f"{window['worst_maximum_drawdown']:.2%} | "
                    f"{window['median_open_rate']:.1%} | "
                    f"{window['aggregate_assignment_frequency']:.1%} | "
                    f"{'PASS' if window['passed'] else 'FAIL'} |"
                )
    lines.extend(
        [
            "",
            "## Evidence boundary",
            "",
            "The backtest can validate causal accounting and risk behavior, but it "
            "cannot turn modeled premiums into observed executable liquidity. The "
            "following evidence remains explicit:",
            "",
        ]
    )
    for name, value in policy["evidence_gates"].items():
        lines.append(f"- `{name}`: {'present' if value else 'missing'}")
    lines.extend(
        [
            "",
            "A `base_sepolia_validation_go`, if issued, is testnet-only and capped. "
            "It is intended to gather the missing evidence and is not an economic "
            "or mainnet recommendation. It does not waive failed return or "
            "loss-probability gates.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def write_checksums(output_dir: Path, names: tuple[str, ...]) -> None:
    lines = []
    for name in names:
        digest = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    (output_dir / "checksums.sha256").write_text("".join(lines))


def dataclass_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
