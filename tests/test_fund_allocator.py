import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eth_account import Account
from eth_account.messages import encode_typed_data

from src.fund_allocator import (
    CspFundAllocator,
    _FUND_QUOTE_TYPES,
    liquid_collateral_target,
    load_testnet_policy,
    option_amount_for_collateral,
    policy_strike,
    required_collateral,
    safe_block_has_coherent_nav,
    select_policy_quote,
    sign_fund_quote,
    UINT256_MAX,
    validate_allocated_exposure,
    validate_fair_nav_policy,
)
from src import api_client


POLICY_PATH = (
    Path(__file__).parents[1] / "policies" / "csp_fund_policy.v3.base-sepolia.json"
)


def test_approved_policy_is_testnet_only_and_uses_dynamic_idle_sizing():
    policy = load_testnet_policy(POLICY_PATH)

    assert policy_strike(1859.32, policy) == 1575
    assert policy.maximum_vault_aum == UINT256_MAX
    assert policy.maximum_collateral == UINT256_MAX
    assert policy.maximum_open_positions == 1
    assert policy.liquid_usdc_reserve_bps == 2_000
    assert policy.onchain_minimum_idle_bps == 0


def test_policy_rejects_parameter_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["selection"]["target_utilization_bps"] = 8001
    drifted = tmp_path / "policy.json"
    drifted.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="approved B1N-374"):
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


def test_selects_compatible_put_when_duplicate_economics_use_wrong_assets():
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    wrong_assets = {
        "asset": "eth",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
    }
    compatible = wrong_assets | {"deadline": now + 299}

    selected = select_policy_quote(
        [wrong_assets, compatible],
        spot=1859.32,
        now=now,
        policy=policy,
        series_validator=lambda quote: quote is compatible,
    )

    assert selected is compatible


@pytest.mark.parametrize("deployment_status", ["virtual", "creating", "failed"])
def test_lazy_put_series_never_reaches_onchain_validator(deployment_status):
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    validator = MagicMock(
        side_effect=AssertionError("non-ready series must not be read on-chain")
    )
    quote = {
        "asset": "eth",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
        "deployment_status": deployment_status,
    }

    selected = select_policy_quote(
        [quote],
        spot=1859.32,
        now=now,
        policy=policy,
        series_validator=validator,
    )

    assert selected is None
    validator.assert_not_called()


def test_ready_put_series_reaches_onchain_validator():
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    validator = MagicMock(return_value=True)
    quote = {
        "asset": "eth",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
        "deployment_status": "ready",
    }

    selected = select_policy_quote(
        [quote],
        spot=1859.32,
        now=now,
        policy=policy,
        series_validator=validator,
    )

    assert selected is quote
    validator.assert_called_once_with(quote)


def test_virtual_put_can_be_selected_only_for_materialization():
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    validator = MagicMock(
        side_effect=AssertionError("virtual series must not be read on-chain")
    )
    quote = {
        "asset": "eth",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
        "deployment_status": "virtual",
    }

    selected = select_policy_quote(
        [quote],
        spot=1859.32,
        now=now,
        policy=policy,
        series_validator=validator,
        deployment_statuses=frozenset({"virtual", "creating"}),
    )

    assert selected is quote
    validator.assert_not_called()


def test_collateral_round_trip_never_exceeds_target():
    strike_raw = 1575 * 10**8
    target = 800 * 10**6

    amount = option_amount_for_collateral(target, strike_raw)
    collateral = required_collateral(amount, strike_raw)

    assert amount == 50_793_650
    assert collateral == 799_999_988
    assert collateral <= target


def test_assignment_rebases_utilization_on_remaining_liquid_usdc():
    policy = load_testnet_policy(POLICY_PATH)

    assert liquid_collateral_target(250 * 10**6, policy) == 200 * 10**6
    assert liquid_collateral_target(25 * 10**6, policy) == 20 * 10**6
    assert liquid_collateral_target(0, policy) == 0


def test_new_round_recalculates_from_all_current_idle_without_static_cap():
    policy = load_testnet_policy(POLICY_PATH)

    assert liquid_collateral_target(1_250 * 10**6, policy) == 1_000 * 10**6
    assert liquid_collateral_target(3_671 * 10**6, policy) == 2_936_800_000


def test_pending_redemptions_take_priority_over_opening_another_csp(monkeypatch):
    allocator = CspFundAllocator.__new__(CspFundAllocator)
    monkeypatch.setattr(
        api_client,
        "get_market_data",
        lambda **_: pytest.fail("must not fetch a quote while redemptions are pending"),
    )

    allocator._open(
        {
            "adapter_state": (0, b"", 0, 0, 0, 0),
            "allocated": 0,
            "pending_shares": 1,
        }
    )


def test_latest_redemption_request_closes_safe_block_handoff_race(monkeypatch):
    allocator = CspFundAllocator.__new__(CspFundAllocator)
    allocator.flow = MagicMock()
    allocator.flow.functions.totalPendingShares.return_value.call.return_value = 1
    monkeypatch.setattr(
        api_client,
        "get_market_data",
        lambda **_: pytest.fail("must not fetch a quote after a new redeem request"),
    )

    allocator._open(
        {
            "adapter_state": (0, b"", 0, 0, 0, 0),
            "allocated": 0,
            "pending_shares": 0,
        }
    )


def test_virtual_policy_quote_materializes_without_allocating(monkeypatch):
    now = 1_000_000
    allocator = CspFundAllocator.__new__(CspFundAllocator)
    allocator.policy = load_testnet_policy(POLICY_PATH)
    allocator.flow = MagicMock()
    allocator.flow.functions.totalPendingShares.return_value.call.return_value = 0
    allocator.adapter_address = "0x" + "34" * 20
    allocator._is_compatible_put_series = MagicMock(
        side_effect=AssertionError("virtual series must not be read on-chain")
    )
    allocator._send = MagicMock(
        side_effect=AssertionError("capital must stay idle until the series is ready")
    )
    quote = {
        "asset": "eth",
        "chain": "base",
        "is_put": True,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1575.0,
        "deployment_status": "virtual",
        "otoken_address": "0x" + "12" * 20,
        "bid_price": 10,
        "quote_id": 7,
        "max_amount": 100_000_000,
        "maker_nonce": 3,
        "signature": "0x" + "ab" * 65,
    }
    monkeypatch.setattr("src.fund_allocator.time.time", lambda: now)
    monkeypatch.setattr(
        api_client,
        "get_market_data",
        lambda **_: {"spot": 1859.32},
    )
    monkeypatch.setattr(api_client, "get_quotes", lambda: [quote])
    ensure = MagicMock(
        return_value={
            "status": "creating",
            "otoken_address": quote["otoken_address"],
            "deployment_tx_hash": "0x" + "cd" * 32,
        }
    )
    monkeypatch.setattr(api_client, "ensure_fund_series", ensure)

    allocator._open(
        {
            "adapter_state": (0, b"", 0, 0, 0, 0),
            "allocated": 0,
            "pending_shares": 0,
            "idle_assets": 1_000 * 10**6,
        }
    )

    ensure.assert_called_once_with(
        adapter_address=allocator.adapter_address,
        quote=quote,
        amount_raw=50_793_650,
    )
    allocator._is_compatible_put_series.assert_not_called()
    allocator._send.assert_not_called()


def test_allocated_exposure_has_no_static_economic_cap():
    policy = load_testnet_policy(POLICY_PATH)

    validate_allocated_exposure(10_000_000 * 10**6, policy)
    with pytest.raises(RuntimeError, match="outside uint256"):
        validate_allocated_exposure(UINT256_MAX + 1, policy)


def test_safe_block_accepts_only_the_mandatory_pre_activation_handoff():
    nav = [0] * 11
    nav[6] = 102
    nav[7] = 150

    assert safe_block_has_coherent_nav(tuple(nav), 100)
    assert safe_block_has_coherent_nav(tuple(nav), 102)
    assert not safe_block_has_coherent_nav(tuple(nav), 99)
    assert not safe_block_has_coherent_nav(tuple(nav), 151)


def test_accepts_only_approved_fair_nav_valuator_policy():
    validate_fair_nav_policy((1, 2, 1, 0, 500, 2))


@pytest.mark.parametrize(
    "policy_state",
    [
        (0, 2, 1, 0, 500, 2),
        (1, 1, 1, 0, 500, 2),
        (1, 2, 2, 0, 500, 2),
        (1, 2, 1, 1_000, 500, 2),
        (1, 2, 1, 0, 501, 2),
        (1, 2, 1, 0, 500, 1),
    ],
)
def test_rejects_legacy_synthetic_or_drifted_valuator_policy(policy_state):
    with pytest.raises(RuntimeError, match="fair-NAV policy"):
        validate_fair_nav_policy(policy_state)


def test_fund_quote_signature_is_bound_to_adapter_owner():
    private_key = "0x" + "11" * 32
    owner = "0x68e5C9f55201a4fa87040830b1A53A4B6E26b0e3"
    settler = "0xb94D6270B336dca566C2077d50c2C50F06398cB8"
    quote = {
        "otoken_address": "0xdb2f3e6a5e69f6ac0d9f6b1e9d51cb9be9063c0d",
        "bid_price": "135",
        "deadline": 1_785_000_000,
        "quote_id": "42",
        "max_amount": "500000000",
        "maker_nonce": 0,
    }

    signature = sign_fund_quote(
        quote,
        owner=owner,
        chain_id=84532,
        settler=settler,
        private_key=private_key,
    )
    signable = encode_typed_data(
        domain_data={
            "name": "b1nary",
            "version": "1",
            "chainId": 84532,
            "verifyingContract": settler,
        },
        message_types=_FUND_QUOTE_TYPES,
        message_data={
            "owner": owner,
            "oToken": quote["otoken_address"],
            "bidPrice": 135,
            "deadline": 1_785_000_000,
            "quoteId": 42,
            "maxAmount": 500000000,
            "makerNonce": 0,
        },
    )

    assert (
        Account.recover_message(signable, signature=signature)
        == Account.from_key(private_key).address
    )
