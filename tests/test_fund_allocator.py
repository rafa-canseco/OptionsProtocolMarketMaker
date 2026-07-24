import json
from pathlib import Path

import pytest

from src.fund_allocator import (
    load_testnet_policy,
    option_amount_for_collateral,
    policy_strike,
    required_collateral,
    select_policy_quote,
)


POLICY_PATH = (
    Path(__file__).parents[1] / "policies" / "csp_fund_policy.v2.base-sepolia.json"
)


def test_approved_policy_is_testnet_only_and_bounded():
    policy = load_testnet_policy(POLICY_PATH)

    assert policy_strike(1859.32, policy) == 1575
    assert policy.maximum_vault_aum == 1_000_000_000
    assert policy.maximum_collateral == 800_000_000
    assert policy.maximum_open_positions == 1


def test_policy_rejects_parameter_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["selection"]["target_utilization_bps"] = 8001
    drifted = tmp_path / "policy.json"
    drifted.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="approved B1N-341"):
        load_testnet_policy(drifted)


def test_selects_only_exact_strike_put_in_expiry_window():
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    expected = {
        "asset": "eth",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
    }
    quotes = [
        expected | {"strike_price": 1600.0},
        expected | {"is_put": False},
        expected | {"expiry": now + 61 * 3600},
        expected,
    ]

    assert select_policy_quote(quotes, spot=1859.32, now=now, policy=policy) is expected


def test_collateral_round_trip_never_exceeds_target():
    strike_raw = 1575 * 10**8
    target = 800 * 10**6

    amount = option_amount_for_collateral(target, strike_raw)
    collateral = required_collateral(amount, strike_raw)

    assert amount == 50_793_650
    assert collateral == 799_999_988
    assert collateral <= target
