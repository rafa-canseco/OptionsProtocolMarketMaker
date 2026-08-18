from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from src.backtest.config import BacktestSettings
from src.backtest.data import MarketSeries, utc_timestamp_ms


def decision_times(
    series: MarketSeries,
    window_days: int,
    cadence_hours: int,
    end=None,
):
    cutoff = end or series.cutoff
    start = cutoff - timedelta(days=window_days)
    timestamp = start.replace(hour=8, minute=0, second=0, microsecond=0)
    if timestamp < start:
        timestamp += timedelta(days=1)
    while timestamp < cutoff:
        yield timestamp
        timestamp += timedelta(hours=cadence_hours)


def run_coverage_probe(
    series: MarketSeries,
    settings: BacktestSettings,
    output_path: Path,
) -> dict:
    gate = settings.coverage_gate
    windows = []
    for window_days in settings.window_days:
        rows = []
        for decision in decision_times(series, window_days, settings.cadence_hours):
            execution = decision + timedelta(
                minutes=max(
                    cost.operational_delay_minutes for cost in settings.cost_scenarios
                )
            )
            expiry = decision + timedelta(hours=settings.cadence_hours)
            execution_ms = utc_timestamp_ms(execution)
            expiry_ms = utc_timestamp_ms(expiry)
            spot = series.spot_at(execution_ms, gate.maximum_spot_age_hours)
            iv = series.iv_at(execution_ms, gate.maximum_iv_age_hours)
            settlement = series.spot_at(expiry_ms, gate.maximum_spot_age_hours)
            valid = spot is not None and iv is not None and settlement is not None
            rows.append(
                {
                    "decision": decision.isoformat(),
                    "execution": execution.isoformat(),
                    "expiry": expiry.isoformat(),
                    "status": "observed" if valid else "missing",
                    "spot_age_hours": None if spot is None else spot.age_hours,
                    "iv_age_hours": None if iv is None else iv.age_hours,
                    "settlement_age_hours": (
                        None if settlement is None else settlement.age_hours
                    ),
                    "binary_premium_source": "modeled" if valid else "missing",
                }
            )
        observed = sum(row["status"] == "observed" for row in rows)
        total = len(rows)
        coverage = observed / total if total else 0.0
        missing_fraction = 1.0 - coverage
        passed = (
            coverage >= gate.minimum_causal_coverage
            and missing_fraction <= gate.maximum_missing_fraction
        )
        windows.append(
            {
                "window_days": window_days,
                "required_rows": total,
                "observed_rows": observed,
                "modeled_premium_rows": observed,
                "missing_rows": total - observed,
                "causal_coverage": coverage,
                "missing_fraction": missing_fraction,
                "passed": passed,
                "rows": rows,
            }
        )
    result = {
        "gate_defined_before_return_inspection": True,
        "minimum_causal_coverage": gate.minimum_causal_coverage,
        "maximum_missing_fraction": gate.maximum_missing_fraction,
        "maximum_spot_age_hours": gate.maximum_spot_age_hours,
        "maximum_iv_age_hours": gate.maximum_iv_age_hours,
        "passed": all(window["passed"] for window in windows),
        "windows": windows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
