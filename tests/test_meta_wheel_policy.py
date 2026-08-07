import json
from pathlib import Path

import pytest

from src.backtest.meta_wheel_policy import apply_current_fees
from src.meta_wheel_policy import load_meta_wheel_policy, sha256_file


POLICY_PATH = Path("policies/meta_wheel_policy.v1.base-sepolia.json")


def test_meta_wheel_policy_requires_exact_external_hash():
    policy = load_meta_wheel_policy(POLICY_PATH, approved_hash=sha256_file(POLICY_PATH))

    assert policy.activation_allowed is True
    assert policy.mainnet_authorized is False
    assert policy.maximum_csp_lanes == 4
    assert policy.maximum_cc_lanes == 4
    assert policy.execution_cost_buffer == 10 * 10**8
    assert policy.protocol_gross_premium_fee_bps == 1000
    assert policy.parent_management_fee_bps == 200
    assert policy.parent_performance_fee_bps == 1000


def test_meta_wheel_policy_fails_closed_without_or_with_wrong_hash():
    with pytest.raises(ValueError, match="required"):
        load_meta_wheel_policy(POLICY_PATH, approved_hash=None)
    with pytest.raises(ValueError, match="does not match"):
        load_meta_wheel_policy(POLICY_PATH, approved_hash="0" * 64)


def test_meta_wheel_policy_rejects_fee_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["fees"]["child_management_fee_bps"] = 1
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="double-charge or drift"):
        load_meta_wheel_policy(drifted, approved_hash=sha256_file(drifted))


def test_current_fee_overlay_charges_once_and_respects_hwm():
    profitable = apply_current_fees(
        {
            "initial_usdc": 100_000,
            "final_nav_usdc": 110_000,
            "premium_gross_usdc": 5_000,
            "window_days": 365,
        }
    )
    losing = apply_current_fees(
        {
            "initial_usdc": 100_000,
            "final_nav_usdc": 90_000,
            "premium_gross_usdc": 5_000,
            "window_days": 30,
        }
    )

    assert profitable["protocol_premium_fee_usdc"] == 500
    assert profitable["parent_management_fee_usdc"] == 2190
    assert profitable["parent_performance_fee_usdc"] == 731
    assert losing["parent_performance_fee_usdc"] == 0
