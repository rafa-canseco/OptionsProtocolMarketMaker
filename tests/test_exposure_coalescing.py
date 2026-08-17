"""Regression tests for cycle-scoped exposure reads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import main as mm_main


def _configure_cycle(monkeypatch, asset_names: tuple[str, ...]) -> tuple[object, ...]:
    assets = tuple(
        SimpleNamespace(name=name, hedge_symbol=name.upper()) for name in asset_names
    )
    chain = SimpleNamespace(name="base", assets=assets)
    monkeypatch.setattr(mm_main.config, "CHAINS", (chain,))
    monkeypatch.setattr(mm_main.config, "ASSETS", ())
    monkeypatch.setattr(mm_main.api_client, "delete_quotes", Mock(return_value={}))
    monkeypatch.setattr(mm_main, "_poll_fills_rest", Mock())
    return assets


def test_single_asset_cycle_reads_exposure_once(monkeypatch):
    assets = _configure_cycle(monkeypatch, ("eth",))
    exposure = {"total_premium_earned": "12.50"}
    get_exposure = Mock(return_value=exposure)
    run_asset = Mock()
    monkeypatch.setattr(mm_main.api_client, "get_exposure", get_exposure)
    monkeypatch.setattr(mm_main, "_run_asset_cycle", run_asset)

    result = mm_main.run_cycle(Mock(), {}, "0xmaker")

    assert result is exposure
    get_exposure.assert_called_once_with()
    assert run_asset.call_count == len(assets)
    assert run_asset.call_args.kwargs["exposure_snapshot"] is exposure


def test_multi_asset_cycle_and_monitor_share_one_snapshot(monkeypatch):
    assets = _configure_cycle(monkeypatch, ("eth", "btc", "sol"))
    first_exposure = {"total_premium_earned": "12.50"}
    second_exposure = {"total_premium_earned": "14.00"}
    get_exposure = Mock(side_effect=(first_exposure, second_exposure))
    snapshots_seen = []

    def run_asset(**kwargs):
        snapshots_seen.append(kwargs["exposure_snapshot"])

    monkeypatch.setattr(mm_main.api_client, "get_exposure", get_exposure)
    monkeypatch.setattr(mm_main, "_run_asset_cycle", run_asset)
    monkeypatch.setattr(mm_main.fill_listener, "is_connected", Mock(return_value=True))
    monkeypatch.setattr(
        mm_main.fill_listener, "get_recent_fills", Mock(return_value=[])
    )

    first_result = mm_main.run_cycle(Mock(), {}, "0xmaker")
    mm_main.log_monitoring(first_result)

    assert get_exposure.call_count == 1
    assert snapshots_seen == [first_exposure] * len(assets)

    second_result = mm_main.run_cycle(Mock(), {}, "0xmaker")

    assert get_exposure.call_count == 2
    assert second_result is second_exposure
    assert snapshots_seen[len(assets) :] == [second_exposure] * len(assets)


def test_exposure_failure_never_fabricates_snapshot_or_bypasses_capacity_gate(
    monkeypatch,
):
    assets = _configure_cycle(monkeypatch, ("eth", "btc"))
    get_exposure = Mock(side_effect=RuntimeError("backend unavailable"))
    run_asset = Mock()
    monkeypatch.setattr(mm_main.api_client, "get_exposure", get_exposure)
    monkeypatch.setattr(mm_main, "_run_asset_cycle", run_asset)

    result = mm_main.run_cycle(Mock(), {}, "0xmaker")

    assert result is None
    get_exposure.assert_called_once_with()
    assert run_asset.call_count == len(assets)
    assert all(
        call.kwargs["exposure_snapshot"] is None for call in run_asset.call_args_list
    )

    asset = SimpleNamespace(name="eth")
    calculate_capacity = Mock(return_value=None)
    submit_quotes = Mock()
    monkeypatch.setattr(
        mm_main,
        "_calculate_and_report_capacity",
        calculate_capacity,
    )
    monkeypatch.setattr(mm_main.api_client, "submit_quotes", submit_quotes)

    mm_main._quote_and_submit(Mock(), {}, "0xmaker", {}, asset, "base")

    calculate_capacity.assert_called_once()
    submit_quotes.assert_not_called()


@pytest.mark.parametrize(
    "exposure",
    [
        {},
        {"total_premium_earned": None},
        {"total_premium_earned": ""},
        {"total_premium_earned": "not-a-number"},
        {"total_premium_earned": float("nan")},
        {"total_premium_earned": float("inf")},
        {"total_premium_earned": "-inf"},
        {"total_premium_earned": True},
        [],
    ],
)
def test_malformed_exposure_is_rejected(monkeypatch, exposure):
    assets = _configure_cycle(monkeypatch, ("eth", "btc"))
    get_exposure = Mock(return_value=exposure)
    run_asset = Mock()
    monkeypatch.setattr(mm_main.api_client, "get_exposure", get_exposure)
    monkeypatch.setattr(mm_main, "_run_asset_cycle", run_asset)

    result = mm_main.run_cycle(Mock(), {}, "0xmaker")

    assert result is None
    get_exposure.assert_called_once_with()
    assert run_asset.call_count == len(assets)
    assert all(
        call.kwargs["exposure_snapshot"] is None for call in run_asset.call_args_list
    )


def test_missing_exposure_skips_capacity_telemetry(monkeypatch):
    asset = SimpleNamespace(name="eth", hedge_symbol="ETH")
    account_value = Mock()
    log_snapshot = Mock()
    monkeypatch.setattr(mm_main.hedge_executor, "get_account_value", account_value)
    monkeypatch.setattr(mm_main.trade_logger, "log_capacity_snapshot", log_snapshot)

    mm_main._log_capacity_snapshot(asset, "base", None)

    account_value.assert_not_called()
    log_snapshot.assert_not_called()
