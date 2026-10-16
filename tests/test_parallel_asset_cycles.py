import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests

from src import main


def _configure_v1_cycle(monkeypatch):
    assets = [SimpleNamespace(name="eth"), SimpleNamespace(name="cbhype")]
    monkeypatch.setattr(main.config, "V2_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(main.config, "ASSETS", [])
    monkeypatch.setattr(
        main.config,
        "CHAINS",
        [SimpleNamespace(name="base", assets=assets)],
    )
    monkeypatch.setattr(main.api_client, "delete_quotes", lambda **_kwargs: {})
    monkeypatch.setattr(main, "_poll_fills_rest", lambda *_args: None)
    monkeypatch.setattr(main, "_fetch_cycle_exposure", lambda: {})
    return assets


def test_base_rpc_timeout_is_bounded_without_hidden_retries(monkeypatch):
    monkeypatch.setattr(main.config, "BASE_RPC_TIMEOUT_SECONDS", 10.0)
    w3 = main._build_transaction_web3()
    session_manager = MagicMock()
    session_manager.make_post_request.side_effect = requests.ReadTimeout("stalled")
    w3.provider._request_session_manager = session_manager

    with pytest.raises(requests.ReadTimeout, match="stalled"):
        w3.provider._make_request("eth_call", b"{}")

    session_manager.make_post_request.assert_called_once()
    assert session_manager.make_post_request.call_args.kwargs["timeout"] == 10.0
    assert w3.provider.exception_retry_configuration is None


def test_v1_asset_cycles_run_concurrently(monkeypatch):
    assets = _configure_v1_cycle(monkeypatch)
    barrier = threading.Barrier(len(assets))
    completed = []

    def run_asset(**kwargs):
        barrier.wait(timeout=1)
        completed.append(kwargs["asset_cfg"].name)

    monkeypatch.setattr(main, "_run_asset_cycle", run_asset)

    main.run_cycle(Mock(), {}, "0xmaker")

    assert set(completed) == {"eth", "cbhype"}


def test_one_asset_failure_does_not_cancel_siblings(monkeypatch):
    _configure_v1_cycle(monkeypatch)
    completed = []

    def run_asset(**kwargs):
        name = kwargs["asset_cfg"].name
        if name == "eth":
            raise RuntimeError("isolated failure")
        completed.append(name)

    monkeypatch.setattr(main, "_run_asset_cycle", run_asset)

    main.run_cycle(Mock(), {}, "0xmaker")

    assert completed == ["cbhype"]
