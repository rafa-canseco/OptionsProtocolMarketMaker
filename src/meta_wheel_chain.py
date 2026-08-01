"""Role-separated, manifest-gated EVM execution for the Meta Wheel runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import TransactionNotFound

from src import config
from src.fund_tx import ConfirmedTransaction, send_confirmed_transaction
from src.meta_wheel_allocator import (
    ActionKind,
    CanonicalReceipt,
    ManagedOperationRequest,
    Reconciliation,
    StrategyManagerWrapper,
    SubmittedAction,
    WheelAction,
    WheelQuote,
    WheelSnapshot,
    managed_operation_for_action,
)
from src.meta_wheel_policy import MetaWheelPolicy


BASE_SEPOLIA_CHAIN_ID = 84532
CONFIRMED_MANIFEST_STATUS = "CONFIRMED_CANONICAL_RECEIPTS"
ZERO_ADDRESS = "0x" + "00" * 20
REQUIRED_FINAL_ROLES = (
    "admin",
    "upgrader",
    "accounting",
    "allocator",
    "processor",
    "curator",
    "guardian",
)


class WheelSignerRole(StrEnum):
    ALLOCATOR = "allocator"
    PROCESSOR = "processor"
    GUARDIAN = "guardian"
    CURATOR = "curator"


AUTOMATED_SIGNER_ROLES = frozenset(
    {WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR}
)
MANUAL_SIGNER_ROLES = frozenset({WheelSignerRole.GUARDIAN, WheelSignerRole.CURATOR})
AUTOMATED_WRAPPERS = frozenset(
    {StrategyManagerWrapper.ALLOCATION, StrategyManagerWrapper.PROCESSING}
)

_ROLE_BY_WRAPPER = {
    StrategyManagerWrapper.ALLOCATION: WheelSignerRole.ALLOCATOR,
    StrategyManagerWrapper.PROCESSING: WheelSignerRole.PROCESSOR,
    StrategyManagerWrapper.GUARDIAN: WheelSignerRole.GUARDIAN,
    StrategyManagerWrapper.CONFIGURATION: WheelSignerRole.CURATOR,
}

_STRATEGY_MANAGER_ABI = [
    {
        "name": "allocate",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "adapter", "type": "address"},
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "outputs": [],
    },
    *[
        {
            "name": wrapper.value,
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "adapter", "type": "address"},
                {"name": "data", "type": "bytes"},
            ],
            "outputs": [],
        }
        for wrapper in StrategyManagerWrapper
    ],
]


def _address(value: object, label: str) -> str:
    if not isinstance(value, str) or not Web3.is_address(value):
        raise RuntimeError(f"Invalid Meta Wheel address for {label}")
    normalized = Web3.to_checksum_address(value)
    if normalized.lower() == ZERO_ADDRESS:
        raise RuntimeError(f"Meta Wheel address for {label} is zero")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contract_proxy(contracts: Mapping[str, object], name: str) -> str:
    entry = contracts.get(name)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Meta Wheel manifest contracts.{name} is missing")
    return _address(entry.get("proxy"), f"contracts.{name}.proxy")


@dataclass(frozen=True)
class WheelManifestGate:
    path: Path
    sha256: str
    chain_id: int
    parent: str
    strategy_manager: str
    coordinator: str
    usdc: str
    final_roles: Mapping[str, str]


def load_wheel_manifest_gate(
    *,
    path: str | Path,
    approved_sha256: str,
    configured_parent: str,
    configured_strategy_manager: str,
    configured_coordinator: str,
    configured_usdc: str,
) -> WheelManifestGate:
    """Load the final B1N-419 handoff and bind configured runtime addresses."""

    manifest_path = Path(path)
    expected_hash = approved_sha256.removeprefix("0x").lower()
    if len(expected_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_hash
    ):
        raise RuntimeError("Invalid Meta Wheel deployment manifest SHA-256")
    try:
        observed_hash = _sha256_file(manifest_path)
    except OSError as error:
        raise RuntimeError("Unable to load Meta Wheel deployment manifest") from error
    if observed_hash != expected_hash:
        raise RuntimeError("Meta Wheel deployment manifest hash is not approved")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Unable to load Meta Wheel deployment manifest") from error
    if not isinstance(manifest, dict):
        raise RuntimeError("Meta Wheel deployment manifest must be an object")
    if (
        manifest.get("schemaVersion") != "1.0.0"
        or manifest.get("issue") != "B1N-419"
        or manifest.get("status") != CONFIRMED_MANIFEST_STATUS
        or manifest.get("deploymentStatus") != "DEPLOYED"
        or manifest.get("handoffReady") is not True
    ):
        raise RuntimeError("Meta Wheel deployment manifest is not a final handoff")

    network = manifest.get("network")
    if not isinstance(network, dict) or (
        network.get("name") != "base-sepolia"
        or network.get("chainId") != BASE_SEPOLIA_CHAIN_ID
    ):
        raise RuntimeError("Meta Wheel deployment manifest is not Base Sepolia")
    readiness = manifest.get("readiness")
    if not isinstance(readiness, dict) or (
        readiness.get("canonicalReceiptsRecorded") is not True
        or readiness.get("finalRolesReconciled") is not True
        or readiness.get("backendHandoffReady") is not True
        or readiness.get("mainnetAuthorized") is not False
    ):
        raise RuntimeError("Meta Wheel deployment readiness is incomplete")
    receipts = manifest.get("canonicalReceipts")
    if (
        not isinstance(receipts, list)
        or not receipts
        or any(
            not isinstance(receipt, dict)
            or receipt.get("status") != 1
            or not receipt.get("transactionHash")
            or not receipt.get("blockHash")
            or not isinstance(receipt.get("blockNumber"), int)
            for receipt in receipts
        )
    ):
        raise RuntimeError("Meta Wheel canonical deployment receipts are incomplete")

    roles = manifest.get("finalRoles")
    if not isinstance(roles, dict) or set(roles) != set(REQUIRED_FINAL_ROLES):
        raise RuntimeError("Meta Wheel finalRoles must contain the seven final roles")
    final_roles = {
        role: _address(roles[role], f"finalRoles.{role}")
        for role in REQUIRED_FINAL_ROLES
    }
    if len({address.lower() for address in final_roles.values()}) != len(
        REQUIRED_FINAL_ROLES
    ):
        raise RuntimeError("Meta Wheel finalRoles must use seven distinct addresses")

    contracts = manifest.get("contracts")
    assets = manifest.get("assets")
    if not isinstance(contracts, dict) or not isinstance(assets, dict):
        raise RuntimeError("Meta Wheel manifest contracts/assets are missing")
    parent = _contract_proxy(contracts, "fundVault")
    strategy_manager = _contract_proxy(contracts, "strategyManager")
    coordinator = _contract_proxy(contracts, "wheelCoordinator")
    usdc = _address(assets.get("usdc"), "assets.usdc")
    configured = {
        "fundVault": _address(configured_parent, "configured parent"),
        "strategyManager": _address(
            configured_strategy_manager, "configured StrategyManager"
        ),
        "wheelCoordinator": _address(
            configured_coordinator, "configured WheelCoordinator"
        ),
        "usdc": _address(configured_usdc, "configured USDC"),
    }
    manifest_addresses = {
        "fundVault": parent,
        "strategyManager": strategy_manager,
        "wheelCoordinator": coordinator,
        "usdc": usdc,
    }
    if any(
        configured[name].lower() != address.lower()
        for name, address in manifest_addresses.items()
    ):
        raise RuntimeError("Meta Wheel runtime addresses differ from final manifest")
    return WheelManifestGate(
        path=manifest_path,
        sha256=observed_hash,
        chain_id=BASE_SEPOLIA_CHAIN_ID,
        parent=parent,
        strategy_manager=strategy_manager,
        coordinator=coordinator,
        usdc=usdc,
        final_roles=final_roles,
    )


@dataclass(frozen=True)
class WheelRoleSigners:
    accounts: Mapping[WheelSignerRole, LocalAccount] = field(repr=False)

    @classmethod
    def _build(
        cls,
        manifest: WheelManifestGate,
        *,
        expected_roles: frozenset[WheelSignerRole],
        private_keys: Mapping[WheelSignerRole, str],
        declared_addresses: Mapping[WheelSignerRole, str],
    ) -> WheelRoleSigners:
        if (
            set(private_keys) != expected_roles
            or set(declared_addresses) != expected_roles
        ):
            raise RuntimeError("Meta Wheel signer set has an invalid role boundary")
        accounts: dict[WheelSignerRole, LocalAccount] = {}
        declared: dict[WheelSignerRole, str] = {}
        for role in expected_roles:
            declared[role] = _address(
                declared_addresses[role], f"configured {role.value} signer"
            )
            try:
                account = Account.from_key(private_keys[role])
            except Exception:
                raise RuntimeError(
                    f"Invalid private key for Meta Wheel {role.value} signer"
                ) from None
            if account.address.lower() != declared[role].lower():
                raise RuntimeError(
                    f"Meta Wheel {role.value} signer does not match declared address"
                )
            if declared[role].lower() != manifest.final_roles[role.value].lower():
                raise RuntimeError(
                    f"Meta Wheel {role.value} signer differs from finalRoles"
                )
            accounts[role] = account
        if len({address.lower() for address in declared.values()}) != len(
            expected_roles
        ):
            raise RuntimeError("Meta Wheel signer addresses must be distinct")
        return cls(accounts=accounts)

    @classmethod
    def build_automated(
        cls,
        manifest: WheelManifestGate,
        *,
        private_keys: Mapping[WheelSignerRole, str],
        declared_addresses: Mapping[WheelSignerRole, str],
    ) -> WheelRoleSigners:
        """Build the MM service boundary: Allocation and Processing only."""

        return cls._build(
            manifest,
            expected_roles=AUTOMATED_SIGNER_ROLES,
            private_keys=private_keys,
            declared_addresses=declared_addresses,
        )

    @classmethod
    def build_manual(
        cls,
        manifest: WheelManifestGate,
        *,
        private_keys: Mapping[WheelSignerRole, str],
        declared_addresses: Mapping[WheelSignerRole, str],
    ) -> WheelRoleSigners:
        """Build an explicit manual-only Guardian/Configuration signer set."""

        roles = frozenset(private_keys)
        if (
            not roles
            or not roles <= MANUAL_SIGNER_ROLES
            or set(declared_addresses) != roles
        ):
            raise RuntimeError(
                "Manual Meta Wheel signer set may contain only guardian/curator"
            )
        return cls._build(
            manifest,
            expected_roles=roles,
            private_keys=private_keys,
            declared_addresses=declared_addresses,
        )

    def for_wrapper(self, wrapper: StrategyManagerWrapper) -> LocalAccount:
        try:
            role = _ROLE_BY_WRAPPER[wrapper]
        except KeyError as error:
            raise RuntimeError(
                f"Unsupported Meta Wheel managed wrapper {wrapper}"
            ) from error
        try:
            return self.accounts[role]
        except KeyError:
            boundary = "manual tool" if role in MANUAL_SIGNER_ROLES else "MM service"
            raise RuntimeError(
                f"Meta Wheel {role.value} signer is unavailable in this {boundary}"
            ) from None

    @property
    def allocator(self) -> LocalAccount:
        return self.accounts[WheelSignerRole.ALLOCATOR]


def load_runtime_gate_and_signers() -> tuple[WheelManifestGate, WheelRoleSigners]:
    """Validate every activation input without returning or logging secret material."""

    required = {
        "META_WHEEL_DEPLOYMENT_MANIFEST_PATH": config.META_WHEEL_DEPLOYMENT_MANIFEST_PATH,
        "META_WHEEL_DEPLOYMENT_MANIFEST_SHA256": (
            config.META_WHEEL_DEPLOYMENT_MANIFEST_SHA256
        ),
        "META_WHEEL_PARENT_ADDRESS": config.META_WHEEL_PARENT_ADDRESS,
        "META_WHEEL_STRATEGY_MANAGER_ADDRESS": (
            config.META_WHEEL_STRATEGY_MANAGER_ADDRESS
        ),
        "META_WHEEL_COORDINATOR_ADDRESS": config.META_WHEEL_COORDINATOR_ADDRESS,
        "USDC_ADDRESS": config.USDC_ADDRESS,
        "META_WHEEL_ALLOCATOR_PRIVATE_KEY": config.META_WHEEL_ALLOCATOR_PRIVATE_KEY,
        "META_WHEEL_PROCESSOR_PRIVATE_KEY": config.META_WHEEL_PROCESSOR_PRIVATE_KEY,
        "META_WHEEL_ALLOCATOR_ADDRESS": config.META_WHEEL_ALLOCATOR_ADDRESS,
        "META_WHEEL_PROCESSOR_ADDRESS": config.META_WHEEL_PROCESSOR_ADDRESS,
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise RuntimeError(f"Missing Meta Wheel activation configuration: {missing}")
    if config.CHAIN_ID != BASE_SEPOLIA_CHAIN_ID:
        raise RuntimeError("Meta Wheel runtime requires Base Sepolia chain 84532")
    environment = config._current_environment()
    if environment and environment not in {"staging", "development", "test"}:
        raise RuntimeError("Meta Wheel runtime is forbidden in production")

    manifest = load_wheel_manifest_gate(
        path=config.META_WHEEL_DEPLOYMENT_MANIFEST_PATH or "",
        approved_sha256=config.META_WHEEL_DEPLOYMENT_MANIFEST_SHA256 or "",
        configured_parent=config.META_WHEEL_PARENT_ADDRESS or "",
        configured_strategy_manager=config.META_WHEEL_STRATEGY_MANAGER_ADDRESS or "",
        configured_coordinator=config.META_WHEEL_COORDINATOR_ADDRESS or "",
        configured_usdc=config.USDC_ADDRESS,
    )
    private_keys = {
        WheelSignerRole.ALLOCATOR: config.META_WHEEL_ALLOCATOR_PRIVATE_KEY or "",
        WheelSignerRole.PROCESSOR: config.META_WHEEL_PROCESSOR_PRIVATE_KEY or "",
    }
    declared_addresses = {
        WheelSignerRole.ALLOCATOR: config.META_WHEEL_ALLOCATOR_ADDRESS or "",
        WheelSignerRole.PROCESSOR: config.META_WHEEL_PROCESSOR_ADDRESS or "",
    }
    return manifest, WheelRoleSigners.build_automated(
        manifest,
        private_keys=private_keys,
        declared_addresses=declared_addresses,
    )


class Web3MetaWheelChainPort:
    """Automated EVM port with injectable authoritative reads/reconciliation."""

    def __init__(
        self,
        *,
        manifest: WheelManifestGate,
        signers: WheelRoleSigners,
        snapshot_reader: Callable[[MetaWheelPolicy], WheelSnapshot],
        quote_reader: Callable[[WheelSnapshot], Sequence[WheelQuote]],
        reconciler: Callable[[WheelAction, CanonicalReceipt], Reconciliation],
        w3: Any | None = None,
        strategy_contract: Any | None = None,
        transaction_sender: Callable[..., ConfirmedTransaction] = (
            send_confirmed_transaction
        ),
    ) -> None:
        self.manifest = manifest
        self.signers = signers
        if set(signers.accounts) != AUTOMATED_SIGNER_ROLES:
            raise RuntimeError(
                "Automated Meta Wheel port requires allocator and processor only"
            )
        self.snapshot_reader = snapshot_reader
        self.quote_reader = quote_reader
        self.reconciler = reconciler
        self.w3 = w3 or Web3(Web3.HTTPProvider(config.RPC_URL))
        if int(self.w3.eth.chain_id) != manifest.chain_id:
            raise RuntimeError("Meta Wheel RPC chain differs from final manifest")
        self.strategy = strategy_contract or self.w3.eth.contract(
            address=manifest.strategy_manager,
            abi=_STRATEGY_MANAGER_ABI,
        )
        for address in (
            manifest.parent,
            manifest.strategy_manager,
            manifest.coordinator,
            manifest.usdc,
        ):
            if not self.w3.eth.get_code(address):
                raise RuntimeError(
                    f"Meta Wheel manifest address has no code: {address}"
                )
        self.transaction_sender = transaction_sender

    def read_snapshot(self, policy: MetaWheelPolicy) -> WheelSnapshot:
        return self.snapshot_reader(policy)

    def list_quotes(self, snapshot: WheelSnapshot) -> Sequence[WheelQuote]:
        return self.quote_reader(snapshot)

    def submit(
        self,
        action: WheelAction,
        policy: MetaWheelPolicy,
        managed_request: ManagedOperationRequest | None,
    ) -> SubmittedAction:
        del policy
        if (
            action.chain_id != self.manifest.chain_id
            or action.parent.lower() != self.manifest.parent.lower()
            or _sha256_file(self.manifest.path) != self.manifest.sha256
        ):
            raise RuntimeError("Meta Wheel action no longer matches the final manifest")
        if action.kind == ActionKind.QUEUE_CSP_USDC:
            if (
                managed_request is not None
                or action.lane.lower() != self.manifest.coordinator.lower()
            ):
                raise RuntimeError(
                    "Meta Wheel queue allocation cannot use managed data"
                )
            account = self.signers.allocator
            function = self.strategy.functions.allocate(
                self.manifest.coordinator,
                self.manifest.usdc,
                action.amount,
                bytes.fromhex(action.key),
            )
        else:
            if (
                managed_request is not None
                and managed_request.wrapper not in AUTOMATED_WRAPPERS
            ):
                raise RuntimeError(
                    "Guardian/Configuration requests require an explicit manual tool"
                )
            expected = managed_operation_for_action(action)
            if managed_request is None or managed_request != expected:
                raise RuntimeError(
                    "Meta Wheel managed operation changed before signing"
                )
            account = self.signers.for_wrapper(managed_request.wrapper)
            function = getattr(self.strategy.functions, managed_request.wrapper.value)(
                self.manifest.coordinator,
                managed_request.data,
            )
        confirmed = self.transaction_sender(
            w3=self.w3,
            account=account,
            function=function,
            chain_id=self.manifest.chain_id,
            confirmations=config.META_WHEEL_ALLOCATOR_CONFIRMATIONS,
        )
        return SubmittedAction(tx_hash=confirmed.tx_hash, nonce=confirmed.nonce)

    def receipt(self, tx_hash: str, confirmations: int) -> CanonicalReceipt | None:
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        block_number = int(receipt.blockNumber)
        try:
            canonical_block = self.w3.eth.get_block(block_number)
        except Exception:
            return None
        canonical = canonical_block.hash == receipt.blockHash
        observed_confirmations = max(
            int(self.w3.eth.block_number) - block_number + 1,
            0,
        )
        return CanonicalReceipt(
            tx_hash=tx_hash,
            block_number=block_number,
            block_hash=Web3.to_hex(receipt.blockHash),
            confirmations=observed_confirmations,
            canonical=canonical and observed_confirmations >= max(confirmations, 2),
            succeeded=int(receipt.status) == 1,
        )

    def reconcile(
        self, action: WheelAction, receipt: CanonicalReceipt
    ) -> Reconciliation:
        return self.reconciler(action, receipt)


def build_runtime_chain_port(
    *,
    snapshot_reader: Callable[[MetaWheelPolicy], WheelSnapshot],
    quote_reader: Callable[[WheelSnapshot], Sequence[WheelQuote]],
    reconciler: Callable[[WheelAction, CanonicalReceipt], Reconciliation],
) -> Web3MetaWheelChainPort:
    """Compose the reusable EVM port after the final manifest/signer gate passes."""

    manifest, signers = load_runtime_gate_and_signers()
    return Web3MetaWheelChainPort(
        manifest=manifest,
        signers=signers,
        snapshot_reader=snapshot_reader,
        quote_reader=quote_reader,
        reconciler=reconciler,
    )
