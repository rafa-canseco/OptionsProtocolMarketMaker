from unittest.mock import MagicMock

import pytest

from src import config
from src.fund_operations_keeper import (
    CspFundOperationsKeeper,
    bounded_redeem_shares,
    marginal_exit_cost,
)


def _batch(*, pending=0, sealed=False, processing=False):
    values = [0] * 22
    values[0] = pending
    values[18] = sealed
    values[19] = processing
    return tuple(values)


def _nav(
    *, net_assets=1_000_000_000, base_exit_cost=0, valid_after=90, valid_until=200
):
    values = [0] * 15
    values[2] = net_assets
    values[4] = base_exit_cost
    values[6] = valid_after
    values[7] = valid_until
    values[9] = 7
    values[11] = b"\x11" * 32
    return tuple(values)


def _keeper(batch):
    keeper = CspFundOperationsKeeper.__new__(CspFundOperationsKeeper)
    keeper.flow = MagicMock()
    keeper.vault = MagicMock()
    keeper._safe_block = MagicMock(return_value=(100, 102))
    keeper._send = MagicMock(return_value="0xabc")
    keeper.flow.functions.nextProcessBatchId.return_value.call.return_value = 1
    keeper.flow.functions.batch.return_value.call.return_value = batch
    return keeper


def test_bounded_redeem_uses_only_idle_assets_and_window_capacity():
    shares = bounded_redeem_shares(
        pending_shares=500 * 10**18,
        idle_assets=200 * 10**6,
        net_assets=1_000 * 10**6,
        eligible_supply=1_000 * 10**18,
        virtual_shares=10**12,
        max_window_outflow_bps=5_000,
        window_eligible_supply=0,
        window_processed_shares=0,
    )

    assert 199 * 10**18 < shares <= 200 * 10**18
    assert (
        bounded_redeem_shares(
            pending_shares=500 * 10**18,
            idle_assets=1_000 * 10**6,
            net_assets=1_000 * 10**6,
            eligible_supply=1_000 * 10**18,
            virtual_shares=10**12,
            max_window_outflow_bps=5_000,
            window_eligible_supply=1_000 * 10**18,
            window_processed_shares=400 * 10**18,
        )
        == 100 * 10**18
    )


def test_bounded_redeem_waits_when_no_liquid_usdc_remains():
    assert (
        bounded_redeem_shares(
            pending_shares=1,
            idle_assets=0,
            net_assets=1,
            eligible_supply=1,
            virtual_shares=1,
            max_window_outflow_bps=10_000,
            window_eligible_supply=0,
            window_processed_shares=0,
        )
        == 0
    )


def test_marginal_exit_cost_matches_onchain_rounding_up():
    assert marginal_exit_cost(10, 3, 4) == 8
    assert marginal_exit_cost(0, 3, 4) == 0


def test_keeper_resumes_an_in_progress_batch_instead_of_starting_twice():
    keeper = _keeper(_batch(pending=10, sealed=True, processing=True))

    keeper.run_once()

    keeper.flow.functions.processRedeemBatch.assert_called_once_with(
        1,
        config.FUND_OPERATIONS_KEEPER_PAGE_SIZE,
    )
    keeper.flow.functions.startRedeemBatch.assert_not_called()
    keeper._send.assert_called_once()


def test_keeper_seals_the_next_open_batch_before_processing():
    keeper = _keeper(_batch(pending=10, sealed=False))
    keeper.flow.functions.openBatchId.return_value.call.return_value = 1

    keeper.run_once()

    keeper.flow.functions.sealRedeemBatch.assert_called_once_with(1)
    keeper.flow.functions.startRedeemBatch.assert_not_called()
    keeper._send.assert_called_once()


def test_keeper_starts_only_the_idle_backed_part_under_fresh_nav():
    pending = 500 * 10**18
    keeper = _keeper(_batch(pending=pending, sealed=True))
    keeper.vault.functions.activeNavWindow.return_value.call.return_value = _nav(
        base_exit_cost=10 * 10**6
    )
    keeper.vault.functions.shareSupply.return_value.call.return_value = 1_000 * 10**18
    keeper.vault.functions.accountedIdleAssets.return_value.call.return_value = (
        200 * 10**6
    )
    keeper.vault.functions.virtualShares.return_value.call.return_value = 10**12
    keeper.flow.functions.exitPolicy.return_value.call.return_value = (0, 5_000)
    keeper.flow.functions.windowOutflow.return_value.call.return_value = (
        1_000 * 10**18,
        0,
    )

    keeper.run_once()

    shares = bounded_redeem_shares(
        pending_shares=pending,
        idle_assets=200 * 10**6,
        net_assets=1_000 * 10**6,
        eligible_supply=1_000 * 10**18,
        virtual_shares=10**12,
        max_window_outflow_bps=5_000,
        window_eligible_supply=1_000 * 10**18,
        window_processed_shares=0,
    )
    exit_cost = marginal_exit_cost(10 * 10**6, shares, 1_000 * 10**18)
    keeper.flow.functions.startRedeemBatch.assert_called_once_with(
        1,
        shares,
        exit_cost,
    )
    keeper._send.assert_called_once()


def test_keeper_fails_closed_on_stale_nav_without_sending():
    keeper = _keeper(_batch(pending=10, sealed=True))
    keeper.vault.functions.activeNavWindow.return_value.call.return_value = _nav(
        valid_until=101
    )

    keeper.run_once()

    keeper._send.assert_not_called()


def test_keeper_propagates_rpc_failure_to_supervising_fail_closed_loop():
    keeper = _keeper(_batch())
    keeper.flow.functions.nextProcessBatchId.return_value.call.side_effect = OSError(
        "rpc unavailable"
    )

    with pytest.raises(OSError, match="rpc unavailable"):
        keeper.run_once()


def test_runtime_requires_separate_allocator_and_processor_keys(monkeypatch):
    key = "0x" + "22" * 32
    monkeypatch.setattr(config, "FUND_OPERATIONS_KEEPER_PRIVATE_KEY", key)
    monkeypatch.setattr(config, "FUND_ALLOCATOR_PRIVATE_KEY", key)
    monkeypatch.setattr(config, "FUND_ALLOCATOR_ENABLED", True)
    monkeypatch.setattr(config, "FUND_VAULT_ADDRESS", "0x" + "11" * 20)
    monkeypatch.setattr(config, "FUND_FLOW_MANAGER_ADDRESS", "0x" + "12" * 20)
    monkeypatch.setattr(config, "CHAIN_ID", 84532)
    monkeypatch.setattr(config, "_current_environment", lambda: "staging")

    with pytest.raises(RuntimeError, match="separate configured keys"):
        CspFundOperationsKeeper._validate_runtime_config()
