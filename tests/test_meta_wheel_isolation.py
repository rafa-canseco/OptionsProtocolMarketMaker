from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config, meta_wheel_allocator
from src.covered_call_allocator import (
    call_collateral_target,
    load_covered_call_policy,
)
from src.fund_allocator import liquid_collateral_target, load_testnet_policy


@pytest.fixture(autouse=True)
def _enable_v2_snapshots(monkeypatch):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", True)


def _clear_railway_environment(monkeypatch):
    for name in (
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_DEPLOYMENT_ID",
        "RAILWAY_VOLUME_MOUNT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_wheel_does_not_start_or_change_standalone_planning(monkeypatch):
    csp = load_testnet_policy("policies/csp_fund_policy.v4.base-sepolia.json")
    covered_call = load_covered_call_policy(
        "policies/covered_call_fund_policy.v5.base-sepolia.json"
    )
    csp_before = liquid_collateral_target(1_000 * 10**6, csp)
    call_before = call_collateral_target(10**18, covered_call)
    monkeypatch.setattr(config, "META_WHEEL_ALLOCATOR_ENABLED", False)
    monkeypatch.setattr(
        meta_wheel_allocator,
        "_chain_port_factory",
        lambda: (_ for _ in ()).throw(AssertionError("Wheel port must stay unused")),
    )

    assert meta_wheel_allocator.start() is None
    assert liquid_collateral_target(1_000 * 10**6, csp) == csp_before
    assert call_collateral_target(10**18, covered_call) == call_before


def test_enabled_wheel_cannot_reach_factory_without_final_manifest(monkeypatch):
    monkeypatch.setattr(config, "META_WHEEL_ALLOCATOR_ENABLED", True)
    monkeypatch.setattr(config, "META_WHEEL_DEPLOYMENT_MANIFEST_PATH", None)
    monkeypatch.setattr(
        meta_wheel_allocator,
        "_chain_port_factory",
        lambda: (_ for _ in ()).throw(AssertionError("factory must remain gated")),
    )

    with pytest.raises(RuntimeError, match="activation configuration"):
        meta_wheel_allocator.start()


def test_local_journal_accepts_only_explicit_absolute_durable_root(
    monkeypatch, tmp_path
):
    _clear_railway_environment(monkeypatch)
    root = tmp_path / "wheel-volume"
    root.mkdir()
    journal = root / "actions.sqlite3"
    monkeypatch.setattr(config, "META_WHEEL_PERSISTENT_ROOT", str(root))
    monkeypatch.setattr(config, "META_WHEEL_ACTION_JOURNAL_PATH", str(journal))

    assert meta_wheel_allocator.persistent_action_journal_path() == journal

    monkeypatch.setattr(config, "META_WHEEL_ACTION_JOURNAL_PATH", "actions.sqlite3")
    with pytest.raises(RuntimeError, match="absolute"):
        meta_wheel_allocator.persistent_action_journal_path()


@pytest.mark.parametrize("mount", (None, "mismatch"))
def test_production_railway_journal_fails_closed_without_matching_volume(
    monkeypatch, tmp_path, mount
):
    _clear_railway_environment(monkeypatch)
    root = tmp_path / "wheel-volume"
    root.mkdir()
    journal = root / "actions.sqlite3"
    monkeypatch.setattr(config, "META_WHEEL_PERSISTENT_ROOT", str(root))
    monkeypatch.setattr(config, "META_WHEEL_ACTION_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    if mount == "mismatch":
        other = tmp_path / "ephemeral"
        other.mkdir()
        monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(other))

    with pytest.raises(RuntimeError, match="RAILWAY_VOLUME_MOUNT_PATH"):
        meta_wheel_allocator.persistent_action_journal_path()


def test_start_fails_before_chain_factory_without_railway_volume(monkeypatch, tmp_path):
    _clear_railway_environment(monkeypatch)
    root = tmp_path / "wheel-volume"
    root.mkdir()
    monkeypatch.setattr(config, "META_WHEEL_ALLOCATOR_ENABLED", True)
    monkeypatch.setattr(config, "META_WHEEL_PERSISTENT_ROOT", str(root))
    monkeypatch.setattr(
        config, "META_WHEEL_ACTION_JOURNAL_PATH", str(root / "actions.sqlite3")
    )
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setattr(
        "src.meta_wheel_chain.load_runtime_gate_and_signers", MagicMock()
    )
    monkeypatch.setattr(
        meta_wheel_allocator,
        "_chain_port_factory",
        lambda: (_ for _ in ()).throw(AssertionError("factory must remain gated")),
    )

    with pytest.raises(RuntimeError, match="RAILWAY_VOLUME_MOUNT_PATH"):
        meta_wheel_allocator.start()


def test_start_uses_installed_factory_and_persistent_journal(monkeypatch, tmp_path):
    _clear_railway_environment(monkeypatch)
    root = tmp_path / "wheel-volume"
    root.mkdir()
    journal = root / "actions.sqlite3"
    chain = MagicMock()
    captured = {}

    class CapturingAllocator:
        def __init__(self, **values):
            captured.update(values)

        def run_forever(self):
            return None

    class InlineThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(config, "META_WHEEL_ALLOCATOR_ENABLED", True)
    monkeypatch.setattr(config, "META_WHEEL_PERSISTENT_ROOT", str(root))
    monkeypatch.setattr(config, "META_WHEEL_ACTION_JOURNAL_PATH", str(journal))
    monkeypatch.setattr(
        "src.meta_wheel_chain.load_runtime_gate_and_signers", MagicMock()
    )
    monkeypatch.setattr(meta_wheel_allocator, "MetaWheelAllocator", CapturingAllocator)
    monkeypatch.setattr(meta_wheel_allocator.threading, "Thread", InlineThread)
    monkeypatch.setattr(meta_wheel_allocator, "_chain_port_factory", lambda: chain)

    thread = meta_wheel_allocator.start()

    assert thread.name == "meta-wheel-allocator"
    assert captured["chain"] is chain
    assert isinstance(captured["journal"], meta_wheel_allocator.SqliteActionJournal)
    database_path = (
        captured["journal"].connection.execute("PRAGMA database_list").fetchone()[2]
    )
    assert Path(database_path) == journal
    captured["journal"].close()
