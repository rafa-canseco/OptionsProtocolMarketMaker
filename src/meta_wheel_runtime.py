"""Authoritative Base Sepolia reads and reconciliation for the Meta Wheel."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import logging
import math
from typing import Any

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.logs import DISCARD

from src import api_client, config
from src.covered_call_allocator import (
    call_collateral_for_option_amount,
    minimum_bid_price_for_net_premium,
    option_amount_for_call_collateral,
)
from src.fund_allocator import (
    incremental_quote_premium,
    option_amount_for_collateral,
    required_collateral,
    validate_market_snapshot,
)
from src.meta_wheel_allocator import (
    ActionKind,
    AssignmentLot,
    LaneKind,
    LanePhase,
    LaneSnapshot,
    LotStatus,
    PendingCspTranche,
    Reconciliation,
    WheelAction,
    WheelQuote,
    WheelSnapshot,
)
from src.meta_wheel_chain import (
    BASE_SEPOLIA_CHAIN_ID,
    Web3MetaWheelChainPort,
    WheelManifestGate,
    load_runtime_gate_and_signers,
)
from src.meta_wheel_policy import BPS, MetaWheelPolicy
from src.pricer import bs_delta, validate_iv
from src.signer import build_domain, sign_quote


log = logging.getLogger(__name__)

WAD = 10**18
USDC_PER_WETH_SCALE = 10**20
EIP1967_IMPLEMENTATION_SLOT = int(
    "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)


def _function(
    name: str,
    outputs: list[dict[str, Any]],
    inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": inputs or [],
        "outputs": outputs,
    }


def _scalar(name: str, output_type: str, inputs=None) -> dict[str, Any]:
    return _function(name, [{"name": "", "type": output_type}], inputs)


_SUMMARY_COMPONENTS = [
    {"name": "stateNonce", "type": "uint64"},
    {"name": "trancheCount", "type": "uint256"},
    {"name": "assignmentLotCount", "type": "uint256"},
    {"name": "pendingCspUsdc", "type": "uint256"},
    {"name": "reservedRedemptionUsdc", "type": "uint256"},
    {"name": "reservedPrincipalUsdc", "type": "uint256"},
    {"name": "transitionWeth", "type": "uint256"},
    {"name": "accountedUsdc", "type": "uint256"},
    {"name": "accountedWeth", "type": "uint256"},
]
_TRANCHE_COMPONENTS = [
    {"name": "leg", "type": "uint8"},
    {"name": "childLane", "type": "address"},
    {"name": "stateNonce", "type": "uint64"},
    {"name": "expiry", "type": "uint64"},
    {"name": "principalUsdc", "type": "uint256"},
    {"name": "pendingUsdc", "type": "uint256"},
    {"name": "childShares", "type": "uint256"},
    {"name": "childPositionId", "type": "uint256"},
    {"name": "assignmentLotId", "type": "uint256"},
    {"name": "childPositionHash", "type": "bytes32"},
    {"name": "stateHash", "type": "bytes32"},
]
_LOT_COMPONENTS = [
    {"name": "originCspLane", "type": "address"},
    {"name": "createdAt", "type": "uint64"},
    {"name": "status", "type": "uint8"},
    {"name": "trancheId", "type": "uint256"},
    {"name": "originCspPositionId", "type": "uint256"},
    {"name": "wethReceived", "type": "uint256"},
    {"name": "remainingWeth", "type": "uint256"},
    {"name": "literalAssignmentStrike8", "type": "uint256"},
]
_NAV_COMPONENTS = [
    {"name": "grossAssets", "type": "uint256"},
    {"name": "liabilities", "type": "uint256"},
    {"name": "netAssets", "type": "uint256"},
    {"name": "liquidAccountingAssets", "type": "uint256"},
    {"name": "baseExitCost", "type": "uint256"},
    {"name": "snapshotBlock", "type": "uint64"},
    {"name": "validAfterBlock", "type": "uint64"},
    {"name": "validUntilBlock", "type": "uint64"},
    {"name": "reporterSetVersion", "type": "uint64"},
    {"name": "reportNonce", "type": "uint64"},
    {"name": "positionsHash", "type": "bytes32"},
    {"name": "reportHash", "type": "bytes32"},
    {"name": "signaturesHash", "type": "bytes32"},
    {"name": "fundFlowNonce", "type": "uint64"},
    {"name": "idleStateHash", "type": "bytes32"},
]
_FEE_COMPONENTS = [
    {"name": "managementFeeWad", "type": "uint64"},
    {"name": "performanceFeeBps", "type": "uint16"},
    {"name": "maxManagementFeeBps", "type": "uint16"},
    {"name": "maxPerformanceFeeBps", "type": "uint16"},
    {"name": "maxAccrualInterval", "type": "uint32"},
    {"name": "crystallizationPeriod", "type": "uint32"},
    {"name": "feeRecipient", "type": "address"},
]
_COMPONENT_STATE = [
    {"name": "valuator", "type": "address"},
    {"name": "interfaceVersion", "type": "uint64"},
    {"name": "nonce", "type": "uint64"},
    {"name": "positionStateHash", "type": "bytes32"},
    {"name": "active", "type": "bool"},
]

_COORDINATOR_ABI = [
    _function(
        "summary",
        [{"name": "state", "type": "tuple", "components": _SUMMARY_COMPONENTS}],
    ),
    _function(
        "tranche",
        [{"name": "", "type": "tuple", "components": _TRANCHE_COMPONENTS}],
        [{"name": "trancheId", "type": "uint256"}],
    ),
    _function(
        "assignmentLot",
        [{"name": "", "type": "tuple", "components": _LOT_COMPONENTS}],
        [{"name": "lotId", "type": "uint256"}],
    ),
    _scalar("registeredLaneCount", "uint256"),
    _function(
        "registeredLaneAt",
        [
            {"name": "lane", "type": "address"},
            {"name": "kind", "type": "uint8"},
            {"name": "active", "type": "bool"},
        ],
        [{"name": "index", "type": "uint256"}],
    ),
    _scalar("positionStateHash", "bytes32"),
    _scalar("policyHash", "bytes32"),
    _scalar("floorBufferUsd8", "uint256"),
    _function(
        "laneCaps",
        [
            {"name": "maxCspLanes", "type": "uint16"},
            {"name": "maxCoveredCallLanes", "type": "uint16"},
        ],
    ),
]

_LANE_ABI = [
    _scalar("coordinator", "address"),
    _scalar("adapter", "address"),
    _scalar("laneKind", "uint8"),
    _scalar("laneState", "uint8"),
    _scalar("stateNonce", "uint64"),
    _scalar("childShares", "uint256"),
    _scalar("activeTrancheId", "uint256"),
    _scalar("activePositionId", "uint256"),
    _scalar("maxAssets", "uint256"),
    _scalar("executionStateHash", "bytes32"),
    _scalar("positionStateHash", "bytes32"),
    _scalar("consumedLotId", "uint256"),
]
_CSP_ACCOUNTING_ABI = [
    _function(
        "accountingState",
        [
            {"name": "accountedUsdc", "type": "uint256"},
            {"name": "accountedWeth", "type": "uint256"},
        ],
    )
]
_CALL_ACCOUNTING_ABI = [
    _function(
        "accountingState",
        [
            {"name": "accountedUsdc", "type": "uint256"},
            {"name": "accountedWeth", "type": "uint256"},
            {"name": "literalFloor8", "type": "uint256"},
            {"name": "requiredFloorValue8", "type": "uint256"},
        ],
    )
]

_PARENT_ABI = [
    _function(
        "activeNavWindow",
        [{"name": "nav", "type": "tuple", "components": _NAV_COMPONENTS}],
    ),
    _scalar("totalAssets", "uint256"),
    _scalar("accountedIdleAssets", "uint256"),
    _scalar("fundFlowNonce", "uint64"),
    _scalar(
        "convertToAssets",
        "uint256",
        [{"name": "shares", "type": "uint256"}],
    ),
]
_FLOW_ABI = [
    _scalar("totalPendingShares", "uint256"),
    _scalar("hasActiveProcessing", "bool"),
]
_ACCOUNTING_ABI = [
    _function(
        "feeConfig", [{"name": "", "type": "tuple", "components": _FEE_COMPONENTS}]
    ),
    _function(
        "componentState",
        [{"name": "", "type": "tuple", "components": _COMPONENT_STATE}],
        [{"name": "componentId", "type": "bytes32"}],
    ),
]
_STRATEGY_READ_ABI = [
    _scalar("positionsHash", "bytes32"),
    _scalar("positionNonce", "uint64", [{"name": "adapter", "type": "address"}]),
    _function(
        "strategyConfig",
        [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "active", "type": "bool"},
                    {"name": "maxAllocationBps", "type": "uint16"},
                    {"name": "maxLossBps", "type": "uint16"},
                    {"name": "cooldown", "type": "uint32"},
                    {"name": "interfaceVersion", "type": "uint64"},
                    {"name": "valuator", "type": "address"},
                    {"name": "absoluteCap", "type": "uint256"},
                ],
            }
        ],
        [{"name": "adapter", "type": "address"}],
    ),
]
_SETTLER_ABI = [
    _scalar("protocolFeeBps", "uint256"),
    _scalar("treasury", "address"),
    _scalar("makerNonce", "uint256", [{"name": "", "type": "address"}]),
    _scalar("whitelistedMMs", "bool", [{"name": "", "type": "address"}]),
    _function(
        "hashQuote",
        [{"name": "", "type": "bytes32"}],
        [
            {
                "name": "quote",
                "type": "tuple",
                "components": [
                    {"name": "oToken", "type": "address"},
                    {"name": "bidPrice", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "quoteId", "type": "uint256"},
                    {"name": "maxAmount", "type": "uint256"},
                    {"name": "makerNonce", "type": "uint256"},
                ],
            }
        ],
    ),
    _function(
        "getQuoteState",
        [
            {"name": "filledAmount", "type": "uint256"},
            {"name": "isCancelled", "type": "bool"},
        ],
        [
            {"name": "mm", "type": "address"},
            {"name": "quoteHash", "type": "bytes32"},
        ],
    ),
]
_ORACLE_ABI = [_scalar("getPrice", "uint256", [{"name": "asset", "type": "address"}])]
_ERC20_ABI = [_scalar("balanceOf", "uint256", [{"name": "account", "type": "address"}])]
_ADAPTER_ABI = [
    _scalar("fund", "address"),
    _scalar("strategyManager", "address"),
    _scalar("accountingAsset", "address"),
    _scalar("weth", "address"),
    _scalar("usdc", "address"),
]
_OTOKEN_ABI = [
    _scalar("isPut", "bool"),
    _scalar("underlying", "address"),
    _scalar("strikeAsset", "address"),
    _scalar("collateralAsset", "address"),
    _scalar("expiry", "uint256"),
    _scalar("strikePrice", "uint256"),
]


def _event(name: str, inputs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": field, "type": kind, "indexed": indexed}
            for field, kind, indexed in inputs
        ],
    }


_COORDINATOR_EVENTS = [
    _event("WheelAllocationPauseSet", [("paused", "bool", False)]),
    _event(
        "WheelTrancheQueued",
        [
            ("trancheId", "uint256", True),
            ("allocationId", "bytes32", True),
            ("usdcAmount", "uint256", False),
            ("pendingCspUsdc", "uint256", False),
            ("stateHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelSiblingTrancheQueued",
        [
            ("parentTrancheId", "uint256", True),
            ("siblingTrancheId", "uint256", True),
            ("usdcAmount", "uint256", False),
            ("principalUsdc", "uint256", False),
            ("stateHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelTrancheOpened",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("leg", "uint8", False),
            ("childPositionId", "uint256", False),
            ("childShares", "uint256", False),
            ("expiry", "uint64", False),
            ("childPositionHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelTrancheSettlementAdvanced",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("leg", "uint8", False),
            ("settlementKind", "uint8", False),
            ("childPositionHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelChildHandoff",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("transitionHash", "bytes32", True),
            ("settlementKind", "uint8", False),
            ("childSharesBurned", "uint256", False),
            ("usdcAmount", "uint256", False),
            ("wethAmount", "uint256", False),
        ],
    ),
    _event(
        "WheelAssignmentLotCreated",
        [
            ("lotId", "uint256", True),
            ("trancheId", "uint256", True),
            ("originCspLane", "address", True),
            ("originCspPositionId", "uint256", False),
            ("wethReceived", "uint256", False),
            ("literalAssignmentStrike8", "uint256", False),
        ],
    ),
    _event(
        "WheelCoveredCallFloorEnforced",
        [
            ("trancheId", "uint256", True),
            ("lotId", "uint256", True),
            ("lane", "address", True),
            ("literalAssignmentStrike8", "uint256", False),
            ("executionCostBuffer8", "uint256", False),
            ("requiredFloor8", "uint256", False),
            ("callStrike8", "uint256", False),
        ],
    ),
    _event(
        "WheelRedemptionUsdcReserved",
        [
            ("trancheId", "uint256", True),
            ("amount", "uint256", False),
            ("principalReserved", "uint256", False),
            ("remainingTrancheUsdc", "uint256", False),
            ("remainingTranchePrincipal", "uint256", False),
        ],
    ),
    _event(
        "WheelRedemptionUsdcReleased",
        [
            ("trancheId", "uint256", True),
            ("amount", "uint256", False),
            ("principalRestored", "uint256", False),
            ("stateHash", "bytes32", False),
        ],
    ),
]
_LANE_EVENTS = [
    _event("LaneAllocationPauseSet", [("paused", "bool", False)]),
    _event(
        "CspOpened",
        [
            ("trancheId", "uint256", True),
            ("positionId", "uint256", True),
            ("usdcAmount", "uint256", False),
            ("literalAssignmentStrike8", "uint256", False),
            ("expiry", "uint64", False),
            ("childShares", "uint256", False),
            ("positionHash", "bytes32", False),
        ],
    ),
    _event(
        "CspSettlementAdvanced",
        [
            ("trancheId", "uint256", True),
            ("positionId", "uint256", True),
            ("settlementKind", "uint8", False),
            ("laneState", "uint8", False),
            ("observedUsdc", "uint256", False),
            ("observedWeth", "uint256", False),
            ("positionHash", "bytes32", False),
        ],
    ),
    _event(
        "CspBasketHandedOff",
        [
            ("trancheId", "uint256", True),
            ("transitionHash", "bytes32", True),
            ("receiver", "address", True),
            ("childSharesBurned", "uint256", False),
            ("usdcAmount", "uint256", False),
            ("wethAmount", "uint256", False),
        ],
    ),
    _event(
        "CoveredCallOpened",
        [
            ("trancheId", "uint256", True),
            ("lotId", "uint256", True),
            ("positionId", "uint256", True),
            ("wethAmount", "uint256", False),
            ("collateral", "uint256", False),
            ("literalAssignmentStrike8", "uint256", False),
            ("requiredFloor8", "uint256", False),
            ("callStrike8", "uint256", False),
            ("expiry", "uint64", False),
            ("childShares", "uint256", False),
            ("positionHash", "bytes32", False),
        ],
    ),
    _event(
        "CoveredCallSettlementAdvanced",
        [
            ("trancheId", "uint256", True),
            ("lotId", "uint256", True),
            ("positionId", "uint256", True),
            ("settlementKind", "uint8", False),
            ("laneState", "uint8", False),
            ("observedUsdc", "uint256", False),
            ("observedWeth", "uint256", False),
            ("positionHash", "bytes32", False),
        ],
    ),
    _event(
        "CoveredCallBasketHandedOff",
        [
            ("trancheId", "uint256", True),
            ("lotId", "uint256", True),
            ("transitionHash", "bytes32", True),
            ("receiver", "address", False),
            ("childSharesBurned", "uint256", False),
            ("usdcAmount", "uint256", False),
            ("wethAmount", "uint256", False),
        ],
    ),
    _event(
        "WheelPremiumAccrued",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("childPositionId", "uint256", True),
            ("grossPremiumAssets", "uint256", False),
            ("protocolFeeAssets", "uint256", False),
            ("netPremiumAssets", "uint256", False),
        ],
    ),
]


def _hex(value: Any) -> str:
    if isinstance(value, str):
        if not value.startswith("0x"):
            raise ValueError("expected hex string")
        return value
    return Web3.to_hex(value)


def _hash(value: Any) -> str:
    return _hex(value).removeprefix("0x").lower()


def _addresses(raw: str | None, label: str) -> frozenset[str]:
    values = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not values:
        raise RuntimeError(f"{label} is required")
    try:
        normalized = [Web3.to_checksum_address(value) for value in values]
    except ValueError:
        raise RuntimeError(f"{label} contains an invalid address") from None
    if len({value.lower() for value in normalized}) != len(normalized):
        raise RuntimeError(f"{label} contains duplicate addresses")
    return frozenset(normalized)


def _quote_created_at(raw: dict[str, Any]) -> int:
    value = raw.get("created_at")
    if isinstance(value, bool):
        raise ValueError("boolean quote timestamp")
    if isinstance(value, (int, float)):
        timestamp = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            timestamp = int(stripped)
        else:
            timestamp = int(
                datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                .astimezone(UTC)
                .timestamp()
            )
    else:
        raise ValueError("missing quote timestamp")
    if timestamp <= 0:
        raise ValueError("invalid quote timestamp")
    return timestamp


def _quote_identity(raw: dict[str, Any]) -> tuple[int, int]:
    return int(raw["maker_nonce"]), int(raw["quote_id"])


def _quote_core(raw: dict[str, Any]) -> tuple[str, int, int, int, int, int]:
    return (
        Web3.to_checksum_address(raw["otoken_address"]),
        int(raw["bid_price"]),
        int(raw["deadline"]),
        int(raw["quote_id"]),
        int(raw["max_amount"]),
        int(raw["maker_nonce"]),
    )


def _ambiguous_quote_identities(
    raw_quotes: list[dict[str, Any]],
) -> frozenset[tuple[int, int]]:
    observed: dict[tuple[int, int], tuple[str, int, int, int, int, int]] = {}
    ambiguous: set[tuple[int, int]] = set()
    for raw in raw_quotes:
        try:
            identity = _quote_identity(raw)
            core = _quote_core(raw)
        except (KeyError, TypeError, ValueError):
            continue
        previous = observed.setdefault(identity, core)
        if previous != core:
            ambiguous.add(identity)
    return frozenset(ambiguous)


@dataclass(frozen=True)
class DecodedWheelEvent:
    name: str
    address: str
    args: dict[str, Any]


@dataclass(frozen=True)
class WheelObservedState:
    parent_idle_usdc: int
    coordinator_accounted_usdc: int
    coordinator_accounted_weth: int
    coordinator_transition_weth: int
    coordinator_raw_usdc: int
    coordinator_raw_weth: int
    pending_csp_usdc: int
    reserved_redemption_usdc: int
    reserved_principal_usdc: int
    tranche_principal_usdc: int = 0
    tranche_pending_usdc: int = 0
    sibling_principal_usdc: int = 0
    sibling_pending_usdc: int = 0
    lane_child_shares: int = 0
    lane_accounted_usdc: int = 0
    lane_accounted_weth: int = 0
    lane_raw_usdc: int = 0
    lane_raw_weth: int = 0
    lane_execution_state_hash: str = ""
    lane_position_state_hash: str = ""


class BaseSepoliaMetaWheelRuntime:
    """Read one coherent confirmed block and reconcile only canonical receipts."""

    def __init__(self, manifest: WheelManifestGate, *, w3: Any | None = None) -> None:
        self.manifest = manifest
        self.w3 = w3 or Web3(Web3.HTTPProvider(config.RPC_URL))
        if int(self.w3.eth.chain_id) != BASE_SEPOLIA_CHAIN_ID:
            raise RuntimeError("Meta Wheel runtime RPC is not Base Sepolia")
        self._verify_manifest_chain()
        self.parent = self.w3.eth.contract(address=manifest.parent, abi=_PARENT_ABI)
        self.coordinator = self.w3.eth.contract(
            address=manifest.coordinator,
            abi=[*_COORDINATOR_ABI, *_COORDINATOR_EVENTS],
        )
        self.strategy = self.w3.eth.contract(
            address=manifest.strategy_manager, abi=_STRATEGY_READ_ABI
        )
        self.accounting = self.w3.eth.contract(
            address=manifest.fund_accounting, abi=_ACCOUNTING_ABI
        )
        self.flow = self.w3.eth.contract(
            address=manifest.fund_flow_manager, abi=_FLOW_ABI
        )
        self.settler = self.w3.eth.contract(
            address=manifest.batch_settler, abi=_SETTLER_ABI
        )
        self.oracle = self.w3.eth.contract(address=manifest.oracle, abi=_ORACLE_ABI)
        self.usdc = self.w3.eth.contract(address=manifest.usdc, abi=_ERC20_ABI)
        self.weth = self.w3.eth.contract(address=manifest.weth, abi=_ERC20_ABI)
        self.expected_csp_lanes = _addresses(
            config.META_WHEEL_CSP_LANE_ADDRESSES,
            "META_WHEEL_CSP_LANE_ADDRESSES",
        )
        self.expected_call_lanes = _addresses(
            config.META_WHEEL_CALL_LANE_ADDRESSES,
            "META_WHEEL_CALL_LANE_ADDRESSES",
        )
        self._pause_cursor = manifest.deployment_start_block - 1
        self._paused = False
        self._lane_pause_cursors: dict[str, int] = {}
        self._lane_paused: dict[str, bool] = {}
        self._policy_id: str | None = None
        self._minimum_net_premium_bps: int | None = None
        self._csp_minimum_net_premium_bps: int | None = None
        self._market_maximum_age: int | None = None
        for address in (
            manifest.parent,
            manifest.strategy_manager,
            manifest.coordinator,
            manifest.fund_accounting,
            manifest.fund_flow_manager,
            manifest.valuator,
            manifest.batch_settler,
            manifest.oracle,
            manifest.usdc,
            manifest.weth,
        ):
            if not self.w3.eth.get_code(address):
                raise RuntimeError(f"Meta Wheel runtime address has no code: {address}")

    def _verify_manifest_chain(self) -> None:
        safe = int(self.w3.eth.block_number) - max(
            config.META_WHEEL_ALLOCATOR_CONFIRMATIONS, 2
        )
        if safe < self.manifest.deployment_end_block:
            raise RuntimeError("Meta Wheel manifest is not confirmed on this RPC")
        for expected in self.manifest.canonical_receipts:
            try:
                receipt = self.w3.eth.get_transaction_receipt(expected.transaction_hash)
                block = self.w3.eth.get_block(expected.block_number)
            except Exception:
                raise RuntimeError(
                    "Meta Wheel canonical deployment receipt is unavailable"
                ) from None
            if (
                int(receipt.status) != 1
                or int(receipt.blockNumber) != expected.block_number
                or _hex(receipt.blockHash).lower() != expected.block_hash
                or _hex(block.hash).lower() != expected.block_hash
            ):
                raise RuntimeError(
                    "Meta Wheel canonical deployment receipt is not canonical"
                )
        for binding in self.manifest.proxy_bindings:
            try:
                raw = self.w3.eth.get_storage_at(
                    binding.proxy,
                    EIP1967_IMPLEMENTATION_SLOT,
                    block_identifier=safe,
                )
                implementation = Web3.to_checksum_address(bytes(raw)[-20:])
            except Exception:
                raise RuntimeError(
                    f"Meta Wheel proxy binding is unreadable: {binding.label}"
                ) from None
            if implementation != binding.implementation:
                raise RuntimeError(
                    f"Meta Wheel proxy implementation changed: {binding.label}"
                )
        for binding in self.manifest.code_bindings:
            code = bytes(self.w3.eth.get_code(binding.address, block_identifier=safe))
            if not code or _hex(Web3.keccak(code)).lower() != binding.codehash:
                raise RuntimeError(f"Meta Wheel codehash changed: {binding.label}")

    def _call(self, function: Any, block: int) -> Any:
        return function.call(block_identifier=block)

    def _lane_contract(self, address: str):
        return self.w3.eth.contract(address=address, abi=[*_LANE_ABI, *_LANE_EVENTS])

    def _adapter_contract(self, address: str):
        return self.w3.eth.contract(address=address, abi=_ADAPTER_ABI)

    def _paused_at(self, block: int) -> bool:
        if block <= self._pause_cursor:
            return self._paused
        logs = self.coordinator.events.WheelAllocationPauseSet().get_logs(
            from_block=self._pause_cursor + 1,
            to_block=block,
        )
        for event in logs:
            self._paused = bool(event["args"]["paused"])
        self._pause_cursor = block
        return self._paused

    def _lane_allocation_paused_at(self, lane: Any, block: int) -> bool:
        address = Web3.to_checksum_address(lane.address)
        cursor = self._lane_pause_cursors.get(
            address, self.manifest.deployment_start_block - 1
        )
        paused = self._lane_paused.get(address, False)
        if block <= cursor:
            return paused
        logs = lane.events.LaneAllocationPauseSet().get_logs(
            from_block=cursor + 1,
            to_block=block,
        )
        for event in logs:
            paused = bool(event["args"]["paused"])
        self._lane_pause_cursors[address] = block
        self._lane_paused[address] = paused
        return paused

    def _lane_snapshot(
        self,
        address: str,
        kind: LaneKind,
        active: bool,
        block: int,
        tranches: dict[int, tuple[Any, ...]],
    ) -> tuple[LaneSnapshot, int, bool]:
        lane = self._lane_contract(address)
        adapter_address = Web3.to_checksum_address(
            self._call(lane.functions.adapter(), block)
        )
        expected_kind = 1 if kind == LaneKind.CSP else 2
        if (
            Web3.to_checksum_address(self._call(lane.functions.coordinator(), block))
            != self.manifest.coordinator
            or int(self._call(lane.functions.laneKind(), block)) != expected_kind
            or not self.w3.eth.get_code(adapter_address, block_identifier=block)
        ):
            raise RuntimeError("Meta Wheel lane is not dedicated to its coordinator")
        adapter = self._adapter_contract(adapter_address)
        expected_asset = (
            self.manifest.usdc if kind == LaneKind.CSP else self.manifest.weth
        )
        asset_getter = adapter.functions.accountingAsset()
        if (
            Web3.to_checksum_address(self._call(adapter.functions.fund(), block))
            != address
            or Web3.to_checksum_address(
                self._call(adapter.functions.strategyManager(), block)
            )
            != address
            or Web3.to_checksum_address(self._call(asset_getter, block))
            != expected_asset
        ):
            raise RuntimeError("Meta Wheel child adapter boundary is crossed")
        token_getter = (
            adapter.functions.weth()
            if kind == LaneKind.CSP
            else adapter.functions.usdc()
        )
        expected_token = (
            self.manifest.weth if kind == LaneKind.CSP else self.manifest.usdc
        )
        if Web3.to_checksum_address(self._call(token_getter, block)) != expected_token:
            raise RuntimeError("Meta Wheel child adapter asset boundary is crossed")

        state = int(self._call(lane.functions.laneState(), block))
        tranche_id = int(self._call(lane.functions.activeTrancheId(), block))
        position_id = int(self._call(lane.functions.activePositionId(), block))
        nonce = int(self._call(lane.functions.stateNonce(), block))
        shares = int(self._call(lane.functions.childShares(), block))
        execution_hash = _hex(self._call(lane.functions.executionStateHash(), block))
        position_hash = _hex(self._call(lane.functions.positionStateHash(), block))
        max_assets = int(self._call(lane.functions.maxAssets(), block))
        allocation_paused = self._lane_allocation_paused_at(lane, block)
        tranche = tranches.get(tranche_id)
        if tranche_id and tranche is None:
            raise RuntimeError("Meta Wheel lane references an unknown tranche")
        expiry = int(tranche[3]) if tranche else 0
        child_hash = _hex(tranche[9]) if tranche else ""
        leg = int(tranche[0]) if tranche else 0
        if kind == LaneKind.CSP and leg == 2 and state == 1:
            phase = LanePhase.CSP_OPEN
        elif kind == LaneKind.CSP and leg == 3 and state == 2:
            phase = LanePhase.CSP_SETTLING
        elif kind == LaneKind.CSP and leg == 3 and state == 3:
            phase = LanePhase.CSP_READY_FOR_HANDOFF
        elif kind == LaneKind.COVERED_CALL and leg == 5 and state == 1:
            phase = LanePhase.CALL_OPEN
        elif kind == LaneKind.COVERED_CALL and leg == 6 and state == 2:
            phase = LanePhase.CALL_SETTLING
        elif kind == LaneKind.COVERED_CALL and leg == 6 and state == 3:
            phase = LanePhase.CALL_READY_FOR_HANDOFF
        elif state == 0 and tranche_id == 0 and (not active or allocation_paused):
            phase = LanePhase.PAUSED
        elif state == 0 and tranche_id == 0:
            phase = LanePhase.IDLE
        else:
            raise RuntimeError("Meta Wheel lane/tranche lifecycle is inconsistent")
        lot_ids: tuple[int, ...] = ()
        if kind == LaneKind.COVERED_CALL:
            lot_id = int(self._call(lane.functions.consumedLotId(), block))
            lot_ids = (lot_id,) if lot_id else ()
        accounting_abi = (
            _CSP_ACCOUNTING_ABI if kind == LaneKind.CSP else _CALL_ACCOUNTING_ABI
        )
        accounting = self.w3.eth.contract(address=address, abi=accounting_abi)
        accounted = tuple(self._call(accounting.functions.accountingState(), block))
        raw_usdc = int(self._call(self.usdc.functions.balanceOf(address), block))
        raw_weth = int(self._call(self.weth.functions.balanceOf(address), block))
        balance_ok = int(accounted[0]) <= raw_usdc and int(accounted[1]) <= raw_weth
        return (
            LaneSnapshot(
                address=address,
                kind=kind,
                phase=phase,
                tranche_id=tranche_id,
                transition_nonce=nonce,
                child_position_id=position_id,
                amount=shares,
                expiry=expiry,
                execution_state_hash=execution_hash if tranche_id else "",
                tranche_child_execution_state_hash=child_hash,
                position_state_hash=position_hash if tranche_id else "",
                nav_position_state_hash=position_hash if tranche_id else "",
                lot_ids=lot_ids,
                adapter=adapter_address,
                dedicated_to_parent=True,
                active_options=int(state != 0 and position_id != 0),
                tranche_principal_usdc=int(tranche[4]) if tranche else 0,
                tranche_pending_usdc=int(tranche[5]) if tranche else 0,
                accounted_usdc=int(accounted[0]),
                accounted_weth=int(accounted[1]),
                raw_usdc=raw_usdc,
                raw_weth=raw_weth,
            ),
            max_assets,
            balance_ok,
        )

    def _bind_nav_observation(
        self,
        *,
        lanes: tuple[LaneSnapshot, ...],
        nav: tuple[Any, ...],
        component_id: bytes,
        safe_block: int,
    ) -> tuple[LaneSnapshot, ...]:
        fund_key = config.META_WHEEL_FUND_KEY
        if not fund_key:
            raise RuntimeError("META_WHEEL_FUND_KEY is required")
        snapshot_block = int(nav[5])
        try:
            observation = api_client.get_meta_wheel_nav_observation(
                fund_key, snapshot_block=snapshot_block
            )
            block = self.w3.eth.get_block(snapshot_block)
            expected_block_hash = _hex(block.hash).lower()
            coordinator_at_snapshot = _hex(
                self._call(
                    self.coordinator.functions.positionStateHash(), snapshot_block
                )
            ).lower()
            if (
                observation.get("fundKey") != fund_key
                or int(observation["chainId"]) != BASE_SEPOLIA_CHAIN_ID
                or Web3.to_checksum_address(observation["fundAddress"])
                != self.manifest.parent
                or Web3.to_checksum_address(observation["coordinator"])
                != self.manifest.coordinator
                or int(observation["reportNonce"]) != int(nav[9])
                or _hex(observation["componentId"]).lower()
                != _hex(component_id).lower()
                or _hex(observation["coordinatorPositionStateHash"]).lower()
                != coordinator_at_snapshot
                or int(observation["snapshotBlock"]) != snapshot_block
                or _hex(observation["snapshotBlockHash"]).lower() != expected_block_hash
                or not int(observation["validAfterBlock"])
                <= safe_block
                <= int(observation["validUntilBlock"])
                or not int(nav[6]) <= safe_block <= int(nav[7])
            ):
                raise RuntimeError("Meta Wheel NAV observation is incoherent")
            raw_lanes = observation["lanes"]
            if not isinstance(raw_lanes, list):
                raise RuntimeError("Meta Wheel NAV lanes are invalid")
            by_address: dict[str, dict[str, Any]] = {}
            for raw in raw_lanes:
                if not isinstance(raw, dict):
                    raise RuntimeError("Meta Wheel NAV lane is invalid")
                address = Web3.to_checksum_address(raw["lane"])
                if address in by_address:
                    raise RuntimeError("Meta Wheel NAV lane is duplicated")
                by_address[address] = raw
            active = {lane.address: lane for lane in lanes if lane.amount > 0}
            if set(by_address) != set(active):
                raise RuntimeError("Meta Wheel NAV lane set is incomplete")
            bound: list[LaneSnapshot] = []
            for lane in lanes:
                if lane.amount == 0:
                    bound.append(lane)
                    continue
                raw = by_address[lane.address]
                lane_contract = self._lane_contract(lane.address)
                position_hash = _hex(
                    self._call(
                        lane_contract.functions.positionStateHash(), snapshot_block
                    )
                ).lower()
                if (
                    int(raw["childShares"]) != lane.amount
                    or _hex(raw["positionStateHash"]).lower() != position_hash
                    or int(raw["snapshotBlock"]) != snapshot_block
                    or _hex(raw["snapshotBlockHash"]).lower() != expected_block_hash
                    or not int(raw["validAfterBlock"])
                    <= snapshot_block
                    <= int(raw["validUntilBlock"])
                ):
                    raise RuntimeError("Meta Wheel NAV lane evidence is incoherent")
                bound.append(replace(lane, nav_position_state_hash=position_hash))
            return tuple(bound)
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Meta Wheel NAV observation is unavailable") from None

    def read_snapshot(self, policy: MetaWheelPolicy) -> WheelSnapshot:
        if (
            policy.policy_hash != self.manifest.policy_hash
            or policy.protocol_gross_premium_fee_bps != self.manifest.premium_fee_bps
            or policy.parent_management_fee_bps
            != self.manifest.management_fee_wad * BPS // WAD
            or policy.parent_performance_fee_bps != self.manifest.performance_fee_bps
        ):
            raise RuntimeError("Meta Wheel runtime policy differs from final manifest")
        self._policy_id = policy.policy_id
        self._minimum_net_premium_bps = policy.minimum_net_premium_bps
        self._csp_minimum_net_premium_bps = policy.csp_minimum_net_premium_bps
        self._market_maximum_age = policy.market_maximum_age
        latest = int(self.w3.eth.block_number)
        confirmations = max(config.META_WHEEL_ALLOCATOR_CONFIRMATIONS, 2)
        safe = latest - confirmations
        if safe < self.manifest.deployment_end_block:
            raise RuntimeError("Meta Wheel safe block precedes final deployment")
        before = self.w3.eth.get_block(safe)
        summary = tuple(self._call(self.coordinator.functions.summary(), safe))
        tranche_count = int(summary[1])
        lot_count = int(summary[2])
        if tranche_count > 512 or lot_count > 512:
            raise RuntimeError("Meta Wheel discovery bound exceeded")
        tranches = {
            tranche_id: tuple(
                self._call(self.coordinator.functions.tranche(tranche_id), safe)
            )
            for tranche_id in range(1, tranche_count + 1)
        }
        lots = {
            lot_id: tuple(
                self._call(self.coordinator.functions.assignmentLot(lot_id), safe)
            )
            for lot_id in range(1, lot_count + 1)
        }
        registered_count = int(
            self._call(self.coordinator.functions.registeredLaneCount(), safe)
        )
        if registered_count > 16:
            raise RuntimeError("Meta Wheel registered lane bound exceeded")
        registered = [
            self._call(self.coordinator.functions.registeredLaneAt(index), safe)
            for index in range(registered_count)
        ]
        csp_addresses = frozenset(
            Web3.to_checksum_address(item[0])
            for item in registered
            if int(item[1]) == 1
        )
        call_addresses = frozenset(
            Web3.to_checksum_address(item[0])
            for item in registered
            if int(item[1]) == 2
        )
        if (
            csp_addresses != self.expected_csp_lanes
            or call_addresses != self.expected_call_lanes
            or len(csp_addresses) + len(call_addresses) != len(registered)
        ):
            raise RuntimeError(
                "Meta Wheel registered lanes differ from configured lanes"
            )
        active_by_address = {
            Web3.to_checksum_address(item[0]): bool(item[2]) for item in registered
        }
        csp_results = [
            self._lane_snapshot(
                address,
                LaneKind.CSP,
                active_by_address[address],
                safe,
                tranches,
            )
            for address in sorted(csp_addresses)
        ]
        call_results = [
            self._lane_snapshot(
                address,
                LaneKind.COVERED_CALL,
                active_by_address[address],
                safe,
                tranches,
            )
            for address in sorted(call_addresses)
        ]
        csp_lanes = tuple(result[0] for result in csp_results)
        call_lanes = tuple(result[0] for result in call_results)
        csp_caps = {result[1] for result in csp_results}
        call_caps = {result[1] for result in call_results}
        if len(csp_caps) != 1 or len(call_caps) != 1:
            raise RuntimeError("Meta Wheel child lane caps are inconsistent")

        nav = tuple(self._call(self.parent.functions.activeNavWindow(), safe))
        strategy_hash = _hex(self._call(self.strategy.functions.positionsHash(), safe))
        coordinator_hash = _hex(
            self._call(self.coordinator.functions.positionStateHash(), safe)
        )
        component_id = Web3.solidity_keccak(
            ["string", "address"], ["STRATEGY", self.manifest.coordinator]
        )
        component = tuple(
            self._call(self.accounting.functions.componentState(component_id), safe)
        )
        position_nonce = int(
            self._call(
                self.strategy.functions.positionNonce(self.manifest.coordinator), safe
            )
        )
        strategy_config = tuple(
            self._call(
                self.strategy.functions.strategyConfig(self.manifest.coordinator),
                safe,
            )
        )
        nav_component_hash = _hex(component[3])
        all_lanes = self._bind_nav_observation(
            lanes=(*csp_lanes, *call_lanes),
            nav=nav,
            component_id=component_id,
            safe_block=safe,
        )
        by_lane = {lane.address: lane for lane in all_lanes}
        csp_lanes = tuple(by_lane[lane.address] for lane in csp_lanes)
        call_lanes = tuple(by_lane[lane.address] for lane in call_lanes)
        nav_coherent = (
            bool(component[4])
            and Web3.to_checksum_address(component[0]) == self.manifest.valuator
            and int(component[1]) == 1
            and int(component[2]) == position_nonce
            and nav_component_hash == coordinator_hash
            and _hex(nav[10]) == strategy_hash
            and int(strategy_config[4]) == 1
            and Web3.to_checksum_address(strategy_config[5]) == self.manifest.valuator
        )
        nav_fresh = int(nav[6]) <= safe <= int(nav[7]) and int(nav[5]) <= safe
        fee = tuple(self._call(self.accounting.functions.feeConfig(), safe))
        premium_fee = int(self._call(self.settler.functions.protocolFeeBps(), safe))
        treasury = Web3.to_checksum_address(
            self._call(self.settler.functions.treasury(), safe)
        )
        if premium_fee > 0 and int(treasury, 16) == 0:
            raise RuntimeError(
                "Meta Wheel protocol fee is configured but the treasury is unset"
            )
        lane_caps = tuple(self._call(self.coordinator.functions.laneCaps(), safe))
        pending_shares = int(self._call(self.flow.functions.totalPendingShares(), safe))
        pending_redemption = int(
            self._call(self.parent.functions.convertToAssets(pending_shares), safe)
        )
        spot = int(self._call(self.oracle.functions.getPrice(self.manifest.weth), safe))
        coordinator_usdc = int(
            self._call(self.usdc.functions.balanceOf(self.manifest.coordinator), safe)
        )
        coordinator_weth = int(
            self._call(self.weth.functions.balanceOf(self.manifest.coordinator), safe)
        )
        transition_balances_reconciled = (
            int(summary[7]) <= coordinator_usdc
            and int(summary[8]) == int(summary[6])
            and int(summary[8]) <= coordinator_weth
            and all(result[2] for result in (*csp_results, *call_results))
            and not bool(self._call(self.flow.functions.hasActiveProcessing(), safe))
        )
        pending_tranches = tuple(
            PendingCspTranche(
                tranche_id=tranche_id,
                state_nonce=int(value[2]),
                pending_usdc=int(value[5]),
                principal_usdc=int(value[4]),
            )
            for tranche_id, value in sorted(tranches.items())
            if int(value[0]) == 1 and int(value[5]) > 0
        )
        lot_status = {
            1: LotStatus.AVAILABLE,
            2: LotStatus.CALL_OPEN,
            3: LotStatus.CALLED_AWAY,
            4: LotStatus.RETURNED,
        }
        assignment_lots = []
        for lot_id, value in sorted(lots.items()):
            raw_status = int(value[2])
            if raw_status not in lot_status:
                raise RuntimeError("Meta Wheel assignment lot status is unsupported")
            tranche_id = int(value[3])
            assignment_lots.append(
                AssignmentLot(
                    lot_id=lot_id,
                    tranche_id=tranche_id,
                    tranche_state_nonce=int(tranches[tranche_id][2]),
                    origin_csp_lane=Web3.to_checksum_address(value[0]),
                    origin_csp_position_id=int(value[4]),
                    weth_received=int(value[5]),
                    remaining_weth=int(value[6]),
                    literal_assignment_strike8=int(value[7]),
                    created_at=int(value[1]),
                    status=lot_status[raw_status],
                    tranche_principal_usdc=int(tranches[tranche_id][4]),
                    tranche_pending_usdc=int(tranches[tranche_id][5]),
                )
            )
        after = self.w3.eth.get_block(safe)
        canonical = before.hash == after.hash
        return WheelSnapshot(
            chain_id=BASE_SEPOLIA_CHAIN_ID,
            parent=self.manifest.parent,
            coordinator=self.manifest.coordinator,
            safe_block=safe,
            safe_block_confirmations=latest - safe,
            safe_block_canonical=canonical,
            timestamp=int(before.timestamp),
            onchain_policy_hash=_hash(
                self._call(self.coordinator.functions.policyHash(), safe)
            ),
            nav_policy_hash=self.manifest.policy_hash,
            onchain_floor_buffer8=int(
                self._call(self.coordinator.functions.floorBufferUsd8(), safe)
            ),
            onchain_max_csp_lanes=int(lane_caps[0]),
            onchain_max_call_lanes=int(lane_caps[1]),
            onchain_max_usdc_per_csp_lane=next(iter(csp_caps)),
            onchain_max_weth_per_call_lane=next(iter(call_caps)),
            coordinator_position_state_hash=coordinator_hash,
            nav_coordinator_position_state_hash=nav_component_hash,
            nav_coherent=nav_coherent,
            nav_fresh=nav_fresh,
            transition_balances_reconciled=transition_balances_reconciled,
            paused=self._paused_at(safe) or not bool(strategy_config[0]),
            parent_total_assets_usdc=int(
                self._call(self.parent.functions.totalAssets(), safe)
            ),
            idle_usdc=int(
                self._call(self.parent.functions.accountedIdleAssets(), safe)
            ),
            pending_csp_usdc=int(summary[3]),
            pending_csp_tranches=pending_tranches,
            pending_redemption_usdc=pending_redemption,
            reserved_redemption_usdc=int(summary[4]),
            reserved_principal_usdc=int(summary[5]),
            coordinator_transition_nonce=int(summary[0]),
            fund_flow_nonce=int(
                self._call(self.parent.functions.fundFlowNonce(), safe)
            ),
            spot_price8=spot,
            protocol_premium_fee_bps=premium_fee,
            parent_management_fee_bps=int(fee[0]) * BPS // WAD,
            parent_performance_fee_bps=int(fee[1]),
            child_management_fee_bps=0,
            child_performance_fee_bps=0,
            csp_lanes=csp_lanes,
            call_lanes=call_lanes,
            assignment_lots=tuple(assignment_lots),
            coordinator_accounted_usdc=int(summary[7]),
            coordinator_accounted_weth=int(summary[8]),
            coordinator_transition_weth=int(summary[6]),
            coordinator_raw_usdc=coordinator_usdc,
            coordinator_raw_weth=coordinator_weth,
        )

    def _compatible_series(self, raw: dict[str, Any], is_put: bool, block: int) -> bool:
        try:
            address = Web3.to_checksum_address(raw["otoken_address"])
            if not self.w3.eth.get_code(address, block_identifier=block):
                return False
            token = self.w3.eth.contract(address=address, abi=_OTOKEN_ABI)
            collateral = self.manifest.usdc if is_put else self.manifest.weth
            return (
                bool(self._call(token.functions.isPut(), block)) is is_put
                and Web3.to_checksum_address(
                    self._call(token.functions.underlying(), block)
                )
                == self.manifest.weth
                and Web3.to_checksum_address(
                    self._call(token.functions.strikeAsset(), block)
                )
                == self.manifest.usdc
                and Web3.to_checksum_address(
                    self._call(token.functions.collateralAsset(), block)
                )
                == collateral
                and int(self._call(token.functions.expiry(), block))
                == int(raw["expiry"])
                and int(self._call(token.functions.strikePrice(), block))
                == int(Decimal(str(raw["strike_price"])) * 10**8)
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _quote_fill_state(
        self, raw: dict[str, Any], signer: str, block: int
    ) -> tuple[int, int]:
        quote_tuple = (
            Web3.to_checksum_address(raw["otoken_address"]),
            int(raw["bid_price"]),
            int(raw["deadline"]),
            int(raw["quote_id"]),
            int(raw["max_amount"]),
            int(raw["maker_nonce"]),
        )
        quote_hash = self._call(self.settler.functions.hashQuote(quote_tuple), block)
        filled, cancelled = self._call(
            self.settler.functions.getQuoteState(signer, quote_hash), block
        )
        filled_amount = int(filled)
        remaining = 0 if cancelled else max(int(raw["max_amount"]) - filled_amount, 0)
        return filled_amount, remaining

    def _open_data(
        self,
        raw: dict[str, Any],
        *,
        option_amount: int,
        collateral: int,
        bid_price: int,
    ) -> bytes:
        quote = {
            "oToken": Web3.to_checksum_address(raw["otoken_address"]),
            "bidPrice": bid_price,
            "deadline": int(raw["deadline"]),
            "quoteId": int(raw["quote_id"]),
            "maxAmount": int(raw["max_amount"]),
            "makerNonce": int(raw["maker_nonce"]),
        }
        try:
            signature = HexBytes(
                sign_quote(
                    config.MM_PRIVATE_KEY,
                    build_domain(BASE_SEPOLIA_CHAIN_ID, self.manifest.batch_settler),
                    quote,
                )
            )
        except Exception:
            raise RuntimeError(
                "Meta Wheel quote signing configuration is invalid"
            ) from None
        return self.w3.codec.encode(
            [
                "((address,uint256,uint256,uint256,uint256,uint256),bytes,uint256,uint256)"
            ],
            [
                (
                    (
                        quote["oToken"],
                        bid_price,
                        int(raw["deadline"]),
                        int(raw["quote_id"]),
                        int(raw["max_amount"]),
                        int(raw["maker_nonce"]),
                    ),
                    signature,
                    option_amount,
                    collateral,
                )
            ],
        )

    def list_quotes(self, snapshot: WheelSnapshot) -> tuple[WheelQuote, ...]:
        if (
            self._policy_id is None
            or self._minimum_net_premium_bps is None
            or self._csp_minimum_net_premium_bps is None
            or self._market_maximum_age is None
            or snapshot.nav_policy_hash != self.manifest.policy_hash
            or snapshot.onchain_policy_hash != self.manifest.policy_hash
        ):
            raise RuntimeError(
                "Meta Wheel quote reader has no authoritative policy snapshot"
            )
        market = api_client.get_market_data(asset="eth", chain="base")
        api_client.require_protocol_fee_match(market, snapshot.protocol_premium_fee_bps)
        try:
            iv = float(market["iv"])
            if not math.isfinite(iv) or not validate_iv(iv, "Meta Wheel allocator"):
                raise ValueError("invalid IV")
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise RuntimeError("Meta Wheel IV snapshot is invalid") from error
        csp_market: tuple[float, float] | None
        try:
            csp_market = validate_market_snapshot(
                market,
                now=snapshot.timestamp,
                maximum_age=self._market_maximum_age,
            )
        except RuntimeError as error:
            csp_market = None
            log.info(
                "Meta Wheel decision=reject policy=%s policy_hash=%s "
                "reason=invalid_or_stale_csp_market protocol_fee_bps=%d detail=%s",
                self._policy_id,
                self.manifest.policy_hash,
                snapshot.protocol_premium_fee_bps,
                error,
            )
        raw_quotes = api_client.get_quotes()
        ambiguous_quote_identities = _ambiguous_quote_identities(raw_quotes)
        try:
            signer = Web3.to_checksum_address(
                Account.from_key(config.MM_PRIVATE_KEY).address
            )
            maker_nonce = int(
                self._call(
                    self.settler.functions.makerNonce(signer), snapshot.safe_block
                )
            )
            if not bool(
                self._call(
                    self.settler.functions.whitelistedMMs(signer),
                    snapshot.safe_block,
                )
            ):
                raise RuntimeError("Meta Wheel market maker is not whitelisted")
        except Exception:
            raise RuntimeError(
                "Meta Wheel market-maker quote identity is invalid"
            ) from None
        result: list[WheelQuote] = []
        idle_csp = [lane for lane in snapshot.csp_lanes if lane.phase == LanePhase.IDLE]
        idle_calls = [
            lane for lane in snapshot.call_lanes if lane.phase == LanePhase.IDLE
        ]
        available_lots = [
            lot
            for lot in snapshot.assignment_lots
            if lot.status == LotStatus.AVAILABLE and lot.remaining_weth > 0
        ]
        for raw in raw_quotes:
            try:
                if _quote_identity(raw) in ambiguous_quote_identities:
                    continue
                is_put = raw.get("is_put") is True
                if (
                    raw.get("asset") != "eth"
                    or raw.get("chain", "base") != "base"
                    or str(raw.get("deployment_status") or "ready").lower() != "ready"
                    or int(raw["maker_nonce"]) != maker_nonce
                    or int(raw["bid_price"]) <= 0
                    or not self._compatible_series(raw, is_put, snapshot.safe_block)
                ):
                    continue
                strike8 = int(Decimal(str(raw["strike_price"])) * 10**8)
                filled_amount, remaining = self._quote_fill_state(
                    raw, signer, snapshot.safe_block
                )
                if remaining <= 0:
                    continue
                common = {
                    "is_put": is_put,
                    "strike8": strike8,
                    "expiry": int(raw["expiry"]),
                    "created_at": _quote_created_at(raw),
                    "deadline": int(raw["deadline"]),
                    "canonical_series": True,
                }
                if is_put and csp_market is None:
                    log.info(
                        "Meta Wheel decision=reject quote_id=%s reason=invalid_or_stale_csp_market",
                        raw.get("quote_id"),
                    )
                    continue
                delta_spot = (
                    csp_market[0]
                    if is_put and csp_market is not None
                    else snapshot.spot_price8 / 10**8
                )
                delay_years = max(int(raw["expiry"]) - snapshot.timestamp, 1) / (
                    365 * 86_400
                )
                delta_bps = round(
                    abs(
                        bs_delta(
                            is_put,
                            delta_spot,
                            strike8 / 10**8,
                            delay_years,
                            config.RISK_FREE_RATE,
                            iv,
                        )
                    )
                    * BPS
                )
                if not 0 < delta_bps < BPS:
                    log.info(
                        "Meta Wheel decision=reject quote_id=%s reason=invalid_delta",
                        raw.get("quote_id"),
                    )
                    continue
                if is_put:
                    maximum_collateral = required_collateral(remaining, strike8)
                    for tranche in snapshot.pending_csp_tranches:
                        option_amount = option_amount_for_collateral(
                            tranche.pending_usdc, strike8
                        )
                        collateral = required_collateral(option_amount, strike8)
                        if option_amount <= 0 or option_amount > remaining:
                            continue
                        premium, net_premium = incremental_quote_premium(
                            filled_amount=filled_amount,
                            option_amount=option_amount,
                            bid_price=int(raw["bid_price"]),
                            protocol_fee_bps=snapshot.protocol_premium_fee_bps,
                        )
                        gross_bps = premium * BPS // collateral
                        for lane in idle_csp:
                            result.append(
                                WheelQuote(
                                    quote_id=f"{maker_nonce}:{raw['quote_id']}",
                                    gross_premium_bps=gross_bps,
                                    maximum_collateral=maximum_collateral,
                                    delta_bps=delta_bps,
                                    gross_premium=premium,
                                    net_premium=net_premium,
                                    collateral=collateral,
                                    open_data=self._open_data(
                                        raw,
                                        option_amount=option_amount,
                                        collateral=collateral,
                                        bid_price=int(raw["bid_price"]),
                                    ),
                                    lane=lane.address,
                                    tranche_id=tranche.tranche_id,
                                    allocation_amount=tranche.pending_usdc,
                                    **common,
                                )
                            )
                else:
                    maximum_collateral = call_collateral_for_option_amount(remaining)
                    for lot in available_lots:
                        option_amount = option_amount_for_call_collateral(
                            lot.remaining_weth
                        )
                        collateral = call_collateral_for_option_amount(option_amount)
                        if option_amount <= 0 or option_amount > remaining:
                            continue
                        bid_price = minimum_bid_price_for_net_premium(
                            option_amount=option_amount,
                            collateral_weth=collateral,
                            spot_price_8=snapshot.spot_price8,
                            minimum_net_premium_bps=self._minimum_net_premium_bps,
                            protocol_fee_bps=snapshot.protocol_premium_fee_bps,
                        )
                        if int(raw["bid_price"]) < bid_price:
                            # Never mutate and re-sign a backend quote. Its exact raw
                            # digest is the only key checked for fills/cancellation.
                            continue
                        bid_price = int(raw["bid_price"])
                        premium = option_amount * bid_price // 10**8
                        collateral_value = (
                            lot.remaining_weth
                            * snapshot.spot_price8
                            // USDC_PER_WETH_SCALE
                        )
                        if collateral_value <= 0:
                            continue
                        gross_bps = premium * BPS // collateral_value
                        for lane in idle_calls:
                            result.append(
                                WheelQuote(
                                    quote_id=f"{maker_nonce}:{raw['quote_id']}",
                                    gross_premium_bps=gross_bps,
                                    maximum_collateral=maximum_collateral,
                                    delta_bps=delta_bps,
                                    gross_premium=premium,
                                    collateral=collateral_value,
                                    open_data=self._open_data(
                                        raw,
                                        option_amount=option_amount,
                                        collateral=collateral,
                                        bid_price=bid_price,
                                    ),
                                    lane=lane.address,
                                    tranche_id=lot.tranche_id,
                                    lot_id=lot.lot_id,
                                    allocation_amount=lot.remaining_weth,
                                    **common,
                                )
                            )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return tuple(result)

    def _decode(
        self,
        contract: Any,
        receipt: Any,
        event_abis: list[dict[str, Any]],
    ) -> list[DecodedWheelEvent]:
        decoded: list[DecodedWheelEvent] = []
        for event_name in (event["name"] for event in event_abis):
            factory = getattr(contract.events, event_name)
            for event in factory().process_receipt(receipt, errors=DISCARD):
                address = Web3.to_checksum_address(event["address"])
                if address != Web3.to_checksum_address(contract.address):
                    continue
                decoded.append(
                    DecodedWheelEvent(
                        name=event_name,
                        address=address,
                        args=dict(event["args"]),
                    )
                )
        return decoded

    def reconcile(self, action: WheelAction, receipt) -> Reconciliation:
        observed = self.w3.eth.get_transaction_receipt(receipt.tx_hash)
        block = self.w3.eth.get_block(receipt.block_number)
        if (
            int(observed.status) != 1
            or observed.blockHash != block.hash
            or _hex(observed.blockHash).lower() != receipt.block_hash.lower()
        ):
            return Reconciliation(False, False, False, False, False, False)
        events = self._decode(self.coordinator, observed, _COORDINATOR_EVENTS)
        if action.kind != ActionKind.QUEUE_CSP_USDC:
            lane = self._lane_contract(Web3.to_checksum_address(action.lane))
            events.extend(self._decode(lane, observed, _LANE_EVENTS))
        reconciled = reconcile_wheel_events(
            action, events, premium_fee_bps=self.manifest.premium_fee_bps
        )
        state_reconciled = reconcile_wheel_state(
            action,
            events,
            self._observed_state(action, events, receipt.block_number),
        )
        if action.kind == ActionKind.QUEUE_CSP_USDC:
            advanced = (
                int(
                    self._call(
                        self.parent.functions.fundFlowNonce(),
                        receipt.block_number,
                    )
                )
                > action.transition_nonce
            )
        elif action.kind in {ActionKind.OPEN_CSP, ActionKind.OPEN_CALL}:
            post = tuple(
                self._call(
                    self.coordinator.functions.tranche(action.tranche_id),
                    receipt.block_number,
                )
            )
            advanced = int(post[2]) > action.transition_nonce
        elif action.kind in {
            ActionKind.SETTLE_CSP,
            ActionKind.SETTLE_CALL,
            ActionKind.HANDOFF_ASSIGNMENT,
            ActionKind.HANDOFF_CALL_AWAY,
        }:
            lane = self._lane_contract(Web3.to_checksum_address(action.lane))
            advanced = (
                int(self._call(lane.functions.stateNonce(), receipt.block_number))
                > action.transition_nonce
            )
        elif action.kind == ActionKind.RELEASE_REDEMPTION:
            post = tuple(
                self._call(self.coordinator.functions.summary(), receipt.block_number)
            )
            advanced = int(post[0]) > action.transition_nonce
        else:
            post = tuple(
                self._call(
                    self.coordinator.functions.tranche(action.tranche_id),
                    receipt.block_number,
                )
            )
            advanced = int(post[2]) > action.transition_nonce
        return replace(
            reconciled,
            child_shares_delta_matches=(
                reconciled.child_shares_delta_matches
                and state_reconciled.child_shares_delta_matches
            ),
            usdc_delta_matches=(
                reconciled.usdc_delta_matches and state_reconciled.usdc_delta_matches
            ),
            weth_delta_matches=(
                reconciled.weth_delta_matches and state_reconciled.weth_delta_matches
            ),
            principal_delta_matches=(
                reconciled.principal_delta_matches
                and state_reconciled.principal_delta_matches
            ),
            transition_nonce_advanced=(
                reconciled.transition_nonce_advanced
                and advanced
                and state_reconciled.transition_nonce_advanced
            ),
            premium_fee_matches=(
                reconciled.premium_fee_matches and state_reconciled.premium_fee_matches
            ),
        )

    def _observed_state(
        self,
        action: WheelAction,
        events: list[DecodedWheelEvent],
        block: int,
    ) -> WheelObservedState:
        summary = tuple(self._call(self.coordinator.functions.summary(), block))
        tranche = (
            tuple(
                self._call(self.coordinator.functions.tranche(action.tranche_id), block)
            )
            if action.tranche_id
            else None
        )
        sibling_id = 0
        for event_name in (
            "WheelSiblingTrancheQueued",
            "WheelRedemptionUsdcReleased",
            "WheelTrancheQueued",
        ):
            event = _matching(events, event_name)
            if event is not None:
                sibling_id = int(
                    event.args.get("siblingTrancheId")
                    or event.args.get("trancheId")
                    or 0
                )
        sibling = (
            tuple(self._call(self.coordinator.functions.tranche(sibling_id), block))
            if sibling_id
            else None
        )
        lane_shares = lane_accounted_usdc = lane_accounted_weth = 0
        lane_raw_usdc = lane_raw_weth = 0
        lane_execution_hash = lane_position_hash = ""
        if action.kind in {
            ActionKind.OPEN_CSP,
            ActionKind.OPEN_CALL,
            ActionKind.SETTLE_CSP,
            ActionKind.SETTLE_CALL,
            ActionKind.HANDOFF_ASSIGNMENT,
            ActionKind.HANDOFF_CALL_AWAY,
        }:
            lane = self._lane_contract(Web3.to_checksum_address(action.lane))
            lane_shares = int(self._call(lane.functions.childShares(), block))
            lane_execution_hash = _hex(
                self._call(lane.functions.executionStateHash(), block)
            )
            lane_position_hash = _hex(
                self._call(lane.functions.positionStateHash(), block)
            )
            accounting_abi = (
                _CSP_ACCOUNTING_ABI
                if action.kind
                in {
                    ActionKind.OPEN_CSP,
                    ActionKind.SETTLE_CSP,
                    ActionKind.HANDOFF_ASSIGNMENT,
                }
                else _CALL_ACCOUNTING_ABI
            )
            accounting = self.w3.eth.contract(
                address=Web3.to_checksum_address(action.lane), abi=accounting_abi
            )
            values = tuple(self._call(accounting.functions.accountingState(), block))
            lane_accounted_usdc, lane_accounted_weth = map(int, values[:2])
            lane_raw_usdc = int(
                self._call(self.usdc.functions.balanceOf(action.lane), block)
            )
            lane_raw_weth = int(
                self._call(self.weth.functions.balanceOf(action.lane), block)
            )
        return WheelObservedState(
            parent_idle_usdc=int(
                self._call(self.parent.functions.accountedIdleAssets(), block)
            ),
            coordinator_accounted_usdc=int(summary[7]),
            coordinator_accounted_weth=int(summary[8]),
            coordinator_transition_weth=int(summary[6]),
            coordinator_raw_usdc=int(
                self._call(
                    self.usdc.functions.balanceOf(self.manifest.coordinator), block
                )
            ),
            coordinator_raw_weth=int(
                self._call(
                    self.weth.functions.balanceOf(self.manifest.coordinator), block
                )
            ),
            pending_csp_usdc=int(summary[3]),
            reserved_redemption_usdc=int(summary[4]),
            reserved_principal_usdc=int(summary[5]),
            tranche_principal_usdc=int(tranche[4]) if tranche else 0,
            tranche_pending_usdc=int(tranche[5]) if tranche else 0,
            sibling_principal_usdc=int(sibling[4]) if sibling else 0,
            sibling_pending_usdc=int(sibling[5]) if sibling else 0,
            lane_child_shares=lane_shares,
            lane_accounted_usdc=lane_accounted_usdc,
            lane_accounted_weth=lane_accounted_weth,
            lane_raw_usdc=lane_raw_usdc,
            lane_raw_weth=lane_raw_weth,
            lane_execution_state_hash=lane_execution_hash,
            lane_position_state_hash=lane_position_hash,
        )


def _matching(
    events: list[DecodedWheelEvent], name: str, **expected: Any
) -> DecodedWheelEvent | None:
    def normalized(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return Web3.to_hex(value).lower()
        if isinstance(value, str) and value.startswith("0x"):
            return value.lower()
        return value

    candidates = [event for event in events if event.name == name]
    if len(candidates) != 1:
        return None
    event = candidates[0]
    return (
        event
        if all(
            normalized(event.args.get(field)) == normalized(value)
            for field, value in expected.items()
        )
        else None
    )


def _premium_ok(
    events: list[DecodedWheelEvent], action: WheelAction, premium_fee_bps: int
) -> bool:
    event = _matching(
        events,
        "WheelPremiumAccrued",
        trancheId=action.tranche_id,
        lane=action.lane,
    )
    if event is None:
        return False
    gross = int(event.args["grossPremiumAssets"])
    fee = int(event.args["protocolFeeAssets"])
    net = int(event.args["netPremiumAssets"])
    return gross > 0 and fee == gross * premium_fee_bps // BPS and net == gross - fee


def _principal_share(principal: int, total_assets: int, assets: int) -> int:
    if total_assets <= 0 or not 0 <= assets <= total_assets:
        return -1
    return principal if assets == total_assets else principal * assets // total_assets


def reconcile_wheel_state(
    action: WheelAction,
    events: list[DecodedWheelEvent],
    post: WheelObservedState,
) -> Reconciliation:
    """Check receipt-block custody, child-share, and principal deltas."""

    pre = action.pre_state
    if pre is None:
        return Reconciliation(False, False, False, False, False, False)
    coordinator_reserves_unchanged = (
        post.reserved_redemption_usdc == pre.reserved_redemption_usdc
        and post.reserved_principal_usdc == pre.reserved_principal_usdc
    )
    coordinator_usdc_unchanged = (
        post.coordinator_accounted_usdc == pre.coordinator_accounted_usdc
        and post.coordinator_raw_usdc == pre.coordinator_raw_usdc
        and post.pending_csp_usdc == pre.pending_csp_usdc
        and post.parent_idle_usdc == pre.parent_idle_usdc
    )
    coordinator_weth_unchanged = (
        post.coordinator_accounted_weth == pre.coordinator_accounted_weth
        and post.coordinator_raw_weth == pre.coordinator_raw_weth
        and post.coordinator_transition_weth == pre.coordinator_transition_weth
    )
    child = usdc = weth = principal = transition = False
    if action.kind == ActionKind.QUEUE_CSP_USDC:
        event = _matching(events, "WheelTrancheQueued")
        child = transition = event is not None
        usdc = bool(
            event
            and post.parent_idle_usdc == pre.parent_idle_usdc - action.amount
            and post.coordinator_accounted_usdc
            == pre.coordinator_accounted_usdc + action.amount
            and post.coordinator_raw_usdc == pre.coordinator_raw_usdc + action.amount
            and post.pending_csp_usdc == pre.pending_csp_usdc + action.amount
            and coordinator_reserves_unchanged
        )
        weth = coordinator_weth_unchanged
        principal = bool(
            event
            and post.sibling_principal_usdc == action.amount
            and post.sibling_pending_usdc == action.amount
        )
    elif action.kind == ActionKind.SPLIT_PENDING_CSP:
        event = _matching(events, "WheelSiblingTrancheQueued")
        moved = int(event.args["principalUsdc"]) if event else -1
        child = transition = event is not None
        usdc = coordinator_usdc_unchanged and coordinator_reserves_unchanged
        weth = coordinator_weth_unchanged
        principal = bool(
            event
            and moved
            == _principal_share(
                pre.tranche_principal_usdc,
                pre.tranche_pending_usdc,
                action.amount,
            )
            and post.tranche_principal_usdc == pre.tranche_principal_usdc - moved
            and post.tranche_pending_usdc == pre.tranche_pending_usdc - action.amount
            and post.sibling_principal_usdc == moved
            and post.sibling_pending_usdc == action.amount
        )
    elif action.kind == ActionKind.RESERVE_REDEMPTION:
        event = _matching(events, "WheelRedemptionUsdcReserved")
        moved = int(event.args["principalReserved"]) if event else -1
        child = transition = event is not None
        usdc = (
            post.parent_idle_usdc == pre.parent_idle_usdc
            and post.coordinator_accounted_usdc == pre.coordinator_accounted_usdc
            and post.coordinator_raw_usdc == pre.coordinator_raw_usdc
            and post.pending_csp_usdc == pre.pending_csp_usdc - action.amount
            and post.reserved_redemption_usdc
            == pre.reserved_redemption_usdc + action.amount
        )
        weth = coordinator_weth_unchanged
        principal = bool(
            event
            and moved
            == _principal_share(
                pre.tranche_principal_usdc,
                pre.tranche_pending_usdc,
                action.amount,
            )
            and post.tranche_principal_usdc == pre.tranche_principal_usdc - moved
            and post.tranche_pending_usdc == pre.tranche_pending_usdc - action.amount
            and post.reserved_principal_usdc == pre.reserved_principal_usdc + moved
        )
    elif action.kind == ActionKind.RELEASE_REDEMPTION:
        event = _matching(events, "WheelRedemptionUsdcReleased")
        restored = int(event.args["principalRestored"]) if event else -1
        child = transition = event is not None
        usdc = (
            post.parent_idle_usdc == pre.parent_idle_usdc
            and post.coordinator_accounted_usdc == pre.coordinator_accounted_usdc
            and post.coordinator_raw_usdc == pre.coordinator_raw_usdc
            and post.pending_csp_usdc == pre.pending_csp_usdc + action.amount
            and post.reserved_redemption_usdc
            == pre.reserved_redemption_usdc - action.amount
        )
        weth = coordinator_weth_unchanged
        principal = bool(
            event
            and restored
            == _principal_share(
                pre.reserved_principal_usdc,
                pre.reserved_redemption_usdc,
                action.amount,
            )
            and post.reserved_principal_usdc == pre.reserved_principal_usdc - restored
            and post.sibling_principal_usdc == restored
            and post.sibling_pending_usdc == action.amount
        )
    elif action.kind in {ActionKind.OPEN_CSP, ActionKind.OPEN_CALL}:
        opened = _matching(events, "WheelTrancheOpened")
        opened_child = _matching(
            events,
            "CspOpened" if action.kind == ActionKind.OPEN_CSP else "CoveredCallOpened",
        )
        child = bool(
            opened
            and opened_child
            and post.lane_child_shares == action.amount
            and _hex(opened_child.args["positionHash"]).lower()
            == post.lane_execution_state_hash.lower()
            and bool(post.lane_position_state_hash)
        )
        principal = (
            post.tranche_principal_usdc == pre.tranche_principal_usdc
            and post.tranche_pending_usdc == 0
        )
        if action.kind == ActionKind.OPEN_CSP:
            usdc = (
                post.parent_idle_usdc == pre.parent_idle_usdc
                and post.coordinator_accounted_usdc
                == pre.coordinator_accounted_usdc - action.amount
                and post.coordinator_raw_usdc
                == pre.coordinator_raw_usdc - action.amount
                and post.pending_csp_usdc == pre.pending_csp_usdc - action.amount
                and post.lane_accounted_usdc == pre.lane_accounted_usdc
                and post.lane_raw_usdc == pre.lane_raw_usdc
                and coordinator_reserves_unchanged
            )
            weth = (
                coordinator_weth_unchanged
                and post.lane_accounted_weth == pre.lane_accounted_weth
                and post.lane_raw_weth == pre.lane_raw_weth
            )
        else:
            collateral = int(opened_child.args["collateral"]) if opened_child else -1
            usdc = (
                coordinator_usdc_unchanged
                and coordinator_reserves_unchanged
                and post.lane_accounted_usdc == pre.lane_accounted_usdc
                and post.lane_raw_usdc == pre.lane_raw_usdc
            )
            weth = bool(
                opened_child
                and 0 < collateral <= action.amount
                and post.coordinator_accounted_weth
                == pre.coordinator_accounted_weth - action.amount
                and post.coordinator_raw_weth
                == pre.coordinator_raw_weth - action.amount
                and post.coordinator_transition_weth
                == pre.coordinator_transition_weth - action.amount
                and post.lane_accounted_weth
                == pre.lane_accounted_weth + action.amount - collateral
                and post.lane_raw_weth == pre.lane_raw_weth + action.amount - collateral
            )
        transition = child and (
            post.lane_execution_state_hash.lower()
            != pre.lane_execution_state_hash.lower()
        )
    elif action.kind in {ActionKind.SETTLE_CSP, ActionKind.SETTLE_CALL}:
        settled = _matching(
            events,
            "CspSettlementAdvanced"
            if action.kind == ActionKind.SETTLE_CSP
            else "CoveredCallSettlementAdvanced",
        )
        observed_usdc = int(settled.args["observedUsdc"]) if settled else -1
        observed_weth = int(settled.args["observedWeth"]) if settled else -1
        child = bool(
            settled
            and post.lane_child_shares == pre.lane_child_shares
            and post.lane_execution_state_hash.lower()
            == _hex(settled.args["positionHash"]).lower()
        )
        usdc = bool(
            settled
            and coordinator_usdc_unchanged
            and coordinator_reserves_unchanged
            and post.lane_accounted_usdc == pre.lane_accounted_usdc + observed_usdc
            and post.lane_raw_usdc == pre.lane_raw_usdc + observed_usdc
        )
        weth = bool(
            settled
            and coordinator_weth_unchanged
            and post.lane_accounted_weth == pre.lane_accounted_weth + observed_weth
            and post.lane_raw_weth == pre.lane_raw_weth + observed_weth
        )
        principal = (
            post.tranche_principal_usdc == pre.tranche_principal_usdc
            and post.tranche_pending_usdc == pre.tranche_pending_usdc
        )
        transition = child and (
            post.lane_execution_state_hash.lower()
            != pre.lane_execution_state_hash.lower()
        )
    elif action.kind in {ActionKind.HANDOFF_ASSIGNMENT, ActionKind.HANDOFF_CALL_AWAY}:
        handed = _matching(events, "WheelChildHandoff")
        returned_usdc = int(handed.args["usdcAmount"]) if handed else -1
        returned_weth = int(handed.args["wethAmount"]) if handed else -1
        child = bool(
            handed
            and int(handed.args["childSharesBurned"]) == pre.lane_child_shares
            and post.lane_child_shares == 0
        )
        usdc = bool(
            handed
            and post.parent_idle_usdc == pre.parent_idle_usdc
            and post.coordinator_accounted_usdc
            == pre.coordinator_accounted_usdc + returned_usdc
            and post.coordinator_raw_usdc == pre.coordinator_raw_usdc + returned_usdc
            and post.pending_csp_usdc == pre.pending_csp_usdc + returned_usdc
            and post.lane_accounted_usdc == pre.lane_accounted_usdc - returned_usdc
            and post.lane_raw_usdc == pre.lane_raw_usdc - returned_usdc
            and coordinator_reserves_unchanged
        )
        weth = bool(
            handed
            and post.coordinator_accounted_weth
            == pre.coordinator_accounted_weth + returned_weth
            and post.coordinator_raw_weth == pre.coordinator_raw_weth + returned_weth
            and post.coordinator_transition_weth
            == pre.coordinator_transition_weth + returned_weth
            and post.lane_accounted_weth == pre.lane_accounted_weth - returned_weth
            and post.lane_raw_weth == pre.lane_raw_weth - returned_weth
        )
        principal = False
        if handed and action.kind == ActionKind.HANDOFF_ASSIGNMENT:
            if returned_weth == 0:
                principal = (
                    post.tranche_principal_usdc == pre.tranche_principal_usdc
                    and post.tranche_pending_usdc == returned_usdc
                )
            else:
                lot = _matching(events, "WheelAssignmentLotCreated")
                literal_strike = int(lot.args["literalAssignmentStrike8"]) if lot else 0
                retained = min(
                    pre.tranche_principal_usdc,
                    returned_weth * literal_strike // USDC_PER_WETH_SCALE,
                )
                sibling_principal = (
                    pre.tranche_principal_usdc - retained if returned_usdc else 0
                )
                current_principal = (
                    pre.tranche_principal_usdc if returned_usdc == 0 else retained
                )
                principal = bool(
                    lot
                    and post.tranche_principal_usdc == current_principal
                    and post.tranche_pending_usdc == 0
                    and (
                        returned_usdc == 0
                        and sibling_principal == 0
                        or post.sibling_principal_usdc == sibling_principal
                        and post.sibling_pending_usdc == returned_usdc
                    )
                )
        elif handed:
            settlement_kind = int(handed.args["settlementKind"])
            if returned_weth == 0:
                principal = (
                    post.tranche_principal_usdc == pre.tranche_principal_usdc
                    and post.tranche_pending_usdc
                    == pre.tranche_pending_usdc + returned_usdc
                )
            else:
                consumed = (
                    _principal_share(
                        pre.tranche_principal_usdc,
                        pre.lane_child_shares,
                        pre.lane_child_shares - returned_weth,
                    )
                    if settlement_kind == 4
                    else 0
                )
                principal = (
                    consumed >= 0
                    and post.tranche_principal_usdc
                    == pre.tranche_principal_usdc - consumed
                    and post.tranche_pending_usdc == 0
                    and (
                        returned_usdc == 0
                        and consumed == 0
                        or post.sibling_principal_usdc == consumed
                        and post.sibling_pending_usdc == returned_usdc
                    )
                )
        transition = child and (
            post.lane_execution_state_hash.lower()
            != pre.lane_execution_state_hash.lower()
            and post.lane_position_state_hash.lower()
            != pre.lane_position_state_hash.lower()
        )
    return Reconciliation(child, usdc, weth, principal, transition, True)


def reconcile_wheel_events(
    action: WheelAction,
    events: list[DecodedWheelEvent],
    *,
    premium_fee_bps: int,
) -> Reconciliation:
    """Validate exact coordinator/child event agreement for one receipt."""

    child = usdc = weth = principal = transition = premium = False
    if action.kind == ActionKind.QUEUE_CSP_USDC:
        event = _matching(
            events,
            "WheelTrancheQueued",
            allocationId="0x" + action.key,
            usdcAmount=action.amount,
        )
        child = weth = premium = event is not None
        usdc = principal = transition = event is not None
    elif action.kind == ActionKind.SPLIT_PENDING_CSP:
        event = _matching(
            events,
            "WheelSiblingTrancheQueued",
            parentTrancheId=action.tranche_id,
            usdcAmount=action.amount,
        )
        valid = (
            event is not None and 0 < int(event.args["principalUsdc"]) <= action.amount
        )
        child = usdc = weth = premium = valid
        principal = transition = valid
    elif action.kind in {ActionKind.OPEN_CSP, ActionKind.OPEN_CALL}:
        opened = _matching(
            events,
            "WheelTrancheOpened",
            trancheId=action.tranche_id,
            lane=action.lane,
        )
        child_name = (
            "CspOpened" if action.kind == ActionKind.OPEN_CSP else "CoveredCallOpened"
        )
        opened_child = _matching(events, child_name, trancheId=action.tranche_id)
        valid = (
            opened is not None
            and opened_child is not None
            and int(opened.args["childShares"]) == action.amount
            and int(opened_child.args["childShares"]) == action.amount
            and int(opened.args["childPositionId"])
            == int(opened_child.args["positionId"])
            and _hex(opened.args["childPositionHash"])
            == _hex(opened_child.args["positionHash"])
        )
        if action.kind == ActionKind.OPEN_CSP:
            valid = (
                valid
                and int(opened_child.args["usdcAmount"]) == action.amount
                and int(opened_child.args["literalAssignmentStrike8"]) == action.strike8
            )
        else:
            floor = _matching(
                events,
                "WheelCoveredCallFloorEnforced",
                trancheId=action.tranche_id,
                lotId=action.lot_ids[0],
                lane=action.lane,
                requiredFloor8=action.required_floor8,
                callStrike8=action.strike8,
            )
            valid = (
                valid
                and floor is not None
                and int(opened_child.args["lotId"]) == action.lot_ids[0]
                and int(opened_child.args["wethAmount"]) == action.amount
                and int(opened_child.args["requiredFloor8"]) == action.required_floor8
                and int(opened_child.args["callStrike8"]) == action.strike8
            )
        child = usdc = weth = principal = transition = valid
        premium = valid and _premium_ok(events, action, premium_fee_bps)
    elif action.kind in {ActionKind.SETTLE_CSP, ActionKind.SETTLE_CALL}:
        coordinator = _matching(
            events,
            "WheelTrancheSettlementAdvanced",
            trancheId=action.tranche_id,
            lane=action.lane,
        )
        child_name = (
            "CspSettlementAdvanced"
            if action.kind == ActionKind.SETTLE_CSP
            else "CoveredCallSettlementAdvanced"
        )
        settled = _matching(events, child_name, trancheId=action.tranche_id)
        valid = (
            coordinator is not None
            and settled is not None
            and int(coordinator.args["settlementKind"])
            == int(settled.args["settlementKind"])
            and _hex(coordinator.args["childPositionHash"])
            == _hex(settled.args["positionHash"])
        )
        child = usdc = weth = principal = transition = premium = valid
    elif action.kind in {ActionKind.HANDOFF_ASSIGNMENT, ActionKind.HANDOFF_CALL_AWAY}:
        coordinator = _matching(
            events,
            "WheelChildHandoff",
            trancheId=action.tranche_id,
            lane=action.lane,
        )
        child_name = (
            "CspBasketHandedOff"
            if action.kind == ActionKind.HANDOFF_ASSIGNMENT
            else "CoveredCallBasketHandedOff"
        )
        handed = _matching(events, child_name, trancheId=action.tranche_id)
        valid = (
            coordinator is not None
            and handed is not None
            and int(coordinator.args["childSharesBurned"])
            == int(handed.args["childSharesBurned"])
            and int(coordinator.args["usdcAmount"]) == int(handed.args["usdcAmount"])
            and int(coordinator.args["wethAmount"]) == int(handed.args["wethAmount"])
            and _hex(coordinator.args["transitionHash"])
            == _hex(handed.args["transitionHash"])
        )
        if (
            valid
            and action.kind == ActionKind.HANDOFF_ASSIGNMENT
            and int(coordinator.args["wethAmount"]) > 0
        ):
            lot = _matching(
                events,
                "WheelAssignmentLotCreated",
                trancheId=action.tranche_id,
                originCspLane=action.lane,
                wethReceived=int(coordinator.args["wethAmount"]),
            )
            valid = lot is not None
        child = usdc = weth = principal = transition = premium = valid
    elif action.kind == ActionKind.RESERVE_REDEMPTION:
        event = _matching(
            events,
            "WheelRedemptionUsdcReserved",
            trancheId=action.tranche_id,
            amount=action.amount,
        )
        valid = (
            event is not None
            and 0 <= int(event.args["principalReserved"]) <= action.amount
        )
        child = usdc = weth = principal = transition = premium = valid
    elif action.kind == ActionKind.RELEASE_REDEMPTION:
        event = _matching(events, "WheelRedemptionUsdcReleased", amount=action.amount)
        valid = (
            event is not None
            and int(event.args["trancheId"]) > 0
            and int(event.args["principalRestored"]) >= 0
        )
        child = usdc = weth = principal = transition = premium = valid
    return Reconciliation(child, usdc, weth, principal, transition, premium)


def build_authoritative_chain_port() -> Web3MetaWheelChainPort:
    """Compose the default automatic port from the final manifest and two signers."""

    manifest, signers = load_runtime_gate_and_signers()
    runtime = BaseSepoliaMetaWheelRuntime(manifest)
    return Web3MetaWheelChainPort(
        manifest=manifest,
        signers=signers,
        snapshot_reader=runtime.read_snapshot,
        quote_reader=runtime.list_quotes,
        reconciler=runtime.reconcile,
        w3=runtime.w3,
    )
