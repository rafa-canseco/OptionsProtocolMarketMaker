"""Versioned, hash-pinned policy for the Base Sepolia Meta Wheel.

This module is intentionally independent from the standalone CSP and Covered
Call policy loaders.  A Wheel worker must receive the approved SHA-256 through
configuration (and later compare it with the coordinator's on-chain hash); a
policy file is not self-authorizing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BPS = 10_000
BASE_SEPOLIA_CHAIN_ID = 84_532

_ROOT_FIELDS = {
    "activation_allowed",
    "assignment",
    "authority_issue",
    "cadence",
    "decision",
    "evidence",
    "fees",
    "liquidity",
    "mainnet_authorized",
    "policy_id",
    "runtime_enabled_by_default",
    "schema_version",
    "selection",
    "scope",
    "tranches",
}
_SCOPE_FIELDS = {
    "accounting_asset",
    "chain_id",
    "child_isolation",
    "environment",
    "redemption_asset",
    "strategy",
    "underlying",
}
_CADENCE_FIELDS = {
    "market_maximum_age_seconds",
    "maximum_expiry_delay_seconds",
    "minimum_expiry_delay_seconds",
    "quote_maximum_age_seconds",
    "quote_minimum_ttl_seconds",
    "settlement_maximum_delay_seconds",
    "target_duration_hours",
}
_TRANCHE_FIELDS = {
    "maximum_active_option_per_lane",
    "maximum_cc_lanes",
    "maximum_csp_lanes",
    "maximum_parent_aum_usdc",
    "maximum_weth_per_cc_lane",
    "maximum_usdc_per_csp_lane",
}
_ASSIGNMENT_FIELDS = {
    "below_floor_emergency_action",
    "call_strike_rule",
    "execution_cost_buffer_usd",
    "floor_basis",
    "no_quote_action",
    "premiums_may_reduce_floor",
    "strike_tick_usd",
}
_SELECTION_FIELDS = {
    "csp_maximum_expiry_delay_seconds",
    "csp_minimum_net_premium_bps",
    "csp_strike_rule",
    "csp_strike_tick_usd",
    "maximum_call_delta_deviation_bps",
    "maximum_execution_slippage_bps",
    "maximum_put_delta_deviation_bps",
    "minimum_net_premium_bps",
    "target_call_delta_bps",
    "target_put_delta_bps",
}
_LIQUIDITY_FIELDS = {
    "csp_target_utilization_bps",
    "maximum_idle_duration_hours",
    "parent_liquid_reserve_bps",
    "redemption_priority",
    "redemption_reserve_rule",
}
_FEE_FIELDS = {
    "child_management_fee_bps",
    "child_performance_fee_bps",
    "parent_management_fee_bps_annual",
    "parent_performance_fee_bps_hwm",
    "protocol_gross_premium_fee_bps",
}
_EVIDENCE_FIELDS = {
    "economic_observation_class",
    "mainnet_production_ready",
    "production_ready_fixed_policies",
    "research_issue",
    "testnet_scope",
}


@dataclass(frozen=True)
class MetaWheelPolicy:
    policy_id: str
    policy_hash: str
    chain_id: int
    activation_allowed: bool
    mainnet_authorized: bool
    target_duration_seconds: int
    min_expiry_delay: int
    max_expiry_delay: int
    csp_max_expiry_delay: int
    quote_maximum_age: int
    market_maximum_age: int
    quote_minimum_ttl: int
    settlement_maximum_delay: int
    maximum_csp_lanes: int
    maximum_cc_lanes: int
    maximum_active_option_per_lane: int
    maximum_parent_aum: int
    maximum_usdc_per_csp_lane: int
    maximum_weth_per_cc_lane: int
    execution_cost_buffer: int
    strike_tick: int
    target_put_delta_bps: int
    maximum_put_delta_deviation_bps: int
    csp_strike_tick: int
    target_call_delta_bps: int
    maximum_call_delta_deviation_bps: int
    maximum_execution_slippage_bps: int
    csp_target_utilization_bps: int
    parent_liquid_reserve_bps: int
    maximum_idle_duration_seconds: int
    csp_minimum_net_premium_bps: int
    minimum_net_premium_bps: int
    protocol_gross_premium_fee_bps: int
    parent_management_fee_bps: int
    parent_performance_fee_bps: int


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _exact_fields(
    value: Any, expected: set[str], *, path: Path, label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid Meta Wheel policy {path}: {label} must be an object")
    fields = set(value)
    if fields != expected:
        raise ValueError(
            f"Invalid Meta Wheel policy {path} {label}: "
            f"missing={sorted(expected - fields)}, unknown={sorted(fields - expected)}"
        )
    return value


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Meta Wheel policy {label} must be a positive integer")
    return value


def _bounded_bps(value: Any, *, label: str, allow_zero: bool = False) -> int:
    lower = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not lower <= value <= BPS
    ):
        raise ValueError(f"Meta Wheel policy {label} must be in [{lower}, {BPS}]")
    return value


def load_meta_wheel_policy(
    path: str | Path, *, approved_hash: str | None
) -> MetaWheelPolicy:
    """Load a policy only when an external approval hash pins its exact bytes."""
    policy_path = Path(path)
    if not approved_hash or len(approved_hash) != 64:
        raise ValueError("META_WHEEL_APPROVED_POLICY_SHA256 is required")
    actual_hash = sha256_file(policy_path)
    if actual_hash != approved_hash.lower():
        raise ValueError("Meta Wheel policy hash does not match approved hash")
    try:
        raw = json.loads(policy_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Unable to load Meta Wheel policy {policy_path}: {error}"
        ) from error

    root = _exact_fields(raw, _ROOT_FIELDS, path=policy_path, label="root")
    scope = _exact_fields(root["scope"], _SCOPE_FIELDS, path=policy_path, label="scope")
    cadence = _exact_fields(
        root["cadence"], _CADENCE_FIELDS, path=policy_path, label="cadence"
    )
    tranches = _exact_fields(
        root["tranches"], _TRANCHE_FIELDS, path=policy_path, label="tranches"
    )
    assignment = _exact_fields(
        root["assignment"], _ASSIGNMENT_FIELDS, path=policy_path, label="assignment"
    )
    selection = _exact_fields(
        root["selection"], _SELECTION_FIELDS, path=policy_path, label="selection"
    )
    liquidity = _exact_fields(
        root["liquidity"], _LIQUIDITY_FIELDS, path=policy_path, label="liquidity"
    )
    fees = _exact_fields(root["fees"], _FEE_FIELDS, path=policy_path, label="fees")
    evidence = _exact_fields(
        root["evidence"], _EVIDENCE_FIELDS, path=policy_path, label="evidence"
    )

    expected_scope = {
        "accounting_asset": "USDC",
        "chain_id": BASE_SEPOLIA_CHAIN_ID,
        "child_isolation": "dedicated_wheel_lanes_only",
        "environment": "base_sepolia",
        "redemption_asset": "USDC",
        "strategy": "meta_wheel",
        "underlying": "ETH",
    }
    authorization = (
        root["schema_version"] == 2
        and root["policy_id"] == "eth_usdc_meta_wheel_base_sepolia_v2"
        and root["authority_issue"] == "B1N-438"
        and root["decision"] == "go_testnet_only"
        and root["activation_allowed"] is True
        and root["runtime_enabled_by_default"] is False
        and root["mainnet_authorized"] is False
        and scope == expected_scope
    )
    if not authorization:
        raise ValueError("Meta Wheel policy is not an approved Base Sepolia policy")

    if cadence["target_duration_hours"] != 48:
        raise ValueError("Meta Wheel policy must use the approved 48-hour cadence")
    min_expiry = _positive_int(
        cadence["minimum_expiry_delay_seconds"], label="minimum_expiry_delay_seconds"
    )
    max_expiry = _positive_int(
        cadence["maximum_expiry_delay_seconds"], label="maximum_expiry_delay_seconds"
    )
    if not min_expiry <= 48 * 3600 <= max_expiry:
        raise ValueError("Meta Wheel expiry bounds exclude the target duration")

    maximum_csp_lanes = _positive_int(
        tranches["maximum_csp_lanes"], label="maximum_csp_lanes"
    )
    maximum_cc_lanes = _positive_int(
        tranches["maximum_cc_lanes"], label="maximum_cc_lanes"
    )
    if maximum_csp_lanes > 8 or maximum_cc_lanes > 8:
        raise ValueError("Meta Wheel lane count exceeds the reviewed bound")
    if tranches["maximum_active_option_per_lane"] != 1:
        raise ValueError("Initial Meta Wheel lanes must allow one active option")

    expected_assignment = {
        "below_floor_emergency_action": "none_pause_and_wait_for_safe_usdc",
        "call_strike_rule": "at_or_above_literal_floor_plus_buffer",
        "execution_cost_buffer_usd": assignment["execution_cost_buffer_usd"],
        "floor_basis": "maximum_literal_csp_assignment_strike",
        "no_quote_action": "hold_weth_idle_and_retry",
        "premiums_may_reduce_floor": False,
        "strike_tick_usd": assignment["strike_tick_usd"],
    }
    if assignment != expected_assignment:
        raise ValueError("Meta Wheel assignment-floor semantics are not approved")

    expected_liquidity = {
        "csp_target_utilization_bps": liquidity["csp_target_utilization_bps"],
        "maximum_idle_duration_hours": liquidity["maximum_idle_duration_hours"],
        "parent_liquid_reserve_bps": liquidity["parent_liquid_reserve_bps"],
        "redemption_priority": "before_new_csp_allocation",
        "redemption_reserve_rule": "max_pending_claims_and_base_reserve",
    }
    if liquidity != expected_liquidity:
        raise ValueError("Meta Wheel redemption priority differs from policy")

    expected_fees = {
        "child_management_fee_bps": 0,
        "child_performance_fee_bps": 0,
        "parent_management_fee_bps_annual": 200,
        "parent_performance_fee_bps_hwm": 1000,
        "protocol_gross_premium_fee_bps": 1000,
    }
    if fees != expected_fees:
        raise ValueError("Meta Wheel fee policy would double-charge or drift")
    if evidence != {
        "economic_observation_class": "modeled",
        "mainnet_production_ready": False,
        "production_ready_fixed_policies": 0,
        "research_issue": "B1N-345",
        "testnet_scope": "functional_validation_only",
    }:
        raise ValueError("Meta Wheel evidence boundary is not explicit")

    expected_selection = {
        "csp_maximum_expiry_delay_seconds": 216000,
        "csp_minimum_net_premium_bps": 20,
        "csp_strike_rule": "target_absolute_put_delta",
        "csp_strike_tick_usd": 25,
        "maximum_call_delta_deviation_bps": 150,
        "maximum_execution_slippage_bps": 100,
        "maximum_put_delta_deviation_bps": 150,
        "minimum_net_premium_bps": 10,
        "target_call_delta_bps": 500,
        "target_put_delta_bps": 900,
    }
    if selection != expected_selection:
        raise ValueError("Meta Wheel option selection differs from B1N-438 policy")

    protocol_fee_bps = _bounded_bps(
        fees["protocol_gross_premium_fee_bps"], label="protocol fee"
    )
    csp_minimum_net_premium_bps = _bounded_bps(
        selection["csp_minimum_net_premium_bps"],
        label="csp_minimum_net_premium_bps",
    )
    minimum_net_premium_bps = _bounded_bps(
        selection["minimum_net_premium_bps"], label="minimum_net_premium_bps"
    )
    return MetaWheelPolicy(
        policy_id=str(root["policy_id"]),
        policy_hash=actual_hash,
        chain_id=BASE_SEPOLIA_CHAIN_ID,
        activation_allowed=True,
        mainnet_authorized=False,
        target_duration_seconds=48 * 3600,
        min_expiry_delay=min_expiry,
        max_expiry_delay=max_expiry,
        csp_max_expiry_delay=_positive_int(
            selection["csp_maximum_expiry_delay_seconds"],
            label="csp_maximum_expiry_delay_seconds",
        ),
        quote_maximum_age=_positive_int(
            cadence["quote_maximum_age_seconds"], label="quote_maximum_age_seconds"
        ),
        market_maximum_age=_positive_int(
            cadence["market_maximum_age_seconds"], label="market_maximum_age_seconds"
        ),
        quote_minimum_ttl=_positive_int(
            cadence["quote_minimum_ttl_seconds"], label="quote_minimum_ttl_seconds"
        ),
        settlement_maximum_delay=_positive_int(
            cadence["settlement_maximum_delay_seconds"],
            label="settlement_maximum_delay_seconds",
        ),
        maximum_csp_lanes=maximum_csp_lanes,
        maximum_cc_lanes=maximum_cc_lanes,
        maximum_active_option_per_lane=1,
        maximum_parent_aum=_positive_int(
            tranches["maximum_parent_aum_usdc"], label="maximum_parent_aum_usdc"
        )
        * 10**6,
        maximum_usdc_per_csp_lane=_positive_int(
            tranches["maximum_usdc_per_csp_lane"],
            label="maximum_usdc_per_csp_lane",
        )
        * 10**6,
        maximum_weth_per_cc_lane=_weth_amount(tranches["maximum_weth_per_cc_lane"]),
        execution_cost_buffer=_positive_int(
            assignment["execution_cost_buffer_usd"],
            label="execution_cost_buffer_usd",
        )
        * 10**8,
        strike_tick=_positive_int(
            assignment["strike_tick_usd"], label="strike_tick_usd"
        )
        * 10**8,
        target_put_delta_bps=_bounded_bps(
            selection["target_put_delta_bps"], label="target_put_delta_bps"
        ),
        maximum_put_delta_deviation_bps=_bounded_bps(
            selection["maximum_put_delta_deviation_bps"],
            label="maximum_put_delta_deviation_bps",
        ),
        csp_strike_tick=_positive_int(
            selection["csp_strike_tick_usd"], label="csp_strike_tick_usd"
        )
        * 10**8,
        target_call_delta_bps=_bounded_bps(
            selection["target_call_delta_bps"], label="target_call_delta_bps"
        ),
        maximum_call_delta_deviation_bps=_bounded_bps(
            selection["maximum_call_delta_deviation_bps"],
            label="maximum_call_delta_deviation_bps",
        ),
        maximum_execution_slippage_bps=_bounded_bps(
            selection["maximum_execution_slippage_bps"],
            label="maximum_execution_slippage_bps",
            allow_zero=True,
        ),
        csp_target_utilization_bps=_bounded_bps(
            liquidity["csp_target_utilization_bps"],
            label="csp_target_utilization_bps",
        ),
        parent_liquid_reserve_bps=_bounded_bps(
            liquidity["parent_liquid_reserve_bps"],
            label="parent_liquid_reserve_bps",
            allow_zero=True,
        ),
        maximum_idle_duration_seconds=_positive_int(
            liquidity["maximum_idle_duration_hours"],
            label="maximum_idle_duration_hours",
        )
        * 3600,
        csp_minimum_net_premium_bps=csp_minimum_net_premium_bps,
        minimum_net_premium_bps=minimum_net_premium_bps,
        protocol_gross_premium_fee_bps=protocol_fee_bps,
        parent_management_fee_bps=fees["parent_management_fee_bps_annual"],
        parent_performance_fee_bps=fees["parent_performance_fee_bps_hwm"],
    )


def _weth_amount(value: Any) -> int:
    # JSON numeric values are bounded testnet configuration, not token math.
    try:
        text = format(float(value), ".18f")
    except (TypeError, ValueError) as error:
        raise ValueError("maximum_weth_per_cc_lane must be numeric") from error
    whole, fractional = text.split(".")
    raw = int(whole) * 10**18 + int(fractional[:18].ljust(18, "0"))
    return _positive_int(raw, label="maximum_weth_per_cc_lane")
