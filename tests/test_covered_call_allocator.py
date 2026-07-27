import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config
from src.covered_call_allocator import (
    CoveredCallFundAllocator,
    CoveredCallPolicy,
    call_collateral_for_option_amount,
    call_collateral_target,
    count_called_away,
    load_covered_call_policy,
    normalization_minimum_weth_out,
    option_amount_for_call_collateral,
    select_covered_call_quote,
)
from src.fund_tx import ConfirmedTransaction


POLICY_PATH = (
    Path(__file__).parents[1]
    / "policies"
    / "covered_call_fund_policy.v1.base-sepolia.json"
)


def _policy() -> CoveredCallPolicy:
    return load_covered_call_policy(POLICY_PATH)


def _quote(now: int, **overrides):
    value = {
        "asset": "eth",
        "chain": "base",
        "is_put": False,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 2150,
        "bid_price": 1,
        "max_amount": 250_000,
        "quote_id": 1,
        "maker_nonce": 2,
        "otoken_address": "0x" + "12" * 20,
    }
    return value | overrides


def test_policy_is_exactly_bounded_and_weth_only():
    policy = _policy()
    assert float(policy.target_delta) == pytest.approx(0.05)
    assert policy.target_utilization_bps == 2500
    assert policy.maximum_vault_aum == 10**16
    assert policy.maximum_collateral == 2_500_000_000_000_000
    assert policy.minimum_net_premium_bps == 10
    assert policy.maximum_open_positions == 1


def test_policy_loader_rejects_parameter_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["selection"]["target_utilization_bps"] = 8000
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="not approved"):
        load_covered_call_policy(changed)


def test_collateral_is_one_to_one_with_otoken_amount():
    policy = _policy()
    target = call_collateral_target(10**16, policy)
    assert target == policy.maximum_collateral
    amount = option_amount_for_call_collateral(target)
    assert amount == 250_000
    assert call_collateral_for_option_amount(amount) == target


def test_quote_selection_requires_48h_otm_call_near_target_delta():
    now = int(time.time())
    selected = select_covered_call_quote(
        [
            _quote(now, is_put=True),
            _quote(now, expiry=now + 12 * 3600),
            _quote(now, strike_price=2050),
            _quote(now, expiry=now + 36 * 3600, strike_price=2125),
            _quote(now),
        ],
        spot=2000,
        iv=0.6,
        now=now,
        risk_free_rate=0.05,
        policy=_policy(),
    )
    assert selected is not None
    assert selected["strike_price"] == 2150
    assert selected["expiry"] == now + 48 * 3600


def test_missing_approved_quote_fails_closed():
    now = int(time.time())
    assert (
        select_covered_call_quote(
            [_quote(now, strike_price=2050)],
            spot=2000,
            iv=0.6,
            now=now,
            risk_free_rate=0.05,
            policy=_policy(),
        )
        is None
    )


def test_normalization_floor_matches_contract_rounding():
    # $2,000/WETH uses an 8-decimal oracle value.
    amount = 32 * 10**6
    minimum = normalization_minimum_weth_out(amount, 2000 * 10**8, 500)
    assert minimum == 15_200_000_000_000_000


def test_called_away_count_is_reconstructed_from_onchain_positions():
    position = [0] * 15
    position[13] = 4
    otm = [0] * 15
    otm[13] = 3
    assert count_called_away([tuple(position), tuple(otm), tuple(position)]) == 2


def test_stale_nav_prevents_any_lifecycle_action():
    allocator = object.__new__(CoveredCallFundAllocator)
    allocator.policy = _policy()
    allocator.valuator_address = "0x" + "34" * 20
    state = {
        "nav": (
            0,
            0,
            1,
            1,
            0,
            10,
            11,
            12,
            1,
            1,
            b"a" * 32,
            b"",
            b"",
            0,
            b"",
        ),
        "block": 20,
        "strategy_hash": b"a" * 32,
        "processing": False,
        "valuation_policy": (1, 1000, 2),
        "strategy_config": (
            True,
            2500,
            10000,
            0,
            1,
            allocator.valuator_address,
            2_500_000_000_000_000,
        ),
        "adapter_config": (
            (
                129600,
                216000,
                3600,
                10,
                500,
                1,
                2500,
                1000 * 10**8,
                10000 * 10**8,
                2_500_000_000_000_000,
                32 * 10**6,
            ),
            "0x" + "56" * 20,
            3000,
        ),
        "minimum_idle_bps": 0,
    }
    with pytest.raises(RuntimeError, match="No coherent active NAV"):
        allocator._validate_policy_gates(state)


def test_pending_physical_delivery_can_progress_without_impossible_nav():
    allocator = object.__new__(CoveredCallFundAllocator)
    allocator.policy = _policy()
    allocator.valuator_address = "0x" + "34" * 20
    state = {
        "nav": (
            0,
            0,
            1,
            1,
            0,
            10,
            11,
            12,
            1,
            1,
            b"a" * 32,
            b"",
            b"",
            0,
            b"",
        ),
        "block": 20,
        "strategy_hash": b"b" * 32,
        "processing": False,
        "valuation_policy": (1, 1000, 2),
        "strategy_config": (
            True,
            2500,
            10000,
            0,
            1,
            allocator.valuator_address,
            2_500_000_000_000_000,
        ),
        "adapter_config": (
            (
                129600,
                216000,
                3600,
                10,
                500,
                1,
                2500,
                1000 * 10**8,
                10000 * 10**8,
                2_500_000_000_000_000,
                32 * 10**6,
            ),
            "0x" + "56" * 20,
            3000,
        ),
        "minimum_idle_bps": 0,
    }
    allocator._validate_policy_gates(state, require_active_nav=False)


def test_terminal_usdc_is_normalized_before_any_reopen():
    allocator = object.__new__(CoveredCallFundAllocator)
    allocator.policy = _policy()
    allocator.weth = "0x" + "11" * 20
    allocator.adapter_address = "0x" + "22" * 20
    allocator.policy_hash = "policy"
    allocator.oracle = MagicMock()
    allocator.oracle.functions.getPrice.return_value.call.return_value = 2000 * 10**8
    allocator.strategy = MagicMock()
    allocator.w3 = MagicMock()
    allocator.w3.codec.encode.return_value = b"normalize"
    allocator._send = MagicMock(
        return_value=ConfirmedTransaction("0xtx", 1, 10, "0xblock", False)
    )
    allocator._result_state = MagicMock(return_value=(2, b"h" * 32, 1, 0, 0, 0, 0))
    nav = [0] * 15
    nav[9] = 7
    nav[11] = b"r" * 32
    state = {
        "adapter_state": (1, b"", 1, 0, 0, 0, 32 * 10**6),
        "allocated": 2_499_999_999_999_999,
        "nav": tuple(nav),
    }

    assert allocator._settle_or_normalize(state) is True
    allocator.w3.codec.encode.assert_called_once_with(
        ["(uint8,uint256,uint256,uint256)"],
        [(2, 0, 32 * 10**6, 15_200_000_000_000_000)],
    )
    allocator.strategy.functions.deallocate.assert_called_once_with(
        allocator.adapter_address,
        state["allocated"],
        0,
        b"normalize",
    )


def test_allocator_and_processor_keys_must_be_separate(monkeypatch):
    private_key = "0x" + "11" * 32
    monkeypatch.setattr(config, "COVERED_CALL_ALLOCATOR_ENABLED", True)
    monkeypatch.setattr(config, "COVERED_CALL_ALLOCATOR_PRIVATE_KEY", private_key)
    monkeypatch.setattr(config, "COVERED_CALL_PROCESSOR_PRIVATE_KEY", private_key)
    monkeypatch.setattr(config, "COVERED_CALL_VAULT_ADDRESS", "0x" + "12" * 20)
    monkeypatch.setattr(config, "COVERED_CALL_FLOW_MANAGER_ADDRESS", "0x" + "13" * 20)
    monkeypatch.setattr(config, "CHAIN_ID", 84532)
    monkeypatch.setattr(
        config, "_current_environment", MagicMock(return_value="staging")
    )
    from src.covered_call_operations_keeper import CoveredCallFundOperationsKeeper

    with pytest.raises(RuntimeError, match="separate keys"):
        CoveredCallFundOperationsKeeper._validate_runtime_config()
