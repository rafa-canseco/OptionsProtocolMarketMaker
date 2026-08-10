"""Build the immutable B1N-450 ETH CSP ladder research outputs."""

from pathlib import Path

from src.backtest.ladder_comparison import build_rows, write_outputs


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config, rows = build_rows(root)
    output = write_outputs(root, config, rows)
    print(f"wrote {len(rows)} normalized rows to {output}")


if __name__ == "__main__":
    main()
