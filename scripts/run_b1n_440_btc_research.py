"""Rebuild the B1N-440 BTC research artifacts without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.backtest.btc_policy_research import (
    analyze_bita_holdings,
    build_btc_parameter_matrix,
    validate_base_sepolia_feed_observation,
    validate_binary_lbtc_identity,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package",
        type=Path,
        default=Path("backtests/b1n_440"),
    )
    parser.add_argument(
        "--published-rows",
        type=Path,
        default=Path("backtests/b1n_345/production_results/results.jsonl.gz"),
    )
    parser.add_argument(
        "--published-config",
        type=Path,
        default=Path("backtests/b1n_345/config.json"),
    )
    args = parser.parse_args()

    fixtures = args.package / "fixtures"
    results = args.package / "results"
    holdings_path = fixtures / "BITA_holdings_2026-08-06.csv"
    source_manifest_path = fixtures / "source_manifest.json"
    feed_observation_path = fixtures / "base_sepolia_btc_usd_feed_observation.json"
    feed_directory_path = fixtures / "chainlink_base_sepolia_feeds.json"
    lbtc_observation_path = fixtures / "binary_lbtc_identity_observation.json"
    lbtc_contract_path = fixtures / "binary_lbtc_verified_contract.json"
    policy_path = Path("policies/btc_vault_policy.v1.base-sepolia.json")
    holdings_output = results / "holdings_analysis.json"
    matrix_output = results / "parameter_matrix.json"
    lbtc_output = results / "test_asset_identity_validation.json"

    validate_base_sepolia_feed_observation(
        feed_observation_path,
        feed_directory_path,
        policy_path,
        source_manifest_path,
    )
    _write_json(
        lbtc_output,
        validate_binary_lbtc_identity(
            lbtc_observation_path,
            lbtc_contract_path,
            policy_path,
            source_manifest_path,
        ),
    )
    _write_json(holdings_output, analyze_bita_holdings(holdings_path))
    _write_json(
        matrix_output,
        build_btc_parameter_matrix(
            args.published_rows, args.published_config, source_manifest_path
        ),
    )

    hashed_paths = [
        fixtures / "BITA_holdings_2026-08-06.csv",
        fixtures / "base_sepolia_btc_usd_feed_observation.json",
        fixtures / "chainlink_base_sepolia_feeds.json",
        fixtures / "binary_lbtc_identity_observation.json",
        fixtures / "binary_lbtc_verified_contract.json",
        fixtures / "source_manifest.json",
        holdings_output,
        matrix_output,
        lbtc_output,
    ]
    checksum_path = results / "checksums.sha256"
    checksum_path.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(args.package)}\n"
            for path in hashed_paths
        )
    )


if __name__ == "__main__":
    main()
