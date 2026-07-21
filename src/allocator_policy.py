from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TOP_LEVEL_FIELDS = {
    "activation_allowed",
    "allocator_actions",
    "base_sepolia_overrides",
    "curator_bounds",
    "decision",
    "decision_gates",
    "evidence",
    "observed_metrics",
    "policy_id",
    "schema_version",
    "scope",
    "selection",
    "tested_policy",
}
_SELECTION_FIELDS = {
    "maximum_collateral_per_position_usdc",
    "maximum_utilization",
    "minimum_net_premium_bps",
    "quote_liquidity_minimum_usdc",
    "reopen_cadence_hours",
    "target_duration_hours",
    "target_put_delta",
}
_TESTNET_OVERRIDE_FIELDS = {
    "allocator_enabled",
    "label",
    "maximum_collateral_per_position_usdc",
    "maximum_utilization",
    "maximum_vault_aum_usdc",
}
_CURATOR_FIELDS = {
    "maximum_deallocation_loss_bps",
    "maximum_per_window_outflow_usdc",
    "maximum_rolling_outflow_usdc",
    "maximum_utilization",
    "maximum_weth_inventory",
    "nav_reporter_max_age_seconds",
    "option_liability_haircut_bps",
}
_GATE_FIELDS = {
    "assignment",
    "maximum_loss_probability",
    "maximum_worst_drawdown",
    "minimum_period_return",
    "quote_liquidity",
}
_EVIDENCE_FIELDS = {
    "decision_issue",
    "evidence_issue",
    "market_maker_staging_commit",
    "paths",
}
_METRIC_FIELDS = {
    "expected_shortfall_5",
    "loss_probability",
    "median_return",
    "premium_observation_class",
    "strategy",
    "target_delta",
    "window_days",
    "worst_drawdown",
}
_TESTED_POLICY_FIELDS = {
    "cost_scenario",
    "minimum_premium_bps",
    "premium_observation_class",
    "reopen_cadence_hours",
    "strategy",
    "target_deltas",
    "utilization",
}
_MINIMUM_RETURNS = {
    "30": 0.006498656494542621,
    "90": 0.01962294154707611,
    "180": 0.03963094292911218,
}
_EVIDENCE_PATHS = {
    "backtests/b1n_345/config.json",
    "backtests/b1n_345/production_results/results.jsonl.gz",
    "backtests/b1n_345/production_results/coverage_probe.json",
    "backtests/b1n_345/production_results/checksums.sha256",
}
_TESTED_POLICY = {
    "cost_scenario": "base",
    "minimum_premium_bps": 0,
    "premium_observation_class": "modeled",
    "reopen_cadence_hours": 48,
    "strategy": "csp_only",
    "target_deltas": [0.1, 0.2, 0.3, 0.4],
    "utilization": 1.0,
}
_EXPECTED_METRICS = [
    (30, 0.00906, 0.36585, -0.20676, -0.29426),
    (90, -0.00225, 0.52286, -0.2629, -0.36767),
    (180, -0.03421, 0.67686, -0.30293, -0.36767),
]


@dataclass(frozen=True)
class AllocatorPolicy:
    policy_id: str
    decision: str
    activation_allowed: bool
    selected_parameters: dict[str, Any]
    base_sepolia_overrides: dict[str, Any]


def _require_exact_fields(
    raw: dict[str, Any], expected: set[str], path: Path, label: str
) -> None:
    fields = set(raw)
    if fields != expected:
        missing = sorted(expected - fields)
        unknown = sorted(fields - expected)
        raise ValueError(
            f"Invalid allocator policy {path} {label}: "
            f"missing={missing}, unknown={unknown}"
        )


def _validate_no_go(raw: dict[str, Any], path: Path) -> None:
    if raw["activation_allowed"] is not False:
        raise ValueError(
            f"Invalid allocator policy {path}: no-go must disable activation"
        )
    selection = raw["selection"]
    if not isinstance(selection, dict):
        raise ValueError(
            f"Invalid allocator policy {path}: selection must be an object"
        )
    _require_exact_fields(selection, _SELECTION_FIELDS, path, "selection")
    if any(value is not None for value in selection.values()):
        raise ValueError(
            f"Invalid allocator policy {path}: no-go cannot select parameters"
        )
    actions = raw["allocator_actions"]
    if not isinstance(actions, dict) or set(actions) != {
        "open_new_positions",
        "reason",
    }:
        raise ValueError(f"Invalid allocator policy {path}: invalid allocator_actions")
    if actions.get("open_new_positions") is not False:
        raise ValueError(
            f"Invalid allocator policy {path}: no-go must block new positions"
        )
    overrides = raw["base_sepolia_overrides"]
    if not isinstance(overrides, dict):
        raise ValueError(
            f"Invalid allocator policy {path}: base_sepolia_overrides must be an object"
        )
    _require_exact_fields(overrides, _TESTNET_OVERRIDE_FIELDS, path, "overrides")
    authorization = (
        overrides.get("allocator_enabled") is False
        and overrides.get("maximum_vault_aum_usdc") == 0
        and overrides.get("maximum_utilization") == 0
        and overrides.get("maximum_collateral_per_position_usdc") == 0
    )
    if not authorization:
        raise ValueError(
            f"Invalid allocator policy {path}: testnet override must fail closed"
        )
    if overrides["label"] != "testnet_only_non_authorizing":
        raise ValueError(f"Invalid allocator policy {path}: invalid testnet label")


def _validate_evidence(raw: dict[str, Any], path: Path) -> None:
    curator = raw["curator_bounds"]
    if not isinstance(curator, dict):
        raise ValueError(
            f"Invalid allocator policy {path}: curator_bounds must be an object"
        )
    _require_exact_fields(curator, _CURATOR_FIELDS, path, "curator_bounds")
    if any(value is not None for value in curator.values()):
        raise ValueError(f"Invalid allocator policy {path}: unvalidated curator bound")
    gates = raw["decision_gates"]
    if not isinstance(gates, dict):
        raise ValueError(
            f"Invalid allocator policy {path}: decision_gates must be an object"
        )
    _require_exact_fields(gates, _GATE_FIELDS, path, "decision_gates")
    fixed_gates = (
        gates["maximum_loss_probability"] == 0.25
        and gates["maximum_worst_drawdown"] == -0.3
        and gates["minimum_period_return"] == _MINIMUM_RETURNS
    )
    if not fixed_gates:
        raise ValueError(
            f"Invalid allocator policy {path}: decision gates differ from evidence"
        )
    for name in ("assignment", "quote_liquidity"):
        gate = gates[name]
        criterion = (
            "physical_weth_assignment_validated"
            if name == "assignment"
            else "observed_executable_quote_liquidity_validated"
        )
        expected = {
            "criterion": criterion,
            "evidence_present": False,
            "passed": False,
            "required": True,
        }
        if gate != expected:
            raise ValueError(f"Invalid allocator policy {path}: invalid {name} gate")


def _validate_traceability(raw: dict[str, Any], path: Path) -> None:
    evidence = raw["evidence"]
    if not isinstance(evidence, dict):
        raise ValueError(f"Invalid allocator policy {path}: evidence must be an object")
    _require_exact_fields(evidence, _EVIDENCE_FIELDS, path, "evidence")
    provenance = (
        evidence["evidence_issue"] == "B1N-345"
        and evidence["decision_issue"] == "B1N-346"
        and evidence["market_maker_staging_commit"]
        == "7b8ffbd6bef1cd754c111c7679adde37b39cfa35"
        and set(evidence["paths"]) == _EVIDENCE_PATHS
    )
    if not provenance:
        raise ValueError(f"Invalid allocator policy {path}: incorrect issue provenance")
    tested = raw["tested_policy"]
    if not isinstance(tested, dict):
        raise ValueError(
            f"Invalid allocator policy {path}: tested_policy must be an object"
        )
    _require_exact_fields(tested, _TESTED_POLICY_FIELDS, path, "tested_policy")
    if tested != _TESTED_POLICY:
        raise ValueError(
            f"Invalid allocator policy {path}: tested policy differs from evidence"
        )
    metrics = raw["observed_metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ValueError(
            f"Invalid allocator policy {path}: observed_metrics must be non-empty"
        )
    compact_metrics = []
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ValueError(
                f"Invalid allocator policy {path}: metric must be an object"
            )
        _require_exact_fields(metric, _METRIC_FIELDS, path, "metric")
        classification = (
            metric["strategy"] == "csp_only"
            and metric["premium_observation_class"] == "modeled"
            and metric["target_delta"] == 0.1
        )
        if not classification:
            raise ValueError(
                f"Invalid allocator policy {path}: invalid metric classification"
            )
        compact_metrics.append(
            (
                metric["window_days"],
                metric["median_return"],
                metric["loss_probability"],
                metric["expected_shortfall_5"],
                metric["worst_drawdown"],
            )
        )
    if compact_metrics != _EXPECTED_METRICS:
        raise ValueError(
            f"Invalid allocator policy {path}: metrics differ from evidence"
        )


def load_allocator_policy(path: Path) -> AllocatorPolicy:
    """Load and validate a versioned allocator policy without runtime secrets."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to load allocator policy {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid allocator policy {path}: root must be an object")
    _require_exact_fields(raw, _TOP_LEVEL_FIELDS, path, "root")
    if raw["schema_version"] != 1:
        raise ValueError(f"Invalid allocator policy {path}: unsupported schema_version")
    if raw["decision"] != "no_go":
        raise ValueError(f"Invalid allocator policy {path}: v1 supports no-go only")
    scope = raw["scope"]
    expected_scope = {
        "chain": "base",
        "collateral": "USDC",
        "milestone": "Hito 1",
        "strategy": "cash_secured_put",
        "underlying": "ETH",
    }
    if scope != expected_scope:
        raise ValueError(f"Invalid allocator policy {path}: unsupported v1 scope")
    _validate_no_go(raw, path)
    _validate_evidence(raw, path)
    _validate_traceability(raw, path)
    return AllocatorPolicy(
        policy_id=str(raw["policy_id"]),
        decision=str(raw["decision"]),
        activation_allowed=bool(raw["activation_allowed"]),
        selected_parameters=dict(raw["selection"]),
        base_sepolia_overrides=dict(raw["base_sepolia_overrides"]),
    )
