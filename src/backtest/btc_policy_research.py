"""Reproducible B1N-440 BTC holdings and policy research.

This module deliberately separates observed source fields from derived holdings
calculations and from modeled B1N-345 backtest results. It does not provide an
allocator runtime or authorize activation.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest.config import load_settings
from src.backtest.production import build_production_summary

_OPTION_STRIKE = re.compile(r"\bC\s*@\s*([0-9]+(?:\.[0-9]+)?)$")
_CONTRACT_MULTIPLIER = Decimal(100)
_EXPECTED_BTC_USD_FEED = "0x0fb99723aee6f420bead13e6bbb79b7e6f034298"
_EXPECTED_LBTC = "0x39fa11ebbe82699fd9f79c566d7384064571d2b4"
_EXPECTED_LBTC_CODEHASH = (
    "0x599a6b80cccf2c7082103129c3725529a49d37b569dc0ecc031f0444b0ce0fff"
)
_ZERO_ADDRESS = "0x" + "0" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid {field} timestamp") from exc


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", "").strip())


def _json_decimal(value: Decimal) -> float:
    return float(value)


def load_bita_holdings(path: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Load the official iShares CSV while preserving its displayed fields."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    try:
        header_index = next(
            i for i, line in enumerate(lines) if line.startswith("Ticker,")
        )
    except StopIteration as exc:
        raise ValueError("holdings CSV has no Ticker header") from exc

    metadata: dict[str, str] = {}
    for row in csv.reader(lines[:header_index]):
        if len(row) >= 2 and row[0]:
            metadata[row[0]] = row[1]
    holdings = list(csv.DictReader(lines[header_index:]))
    if not holdings:
        raise ValueError("holdings CSV has no holdings")
    return metadata, holdings


def analyze_bita_holdings(path: Path) -> dict[str, Any]:
    """Reproduce BITA coverage, overwrite, and observed weighted delta.

    BlackRock documents that option Notional Value in the holdings table is
    delta-adjusted. Therefore each row's observed delta is derived as absolute
    option notional divided by covered IBIT shares times the official snapshot's
    IBIT unit value. It is an observed snapshot result, never a target.
    """
    metadata, holdings = load_bita_holdings(path)
    if metadata.get("Fund Holdings as of") != "Aug 06, 2026":
        raise ValueError("unexpected BITA holdings cutoff")

    ibit_rows = [
        row
        for row in holdings
        if row["Ticker"] == "IBIT" and row["Asset Class"] == "Alternative"
    ]
    if len(ibit_rows) != 1:
        raise ValueError("expected one optionable IBIT holding")
    ibit = ibit_rows[0]
    ibit_shares = _decimal(ibit["Quantity"])
    ibit_market_value = _decimal(ibit["Market Value"])
    if ibit_shares <= 0:
        raise ValueError("optionable IBIT shares must be positive")
    ibit_unit_value = ibit_market_value / ibit_shares

    nav = sum((_decimal(row["Market Value"]) for row in holdings), Decimal(0))
    if nav <= 0:
        raise ValueError("derived holdings NAV must be positive")

    option_rows: list[dict[str, Any]] = []
    total_call_shares = Decimal(0)
    gross_strike_notional = Decimal(0)
    delta_adjusted_notional = Decimal(0)
    for index, row in enumerate(
        (item for item in holdings if item["Asset Class"] == "Other Derivatives"),
        start=1,
    ):
        match = _OPTION_STRIKE.search(row["Name"])
        if not match:
            raise ValueError(f"unsupported option name: {row['Name']}")
        if row["Ticker"] != "IBIT":
            raise ValueError("BITA derivative must be an IBIT call")
        signed_quantity = _decimal(row["Quantity"])
        signed_notional = _decimal(row["Notional Value"])
        signed_market_value = _decimal(row["Market Value"])
        if signed_quantity >= 0 or signed_notional >= 0 or signed_market_value >= 0:
            raise ValueError(
                "BITA covered-call rows must be short with negative values"
            )
        quantity = -signed_quantity
        strike = Decimal(match.group(1))
        call_shares = quantity * _CONTRACT_MULTIPLIER
        row_delta_notional = -signed_notional
        observed_delta = row_delta_notional / (call_shares * ibit_unit_value)
        if not Decimal(0) < observed_delta <= Decimal(1):
            raise ValueError("observed call delta must be in (0, 1]")
        total_call_shares += call_shares
        gross_strike_notional += call_shares * strike
        delta_adjusted_notional += row_delta_notional
        option_rows.append(
            {
                "bucket_id": f"weekly_bucket_{index}",
                "ticker": row["Ticker"],
                "official_name": row["Name"],
                "contracts": _json_decimal(quantity),
                "contract_multiplier_shares": int(_CONTRACT_MULTIPLIER),
                "covered_shares": _json_decimal(call_shares),
                "strike_usd": _json_decimal(strike),
                "official_delta_adjusted_notional_usd": _json_decimal(
                    row_delta_notional
                ),
                "observed_delta": _json_decimal(observed_delta),
                "delta_classification": "derived_observation_not_target",
                "exact_expiry": None,
                "dte": None,
            }
        )

    if not option_rows:
        raise ValueError("holdings CSV has no option rows")
    weighted_delta = delta_adjusted_notional / (total_call_shares * ibit_unit_value)
    return {
        "schema_version": 1,
        "authority_issue": "B1N-440",
        "source_class": "official_ishares_holdings_observed",
        "cutoff": "2026-08-06",
        "calculation_rules": {
            "nav": "sum displayed Market Value across all holdings",
            "gross_call_overwrite": (
                "sum(abs(contracts) * 100 * strike) / derived holdings NAV"
            ),
            "covered_share_ratio": (
                "sum(abs(contracts) * 100) / optionable IBIT shares"
            ),
            "observed_weighted_delta": (
                "sum(abs(option Notional Value)) / "
                "(total covered IBIT shares * IBIT unit value)"
            ),
            "notional_note": (
                "BlackRock states option Notional Value is delta-adjusted; "
                "the result is not a target delta"
            ),
        },
        "derived": {
            "holdings_nav_usd": _json_decimal(nav),
            "optionable_ibit_shares": _json_decimal(ibit_shares),
            "ibit_unit_value_usd": _json_decimal(ibit_unit_value),
            "total_call_contracts": _json_decimal(
                total_call_shares / _CONTRACT_MULTIPLIER
            ),
            "total_covered_ibit_shares": _json_decimal(total_call_shares),
            "calls_fully_covered": total_call_shares <= ibit_shares,
            "covered_share_ratio": _json_decimal(total_call_shares / ibit_shares),
            "gross_strike_notional_usd": _json_decimal(gross_strike_notional),
            "gross_call_overwrite_nav_ratio": _json_decimal(
                gross_strike_notional / nav
            ),
            "observed_weighted_delta": _json_decimal(weighted_delta),
            "observed_weighted_delta_classification": (
                "derived_observation_not_target"
            ),
            "weekly_bucket_count": len(option_rows),
            "equal_contract_buckets": len({row["contracts"] for row in option_rows})
            == 1,
            "strikes_usd_in_source_order": [row["strike_usd"] for row in option_rows],
        },
        "option_rows": option_rows,
        "unknowns": [
            "The CSV names only AUG26 and does not expose each exact expiry or DTE.",
            "The official holdings snapshot does not define a target delta.",
        ],
    }


def validate_base_sepolia_feed_observation(
    observation_path: Path,
    directory_path: Path,
    policy_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    """Validate the BTC/USD observation against all pinned source claims."""
    observation = json.loads(observation_path.read_text())
    directory = json.loads(directory_path.read_text())
    policy = json.loads(policy_path.read_text())
    source_manifest = json.loads(source_manifest_path.read_text())
    rpc_source = next(
        (
            source
            for source in source_manifest.get("sources", [])
            if source.get("id") == "base_sepolia_feed_rpc_observation"
        ),
        None,
    )
    if not isinstance(rpc_source, dict):
        raise ValueError("pinned Base Sepolia RPC source is missing")
    entry = directory.get("selected_official_entry", {})
    feed = observation.get("feed", {})
    policy_feed = (
        policy.get("prerequisites", {}).get("valuation_feeds", {}).get("btc_usd", {})
    )

    address = str(feed.get("address", "")).lower()
    directory_address = str(entry.get("proxyAddress", "")).lower()
    policy_address = str(policy_feed.get("address", "")).lower()
    if address in {"", _ZERO_ADDRESS} or address != _EXPECTED_BTC_USD_FEED:
        raise ValueError("unexpected or zero BTC/USD feed address")
    if not address == directory_address == policy_address:
        raise ValueError("BTC/USD feed identity mismatch")
    if (
        observation.get("chain_id") != 84532
        or policy.get("scope", {}).get("chain_id") != 84532
    ):
        raise ValueError("BTC/USD observation is not Base Sepolia")
    if entry.get("pair") != ["BTC", "USD"] or entry.get("name") != "BTC / USD":
        raise ValueError("official directory pair is not BTC / USD")
    if (
        feed.get("official_directory_pair") != "BTC / USD"
        or feed.get("description") != "BTC / USD"
    ):
        raise ValueError("observed feed description mismatch")

    heartbeat = entry.get("heartbeat")
    decimals = entry.get("decimals")
    if not isinstance(heartbeat, int) or heartbeat <= 0:
        raise ValueError("invalid official heartbeat")
    if (
        feed.get("official_heartbeat_seconds") != heartbeat
        or policy_feed.get("heartbeat_seconds") != heartbeat
    ):
        raise ValueError("BTC/USD heartbeat mismatch")
    if (
        decimals != 8
        or feed.get("decimals") != decimals
        or policy_feed.get("decimals") != decimals
    ):
        raise ValueError("BTC/USD decimals mismatch")

    block = observation.get("block", {})
    block_number = block.get("number")
    block_hash = str(block.get("hash", "")).lower()
    pinned_block_number = rpc_source.get("as_of_block")
    pinned_block_hash = str(rpc_source.get("as_of_block_hash", "")).lower()
    if (
        not isinstance(block_number, int)
        or block_number <= 0
        or block_number != pinned_block_number
    ):
        raise ValueError("BTC/USD observation block number does not match source pin")
    if (
        not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
        or block_hash == "0x" + "0" * 64
        or block_hash != pinned_block_hash
    ):
        raise ValueError("BTC/USD observation block hash does not match source pin")

    round_data = feed.get("latest_round_data", {})
    block_time = _parse_timestamp(block.get("timestamp"), "block")
    started_at = _parse_timestamp(round_data.get("started_at"), "round started_at")
    updated_at = _parse_timestamp(round_data.get("updated_at"), "round updated_at")
    round_id = round_data.get("round_id")
    answered_in_round = round_data.get("answered_in_round")
    if not isinstance(round_id, int) or round_id <= 0:
        raise ValueError("invalid BTC/USD round id")
    if not isinstance(answered_in_round, int) or answered_in_round < round_id:
        raise ValueError("BTC/USD answeredInRound is stale")
    if not started_at <= updated_at <= block_time:
        raise ValueError("BTC/USD round is not causal at pinned block")
    age = int((block_time - updated_at).total_seconds())
    if age < 0 or age > heartbeat or feed.get("age_at_observation_seconds") != age:
        raise ValueError("BTC/USD observation exceeds official heartbeat")
    if not isinstance(round_data.get("answer"), int) or round_data["answer"] <= 0:
        raise ValueError("BTC/USD answer must be positive")
    code_sha256 = str(feed.get("code_sha256", "")).lower()
    pinned_code_sha256 = str(rpc_source.get("feed_proxy_code_sha256", "")).lower()
    if (
        feed.get("code_bytes", 0) <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", code_sha256)
        or code_sha256 == "0" * 64
        or not re.fullmatch(r"[0-9a-f]{64}", pinned_code_sha256)
        or pinned_code_sha256 == "0" * 64
        or code_sha256 != pinned_code_sha256
    ):
        raise ValueError("BTC/USD feed proxy code hash does not match source pin")

    expected_truths = {
        "positive_answer": True,
        "fresh_within_official_heartbeat": True,
        "btc_usd_feed_qualified_at_observation": True,
        "cbbtc_peg_or_reserve_feed_qualified": False,
    }
    if any(feed.get(key) is not value for key, value in expected_truths.items()):
        raise ValueError("BTC/USD qualification flags contradict source evidence")
    if directory.get("cbbtc_specific_entry_present") is not False:
        raise ValueError("cbBTC feed directory claim changed")
    if not (
        policy_feed.get("official_directory_verified") is True
        and policy_feed.get("live_read_at_pinned_block_verified") is True
        and policy.get("prerequisites", {})
        .get("valuation_feeds", {})
        .get("cbbtc_btc_peg_or_reserve", {})
        .get("official_directory_verified")
        is False
    ):
        raise ValueError("policy feed qualification contradicts pinned evidence")
    return {
        "address": feed["address"],
        "chain_id": observation["chain_id"],
        "block_number": block["number"],
        "round_id": round_id,
        "age_seconds": age,
        "qualified": True,
    }


def validate_binary_lbtc_identity(
    observation_path: Path,
    verified_contract_path: Path,
    policy_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    """Validate Binary's existing LBTC as an unrestricted-mint test asset.

    This proves only identity and functional availability on Base Sepolia. It
    deliberately does not qualify any Oracle, Whitelist, router, option product,
    settlement path, executable quote, or production-backed BTC claim.
    """
    observation = json.loads(observation_path.read_text())
    verified_contract = json.loads(verified_contract_path.read_text())
    policy = json.loads(policy_path.read_text())
    manifest = json.loads(source_manifest_path.read_text())
    rpc_source = _source_entry(manifest, "binary_lbtc_rpc_observation")
    source_entry = _source_entry(manifest, "binary_lbtc_verified_contract")

    package_root = source_manifest_path.parent.parent
    expected_observation = (package_root / rpc_source["local_path"]).resolve()
    expected_contract = (package_root / source_entry["local_path"]).resolve()
    if observation_path.resolve() != expected_observation:
        raise ValueError("LBTC observation path does not match pinned manifest")
    if verified_contract_path.resolve() != expected_contract:
        raise ValueError("LBTC verified contract path does not match pinned manifest")
    if _sha256(observation_path) != rpc_source["sha256"]:
        raise ValueError("LBTC observation hash mismatch")
    if _sha256(verified_contract_path) != source_entry["sha256"]:
        raise ValueError("LBTC verified contract hash mismatch")

    token = observation.get("token", {})
    source_metadata = verified_contract.get("blockscout", {})
    source_classification = verified_contract.get("security_classification", {})
    policy_asset = policy.get("prerequisites", {}).get(
        "binary_lbtc_base_sepolia_test_asset", {}
    )
    addresses = {
        str(token.get("address", "")).lower(),
        str(verified_contract.get("address", "")).lower(),
        str(policy_asset.get("address", "")).lower(),
        str(rpc_source.get("address", "")).lower(),
    }
    if addresses != {_EXPECTED_LBTC}:
        raise ValueError("LBTC address identity mismatch")
    if (
        observation.get("chain_id") != 84532
        or verified_contract.get("chain_id") != 84532
        or policy.get("scope", {}).get("chain_id") != 84532
        or rpc_source.get("chain_id") != 84532
    ):
        raise ValueError("LBTC evidence is not Base Sepolia")

    block = observation.get("block", {})
    block_hash = str(block.get("hash", "")).lower()
    if (
        block.get("number") != rpc_source.get("as_of_block")
        or block_hash != str(rpc_source.get("as_of_block_hash", "")).lower()
        or not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
        or block_hash == "0x" + "0" * 64
    ):
        raise ValueError("LBTC observation block does not match source pin")
    _parse_timestamp(block.get("timestamp"), "LBTC block")

    if (
        token.get("name") != "Loot BTC"
        or token.get("symbol") != "LBTC"
        or token.get("decimals") != 8
        or token.get("total_supply_base_units", 0) <= 0
        or token.get("runtime_code_bytes", 0) <= 0
    ):
        raise ValueError("LBTC token metadata mismatch")
    observed_codehash = str(token.get("runtime_codehash_keccak256", "")).lower()
    pinned_codehash = str(rpc_source.get("runtime_codehash_keccak256", "")).lower()
    policy_codehash = str(policy_asset.get("runtime_codehash_keccak256", "")).lower()
    if (
        not observed_codehash
        == pinned_codehash
        == policy_codehash
        == _EXPECTED_LBTC_CODEHASH
    ):
        raise ValueError("LBTC runtime codehash mismatch")

    constructor = source_metadata.get("decoded_constructor_args", {})
    source = verified_contract.get("source_code", "")
    if not (
        verified_contract.get("contract_name") == "MockERC20"
        and source_metadata.get("is_verified") is True
        and source_metadata.get("is_fully_verified") is True
        and constructor == {"name": "Loot BTC", "symbol": "LBTC", "decimals": 8}
        and "function mint(address to, uint256 amount) external" in source
        and "_mint(to, amount);" in source
        and not any(
            marker in source
            for marker in (
                "onlyOwner",
                "onlyRole",
                "AccessControl",
                "Ownable",
                "require(msg.sender",
            )
        )
    ):
        raise ValueError("LBTC verified source does not establish MockERC20 identity")

    mint_call = observation.get("mint_eth_call", {})
    caller = str(mint_call.get("caller", "")).lower()
    creator = str(source_metadata.get("creator_address", "")).lower()
    classifications = [
        observation.get("classification", {}),
        source_classification,
        policy_asset,
    ]
    if not (
        mint_call.get("signature") == "mint(address,uint256)"
        and mint_call.get("success") is True
        and mint_call.get("result") == "0x"
        and mint_call.get("state_change_broadcast") is False
        and caller not in {"", _ZERO_ADDRESS, creator}
        and all(item.get("testnet_only") is True for item in classifications)
        and all(item.get("production_backed_btc") is False for item in classifications)
        and all(item.get("canonical_cbbtc") is False for item in classifications)
        and source_classification.get("unrestricted_external_mint") is True
        and observation.get("classification", {}).get("unrestricted_mint") is True
        and policy_asset.get("unrestricted_mint") is True
    ):
        raise ValueError("LBTC unrestricted test-only mint classification mismatch")

    if not (
        policy.get("test_asset_functional_go") is True
        and policy.get("strategy_activation_go") is False
        and policy.get("activation_allowed") is False
        and policy.get("runtime_enabled_by_default") is False
        and policy.get("mainnet_authorized") is False
        and policy_asset.get("decision") == "functional_go"
    ):
        raise ValueError("LBTC policy authority boundary mismatch")
    required_paused = {
        "current_v2_oracle_binding",
        "current_v2_whitelist_products",
        "current_v2_router_and_pool",
        "physical_settlement",
        "executable_liquidity",
    }
    if any(
        policy.get("prerequisites", {}).get(name, {}).get("decision") != "no_go"
        for name in required_paused
    ):
        raise ValueError(
            "LBTC functional identity cannot enable current-stack prerequisites"
        )
    if any(
        decision.get("decision") != "no_go"
        or decision.get("selected_activation_parameters") is not None
        for decision in policy.get("strategy_decisions", {}).values()
    ):
        raise ValueError("LBTC functional identity cannot activate a strategy")

    return {
        "address": token["address"],
        "chain_id": observation["chain_id"],
        "block_number": block["number"],
        "name": token["name"],
        "symbol": token["symbol"],
        "decimals": token["decimals"],
        "runtime_codehash_keccak256": token["runtime_codehash_keccak256"],
        "unrestricted_mint": True,
        "test_asset_functional_go": True,
        "strategy_activation_go": False,
    }


def load_published_btc_rows(path: Path) -> list[dict[str, Any]]:
    """Load only B1N-345 BTC CSP and Wheel research rows."""
    rows = []
    with gzip.open(path, "rt") as source:
        for line in source:
            row = json.loads(line)
            if row.get("asset") == "BTC" and row.get("strategy") in {
                "csp_only",
                "wheel",
            }:
                rows.append(row)
    if not rows:
        raise ValueError("published B1N-345 artifact has no BTC policy rows")
    return rows


def _source_entry(manifest: dict[str, Any], source_id: str) -> dict[str, Any]:
    matches = [
        source
        for source in manifest.get("sources", [])
        if source.get("id") == source_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {source_id} source")
    return matches[0]


def _verify_published_btc_sources(
    rows_path: Path,
    config_path: Path,
    source_manifest_path: Path,
) -> tuple[dict[str, Any], datetime]:
    manifest = json.loads(source_manifest_path.read_text())
    source = _source_entry(manifest, "b1n_345_btc_backtest")
    package_root = source_manifest_path.parent.parent
    expected_rows = (package_root / source["rows_path"]).resolve()
    expected_config = (package_root / source["config_path"]).resolve()
    if rows_path.resolve() != expected_rows or config_path.resolve() != expected_config:
        raise ValueError("B1N-345 source path does not match pinned manifest")
    if _sha256(rows_path) != source["rows_sha256"]:
        raise ValueError("B1N-345 rows hash mismatch")
    if _sha256(config_path) != source["config_sha256"]:
        raise ValueError("B1N-345 config hash mismatch")
    btc_market_path = config_path.parent / "production_data/BTC/market.json"
    if _sha256(btc_market_path) != source["btc_market_sha256"]:
        raise ValueError("B1N-345 BTC market hash mismatch")
    cutoff = _parse_timestamp(source["historical_cutoff"], "historical cutoff")
    return source, cutoff


def build_btc_parameter_matrix(
    rows_path: Path,
    config_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    """Apply pinned B1N-345 gates independently to CSP and Wheel."""
    source, cutoff = _verify_published_btc_sources(
        rows_path, config_path, source_manifest_path
    )
    rows = load_published_btc_rows(rows_path)
    for row in rows:
        window_end = _parse_timestamp(row.get("window_end"), "window_end")
        if window_end > cutoff:
            raise ValueError("B1N-345 row exceeds pinned historical cutoff")
    settings = load_settings(config_path)
    strategies: dict[str, Any] = {}
    for source_strategy, policy_strategy in (
        ("csp_only", "standalone_csp"),
        ("wheel", "wheel"),
    ):
        strategy_rows = [
            {**row, "strategy": "wheel"}
            for row in rows
            if row["strategy"] == source_strategy
        ]
        summary = build_production_summary(strategy_rows, settings)
        matrix = []
        for item in summary["policies"]:
            matrix.append(
                {
                    "window_days": item["window_days"],
                    "target_delta": item["target_delta"],
                    "sample_count": item["sample_count"],
                    "effective_sample_count": item["effective_sample_count"],
                    "median_return": item["return_distribution"]["p50"],
                    "p05_return": item["return_distribution"]["p5"],
                    "loss_probability": item["loss_probability"],
                    "worst_maximum_drawdown": item["worst_maximum_drawdown"],
                    "median_mm_hedged_return": item["mm_hedged_return_distribution"][
                        "p50"
                    ],
                    "maximum_modeled_jointly_viable_aum_usdc": item[
                        "maximum_modeled_jointly_viable_aum_usdc"
                    ],
                    "historical_gate_checks": item["checks"],
                    "historical_gate_pass": item["production_ready"],
                    "activation_decision": "no_go",
                    "activation_blockers": [
                        "PREMIUMS_AND_LIQUIDITY_MODELED_NOT_EXECUTABLE",
                        "BASE_SEPOLIA_BTC_PRODUCT_UNQUALIFIED",
                        "CURRENT_V2_LBTC_PRODUCT_UNATTESTED",
                    ],
                }
            )
        strategies[policy_strategy] = {
            "evaluated_parameters": {
                "cadence_hours": settings.cadence_hours,
                "target_deltas": list(settings.production_validation.target_deltas),
                "utilization": settings.production_validation.utilization,
                "minimum_premium_bps": (
                    settings.production_validation.minimum_premium_bps
                ),
                "wheel_call_margin_usd": (
                    settings.production_validation.call_margin_usd
                    if policy_strategy == "wheel"
                    else None
                ),
            },
            "selected_activation_parameters": None,
            "decision": "no_go",
            "matrix": matrix,
        }

    return {
        "schema_version": 1,
        "authority_issue": "B1N-440",
        "source_issue": "B1N-345",
        "historical_cutoff": source["historical_cutoff"],
        "observation_rules": {
            "spot": "observed hourly Deribit BTC-PERPETUAL close, causal after candle end",
            "iv": "observed hourly Deribit BTC DVOL",
            "premium": "modeled by Binary pricing functions; never an executable fill",
            "liquidity": "modeled sensitivity; never an observed order-book limit",
        },
        "strategies": strategies,
        "standalone_covered_call": {
            "backtest_available": False,
            "selected_activation_parameters": None,
            "decision": "no_go",
            "reason": (
                "B1N-345 has no independent BTC covered-call cohort; Wheel call rows "
                "are contingent on CSP assignment and cannot be relabeled standalone."
            ),
        },
        "mainnet_authorized": False,
    }
