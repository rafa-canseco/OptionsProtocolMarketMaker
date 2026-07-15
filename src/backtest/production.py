from __future__ import annotations

import json
import hashlib
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.backtest.config import BacktestSettings, ProductionValidationSettings
from src.backtest.data import MarketSeries, utc_timestamp_ms
from src.backtest.engine import hold_benchmarks, run_strategy
from src.backtest.models import StrategyConfig
from src.backtest.probe import decision_times
from src.backtest.reporting import write_results


def _validation(settings: BacktestSettings) -> ProductionValidationSettings:
    if settings.production_validation is None:
        raise RuntimeError("production_validation is missing from config")
    return settings.production_validation


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


def rolling_end_times(
    series: MarketSeries,
    window_days: int,
    step_days: int,
) -> list[datetime]:
    earliest = series.start + timedelta(days=window_days, hours=12)
    current = earliest.replace(hour=8, minute=0, second=0, microsecond=0)
    if current < earliest:
        current += timedelta(days=1)
    values = []
    while current <= series.cutoff:
        values.append(current)
        current += timedelta(days=step_days)
    return values


def _drawdown(values: list[float]) -> float:
    peak = -math.inf
    result = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            result = min(result, value / peak - 1)
    return result


def classify_regime(
    series: MarketSeries,
    settings: BacktestSettings,
    window_days: int,
    end: datetime,
) -> dict[str, Any]:
    validation = _validation(settings)
    start = end - timedelta(days=window_days)
    spots, ivs = series.observed_range(utc_timestamp_ms(start), utc_timestamp_ms(end))
    if len(spots) < 2 or not ivs:
        return {
            "regime": "missing",
            "volatility_crash": False,
            "underlying_period_return": None,
            "underlying_maximum_drawdown": None,
            "iv_spike": None,
        }
    underlying_return = spots[-1] / spots[0] - 1
    maximum_drawdown = _drawdown(spots)
    iv_spike = max(ivs) - statistics.median(ivs)
    threshold = validation.regime_return_thresholds[window_days]
    is_crash = (
        maximum_drawdown <= validation.crash_drawdown_threshold
        and iv_spike >= validation.crash_iv_spike_threshold
    )
    if underlying_return >= threshold:
        regime = "bull"
    elif underlying_return <= -threshold:
        regime = "bear"
    else:
        regime = "sideways"
    return {
        "regime": regime,
        "volatility_crash": is_crash,
        "underlying_period_return": underlying_return,
        "underlying_maximum_drawdown": maximum_drawdown,
        "iv_spike": iv_spike,
    }


def run_multiyear_probe(
    series_by_asset: dict[str, MarketSeries],
    settings: BacktestSettings,
    output_path: Path,
) -> dict[str, Any]:
    validation = _validation(settings)
    gate = settings.coverage_gate
    assets = []
    for asset, series in sorted(series_by_asset.items()):
        windows = []
        for window_days in settings.window_days:
            endpoints = rolling_end_times(
                series, window_days, validation.rolling_step_days
            )
            required = observed = 0
            for end in endpoints:
                for decision in decision_times(
                    series, window_days, settings.cadence_hours, end=end
                ):
                    execution = decision + timedelta(
                        minutes=max(
                            cost.operational_delay_minutes
                            for cost in settings.cost_scenarios
                        )
                    )
                    expiry = decision + timedelta(hours=settings.cadence_hours)
                    required += 1
                    valid = (
                        series.spot_at(
                            utc_timestamp_ms(execution), gate.maximum_spot_age_hours
                        )
                        is not None
                        and series.iv_at(
                            utc_timestamp_ms(execution), gate.maximum_iv_age_hours
                        )
                        is not None
                        and series.spot_at(
                            utc_timestamp_ms(expiry), gate.maximum_spot_age_hours
                        )
                        is not None
                    )
                    observed += int(valid)
            coverage = observed / required if required else 0.0
            missing_fraction = 1 - coverage
            windows.append(
                {
                    "window_days": window_days,
                    "rolling_windows": len(endpoints),
                    "required_events": required,
                    "observed_events": observed,
                    "modeled_premium_events": observed,
                    "missing_events": required - observed,
                    "causal_coverage": coverage,
                    "missing_fraction": missing_fraction,
                    "passed": (
                        coverage >= gate.minimum_causal_coverage
                        and missing_fraction <= gate.maximum_missing_fraction
                    ),
                }
            )
        assets.append({"asset": asset, "windows": windows})
    result = {
        "gate_defined_before_return_inspection": True,
        "minimum_causal_coverage": gate.minimum_causal_coverage,
        "maximum_missing_fraction": gate.maximum_missing_fraction,
        "assets": assets,
        "passed": all(
            window["passed"] for asset in assets for window in asset["windows"]
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def run_rolling_validation(
    series_by_asset: dict[str, MarketSeries],
    settings: BacktestSettings,
) -> list[dict[str, Any]]:
    validation = _validation(settings)
    costs = next(
        cost
        for cost in settings.cost_scenarios
        if cost.name == validation.cost_scenario
    )
    rows: list[dict[str, Any]] = []
    for asset, series in sorted(series_by_asset.items()):
        for window_days in settings.window_days:
            for end in rolling_end_times(
                series, window_days, validation.rolling_step_days
            ):
                regime = classify_regime(series, settings, window_days, end)
                for benchmark in hold_benchmarks(
                    series, settings, window_days, end=end
                ):
                    rows.append({**benchmark, **regime})
                for delta in validation.target_deltas:
                    config = StrategyConfig(
                        target_delta=delta,
                        utilization=validation.utilization,
                        minimum_premium_bps=validation.minimum_premium_bps,
                        call_margin_usd=validation.call_margin_usd,
                        protection_mode=validation.protection_mode,
                        costs=costs,
                    )
                    rows.append(
                        {
                            **run_strategy(
                                series=series,
                                settings=settings,
                                window_days=window_days,
                                config=config,
                                end=end,
                            ),
                            **regime,
                        }
                    )
                    rows.append(
                        {
                            **run_strategy(
                                series=series,
                                settings=settings,
                                window_days=window_days,
                                config=config,
                                csp_only=True,
                                end=end,
                            ),
                            **regime,
                        }
                    )
    return rows


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


def build_production_summary(
    rows: list[dict[str, Any]],
    settings: BacktestSettings,
) -> dict[str, Any]:
    validation = _validation(settings)
    wheel_rows = [row for row in rows if row["strategy"] == "wheel"]
    groups: dict[tuple[str, int, float], list[dict[str, Any]]] = defaultdict(list)
    for row in wheel_rows:
        groups[(row["asset"], row["window_days"], row["target_delta"])].append(row)
    policies = []
    for (asset, window_days, target_delta), group in sorted(groups.items()):
        returns = [float(row["absolute_return"]) for row in group]
        drawdowns = [float(row["maximum_drawdown"]) for row in group]
        mm_returns = [
            float(row["mm_hedged_return_on_initial_vault_aum"]) for row in group
        ]
        hurdle = (
            1 + validation.benchmark_usdc_apy + validation.minimum_risk_premium_apy
        ) ** (window_days / 365) - 1
        regimes = []
        for regime in ("bull", "bear", "sideways", "volatility_crash"):
            regime_group = [
                row
                for row in group
                if (
                    row.get("volatility_crash", False)
                    if regime == "volatility_crash"
                    else row["regime"] == regime
                )
            ]
            regime_returns = [float(row["absolute_return"]) for row in regime_group]
            regimes.append(
                {
                    "regime": regime,
                    "sample_count": len(regime_group),
                    "sufficient_sample": (
                        len(regime_group) >= validation.minimum_regime_samples
                    ),
                    "return_distribution": _distribution(regime_returns),
                    "loss_probability": (
                        sum(value < 0 for value in regime_returns) / len(regime_returns)
                        if regime_returns
                        else None
                    ),
                }
            )
        loss_probability = sum(value < 0 for value in returns) / len(returns)
        return_distribution = _distribution(returns)
        mm_distribution = _distribution(mm_returns)
        expected_shortfall_5 = statistics.fmean(
            sorted(returns)[: max(1, math.ceil(len(returns) * 0.05))]
        )
        checks = {
            "median_return_above_hurdle": return_distribution["p50"] >= hurdle,
            "loss_probability_within_limit": (
                loss_probability <= validation.maximum_loss_probability
            ),
            "worst_drawdown_within_limit": (
                min(drawdowns) >= validation.maximum_worst_drawdown
            ),
            "median_mm_hedged_return_nonnegative": (
                mm_distribution["p50"] >= validation.minimum_mm_hedged_return
            ),
            "all_regimes_sufficient": all(
                item["sufficient_sample"] for item in regimes
            ),
        }
        capacity = []
        for point in validation.capacity_curve:
            adjusted_vault_returns = [
                float(row["absolute_return"])
                - float(row["premium_net_usdc"])
                / float(row["initial_usdc"])
                * point.premium_haircut_bps
                / 10_000
                for row in group
            ]
            adjusted_mm_returns = [
                float(row["mm_hedged_return_on_initial_vault_aum"])
                + float(row["premium_net_usdc"])
                / float(row["initial_usdc"])
                * point.premium_haircut_bps
                / 10_000
                - float(row["mm_hedge_turnover_usdc"])
                / float(row["initial_usdc"])
                * point.hedge_cost_bps
                / 10_000
                for row in group
            ]
            vault_median = _quantile(adjusted_vault_returns, 0.5)
            mm_median = _quantile(adjusted_mm_returns, 0.5)
            capacity.append(
                {
                    "aum_usdc": point.aum_usdc,
                    "premium_haircut_bps": point.premium_haircut_bps,
                    "additional_hedge_cost_bps": point.hedge_cost_bps,
                    "vault_median_return": vault_median,
                    "mm_median_return_on_vault_aum": mm_median,
                    "vault_passes_hurdle": vault_median >= hurdle,
                    "mm_is_profitable": mm_median >= 0,
                    "jointly_viable": vault_median >= hurdle and mm_median >= 0,
                    "observation_class": "modeled",
                }
            )
        viable = [item["aum_usdc"] for item in capacity if item["jointly_viable"]]
        policies.append(
            {
                "asset": asset,
                "window_days": window_days,
                "target_delta": target_delta,
                "sample_count": len(group),
                "hurdle_return": hurdle,
                "return_distribution": return_distribution,
                "loss_probability": loss_probability,
                "expected_shortfall_5": expected_shortfall_5,
                "worst_maximum_drawdown": min(drawdowns),
                "mm_hedged_return_distribution": mm_distribution,
                "regimes": regimes,
                "checks": checks,
                "production_ready": all(checks.values()),
                "capacity": capacity,
                "maximum_modeled_jointly_viable_aum_usdc": max(viable, default=0),
            }
        )
    return {
        "warning": (
            "Research output only. Premiums, MM hedging and capacity are modeled; "
            "none are observed Binary fills. BTC is research-only and does not alter "
            "the ETH/USDC Milestone 1 scope."
        ),
        "methodology": {
            "rolling_step_days": validation.rolling_step_days,
            "benchmark_usdc_apy": validation.benchmark_usdc_apy,
            "minimum_risk_premium_apy": validation.minimum_risk_premium_apy,
            "maximum_loss_probability": validation.maximum_loss_probability,
            "maximum_worst_drawdown": validation.maximum_worst_drawdown,
            "minimum_mm_hedged_return": validation.minimum_mm_hedged_return,
            "minimum_regime_samples": validation.minimum_regime_samples,
        },
        "policies": policies,
        "production_ready_policy_count": sum(
            policy["production_ready"] for policy in policies
        ),
    }


def reclassify_rows(
    rows: list[dict[str, Any]],
    series_by_asset: dict[str, MarketSeries],
    settings: BacktestSettings,
) -> list[dict[str, Any]]:
    """Refresh regime metadata without rerunning option simulations."""
    cache: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["asset"], int(row["window_days"]), row["window_end"])
        if key not in cache:
            cache[key] = classify_regime(
                series_by_asset[row["asset"]],
                settings,
                int(row["window_days"]),
                datetime.fromisoformat(row["window_end"]),
            )
        row.update(cache[key])
    return rows


def write_production_outputs(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    probe: dict[str, Any],
    output_dir: Path,
) -> None:
    write_results(rows, output_dir, compressed=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    lines = [
        "# B1N-345 production validation — ETH and BTC",
        "",
        "> Research only. BTC does not change the ETH/USDC v2 Milestone 1 scope.",
        "",
        "All option premiums, MM hedging and capacity curves are modeled. Spot/perpetual",
        "closes and DVOL inputs are observed Deribit data. No Binary fills are claimed.",
        "",
        "## Coverage",
        "",
        "| Asset | Window | Rolling samples | Coverage | Gate |",
        "|---|---:|---:|---:|---|",
    ]
    for asset in probe["assets"]:
        for window in asset["windows"]:
            lines.append(
                f"| {asset['asset']} | {window['window_days']}d | "
                f"{window['rolling_windows']} | {window['causal_coverage']:.2%} | "
                f"{'PASS' if window['passed'] else 'FAIL'} |"
            )
    lines.extend(
        [
            "",
            "## Executive verdict",
            "",
            f"Production-ready fixed policies: **{summary['production_ready_policy_count']}**.",
            "A zero count means the research does not authorize allocator activation.",
            "The most common blocking checks are reported per policy in `summary.json`.",
            "",
            "## Fixed-policy distributions",
            "",
            "| Asset | Window | Delta | N | P5 | Median | P95 | Loss prob. | Worst DD | MM median | Capacity | Ready |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for policy in summary["policies"]:
        distribution = policy["return_distribution"]
        mm = policy["mm_hedged_return_distribution"]
        lines.append(
            f"| {policy['asset']} | {policy['window_days']}d | "
            f"{policy['target_delta']:.2f} | {policy['sample_count']} | "
            f"{distribution['p5']:.2%} | {distribution['p50']:.2%} | "
            f"{distribution['p95']:.2%} | {policy['loss_probability']:.1%} | "
            f"{policy['worst_maximum_drawdown']:.2%} | {mm['p50']:.2%} | "
            f"${policy['maximum_modeled_jointly_viable_aum_usdc']:,.0f} | "
            f"{'PASS' if policy['production_ready'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## Acceptance interpretation",
            "",
            "A policy passes only when its median beats the period-equivalent USDC hurdle,",
            "loss probability and worst drawdown remain within their fixed limits, median",
            "hedged MM PnL is non-negative, and every requested regime has enough samples.",
            "Capacity is a sensitivity model, not an observed liquidity limit.",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(lines))
    checksum_files = (
        "results.jsonl.gz",
        "results.csv.gz",
        "summary.json",
        "coverage_probe.json",
        "REPORT.md",
    )
    (output_dir / "checksums.sha256").write_text(
        "".join(
            f"{hashlib.sha256((output_dir / name).read_bytes()).hexdigest()}  {name}\n"
            for name in checksum_files
        )
    )
