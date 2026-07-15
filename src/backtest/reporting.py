from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json.dumps(value, sort_keys=True)
        if isinstance(value, (dict, list))
        else value
        for key, value in row.items()
    }


def write_results(rows: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "results.jsonl"
    with jsonl_path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    fieldnames = sorted({key for row in rows for key in row})
    with (output_dir / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_flatten(row) for row in rows)


def _pareto_flags(rows: list[dict[str, Any]]) -> set[int]:
    frontier: set[int] = set()
    for index, candidate in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            better_or_equal = (
                other["absolute_return"] >= candidate["absolute_return"]
                and other["maximum_drawdown"] >= candidate["maximum_drawdown"]
                and other["estimated_costs_usdc"] <= candidate["estimated_costs_usdc"]
            )
            strictly_better = (
                other["absolute_return"] > candidate["absolute_return"]
                or other["maximum_drawdown"] > candidate["maximum_drawdown"]
                or other["estimated_costs_usdc"] < candidate["estimated_costs_usdc"]
            )
            if better_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.add(index)
    return frontier


def build_summary(rows: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    wheels_by_window: dict[int, list[dict[str, Any]]] = defaultdict(list)
    benchmarks_by_window: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["strategy"] == "wheel":
            wheels_by_window[int(row["window_days"])].append(row)
        elif row["strategy"].startswith("hold_"):
            benchmarks_by_window[int(row["window_days"])].append(row)

    windows: dict[str, Any] = {}
    for window_days, wheel_rows in sorted(wheels_by_window.items()):
        ranked = sorted(
            wheel_rows,
            key=lambda row: (
                row["absolute_return"],
                row["maximum_drawdown"],
                -row["estimated_costs_usdc"],
            ),
            reverse=True,
        )
        frontier = _pareto_flags(wheel_rows)
        pareto_rows = [wheel_rows[index] for index in sorted(frontier)]
        windows[str(window_days)] = {
            "scenario_count": len(wheel_rows),
            "top_20_by_return": ranked[:20],
            "pareto_frontier": pareto_rows,
            "benchmarks": benchmarks_by_window[window_days],
        }

    sensitivity: dict[str, Any] = {}
    for field in ("target_delta", "call_margin_usd"):
        groups: dict[tuple[int, Any], list[float]] = defaultdict(list)
        for row in rows:
            if row["strategy"] == "wheel":
                groups[(int(row["window_days"]), row[field])].append(
                    float(row["absolute_return"])
                )
        sensitivity[field] = [
            {
                "window_days": key[0],
                field: key[1],
                "mean_absolute_return": sum(values) / len(values),
                "scenario_count": len(values),
            }
            for key, values in sorted(groups.items())
        ]

    summary = {
        "warning": (
            "Research output only. B1N-345 does not authorize allocator activation. "
            "All Binary option premiums are counterfactual modeled quotes."
        ),
        "windows": windows,
        "sensitivity": sensitivity,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def write_markdown_report(
    rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    output_dir: Path,
) -> None:
    lines = [
        "# B1N-345 results",
        "",
        "> Research output only. This report does not recommend or authorize allocator activation.",
        "",
        "## Data and interpretation",
        "",
        "Deribit ETH/USD spot and ETH DVOL are observed historical inputs. Every option",
        "premium is a counterfactual Binary quote produced by the production Market Maker",
        "pricer; therefore premium-derived PnL is labeled modeled, never observed Binary PnL.",
        "",
        "The canonical covered-call floor is strict per assignment lot:",
        "`call strike > gross lot assignment basis + X`. Weighted-basis modes can only",
        "tighten that floor.",
        "",
        "## Coverage gate",
        "",
        "| Window | Required | Observed causal | Modeled premium | Missing | Coverage |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for window in coverage["windows"]:
        lines.append(
            f"| {window['window_days']}d | {window['required_rows']} | "
            f"{window['observed_rows']} | {window['modeled_premium_rows']} | "
            f"{window['missing_rows']} | {window['causal_coverage']:.1%} |"
        )

    lines.extend(
        [
            "",
            "## Results",
            "",
            "The table uses the approved **base** execution scenario and canonical lot-level",
            "protection. It shows the highest-return configuration in that constrained set,",
            "not a recommendation.",
            "",
            "| Window | Return | Max DD | Net premium | Buy-low/sell-high | Unrealized ETH | Delta | Util. | Min premium | X | Assign. | Cycles | ETH idle |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    base_bests: dict[int, dict[str, Any]] = {}
    for window_days in (30, 90, 180):
        candidates = [
            row
            for row in rows
            if row["strategy"] == "wheel"
            and row["window_days"] == window_days
            and row["costs"] == "base"
            and row["protection_mode"] == "lot_gross"
        ]
        best = max(candidates, key=lambda row: row["absolute_return"])
        base_bests[window_days] = best
        lines.append(
            f"| {window_days}d | {best['absolute_return']:.2%} | "
            f"{best['maximum_drawdown']:.2%} | ${best['premium_net_usdc']:,.0f} | "
            f"${best['realized_low_high_pnl_usdc']:,.0f} | "
            f"${best['unrealized_eth_pnl_usdc']:,.0f} | {best['target_delta']:.2f} | "
            f"{best['utilization']:.0%} | {best['minimum_premium_bps']} bps | "
            f"${best['call_margin_usd']:,.0f} | {best['assignments']} | "
            f"{best['complete_cycles']} | {best['eth_idle_share']:.1%} |"
        )

    lines.extend(
        [
            "",
            "### Benchmarks",
            "",
            "| Window | Hold USDC | Hold ETH | 50/50 no rebalance |",
            "|---:|---:|---:|---:|",
        ]
    )
    for window_days in (30, 90, 180):
        benchmarks = {
            row["strategy"]: row["absolute_return"]
            for row in rows
            if row["window_days"] == window_days and row["strategy"].startswith("hold_")
        }
        lines.append(
            f"| {window_days}d | {benchmarks['hold_usdc']:.2%} | "
            f"{benchmarks['hold_eth']:.2%} | "
            f"{benchmarks['hold_50_50_no_rebalance']:.2%} |"
        )

    lines.extend(
        [
            "",
            "## Material tradeoffs",
            "",
            *[
                f"- The leading {window_days}-day base result completed "
                f"{base_bests[window_days]['complete_cycles']} cycle(s), used "
                f"X=${base_bests[window_days]['call_margin_usd']:,.0f}, and had "
                f"{base_bests[window_days]['eth_idle_share']:.1%} ETH idle exposure."
                for window_days in (30, 90, 180)
            ],
            "- X=0 selects the first $5 strike strictly above each lot's gross basis; larger",
            "  X values intentionally trade less call premium/frequency for a higher sale price.",
            "- Low/base/stressed sensitivities vary Binary's embedded MM spread and",
            "  operational delay only; sponsored gas and platform fees are not deducted",
            "  a second time.",
            "- All positive-premium wheel rows have a modeled-premium fraction of 100%; there",
            "  are no historical Binary fills in these windows.",
            "- `lot_floor_breach_opportunities` quantifies occasions where an average basis",
            "  would have permitted a call below an individual lot. The engine still enforced",
            "  the lot's gross floor, so realized buy-low/sell-high PnL is never negative.",
            "",
            "Full scenario rows, Pareto frontiers, sensitivities, and CSP-only comparisons are",
            "available in `results.jsonl`, `results.csv`, and `summary.json`.",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(lines))
