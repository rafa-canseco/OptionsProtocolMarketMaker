from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.backtest.btc_policy_research import (
    analyze_bita_holdings,
    build_btc_parameter_matrix,
    validate_base_sepolia_feed_observation,
    validate_binary_lbtc_identity,
)

PACKAGE = Path("backtests/b1n_440")
HOLDINGS = PACKAGE / "fixtures/BITA_holdings_2026-08-06.csv"
ROWS = Path("backtests/b1n_345/production_results/results.jsonl.gz")
CONFIG = Path("backtests/b1n_345/config.json")
POLICY = Path("policies/btc_vault_policy.v1.base-sepolia.json")
SOURCE_MANIFEST = PACKAGE / "fixtures/source_manifest.json"
FEED_OBSERVATION = PACKAGE / "fixtures/base_sepolia_btc_usd_feed_observation.json"
FEED_DIRECTORY = PACKAGE / "fixtures/chainlink_base_sepolia_feeds.json"
LBTC_OBSERVATION = PACKAGE / "fixtures/binary_lbtc_identity_observation.json"
LBTC_CONTRACT = PACKAGE / "fixtures/binary_lbtc_verified_contract.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_published_package(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "b1n_345"
    rows = source_root / "production_results/results.jsonl.gz"
    config = source_root / "config.json"
    market = source_root / "production_data/BTC/market.json"
    rows.parent.mkdir(parents=True)
    market.parent.mkdir(parents=True)
    shutil.copy2(ROWS, rows)
    shutil.copy2(CONFIG, config)
    shutil.copy2(Path("backtests/b1n_345/production_data/BTC/market.json"), market)

    package = tmp_path / "b1n_440"
    manifest_path = package / "fixtures/source_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = json.loads(SOURCE_MANIFEST.read_text())
    source = next(
        item for item in manifest["sources"] if item["id"] == "b1n_345_btc_backtest"
    )
    source["rows_sha256"] = _sha256(rows)
    source["config_sha256"] = _sha256(config)
    source["btc_market_sha256"] = _sha256(market)
    manifest_path.write_text(json.dumps(manifest))
    return rows, config, manifest_path


def test_bita_holdings_reproduce_official_snapshot() -> None:
    analysis = analyze_bita_holdings(HOLDINGS)
    derived = analysis["derived"]

    assert derived["weekly_bucket_count"] == 4
    assert derived["equal_contract_buckets"] is True
    assert [row["contracts"] for row in analysis["option_rows"]] == [1236.0] * 4
    assert sorted(derived["strikes_usd_in_source_order"]) == [35.5, 36.5, 36.5, 37.5]
    assert derived["calls_fully_covered"] is True
    assert derived["gross_call_overwrite_nav_ratio"] == pytest.approx(
        0.30133057416302333
    )
    assert derived["observed_weighted_delta"] == pytest.approx(0.5045764566432618)
    assert (
        derived["observed_weighted_delta_classification"]
        == "derived_observation_not_target"
    )
    assert all(row["exact_expiry"] is None for row in analysis["option_rows"])
    assert all(row["dte"] is None for row in analysis["option_rows"])


def test_bita_holdings_fail_closed_on_wrong_cutoff(tmp_path: Path) -> None:
    altered = tmp_path / "holdings.csv"
    altered.write_text(HOLDINGS.read_text().replace("Aug 06, 2026", "Aug 05, 2026"))

    with pytest.raises(ValueError, match="unexpected BITA holdings cutoff"):
        analyze_bita_holdings(altered)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('"IBIT","AUG26', '"BTC","AUG26', "must be an IBIT call"),
        ('"-1,236.00"', '"1,236.00"', "must be short"),
    ],
)
def test_bita_holdings_reject_malformed_call_semantics(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    altered = tmp_path / "holdings.csv"
    altered.write_text(HOLDINGS.read_text().replace(old, new))

    with pytest.raises(ValueError, match=message):
        analyze_bita_holdings(altered)


def test_btc_strategy_matrix_is_independent_and_no_go() -> None:
    matrix = build_btc_parameter_matrix(ROWS, CONFIG, SOURCE_MANIFEST)

    csp = matrix["strategies"]["standalone_csp"]
    wheel = matrix["strategies"]["wheel"]
    assert len(csp["matrix"]) == 12
    assert len(wheel["matrix"]) == 12
    assert csp["selected_activation_parameters"] is None
    assert wheel["selected_activation_parameters"] is None
    assert all(not row["historical_gate_pass"] for row in csp["matrix"])
    assert all(not row["historical_gate_pass"] for row in wheel["matrix"])
    assert all(row["activation_decision"] == "no_go" for row in csp["matrix"])
    assert all(row["activation_decision"] == "no_go" for row in wheel["matrix"])
    assert matrix["standalone_covered_call"] == {
        "backtest_available": False,
        "selected_activation_parameters": None,
        "decision": "no_go",
        "reason": (
            "B1N-345 has no independent BTC covered-call cohort; Wheel call rows "
            "are contingent on CSP assignment and cannot be relabeled standalone."
        ),
    }


def test_btc_strategy_matrix_rejects_substituted_source(tmp_path: Path) -> None:
    rows, config, manifest = _copy_published_package(tmp_path)
    config.write_text(config.read_text() + "\n")

    with pytest.raises(ValueError, match="config hash mismatch"):
        build_btc_parameter_matrix(rows, config, manifest)


def test_btc_strategy_matrix_rejects_future_row_even_if_rehashed(
    tmp_path: Path,
) -> None:
    rows, config, manifest_path = _copy_published_package(tmp_path)
    rewritten = []
    changed = False
    with gzip.open(rows, "rt") as source:
        for line in source:
            row = json.loads(line)
            if (
                not changed
                and row.get("asset") == "BTC"
                and row.get("strategy") == "wheel"
            ):
                row["window_end"] = "2099-01-01T00:00:00+00:00"
                changed = True
            rewritten.append(json.dumps(row, separators=(",", ":")))
    with gzip.open(rows, "wt") as destination:
        destination.write("\n".join(rewritten) + "\n")
    manifest = json.loads(manifest_path.read_text())
    source = next(
        item for item in manifest["sources"] if item["id"] == "b1n_345_btc_backtest"
    )
    source["rows_sha256"] = _sha256(rows)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="exceeds pinned historical cutoff"):
        build_btc_parameter_matrix(rows, config, manifest_path)


def test_feed_observation_matches_directory_and_policy() -> None:
    result = validate_base_sepolia_feed_observation(
        FEED_OBSERVATION, FEED_DIRECTORY, POLICY, SOURCE_MANIFEST
    )
    assert result == {
        "address": "0x0FB99723Aee6f420beAD13e6bBB79b7E6F034298",
        "chain_id": 84532,
        "block_number": 45180459,
        "round_id": 18446744073709846714,
        "age_seconds": 446,
        "qualified": True,
    }


@pytest.mark.parametrize("mutation", ["zero_address", "stale_round", "bad_answer"])
def test_feed_observation_rejects_contradictory_evidence(
    tmp_path: Path, mutation: str
) -> None:
    observation = json.loads(FEED_OBSERVATION.read_text())
    if mutation == "zero_address":
        observation["feed"]["address"] = "0x" + "0" * 40
    elif mutation == "stale_round":
        observation["feed"]["latest_round_data"]["updated_at"] = "2026-08-07T17:00:00Z"
    else:
        observation["feed"]["latest_round_data"]["answer"] = 0
    altered = tmp_path / "feed.json"
    altered.write_text(json.dumps(observation))

    with pytest.raises(ValueError):
        validate_base_sepolia_feed_observation(
            altered, FEED_DIRECTORY, POLICY, SOURCE_MANIFEST
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("number", 1, "block number does not match source pin"),
        ("hash", "0x" + "1" * 64, "block hash does not match source pin"),
    ],
)
def test_feed_observation_rejects_unpinned_block(
    tmp_path: Path, field: str, value: int | str, message: str
) -> None:
    observation = json.loads(FEED_OBSERVATION.read_text())
    observation["block"][field] = value
    altered = tmp_path / "feed.json"
    altered.write_text(json.dumps(observation))

    with pytest.raises(ValueError, match=message):
        validate_base_sepolia_feed_observation(
            altered, FEED_DIRECTORY, POLICY, SOURCE_MANIFEST
        )


@pytest.mark.parametrize("mutate_manifest", [False, True])
def test_feed_observation_rejects_zero_or_unpinned_proxy_code_hash(
    tmp_path: Path, mutate_manifest: bool
) -> None:
    observation = json.loads(FEED_OBSERVATION.read_text())
    manifest = json.loads(SOURCE_MANIFEST.read_text())
    manifest_path = tmp_path / "source_manifest.json"
    if mutate_manifest:
        rpc_source = next(
            source
            for source in manifest["sources"]
            if source["id"] == "base_sepolia_feed_rpc_observation"
        )
        rpc_source["feed_proxy_code_sha256"] = "0" * 64
    else:
        observation["feed"]["code_sha256"] = "1" * 64
    altered = tmp_path / "feed.json"
    altered.write_text(json.dumps(observation))
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="proxy code hash does not match source pin"):
        validate_base_sepolia_feed_observation(
            altered, FEED_DIRECTORY, POLICY, manifest_path
        )


def test_lbtc_identity_is_functional_go_without_strategy_activation() -> None:
    result = validate_binary_lbtc_identity(
        LBTC_OBSERVATION, LBTC_CONTRACT, POLICY, SOURCE_MANIFEST
    )
    assert result == {
        "address": "0x39fA11EbBE82699Fd9F79C566D7384064571d2b4",
        "chain_id": 84532,
        "block_number": 45216602,
        "name": "Loot BTC",
        "symbol": "LBTC",
        "decimals": 8,
        "runtime_codehash_keccak256": (
            "0x599a6b80cccf2c7082103129c3725529a49d37b569dc0ecc031f0444b0ce0fff"
        ),
        "unrestricted_mint": True,
        "test_asset_functional_go": True,
        "strategy_activation_go": False,
    }


@pytest.mark.parametrize(
    ("target", "mutation", "message"),
    [
        ("observation", "address", "address identity mismatch"),
        ("observation", "decimals", "token metadata mismatch"),
        ("observation", "codehash", "runtime codehash mismatch"),
        ("observation", "mint", "mint classification mismatch"),
        ("contract", "source", "does not establish MockERC20 identity"),
        ("contract", "restricted_source", "does not establish MockERC20 identity"),
        ("policy", "activation", "authority boundary mismatch"),
        ("policy", "oracle", "cannot enable current-stack prerequisites"),
    ],
)
def test_lbtc_identity_rejects_mutated_or_overclaimed_evidence(
    tmp_path: Path, target: str, mutation: str, message: str
) -> None:
    package = tmp_path / "b1n_440"
    fixtures = package / "fixtures"
    fixtures.mkdir(parents=True)
    observation_path = fixtures / LBTC_OBSERVATION.name
    contract_path = fixtures / LBTC_CONTRACT.name
    manifest_path = fixtures / SOURCE_MANIFEST.name
    policy_path = tmp_path / POLICY.name
    observation = json.loads(LBTC_OBSERVATION.read_text())
    contract = json.loads(LBTC_CONTRACT.read_text())
    manifest = json.loads(SOURCE_MANIFEST.read_text())
    policy = json.loads(POLICY.read_text())

    if mutation == "address":
        observation["token"]["address"] = "0x" + "1" * 40
    elif mutation == "decimals":
        observation["token"]["decimals"] = 18
    elif mutation == "codehash":
        observation["token"]["runtime_codehash_keccak256"] = "0x" + "1" * 64
    elif mutation == "mint":
        observation["mint_eth_call"]["success"] = False
    elif mutation == "source":
        contract["source_code"] = contract["source_code"].replace(
            "function mint(address to, uint256 amount) external",
            "function removed() external",
        )
    elif mutation == "restricted_source":
        contract["source_code"] += "\n// onlyOwner\n"
    elif mutation == "activation":
        policy["strategy_activation_go"] = True
    else:
        policy["prerequisites"]["current_v2_oracle_binding"]["decision"] = "go"

    observation_path.write_text(json.dumps(observation))
    contract_path.write_text(json.dumps(contract))
    policy_path.write_text(json.dumps(policy))
    for source in manifest["sources"]:
        if source["id"] == "binary_lbtc_rpc_observation":
            source["sha256"] = _sha256(observation_path)
        elif source["id"] == "binary_lbtc_verified_contract":
            source["sha256"] = _sha256(contract_path)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=message):
        validate_binary_lbtc_identity(
            observation_path, contract_path, policy_path, manifest_path
        )


def test_lbtc_identity_rejects_unpinned_fixture_bytes(tmp_path: Path) -> None:
    package = tmp_path / "b1n_440"
    fixtures = package / "fixtures"
    fixtures.mkdir(parents=True)
    observation_path = fixtures / LBTC_OBSERVATION.name
    contract_path = fixtures / LBTC_CONTRACT.name
    manifest_path = fixtures / SOURCE_MANIFEST.name
    shutil.copy2(LBTC_OBSERVATION, observation_path)
    shutil.copy2(LBTC_CONTRACT, contract_path)
    shutil.copy2(SOURCE_MANIFEST, manifest_path)
    observation_path.write_text(observation_path.read_text() + "\n")

    with pytest.raises(ValueError, match="observation hash mismatch"):
        validate_binary_lbtc_identity(
            observation_path, contract_path, POLICY, manifest_path
        )


def test_policy_fails_closed_while_lbtc_identity_is_functional_go() -> None:
    policy = json.loads(POLICY.read_text())

    assert policy["decision"] == "no_go"
    assert policy["test_asset_functional_go"] is True
    assert policy["strategy_activation_go"] is False
    assert policy["activation_allowed"] is False
    assert policy["runtime_enabled_by_default"] is False
    assert policy["mainnet_authorized"] is False
    assert policy["bita_public_profile"]["target_delta"] is None
    assert policy["bita_public_profile"]["csp_leg"] is None
    assert (
        policy["prerequisites"]["canonical_cbbtc_base_sepolia"][
            "canonical_token_address"
        ]
        is None
    )
    test_asset = policy["prerequisites"]["binary_lbtc_base_sepolia_test_asset"]
    assert test_asset["decision"] == "functional_go"
    assert test_asset["symbol"] == "LBTC"
    assert test_asset["decimals"] == 8
    assert test_asset["unrestricted_mint"] is True
    assert test_asset["testnet_only"] is True
    assert test_asset["production_backed_btc"] is False
    assert test_asset["canonical_cbbtc"] is False
    assert (
        policy["prerequisites"]["executable_liquidity"][
            "testnet_premiums_are_executable_yield"
        ]
        is False
    )
    assert set(policy["strategy_decisions"]) == {
        "standalone_csp",
        "standalone_covered_call",
        "wheel",
    }
    assert all(
        decision["decision"] == "no_go"
        for decision in policy["strategy_decisions"].values()
    )
    assert all(
        decision["selected_activation_parameters"] is None
        for decision in policy["strategy_decisions"].values()
    )


def test_policy_and_official_holdings_hashes_are_pinned() -> None:
    manifest = json.loads((PACKAGE / "fixtures/source_manifest.json").read_text())
    holdings_source = next(
        source
        for source in manifest["sources"]
        if source["id"] == "blackrock_bita_holdings_2026_08_06"
    )
    assert (
        hashlib.sha256(HOLDINGS.read_bytes()).hexdigest() == holdings_source["sha256"]
    )

    policy_checksums = {}
    checksum_lines = (
        Path("policies/btc_vault_policy.v1.base-sepolia.sha256")
        .read_text()
        .splitlines()
    )
    for line in checksum_lines:
        digest, name = line.split()
        policy_checksums[name] = digest
    for name, digest in policy_checksums.items():
        path = Path("policies") / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    artifact_lines = (PACKAGE / "results/checksums.sha256").read_text().splitlines()
    for line in artifact_lines:
        digest, relative_path = line.split()
        artifact = PACKAGE / relative_path
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
