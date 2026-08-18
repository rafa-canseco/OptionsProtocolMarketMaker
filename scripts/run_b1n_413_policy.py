"""Rebuild the B1N-413 current-fee evidence overlay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.backtest.meta_wheel_policy import (
    build_fee_adjusted_summary,
    load_published_wheel_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("backtests/b1n_345/production_results/results.jsonl.gz"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backtests/b1n_413/results/fee_adjusted_summary.json"),
    )
    args = parser.parse_args()
    summary = build_fee_adjusted_summary(load_published_wheel_rows(args.source))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
