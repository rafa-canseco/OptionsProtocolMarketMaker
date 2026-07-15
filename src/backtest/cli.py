from __future__ import annotations

import argparse
import itertools
import json
from datetime import datetime
from pathlib import Path

from src.backtest.config import load_settings
from src.backtest.data import (
    MarketSeries,
    extract_market_snapshot,
    last_completed_deribit_expiry,
)
from src.backtest.engine import hold_benchmarks, run_strategy
from src.backtest.models import StrategyConfig
from src.backtest.probe import run_coverage_probe
from src.backtest.reporting import build_summary, write_markdown_report, write_results


def _paths(root: Path) -> tuple[Path, Path, Path]:
    project = root / "backtests" / "b1n_345"
    return project / "config.json", project / "data", project / "results"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="B1N-345 Binary wheel backtest")
    parser.add_argument("command", choices=("extract", "probe", "run", "all"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--cutoff", help="Fixed ISO-8601 cutoff for extraction")
    args = parser.parse_args()

    if args.command in ("extract", "all"):
        extract(args.root, args.cutoff)
    if args.command in ("probe", "all") and not probe(args.root):
        raise SystemExit("Coverage gate failed; stopping before returns")
    if args.command in ("run", "all"):
        run(args.root)


if __name__ == "__main__":
    main()
