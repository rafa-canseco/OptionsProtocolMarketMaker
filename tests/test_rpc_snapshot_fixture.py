from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from src import config, main
from src.capacity import calculate_capacity_internal
from src.covered_call_allocator import (
    CoveredCallFundAllocator,
    load_covered_call_policy,
)
from src.covered_call_operations_keeper import CoveredCallFundOperationsKeeper
from src.fund_allocator import CspFundAllocator, load_testnet_policy
from src.fund_operations_keeper import CspFundOperationsKeeper
from src.meta_wheel_allocator import MetaWheelAllocator
from src.meta_wheel_runtime import BaseSepoliaMetaWheelRuntime
from src.snapshot_consumer import SnapshotConsumer

FIXTURE = Path(__file__).parent / "fixtures" / "rpc_snapshot_envelope.json"


class NoRecurrentProviderReads:
    def __init__(self) -> None:
        self.reads = 0

    def __getattr__(self, name):
        self.reads += 1
        raise AssertionError(f"recurrent provider access: {name}")


def _meta_wheel_fund(raw):
    return next(fund for fund in raw["funds"] if fund["fund_type"] == "meta_wheel")


def _consumed_runtime(raw, expected_raw=None):
    expected_wheel = _meta_wheel_fund(expected_raw or raw)["state"]["allocator"][
        "wheel_snapshot"
    ]
    consumer = SnapshotConsumer(
        lambda: raw,
        environment=raw["environment"],
        chain_id=raw["chain_id"],
    )
    consumer.ingest(raw)
    runtime = BaseSepoliaMetaWheelRuntime.__new__(BaseSepoliaMetaWheelRuntime)
    runtime.snapshots = consumer
    runtime.manifest = SimpleNamespace(parent=_meta_wheel_fund(raw)["fund_address"])
    runtime.expected_csp_lanes = frozenset(
        Web3.to_checksum_address(lane["address"])
        for lane in expected_wheel["csp_lanes"]
    )
    runtime.expected_call_lanes = frozenset(
        Web3.to_checksum_address(lane["address"])
        for lane in expected_wheel["call_lanes"]
    )
    runtime._consumed_quotes = {}
    return runtime


class EmptyTracker:
    def open_positions(self, **_):
        return []

    def deployed_usd(self, **_):
        return 0.0

    def inventory_imbalance(self, **_):
        return 0.0


def test_canonical_fixture_drives_every_recurrent_seam_without_provider_reads(
    monkeypatch,
):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", True)
    raw = json.loads(FIXTURE.read_text())
    assert [fund["fund_type"] for fund in raw["funds"]] == [
        "csp",
        "covered_call",
        "meta_wheel",
    ]
    assert len(raw["funds"]) <= 3
    assert "market_maker" not in {fund["fund_type"] for fund in raw["funds"]}

    consumer = SnapshotConsumer(
        lambda: raw,
        environment=raw["environment"],
        chain_id=raw["chain_id"],
    )
    consumer.ingest(raw)
    bundle = consumer.current()
    market = bundle.market("eth")
    quotes = bundle.quotes()
    assert len(quotes) == 2
    common = bundle.market_maker(
        expected_address=raw["common"]["market_maker"]["mm_address"],
        expected_usdc_address=raw["common"]["market_maker"]["usdc_address"],
        expected_allowance_spender=raw["common"]["market_maker"]["allowance_spender"],
    )

    csp_fund, call_fund, wheel_fund = raw["funds"]
    monkeypatch.setattr(config, "USDC_ADDRESS", common["usdc_address"])
    monkeypatch.setattr(config, "MARGIN_POOL_ADDRESS", common["allowance_spender"])
    monkeypatch.setattr(config, "FUND_VAULT_ADDRESS", csp_fund["fund_address"])
    monkeypatch.setattr(config, "COVERED_CALL_VAULT_ADDRESS", call_fund["fund_address"])

    provider = NoRecurrentProviderReads()
    now = raw["snapshot_block_timestamp"]
    monkeypatch.setattr("src.fund_allocator.time.time", lambda: now)
    monkeypatch.setattr("src.covered_call_allocator.time.time", lambda: now)
    ensure_series = MagicMock(
        return_value={
            "status": "creating",
            "otoken_address": raw["common"]["quotes"][0]["otoken_address"],
            "deployment_tx_hash": None,
        }
    )
    monkeypatch.setattr(main.api_client, "ensure_fund_series", ensure_series)
    monkeypatch.setattr(
        main.api_client,
        "get_market_data",
        MagicMock(side_effect=AssertionError("recurrent market API read")),
    )
    monkeypatch.setattr(
        main.api_client,
        "get_quotes",
        MagicMock(side_effect=AssertionError("recurrent quote API read")),
    )

    csp = CspFundAllocator.__new__(CspFundAllocator)
    csp.snapshots = consumer
    csp.w3 = provider
    csp.policy = load_testnet_policy(config.FUND_ALLOCATOR_POLICY_PATH)
    csp.valuator_address = csp_fund["state"]["allocator"]["strategy_config"][5]
    csp.adapter_address = "0x1212121212121212121212121212121212121212"
    csp.usdc = common["usdc_address"]
    csp._send = MagicMock(side_effect=AssertionError("unexpected CSP transaction"))
    csp.run_once()

    covered = CoveredCallFundAllocator.__new__(CoveredCallFundAllocator)
    covered.snapshots = consumer
    covered.w3 = provider
    covered.policy = load_covered_call_policy(config.COVERED_CALL_ALLOCATOR_POLICY_PATH)
    covered.valuator_address = call_fund["state"]["allocator"]["strategy_config"][5]
    covered.adapter_address = "0x3434343434343434343434343434343434343434"
    covered.weth = "0x5555555555555555555555555555555555555555"
    covered.usdc = common["usdc_address"]
    covered._send = MagicMock(
        side_effect=AssertionError("unexpected covered-call transaction")
    )
    covered.run_once()
    assert ensure_series.call_count == 2

    csp_operations = CspFundOperationsKeeper.__new__(CspFundOperationsKeeper)
    csp_operations.snapshots = consumer
    csp_operations.w3 = provider
    csp_operations.flow = MagicMock()
    csp_operations._send = MagicMock(
        return_value=SimpleNamespace(tx_hash="0xcsp-operation")
    )
    csp_operations.run_once()
    csp_operations.flow.functions.processRedeemBatch.assert_called_once()

    call_operations = CoveredCallFundOperationsKeeper.__new__(
        CoveredCallFundOperationsKeeper
    )
    call_operations.snapshots = consumer
    call_operations.w3 = provider
    call_operations.flow = MagicMock()
    call_operations.policy_hash = "fixture-policy"
    call_operations._send = MagicMock(
        return_value=SimpleNamespace(
            tx_hash="0xcall-operation", nonce=1, replaced=False
        )
    )
    call_operations.run_once()
    call_operations.flow.functions.processRedeemBatch.assert_called_once()

    policy_path = Path(config.META_WHEEL_ALLOCATOR_POLICY_PATH)
    policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    runtime = BaseSepoliaMetaWheelRuntime.__new__(BaseSepoliaMetaWheelRuntime)
    runtime.snapshots = consumer
    runtime.manifest = SimpleNamespace(parent=wheel_fund["fund_address"])
    wheel_snapshot = wheel_fund["state"]["allocator"]["wheel_snapshot"]
    runtime.expected_csp_lanes = frozenset(
        Web3.to_checksum_address(lane["address"])
        for lane in wheel_snapshot["csp_lanes"]
    )
    runtime.expected_call_lanes = frozenset(
        Web3.to_checksum_address(lane["address"])
        for lane in wheel_snapshot["call_lanes"]
    )
    runtime._consumed_quotes = {}

    class SnapshotChain:
        def read_snapshot(self, policy):
            return runtime.read_consumed_snapshot(policy)

        def list_quotes(self, snapshot):
            return runtime.list_consumed_quotes(snapshot)

    wheel = MetaWheelAllocator.__new__(MetaWheelAllocator)
    wheel.policy_path = policy_path
    wheel.approved_policy_hash = policy_hash
    wheel.chain = SnapshotChain()
    wheel._execute = MagicMock()
    actions = wheel.run_once()
    assert actions
    assert wheel._execute.call_count == len(actions)
    snapshot = runtime.read_consumed_snapshot(None)
    runtime._consumed_quotes.pop(id(snapshot), None)
    runtime._observed_state_from_snapshot(actions[0], [], snapshot)

    monkeypatch.setattr(config, "HEDGE_MODE", "simulate")
    capacity = calculate_capacity_internal(
        None,
        2000.0,
        common["mm_address"],
        EmptyTracker(),
        asset_config=config.ASSETS[0],
        base_snapshot=common,
    )
    assert capacity.premium_pool_usd == 75_000.0

    captured_nonce = []
    captured_market = []
    monkeypatch.setattr(
        main,
        "_calculate_and_report_capacity",
        MagicMock(
            return_value=SimpleNamespace(
                status="active",
                capacity_eth=1,
                capacity_usd=1,
                open_positions_notional_usd=0,
            )
        ),
    )

    def build_quotes(observed_market, nonce, **_):
        captured_market.append(observed_market)
        captured_nonce.append(nonce)
        return [{"quote": 1}]

    quote_and_submit = main._quote_and_submit
    tracker = MagicMock()
    tracker.open_positions.return_value = []
    monkeypatch.setattr(main, "_tracker", tracker)
    routed_asset_cycle = MagicMock()
    monkeypatch.setattr(main, "_quote_and_submit", routed_asset_cycle)
    monkeypatch.setattr(main, "_log_capacity_snapshot", MagicMock())
    main._run_asset_cycle(
        provider,
        {},
        common["mm_address"],
        config.ASSETS[0],
        "base",
        None,
        common,
        bundle,
        consumer,
    )
    assert routed_asset_cycle.call_args.args[3] == market

    monkeypatch.setattr(main, "_quote_and_submit", quote_and_submit)
    monkeypatch.setattr(main, "build_quotes", build_quotes)
    monkeypatch.setattr(main, "_sign_quotes_base", lambda *_: [{"signed": 1}])
    submit = MagicMock(return_value={"accepted": 1, "rejected": 0, "errors": []})
    monkeypatch.setattr(main.api_client, "submit_quotes", submit)
    main._quote_and_submit(
        provider,
        {},
        common["mm_address"],
        market,
        config.ASSETS[0],
        "base",
        common,
        bundle,
        consumer,
    )

    assert captured_market == [market]
    assert captured_nonce == [common["maker_nonce"]]
    submit.assert_called_once()
    assert provider.reads == 0


def test_consumed_meta_wheel_snapshot_uses_explicit_receipt_bundle_without_reread():
    raw = json.loads(FIXTURE.read_text())
    runtime = _consumed_runtime(raw)
    bundle = runtime.snapshots.current()
    runtime.snapshots.current = MagicMock(
        side_effect=AssertionError("post-receipt generation was reread")
    )

    snapshot = runtime.read_consumed_snapshot(None, bundle=bundle)

    assert snapshot.safe_block == bundle.snapshot_block
    runtime.snapshots.current.assert_not_called()


def test_consumed_meta_wheel_snapshot_accepts_approved_lane_addresses():
    raw = json.loads(FIXTURE.read_text())
    runtime = _consumed_runtime(raw)

    snapshot = runtime.read_consumed_snapshot(None)

    assert {lane.address for lane in snapshot.csp_lanes} == {
        lane["address"]
        for lane in _meta_wheel_fund(raw)["state"]["allocator"]["wheel_snapshot"][
            "csp_lanes"
        ]
    }
    assert {lane.address for lane in snapshot.call_lanes} == {
        lane["address"]
        for lane in _meta_wheel_fund(raw)["state"]["allocator"]["wheel_snapshot"][
            "call_lanes"
        ]
    }


@pytest.mark.parametrize(
    "case",
    (
        "substituted_csp",
        "substituted_call",
        "extra_csp",
        "missing_call",
        "duplicate_call",
    ),
)
def test_consumed_meta_wheel_snapshot_rejects_unapproved_lane_sets(case):
    expected = json.loads(FIXTURE.read_text())
    raw = copy.deepcopy(expected)
    wheel = _meta_wheel_fund(raw)["state"]["allocator"]["wheel_snapshot"]
    if case == "substituted_csp":
        wheel["csp_lanes"][0]["address"] = "0x1111111111111111111111111111111111111111"
    elif case == "substituted_call":
        wheel["call_lanes"][0]["address"] = "0x2222222222222222222222222222222222222222"
    elif case == "extra_csp":
        extra = copy.deepcopy(wheel["csp_lanes"][0])
        extra["address"] = "0x3333333333333333333333333333333333333333"
        wheel["csp_lanes"].append(extra)
    elif case == "missing_call":
        wheel["call_lanes"].clear()
    else:
        wheel["call_lanes"].append(copy.deepcopy(wheel["call_lanes"][0]))

    runtime = _consumed_runtime(raw, expected)
    with pytest.raises(
        RuntimeError, match="registered lanes differ from configured lanes"
    ):
        runtime.read_consumed_snapshot(None)
