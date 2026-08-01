from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from eth_abi import decode, encode

from src.meta_wheel_allocator import (
    ActionKind,
    AssignmentLot,
    CanonicalReceipt,
    ContractLaneKind,
    LaneKind,
    LanePhase,
    LaneSnapshot,
    LotStatus,
    ManagedOperation,
    ManagedOperationClass,
    MetaWheelAllocator,
    MetaWheelPlanner,
    PendingCspTranche,
    Reconciliation,
    SqliteActionJournal,
    WheelAction,
    WheelQuote,
    WheelSnapshot,
    StrategyManagerWrapper,
    encode_managed_operation,
    managed_operation_for_action,
    required_call_floor8,
)
from src.meta_wheel_policy import load_meta_wheel_policy, sha256_file


POLICY_PATH = Path("policies/meta_wheel_policy.v1.base-sepolia.json")


@pytest.fixture
def policy():
    return load_meta_wheel_policy(POLICY_PATH, approved_hash=sha256_file(POLICY_PATH))


def lane(
    address: str,
    kind: LaneKind,
    phase: LanePhase = LanePhase.IDLE,
    *,
    tranche_id: int = 0,
    nonce: int = 0,
    position_id: int = 0,
    amount: int = 0,
    expiry: int = 0,
    lot_ids: tuple[int, ...] = (),
) -> LaneSnapshot:
    active = phase not in {LanePhase.IDLE, LanePhase.PAUSED}
    execution_hash = f"execution:{address}:{nonce}" if active else ""
    position_hash = f"position:{address}:{nonce}" if active else ""
    return LaneSnapshot(
        address=address,
        kind=kind,
        phase=phase,
        tranche_id=tranche_id,
        transition_nonce=nonce,
        child_position_id=position_id,
        amount=amount,
        expiry=expiry,
        execution_state_hash=execution_hash,
        tranche_child_execution_state_hash=execution_hash,
        position_state_hash=position_hash,
        nav_position_state_hash=position_hash,
        lot_ids=lot_ids,
        active_options=int(phase in {LanePhase.CSP_OPEN, LanePhase.CALL_OPEN}),
    )


def lot(
    lot_id: int,
    strike: int,
    *,
    amount: int = 10**18,
    status: LotStatus = LotStatus.AVAILABLE,
) -> AssignmentLot:
    return AssignmentLot(
        lot_id=lot_id,
        tranche_id=lot_id,
        tranche_state_nonce=2,
        origin_csp_lane=f"0xcsp{lot_id}",
        origin_csp_position_id=lot_id,
        weth_received=amount,
        remaining_weth=amount,
        literal_assignment_strike8=strike * 10**8,
        created_at=100,
        status=status,
    )


def snapshot(policy, **changes) -> WheelSnapshot:
    base = WheelSnapshot(
        chain_id=84532,
        parent="0xparent",
        coordinator="0xcoordinator",
        safe_block=100,
        safe_block_confirmations=2,
        safe_block_canonical=True,
        timestamp=1_000_000,
        onchain_policy_hash=policy.policy_hash,
        nav_policy_hash=policy.policy_hash,
        onchain_floor_buffer8=policy.execution_cost_buffer,
        onchain_max_csp_lanes=policy.maximum_csp_lanes,
        onchain_max_call_lanes=policy.maximum_cc_lanes,
        onchain_max_usdc_per_csp_lane=policy.maximum_usdc_per_csp_lane,
        onchain_max_weth_per_call_lane=policy.maximum_weth_per_cc_lane,
        coordinator_position_state_hash="coordinator-position-hash",
        nav_coordinator_position_state_hash="coordinator-position-hash",
        nav_coherent=True,
        nav_fresh=True,
        transition_balances_reconciled=True,
        paused=False,
        parent_total_assets_usdc=10_000 * 10**6,
        idle_usdc=10_000 * 10**6,
        pending_csp_usdc=0,
        pending_csp_tranches=(),
        pending_redemption_usdc=0,
        reserved_redemption_usdc=0,
        reserved_principal_usdc=0,
        coordinator_transition_nonce=1,
        fund_flow_nonce=1,
        spot_price8=2_000 * 10**8,
        protocol_premium_fee_bps=1000,
        parent_management_fee_bps=200,
        parent_performance_fee_bps=1000,
        child_management_fee_bps=0,
        child_performance_fee_bps=0,
        csp_lanes=(
            lane("0xcsp1", LaneKind.CSP),
            lane("0xcsp2", LaneKind.CSP),
        ),
        call_lanes=(
            lane("0xcc1", LaneKind.COVERED_CALL),
            lane("0xcc2", LaneKind.COVERED_CALL),
        ),
        assignment_lots=(),
    )
    return replace(base, **changes)


def quote(
    *,
    quote_id: str,
    is_put: bool,
    strike: int,
    now: int = 1_000_000,
    delta_bps: int | None = None,
) -> WheelQuote:
    return WheelQuote(
        quote_id=quote_id,
        is_put=is_put,
        strike8=strike * 10**8,
        expiry=now + 48 * 3600,
        created_at=now - 5,
        deadline=now + 60,
        gross_premium_bps=20,
        maximum_collateral=10_000 * 10**18,
        canonical_series=True,
        delta_bps=delta_bps,
        open_data=b"signed-open-data",
    )


def test_frozen_managed_operation_discriminants_and_wrappers():
    assert list(ManagedOperationClass) == [
        ManagedOperationClass.NONE,
        ManagedOperationClass.ALLOCATION,
        ManagedOperationClass.PROCESSING,
        ManagedOperationClass.GUARDIAN,
        ManagedOperationClass.CONFIGURATION,
    ]
    assert ManagedOperation.OPEN_CSP == 1
    assert ManagedOperation.SPLIT_PENDING_CSP == 3
    assert ManagedOperation.RESERVE_REDEMPTION == 8
    assert ManagedOperation.REMOVE_LANE == 12
    assert ManagedOperation.RESUME_ALLOCATIONS == 16

    request = encode_managed_operation(
        ManagedOperation.RESERVE_REDEMPTION, 7, 1_500 * 10**6
    )

    expected_arguments = encode(["uint256", "uint256"], [7, 1_500 * 10**6])
    assert request.wrapper == StrategyManagerWrapper.PROCESSING
    assert request.operation_class == ManagedOperationClass.PROCESSING
    assert request.arguments == expected_arguments
    assert request.data == encode(
        ["uint8", "bytes"],
        [int(ManagedOperation.RESERVE_REDEMPTION), expected_arguments],
    )


def test_remove_lane_uses_configuration_wrapper_and_exact_address_encoding():
    address = "0x00000000000000000000000000000000000000a1"

    request = encode_managed_operation(ManagedOperation.REMOVE_LANE, address)
    operation, arguments = decode(["uint8", "bytes"], request.data)
    (decoded_address,) = decode(["address"], arguments)

    assert request.wrapper == StrategyManagerWrapper.CONFIGURATION
    assert request.operation_class == ManagedOperationClass.CONFIGURATION
    assert operation == ManagedOperation.REMOVE_LANE
    assert decoded_address.lower() == address

    register = encode_managed_operation(
        ManagedOperation.REGISTER_LANE, address, ContractLaneKind.CSP
    )
    register_operation, register_arguments = decode(["uint8", "bytes"], register.data)
    registered_address, lane_kind = decode(["address", "uint8"], register_arguments)
    assert register.wrapper == StrategyManagerWrapper.CONFIGURATION
    assert register_operation == ManagedOperation.REGISTER_LANE
    assert (registered_address.lower(), lane_kind) == (address, ContractLaneKind.CSP)


def test_open_action_encodes_final_managed_allocation_payload():
    address = "0x00000000000000000000000000000000000000c5"
    action = WheelAction(
        kind=ActionKind.OPEN_CSP,
        chain_id=84532,
        parent="0xparent",
        lane=address,
        tranche_id=4,
        transition_nonce=2,
        child_position_id=0,
        amount=5_000 * 10**6,
        open_data=b"signed-open-data",
    )

    request = managed_operation_for_action(action)
    operation, arguments = decode(["uint8", "bytes"], request.data)
    tranche_id, lane_address, open_data = decode(
        ["uint256", "address", "bytes"], arguments
    )

    assert request.wrapper == StrategyManagerWrapper.ALLOCATION
    assert operation == ManagedOperation.OPEN_CSP
    assert (tranche_id, lane_address.lower(), open_data) == (
        4,
        address,
        b"signed-open-data",
    )


def test_automated_action_surface_never_reaches_guardian_or_configuration():
    address = "0x00000000000000000000000000000000000000c5"
    base = WheelAction(
        kind=ActionKind.OPEN_CSP,
        chain_id=84532,
        parent="0xparent",
        lane=address,
        tranche_id=4,
        transition_nonce=2,
        child_position_id=0,
        amount=1,
        open_data=b"signed-open-data",
    )

    for kind in set(ActionKind) - {ActionKind.QUEUE_CSP_USDC}:
        request = managed_operation_for_action(replace(base, kind=kind))
        assert request.operation_class in {
            ManagedOperationClass.ALLOCATION,
            ManagedOperationClass.PROCESSING,
        }


def test_new_usdc_is_queued_in_bounded_csp_tranches(policy):
    actions = MetaWheelPlanner(policy).plan(snapshot(policy), ())

    assert len(actions) == 1
    assert actions[0].kind == ActionKind.QUEUE_CSP_USDC
    assert actions[0].amount == 5_000 * 10**6
    assert actions[0].transition_nonce == 1


def test_pending_csp_tranche_opens_atomically_on_free_lane(policy):
    pending = PendingCspTranche(1, 2, 5_000 * 10**6, 5_000 * 10**6)
    state = snapshot(
        policy,
        idle_usdc=0,
        pending_csp_usdc=pending.pending_usdc,
        pending_csp_tranches=(pending,),
    )
    put = quote(quote_id="put", is_put=True, strike=1700)

    actions = MetaWheelPlanner(policy).plan(state, (put,))

    assert len(actions) == 1
    assert actions[0].kind == ActionKind.OPEN_CSP
    assert actions[0].tranche_id == 1
    assert actions[0].lane == "0xcsp1"


def test_oversized_pending_tranche_splits_then_sibling_opens(policy):
    oversized = PendingCspTranche(1, 2, 12_000 * 10**6, 10_000 * 10**6)
    first_tick = snapshot(
        policy,
        idle_usdc=0,
        pending_csp_usdc=oversized.pending_usdc,
        pending_csp_tranches=(oversized,),
        csp_lanes=(lane("0xcsp1", LaneKind.CSP),),
    )
    put = quote(quote_id="put", is_put=True, strike=1700)

    first_actions = MetaWheelPlanner(policy).plan(first_tick, (put,))

    assert [
        (action.kind, action.tranche_id, action.amount) for action in first_actions
    ] == [(ActionKind.SPLIT_PENDING_CSP, 1, 5_000 * 10**6)]
    assert managed_operation_for_action(first_actions[0]).operation == (
        ManagedOperation.SPLIT_PENDING_CSP
    )

    original = PendingCspTranche(1, 3, 7_000 * 10**6, 5_833_333_334)
    sibling = PendingCspTranche(2, 1, 5_000 * 10**6, 4_166_666_666)
    second_tick = replace(
        first_tick,
        pending_csp_tranches=(original, sibling),
    )

    second_actions = MetaWheelPlanner(policy).plan(second_tick, (put,))

    assert [(action.kind, action.tranche_id) for action in second_actions] == [
        (ActionKind.SPLIT_PENDING_CSP, 1),
        (ActionKind.OPEN_CSP, 2),
    ]


def test_redemptions_preempt_new_allocation_but_not_expired_settlement(policy):
    open_lane = lane(
        "0xcsp1",
        LaneKind.CSP,
        LanePhase.CSP_OPEN,
        tranche_id=7,
        nonce=3,
        position_id=7,
        amount=1_000 * 10**6,
        expiry=999_999,
    )
    pending = PendingCspTranche(8, 1, 5_000 * 10**6, 4_500 * 10**6)
    state = snapshot(
        policy,
        pending_csp_usdc=pending.pending_usdc,
        pending_csp_tranches=(pending,),
        pending_redemption_usdc=4_000 * 10**6,
        reserved_redemption_usdc=1_000 * 10**6,
        csp_lanes=(open_lane, lane("0xcsp2", LaneKind.CSP)),
    )

    kinds = [action.kind for action in MetaWheelPlanner(policy).plan(state, ())]

    assert kinds == [ActionKind.SETTLE_CSP, ActionKind.RESERVE_REDEMPTION]
    reserve = MetaWheelPlanner(policy).plan(state, ())[-1]
    assert (reserve.tranche_id, reserve.amount) == (8, 3_000 * 10**6)


def test_settling_tranches_handoff_once(policy):
    state = snapshot(
        policy,
        idle_usdc=0,
        csp_lanes=(
            lane(
                "0xcsp1",
                LaneKind.CSP,
                LanePhase.CSP_SETTLING,
                tranche_id=3,
                nonce=4,
                position_id=9,
            ),
        ),
        call_lanes=(
            lane(
                "0xcc1",
                LaneKind.COVERED_CALL,
                LanePhase.CALL_SETTLING,
                tranche_id=4,
                nonce=5,
                position_id=10,
                lot_ids=(4,),
            ),
        ),
    )

    actions = MetaWheelPlanner(policy).plan(state, ())

    assert [action.kind for action in actions] == [
        ActionKind.HANDOFF_ASSIGNMENT,
        ActionKind.HANDOFF_CALL_AWAY,
    ]
    assert actions[0].key != actions[1].key


def test_call_floor_uses_literal_strike_plus_buffer_and_ceil(policy):
    assert required_call_floor8(lot(1, 2001), policy) == 2015 * 10**8


def test_assignment_lot_origin_and_strike_are_immutable():
    assignment = lot(1, 2000)

    with pytest.raises(FrozenInstanceError):
        assignment.literal_assignment_strike8 = 1900 * 10**8  # type: ignore[misc]


def test_below_floor_or_stale_call_quote_leaves_weth_idle(policy):
    assignment = lot(1, 2000)
    state = snapshot(
        policy,
        idle_usdc=0,
        csp_lanes=(lane("0xcsp1", LaneKind.CSP, LanePhase.PAUSED),),
        call_lanes=(lane("0xcc1", LaneKind.COVERED_CALL),),
        assignment_lots=(assignment,),
    )
    below = quote(quote_id="below", is_put=False, strike=2005, delta_bps=500)
    stale = replace(
        quote(quote_id="stale", is_put=False, strike=2010, delta_bps=500),
        created_at=state.timestamp - policy.quote_maximum_age - 1,
    )

    assert MetaWheelPlanner(policy).plan(state, (below, stale)) == ()


def test_call_quote_at_or_above_floor_is_selected(policy):
    assignment = lot(1, 2001)
    state = snapshot(
        policy,
        idle_usdc=0,
        csp_lanes=(lane("0xcsp1", LaneKind.CSP, LanePhase.PAUSED),),
        call_lanes=(lane("0xcc1", LaneKind.COVERED_CALL),),
        assignment_lots=(assignment,),
    )

    actions = MetaWheelPlanner(policy).plan(
        state,
        (
            quote(quote_id="below", is_put=False, strike=2010, delta_bps=500),
            quote(quote_id="protected", is_put=False, strike=2015, delta_bps=500),
        ),
    )

    assert len(actions) == 1
    assert actions[0].kind == ActionKind.OPEN_CALL
    assert actions[0].lot_ids == (1,)
    assert actions[0].quote_id == "protected"
    assert actions[0].required_floor8 == 2015 * 10**8


def test_assignment_and_sibling_cash_tranches_progress_in_parallel(policy):
    """A mixed CSP handoff keeps the WETH tranche and queues USDC as a sibling."""
    sibling = PendingCspTranche(2, 1, 1_000 * 10**6, 800 * 10**6)
    assignment = lot(1, 2001)
    first_tick = snapshot(
        policy,
        idle_usdc=0,
        pending_csp_usdc=sibling.pending_usdc,
        pending_csp_tranches=(sibling,),
        csp_lanes=(lane("0xcsp1", LaneKind.CSP),),
        call_lanes=(lane("0xcc1", LaneKind.COVERED_CALL),),
        assignment_lots=(assignment,),
    )
    actions = MetaWheelPlanner(policy).plan(
        first_tick,
        (
            quote(quote_id="sibling-put", is_put=True, strike=1700),
            quote(
                quote_id="assigned-call",
                is_put=False,
                strike=2015,
                delta_bps=500,
            ),
        ),
    )

    assert [(action.kind, action.tranche_id) for action in actions] == [
        (ActionKind.OPEN_CALL, 1),
        (ActionKind.OPEN_CSP, 2),
    ]

    next_tick = replace(
        first_tick,
        timestamp=first_tick.timestamp + 48 * 3600,
        pending_csp_usdc=0,
        pending_csp_tranches=(),
        assignment_lots=(replace(assignment, status=LotStatus.CALL_OPEN),),
        csp_lanes=(
            lane(
                "0xcsp1",
                LaneKind.CSP,
                LanePhase.CSP_OPEN,
                tranche_id=2,
                nonce=2,
                position_id=20,
                expiry=first_tick.timestamp + 48 * 3600,
            ),
        ),
        call_lanes=(
            lane(
                "0xcc1",
                LaneKind.COVERED_CALL,
                LanePhase.CALL_OPEN,
                tranche_id=1,
                nonce=3,
                position_id=10,
                expiry=first_tick.timestamp + 48 * 3600,
                lot_ids=(1,),
            ),
        ),
    )

    next_actions = MetaWheelPlanner(policy).plan(next_tick, ())

    assert [(action.kind, action.tranche_id) for action in next_actions] == [
        (ActionKind.SETTLE_CSP, 2),
        (ActionKind.SETTLE_CALL, 1),
    ]


def test_available_lots_are_fifo_and_unselected_lot_remains_queued(policy):
    assignments = (lot(1, 1800), lot(2, 2200))
    state = snapshot(
        policy,
        idle_usdc=0,
        csp_lanes=(
            lane("0xcsp1", LaneKind.CSP, LanePhase.PAUSED),
            lane("0xcsp2", LaneKind.CSP, LanePhase.PAUSED),
        ),
        call_lanes=(lane("0xcc1", LaneKind.COVERED_CALL),),
        assignment_lots=assignments,
    )

    actions = MetaWheelPlanner(policy).plan(
        state,
        (quote(quote_id="high", is_put=False, strike=2210, delta_bps=500),),
    )

    assert len(actions) == 1
    assert actions[0].lot_ids == (1,)
    assert assignments[1].status == LotStatus.AVAILABLE


def test_four_lanes_progress_one_lot_each_and_fifth_remains_queued(policy):
    assignments = tuple(lot(index, 2000 + index * 5) for index in range(1, 5)) + (
        replace(lot(5, 2025), origin_csp_lane="0xcsp1"),
    )
    state = snapshot(
        policy,
        idle_usdc=0,
        csp_lanes=tuple(
            lane(f"0xcsp{index}", LaneKind.CSP, LanePhase.PAUSED)
            for index in range(1, 5)
        ),
        call_lanes=tuple(
            lane(f"0xcc{index}", LaneKind.COVERED_CALL) for index in range(1, 5)
        ),
        assignment_lots=assignments,
    )
    quotes = tuple(
        quote(
            quote_id=f"call-{index}",
            is_put=False,
            strike=2020 + index * 5,
            delta_bps=500,
        )
        for index in range(1, 6)
    )

    actions = MetaWheelPlanner(policy).plan(state, quotes)

    assert len(actions) == 4
    assert [action.lot_ids for action in actions] == [(1,), (2,), (3,), (4,)]
    assert assignments[4].status == LotStatus.AVAILABLE


def test_wrong_fee_hash_stale_nav_or_standalone_lane_fails_closed(policy):
    planner = MetaWheelPlanner(policy)
    with pytest.raises(RuntimeError, match="policy hash"):
        planner.plan(snapshot(policy, onchain_policy_hash="wrong"), ())
    with pytest.raises(RuntimeError, match="NAV or custody"):
        planner.plan(snapshot(policy, nav_fresh=False), ())
    public_lane = replace(lane("0xpublic", LaneKind.CSP), dedicated_to_parent=False)
    with pytest.raises(RuntimeError, match="non-dedicated"):
        planner.plan(snapshot(policy, csp_lanes=(public_lane,)), ())
    with pytest.raises(RuntimeError, match="fee configuration"):
        planner.plan(snapshot(policy, child_management_fee_bps=1), ())


def test_execution_and_nav_hash_domains_are_reconciled_separately(policy):
    active = lane(
        "0xcsp1",
        LaneKind.CSP,
        LanePhase.CSP_OPEN,
        tranche_id=1,
        nonce=2,
        position_id=3,
        expiry=2_000_000,
    )
    planner = MetaWheelPlanner(policy)

    with pytest.raises(RuntimeError, match="executionStateHash"):
        planner.plan(
            snapshot(
                policy,
                idle_usdc=0,
                csp_lanes=(
                    replace(active, tranche_child_execution_state_hash="orphaned"),
                ),
            ),
            (),
        )
    with pytest.raises(RuntimeError, match="child positionStateHash"):
        planner.plan(
            snapshot(
                policy,
                idle_usdc=0,
                csp_lanes=(replace(active, nav_position_state_hash="stale-nav"),),
            ),
            (),
        )
    with pytest.raises(RuntimeError, match="coordinator positionStateHash"):
        planner.plan(
            snapshot(
                policy,
                coordinator_position_state_hash="new",
                nav_coordinator_position_state_hash="old",
            ),
            (),
        )


class MemoryJournal:
    def __init__(self):
        self.items: dict[str, tuple[str, str | None]] = {}

    def get(self, action_key):
        return self.items.get(action_key)

    def record(self, action_key, status, tx_hash):
        self.items[action_key] = (status, tx_hash)


class FakeChain:
    def __init__(self, state, actions_quotes=()):
        self.state = state
        self.quotes = actions_quotes
        self.submissions = []
        self.canonical = True
        self.missing_receipts: set[str] = set()
        self.noncanonical_receipts: set[str] = set()

    def read_snapshot(self, _policy):
        return self.state

    def list_quotes(self, _snapshot):
        return self.quotes

    def submit(self, action, _policy, managed_request):
        from src.meta_wheel_allocator import SubmittedAction

        self.submissions.append((action, managed_request))
        return SubmittedAction(tx_hash=f"0xtx{len(self.submissions)}", nonce=1)

    def receipt(self, tx_hash, _confirmations):
        if tx_hash in self.missing_receipts:
            return None
        return CanonicalReceipt(
            tx_hash,
            101,
            "0xblock",
            2,
            self.canonical and tx_hash not in self.noncanonical_receipts,
            True,
        )

    def reconcile(self, _action, _receipt):
        return Reconciliation(True, True, True, True, True, True)


def test_restart_skips_canonical_confirmed_action(policy):
    chain = FakeChain(snapshot(policy))
    journal = MemoryJournal()
    allocator = MetaWheelAllocator(
        policy_path=POLICY_PATH,
        approved_policy_hash=policy.policy_hash,
        chain=chain,
        journal=journal,
    )

    first = allocator.run_once()
    second = allocator.run_once()

    assert len(first) == 1
    assert second == first
    assert len(chain.submissions) == 1
    assert chain.submissions[0][1] is None


def test_runtime_passes_managed_processing_request_to_chain_port(policy):
    active = lane(
        "0xcsp1",
        LaneKind.CSP,
        LanePhase.CSP_OPEN,
        tranche_id=7,
        nonce=3,
        position_id=9,
        expiry=999_999,
    )
    chain = FakeChain(
        snapshot(
            policy,
            idle_usdc=0,
            csp_lanes=(active,),
            call_lanes=(),
        )
    )
    allocator = MetaWheelAllocator(
        policy_path=POLICY_PATH,
        approved_policy_hash=policy.policy_hash,
        chain=chain,
        journal=MemoryJournal(),
    )

    allocator.run_once()

    action, request = chain.submissions[0]
    assert action.kind == ActionKind.SETTLE_CSP
    assert request.wrapper == StrategyManagerWrapper.PROCESSING
    assert request.operation == ManagedOperation.SETTLE_CSP


def test_duplicate_action_key_is_chain_parent_tranche_nonce_position_scoped(policy):
    first = MetaWheelPlanner(policy).plan(snapshot(policy), ())[0]
    second = replace(first, amount=first.amount - 1)
    next_nonce = replace(first, transition_nonce=first.transition_nonce + 1)

    assert first.key == second.key
    assert first.key != next_nonce.key


def test_dropped_submission_is_replanned_from_same_onchain_nonce(policy):
    state = snapshot(policy)
    action = MetaWheelPlanner(policy).plan(state, ())[0]
    chain = FakeChain(state)
    chain.missing_receipts.add("0xdropped")
    journal = MemoryJournal()
    journal.record(action.key, "submitted", "0xdropped")
    allocator = MetaWheelAllocator(
        policy_path=POLICY_PATH,
        approved_policy_hash=policy.policy_hash,
        chain=chain,
        journal=journal,
    )

    allocator.run_once()

    assert len(chain.submissions) == 1
    assert journal.get(action.key) == ("confirmed", "0xtx1")


def test_reorged_confirmation_is_not_final_and_is_safely_replanned(policy):
    state = snapshot(policy)
    action = MetaWheelPlanner(policy).plan(state, ())[0]
    chain = FakeChain(state)
    chain.noncanonical_receipts.add("0xorphaned")
    journal = MemoryJournal()
    journal.record(action.key, "confirmed", "0xorphaned")
    allocator = MetaWheelAllocator(
        policy_path=POLICY_PATH,
        approved_policy_hash=policy.policy_hash,
        chain=chain,
        journal=journal,
    )

    allocator.run_once()

    assert len(chain.submissions) == 1
    assert journal.get(action.key) == ("confirmed", "0xtx1")


def test_sqlite_journal_persists_idempotency_state(tmp_path):
    path = tmp_path / "actions.sqlite3"
    first = SqliteActionJournal(path)
    first.record("key", "submitted", "0xtx")
    second = SqliteActionJournal(path)

    assert second.get("key") == ("submitted", "0xtx")
