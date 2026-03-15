#!/usr/bin/env python3
"""Convert trade_history.jsonl to CSV.

Usage: uv run python scripts/history_to_csv.py [input.jsonl] [output.csv]
"""

import csv
import json
import sys


def main() -> None:
    infile = sys.argv[1] if len(sys.argv) > 1 else "data/trade_history.jsonl"
    outfile = sys.argv[2] if len(sys.argv) > 2 else "data/trade_history.csv"

    events: list[dict] = []
    with open(infile) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    if not events:
        print(f"No events in {infile}")
        return

    all_keys: list[str] = []
    seen: set[str] = set()
    for ev in events:
        for k in ev:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    with open(outfile, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(events)

    print(f"Wrote {len(events)} events to {outfile}")


if __name__ == "__main__":
    main()
