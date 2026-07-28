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
        "PYTHON_DOTENV_DISABLED": "1",
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


def test_solana_quotes_default_to_legacy_enabled_outside_production():
    env = _base_env() | {
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is True
    assert [asset.name for asset in config.SOLANA_ASSETS] == ["sol", "tslax"]
    assert [chain.name for chain in config.CHAINS] == ["base", "solana"]


def test_covered_call_workers_are_disabled_by_default():
    config = _reload_config(_base_env())

    assert config.COVERED_CALL_ALLOCATOR_ENABLED is False
    assert config.COVERED_CALL_OPERATIONS_KEEPER_ENABLED is False
    assert config.COVERED_CALL_ALLOCATOR_CONFIRMATIONS == 2
    assert config.COVERED_CALL_ALLOCATOR_POLICY_PATH.endswith(
        "covered_call_fund_policy.v3.base-sepolia.json"
    )


def test_lazy_quote_ttl_defaults_leave_creation_budget_without_extending_deadline():
    config = _reload_config(_base_env())

    assert config.DEADLINE_SECONDS == 300
    assert config.MIN_LAZY_QUOTE_TTL_SECONDS == 180


def test_lazy_quote_ttl_accepts_deadline_at_backend_minimum():
    config = _reload_config(
        _base_env()
        | {
            "DEADLINE_SECONDS": "180",
            "MIN_LAZY_QUOTE_TTL_SECONDS": "180",
        }
    )

    assert config.DEADLINE_SECONDS == config.MIN_LAZY_QUOTE_TTL_SECONDS


def test_lazy_quote_ttl_rejects_deadline_below_backend_minimum():
    env = _base_env() | {
        "DEADLINE_SECONDS": "179",
        "MIN_LAZY_QUOTE_TTL_SECONDS": "180",
    }

    with pytest.raises(SystemExit):
        _reload_config(env)


def test_lazy_quote_ttl_rejects_non_positive_minimum():
    env = _base_env() | {"MIN_LAZY_QUOTE_TTL_SECONDS": "0"}

    with pytest.raises(SystemExit):
        _reload_config(env)


def test_solana_quotes_enable_only_with_explicit_flag():
    env = _base_env() | {
        "SOLANA_QUOTE_PUBLISHING_ENABLED": "true",
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
        "SOL_HEDGE_ENABLED": "true",
        "TSLAX_HEDGE_ENABLED": "false",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is True
    assert [asset.name for asset in config.SOLANA_ASSETS] == ["sol", "tslax"]
    assert config.SOLANA_ASSETS[0].hedge_enabled is True
    assert config.SOLANA_ASSETS[1].hedge_enabled is False
    assert [chain.name for chain in config.CHAINS] == ["base", "solana"]


def test_solana_quotes_default_to_disabled_in_production():
    env = _base_env() | {
        "SOLANA_PRIVATE_KEY": "base58-secret",
        "SOLANA_RPC_URL": "https://solana-rpc.example.com",
        "SOLANA_ASSETS": "sol,tslax",
        "RAILWAY_ENVIRONMENT_NAME": "production",
    }

    config = _reload_config(env)

    assert config.SOLANA_QUOTE_PUBLISHING_ENABLED is False
    assert config.SOLANA_ASSETS == []
    assert [chain.name for chain in config.CHAINS] == ["base"]


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


def test_fund_processor_uses_existing_canonical_secret_name():
    config = _reload_config(
        _base_env()
        | {
            "FUND_PROCESSOR_PRIVATE_KEY": "0x" + "22" * 32,
            "FUND_OPERATIONS_KEEPER_PRIVATE_KEY": "0x" + "33" * 32,
        }
    )

    assert config.FUND_PROCESSOR_PRIVATE_KEY == "0x" + "22" * 32


def test_fund_processor_accepts_pre_release_keeper_secret_alias():
    config = _reload_config(
        _base_env() | {"FUND_OPERATIONS_KEEPER_PRIVATE_KEY": "0x" + "33" * 32}
    )

    assert config.FUND_PROCESSOR_PRIVATE_KEY == "0x" + "33" * 32
