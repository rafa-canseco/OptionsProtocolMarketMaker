import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eth_account import Account
from eth_account.messages import encode_typed_data

from src.fund_allocator import (
    CspFundAllocator,
    _FUND_QUOTE_TYPES,
    evaluate_csp_quote,
    incremental_quote_premium,
    liquid_collateral_target,
    load_testnet_policy,
    option_amount_for_collateral,
    premium_after_protocol_fee,
    premium_meets_floor,
    required_collateral,
    safe_block_has_coherent_nav,
    select_policy_quote,
    sign_fund_quote,
    UINT256_MAX,
    validate_allocated_exposure,
    validate_fair_nav_policy,
    validate_market_snapshot,
)
from src import api_client


POLICY_PATH = (
    Path(__file__).parents[1] / "policies" / "csp_fund_policy.v4.base-sepolia.json"
)


def test_approved_policy_is_testnet_only_and_uses_delta_selection():
    policy = load_testnet_policy(POLICY_PATH)

    assert policy.policy_id == "eth_usdc_csp_delta_009_base_sepolia_v4"
    assert policy.target_put_delta_bps == 900
    assert policy.maximum_delta_deviation_bps == 150
    assert policy.strike_tick_usd == 25
    assert policy.target_duration_seconds == 48 * 3600
    assert policy.minimum_net_premium_bps == 20
    assert policy.maximum_vault_aum == UINT256_MAX
    assert policy.maximum_collateral == UINT256_MAX
    assert policy.maximum_open_positions == 1
    assert policy.liquid_usdc_reserve_bps == 2_000
    assert policy.onchain_minimum_idle_bps == 0


def test_checked_in_csp_checksum_matches_active_policy():
    checksum, filename = (
        Path("policies/csp_fund_policy.v4.base-sepolia.sha256").read_text().split()
    )

    assert filename == POLICY_PATH.name
    assert checksum == hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def test_legacy_fixed_moneyness_policy_cannot_load_as_active():
    with pytest.raises(ValueError, match="approved B1N-438"):
        load_testnet_policy("policies/csp_fund_policy.v3.base-sepolia.json")


def test_policy_rejects_parameter_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["selection"]["target_utilization_bps"] = 8001
    drifted = tmp_path / "policy.json"
    drifted.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="approved B1N-438"):
        load_testnet_policy(drifted)


def policy_quote(now=1_000_000, **changes):
    quote = {
        "asset": "eth",
        "chain": "base",
        "is_put": True,
        "created_at": now - 5,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1750.0,
        "bid_price": 4_000_000,
        "max_amount": 100_000_000,
        "quote_id": 1,
    }
    return quote | changes


def select(quotes, policy, now=1_000_000, **changes):
    return select_policy_quote(
        quotes,
        spot=1859.32,
        iv=0.6,
        now=now,
        policy=policy,
        protocol_fee_bps=1_000,
        collateral_target=800 * 10**6,
        **changes,
    )


def test_selects_48h_tick_put_nearest_target_delta_without_moneyness_fallback():
    policy = load_testnet_policy(POLICY_PATH)
    now = 1_000_000
    expected = policy_quote(now)
    quotes = [
        policy_quote(now, strike_price=1725.0, quote_id=2),
        policy_quote(now, strike_price=1760.0, quote_id=3),
        policy_quote(now, is_put=False, quote_id=4),
        policy_quote(now, expiry=now + 61 * 3600, quote_id=5),
        expected,
    ]

    assert select(quotes, policy, now) is expected


def test_delta_tie_prefers_lower_absolute_put_delta_then_strike(monkeypatch):
    policy = load_testnet_policy(POLICY_PATH)
    low = policy_quote(strike_price=1725.0, quote_id=2)
    high = policy_quote(strike_price=1775.0, quote_id=3)

    monkeypatch.setattr(
        "src.fund_allocator.bs_delta",
        lambda _put, _spot, strike, *_: -0.08 if strike == 1725.0 else -0.10,
    )

    assert select([high, low], policy) is low


def test_delta_tolerance_rejects_just_over_150_bps(monkeypatch):
    policy = load_testnet_policy(POLICY_PATH)
    candidate = policy_quote()
    monkeypatch.setattr("src.fund_allocator.bs_delta", lambda *_: -0.105_001)

    assert select([candidate], policy) is None


def test_selects_compatible_put_when_duplicate_economics_use_wrong_assets():
    policy = load_testnet_policy(POLICY_PATH)
    wrong_assets = policy_quote(deadline=1_000_300, quote_id=1)
    compatible = policy_quote(deadline=1_000_299, quote_id=2)

    selected = select(
        [wrong_assets, compatible],
        policy,
        series_validator=lambda quote: quote is compatible,
    )

    assert selected is compatible


@pytest.mark.parametrize("deployment_status", ["virtual", "creating", "failed"])
def test_lazy_put_series_never_reaches_onchain_validator(deployment_status):
    policy = load_testnet_policy(POLICY_PATH)
    validator = MagicMock(
        side_effect=AssertionError("non-ready series must not be read on-chain")
    )
    quote = policy_quote(deployment_status=deployment_status)

    selected = select([quote], policy, series_validator=validator)

    assert selected is None
    validator.assert_not_called()


def test_ready_put_series_reaches_onchain_validator():
    policy = load_testnet_policy(POLICY_PATH)
    validator = MagicMock(return_value=True)
    quote = policy_quote(deployment_status="ready")

    selected = select([quote], policy, series_validator=validator)

    assert selected is quote
    validator.assert_called_once_with(quote)


def test_virtual_put_can_be_selected_only_for_materialization():
    policy = load_testnet_policy(POLICY_PATH)
    validator = MagicMock(
        side_effect=AssertionError("virtual series must not be read on-chain")
    )
    quote = policy_quote(deployment_status="virtual")

    selected = select(
        [quote],
        policy,
        series_validator=validator,
        deployment_statuses=frozenset({"virtual", "creating"}),
    )

    assert selected is quote
    validator.assert_not_called()


@pytest.mark.parametrize(
    "market",
    (
        {"spot": 1859.32, "observed_at": 1_000_000},
        {"spot": 1859.32, "iv": 0.6},
        {"spot": 1859.32, "iv": float("nan"), "observed_at": 1_000_000},
        {"spot": 1859.32, "iv": 0.6, "observed_at": 999_939},
    ),
)
def test_missing_invalid_or_stale_iv_snapshot_fails_closed(market):
    with pytest.raises(RuntimeError, match="snapshot"):
        validate_market_snapshot(market, now=1_000_000, maximum_age=60)


def test_stale_or_missing_delta_inputs_have_no_fixed_moneyness_fallback():
    policy = load_testnet_policy(POLICY_PATH)
    stale = policy_quote(created_at=1_000_000 - policy.quote_maximum_age - 1)
    outside_delta = policy_quote(strike_price=1700.0, quote_id=2)

    assert select([stale, outside_delta], policy) is None


def test_exact_net_premium_floor_matches_contract_fee_rounding():
    collateral = 500_000_000
    exact_net = collateral * 20 // 10_000
    # At 10%, gross=1,111,111 has a floored fee of 111,111 and exact net 1,000,000.
    exact_gross = 1_111_111

    assert premium_after_protocol_fee(exact_gross, 1_000) == exact_net
    assert premium_meets_floor(
        gross_premium=exact_gross,
        collateral=collateral,
        protocol_fee_bps=1_000,
        minimum_net_premium_bps=20,
    )
    assert not premium_meets_floor(
        gross_premium=exact_gross - 1,
        collateral=collateral,
        protocol_fee_bps=1_000,
        minimum_net_premium_bps=20,
    )


def test_incremental_premium_matches_settler_cumulative_rounding():
    gross, net = incremental_quote_premium(
        filled_amount=42_857_143,
        option_amount=45_714_285,
        bid_price=4_131_531,
        protocol_fee_bps=1_000,
    )
    previous = 42_857_143 * 4_131_531 // 10**8
    cumulative = (42_857_143 + 45_714_285) * 4_131_531 // 10**8

    assert gross == cumulative - previous
    assert net == gross - (cumulative * 1_000 // 10_000 - previous * 1_000 // 10_000)


def test_prior_fills_bound_capacity_and_fully_filled_quote_is_rejected():
    policy = load_testnet_policy(POLICY_PATH)
    partial = policy_quote(_remaining_amount=10_000_000)
    evaluation = evaluate_csp_quote(
        partial,
        spot=1859.32,
        iv=0.6,
        now=1_000_000,
        policy=policy,
        protocol_fee_bps=1_000,
        collateral_target=800_000_000,
    )

    assert evaluation.accepted
    assert evaluation.option_amount == 10_000_000
    assert evaluation.collateral == 175_000_000
    assert select([policy_quote(_remaining_amount=0)], policy) is None


def test_quote_one_unit_below_floor_is_rejected():
    policy = load_testnet_policy(POLICY_PATH)
    quote = policy_quote(bid_price=3_888_888)
    evaluation = evaluate_csp_quote(
        quote,
        spot=1859.32,
        iv=0.6,
        now=1_000_000,
        policy=policy,
        protocol_fee_bps=1_000,
        collateral_target=500_000_000,
    )

    assert evaluation.rejection_reason == "net_premium_below_floor"


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
    allocator.settler = MagicMock()
    allocator.settler.functions.protocolFeeBps.return_value.call.return_value = 1_000
    allocator.settler.functions.treasury.return_value.call.return_value = (
        "0x" + "56" * 20
    )
    allocator._quote_fill_state = MagicMock(return_value=(0, 100_000_000))
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
        "created_at": now - 5,
        "deadline": now + 300,
        "expiry": now + 48 * 3600,
        "strike_price": 1750.0,
        "deployment_status": "virtual",
        "otoken_address": "0x" + "12" * 20,
        "bid_price": 4_000_000,
        "quote_id": 7,
        "max_amount": 100_000_000,
        "maker_nonce": 3,
        "signature": "0x" + "ab" * 65,
    }
    monkeypatch.setattr("src.fund_allocator.time.time", lambda: now)
    monkeypatch.setattr(
        api_client,
        "get_market_data",
        lambda **_: {
            "spot": 1859.32,
            "iv": 0.6,
            "observed_at": now,
            "protocol_fee_bps": 1_000,
        },
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
            "block": 100,
        }
    )

    ensure.assert_called_once_with(
        adapter_address=allocator.adapter_address,
        quote=quote,
        amount_raw=45_714_285,
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
