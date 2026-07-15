from __future__ import annotations

import argparse
import gzip
import itertools
import json
from datetime import datetime
from pathlib import Path

from src.backtest.config import BacktestSettings, load_settings
from src.backtest.data import (
    MarketSeries,
    extract_asset_market_snapshot,
    extract_market_snapshot,
    last_completed_deribit_expiry,
)
from src.backtest.engine import hold_benchmarks, run_strategy
from src.backtest.models import StrategyConfig
from src.backtest.probe import run_coverage_probe
from src.backtest.production import (
    build_production_summary,
    reclassify_rows,
    run_multiyear_probe,
    run_rolling_validation,
    write_production_outputs,
)
from src.backtest.reporting import build_summary, write_markdown_report, write_results


def _paths(root: Path) -> tuple[Path, Path, Path]:
    project = root / "backtests" / "b1n_345"
    return project / "config.json", project / "data", project / "results"


def _production_paths(root: Path) -> tuple[Path, Path, Path]:
    project = root / "backtests" / "b1n_345"
    return (
        project / "config.json",
        project / "production_data",
        project / "production_results",
    )


def extract(root: Path, cutoff: str | None) -> None:
    _, data_dir, _ = _paths(root)
    fixed_cutoff = (
        datetime.fromisoformat(cutoff) if cutoff else last_completed_deribit_expiry()
    )
    path = extract_market_snapshot(data_dir, fixed_cutoff)
    print(path)


def probe(root: Path) -> bool:
    config_path, data_dir, results_dir = _paths(root)
    settings = load_settings(config_path)
    series = MarketSeries(data_dir / "market.json")
    result = run_coverage_probe(series, settings, results_dir / "coverage_probe.json")
    print(
        json.dumps({"passed": result["passed"], "windows": result["windows"]}, indent=2)
    )
    return bool(result["passed"])


def run(root: Path) -> None:
    config_path, data_dir, results_dir = _paths(root)
    settings = load_settings(config_path)
    series = MarketSeries(data_dir / "market.json")
    coverage_path = results_dir / "coverage_probe.json"
    if not coverage_path.exists():
        raise RuntimeError("Run the coverage probe before the backtest")
    coverage = json.loads(coverage_path.read_text())
    if not coverage.get("passed"):
        raise RuntimeError("Coverage gate failed; full backtest is prohibited")

    rows = []
    for window_days in settings.window_days:
        rows.extend(hold_benchmarks(series, settings, window_days))
        csp_seen: set[tuple] = set()
        combinations = itertools.product(
            settings.target_deltas,
            settings.utilizations,
            settings.minimum_premium_bps,
            settings.call_margins_usd,
            settings.protection_modes,
            settings.cost_scenarios,
        )
        for delta, utilization, minimum, margin, protection, costs in combinations:
            config = StrategyConfig(
                target_delta=delta,
                utilization=utilization,
                minimum_premium_bps=minimum,
                call_margin_usd=margin,
                protection_mode=protection,
                costs=costs,
            )
            rows.append(
                run_strategy(
                    series=series,
                    settings=settings,
                    window_days=window_days,
                    config=config,
                )
            )
            csp_key = (delta, utilization, minimum, costs.name)
            if csp_key not in csp_seen:
                csp_seen.add(csp_key)
                csp_config = StrategyConfig(
                    target_delta=delta,
                    utilization=utilization,
                    minimum_premium_bps=minimum,
                    call_margin_usd=0.0,
                    protection_mode="lot_gross",
                    costs=costs,
                )
                rows.append(
                    run_strategy(
                        series=series,
                        settings=settings,
                        window_days=window_days,
                        config=csp_config,
                        csp_only=True,
                    )
                )
    write_results(rows, results_dir)
    build_summary(rows, results_dir)
    write_markdown_report(rows, coverage, results_dir)
    print(json.dumps({"rows": len(rows), "results": str(results_dir)}, indent=2))


def extract_production(root: Path, cutoff: str | None) -> None:
    config_path, data_dir, _ = _production_paths(root)
    settings = load_settings(config_path)
    validation = settings.production_validation
    if validation is None:
        raise RuntimeError("production_validation is missing from config")
    fixed_cutoff = (
        datetime.fromisoformat(cutoff) if cutoff else last_completed_deribit_expiry()
    )
    for asset in settings.assets:
        path = extract_asset_market_snapshot(
            data_dir / asset.symbol,
            fixed_cutoff,
            symbol=asset.symbol,
            deribit_currency=asset.deribit_currency,
            deribit_index_name=asset.deribit_index_name,
            deribit_perpetual=asset.deribit_perpetual,
            lookback_days=validation.lookback_days,
        )
        print(path)


def _production_series(
    root: Path,
) -> tuple[BacktestSettings, dict[str, MarketSeries], Path]:
    config_path, data_dir, results_dir = _production_paths(root)
    settings = load_settings(config_path)
    series = {
        asset.symbol: MarketSeries(data_dir / asset.symbol / "market.json")
        for asset in settings.assets
    }
    return settings, series, results_dir


def probe_production(root: Path) -> bool:
    settings, series, results_dir = _production_series(root)
    result = run_multiyear_probe(series, settings, results_dir / "coverage_probe.json")
    print(json.dumps(result, indent=2))
    return bool(result["passed"])


def run_production(root: Path) -> None:
    settings, series, results_dir = _production_series(root)
    coverage_path = results_dir / "coverage_probe.json"
    if not coverage_path.exists():
        raise RuntimeError("Run the production coverage probe before returns")
    coverage = json.loads(coverage_path.read_text())
    if not coverage.get("passed"):
        raise RuntimeError("Production coverage gate failed; returns are prohibited")
    rows = run_rolling_validation(series, settings)
    summary = build_production_summary(rows, settings)
    write_production_outputs(rows, summary, coverage, results_dir)
    print(json.dumps({"rows": len(rows), "results": str(results_dir)}, indent=2))


def summarize_production(root: Path) -> None:
    settings, series, results_dir = _production_series(root)
    coverage = json.loads((results_dir / "coverage_probe.json").read_text())
    compressed_path = results_dir / "results.jsonl.gz"
    if compressed_path.exists():
        with gzip.open(compressed_path, "rt") as handle:
            rows = [json.loads(line) for line in handle if line]
    else:
        with (results_dir / "results.jsonl").open() as handle:
            rows = [json.loads(line) for line in handle if line]
    rows = reclassify_rows(rows, series, settings)
    summary = build_production_summary(rows, settings)
    write_production_outputs(rows, summary, coverage, results_dir)
    print(json.dumps({"rows": len(rows), "results": str(results_dir)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="B1N-345 Binary wheel backtest")
    parser.add_argument(
        "command",
        choices=(
            "extract",
            "probe",
            "run",
            "all",
            "extract-production",
            "probe-production",
            "run-production",
            "summarize-production",
            "all-production",
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--cutoff", help="Fixed ISO-8601 cutoff for extraction")
    args = parser.parse_args()

    if args.command in ("extract", "all"):
        extract(args.root, args.cutoff)
    if args.command in ("probe", "all") and not probe(args.root):
        raise SystemExit("Coverage gate failed; stopping before returns")
    if args.command in ("run", "all"):
        run(args.root)
    if args.command in ("extract-production", "all-production"):
        extract_production(args.root, args.cutoff)
    if args.command in ("probe-production", "all-production") and not probe_production(
        args.root
    ):
        raise SystemExit("Production coverage gate failed; stopping before returns")
    if args.command in ("run-production", "all-production"):
        run_production(args.root)
    if args.command == "summarize-production":
        summarize_production(args.root)


if __name__ == "__main__":
    main()
