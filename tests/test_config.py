"""Tests for explicit environment gating in config."""

import importlib
import os
import sys

import pytest

import src.config as config_module

# Snapshot at import time so tests can restore a clean module state.
_ORIGINAL_CONFIG_DICT: dict[str, object] = dict(config_module.__dict__)


@pytest.fixture(autouse=True)
def _restore_config_module():
    """Undo any module-level mutations so other test files see clean state."""
    yield
    current = set(config_module.__dict__)
    original = set(_ORIGINAL_CONFIG_DICT)
    for name in current - original:
        del config_module.__dict__[name]
    for name, value in _ORIGINAL_CONFIG_DICT.items():
        config_module.__dict__[name] = value
    sys.modules["src.config"] = config_module


def _base_env() -> dict[str, str]:
    return {
        "MM_PRIVATE_KEY": "0x" + "11" * 32,
        "MM_API_KEY": "test-api-key",
        "BACKEND_URL": "https://backend.example.com",
        "RPC_URL": "https://base-rpc.example.com",
    }


def _reload_config(env: dict[str, str]):
    previous = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        return importlib.reload(config_module)
    finally:
        os.environ.clear()
        os.environ.update(previous)
        sys.modules["src.config"] = config_module


def test_solana_quotes_stay_disabled_without_explicit_flag():
    env = _base_env() | {
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is False
    assert config.SOLANA_ASSETS == []
    assert [chain.name for chain in config.CHAINS] == ["base"]


def test_solana_quotes_enable_only_with_explicit_flag():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "true",
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is True
    assert [asset.name for asset in config.SOLANA_ASSETS] == ["sol", "tslax"]
    assert [chain.name for chain in config.CHAINS] == ["base", "solana"]


def test_solana_quotes_flag_requires_private_key():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "true",
        "SOLANA_PRIVATE_KEY": "",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
    }

    with pytest.raises(SystemExit):
        _reload_config(env)


def test_solana_quotes_flag_requires_rpc_url():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "true",
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "",
    }

    with pytest.raises(SystemExit):
        _reload_config(env)


def test_solana_quotes_explicit_false_behaves_like_unset():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "false",
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is False
    assert config.SOLANA_ASSETS == []
    assert [chain.name for chain in config.CHAINS] == ["base"]


@pytest.mark.parametrize(
    "raw", ["true", "TRUE", "True", "1", "yes", "YES", "on", "ON", "  true  "]
)
def test_env_flag_accepts_canonical_truthy_values(
    raw: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_FLAG", raw)
    assert config_module._env_flag("TEST_FLAG") is True


@pytest.mark.parametrize(
    "raw", ["false", "FALSE", "0", "no", "NO", "off", "OFF", "  false  "]
)
def test_env_flag_accepts_canonical_falsy_values(
    raw: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_FLAG", raw)
    assert config_module._env_flag("TEST_FLAG", default=True) is False


def test_env_flag_empty_string_uses_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_FLAG", "")
    assert config_module._env_flag("TEST_FLAG", default=False) is False
    assert config_module._env_flag("TEST_FLAG", default=True) is True


def test_env_flag_unset_uses_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert config_module._env_flag("TEST_FLAG", default=False) is False
    assert config_module._env_flag("TEST_FLAG", default=True) is True


@pytest.mark.parametrize("raw", ["enabled", "y", "t", "ture", "2", "anything"])
def test_env_flag_rejects_unrecognized_values(
    raw: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_FLAG", raw)
    with pytest.raises(SystemExit):
        config_module._env_flag("TEST_FLAG")


def test_solana_quotes_flag_rejects_garbage_value():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "enabled",
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
    }

    with pytest.raises(SystemExit):
        _reload_config(env)
