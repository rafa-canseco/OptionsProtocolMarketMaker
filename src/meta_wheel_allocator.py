"""Fail-closed planner and idempotent runtime for dedicated Meta Wheel lanes.

Contract-specific reads and writes live behind :class:`MetaWheelChainPort`. The
managed operation encoding mirrors the ABI frozen by B1N-414/415. Standalone CSP
and Covered Call workers do not import this module and remain independent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol, Sequence

from eth_abi import encode

from src import config
from src.meta_wheel_policy import BPS, MetaWheelPolicy, load_meta_wheel_policy


log = logging.getLogger(__name__)
USDC_SCALE = 10**6
WETH_SCALE = 10**18
STRIKE_SCALE = 10**8


class LaneKind(StrEnum):
    CSP = "csp"
    COVERED_CALL = "covered_call"


class LanePhase(StrEnum):
    IDLE = "idle"
    CSP_OPEN = "csp_open"
    CSP_SETTLING = "csp_settling"
    CALL_OPEN = "call_open"
    CALL_SETTLING = "call_settling"
    PAUSED = "paused"


class LotStatus(StrEnum):
    AVAILABLE = "available"
    COMMITTED = "committed"
    CALL_OPEN = "call_open"
    RETURNED = "returned"
    CALLED_AWAY = "called_away"


class ActionKind(StrEnum):
    RESERVE_REDEMPTION = "reserve_redemption"
    RELEASE_REDEMPTION = "release_redemption"
    QUEUE_CSP_USDC = "queue_csp_usdc"
    SPLIT_PENDING_CSP = "split_pending_csp"
    OPEN_CSP = "open_csp"
    SETTLE_CSP = "settle_csp"
    HANDOFF_ASSIGNMENT = "handoff_assignment"
    OPEN_CALL = "open_call"
    SETTLE_CALL = "settle_call"
    HANDOFF_CALL_AWAY = "handoff_call_away"


class ManagedOperationClass(IntEnum):
    """Exact ``WheelTypes.ManagedOperationClass`` discriminants."""

    NONE = 0
    ALLOCATION = 1
    PROCESSING = 2
    GUARDIAN = 3
    CONFIGURATION = 4


class ContractLaneKind(IntEnum):
    """Exact ``WheelTypes.LaneKind`` discriminants used by ``RegisterLane``."""

    NONE = 0
    CSP = 1
    COVERED_CALL = 2


class ManagedOperation(IntEnum):
    """Exact ``WheelTypes.ManagedOperation`` discriminants."""

    NONE = 0
    OPEN_CSP = 1
    OPEN_COVERED_CALL = 2
    SPLIT_PENDING_CSP = 3
    SETTLE_CSP = 4
    HANDOFF_CSP = 5
    SETTLE_COVERED_CALL = 6
    HANDOFF_COVERED_CALL = 7
    RESERVE_REDEMPTION = 8
    RELEASE_REDEMPTION = 9
    PAUSE_ALLOCATIONS = 10
    REGISTER_LANE = 11
    REMOVE_LANE = 12
    SET_LANE_ACTIVE = 13
    SET_POLICY_HASH = 14
    SET_FLOOR_BUFFER = 15
    RESUME_ALLOCATIONS = 16


class StrategyManagerWrapper(StrEnum):
    ALLOCATION = "executeAdapterAllocationOperation"
    PROCESSING = "executeAdapterProcessingOperation"
    GUARDIAN = "executeAdapterGuardianOperation"
    CONFIGURATION = "executeAdapterConfigurationOperation"


@dataclass(frozen=True)
class ManagedOperationRequest:
    """Calldata submitted through a role-separated ``StrategyManager`` wrapper."""

    wrapper: StrategyManagerWrapper
    operation_class: ManagedOperationClass
    operation: ManagedOperation
    arguments: bytes
    data: bytes


_MANAGED_OPERATION_SPECS: dict[
    ManagedOperation, tuple[ManagedOperationClass, tuple[str, ...]]
] = {
    ManagedOperation.OPEN_CSP: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "address", "bytes"),
    ),
    ManagedOperation.OPEN_COVERED_CALL: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "address", "bytes"),
    ),
    ManagedOperation.SPLIT_PENDING_CSP: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "uint256"),
    ),
    ManagedOperation.SETTLE_CSP: (ManagedOperationClass.PROCESSING, ("uint256",)),
    ManagedOperation.HANDOFF_CSP: (ManagedOperationClass.PROCESSING, ("uint256",)),
    ManagedOperation.SETTLE_COVERED_CALL: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.HANDOFF_COVERED_CALL: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.RESERVE_REDEMPTION: (
        ManagedOperationClass.PROCESSING,
        ("uint256", "uint256"),
    ),
    ManagedOperation.RELEASE_REDEMPTION: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.PAUSE_ALLOCATIONS: (ManagedOperationClass.GUARDIAN, ()),
    ManagedOperation.REGISTER_LANE: (
        ManagedOperationClass.CONFIGURATION,
        ("address", "uint8"),
    ),
    ManagedOperation.REMOVE_LANE: (
        ManagedOperationClass.CONFIGURATION,
        ("address",),
    ),
    ManagedOperation.SET_LANE_ACTIVE: (
        ManagedOperationClass.CONFIGURATION,
        ("address", "bool"),
    ),
    ManagedOperation.SET_POLICY_HASH: (
        ManagedOperationClass.CONFIGURATION,
        ("bytes32",),
    ),
    ManagedOperation.SET_FLOOR_BUFFER: (
        ManagedOperationClass.CONFIGURATION,
        ("uint256",),
    ),
    ManagedOperation.RESUME_ALLOCATIONS: (ManagedOperationClass.CONFIGURATION, ()),
}

_WRAPPER_BY_CLASS = {
    ManagedOperationClass.ALLOCATION: StrategyManagerWrapper.ALLOCATION,
    ManagedOperationClass.PROCESSING: StrategyManagerWrapper.PROCESSING,
    ManagedOperationClass.GUARDIAN: StrategyManagerWrapper.GUARDIAN,
    ManagedOperationClass.CONFIGURATION: StrategyManagerWrapper.CONFIGURATION,
}


def encode_managed_operation(
    operation: ManagedOperation, *values: object
) -> ManagedOperationRequest:
    """Encode the dispatcher's exact ``abi.encode(operation, arguments)`` payload."""

    try:
        operation_class, argument_types = _MANAGED_OPERATION_SPECS[operation]
    except KeyError as error:
        raise ValueError(
            f"Unsupported managed Wheel operation {operation!r}"
        ) from error
    arguments = encode(list(argument_types), list(values)) if argument_types else b""
    data = encode(["uint8", "bytes"], [int(operation), arguments])
    return ManagedOperationRequest(
        wrapper=_WRAPPER_BY_CLASS[operation_class],
        operation_class=operation_class,
        operation=operation,
        arguments=arguments,
        data=data,
    )


@dataclass(frozen=True)
class AssignmentLot:
    lot_id: int
    tranche_id: int
    tranche_state_nonce: int
    origin_csp_lane: str
    origin_csp_position_id: int
    weth_received: int
    remaining_weth: int
    literal_assignment_strike8: int
    created_at: int
    status: LotStatus

    def validate(self) -> None:
        if (
            self.lot_id <= 0
            or self.tranche_id <= 0
            or self.tranche_state_nonce <= 0
            or self.origin_csp_position_id <= 0
            or self.weth_received <= 0
            or not 0 <= self.remaining_weth <= self.weth_received
            or self.literal_assignment_strike8 <= 0
            or self.created_at <= 0
            or not self.origin_csp_lane
        ):
            raise RuntimeError(f"Invalid immutable assignment lot {self.lot_id}")


@dataclass(frozen=True)
class PendingCspTranche:
    tranche_id: int
    state_nonce: int
    pending_usdc: int
    principal_usdc: int

    def validate(self) -> None:
        if (
            self.tranche_id <= 0
            or self.state_nonce <= 0
            or self.pending_usdc <= 0
            or self.principal_usdc < 0
        ):
            raise RuntimeError(f"Invalid pending CSP tranche {self.tranche_id}")


@dataclass(frozen=True)
class LaneSnapshot:
    address: str
    kind: LaneKind
    phase: LanePhase
    tranche_id: int
    transition_nonce: int
    child_position_id: int
    amount: int
    expiry: int
    execution_state_hash: str
    tranche_child_execution_state_hash: str
    position_state_hash: str
    nav_position_state_hash: str
    lot_ids: tuple[int, ...] = ()
    dedicated_to_parent: bool = True
    active_options: int = 0


@dataclass(frozen=True)
class WheelQuote:
    quote_id: str
    is_put: bool
    strike8: int
    expiry: int
    created_at: int
    deadline: int
    gross_premium_bps: int
    maximum_collateral: int
    canonical_series: bool
    delta_bps: int | None = None
    execution_slippage_bps: int = 0
    open_data: bytes = b""


@dataclass(frozen=True)
class WheelSnapshot:
    chain_id: int
    parent: str
    coordinator: str
    safe_block: int
    safe_block_confirmations: int
    safe_block_canonical: bool
    timestamp: int
    onchain_policy_hash: str
    nav_policy_hash: str
    onchain_floor_buffer8: int
    onchain_max_csp_lanes: int
    onchain_max_call_lanes: int
    onchain_max_usdc_per_csp_lane: int
    onchain_max_weth_per_call_lane: int
    coordinator_position_state_hash: str
    nav_coordinator_position_state_hash: str
    nav_coherent: bool
    nav_fresh: bool
    transition_balances_reconciled: bool
    paused: bool
    parent_total_assets_usdc: int
    idle_usdc: int
    pending_csp_usdc: int
    pending_csp_tranches: tuple[PendingCspTranche, ...]
    pending_redemption_usdc: int
    reserved_redemption_usdc: int
    reserved_principal_usdc: int
    coordinator_transition_nonce: int
    fund_flow_nonce: int
    spot_price8: int
    protocol_premium_fee_bps: int
    parent_management_fee_bps: int
    parent_performance_fee_bps: int
    child_management_fee_bps: int
    child_performance_fee_bps: int
    csp_lanes: tuple[LaneSnapshot, ...]
    call_lanes: tuple[LaneSnapshot, ...]
    assignment_lots: tuple[AssignmentLot, ...]


@dataclass(frozen=True)
class WheelAction:
    kind: ActionKind
    chain_id: int
    parent: str
    lane: str
    tranche_id: int
    transition_nonce: int
    child_position_id: int
    amount: int = 0
    lot_ids: tuple[int, ...] = ()
    quote_id: str | None = None
    strike8: int | None = None
    required_floor8: int | None = None
    open_data: bytes = b""

    @property
    def key(self) -> str:
        canonical = {
            "chain_id": self.chain_id,
            "child_position_id": self.child_position_id,
            "kind": self.kind,
            "lane": self.lane.lower(),
            "parent": self.parent.lower(),
            "tranche_id": self.tranche_id,
            "transition_nonce": self.transition_nonce,
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def managed_operation_for_action(action: WheelAction) -> ManagedOperationRequest:
    """Map a lifecycle action to the frozen coordinator dispatcher ABI."""

    if action.kind == ActionKind.OPEN_CSP:
        return encode_managed_operation(
            ManagedOperation.OPEN_CSP,
            action.tranche_id,
            action.lane,
            action.open_data,
        )
    if action.kind == ActionKind.OPEN_CALL:
        return encode_managed_operation(
            ManagedOperation.OPEN_COVERED_CALL,
            action.tranche_id,
            action.lane,
            action.open_data,
        )
    if action.kind == ActionKind.SPLIT_PENDING_CSP:
        return encode_managed_operation(
            ManagedOperation.SPLIT_PENDING_CSP, action.tranche_id, action.amount
        )
    if action.kind == ActionKind.SETTLE_CSP:
        return encode_managed_operation(ManagedOperation.SETTLE_CSP, action.tranche_id)
    if action.kind == ActionKind.HANDOFF_ASSIGNMENT:
        return encode_managed_operation(ManagedOperation.HANDOFF_CSP, action.tranche_id)
    if action.kind == ActionKind.SETTLE_CALL:
        return encode_managed_operation(
            ManagedOperation.SETTLE_COVERED_CALL, action.tranche_id
        )
    if action.kind == ActionKind.HANDOFF_CALL_AWAY:
        return encode_managed_operation(
            ManagedOperation.HANDOFF_COVERED_CALL, action.tranche_id
        )
    if action.kind == ActionKind.RESERVE_REDEMPTION:
        return encode_managed_operation(
            ManagedOperation.RESERVE_REDEMPTION, action.tranche_id, action.amount
        )
    if action.kind == ActionKind.RELEASE_REDEMPTION:
        return encode_managed_operation(
            ManagedOperation.RELEASE_REDEMPTION, action.amount
        )
    raise ValueError(f"Action {action.kind} is not a managed coordinator operation")


@dataclass(frozen=True)
class SubmittedAction:
    tx_hash: str
    nonce: int


@dataclass(frozen=True)
class CanonicalReceipt:
    tx_hash: str
    block_number: int
    block_hash: str
    confirmations: int
    canonical: bool
    succeeded: bool


@dataclass(frozen=True)
class Reconciliation:
    child_shares_delta_matches: bool
    usdc_delta_matches: bool
    weth_delta_matches: bool
    principal_delta_matches: bool
    transition_nonce_advanced: bool
    premium_fee_matches: bool

    @property
    def valid(self) -> bool:
        return all(asdict(self).values())


class MetaWheelChainPort(Protocol):
    """Contract adapter boundary; every read must use a confirmed safe block.

    Implementations must route every non-queue action through
    :func:`managed_operation_for_action` and the request's role-separated
    ``StrategyManager`` wrapper. ``QUEUE_CSP_USDC`` remains the manager's normal
    ``allocate`` entrypoint.
    """

    def read_snapshot(self, policy: MetaWheelPolicy) -> WheelSnapshot: ...

    def list_quotes(self, snapshot: WheelSnapshot) -> Sequence[WheelQuote]: ...

    def submit(
        self,
        action: WheelAction,
        policy: MetaWheelPolicy,
        managed_request: ManagedOperationRequest | None,
    ) -> SubmittedAction: ...

    def receipt(self, tx_hash: str, confirmations: int) -> CanonicalReceipt | None: ...

    def reconcile(
        self, action: WheelAction, receipt: CanonicalReceipt
    ) -> Reconciliation: ...


class ActionJournal(Protocol):
    def get(self, action_key: str) -> tuple[str, str | None] | None: ...

    def record(self, action_key: str, status: str, tx_hash: str | None) -> None: ...


class SqliteActionJournal:
    """Durable idempotency journal used in addition to authoritative chain state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meta_wheel_actions (
                action_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                tx_hash TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()

    def get(self, action_key: str) -> tuple[str, str | None] | None:
        row = self.connection.execute(
            "SELECT status, tx_hash FROM meta_wheel_actions WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        return (str(row[0]), None if row[1] is None else str(row[1])) if row else None

    def record(self, action_key: str, status: str, tx_hash: str | None) -> None:
        self.connection.execute(
            """
            INSERT INTO meta_wheel_actions(action_key, status, tx_hash, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(action_key) DO UPDATE SET
                status=excluded.status,
                tx_hash=excluded.tx_hash,
                updated_at=excluded.updated_at
            """,
            (action_key, status, tx_hash, int(time.time())),
        )
        self.connection.commit()


def required_call_floor8(lot: AssignmentLot, policy: MetaWheelPolicy) -> int:
    lot.validate()
    if lot.status not in {LotStatus.AVAILABLE, LotStatus.COMMITTED}:
        raise RuntimeError(f"Assignment lot {lot.lot_id} cannot fund a call")
    buffered = lot.literal_assignment_strike8 + policy.execution_cost_buffer
    return (
        (buffered + policy.strike_tick - 1) // policy.strike_tick
    ) * policy.strike_tick


def _quote_is_fresh(quote: WheelQuote, now: int, policy: MetaWheelPolicy) -> bool:
    return (
        quote.canonical_series
        and bool(quote.open_data)
        and 0 <= now - quote.created_at <= policy.quote_maximum_age
        and quote.deadline - now >= policy.quote_minimum_ttl
        and policy.min_expiry_delay <= quote.expiry - now <= policy.max_expiry_delay
        and quote.maximum_collateral > 0
        and quote.execution_slippage_bps <= policy.maximum_execution_slippage_bps
        and quote.gross_premium_bps
        * (BPS - policy.protocol_gross_premium_fee_bps)
        // BPS
        >= policy.minimum_net_premium_bps
    )


def select_csp_quote(
    quotes: Sequence[WheelQuote], snapshot: WheelSnapshot, policy: MetaWheelPolicy
) -> WheelQuote | None:
    discounted = snapshot.spot_price8 * (BPS - policy.csp_strike_otm_bps) // BPS
    target = discounted // policy.csp_strike_tick * policy.csp_strike_tick
    candidates = [
        quote
        for quote in quotes
        if quote.is_put
        and quote.strike8 == target
        and _quote_is_fresh(quote, snapshot.timestamp, policy)
    ]
    return max(
        candidates, key=lambda quote: (quote.deadline, quote.quote_id), default=None
    )


def select_call_quote(
    quotes: Sequence[WheelQuote],
    snapshot: WheelSnapshot,
    policy: MetaWheelPolicy,
    lot: AssignmentLot,
) -> tuple[WheelQuote, int] | None:
    floor = required_call_floor8(lot, policy)
    candidates = [
        quote
        for quote in quotes
        if not quote.is_put
        and quote.strike8 >= floor
        and _quote_is_fresh(quote, snapshot.timestamp, policy)
        and quote.maximum_collateral >= lot.remaining_weth
    ]
    if not candidates:
        return None
    target = [
        quote
        for quote in candidates
        if quote.delta_bps is not None
        and abs(abs(quote.delta_bps) - policy.target_call_delta_bps)
        <= policy.maximum_call_delta_deviation_bps
    ]
    if target:
        selected = min(
            target,
            key=lambda quote: (
                abs(abs(quote.delta_bps or 0) - policy.target_call_delta_bps),
                quote.strike8,
                -quote.deadline,
            ),
        )
    else:
        # The protected floor wins when a target-delta series would violate it.
        selected = min(candidates, key=lambda quote: (quote.strike8, -quote.deadline))
    return selected, floor


class MetaWheelPlanner:
    def __init__(self, policy: MetaWheelPolicy) -> None:
        self.policy = policy

    def _validate_snapshot(self, snapshot: WheelSnapshot) -> None:
        policy = self.policy
        if snapshot.chain_id != policy.chain_id:
            raise RuntimeError("Meta Wheel snapshot is from the wrong chain")
        if not snapshot.parent or not snapshot.coordinator:
            raise RuntimeError("Meta Wheel parent/coordinator is not configured")
        if (
            snapshot.onchain_policy_hash != policy.policy_hash
            or snapshot.nav_policy_hash != policy.policy_hash
        ):
            raise RuntimeError("Meta Wheel policy hash differs from on-chain state")
        if (
            snapshot.onchain_floor_buffer8 != policy.execution_cost_buffer
            or snapshot.onchain_max_csp_lanes != policy.maximum_csp_lanes
            or snapshot.onchain_max_call_lanes != policy.maximum_cc_lanes
            or snapshot.onchain_max_usdc_per_csp_lane
            != policy.maximum_usdc_per_csp_lane
            or snapshot.onchain_max_weth_per_call_lane
            != policy.maximum_weth_per_cc_lane
        ):
            raise RuntimeError(
                "Meta Wheel on-chain floor or lane caps differ from policy"
            )
        if (
            snapshot.safe_block_confirmations < 2
            or not snapshot.safe_block_canonical
            or snapshot.safe_block <= 0
        ):
            raise RuntimeError("Meta Wheel safe block is not canonical and confirmed")
        if not (
            snapshot.nav_coherent
            and snapshot.nav_fresh
            and snapshot.transition_balances_reconciled
        ):
            raise RuntimeError("Meta Wheel NAV or custody state is not coherent")
        if (
            not snapshot.coordinator_position_state_hash
            or snapshot.coordinator_position_state_hash
            != snapshot.nav_coordinator_position_state_hash
        ):
            raise RuntimeError(
                "Meta Wheel coordinator positionStateHash differs from NAV"
            )
        if snapshot.reserved_principal_usdc < 0:
            raise RuntimeError("Meta Wheel reserved principal is invalid")
        expected_fees = (
            policy.protocol_gross_premium_fee_bps,
            policy.parent_management_fee_bps,
            policy.parent_performance_fee_bps,
            0,
            0,
        )
        actual_fees = (
            snapshot.protocol_premium_fee_bps,
            snapshot.parent_management_fee_bps,
            snapshot.parent_performance_fee_bps,
            snapshot.child_management_fee_bps,
            snapshot.child_performance_fee_bps,
        )
        if actual_fees != expected_fees:
            raise RuntimeError("Meta Wheel fee configuration differs from policy")
        if (
            len(snapshot.csp_lanes) > policy.maximum_csp_lanes
            or len(snapshot.call_lanes) > policy.maximum_cc_lanes
        ):
            raise RuntimeError("Meta Wheel registered lane count exceeds policy")
        addresses: set[str] = set()
        csp_addresses = {lane.address.lower() for lane in snapshot.csp_lanes}
        consumed_lots: set[int] = set()
        for expected_kind, lanes in (
            (LaneKind.CSP, snapshot.csp_lanes),
            (LaneKind.COVERED_CALL, snapshot.call_lanes),
        ):
            for lane in lanes:
                if lane.kind != expected_kind:
                    raise RuntimeError("Meta Wheel child lane kind is inconsistent")
                if lane.kind == LaneKind.COVERED_CALL:
                    for lot_id in lane.lot_ids:
                        if lot_id in consumed_lots:
                            raise RuntimeError(
                                "Meta Wheel lot is attached to two call lanes"
                            )
                        consumed_lots.add(lot_id)
        for lane in (*snapshot.csp_lanes, *snapshot.call_lanes):
            if not lane.dedicated_to_parent or not lane.address:
                raise RuntimeError("Meta Wheel references a non-dedicated child lane")
            address = lane.address.lower()
            if address in addresses:
                raise RuntimeError("Meta Wheel child lane is registered twice")
            addresses.add(address)
            if lane.active_options > policy.maximum_active_option_per_lane:
                raise RuntimeError("Meta Wheel child lane has excess active options")
            if lane.tranche_id < 0 or lane.transition_nonce < 0:
                raise RuntimeError("Meta Wheel lane identifiers are invalid")
            active = lane.phase not in {LanePhase.IDLE, LanePhase.PAUSED}
            if active and (
                not lane.execution_state_hash
                or lane.execution_state_hash != lane.tranche_child_execution_state_hash
            ):
                raise RuntimeError(
                    "Meta Wheel child executionStateHash differs from tranche"
                )
            if active and (
                not lane.position_state_hash
                or lane.position_state_hash != lane.nav_position_state_hash
            ):
                raise RuntimeError(
                    "Meta Wheel child positionStateHash differs from NAV"
                )
        seen_lots: set[int] = set()
        for lot in snapshot.assignment_lots:
            lot.validate()
            if lot.lot_id in seen_lots:
                raise RuntimeError("Meta Wheel assignment lot is duplicated")
            seen_lots.add(lot.lot_id)
            if lot.origin_csp_lane.lower() not in csp_addresses:
                raise RuntimeError("Meta Wheel lot origin is not a dedicated CSP lane")
        seen_tranches: set[int] = set()
        for tranche in snapshot.pending_csp_tranches:
            tranche.validate()
            if tranche.tranche_id in seen_tranches:
                raise RuntimeError("Meta Wheel pending CSP tranche is duplicated")
            seen_tranches.add(tranche.tranche_id)
        if (
            sum(tranche.pending_usdc for tranche in snapshot.pending_csp_tranches)
            != snapshot.pending_csp_usdc
        ):
            raise RuntimeError(
                "Meta Wheel pending CSP tranche accounting is incoherent"
            )

    @staticmethod
    def _action(
        snapshot: WheelSnapshot,
        lane: LaneSnapshot,
        kind: ActionKind,
        **values: object,
    ) -> WheelAction:
        return WheelAction(
            kind=kind,
            chain_id=snapshot.chain_id,
            parent=snapshot.parent,
            lane=lane.address,
            tranche_id=lane.tranche_id,
            transition_nonce=lane.transition_nonce,
            child_position_id=lane.child_position_id,
            **values,
        )

    def plan(
        self, snapshot: WheelSnapshot, quotes: Sequence[WheelQuote]
    ) -> tuple[WheelAction, ...]:
        self._validate_snapshot(snapshot)
        actions: list[WheelAction] = []

        # Settlements and terminal handoffs remain live so risk can decrease.
        for lane in snapshot.csp_lanes:
            if lane.phase == LanePhase.CSP_OPEN and lane.expiry <= snapshot.timestamp:
                actions.append(self._action(snapshot, lane, ActionKind.SETTLE_CSP))
            elif lane.phase == LanePhase.CSP_SETTLING:
                actions.append(
                    self._action(snapshot, lane, ActionKind.HANDOFF_ASSIGNMENT)
                )
        for lane in snapshot.call_lanes:
            if lane.phase == LanePhase.CALL_OPEN and lane.expiry <= snapshot.timestamp:
                actions.append(self._action(snapshot, lane, ActionKind.SETTLE_CALL))
            elif lane.phase == LanePhase.CALL_SETTLING:
                actions.append(
                    self._action(snapshot, lane, ActionKind.HANDOFF_CALL_AWAY)
                )

        reserve_gap = max(
            snapshot.pending_redemption_usdc - snapshot.reserved_redemption_usdc,
            0,
        )
        for tranche in sorted(
            snapshot.pending_csp_tranches, key=lambda item: item.tranche_id
        ):
            if reserve_gap == 0:
                break
            amount = min(reserve_gap, tranche.pending_usdc)
            actions.append(
                WheelAction(
                    kind=ActionKind.RESERVE_REDEMPTION,
                    chain_id=snapshot.chain_id,
                    parent=snapshot.parent,
                    lane=snapshot.coordinator,
                    tranche_id=tranche.tranche_id,
                    transition_nonce=tranche.state_nonce,
                    child_position_id=0,
                    amount=amount,
                )
            )
            reserve_gap -= amount

        if (
            snapshot.paused
            or snapshot.parent_total_assets_usdc > self.policy.maximum_parent_aum
            or snapshot.pending_redemption_usdc > snapshot.reserved_redemption_usdc
        ):
            return tuple(actions)

        free_call_lanes = [
            lane for lane in snapshot.call_lanes if lane.phase == LanePhase.IDLE
        ]
        available_lots = sorted(
            (
                lot
                for lot in snapshot.assignment_lots
                if lot.status == LotStatus.AVAILABLE and lot.remaining_weth > 0
            ),
            key=lambda lot: (lot.literal_assignment_strike8, lot.lot_id),
        )
        for assignment in available_lots:
            if not free_call_lanes:
                break
            selected = select_call_quote(quotes, snapshot, self.policy, assignment)
            if selected is None:
                # Do not starve a lower-floor lot that may have an executable quote.
                continue
            quote, floor = selected
            lane = free_call_lanes.pop(0)
            actions.append(
                WheelAction(
                    kind=ActionKind.OPEN_CALL,
                    chain_id=snapshot.chain_id,
                    parent=snapshot.parent,
                    lane=lane.address,
                    tranche_id=assignment.tranche_id,
                    transition_nonce=assignment.tranche_state_nonce,
                    child_position_id=0,
                    amount=assignment.remaining_weth,
                    lot_ids=(assignment.lot_id,),
                    quote_id=quote.quote_id,
                    strike8=quote.strike8,
                    required_floor8=floor,
                    open_data=quote.open_data,
                )
            )

        free_csp_lanes = [
            lane for lane in snapshot.csp_lanes if lane.phase == LanePhase.IDLE
        ]
        quote = select_csp_quote(quotes, snapshot, self.policy)
        available_pending_csp = snapshot.pending_csp_usdc
        for tranche in sorted(
            snapshot.pending_csp_tranches, key=lambda item: item.tranche_id
        ):
            if not free_csp_lanes:
                break
            if tranche.pending_usdc > self.policy.maximum_usdc_per_csp_lane:
                actions.append(
                    WheelAction(
                        kind=ActionKind.SPLIT_PENDING_CSP,
                        chain_id=snapshot.chain_id,
                        parent=snapshot.parent,
                        lane=snapshot.coordinator,
                        tranche_id=tranche.tranche_id,
                        transition_nonce=tranche.state_nonce,
                        child_position_id=0,
                        amount=self.policy.maximum_usdc_per_csp_lane,
                    )
                )
                continue
            if (
                quote is None
                or tranche.pending_usdc > available_pending_csp
                or quote.maximum_collateral < tranche.pending_usdc
            ):
                # No quote/no capacity leaves the tranche in the pending queue.
                continue
            lane = free_csp_lanes.pop(0)
            amount = tranche.pending_usdc
            actions.append(
                WheelAction(
                    kind=ActionKind.OPEN_CSP,
                    chain_id=snapshot.chain_id,
                    parent=snapshot.parent,
                    lane=lane.address,
                    tranche_id=tranche.tranche_id,
                    transition_nonce=tranche.state_nonce,
                    child_position_id=0,
                    amount=amount,
                    quote_id=quote.quote_id,
                    strike8=quote.strike8,
                    open_data=quote.open_data,
                )
            )
            available_pending_csp -= amount

        protected = max(
            snapshot.idle_usdc * self.policy.parent_liquid_reserve_bps // BPS,
            max(
                snapshot.pending_redemption_usdc - snapshot.reserved_redemption_usdc,
                0,
            ),
        )
        allocatable_parent_usdc = min(
            snapshot.idle_usdc * self.policy.csp_target_utilization_bps // BPS,
            max(snapshot.idle_usdc - protected, 0),
        )
        occupied_or_pending = sum(
            lane.phase not in {LanePhase.IDLE, LanePhase.PAUSED}
            for lane in snapshot.csp_lanes
        ) + len(snapshot.pending_csp_tranches)
        if (
            allocatable_parent_usdc > 0
            and occupied_or_pending < self.policy.maximum_csp_lanes
        ):
            actions.append(
                WheelAction(
                    kind=ActionKind.QUEUE_CSP_USDC,
                    chain_id=snapshot.chain_id,
                    parent=snapshot.parent,
                    lane=snapshot.coordinator,
                    tranche_id=0,
                    transition_nonce=snapshot.fund_flow_nonce,
                    child_position_id=0,
                    amount=min(
                        allocatable_parent_usdc,
                        self.policy.maximum_usdc_per_csp_lane,
                    ),
                )
            )
        return tuple(actions)


class MetaWheelAllocator:
    def __init__(
        self,
        *,
        policy_path: str | Path,
        approved_policy_hash: str,
        chain: MetaWheelChainPort,
        journal: ActionJournal,
        confirmations: int = 2,
    ) -> None:
        self.policy_path = Path(policy_path)
        self.approved_policy_hash = approved_policy_hash
        self.chain = chain
        self.journal = journal
        self.confirmations = max(confirmations, 2)

    def _policy(self) -> MetaWheelPolicy:
        return load_meta_wheel_policy(
            self.policy_path, approved_hash=self.approved_policy_hash
        )

    def _already_final(self, action: WheelAction) -> bool:
        existing = self.journal.get(action.key)
        if existing is None:
            return False
        status, tx_hash = existing
        if status == "confirmed" and tx_hash:
            receipt = self.chain.receipt(tx_hash, self.confirmations)
            if receipt and receipt.succeeded and receipt.canonical:
                return True
            self.journal.record(action.key, "orphaned", tx_hash)
        elif status == "submitted" and tx_hash:
            receipt = self.chain.receipt(tx_hash, self.confirmations)
            if receipt is None:
                # Unknown/dropped remains non-final and may be reconsidered only
                # after a fresh authoritative snapshot produces the same nonce.
                self.journal.record(action.key, "dropped", tx_hash)
            elif receipt.succeeded and receipt.canonical:
                reconciliation = self.chain.reconcile(action, receipt)
                if not reconciliation.valid:
                    self.journal.record(action.key, "reconciliation_failed", tx_hash)
                    raise RuntimeError(
                        "Meta Wheel action balance reconciliation failed"
                    )
                self.journal.record(action.key, "confirmed", tx_hash)
                return True
            else:
                self.journal.record(action.key, "orphaned", tx_hash)
        return False

    def _execute(self, action: WheelAction) -> None:
        if self._already_final(action):
            return
        # Reload the file and authoritative state immediately before submission.
        policy = self._policy()
        fresh = self.chain.read_snapshot(policy)
        candidates = MetaWheelPlanner(policy).plan(fresh, self.chain.list_quotes(fresh))
        candidate = next((item for item in candidates if item.key == action.key), None)
        if candidate != action:
            raise RuntimeError("Meta Wheel action changed during preflight")
        managed_request = (
            None
            if action.kind == ActionKind.QUEUE_CSP_USDC
            else managed_operation_for_action(action)
        )
        submitted = self.chain.submit(action, policy, managed_request)
        self.journal.record(action.key, "submitted", submitted.tx_hash)
        receipt = self.chain.receipt(submitted.tx_hash, self.confirmations)
        if receipt is None:
            raise RuntimeError("Meta Wheel transaction dropped or is not confirmed")
        if not receipt.succeeded or not receipt.canonical:
            self.journal.record(action.key, "orphaned", submitted.tx_hash)
            raise RuntimeError("Meta Wheel transaction receipt is not canonical")
        reconciliation = self.chain.reconcile(action, receipt)
        if not reconciliation.valid:
            self.journal.record(action.key, "reconciliation_failed", submitted.tx_hash)
            raise RuntimeError("Meta Wheel action balance reconciliation failed")
        self.journal.record(action.key, "confirmed", submitted.tx_hash)

    def run_once(self) -> tuple[WheelAction, ...]:
        policy = self._policy()
        snapshot = self.chain.read_snapshot(policy)
        quotes = self.chain.list_quotes(snapshot)
        actions = MetaWheelPlanner(policy).plan(snapshot, quotes)
        for action in actions:
            self._execute(action)
        return actions

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning("Meta Wheel allocator cycle failed closed", exc_info=True)
            time.sleep(config.META_WHEEL_ALLOCATOR_INTERVAL_SECONDS)


_chain_port_factory: Callable[[], MetaWheelChainPort] | None = None


def install_chain_port_factory(factory: Callable[[], MetaWheelChainPort]) -> None:
    """Install the B1N-414/415 ABI adapter without coupling standalone workers."""
    global _chain_port_factory
    _chain_port_factory = factory


def start() -> threading.Thread | None:
    if not config.META_WHEEL_ALLOCATOR_ENABLED:
        log.info("Meta Wheel allocator disabled")
        return None
    if _chain_port_factory is None:
        raise RuntimeError("Meta Wheel contract ABI adapter is not installed")
    allocator = MetaWheelAllocator(
        policy_path=config.META_WHEEL_ALLOCATOR_POLICY_PATH,
        approved_policy_hash=config.META_WHEEL_APPROVED_POLICY_SHA256 or "",
        chain=_chain_port_factory(),
        journal=SqliteActionJournal(config.META_WHEEL_ACTION_JOURNAL_PATH),
        confirmations=config.META_WHEEL_ALLOCATOR_CONFIRMATIONS,
    )
    thread = threading.Thread(
        target=allocator.run_forever,
        name="meta-wheel-allocator",
        daemon=True,
    )
    thread.start()
    return thread
