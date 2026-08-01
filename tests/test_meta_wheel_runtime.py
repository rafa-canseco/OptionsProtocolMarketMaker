from hexbytes import HexBytes

from src.meta_wheel_allocator import ActionKind, WheelAction
from src.meta_wheel_runtime import DecodedWheelEvent, reconcile_wheel_events


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
