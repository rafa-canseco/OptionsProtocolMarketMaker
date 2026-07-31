import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import api_client, config
from src.covered_call_allocator import (
    CoveredCallFundAllocator,
    CoveredCallPolicy,
    call_collateral_for_option_amount,
    call_collateral_target,
    count_called_away,
    fair_call_liability_weth,
    load_covered_call_policy,
    minimum_bid_price_for_net_premium,
    normalization_minimum_weth_out,
    option_amount_for_call_collateral,
    select_covered_call_quote,
)
from src.fund_tx import ConfirmedTransaction
from src.fund_allocator import UINT256_MAX


POLICY_PATH = (
    Path(__file__).parents[1]
    / "policies"
    / "covered_call_fund_policy.v5.base-sepolia.json"
)
LEGACY_POLICY_PATH = (
    Path(__file__).parents[1]
    / "policies"
    / "covered_call_fund_policy.v3.base-sepolia.json"
)
V4_POLICY_PATH = (
    Path(__file__).parents[1]
    / "policies"
    / "covered_call_fund_policy.v4.base-sepolia.json"
)
SPOT_FEED = "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1"
VALUATION_POLICY = (1, 2, 1, 0, 500, 2, 120, SPOT_FEED, 8, 3600)


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


def test_policy_uses_dynamic_idle_sizing_and_is_weth_only():
    policy = _policy()
    assert float(policy.target_delta) == pytest.approx(0.05)
    assert policy.strike_tick_usd == 5
    assert policy.target_utilization_bps == 8000
    assert policy.maximum_vault_aum == UINT256_MAX
    assert policy.maximum_collateral == UINT256_MAX
    assert policy.minimum_net_premium_bps == 10
    assert policy.maximum_open_positions == 1
    assert policy.max_expiry_delay == 61 * 3600
    assert policy.valuation_policy_version == 2
    assert policy.model_version == 1
    assert policy.liability_buffer_bps == 0
    assert policy.max_observation_divergence_bps == 500
    assert policy.maximum_observation_window_blocks == 120
    assert policy.spot_feed == SPOT_FEED
    assert policy.maximum_spot_staleness_seconds == 3600


def test_legacy_policy_remains_loadable_during_staged_rollout():
    assert load_covered_call_policy(LEGACY_POLICY_PATH).target_utilization_bps == 2500
    assert load_covered_call_policy(V4_POLICY_PATH).strike_tick_usd == 25


def test_policy_loader_rejects_parameter_drift(tmp_path):
    raw = json.loads(POLICY_PATH.read_text())
    raw["selection"]["target_utilization_bps"] = 2500
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="not approved"):
        load_covered_call_policy(changed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model_name",), "b1nary-european-bs-put-v1"),
        (("model_version",), 2),
        (("liability_buffer_bps",), 1000),
        (("max_observation_divergence_bps",), 1000),
        (("maximum_observation_window_blocks",), 25),
        (("implied_volatility", "bps"), 0),
        (("implied_volatility", "source"), ""),
        (("spot", "maximum_staleness_seconds"), 7200),
    ],
)
def test_policy_loader_rejects_fair_value_policy_drift(tmp_path, path, value):
    raw = json.loads(POLICY_PATH.read_text())
    target = raw["valuation"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="not approved"):
        load_covered_call_policy(changed)


def test_collateral_is_one_to_one_with_otoken_amount():
    policy = _policy()
    target = call_collateral_target(5 * 10**18, policy)
    assert target == 4_000_000_000_000_000_000
    amount = option_amount_for_call_collateral(target)
    assert amount == 400_000_000
    assert call_collateral_for_option_amount(amount) == target


@pytest.mark.parametrize(
    ("idle_weth", "expected_target"),
    [
        (1 * 10**18, 8 * 10**17),
        (5 * 10**18, 4 * 10**18),
        (10 * 10**18, 8 * 10**18),
    ],
)
def test_collateral_target_recalculates_at_80_percent(idle_weth, expected_target):
    assert call_collateral_target(idle_weth, _policy()) == expected_target


def test_premium_floor_matches_adapter_and_protocol_fee_rounding():
    amount = 480_112_863
    collateral = call_collateral_for_option_amount(amount)
    spot = 1862_74330099
    bid = minimum_bid_price_for_net_premium(
        option_amount=amount,
        collateral_weth=collateral,
        spot_price_8=spot,
        minimum_net_premium_bps=10,
        protocol_fee_bps=1000,
    )

    collateral_value = collateral * spot // 10**20
    gross = amount * bid // 10**8
    net = gross - gross * 1000 // 10_000
    prior_gross = amount * (bid - 1) // 10**8
    prior_net = prior_gross - prior_gross * 1000 // 10_000

    assert net * 10_000 >= collateral_value * 10
    assert prior_net * 10_000 < collateral_value * 10


def test_premium_floor_never_reduces_a_better_market_bid():
    floor = minimum_bid_price_for_net_premium(
        option_amount=100_000_000,
        collateral_weth=10**18,
        spot_price_8=2000 * 10**8,
        minimum_net_premium_bps=10,
        protocol_fee_bps=1000,
    )
    quoted_bid = floor + 1
    assert max(quoted_bid, floor) == quoted_bid


@pytest.mark.parametrize(
    "overrides",
    [
        {"option_amount": 0},
        {"collateral_weth": 0},
        {"spot_price_8": 0},
        {"minimum_net_premium_bps": 10_001},
        {"protocol_fee_bps": 10_000},
    ],
)
def test_premium_floor_rejects_unsafe_domains(overrides):
    inputs = {
        "option_amount": 100_000_000,
        "collateral_weth": 10**18,
        "spot_price_8": 2000 * 10**8,
        "minimum_net_premium_bps": 10,
        "protocol_fee_bps": 1000,
    }
    with pytest.raises(ValueError):
        minimum_bid_price_for_net_premium(**(inputs | overrides))


def test_fair_call_liability_weth_golden_conversion():
    assert (
        fair_call_liability_weth(
            call_price_usd8=10 * 10**8,
            option_amount_8=250_000,
            spot_price_8=2000 * 10**8,
            collateral_weth=2_500_000_000_000_000,
        )
        == 12_500_000_000_000
    )


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


def test_quote_selection_skips_duplicate_series_with_wrong_assets():
    now = int(time.time())
    wrong_assets = _quote(now)
    compatible = _quote(now, quote_id=2)

    selected = select_covered_call_quote(
        [wrong_assets, compatible],
        spot=2000,
        iv=0.6,
        now=now,
        risk_free_rate=0.05,
        policy=_policy(),
        series_validator=lambda quote: quote is compatible,
    )

    assert selected is compatible


@pytest.mark.parametrize("deployment_status", ["virtual", "creating", "failed"])
def test_lazy_call_series_never_reaches_onchain_validator(deployment_status):
    now = int(time.time())
    validator = MagicMock(
        side_effect=AssertionError("non-ready series must not be read on-chain")
    )
    quote = _quote(now, deployment_status=deployment_status)

    selected = select_covered_call_quote(
        [quote],
        spot=2000,
        iv=0.6,
        now=now,
        risk_free_rate=0.05,
        policy=_policy(),
        series_validator=validator,
    )

    assert selected is None
    validator.assert_not_called()


def test_ready_call_series_reaches_onchain_validator():
    now = int(time.time())
    validator = MagicMock(return_value=True)
    quote = _quote(now, deployment_status="ready")

    selected = select_covered_call_quote(
        [quote],
        spot=2000,
        iv=0.6,
        now=now,
        risk_free_rate=0.05,
        policy=_policy(),
        series_validator=validator,
    )

    assert selected is quote
    validator.assert_called_once_with(quote)


def test_virtual_call_can_be_selected_only_for_materialization():
    now = int(time.time())
    validator = MagicMock(
        side_effect=AssertionError("virtual series must not be read on-chain")
    )
    quote = _quote(now, deployment_status="virtual")

    selected = select_covered_call_quote(
        [quote],
        spot=2000,
        iv=0.6,
        now=now,
        risk_free_rate=0.05,
        policy=_policy(),
        series_validator=validator,
        deployment_statuses=frozenset({"virtual", "creating"}),
    )

    assert selected is quote
    validator.assert_not_called()


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


def test_virtual_call_quote_materializes_without_allocating(monkeypatch):
    now = 1_000_000
    allocator = CoveredCallFundAllocator.__new__(CoveredCallFundAllocator)
    allocator.policy = _policy()
    allocator.flow = MagicMock()
    allocator.flow.functions.totalPendingShares.return_value.call.return_value = 0
    allocator.adapter_address = "0x" + "34" * 20
    allocator._is_compatible_call_series = MagicMock(
        side_effect=AssertionError("virtual series must not be read on-chain")
    )
    allocator._send = MagicMock(
        side_effect=AssertionError("capital must stay idle until the series is ready")
    )
    quote = _quote(
        now,
        deployment_status="virtual",
        signature="0x" + "ab" * 65,
        max_amount=100_000_000,
    )
    monkeypatch.setattr("src.covered_call_allocator.time.time", lambda: now)
    monkeypatch.setattr(
        api_client,
        "get_market_data",
        lambda **_: {"spot": 2000, "iv": 0.6},
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
            "adapter_state": (0, b"", 0, 0, 0, 0, 0),
            "positions": [],
            "allocated": 0,
            "pending_shares": 0,
            "idle_assets": 1 * 10**18,
        }
    )

    ensure.assert_called_once_with(
        adapter_address=allocator.adapter_address,
        quote=quote,
        amount_raw=80_000_000,
    )
    allocator._is_compatible_call_series.assert_not_called()
    allocator._send.assert_not_called()


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
        "valuation_policy": VALUATION_POLICY,
        "valuation_observers": (True, True),
        "strategy_config": (
            True,
            8000,
            10000,
            0,
            1,
            allocator.valuator_address,
            UINT256_MAX,
        ),
        "adapter_config": (
            (
                129600,
                219600,
                3600,
                10,
                500,
                1,
                8000,
                1000 * 10**8,
                10000 * 10**8,
                UINT256_MAX,
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
        "valuation_policy": VALUATION_POLICY,
        "valuation_observers": (True, True),
        "strategy_config": (
            True,
            8000,
            10000,
            0,
            1,
            allocator.valuator_address,
            UINT256_MAX,
        ),
        "adapter_config": (
            (
                129600,
                219600,
                3600,
                10,
                500,
                1,
                8000,
                1000 * 10**8,
                10000 * 10**8,
                UINT256_MAX,
                32 * 10**6,
            ),
            "0x" + "56" * 20,
            3000,
        ),
        "minimum_idle_bps": 0,
    }
    allocator._validate_policy_gates(state, require_active_nav=False)


def test_onchain_valuator_policy_drift_fails_closed():
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
        "block": 10,
        "strategy_hash": b"a" * 32,
        "processing": False,
        "valuation_policy": (*VALUATION_POLICY[:4], 501, *VALUATION_POLICY[5:]),
        "valuation_observers": (True, True),
        "strategy_config": (
            True,
            8000,
            10000,
            0,
            1,
            allocator.valuator_address,
            UINT256_MAX,
        ),
        "adapter_config": (
            (
                129600,
                219600,
                3600,
                10,
                500,
                1,
                8000,
                1000 * 10**8,
                10000 * 10**8,
                UINT256_MAX,
                32 * 10**6,
            ),
            "0x" + "56" * 20,
            3000,
        ),
        "minimum_idle_bps": 0,
    }
    with pytest.raises(RuntimeError, match="valuator differs from policy"):
        allocator._validate_policy_gates(state)


def test_unapproved_fair_value_observer_fails_closed():
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
        "block": 10,
        "strategy_hash": b"a" * 32,
        "processing": False,
        "valuation_policy": VALUATION_POLICY,
        "valuation_observers": (True, False),
        "strategy_config": (
            True,
            8000,
            10000,
            0,
            1,
            allocator.valuator_address,
            UINT256_MAX,
        ),
        "adapter_config": (
            (
                129600,
                219600,
                3600,
                10,
                500,
                1,
                8000,
                1000 * 10**8,
                10000 * 10**8,
                UINT256_MAX,
                32 * 10**6,
            ),
            "0x" + "56" * 20,
            3000,
        ),
        "minimum_idle_bps": 0,
    }
    with pytest.raises(RuntimeError, match="observer set differs from policy"):
        allocator._validate_policy_gates(state)


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
