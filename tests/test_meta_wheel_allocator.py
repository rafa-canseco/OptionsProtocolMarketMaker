from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from src.meta_wheel_allocator import (
    ActionKind,
    AssignmentLot,
    CanonicalReceipt,
    LaneKind,
    LanePhase,
    LaneSnapshot,
    LotStatus,
    MetaWheelAllocator,
    MetaWheelPlanner,
    PendingCspTranche,
    Reconciliation,
    SqliteActionJournal,
    WheelQuote,
    WheelSnapshot,
    required_call_floor8,
)
from src.meta_wheel_policy import load_meta_wheel_policy, sha256_file


POLICY_PATH = Path("policies/meta_wheel_policy.v1.base-sepolia.json")


@pytest.fixture
def policy():
    return load_meta_wheel_policy(
        POLICY_PATH, approved_hash=sha256_file(POLICY_PATH)
    )


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
    return LaneSnapshot(
        address=address,
        kind=kind,
        phase=phase,
        tranche_id=tranche_id,
        transition_nonce=nonce,
        child_position_id=position_id,
        amount=amount,
        expiry=expiry,
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
        nav_coherent=True,
        nav_fresh=True,
        positions_hash_match=True,
        transition_balances_reconciled=True,
        paused=False,
        parent_total_assets_usdc=10_000 * 10**6,
        idle_usdc=10_000 * 10**6,
        pending_csp_usdc=0,
        pending_csp_tranches=(),
        pending_redemption_usdc=0,
        reserved_redemption_usdc=0,
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
    )


def test_new_usdc_is_queued_in_bounded_csp_tranches(policy):
    actions = MetaWheelPlanner(policy).plan(snapshot(policy), ())

    assert len(actions) == 1
    assert actions[0].kind == ActionKind.QUEUE_CSP_USDC
    assert actions[0].amount == 5_000 * 10**6
    assert actions[0].transition_nonce == 1


def test_pending_csp_tranche_opens_atomically_on_free_lane(policy):
    pending = PendingCspTranche(1, 2, 5_000 * 10**6)
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
    state = snapshot(
        policy,
        pending_csp_usdc=5_000 * 10**6,
        pending_redemption_usdc=4_000 * 10**6,
        reserved_redemption_usdc=1_000 * 10**6,
        csp_lanes=(open_lane, lane("0xcsp2", LaneKind.CSP)),
    )

    kinds = [action.kind for action in MetaWheelPlanner(policy).plan(state, ())]

    assert kinds == [ActionKind.SETTLE_CSP, ActionKind.RESERVE_REDEMPTION]


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
    public_lane = replace(
        lane("0xpublic", LaneKind.CSP), dedicated_to_parent=False
    )
    with pytest.raises(RuntimeError, match="non-dedicated"):
        planner.plan(snapshot(policy, csp_lanes=(public_lane,)), ())
    with pytest.raises(RuntimeError, match="fee configuration"):
        planner.plan(snapshot(policy, child_management_fee_bps=1), ())


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

    def submit(self, action, _policy):
        from src.meta_wheel_allocator import SubmittedAction

        self.submissions.append(action)
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
        return Reconciliation(True, True, True, True, True)


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
