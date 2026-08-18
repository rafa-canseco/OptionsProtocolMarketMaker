from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from src.backtest.config import load_settings
from src.backtest.covered_call_policy import (
    build_candidates,
    build_policy,
    candidate_from_summary,
    run_covered_call_window,
    selection_sort_key,
    summarize_candidate,
    window_starts,
    write_checksums,
    write_report,
    write_rows,
)
from src.backtest.data import MarketSeries


def _rows(
    *,
    series,
    settings,
    candidate,
    start,
    end,
    window_days,
    step_by_window,
    initial_weth,
):
    return [
        run_covered_call_window(
            series=series,
            settings=settings,
            candidate=candidate,
            start=window_start,
            window_days=days,
            initial_weth=initial_weth,
        )
        for days in window_days
        for window_start in window_starts(
            start=start,
            end=end,
            window_days=days,
            step_days=step_by_window[str(days)],
        )
    ]


def run(root: Path) -> None:
    project = root / "backtests" / "b1n_358"
    config = json.loads((project / "config.json").read_text())
    settings = load_settings(root / config["source"]["settings"])
    market_path = root / config["source"]["market"]
    digest = hashlib.sha256(market_path.read_bytes()).hexdigest()
    if digest != config["source"]["market_sha256"]:
        raise RuntimeError("B1N-358 source market digest mismatch")
    series = MarketSeries(market_path)
    split = config["splits"]
    development_start = datetime.fromisoformat(split["development_start"])
    development_end = datetime.fromisoformat(split["development_end"])
    validation_start = datetime.fromisoformat(split["validation_start"])
    validation_end = datetime.fromisoformat(split["validation_end"])
    window_days = config["decision_gates"]["window_days"]
    base_cost = config["candidate_family"]["base_cost_scenario"]

    development_rows = []
    summaries = []
    for candidate in build_candidates(config, settings, base_cost):
        rows = _rows(
            series=series,
            settings=settings,
            candidate=candidate,
            start=development_start,
            end=development_end,
            window_days=window_days,
            step_by_window=split["development_step_by_window_days"],
            initial_weth=1.0,
        )
        development_rows.extend(rows)
        summaries.append(summarize_candidate(rows, config["decision_gates"]))
    ranking = sorted(summaries, key=selection_sort_key)
    selected = ranking[0]

    validation_rows = []
    validation = {}
    for label, cost_name in (
        ("validation_base", base_cost),
        ("validation_stressed", config["candidate_family"]["stress_cost_scenario"]),
    ):
        candidate = candidate_from_summary(selected, settings, cost_name, config)
        rows = _rows(
            series=series,
            settings=settings,
            candidate=candidate,
            start=validation_start,
            end=validation_end,
            window_days=window_days,
            step_by_window={
                str(days): int(split["validation_step_days"]) for days in window_days
            },
            initial_weth=1.0,
        )
        validation_rows.extend(rows)
        validation[label] = summarize_candidate(rows, config["decision_gates"])

    summary = {
        "schema_version": 2,
        "issue": "B1N-358",
        "source_market_sha256": digest,
        "candidate_count": len(ranking),
        "selected_development": selected,
        "development_ranking": ranking,
        **validation,
    }
    policy = build_policy(
        config=config,
        development=selected,
        validation_base=validation["validation_base"],
        validation_stressed=validation["validation_stressed"],
        source_digest=digest,
    )
    output = project / "results"
    output.mkdir(parents=True, exist_ok=True)
    write_rows(output / "development_results.jsonl", development_rows)
    write_rows(output / "validation_results.jsonl", validation_rows)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    write_report(output / "REPORT.md", summary, policy)
    policy_path = root / "policies" / "covered_call_fund_policy.v2.base-sepolia.json"
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    policy_path.with_suffix(".sha256").write_text(
        f"{hashlib.sha256(policy_path.read_bytes()).hexdigest()}  {policy_path.name}\n"
    )
    write_checksums(
        output,
        (
            "development_results.jsonl",
            "validation_results.jsonl",
            "summary.json",
            "REPORT.md",
        ),
    )
    print(
        json.dumps(
            {
                "candidate_count": len(ranking),
                "selected_candidate": selected["candidate_id"],
                "decision": policy["decision"],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run B1N-358 covered-call policy")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.root)


if __name__ == "__main__":
    main()
