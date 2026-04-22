"""Tests for explicit environment gating in config."""

import importlib
import os
import sys

import pytest

import src.config as config_module


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
