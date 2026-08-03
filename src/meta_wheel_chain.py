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


def _contract_address(contracts: Mapping[str, object], name: str) -> str:
    entry = contracts.get(name)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Meta Wheel manifest contracts.{name} is missing")
    return _address(entry.get("address"), f"contracts.{name}.address")


def _boundary_address(boundary: Mapping[str, object], name: str) -> str:
    entry = boundary.get(name)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Meta Wheel manifest v1Boundary.{name} is missing")
    return _address(entry.get("proxy") or entry.get("address"), f"v1Boundary.{name}")


def _bytes32(value: object, label: str, *, allow_zero: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 66
        or not value.startswith("0x")
        or any(character not in "0123456789abcdefABCDEF" for character in value[2:])
        or not allow_zero
        and int(value, 16) == 0
    ):
        raise RuntimeError(f"Invalid Meta Wheel bytes32 for {label}")
    return value.lower()


def _block_number(value: object, label: str, *, allow_zero: bool = False) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (0 if allow_zero else 1)
    ):
        raise RuntimeError(f"Invalid Meta Wheel block for {label}")
    return value


@dataclass(frozen=True)
class ManifestReceiptBinding:
    transaction_hash: str
    block_number: int
    block_hash: str


@dataclass(frozen=True)
class ManifestProxyBinding:
    label: str
    proxy: str
    implementation: str
    implementation_codehash: str | None


@dataclass(frozen=True)
class ManifestCodeBinding:
    label: str
    address: str
    codehash: str


@dataclass(frozen=True)
class WheelManifestGate:
    path: Path
    sha256: str
    chain_id: int
    parent: str
    strategy_manager: str
    coordinator: str
    usdc: str
    weth: str
    fund_accounting: str
    fund_flow_manager: str
    valuator: str
    batch_settler: str
    oracle: str
    deployment_start_block: int
    deployment_end_block: int
    policy_hash: str
    premium_fee_bps: int
    management_fee_wad: int
    performance_fee_bps: int
    final_roles: Mapping[str, str]
    canonical_receipts: tuple[ManifestReceiptBinding, ...]
    proxy_bindings: tuple[ManifestProxyBinding, ...]
    code_bindings: tuple[ManifestCodeBinding, ...]


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
    required_top_level = {
        "schemaVersion",
        "issue",
        "status",
        "deploymentStatus",
        "handoffReady",
        "sourceCommit",
        "deploymentId",
        "network",
        "canonicalReceipts",
        "assets",
        "contracts",
        "v1Boundary",
        "policy",
        "linkedLibraries",
        "linkedLibraryCodehashes",
        "finalRoles",
        "standaloneBaselines",
        "readiness",
        "verificationEvidence",
    }
    if not required_top_level <= set(manifest):
        raise RuntimeError("Meta Wheel deployment manifest schema is incomplete")
    if (
        manifest.get("schemaVersion") != "1.0.0"
        or manifest.get("issue") != "B1N-419"
        or manifest.get("status") != CONFIRMED_MANIFEST_STATUS
        or manifest.get("deploymentStatus") != "DEPLOYED"
        or manifest.get("handoffReady") is not True
    ):
        raise RuntimeError("Meta Wheel deployment manifest is not a final handoff")
    source_commit = manifest.get("sourceCommit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit.removeprefix("0x")) != 40
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in source_commit.removeprefix("0x")
        )
    ):
        raise RuntimeError("Meta Wheel source commit is invalid")
    _bytes32(manifest.get("deploymentId"), "deploymentId")

    network = manifest.get("network")
    if not isinstance(network, dict) or (
        network.get("name") != "base-sepolia"
        or network.get("chainId") != BASE_SEPOLIA_CHAIN_ID
    ):
        raise RuntimeError("Meta Wheel deployment manifest is not Base Sepolia")
    deployment_blocks = network.get("deploymentBlocks")
    if not isinstance(deployment_blocks, dict):
        raise RuntimeError("Meta Wheel deployment blocks are missing")
    deployment_start_block = _block_number(
        deployment_blocks.get("fundFirst"), "network.deploymentBlocks.fundFirst"
    )
    deployment_last_block = _block_number(
        deployment_blocks.get("fundLast"), "network.deploymentBlocks.fundLast"
    )
    if deployment_last_block < deployment_start_block:
        raise RuntimeError("Meta Wheel deployment block range is invalid")
    readiness = manifest.get("readiness")
    if not isinstance(readiness, dict) or (
        readiness.get("canonicalReceiptsRecorded") is not True
        or readiness.get("exactSourceRuntimeBytecodeVerified") is not True
        or readiness.get("bootstrapReconciled") is not True
        or readiness.get("finalRolesReconciled") is not True
        or readiness.get("standaloneBaselinesUnchanged") is not True
        or readiness.get("backendHandoffReady") is not True
        or readiness.get("mainnetAuthorized") is not False
    ):
        raise RuntimeError("Meta Wheel deployment readiness is incomplete")
    verification_evidence = manifest.get("verificationEvidence")
    if not isinstance(verification_evidence, dict) or (
        verification_evidence.get("method")
        != "SOLC_STANDARD_JSON_RPC_EXACT_V2"
        or verification_evidence.get("compilerVersion")
        != "0.8.24+commit.e11b9ed9"
        or verification_evidence.get("addressCount") != 47
        or verification_evidence.get("artifactCount") != 25
    ):
        raise RuntimeError("Meta Wheel source/runtime verification evidence is invalid")
    for digest_field in (
        "sourceRuntimeEvidenceSha256",
        "coreBuildInfoSha256",
        "libraryBuildInfoSha256",
        "coreStandardJsonInputSha256",
        "libraryStandardJsonInputSha256",
        "inventorySha256",
    ):
        _bytes32(
            verification_evidence.get(digest_field),
            f"verificationEvidence.{digest_field}",
        )
    receipts = manifest.get("canonicalReceipts")
    if (
        not isinstance(receipts, list)
        or len(receipts) < 2
        or any(
            not isinstance(receipt, dict)
            or isinstance(receipt.get("status"), bool)
            or not isinstance(receipt.get("status"), int)
            or receipt.get("status") != 1
            or not isinstance(receipt.get("blockNumber"), int)
            for receipt in receipts
        )
    ):
        raise RuntimeError("Meta Wheel canonical deployment receipts are incomplete")
    canonical_receipts = tuple(
        ManifestReceiptBinding(
            transaction_hash=_bytes32(
                receipt["transactionHash"], "canonicalReceipts.transactionHash"
            ),
            block_number=_block_number(
                receipt["blockNumber"], "canonicalReceipts.blockNumber"
            ),
            block_hash=_bytes32(receipt["blockHash"], "canonicalReceipts.blockHash"),
        )
        for receipt in receipts
    )
    if len({receipt.transaction_hash for receipt in canonical_receipts}) != len(
        canonical_receipts
    ):
        raise RuntimeError("Meta Wheel canonical receipts are duplicated")
    receipt_blocks = {receipt.block_number for receipt in canonical_receipts}
    if (
        deployment_start_block not in receipt_blocks
        or deployment_last_block not in receipt_blocks
    ):
        raise RuntimeError("Meta Wheel receipts do not bind deployment boundaries")

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
    boundary = manifest.get("v1Boundary")
    policy = manifest.get("policy")
    standalone = manifest.get("standaloneBaselines")
    linked_libraries = manifest.get("linkedLibraries")
    linked_library_codehashes = manifest.get("linkedLibraryCodehashes")
    if (
        not isinstance(contracts, dict)
        or not isinstance(assets, dict)
        or not isinstance(boundary, dict)
        or not isinstance(policy, dict)
        or not isinstance(standalone, dict)
        or not isinstance(linked_libraries, list)
        or not isinstance(linked_library_codehashes, list)
    ):
        raise RuntimeError("Meta Wheel manifest contracts/assets are missing")
    required_proxy_contracts = {
        "fundVault",
        "fundShare",
        "fundAccounting",
        "fundFlowManager",
        "strategyManager",
        "wheelCoordinator",
    }
    required_immutable_contracts = {
        "claimEscrow",
        "accessManager",
        "metaWheelValuator",
        "navReportVerifier",
    }
    if not (required_proxy_contracts | required_immutable_contracts) <= set(contracts):
        raise RuntimeError("Meta Wheel manifest contract set is incomplete")
    proxy_bindings: list[ManifestProxyBinding] = []
    code_bindings: list[ManifestCodeBinding] = []
    for name in sorted(required_proxy_contracts):
        entry = contracts[name]
        if not isinstance(entry, dict):
            raise RuntimeError(f"Meta Wheel contracts.{name} is invalid")
        proxy = _address(entry.get("proxy"), f"contracts.{name}.proxy")
        implementation = _address(
            entry.get("implementation"), f"contracts.{name}.implementation"
        )
        implementation_codehash = _bytes32(
            entry.get("implementationCodehash"),
            f"contracts.{name}.implementationCodehash",
        )
        _block_number(
            entry.get("validFromBlock"),
            f"contracts.{name}.validFromBlock",
            allow_zero=True,
        )
        _block_number(
            entry.get("implementationValidFromBlock"),
            f"contracts.{name}.implementationValidFromBlock",
            allow_zero=True,
        )
        proxy_bindings.append(
            ManifestProxyBinding(
                label=f"contracts.{name}",
                proxy=proxy,
                implementation=implementation,
                implementation_codehash=implementation_codehash,
            )
        )
        code_bindings.append(
            ManifestCodeBinding(
                label=f"contracts.{name}.implementation",
                address=implementation,
                codehash=implementation_codehash,
            )
        )
    for name in sorted(required_immutable_contracts):
        entry = contracts[name]
        if not isinstance(entry, dict):
            raise RuntimeError(f"Meta Wheel contracts.{name} is invalid")
        address = _address(entry.get("address"), f"contracts.{name}.address")
        codehash = _bytes32(entry.get("codehash"), f"contracts.{name}.codehash")
        _block_number(
            entry.get("validFromBlock"),
            f"contracts.{name}.validFromBlock",
            allow_zero=True,
        )
        code_bindings.append(
            ManifestCodeBinding(
                label=f"contracts.{name}",
                address=address,
                codehash=codehash,
            )
        )
    if "swapRouter" not in assets:
        raise RuntimeError("Meta Wheel manifest assets are incomplete")
    _address(assets.get("swapRouter"), "assets.swapRouter")
    parent = _contract_proxy(contracts, "fundVault")
    strategy_manager = _contract_proxy(contracts, "strategyManager")
    coordinator = _contract_proxy(contracts, "wheelCoordinator")
    usdc = _address(assets.get("usdc"), "assets.usdc")
    weth = _address(assets.get("weth"), "assets.weth")
    fund_accounting = _contract_proxy(contracts, "fundAccounting")
    fund_flow_manager = _contract_proxy(contracts, "fundFlowManager")
    valuator = _contract_address(contracts, "metaWheelValuator")
    batch_settler = _boundary_address(boundary, "batchSettler")
    oracle = _boundary_address(boundary, "oracle")
    required_v1 = {
        "addressBook",
        "controller",
        "batchSettler",
        "marginPool",
        "oracle",
        "oTokenFactory",
        "whitelist",
    }
    if not required_v1 <= set(boundary):
        raise RuntimeError("Meta Wheel V1 boundary is incomplete")
    for name in sorted(required_v1):
        entry = boundary[name]
        if not isinstance(entry, dict) or entry.get("unchanged") is not True:
            raise RuntimeError(f"Meta Wheel v1Boundary.{name} is not unchanged")
        proxy = _address(entry.get("proxy"), f"v1Boundary.{name}.proxy")
        if name in {"controller", "batchSettler"}:
            implementation = _address(
                entry.get("implementation"), f"v1Boundary.{name}.implementation"
            )
            proxy_bindings.append(
                ManifestProxyBinding(
                    label=f"v1Boundary.{name}",
                    proxy=proxy,
                    implementation=implementation,
                    implementation_codehash=None,
                )
            )
    required_standalone = {
        "cspVault",
        "cspAdapter",
        "coveredCallVault",
        "coveredCallAdapter",
    }
    if not required_standalone <= set(standalone):
        raise RuntimeError("Meta Wheel standalone baselines are incomplete")
    for name in sorted(required_standalone):
        entry = standalone[name]
        if not isinstance(entry, dict) or entry.get("unchanged") is not True:
            raise RuntimeError(f"Meta Wheel standaloneBaselines.{name} changed")
        proxy = _address(entry.get("proxy"), f"standaloneBaselines.{name}.proxy")
        implementation = _address(
            entry.get("implementation"),
            f"standaloneBaselines.{name}.implementation",
        )
        implementation_codehash = _bytes32(
            entry.get("implementationCodehash"),
            f"standaloneBaselines.{name}.implementationCodehash",
        )
        proxy_bindings.append(
            ManifestProxyBinding(
                label=f"standaloneBaselines.{name}",
                proxy=proxy,
                implementation=implementation,
                implementation_codehash=implementation_codehash,
            )
        )
        code_bindings.append(
            ManifestCodeBinding(
                label=f"standaloneBaselines.{name}.implementation",
                address=implementation,
                codehash=implementation_codehash,
            )
        )
    if len(linked_libraries) < 5 or len(linked_libraries) != len(
        linked_library_codehashes
    ):
        raise RuntimeError("Meta Wheel linked library evidence is incomplete")
    for index, (address, codehash) in enumerate(
        zip(linked_libraries, linked_library_codehashes, strict=True)
    ):
        code_bindings.append(
            ManifestCodeBinding(
                label=f"linkedLibraries[{index}]",
                address=_address(address, f"linkedLibraries[{index}]"),
                codehash=_bytes32(codehash, f"linkedLibraryCodehashes[{index}]"),
            )
        )
    policy_hash = str(policy.get("policyHash", "")).removeprefix("0x").lower()
    if len(policy_hash) != 64 or any(
        character not in "0123456789abcdef" for character in policy_hash
    ):
        raise RuntimeError("Meta Wheel manifest policy hash is invalid")
    premium_fee_bps = policy.get("premiumFeeBps")
    management_fee_wad = policy.get("managementFeeWad")
    performance_fee_bps = policy.get("performanceFeeBps")
    if (
        premium_fee_bps != 1_000
        or management_fee_wad != 20_000_000_000_000_000
        or performance_fee_bps != 1_000
    ):
        raise RuntimeError("Meta Wheel manifest fee policy is invalid")
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
        weth=weth,
        fund_accounting=fund_accounting,
        fund_flow_manager=fund_flow_manager,
        valuator=valuator,
        batch_settler=batch_settler,
        oracle=oracle,
        deployment_start_block=deployment_start_block,
        deployment_end_block=deployment_last_block,
        policy_hash=policy_hash,
        premium_fee_bps=premium_fee_bps,
        management_fee_wad=management_fee_wad,
        performance_fee_bps=performance_fee_bps,
        final_roles=final_roles,
        canonical_receipts=canonical_receipts,
        proxy_bindings=tuple(proxy_bindings),
        code_bindings=tuple(code_bindings),
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
        "META_WHEEL_APPROVED_POLICY_SHA256": (config.META_WHEEL_APPROVED_POLICY_SHA256),
        "META_WHEEL_PARENT_ADDRESS": config.META_WHEEL_PARENT_ADDRESS,
        "META_WHEEL_STRATEGY_MANAGER_ADDRESS": (
            config.META_WHEEL_STRATEGY_MANAGER_ADDRESS
        ),
        "META_WHEEL_COORDINATOR_ADDRESS": config.META_WHEEL_COORDINATOR_ADDRESS,
        "META_WHEEL_VALUATOR_ADDRESS": config.META_WHEEL_VALUATOR_ADDRESS,
        "META_WHEEL_CSP_LANE_ADDRESSES": config.META_WHEEL_CSP_LANE_ADDRESSES,
        "META_WHEEL_CALL_LANE_ADDRESSES": config.META_WHEEL_CALL_LANE_ADDRESSES,
        "META_WHEEL_FUND_KEY": config.META_WHEEL_FUND_KEY,
        "USDC_ADDRESS": config.USDC_ADDRESS,
        "COVERED_CALL_WETH_ADDRESS": config.COVERED_CALL_WETH_ADDRESS,
        "BATCH_SETTLER": config.BATCH_SETTLER,
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
    approved_policy_hash = (
        (config.META_WHEEL_APPROVED_POLICY_SHA256 or "").removeprefix("0x").lower()
    )
    if approved_policy_hash != manifest.policy_hash:
        raise RuntimeError("Meta Wheel approved policy differs from final manifest")
    configured_runtime_addresses = {
        "COVERED_CALL_WETH_ADDRESS": (
            _address(config.COVERED_CALL_WETH_ADDRESS, "configured WETH"),
            manifest.weth,
        ),
        "BATCH_SETTLER": (
            _address(config.BATCH_SETTLER, "configured BatchSettler"),
            manifest.batch_settler,
        ),
        "META_WHEEL_VALUATOR_ADDRESS": (
            _address(
                config.META_WHEEL_VALUATOR_ADDRESS,
                "configured Meta Wheel valuator",
            ),
            manifest.valuator,
        ),
    }
    if any(
        configured.lower() != expected.lower()
        for configured, expected in configured_runtime_addresses.values()
    ):
        raise RuntimeError("Meta Wheel runtime boundary differs from final manifest")
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
