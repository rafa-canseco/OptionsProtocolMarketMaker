from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from src.backtest.config import load_settings
from src.backtest.csp_fund_policy import (
    FundRiskSettings,
    build_candidates,
    build_policy,
    candidate_from_summary,
    run_physical_csp_window,
    selection_sort_key,
    summarize_candidate,
    window_end_times,
    write_checksums,
    write_report,
    write_rows,
)
from src.backtest.data import MarketSeries


def _load(root: Path):
    project = root / "backtests" / "b1n_356"
    config = json.loads((project / "config.json").read_text())
    source_settings = root / config["source"]["settings"]
    market_path = root / config["source"]["market"]
    digest = hashlib.sha256(market_path.read_bytes()).hexdigest()
    if digest != config["source"]["market_sha256"]:
        raise RuntimeError(
            "B1N-356 source market digest mismatch: "
            f"expected={config['source']['market_sha256']} actual={digest}"
        )
    return (
        project,
        config,
        load_settings(source_settings),
        MarketSeries(market_path),
        digest,
    )


def _risk(config):
    fund = config["fund_policy"]
    return FundRiskSettings(
        maximum_weth_nav_fraction_for_new_entry=float(
            fund["maximum_weth_nav_fraction_for_new_entry"]
        ),
        minimum_deployable_collateral_usdc=float(
            fund["minimum_deployable_collateral_usdc"]
        ),
    )


def _strike_variant_id(summary):
    candidate = summary["candidate"]
    return f"{candidate['strike_rule']}:{candidate['strike_parameter']:.6g}"


def _run_candidate(
    *,
    series,
    settings,
    config,
    candidate,
    start,
    end,
    step_by_window,
):
    rows = []
    for window_days in config["decision_gates"]["window_days"]:
        for window_end in window_end_times(
            start=start,
            end=end,
            window_days=int(window_days),
            step_days=int(step_by_window[str(window_days)]),
        ):
            rows.append(
                run_physical_csp_window(
                    series=series,
                    settings=settings,
                    window_days=int(window_days),
                    end=window_end,
                    candidate=candidate,
                    fund_risk=_risk(config),
                )
            )
    return rows


def run(root: Path) -> None:
    project, config, settings, series, digest = _load(root)
    split = config["splits"]
    development_start = datetime.fromisoformat(split["development_start"])
    development_end = datetime.fromisoformat(split["development_end"])
    validation_start = datetime.fromisoformat(split["validation_start"])
    validation_end = datetime.fromisoformat(split["validation_end"])
    base_cost = config["candidate_family"]["base_cost_scenario"]
    candidates = build_candidates(
        config=config,
        settings=settings,
        cost_name=base_cost,
    )

    development_rows = []
    candidate_summaries = []
    for candidate in candidates:
        rows = _run_candidate(
            series=series,
            settings=settings,
            config=config,
            candidate=candidate,
            start=development_start,
            end=development_end,
            step_by_window=split["development_step_by_window_days"],
        )
        development_rows.extend(rows)
        candidate_summaries.append(
            summarize_candidate(
                rows=rows,
                decision_gates=config["decision_gates"],
            )
        )
    ranked = sorted(candidate_summaries, key=selection_sort_key)
    selected_development = ranked[0]
    strike_variants = sorted(
        {
            (
                summary["candidate"]["strike_rule"],
                float(summary["candidate"]["strike_parameter"]),
            )
            for summary in candidate_summaries
        }
    )
    selected_development_by_strike_variant = {
        f"{strike_rule}:{strike_parameter:.6g}": sorted(
            [
                summary
                for summary in candidate_summaries
                if summary["candidate"]["strike_rule"] == strike_rule
                and float(summary["candidate"]["strike_parameter"]) == strike_parameter
            ],
            key=selection_sort_key,
        )[0]
        for strike_rule, strike_parameter in strike_variants
    }

    validation_step = {
        str(window): int(split["validation_step_days"])
        for window in config["decision_gates"]["window_days"]
    }
    validation_comparison = {}
    validation_rows = []
    for (
        strike_variant,
        development_winner,
    ) in selected_development_by_strike_variant.items():
        validation_comparison[strike_variant] = {
            "selected_development": development_winner
        }
        for key, cost_name in (
            ("validation_base", base_cost),
            (
                "validation_stressed",
                config["candidate_family"]["stress_cost_scenario"],
            ),
        ):
            candidate = candidate_from_summary(
                summary=development_winner,
                settings=settings,
                cost_name=cost_name,
            )
            rows = _run_candidate(
                series=series,
                settings=settings,
                config=config,
                candidate=candidate,
                start=validation_start,
                end=validation_end,
                step_by_window=validation_step,
            )
            validation_rows.extend(rows)
            validation_comparison[strike_variant][key] = summarize_candidate(
                rows=rows,
                decision_gates=config["decision_gates"],
            )
    selected_validation = validation_comparison[
        _strike_variant_id(selected_development)
    ]

    summary = {
        "schema_version": 1,
        "issue": config["issue"],
        "source_market_sha256": digest,
        "candidate_count": len(candidates),
        "development_split": {
            "start": development_start.isoformat(),
            "end": development_end.isoformat(),
        },
        "validation_split": {
            "start": validation_start.isoformat(),
            "end": validation_end.isoformat(),
        },
        "selected_development": selected_development,
        "selected_development_by_strike_variant": (
            selected_development_by_strike_variant
        ),
        "development_ranking": ranked,
        "validation_comparison": validation_comparison,
        "validation_base": selected_validation["validation_base"],
        "validation_stressed": selected_validation["validation_stressed"],
    }
    policy = build_policy(
        config=config,
        development=selected_development,
        validation_base=selected_validation["validation_base"],
        validation_stressed=selected_validation["validation_stressed"],
        source_digest=digest,
    )

    output = project / "results"
    output.mkdir(parents=True, exist_ok=True)
    write_rows(output / "development_results.jsonl", development_rows)
    write_rows(output / "validation_results.jsonl", validation_rows)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    write_report(
        path=output / "REPORT.md",
        summary=summary,
        policy=policy,
    )
    policy_path = root / "policies" / "csp_fund_policy.v2.json"
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    write_checksums(
        output,
        (
            "development_results.jsonl",
            "development_results.csv",
            "validation_results.jsonl",
            "validation_results.csv",
            "summary.json",
            "REPORT.md",
        ),
    )
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "selected_candidate": selected_development["candidate_id"],
                "decision": policy["decision"],
                "results": str(output),
                "policy": str(policy_path),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run B1N-356 CSP Fund policy v2")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.root)


if __name__ == "__main__":
    main()
