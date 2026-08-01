import hashlib
import json
from unittest.mock import MagicMock

import pytest
from eth_account import Account
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


def _private_key(index: int) -> str:
    return f"0x{index:064x}"


def _account_address(index: int) -> str:
    return Account.from_key(_private_key(index)).address


def _address(index: int) -> str:
    return Web3.to_checksum_address(f"0x{index:040x}")


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
            "network": {"name": "base-sepolia", "chainId": 84532},
            "readiness": {
                "canonicalReceiptsRecorded": True,
                "finalRolesReconciled": True,
                "backendHandoffReady": True,
                "mainnetAuthorized": False,
            },
            "canonicalReceipts": [
                {
                    "transactionHash": "0x" + "11" * 32,
                    "blockHash": "0x" + "22" * 32,
                    "blockNumber": 100,
                    "status": 1,
                }
            ],
            "finalRoles": {
                role: _account_address(index) for role, index in role_indexes.items()
            },
            "assets": {"usdc": _address(10)},
            "contracts": {
                "fundVault": {"proxy": _address(11)},
                "strategyManager": {"proxy": _address(12)},
                "wheelCoordinator": {"proxy": _address(13)},
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
