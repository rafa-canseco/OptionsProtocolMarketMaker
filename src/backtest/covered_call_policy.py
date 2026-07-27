"""Reproducible WETH-accounted covered-call policy research for B1N-358."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.backtest.config import BacktestSettings
from src.backtest.data import MarketSeries, utc_timestamp_ms
from src.backtest.engine import binary_bid_premium, select_strike
from src.backtest.models import CostScenario
from src.pricer import apply_vol_skew, bs_delta, bs_price


@dataclass(frozen=True)
class CoveredCallCandidate:
    strike_rule: str
    strike_parameter: float
    utilization: float
    minimum_net_premium_bps: int
    normalization_slippage_bps: int
    costs: CostScenario

    @property
    def candidate_id(self) -> str:
        parameter = f"{self.strike_parameter:.6g}".replace(".", "p")
        utilization = (
            f"{self.utilization:.4f}".rstrip("0").rstrip(".").replace(".", "p")
        )
        return (
            f"{self.strike_rule}_{parameter}__u_{utilization}"
            f"__p_{self.minimum_net_premium_bps}__{self.costs.name}"
        )


@dataclass
class CoveredCallPosition:
    strike: float
    amount_weth: float
    opened_at: datetime
    expiry: datetime
    premium_usdc: float


def _quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values, default=0.0),
        "p05": _quantile(values, 0.05),
        "p25": _quantile(values, 0.25),
        "p50": _quantile(values, 0.50),
        "p75": _quantile(values, 0.75),
        "p95": _quantile(values, 0.95),
        "max": max(values, default=0.0),
        "mean": statistics.fmean(values) if values else 0.0,
    }


def fixed_moneyness_call_strike(
    spot: float, distance_above_spot: float, strike_increment: float
) -> float:
    if spot <= 0 or distance_above_spot <= 0 or strike_increment <= 0:
        raise ValueError("invalid fixed-moneyness strike inputs")
    raw = spot * (1 + distance_above_spot)
    return math.ceil(raw / strike_increment) * strike_increment


def candidate_call_strike(
    *,
    candidate: CoveredCallCandidate,
    spot: float,
    iv: float,
    time_years: float,
    risk_free_rate: float,
    strike_increment: float,
) -> float | None:
    if candidate.strike_rule == "fixed_moneyness_above_spot":
        return fixed_moneyness_call_strike(
            spot, candidate.strike_parameter, strike_increment
        )
    if candidate.strike_rule == "target_call_delta":
        return select_strike(
            is_put=False,
            spot=spot,
            iv=iv,
            time_years=time_years,
            target_delta=candidate.strike_parameter,
            risk_free_rate=risk_free_rate,
            strike_increment=strike_increment,
        )
    raise ValueError(f"unsupported strike rule: {candidate.strike_rule}")


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


def _maximum_drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def _nav_weth(
    *,
    idle_weth: float,
    locked_weth: float,
    usdc: float,
    position: CoveredCallPosition | None,
    at: datetime,
    spot: float,
    iv: float,
    risk_free_rate: float,
) -> float:
    liability_usdc = 0.0
    if position is not None:
        seconds = max((position.expiry - at).total_seconds(), 0.0)
        time_years = seconds / (365 * 86_400)
        liability_usdc = (
            bs_price(
                False,
                spot,
                position.strike,
                time_years,
                risk_free_rate,
                apply_vol_skew(iv, spot, position.strike, False),
            )
            * position.amount_weth
        )
    return idle_weth + locked_weth + (usdc - liability_usdc) / spot


def run_covered_call_window(
    *,
    series: MarketSeries,
    settings: BacktestSettings,
    candidate: CoveredCallCandidate,
    start: datetime,
    window_days: int,
    initial_weth: float,
) -> dict[str, Any]:
    """Replay one physical WETH→USDC→WETH covered-call window."""
    end = start + timedelta(days=window_days)
    cadence = timedelta(hours=settings.cadence_hours)
    time_years = settings.cadence_hours / 24 / 365
    current = start
    next_open = start
    idle_weth = initial_weth
    locked_weth = 0.0
    transient_usdc = 0.0
    position: CoveredCallPosition | None = None
    opens = 0
    settlements = 0
    called_away = 0
    opportunities = 0
    missing = 0
    premiums_usdc = 0.0
    normalization_costs_usdc = 0.0
    strike_distances: list[float] = []
    opened_deltas: list[float] = []
    nav_marks: list[float] = []
    start_spot: float | None = None
    end_spot: float | None = None

    while current <= end:
        market = _market(series, settings, current)
        if market is None:
            missing += 1
            current += timedelta(hours=1)
            continue
        spot, iv = market
        start_spot = start_spot or spot
        end_spot = spot

        if position is not None and current >= position.expiry:
            settlements += 1
            if spot > position.strike:
                called_away += 1
                transient_usdc += position.strike * position.amount_weth
            else:
                idle_weth += locked_weth
            locked_weth = 0.0
            position = None

            gross_usdc = transient_usdc
            fixed_cost = candidate.costs.gas_usdc
            proportional_cost = (
                gross_usdc * candidate.normalization_slippage_bps / 10_000
            )
            normalization_costs_usdc += min(gross_usdc, fixed_cost + proportional_cost)
            net_usdc = max(gross_usdc - fixed_cost - proportional_cost, 0.0)
            idle_weth += net_usdc / spot
            transient_usdc = 0.0
            next_open = current

        if position is None and current >= next_open and current < end:
            opportunities += 1
            strike = candidate_call_strike(
                candidate=candidate,
                spot=spot,
                iv=iv,
                time_years=time_years,
                risk_free_rate=settings.risk_free_rate,
                strike_increment=settings.strike_increment_usd,
            )
            if strike is not None:
                amount = idle_weth * candidate.utilization
                premium_per_weth, _, skewed_iv = binary_bid_premium(
                    is_put=False,
                    spot=spot,
                    strike=strike,
                    time_years=time_years,
                    iv=iv,
                    risk_free_rate=settings.risk_free_rate,
                    base_spread_bps=candidate.costs.base_spread_bps,
                    utilization=candidate.utilization,
                )
                gross_premium = premium_per_weth * amount
                opening_cost = (
                    amount * spot * candidate.costs.fee_bps_notional / 10_000
                    + candidate.costs.gas_usdc
                )
                net_premium = max(
                    gross_premium
                    * (1 - candidate.costs.execution_slippage_bps / 10_000)
                    - opening_cost,
                    0.0,
                )
                premium_bps = (
                    net_premium * 10_000 / (amount * spot) if amount > 0 else 0.0
                )
                if amount > 0 and premium_bps >= candidate.minimum_net_premium_bps:
                    idle_weth -= amount
                    locked_weth = amount
                    transient_usdc += net_premium
                    premiums_usdc += net_premium
                    opens += 1
                    expiry = current + cadence
                    position = CoveredCallPosition(
                        strike=strike,
                        amount_weth=amount,
                        opened_at=current,
                        expiry=expiry,
                        premium_usdc=net_premium,
                    )
                    strike_distances.append(strike / spot - 1)
                    opened_deltas.append(
                        bs_delta(
                            False,
                            spot,
                            strike,
                            time_years,
                            settings.risk_free_rate,
                            skewed_iv,
                        )
                    )
            next_open = current + cadence

        nav_marks.append(
            _nav_weth(
                idle_weth=idle_weth,
                locked_weth=locked_weth,
                usdc=transient_usdc,
                position=position,
                at=current,
                spot=spot,
                iv=iv,
                risk_free_rate=settings.risk_free_rate,
            )
        )
        current += timedelta(hours=1)

    final_nav_weth = nav_marks[-1] if nav_marks else 0.0
    total_hours = window_days * 24 + 1
    strategy_usd_return = (
        final_nav_weth * end_spot / (initial_weth * start_spot) - 1
        if start_spot and end_spot
        else -1.0
    )
    hold_usd_return = end_spot / start_spot - 1 if start_spot and end_spot else -1.0
    return {
        "candidate_id": candidate.candidate_id,
        "window_start": start.astimezone(UTC).isoformat(),
        "window_end": end.astimezone(UTC).isoformat(),
        "window_days": window_days,
        "strike_rule": candidate.strike_rule,
        "strike_parameter": candidate.strike_parameter,
        "utilization": candidate.utilization,
        "minimum_net_premium_bps": candidate.minimum_net_premium_bps,
        "normalization_slippage_bps": candidate.normalization_slippage_bps,
        "costs": asdict(candidate.costs),
        "initial_weth": initial_weth,
        "final_nav_weth": final_nav_weth,
        "weth_return": final_nav_weth / initial_weth - 1,
        "strategy_usd_return": strategy_usd_return,
        "hold_weth_usd_return": hold_usd_return,
        "upside_foregone_vs_hold": strategy_usd_return - hold_usd_return,
        "maximum_drawdown_weth": _maximum_drawdown(nav_marks),
        "positions_opened": opens,
        "positions_settled": settlements,
        "called_away": called_away,
        "call_away_frequency": called_away / settlements if settlements else 0.0,
        "open_rate": opens / opportunities if opportunities else 0.0,
        "premium_collected_usdc": premiums_usdc,
        "normalization_costs_usdc": normalization_costs_usdc,
        "median_opened_strike_distance_above_spot": (
            statistics.median(strike_distances) if strike_distances else None
        ),
        "median_opened_call_delta": (
            statistics.median(opened_deltas) if opened_deltas else None
        ),
        "missing_market_fraction": missing / total_hours,
    }


def window_starts(
    *, start: datetime, end: datetime, window_days: int, step_days: int
) -> list[datetime]:
    values = []
    current = start
    while current + timedelta(days=window_days) <= end:
        values.append(current)
        current += timedelta(days=step_days)
    return values


def summarize_candidate(
    rows: list[dict[str, Any]], decision_gates: dict[str, Any]
) -> dict[str, Any]:
    windows = []
    checks_passed = 0
    checks_total = 0
    for days in decision_gates["window_days"]:
        group = [row for row in rows if row["window_days"] == days]
        returns = [float(row["weth_return"]) for row in group]
        relative = [float(row["upside_foregone_vs_hold"]) for row in group]
        drawdowns = [float(row["maximum_drawdown_weth"]) for row in group]
        open_rates = [float(row["open_rate"]) for row in group]
        missing = [float(row["missing_market_fraction"]) for row in group]
        call_aways = sum(int(row["called_away"]) for row in group)
        settlements = sum(int(row["positions_settled"]) for row in group)
        loss_probability = (
            sum(value < 0 for value in returns) / len(returns) if returns else 1.0
        )
        checks = {
            "median_weth_return_nonnegative": (
                statistics.median(returns) >= 0 if returns else False
            ),
            "loss_probability_within_limit": loss_probability
            <= float(decision_gates["maximum_loss_probability"]),
            "worst_drawdown_within_limit": min(drawdowns, default=-1.0)
            >= float(decision_gates["maximum_worst_drawdown"]),
            "minimum_open_rate_met": statistics.median(open_rates)
            >= float(decision_gates["minimum_open_rate"])
            if open_rates
            else False,
            "market_coverage_within_limit": max(missing, default=1.0)
            <= float(decision_gates["maximum_missing_market_fraction"]),
        }
        checks_passed += sum(checks.values())
        checks_total += len(checks)
        windows.append(
            {
                "window_days": days,
                "sample_count": len(group),
                "weth_return_distribution": _distribution(returns),
                "relative_to_hold_distribution": _distribution(relative),
                "loss_probability": loss_probability,
                "worst_maximum_drawdown": min(drawdowns, default=-1.0),
                "median_open_rate": statistics.median(open_rates)
                if open_rates
                else 0.0,
                "aggregate_call_away_frequency": call_aways / settlements
                if settlements
                else 0.0,
                "maximum_missing_market_fraction": max(missing, default=1.0),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "candidate_id": rows[0]["candidate_id"] if rows else None,
        "candidate": {
            key: rows[0][key]
            for key in (
                "strike_rule",
                "strike_parameter",
                "utilization",
                "minimum_net_premium_bps",
                "normalization_slippage_bps",
                "costs",
            )
        }
        if rows
        else None,
        "windows": windows,
        "economic_gate_count": checks_passed,
        "economic_gate_total": checks_total,
        "all_economic_gates_pass": bool(windows)
        and all(window["passed"] for window in windows),
        "worst_loss_probability": max(
            (window["loss_probability"] for window in windows), default=1.0
        ),
        "worst_drawdown": min(
            (window["worst_maximum_drawdown"] for window in windows), default=-1.0
        ),
        "minimum_median_weth_return": min(
            (window["weth_return_distribution"]["p50"] for window in windows),
            default=-1.0,
        ),
    }


def selection_sort_key(summary: dict[str, Any]) -> tuple[Any, ...]:
    candidate = summary["candidate"]
    return (
        -int(summary["all_economic_gates_pass"]),
        -int(summary["economic_gate_count"]),
        float(summary["worst_loss_probability"]),
        -float(summary["worst_drawdown"]),
        -float(summary["minimum_median_weth_return"]),
        float(candidate["utilization"]),
        str(summary["candidate_id"]),
    )


def build_candidates(
    config: dict[str, Any],
    settings: BacktestSettings,
    cost_name: str,
) -> list[CoveredCallCandidate]:
    family = config["candidate_family"]
    costs = next(item for item in settings.cost_scenarios if item.name == cost_name)
    normalization_slippage_bps = int(family["normalization_slippage_bps"][cost_name])
    strikes = [
        ("fixed_moneyness_above_spot", float(value))
        for value in family["fixed_moneyness_above_spot"]
    ] + [("target_call_delta", float(value)) for value in family["target_call_deltas"]]
    return [
        CoveredCallCandidate(
            strike_rule=rule,
            strike_parameter=parameter,
            utilization=float(utilization),
            minimum_net_premium_bps=int(premium),
            normalization_slippage_bps=normalization_slippage_bps,
            costs=costs,
        )
        for rule, parameter in strikes
        for utilization in family["utilizations"]
        for premium in family["minimum_net_premium_bps"]
    ]


def candidate_from_summary(
    summary: dict[str, Any],
    settings: BacktestSettings,
    cost_name: str,
    config: dict[str, Any],
) -> CoveredCallCandidate:
    raw = summary["candidate"]
    costs = next(item for item in settings.cost_scenarios if item.name == cost_name)
    return CoveredCallCandidate(
        strike_rule=str(raw["strike_rule"]),
        strike_parameter=float(raw["strike_parameter"]),
        utilization=float(raw["utilization"]),
        minimum_net_premium_bps=int(raw["minimum_net_premium_bps"]),
        normalization_slippage_bps=int(
            config["candidate_family"]["normalization_slippage_bps"][cost_name]
        ),
        costs=costs,
    )


def build_policy(
    *,
    config: dict[str, Any],
    development: dict[str, Any],
    validation_base: dict[str, Any],
    validation_stressed: dict[str, Any],
    source_digest: str,
) -> dict[str, Any]:
    evidence = config["evidence_gates"]
    economic_go = (
        development["all_economic_gates_pass"]
        and validation_base["all_economic_gates_pass"]
        and validation_stressed["all_economic_gates_pass"]
        and all(bool(value) for value in evidence.values())
    )
    selected = validation_base["candidate"]
    testnet_checks = (
        "worst_drawdown_within_limit",
        "minimum_open_rate_met",
        "market_coverage_within_limit",
    )
    testnet_go = all(
        all(window["checks"][key] for key in testnet_checks)
        for summary in (validation_base, validation_stressed)
        for window in summary["windows"]
    )
    decision = (
        "go" if economic_go else "base_sepolia_validation_go" if testnet_go else "no_go"
    )
    active = decision != "no_go"
    bounds = config["base_sepolia_validation_bounds"]
    return {
        "schema_version": 2,
        "policy_id": "eth_weth_covered_call_fund",
        "authority_issue": "B1N-362",
        "decision": "go_testnet_only"
        if decision == "base_sepolia_validation_go"
        else decision,
        "research_decision": decision,
        "activation_allowed": active,
        "mainnet_authorized": economic_go and bool(bounds["mainnet_authorized"]),
        "scope": {
            "chain_id": 84532,
            "environment": "base_sepolia",
            "strategy": "covered_call",
            "underlying": "ETH",
            "accounting_asset": "WETH",
            "transient_asset": "USDC",
        },
        "selection": {
            "strike_rule": selected["strike_rule"],
            "strike_parameter": selected["strike_parameter"],
            "maximum_delta_deviation_bps": int(
                config["candidate_family"]["maximum_delta_deviation_bps"]
            ),
            "strike_tick_usd": int(config["scope"]["strike_tick_usd"]),
            "target_duration_hours": int(config["scope"]["cadence_hours"]),
            "reopen_cadence_hours": int(config["scope"]["cadence_hours"]),
            "target_utilization_bps": int(float(selected["utilization"]) * 10_000),
            "minimum_net_premium_bps": int(selected["minimum_net_premium_bps"]),
            "maximum_open_positions": 1,
            "called_away_action": "normalize_all_usdc_to_weth_then_reopen",
            "premium_action": "normalize_all_usdc_to_weth_after_settlement",
            "continuous_operation": "reopen_while_free_weth_and_no_pending_redemptions",
        },
        "base_sepolia_bounds": {
            **bounds,
            "allocator_enabled": active,
        },
        "valuation": config["valuation"],
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
            "issue": "B1N-358",
            "source_market_sha256": source_digest,
            "config_path": "backtests/b1n_358/config.json",
            "summary_path": "backtests/b1n_358/results/summary.json",
            "report_path": "backtests/b1n_358/results/REPORT.md",
        },
        "runtime_fail_closed_reasons": [
            "NETWORK_SCOPE_MISMATCH",
            "STALE_NAV",
            "PENDING_REDEMPTIONS",
            "ACTIVE_FUND_FLOW_PROCESSING",
            "MISSING_OR_UNAPPROVED_QUOTE",
            "CONFIG_MISMATCH",
            "CAP_BREACH",
            "MAXIMUM_POSITIONS_REACHED",
            "VALIDATION_CYCLE_LIMIT_REACHED",
            "VALIDATION_CALL_AWAY_LIMIT_REACHED",
            "UNRESOLVED_USDC",
            "NORMALIZATION_SLIPPAGE",
            "PENDING_PHYSICAL_DELIVERY",
            "UNTRUSTED_REPORT",
        ],
        "notes": (
            "Premiums and normalization remain modeled. Economic/mainnet readiness "
            "therefore remains no-go unless every evidence gate is satisfied. The "
            "testnet profile exists only to validate the WETH-only physical lifecycle."
        ),
    }


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def write_report(path: Path, summary: dict[str, Any], policy: dict[str, Any]) -> None:
    selected = summary["selected_development"]
    base = summary["validation_base"]
    stress = summary["validation_stressed"]
    lines = [
        "# B1N-358 — 48-hour ETH Covered Call Policy",
        "",
        f"- Research decision: `{policy['research_decision']}`",
        f"- Runtime decision: `{policy['decision']}`",
        f"- Selected on development only: `{selected['candidate_id']}`",
        "- Accounting asset: WETH; USDC is transient and normalized only after settlement.",
        "- Premium source: modeled Binary bid, not observed executable liquidity.",
        "- Fair NAV: explicit `b1nary-european-bs-call-v1`; full collateral is stress telemetry only.",
        "",
        "## 30/90-day validation",
        "",
        "| Scenario | Window | Samples | Median WETH return | Loss probability | Worst drawdown | Call-away | Open rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, result in (("base", base), ("stressed", stress)):
        for window in result["windows"]:
            lines.append(
                "| "
                f"{label} | {window['window_days']} | {window['sample_count']} | "
                f"{window['weth_return_distribution']['p50']:.4%} | "
                f"{window['loss_probability']:.2%} | "
                f"{window['worst_maximum_drawdown']:.2%} | "
                f"{window['aggregate_call_away_frequency']:.2%} | "
                f"{window['median_open_rate']:.2%} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A call-away is represented physically: locked WETH leaves the strategy, "
            "strike proceeds arrive as USDC, and the full transient USDC balance is "
            "converted back to WETH before another call can open. NAV subtracts the "
            "current fair value of the live short call; locked WETH remains an asset.",
            "",
            "The versioned call mark uses IV 4,200 bps from the B1N-358-approved "
            "Deribit ETH ATM snapshot, risk-free rate 500 bps, settlement cost zero, "
            "the configured 8-decimal ETH/USD feed with a 3,600-second maximum age, "
            "two model-v1 observations, a 500-bps divergence cap and a 120-block "
            "maximum observation window. It is explicit covered-call policy and does "
            "not reuse CSP configuration.",
            "",
            "The Base Sepolia authorization is functional only. It does not waive the "
            "missing executable-liquidity, live physical-settlement, fund-flow, NAV, "
            "or normalization evidence needed for an economic/mainnet go.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def write_checksums(output_dir: Path, names: tuple[str, ...]) -> None:
    lines = []
    for name in names:
        digest = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (output_dir / "checksums.sha256").write_text("\n".join(lines) + "\n")
