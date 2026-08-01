import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

from src.fund_tx import ConfirmedTransaction
from src.meta_wheel_allocator import (
    ActionKind,
    ManagedOperation,
    StrategyManagerWrapper,
    WheelAction,
    encode_managed_operation,
    managed_operation_for_action,
)
from src.meta_wheel_chain import (
    REQUIRED_FINAL_ROLES,
    Web3MetaWheelChainPort,
    WheelRoleSigners,
    WheelSignerRole,
    load_wheel_manifest_gate,
)
from src.meta_wheel_runtime import (
    EIP1967_IMPLEMENTATION_SLOT,
    BaseSepoliaMetaWheelRuntime,
)


def _private_key(index: int) -> str:
    return f"0x{index:064x}"


def _account_address(index: int) -> str:
    return Account.from_key(_private_key(index)).address


def _address(index: int) -> str:
    return Web3.to_checksum_address(f"0x{index:040x}")


def _bytes32(index: int) -> str:
    return f"0x{index:064x}"


def _proxy(proxy_index: int, implementation_index: int) -> dict:
    return {
        "proxy": _address(proxy_index),
        "implementation": _address(implementation_index),
        "validFromBlock": 100,
        "implementationValidFromBlock": 100,
        "implementationCodehash": _bytes32(implementation_index),
    }


def _immutable(index: int) -> dict:
    return {
        "address": _address(index),
        "validFromBlock": 100,
        "codehash": _bytes32(index),
    }


def _manifest() -> tuple[dict, dict[WheelSignerRole, str]]:
    role_indexes = {
        "admin": 1,
        "upgrader": 2,
        "accounting": 3,
        "allocator": 4,
        "processor": 5,
        "curator": 6,
        "guardian": 7,
    }
    private_keys = {
        WheelSignerRole.ALLOCATOR: _private_key(role_indexes["allocator"]),
        WheelSignerRole.PROCESSOR: _private_key(role_indexes["processor"]),
        WheelSignerRole.CURATOR: _private_key(role_indexes["curator"]),
        WheelSignerRole.GUARDIAN: _private_key(role_indexes["guardian"]),
    }
    return (
        {
            "schemaVersion": "1.0.0",
            "issue": "B1N-419",
            "status": "CONFIRMED_CANONICAL_RECEIPTS",
            "deploymentStatus": "DEPLOYED",
            "handoffReady": True,
            "sourceCommit": "a" * 40,
            "deploymentId": _bytes32(1),
            "network": {
                "name": "base-sepolia",
                "chainId": 84532,
                "deploymentBlocks": {"fundFirst": 100, "fundLast": 101},
            },
            "readiness": {
                "canonicalReceiptsRecorded": True,
                "blockscoutVerificationComplete": True,
                "bootstrapReconciled": True,
                "finalRolesReconciled": True,
                "standaloneBaselinesUnchanged": True,
                "backendHandoffReady": True,
                "mainnetAuthorized": False,
            },
            "canonicalReceipts": [
                {
                    "transactionHash": "0x" + "11" * 32,
                    "blockHash": "0x" + "22" * 32,
                    "blockNumber": 100,
                    "status": 1,
                },
                {
                    "transactionHash": "0x" + "33" * 32,
                    "blockHash": "0x" + "44" * 32,
                    "blockNumber": 101,
                    "status": 1,
                },
            ],
            "finalRoles": {
                role: _account_address(index) for role, index in role_indexes.items()
            },
            "assets": {
                "usdc": _address(10),
                "weth": _address(14),
                "swapRouter": _address(20),
            },
            "contracts": {
                "fundVault": _proxy(11, 41),
                "strategyManager": _proxy(12, 42),
                "wheelCoordinator": _proxy(13, 43),
                "fundAccounting": _proxy(15, 45),
                "fundFlowManager": _proxy(16, 46),
                "fundShare": _proxy(17, 47),
                "claimEscrow": _immutable(48),
                "accessManager": _immutable(49),
                "metaWheelValuator": _immutable(50),
                "navReportVerifier": _immutable(51),
            },
            "v1Boundary": {
                "addressBook": {"proxy": _address(60), "unchanged": True},
                "controller": {
                    "proxy": _address(61),
                    "implementation": _address(71),
                    "unchanged": True,
                },
                "batchSettler": {
                    "proxy": _address(18),
                    "implementation": _address(72),
                    "unchanged": True,
                },
                "marginPool": {"proxy": _address(62), "unchanged": True},
                "oracle": {"proxy": _address(19), "unchanged": True},
                "oTokenFactory": {"proxy": _address(63), "unchanged": True},
                "whitelist": {"proxy": _address(64), "unchanged": True},
            },
            "policy": {
                "policyHash": "db47fcd1f4f96b656fe462956c85194b1f5e25d0c1d8c8862864b256d38fa93c",
                "premiumFeeBps": 1_000,
                "managementFeeWad": 20_000_000_000_000_000,
                "performanceFeeBps": 1_000,
            },
            "linkedLibraries": [_address(index) for index in range(80, 85)],
            "linkedLibraryCodehashes": [_bytes32(index) for index in range(80, 85)],
            "standaloneBaselines": {
                name: {
                    "proxy": _address(index),
                    "implementation": _address(index + 10),
                    "implementationCodehash": _bytes32(index + 10),
                    "unchanged": True,
                }
                for name, index in {
                    "cspVault": 90,
                    "cspAdapter": 91,
                    "coveredCallVault": 92,
                    "coveredCallAdapter": 93,
                }.items()
            },
        },
        private_keys,
    )


def _write_manifest(tmp_path, manifest: dict):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _gate(tmp_path, manifest: dict):
    path, digest = _write_manifest(tmp_path, manifest)
    return load_wheel_manifest_gate(
        path=path,
        approved_sha256=digest,
        configured_parent=manifest["contracts"]["fundVault"]["proxy"],
        configured_strategy_manager=manifest["contracts"]["strategyManager"]["proxy"],
        configured_coordinator=manifest["contracts"]["wheelCoordinator"]["proxy"],
        configured_usdc=manifest["assets"]["usdc"],
    )


def _automated_signers(gate, manifest, private_keys):
    roles = (WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR)
    return WheelRoleSigners.build_automated(
        gate,
        private_keys={role: private_keys[role] for role in roles},
        declared_addresses={role: manifest["finalRoles"][role.value] for role in roles},
    )


def test_final_manifest_and_two_automated_signers_are_bound(tmp_path):
    manifest, private_keys = _manifest()

    gate = _gate(tmp_path, manifest)
    signers = _automated_signers(gate, manifest, private_keys)

    assert tuple(gate.final_roles) == REQUIRED_FINAL_ROLES
    assert len({value.lower() for value in gate.final_roles.values()}) == 7
    assert signers.allocator.address == manifest["finalRoles"]["allocator"]
    assert all(key not in repr(signers) for key in private_keys.values())


@pytest.mark.parametrize(
    ("wrapper", "role"),
    (
        (StrategyManagerWrapper.ALLOCATION, WheelSignerRole.ALLOCATOR),
        (StrategyManagerWrapper.PROCESSING, WheelSignerRole.PROCESSOR),
    ),
)
def test_automated_wrapper_selects_only_its_role_signer(tmp_path, wrapper, role):
    manifest, private_keys = _manifest()
    signers = _automated_signers(_gate(tmp_path, manifest), manifest, private_keys)

    selected = signers.for_wrapper(wrapper)

    assert selected.address == manifest["finalRoles"][role.value]


@pytest.mark.parametrize(
    ("wrapper", "role"),
    (
        (StrategyManagerWrapper.GUARDIAN, WheelSignerRole.GUARDIAN),
        (StrategyManagerWrapper.CONFIGURATION, WheelSignerRole.CURATOR),
    ),
)
def test_manual_wrapper_requires_explicit_manual_signer_set(tmp_path, wrapper, role):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    automated = _automated_signers(gate, manifest, private_keys)
    with pytest.raises(RuntimeError, match="manual tool"):
        automated.for_wrapper(wrapper)

    manual = WheelRoleSigners.build_manual(
        gate,
        private_keys={role: private_keys[role]},
        declared_addresses={role: manifest["finalRoles"][role.value]},
    )

    assert manual.for_wrapper(wrapper).address == manifest["finalRoles"][role.value]


def test_automated_and_manual_signer_boundaries_cannot_mix(tmp_path):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    all_declared = {
        role: manifest["finalRoles"][role.value] for role in WheelSignerRole
    }

    with pytest.raises(RuntimeError, match="invalid role boundary"):
        WheelRoleSigners.build_automated(
            gate,
            private_keys=private_keys,
            declared_addresses=all_declared,
        )
    with pytest.raises(RuntimeError, match="only guardian/curator"):
        WheelRoleSigners.build_manual(
            gate,
            private_keys={
                WheelSignerRole.ALLOCATOR: private_keys[WheelSignerRole.ALLOCATOR]
            },
            declared_addresses={
                WheelSignerRole.ALLOCATOR: all_declared[WheelSignerRole.ALLOCATOR]
            },
        )


def test_manifest_fails_closed_before_deployed_handoff(tmp_path):
    manifest, _ = _manifest()

    for changes in (
        {"deploymentStatus": "NOT_DEPLOYED"},
        {"handoffReady": False},
        {"status": "UNCONFIRMED_REQUIRES_CANONICAL_RECEIPTS"},
    ):
        candidate = manifest | changes
        with pytest.raises(RuntimeError, match="not a final handoff"):
            _gate(tmp_path, candidate)


@pytest.mark.parametrize(
    "field",
    (
        "canonicalReceiptsRecorded",
        "blockscoutVerificationComplete",
        "bootstrapReconciled",
        "finalRolesReconciled",
        "standaloneBaselinesUnchanged",
        "backendHandoffReady",
    ),
)
def test_manifest_requires_every_readiness_attestation(tmp_path, field):
    manifest, _ = _manifest()
    manifest["readiness"] = manifest["readiness"] | {field: False}

    with pytest.raises(RuntimeError, match="readiness is incomplete"):
        _gate(tmp_path, manifest)


def test_manifest_requires_source_deployment_and_two_receipts(tmp_path):
    manifest, _ = _manifest()
    for field, value, message in (
        ("sourceCommit", "not-a-commit", "source commit"),
        ("deploymentId", "0x" + "00" * 32, "deploymentId"),
        ("canonicalReceipts", manifest["canonicalReceipts"][:1], "receipts"),
    ):
        candidate = manifest | {field: value}
        with pytest.raises(RuntimeError, match=message):
            _gate(tmp_path, candidate)


def test_manifest_rejects_missing_or_duplicate_final_roles(tmp_path):
    manifest, _ = _manifest()
    missing = dict(manifest)
    missing["finalRoles"] = dict(manifest["finalRoles"])
    missing["finalRoles"].pop("guardian")
    with pytest.raises(RuntimeError, match="seven final roles"):
        _gate(tmp_path, missing)

    duplicated = dict(manifest)
    duplicated["finalRoles"] = dict(manifest["finalRoles"])
    duplicated["finalRoles"]["guardian"] = duplicated["finalRoles"]["curator"]
    with pytest.raises(RuntimeError, match="seven distinct"):
        _gate(tmp_path, duplicated)


def test_signer_must_match_declared_address_and_final_role(tmp_path):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    declared = {role: manifest["finalRoles"][role.value] for role in WheelSignerRole}
    declared[WheelSignerRole.PROCESSOR] = manifest["finalRoles"]["guardian"]

    with pytest.raises(RuntimeError, match="processor signer does not match"):
        WheelRoleSigners.build_automated(
            gate,
            private_keys={
                role: private_keys[role]
                for role in (WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR)
            },
            declared_addresses={
                role: declared[role]
                for role in (WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR)
            },
        )


def test_invalid_private_key_is_never_exposed_by_error(tmp_path):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    secret = "not-a-valid-secret-key"
    invalid_keys = {
        role: private_keys[role]
        for role in (WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR)
    }
    invalid_keys[WheelSignerRole.ALLOCATOR] = secret

    with pytest.raises(RuntimeError, match="allocator signer") as captured:
        WheelRoleSigners.build_automated(
            gate,
            private_keys=invalid_keys,
            declared_addresses={
                role: manifest["finalRoles"][role.value]
                for role in (WheelSignerRole.ALLOCATOR, WheelSignerRole.PROCESSOR)
            },
        )

    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


def test_chain_port_uses_processor_for_processing_and_allocator_for_queue(tmp_path):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    signers = _automated_signers(gate, manifest, private_keys)
    w3 = MagicMock()
    w3.eth.chain_id = 84532
    w3.eth.get_code.return_value = b"code"
    strategy = MagicMock()
    submissions = []

    def send(**values):
        submissions.append(values)
        return ConfirmedTransaction("0xtx", 9, 100, "0xblock", False)

    port = Web3MetaWheelChainPort(
        manifest=gate,
        signers=signers,
        snapshot_reader=MagicMock(),
        quote_reader=MagicMock(),
        reconciler=MagicMock(),
        w3=w3,
        strategy_contract=strategy,
        transaction_sender=send,
    )
    processing = WheelAction(
        kind=ActionKind.SETTLE_CSP,
        chain_id=84532,
        parent=gate.parent,
        lane=_address(14),
        tranche_id=1,
        transition_nonce=2,
        child_position_id=3,
    )
    request = managed_operation_for_action(processing)

    port.submit(processing, MagicMock(), request)

    assert submissions[-1]["account"].address == gate.final_roles["processor"]
    strategy.functions.executeAdapterProcessingOperation.assert_called_once_with(
        gate.coordinator, request.data
    )

    queue = WheelAction(
        kind=ActionKind.QUEUE_CSP_USDC,
        chain_id=84532,
        parent=gate.parent,
        lane=gate.coordinator,
        tranche_id=0,
        transition_nonce=3,
        child_position_id=0,
        amount=1_000 * 10**6,
    )
    port.submit(queue, MagicMock(), None)

    assert submissions[-1]["account"].address == gate.final_roles["allocator"]
    strategy.functions.allocate.assert_called_once_with(
        gate.coordinator,
        gate.usdc,
        queue.amount,
        bytes.fromhex(queue.key),
    )


def test_chain_port_rejects_wrapper_or_payload_substitution(tmp_path):
    manifest, private_keys = _manifest()
    gate = _gate(tmp_path, manifest)
    w3 = MagicMock()
    w3.eth.chain_id = 84532
    w3.eth.get_code.return_value = b"code"
    port = Web3MetaWheelChainPort(
        manifest=gate,
        signers=_automated_signers(gate, manifest, private_keys),
        snapshot_reader=MagicMock(),
        quote_reader=MagicMock(),
        reconciler=MagicMock(),
        w3=w3,
        strategy_contract=MagicMock(),
        transaction_sender=MagicMock(),
    )
    action = WheelAction(
        kind=ActionKind.SETTLE_CSP,
        chain_id=84532,
        parent=gate.parent,
        lane=_address(14),
        tranche_id=1,
        transition_nonce=2,
        child_position_id=3,
    )
    substituted = encode_managed_operation(ManagedOperation.HANDOFF_CSP, 1)

    with pytest.raises(RuntimeError, match="changed before signing"):
        port.submit(action, MagicMock(), substituted)

    guardian_request = encode_managed_operation(ManagedOperation.PAUSE_ALLOCATIONS)
    with pytest.raises(RuntimeError, match="explicit manual tool"):
        port.submit(action, MagicMock(), guardian_request)


def test_runtime_anchors_manifest_receipts_proxies_and_codehashes(tmp_path):
    manifest, _ = _manifest()
    gate = _gate(tmp_path, manifest)
    code_by_address = {
        binding.address: f"code:{binding.label}".encode()
        for binding in gate.code_bindings
    }
    gate = replace(
        gate,
        code_bindings=tuple(
            replace(
                binding,
                codehash="0x" + Web3.keccak(code_by_address[binding.address]).hex(),
            )
            for binding in gate.code_bindings
        ),
    )
    runtime = object.__new__(BaseSepoliaMetaWheelRuntime)
    runtime.manifest = gate
    runtime.w3 = MagicMock()
    runtime.w3.eth.block_number = 105
    receipts = {
        expected.transaction_hash: SimpleNamespace(
            status=1,
            blockNumber=expected.block_number,
            blockHash=HexBytes(expected.block_hash),
        )
        for expected in gate.canonical_receipts
    }
    blocks = {
        expected.block_number: SimpleNamespace(hash=HexBytes(expected.block_hash))
        for expected in gate.canonical_receipts
    }
    runtime.w3.eth.get_transaction_receipt.side_effect = receipts.__getitem__
    runtime.w3.eth.get_block.side_effect = blocks.__getitem__
    implementation_by_proxy = {
        binding.proxy: bytes.fromhex(binding.implementation[2:]).rjust(32, b"\0")
        for binding in gate.proxy_bindings
    }
    runtime.w3.eth.get_storage_at.side_effect = lambda proxy, slot, **_: (
        implementation_by_proxy[proxy] if slot == EIP1967_IMPLEMENTATION_SLOT else b""
    )
    runtime.w3.eth.get_code.side_effect = lambda address, **_: code_by_address[address]

    runtime._verify_manifest_chain()

    first = gate.proxy_bindings[0]
    implementation_by_proxy[first.proxy] = b"\0" * 32
    with pytest.raises(RuntimeError, match="proxy implementation changed"):
        runtime._verify_manifest_chain()
