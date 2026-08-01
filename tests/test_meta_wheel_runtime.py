from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hexbytes import HexBytes
from web3 import Web3

from src import api_client, config
from src.meta_wheel_allocator import (
    ActionKind,
    LaneKind,
    LanePhase,
    LaneSnapshot,
    WheelAction,
    WheelActionPreState,
)
from src.meta_wheel_runtime import (
    BaseSepoliaMetaWheelRuntime,
    DecodedWheelEvent,
    WheelObservedState,
    _ambiguous_quote_identities,
    _quote_created_at,
    reconcile_wheel_events,
    reconcile_wheel_state,
)


def event(name: str, **args) -> DecodedWheelEvent:
    return DecodedWheelEvent(
        name=name,
        address="0x0000000000000000000000000000000000000001",
        args=args,
    )


def test_queue_reconciliation_normalizes_bytes32_allocation_id():
    action = WheelAction(
        kind=ActionKind.QUEUE_CSP_USDC,
        chain_id=84532,
        parent="0xparent",
        lane="0xcoordinator",
        tranche_id=0,
        transition_nonce=7,
        child_position_id=0,
        amount=5_000 * 10**6,
    )
    events = [
        event(
            "WheelTrancheQueued",
            trancheId=8,
            allocationId=HexBytes("0x" + action.key),
            usdcAmount=action.amount,
        )
    ]

    assert reconcile_wheel_events(action, events, premium_fee_bps=1_000).valid


def test_open_call_reconciliation_requires_exact_child_floor_and_premium():
    lane = "0x00000000000000000000000000000000000000a1"
    position_hash = HexBytes("0x" + "ab" * 32)
    action = WheelAction(
        kind=ActionKind.OPEN_CALL,
        chain_id=84532,
        parent="0xparent",
        lane=lane,
        tranche_id=4,
        transition_nonce=2,
        child_position_id=0,
        amount=10**18,
        lot_ids=(9,),
        strike8=2_015 * 10**8,
        required_floor8=2_015 * 10**8,
        open_data=b"signed",
    )
    events = [
        event(
            "WheelTrancheOpened",
            trancheId=4,
            lane=lane,
            childPositionId=12,
            childShares=10**18,
            childPositionHash=position_hash,
        ),
        event(
            "CoveredCallOpened",
            trancheId=4,
            lotId=9,
            positionId=12,
            wethAmount=10**18,
            requiredFloor8=2_015 * 10**8,
            callStrike8=2_015 * 10**8,
            childShares=10**18,
            positionHash=position_hash,
        ),
        event(
            "WheelCoveredCallFloorEnforced",
            trancheId=4,
            lotId=9,
            lane=lane,
            requiredFloor8=2_015 * 10**8,
            callStrike8=2_015 * 10**8,
        ),
        event(
            "WheelPremiumAccrued",
            trancheId=4,
            lane=lane,
            grossPremiumAssets=1_000,
            protocolFeeAssets=100,
            netPremiumAssets=900,
        ),
    ]

    assert reconcile_wheel_events(action, events, premium_fee_bps=1_000).valid
    mismatched = [
        item
        if item.name != "WheelCoveredCallFloorEnforced"
        else event(
            item.name,
            **(item.args | {"callStrike8": action.strike8 - 1}),
        )
        for item in events
    ]
    assert not reconcile_wheel_events(action, mismatched, premium_fee_bps=1_000).valid


def test_handoff_reconciliation_requires_matching_transition_hash():
    lane = "0x00000000000000000000000000000000000000a1"
    transition_hash = HexBytes("0x" + "cd" * 32)
    action = WheelAction(
        kind=ActionKind.HANDOFF_ASSIGNMENT,
        chain_id=84532,
        parent="0xparent",
        lane=lane,
        tranche_id=4,
        transition_nonce=3,
        child_position_id=12,
    )
    events = [
        event(
            "WheelChildHandoff",
            trancheId=4,
            lane=lane,
            transitionHash=transition_hash,
            childSharesBurned=1_000,
            usdcAmount=900,
            wethAmount=0,
        ),
        event(
            "CspBasketHandedOff",
            trancheId=4,
            transitionHash=transition_hash,
            childSharesBurned=1_000,
            usdcAmount=900,
            wethAmount=0,
        ),
    ]

    assert reconcile_wheel_events(action, events, premium_fee_bps=1_000).valid
    events[1] = event(
        "CspBasketHandedOff",
        **(events[1].args | {"transitionHash": HexBytes("0x" + "ef" * 32)}),
    )
    assert not reconcile_wheel_events(action, events, premium_fee_bps=1_000).valid


def test_duplicate_same_name_event_is_rejected():
    action = WheelAction(
        kind=ActionKind.QUEUE_CSP_USDC,
        chain_id=84532,
        parent="0xparent",
        lane="0xcoordinator",
        tranche_id=0,
        transition_nonce=7,
        child_position_id=0,
        amount=100,
    )
    queued = event(
        "WheelTrancheQueued",
        trancheId=8,
        allocationId="0x" + action.key,
        usdcAmount=100,
    )

    assert not reconcile_wheel_events(
        action, [queued, queued], premium_fee_bps=1_000
    ).valid


def test_quote_timestamp_uses_backend_creation_time_and_rejects_ambiguity():
    assert _quote_created_at({"created_at": "2026-08-01T12:00:00Z"}) == 1_785_585_600
    base = {
        "otoken_address": "0x0000000000000000000000000000000000000001",
        "bid_price": 10,
        "deadline": 100,
        "quote_id": 7,
        "max_amount": 1_000,
        "maker_nonce": 2,
    }
    assert _ambiguous_quote_identities([base, base | {"bid_price": 11}]) == {(2, 7)}
    with pytest.raises(ValueError):
        _quote_created_at({})


def _pre(**changes) -> WheelActionPreState:
    base = WheelActionPreState(
        parent_idle_usdc=1_000,
        coordinator_accounted_usdc=500,
        coordinator_accounted_weth=10,
        coordinator_transition_weth=10,
        coordinator_raw_usdc=500,
        coordinator_raw_weth=10,
        pending_csp_usdc=400,
        reserved_redemption_usdc=100,
        reserved_principal_usdc=80,
        tranche_principal_usdc=300,
        tranche_pending_usdc=400,
        lane_child_shares=100,
        lane_accounted_usdc=50,
        lane_accounted_weth=7,
        lane_raw_usdc=50,
        lane_raw_weth=7,
        lane_execution_state_hash="0xold-execution",
        lane_position_state_hash="0xold-position",
    )
    return replace(base, **changes)


def _post(pre: WheelActionPreState, **changes) -> WheelObservedState:
    values = {
        "parent_idle_usdc": pre.parent_idle_usdc,
        "coordinator_accounted_usdc": pre.coordinator_accounted_usdc,
        "coordinator_accounted_weth": pre.coordinator_accounted_weth,
        "coordinator_transition_weth": pre.coordinator_transition_weth,
        "coordinator_raw_usdc": pre.coordinator_raw_usdc,
        "coordinator_raw_weth": pre.coordinator_raw_weth,
        "pending_csp_usdc": pre.pending_csp_usdc,
        "reserved_redemption_usdc": pre.reserved_redemption_usdc,
        "reserved_principal_usdc": pre.reserved_principal_usdc,
        "tranche_principal_usdc": pre.tranche_principal_usdc,
        "tranche_pending_usdc": pre.tranche_pending_usdc,
        "lane_child_shares": pre.lane_child_shares,
        "lane_accounted_usdc": pre.lane_accounted_usdc,
        "lane_accounted_weth": pre.lane_accounted_weth,
        "lane_raw_usdc": pre.lane_raw_usdc,
        "lane_raw_weth": pre.lane_raw_weth,
        "lane_execution_state_hash": pre.lane_execution_state_hash,
        "lane_position_state_hash": pre.lane_position_state_hash,
    }
    return WheelObservedState(**(values | changes))


def _action(kind: ActionKind, pre: WheelActionPreState, **changes) -> WheelAction:
    values = {
        "kind": kind,
        "chain_id": 84532,
        "parent": "0xparent",
        "lane": "0x00000000000000000000000000000000000000a1",
        "tranche_id": 4,
        "transition_nonce": 3,
        "child_position_id": 12,
        "pre_state": pre,
    }
    return WheelAction(**(values | changes))


def _state_cases():
    queue_pre = _pre(lane_child_shares=0)
    queue = _action(
        ActionKind.QUEUE_CSP_USDC,
        queue_pre,
        tranche_id=0,
        lane="0xcoordinator",
        amount=100,
    )
    yield (
        queue,
        [event("WheelTrancheQueued", trancheId=5)],
        _post(
            queue_pre,
            parent_idle_usdc=900,
            coordinator_accounted_usdc=600,
            coordinator_raw_usdc=600,
            pending_csp_usdc=500,
            sibling_principal_usdc=100,
            sibling_pending_usdc=100,
        ),
    )
    split_pre = _pre()
    yield (
        _action(ActionKind.SPLIT_PENDING_CSP, split_pre, amount=100),
        [event("WheelSiblingTrancheQueued", principalUsdc=75)],
        _post(
            split_pre,
            tranche_principal_usdc=225,
            tranche_pending_usdc=300,
            sibling_principal_usdc=75,
            sibling_pending_usdc=100,
        ),
    )
    reserve_pre = _pre()
    yield (
        _action(ActionKind.RESERVE_REDEMPTION, reserve_pre, amount=100),
        [event("WheelRedemptionUsdcReserved", principalReserved=75)],
        _post(
            reserve_pre,
            pending_csp_usdc=300,
            reserved_redemption_usdc=200,
            reserved_principal_usdc=155,
            tranche_principal_usdc=225,
            tranche_pending_usdc=300,
        ),
    )
    release_pre = _pre()
    yield (
        _action(
            ActionKind.RELEASE_REDEMPTION,
            release_pre,
            tranche_id=0,
            amount=50,
        ),
        [event("WheelRedemptionUsdcReleased", principalRestored=40)],
        _post(
            release_pre,
            pending_csp_usdc=450,
            reserved_redemption_usdc=50,
            reserved_principal_usdc=40,
            sibling_principal_usdc=40,
            sibling_pending_usdc=50,
        ),
    )
    open_csp_pre = _pre(
        lane_child_shares=0,
        lane_accounted_usdc=0,
        lane_accounted_weth=0,
        lane_raw_usdc=0,
        lane_raw_weth=0,
        lane_execution_state_hash="",
        lane_position_state_hash="",
    )
    opened_hash = HexBytes("0x" + "ab" * 32)
    yield (
        _action(ActionKind.OPEN_CSP, open_csp_pre, amount=400),
        [
            event("WheelTrancheOpened"),
            event("CspOpened", positionHash=opened_hash),
        ],
        _post(
            open_csp_pre,
            coordinator_accounted_usdc=100,
            coordinator_raw_usdc=100,
            pending_csp_usdc=0,
            tranche_pending_usdc=0,
            lane_child_shares=400,
            lane_execution_state_hash=Web3.to_hex(opened_hash),
            lane_position_state_hash="0xnew-position",
        ),
    )
    open_call_pre = _pre(
        tranche_pending_usdc=0,
        lane_child_shares=0,
        lane_accounted_usdc=0,
        lane_accounted_weth=0,
        lane_raw_usdc=0,
        lane_raw_weth=0,
        lane_execution_state_hash="",
        lane_position_state_hash="",
    )
    call_hash = HexBytes("0x" + "bc" * 32)
    yield (
        _action(ActionKind.OPEN_CALL, open_call_pre, amount=5),
        [
            event("WheelTrancheOpened"),
            event("CoveredCallOpened", collateral=4, positionHash=call_hash),
        ],
        _post(
            open_call_pre,
            coordinator_accounted_weth=5,
            coordinator_raw_weth=5,
            coordinator_transition_weth=5,
            lane_child_shares=5,
            lane_accounted_weth=1,
            lane_raw_weth=1,
            lane_execution_state_hash=Web3.to_hex(call_hash),
            lane_position_state_hash="0xnew-position",
        ),
    )
    settle_pre = _pre()
    settle_hash = HexBytes("0x" + "cd" * 32)
    yield (
        _action(ActionKind.SETTLE_CSP, settle_pre),
        [
            event(
                "CspSettlementAdvanced",
                observedUsdc=20,
                observedWeth=3,
                positionHash=settle_hash,
            )
        ],
        _post(
            settle_pre,
            lane_accounted_usdc=70,
            lane_raw_usdc=70,
            lane_accounted_weth=10,
            lane_raw_weth=10,
            lane_execution_state_hash=Web3.to_hex(settle_hash),
            lane_position_state_hash="0xsettled-position",
        ),
    )
    handoff_pre = _pre(lane_accounted_weth=0, lane_raw_weth=0)
    yield (
        _action(ActionKind.HANDOFF_ASSIGNMENT, handoff_pre),
        [
            event(
                "WheelChildHandoff",
                childSharesBurned=100,
                usdcAmount=50,
                wethAmount=0,
            )
        ],
        _post(
            handoff_pre,
            coordinator_accounted_usdc=550,
            coordinator_raw_usdc=550,
            pending_csp_usdc=450,
            tranche_pending_usdc=50,
            lane_child_shares=0,
            lane_accounted_usdc=0,
            lane_raw_usdc=0,
            lane_execution_state_hash="0xidle-execution",
            lane_position_state_hash="0xidle-position",
        ),
    )


def test_state_reconciliation_checks_all_runtime_mutation_classes():
    for action, events, post in _state_cases():
        assert reconcile_wheel_state(action, events, post).valid, action.kind
        mutated = replace(post, coordinator_raw_usdc=post.coordinator_raw_usdc + 1)
        assert not reconcile_wheel_state(action, events, mutated).valid, action.kind


def test_assignment_handoff_reconciles_literal_strike_principal_split():
    pre = _pre(
        tranche_principal_usdc=300 * 10**6,
        tranche_pending_usdc=0,
        lane_accounted_usdc=50 * 10**6,
        lane_raw_usdc=50 * 10**6,
        lane_accounted_weth=2 * 10**18,
        lane_raw_weth=2 * 10**18,
    )
    action = _action(ActionKind.HANDOFF_ASSIGNMENT, pre)
    events = [
        event(
            "WheelChildHandoff",
            childSharesBurned=100,
            usdcAmount=50 * 10**6,
            wethAmount=2 * 10**18,
        ),
        event(
            "WheelAssignmentLotCreated",
            literalAssignmentStrike8=100 * 10**8,
        ),
    ]
    post = _post(
        pre,
        coordinator_accounted_usdc=500 + 50 * 10**6,
        coordinator_raw_usdc=500 + 50 * 10**6,
        pending_csp_usdc=400 + 50 * 10**6,
        coordinator_accounted_weth=10 + 2 * 10**18,
        coordinator_raw_weth=10 + 2 * 10**18,
        coordinator_transition_weth=10 + 2 * 10**18,
        tranche_principal_usdc=200 * 10**6,
        tranche_pending_usdc=0,
        sibling_principal_usdc=100 * 10**6,
        sibling_pending_usdc=50 * 10**6,
        lane_child_shares=0,
        lane_accounted_usdc=0,
        lane_raw_usdc=0,
        lane_accounted_weth=0,
        lane_raw_weth=0,
        lane_execution_state_hash="0xidle-execution",
        lane_position_state_hash="0xidle-position",
    )

    assert reconcile_wheel_state(action, events, post).valid
    assert not reconcile_wheel_state(
        action, events, replace(post, tranche_principal_usdc=200 * 10**6 + 1)
    ).valid


def test_nav_observation_binds_historical_lane_hash_and_windows(monkeypatch):
    runtime = object.__new__(BaseSepoliaMetaWheelRuntime)
    parent = Web3.to_checksum_address("0x" + "11" * 20)
    coordinator = Web3.to_checksum_address("0x" + "22" * 20)
    lane_address = Web3.to_checksum_address("0x" + "33" * 20)
    runtime.manifest = SimpleNamespace(parent=parent, coordinator=coordinator)
    block_hash = HexBytes("0x" + "44" * 32)
    coordinator_hash = HexBytes("0x" + "55" * 32)
    lane_hash = HexBytes("0x" + "66" * 32)
    runtime.w3 = MagicMock()
    runtime.w3.eth.get_block.return_value = SimpleNamespace(hash=block_hash)
    runtime.coordinator = MagicMock()
    runtime.coordinator.functions.positionStateHash.return_value.call.return_value = (
        coordinator_hash
    )
    lane_contract = MagicMock()
    lane_contract.functions.positionStateHash.return_value.call.return_value = lane_hash
    runtime._lane_contract = MagicMock(return_value=lane_contract)
    component_id = Web3.solidity_keccak(
        ["string", "address"], ["STRATEGY", coordinator]
    )
    nav = [0] * 15
    nav[5], nav[6], nav[7], nav[9] = 100, 101, 130, 9
    lane = LaneSnapshot(
        address=lane_address,
        kind=LaneKind.CSP,
        phase=LanePhase.CSP_OPEN,
        tranche_id=1,
        transition_nonce=2,
        child_position_id=3,
        amount=500,
        expiry=999,
        execution_state_hash="0xexecution",
        tranche_child_execution_state_hash="0xexecution",
        position_state_hash=Web3.to_hex(lane_hash),
        nav_position_state_hash="",
    )
    observation = {
        "fundKey": "base-sepolia:wheel",
        "chainId": 84532,
        "fundAddress": parent,
        "coordinator": coordinator,
        "reportNonce": 9,
        "componentId": Web3.to_hex(component_id),
        "coordinatorPositionStateHash": Web3.to_hex(coordinator_hash),
        "snapshotBlock": 100,
        "snapshotBlockHash": Web3.to_hex(block_hash),
        "validAfterBlock": 101,
        "validUntilBlock": 125,
        "lanes": [
            {
                "lane": lane_address,
                "childShares": 500,
                "positionStateHash": Web3.to_hex(lane_hash),
                "snapshotBlock": 100,
                "snapshotBlockHash": Web3.to_hex(block_hash),
                "validAfterBlock": 99,
                "validUntilBlock": 110,
            }
        ],
    }
    monkeypatch.setattr(config, "META_WHEEL_FUND_KEY", "base-sepolia:wheel")
    get_observation = MagicMock(return_value=observation)
    monkeypatch.setattr(api_client, "get_meta_wheel_nav_observation", get_observation)

    (bound,) = runtime._bind_nav_observation(
        lanes=(lane,), nav=tuple(nav), component_id=component_id, safe_block=105
    )

    assert bound.nav_position_state_hash == Web3.to_hex(lane_hash)
    get_observation.assert_called_once_with("base-sepolia:wheel", snapshot_block=100)
    bad = observation | {"lanes": [observation["lanes"][0] | {"childShares": 499}]}
    get_observation.return_value = bad
    with pytest.raises(RuntimeError, match="lane evidence"):
        runtime._bind_nav_observation(
            lanes=(lane,), nav=tuple(nav), component_id=component_id, safe_block=105
        )
