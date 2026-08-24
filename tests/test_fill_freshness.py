import builtins
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import hedge_executor, main, position_tracker as tracker_module
from src.position_tracker import PositionTracker
from src.snapshot_consumer import SnapshotUnavailable


FILL = {"tx_hash": "0xfill", "otoken_address": "0xotoken"}


def _fill_runtime(monkeypatch):
    tracker = MagicMock()
    tracker.positions = []
    monkeypatch.setattr(main, "_tracker", tracker)
    monkeypatch.setattr(main, "_seen_tx_hashes", set())
    monkeypatch.setattr(main, "_resolve_underlying", lambda _: ("eth", "ETH", "base"))
    monkeypatch.setattr(
        main,
        "_get_market",
        MagicMock(side_effect=AssertionError("stale Base market cache used")),
    )
    asset = SimpleNamespace(name="eth", hedge_symbol="ETH")
    monkeypatch.setattr(main, "_asset_map_for_chain", lambda _: {"eth": asset})
    monkeypatch.setattr(main, "_asset_is_hedge_ready", lambda *_: True)
    return tracker


def test_rest_fill_fetch_revalidates_before_dispatch(monkeypatch):
    monkeypatch.setattr(
        main, "_market", {"base/eth": main.MarketSnapshot(spot=2_000, iv=0.6)}
    )
    monkeypatch.setattr(main.api_client, "get_fills", MagicMock(return_value=[FILL]))
    handle = MagicMock()
    monkeypatch.setattr(main, "_handle_fill", handle)
    snapshots = MagicMock()
    snapshots.require.side_effect = SnapshotUnavailable("generation changed")

    with pytest.raises(SnapshotUnavailable, match="generation changed"):
        main._poll_fills_rest(object(), snapshots)

    handle.assert_not_called()


def test_websocket_fill_requires_current_snapshot_before_tracking(monkeypatch):
    tracker = _fill_runtime(monkeypatch)
    snapshots = MagicMock()
    snapshots.current.side_effect = SnapshotUnavailable("snapshot absent")

    with pytest.raises(SnapshotUnavailable, match="snapshot absent"):
        main._handle_fill(FILL, snapshot_consumer=snapshots)

    tracker.add_position.assert_not_called()
    tracker.rebalance_hedge.assert_not_called()


def test_websocket_fill_revalidates_immediately_before_position_mutation(monkeypatch):
    tracker = _fill_runtime(monkeypatch)
    snapshots = MagicMock()
    bundle = MagicMock()
    bundle.market.return_value = {"spot": 2_000, "iv": 0.6}
    snapshots.current.return_value = bundle
    snapshots.require.side_effect = [
        None,
        SnapshotUnavailable("superseded before tracking"),
    ]

    main._handle_fill(FILL, snapshot_consumer=snapshots)

    tracker.add_position.assert_not_called()
    tracker.rebalance_hedge.assert_not_called()
    assert FILL["tx_hash"] not in main._seen_tx_hashes


def test_generation_change_during_position_calculation_prevents_tracking(
    monkeypatch,
):
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xotoken",
                "strike_price": 1_800,
                "expiry": 2_000_000_000,
                "is_put": True,
            }
        ],
        underlying="eth",
        chain="base",
    )
    generation_current = True

    def invalidate_generation(*_args, **_kwargs):
        nonlocal generation_current
        generation_current = False
        return 0.0

    monkeypatch.setattr(tracker_module, "bs_theta", invalidate_generation)
    trade_log = MagicMock()
    monkeypatch.setattr(tracker_module.trade_logger, "log_position_opened", trade_log)

    def require_generation():
        if not generation_current:
            raise SnapshotUnavailable("generation changed during pricing")

    with pytest.raises(SnapshotUnavailable, match="during pricing"):
        tracker.add_position(
            {
                "tx_hash": "0xfill",
                "otoken_address": "0xotoken",
                "amount": 100_000_000,
                "gross_premium": 1_000_000,
            },
            2_000,
            0.6,
            0.05,
            decision_validator=require_generation,
        )

    assert tracker.positions == []
    trade_log.assert_not_called()


def test_invalidation_between_append_and_trade_log_rolls_back_position(monkeypatch):
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xotoken",
                "strike_price": 1_800,
                "expiry": 2_000_000_000,
                "is_put": True,
            }
        ]
    )
    trade_log = MagicMock()
    monkeypatch.setattr(tracker_module.trade_logger, "log_position_opened", trade_log)
    validations = 0

    def require_generation():
        nonlocal validations
        validations += 1
        if validations == 2:
            raise SnapshotUnavailable("changed before trade log")

    with pytest.raises(SnapshotUnavailable, match="before trade log"):
        tracker.add_position(
            FILL | {"amount": 100_000_000, "gross_premium": 1_000_000},
            2_000,
            0.6,
            0.05,
            decision_validator=require_generation,
        )

    assert tracker.positions == []
    trade_log.assert_not_called()


def test_trade_logging_failure_rolls_back_and_retry_adds_once(monkeypatch):
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xotoken",
                "strike_price": 1_800,
                "expiry": 2_000_000_000,
                "is_put": True,
            }
        ]
    )
    trade_log = MagicMock(side_effect=[RuntimeError("log unavailable"), None])
    monkeypatch.setattr(tracker_module.trade_logger, "log_position_opened", trade_log)
    fill = FILL | {"amount": 100_000_000, "gross_premium": 1_000_000}

    with pytest.raises(RuntimeError, match="log unavailable"):
        tracker.add_position(fill, 2_000, 0.6, 0.05)
    assert tracker.positions == []

    position = tracker.add_position(fill, 2_000, 0.6, 0.05)
    assert tracker.positions == [position]
    assert trade_log.call_count == 2


def test_actual_local_trade_log_failure_rolls_back_position(monkeypatch):
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xotoken",
                "strike_price": 1_800,
                "expiry": 2_000_000_000,
                "is_put": True,
            }
        ]
    )
    monkeypatch.setattr(
        builtins,
        "open",
        MagicMock(side_effect=PermissionError("trade log is read-only")),
    )

    with pytest.raises(PermissionError, match="read-only"):
        tracker.add_position(
            FILL | {"amount": 100_000_000, "gross_premium": 1_000_000},
            2_000,
            0.6,
            0.05,
        )

    assert tracker.positions == []


def test_generation_change_during_live_hedge_read_prevents_external_hedge(
    monkeypatch,
):
    tracker = PositionTracker()
    monkeypatch.setattr(tracker, "net_delta", MagicMock(return_value=1.0))
    monkeypatch.setattr(main.config, "HEDGE_MODE", "live")
    generation_current = True

    def get_positions():
        nonlocal generation_current
        generation_current = False
        return []

    monkeypatch.setattr(hedge_executor, "get_positions", get_positions)
    open_hedge = MagicMock()
    monkeypatch.setattr(hedge_executor, "open_hedge", open_hedge)

    def require_generation():
        if not generation_current:
            raise SnapshotUnavailable("generation changed during hedge read")

    with pytest.raises(SnapshotUnavailable, match="during hedge read"):
        tracker.rebalance_hedge(
            2_000,
            "eth",
            "ETH",
            decision_validator=require_generation,
        )

    open_hedge.assert_not_called()


def test_live_hedge_read_and_order_failures_are_explicit(monkeypatch):
    tracker = PositionTracker()
    monkeypatch.setattr(tracker, "net_delta", MagicMock(return_value=1.0))
    monkeypatch.setattr(main.config, "HEDGE_MODE", "live")
    open_hedge = MagicMock(return_value=None)
    monkeypatch.setattr(hedge_executor, "open_hedge", open_hedge)
    monkeypatch.setattr(
        hedge_executor,
        "get_positions",
        MagicMock(side_effect=RuntimeError("venue unavailable")),
    )

    with pytest.raises(RuntimeError, match="Failed to read live hedge positions"):
        tracker.rebalance_hedge(2_000, "eth", "ETH")
    open_hedge.assert_not_called()

    monkeypatch.setattr(hedge_executor, "get_positions", MagicMock(return_value=[]))
    with pytest.raises(RuntimeError, match="Live hedge order failed"):
        tracker.rebalance_hedge(2_000, "eth", "ETH")
    open_hedge.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("live position read failed"),
        RuntimeError("live order failed"),
    ),
)
def test_websocket_live_hedge_failure_remains_retryable(monkeypatch, failure):
    tracker = _fill_runtime(monkeypatch)
    position = SimpleNamespace(tx_hash=FILL["tx_hash"])
    tracker.positions = [position]
    tracker.rebalance_hedge.side_effect = [failure, None]
    snapshots = MagicMock()
    bundle = MagicMock()
    bundle.market.return_value = {"spot": 2_000, "iv": 0.6}
    snapshots.current.return_value = bundle

    main._handle_fill(FILL, snapshot_consumer=snapshots)
    assert FILL["tx_hash"] not in main._seen_tx_hashes

    main._handle_fill(FILL, snapshot_consumer=snapshots)
    assert tracker.rebalance_hedge.call_count == 2
    assert FILL["tx_hash"] in main._seen_tx_hashes


def test_websocket_fill_retries_existing_position_when_generation_changes_before_hedge(
    monkeypatch,
):
    tracker = _fill_runtime(monkeypatch)
    snapshots = MagicMock()
    bundle = MagicMock()
    bundle.market.return_value = {"spot": 2_000, "iv": 0.6}
    snapshots.current.return_value = bundle
    position = SimpleNamespace(tx_hash=FILL["tx_hash"])

    def add_position(*_args, **_kwargs):
        tracker.positions.append(position)
        return position

    tracker.add_position.side_effect = add_position
    external_hedge = MagicMock()

    def rebalance(*_args, decision_validator, **_kwargs):
        decision_validator()
        external_hedge()

    tracker.rebalance_hedge.side_effect = rebalance
    snapshots.require.side_effect = [
        None,
        None,
        None,
        SnapshotUnavailable("superseded before hedge"),
    ]

    main._handle_fill(FILL, snapshot_consumer=snapshots)

    tracker.add_position.assert_called_once()
    external_hedge.assert_not_called()
    assert FILL["tx_hash"] not in main._seen_tx_hashes

    snapshots.require.side_effect = None
    snapshots.require.return_value = None
    main._handle_fill(FILL, snapshot_consumer=snapshots)

    tracker.add_position.assert_called_once()
    external_hedge.assert_called_once()
    assert FILL["tx_hash"] in main._seen_tx_hashes
