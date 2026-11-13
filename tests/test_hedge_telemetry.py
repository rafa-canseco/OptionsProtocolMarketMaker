import json
import threading
from unittest.mock import Mock

import pytest

from src import config, hedge_executor, trade_logger
from src.position_tracker import PositionTracker, reconcile_hedge_execution


def _tracker(net_delta=0.5):
    tracker = PositionTracker()
    tracker.net_delta = lambda underlying=None: net_delta
    return tracker


@pytest.mark.parametrize(
    ("net_delta", "current", "side", "signed_diff"),
    [
        (0.5, 0.0, "SELL", -0.5),
        (0.5, -0.2, "SELL", -0.3),
        (0.1, -0.2, "BUY", 0.1),
        (-0.5, 0.2, "BUY", 0.3),
    ],
)
def test_live_decision_persists_signed_fields_without_sync_reconciliation(
    monkeypatch, net_delta, current, side, signed_diff
):
    monkeypatch.setattr(config, "HEDGE_MODE", "live")
    monkeypatch.setattr(
        hedge_executor, "get_positions", lambda: [{"coin": "ETH", "size": current}]
    )
    monkeypatch.setattr(
        hedge_executor,
        "open_hedge",
        lambda *args: {"oid": 123, "size": abs(signed_diff), "avg_price": 2000},
    )
    from src import position_tracker

    thread = Mock()
    monkeypatch.setattr(position_tracker, "_pending_worker_started", False)
    monkeypatch.setattr(position_tracker, "_pending_records", {})
    monkeypatch.setattr(
        "src.position_tracker.threading.Thread", lambda **kwargs: thread
    )
    events = []
    monkeypatch.setattr(
        trade_logger, "log_hedge_execution", lambda record: events.append(record.copy())
    )

    _tracker(net_delta).rebalance_hedge(2000, "eth", "ETH")

    assert events[0]["execution_mode"] == "live"
    assert events[0]["side"] == side
    assert events[0]["signed_difference"] == pytest.approx(signed_diff)
    assert events[0]["target_hedge_position"] == pytest.approx(-net_delta)
    assert events[0]["status"] == "submitted"
    thread.start.assert_called_once()
    assert len(position_tracker._pending_records) == 1


def test_threshold_noop_has_no_side_or_order(monkeypatch):
    monkeypatch.setattr(config, "HEDGE_MODE", "simulate")
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    tracker = _tracker(0.0005)
    tracker._simulated_hedge["eth"] = -0.0004
    assert tracker.rebalance_hedge(2000, "eth", "ETH") is None
    event = trade_logger.log_hedge_execution.call_args.args[0]
    assert event["status"] == "noop" and event["side"] is None
    assert event["execution_mode"] == "simulate" and "order_id" not in event


def test_simulated_execution_is_explicit_and_not_observed_live(monkeypatch):
    monkeypatch.setattr(config, "HEDGE_MODE", "simulate")
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    _tracker().rebalance_hedge(2000, "eth", "ETH")
    record = trade_logger.log_hedge_execution.call_args.args[0]
    assert record["execution_mode"] == "simulate"
    assert "observed_post_position" not in record
    assert "order_id" not in record


def test_live_order_failure_and_sink_failure_do_not_change_failure_semantics(
    monkeypatch,
):
    monkeypatch.setattr(config, "HEDGE_MODE", "live")
    monkeypatch.setattr(hedge_executor, "get_positions", lambda: [])
    monkeypatch.setattr(hedge_executor, "open_hedge", lambda *args: None)
    monkeypatch.setattr(
        trade_logger, "_write_jsonl", Mock(side_effect=RuntimeError("sink"))
    )
    with pytest.raises(RuntimeError, match="Live hedge order failed"):
        _tracker().rebalance_hedge(2000, "eth", "ETH")


def test_reconciliation_aggregates_partial_fills_fees_and_signed_residual(monkeypatch):
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *args: {"status": "order", "order": {"status": "open"}},
    )
    monkeypatch.setattr(
        hedge_executor,
        "get_order_fills",
        lambda *args: [
            {"oid": 9, "sz": "0.2", "px": "10", "fee": "0.01", "feeToken": "USDC"},
            {"oid": 9, "sz": "0.3", "px": "20", "fee": "0.02", "feeToken": "USDC"},
        ],
    )
    monkeypatch.setattr(
        hedge_executor,
        "get_positions_strict",
        lambda asset: [{"coin": asset, "size": -0.4}],
    )
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    record = {
        "order_id": 9,
        "order_started_ms": 1,
        "hedge_symbol": "ETH",
        "requested_size": 1.0,
        "net_option_delta": 0.1,
    }
    assert reconcile_hedge_execution(record) is record
    assert record["status"] == "unreconciled"
    assert record["filled_size"] == pytest.approx(0.5)
    assert record["fee"] == pytest.approx(0.03)
    assert record["average_fill_price"] == pytest.approx(16)
    assert record["fill_count"] == 2 and record["residual_delta"] == pytest.approx(-0.3)


@pytest.mark.parametrize(
    ("venue", "fills", "expected"),
    [
        ("unknownOid", [], "unreconciled"),
        ("open", [], "unreconciled"),
        ("filled", [], "unreconciled"),
        ("canceled", [], "failed"),
        ("canceled", [{"sz": "0.2", "px": "10", "fee": "0"}], "partial"),
        ("filled", [{"sz": "1", "px": "10", "fee": "0"}], "filled"),
        ("filled", [{"sz": "0.2", "px": "10", "fee": "0"}], "unreconciled"),
    ],
)
def test_terminal_order_reconciliation(monkeypatch, venue, fills, expected):
    monkeypatch.setattr("src.position_tracker.time.time", lambda: 1)
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *args: {"status": "order", "order": {"status": venue}},
    )
    monkeypatch.setattr(hedge_executor, "get_order_fills", lambda *args: fills)
    monkeypatch.setattr(
        hedge_executor,
        "get_positions_strict",
        lambda asset: [{"coin": asset, "size": -0.2}],
    )
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    record = {
        "order_id": 2,
        "order_started_ms": 1,
        "hedge_symbol": "ETH",
        "requested_size": 1,
        "net_option_delta": 0.1,
        "status": "submitted",
    }
    assert reconcile_hedge_execution(record)["status"] == expected
    assert (
        record["reconcile_attempts"] == 1
        and record["venue_order_status"] == venue.lower()
    )
    if venue == "filled" and expected == "unreconciled" and fills:
        assert record["error"] == "order_fills_incomplete"


def test_get_order_status_uses_sdk_query_order_by_oid(monkeypatch):
    info = Mock()
    info.query_order_by_oid.return_value = {"status": "order"}
    monkeypatch.setattr(hedge_executor, "_info", info)
    monkeypatch.setattr(hedge_executor, "_dex_infos", {})
    monkeypatch.setattr(hedge_executor, "_address", "address")
    assert hedge_executor.get_order_status("ETH", 7) == {"status": "order"}
    info.query_order_by_oid.assert_called_once_with("address", 7)


def test_terminal_partial_is_removed_from_pending_registry(monkeypatch):
    from src import position_tracker

    class StopWorker(Exception):
        pass

    calls = 0

    def stop_after_one_pass(_seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StopWorker

    monkeypatch.setattr(position_tracker.time, "sleep", stop_after_one_pass)
    monkeypatch.setattr(
        position_tracker, "_pending_records", {"r": {"rebalance_id": "r"}}
    )
    monkeypatch.setattr(position_tracker, "_pending_lock", threading.Lock())
    monkeypatch.setattr(
        position_tracker,
        "reconcile_hedge_execution",
        lambda row: {**row, "status": "partial", "venue_order_status": "canceled"},
    )
    with pytest.raises(StopWorker):
        position_tracker._pending_worker()
    assert "r" not in position_tracker._pending_records


def test_top_level_unknown_oid_is_pending(monkeypatch):
    monkeypatch.setattr(
        hedge_executor, "get_order_status", lambda *args: {"status": "unknownOid"}
    )
    monkeypatch.setattr(hedge_executor, "get_order_fills", Mock(return_value=[]))
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    record = {"order_id": 1, "order_started_ms": 1, "hedge_symbol": "ETH"}
    assert reconcile_hedge_execution(record)["status"] == "unreconciled"
    assert record["venue_order_status"] == "unknownoid"


def test_pending_retry_backoff_is_persisted_and_increases(monkeypatch):
    from src import position_tracker

    class StopWorker(Exception):
        pass

    count = 0

    def stop_after_one_pass(_seconds):
        nonlocal count
        count += 1
        if count == 2:
            raise StopWorker

    monkeypatch.setattr(position_tracker.time, "sleep", stop_after_one_pass)
    monkeypatch.setattr(position_tracker.time, "time", lambda: 100)
    monkeypatch.setattr(
        position_tracker,
        "_pending_records",
        {"r": {"rebalance_id": "r", "_retry_attempts": 0}},
    )
    monkeypatch.setattr(position_tracker, "_pending_lock", threading.Lock())
    monkeypatch.setattr(
        position_tracker,
        "reconcile_hedge_execution",
        lambda row: {**row, "status": "unreconciled"},
    )
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    with pytest.raises(StopWorker):
        position_tracker._pending_worker()
    result = position_tracker._pending_records["r"]
    assert result["_retry_attempts"] == 1 and result["_next_retry_at"] == 102
    assert trade_logger.log_hedge_execution.call_count == 1


def test_missing_order_id_and_sanitized_failure(monkeypatch):
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    record = {"status": "submitted"}
    assert reconcile_hedge_execution(record)["error"] == "missing_order_id"
    record = {"order_id": 2, "order_started_ms": 1, "hedge_symbol": "ETH"}
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        Mock(side_effect=RuntimeError("secret token")),
    )
    assert reconcile_hedge_execution(record)["error"] == "RuntimeError"
    assert "secret" not in record["error"]


def test_resume_schedules_latest_pending_jsonl_record(monkeypatch):
    from src import position_tracker

    records = [
        {
            "event": "hedge_execution",
            "rebalance_id": "r",
            "status": "submitted",
            "execution_mode": "live",
            "order_id": 1,
        },
        {
            "event": "hedge_execution",
            "rebalance_id": "r",
            "status": "filled",
            "execution_mode": "live",
            "order_id": 1,
        },
        {
            "event": "hedge_execution",
            "rebalance_id": "p",
            "status": "partial",
            "execution_mode": "live",
            "order_id": 2,
        },
        {
            "event": "hedge_execution",
            "rebalance_id": "sim",
            "status": "submitted",
            "execution_mode": "simulate",
            "order_id": 3,
        },
        {
            "event": "hedge_execution",
            "rebalance_id": "missing",
            "status": "unreconciled",
            "execution_mode": "live",
            "order_id": None,
        },
        {
            "event": "hedge_execution",
            "rebalance_id": "terminal-partial",
            "status": "partial",
            "venue_order_status": "canceled",
            "execution_mode": "live",
            "order_id": 4,
        },
    ]
    monkeypatch.setattr(trade_logger, "read_events", lambda: records)
    monkeypatch.setattr(
        position_tracker,
        "_schedule_hedge_reconciliation",
        lambda row: scheduled.append(row),
    )
    scheduled = []
    assert position_tracker.resume_pending_hedge_reconciliations() == 1
    assert scheduled[0]["rebalance_id"] == "p"


def test_fill_pagination_is_bounded_and_resumes_without_overlap(monkeypatch):
    from src.position_tracker import (
        _HEDGE_FILL_WINDOW_MS,
        _MAX_FILL_WINDOWS_PER_ATTEMPT,
    )

    fill_query = Mock(return_value=[])
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *args: {"status": "order", "order": {"status": "open"}},
    )
    monkeypatch.setattr(hedge_executor, "get_order_fills", fill_query)
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    start = 1_000
    record = {
        "order_id": 2,
        "order_started_ms": start,
        "hedge_symbol": "ETH",
        "requested_size": 1,
        "net_option_delta": 0.1,
    }
    monkeypatch.setattr("src.position_tracker.time.time", lambda: 10_000_000)
    reconcile_hedge_execution(record)
    first_cursor = record["_fill_cursor_ms"]
    assert len(fill_query.call_args_list) == _MAX_FILL_WINDOWS_PER_ATTEMPT
    assert first_cursor == start + _MAX_FILL_WINDOWS_PER_ATTEMPT * _HEDGE_FILL_WINDOW_MS
    reconcile_hedge_execution(record)
    assert (
        fill_query.call_args_list[_MAX_FILL_WINDOWS_PER_ATTEMPT].args[2] == first_cursor
    )
    assert len(fill_query.call_args_list) == 2 * _MAX_FILL_WINDOWS_PER_ATTEMPT


def test_strict_position_read_propagates_each_initialized_state_failure(monkeypatch):
    monkeypatch.setattr(
        hedge_executor,
        "_info",
        Mock(user_state=Mock(side_effect=RuntimeError("venue down"))),
    )
    monkeypatch.setattr(hedge_executor, "_initialized_dexs", ("",))
    with pytest.raises(RuntimeError, match="venue down"):
        hedge_executor.get_positions_strict("ETH")


def test_missing_fills_are_unreconciled_and_read_failure_never_invents_zero(
    monkeypatch,
):
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *args: {"status": "order", "order": {"status": "open"}},
    )
    monkeypatch.setattr(hedge_executor, "get_order_fills", lambda *args: [])
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    record = {
        "order_id": 9,
        "order_started_ms": 1,
        "hedge_symbol": "ETH",
        "requested_size": 1,
        "net_option_delta": 0.2,
    }
    assert reconcile_hedge_execution(record)["status"] == "unreconciled"
    assert "observed_post_position" not in record
    monkeypatch.setattr(
        hedge_executor,
        "get_order_fills",
        lambda *args: [{"sz": "1", "px": "10", "fee": "0"}],
    )
    monkeypatch.setattr(
        hedge_executor,
        "get_positions_strict",
        Mock(side_effect=RuntimeError("read failed")),
    )
    assert reconcile_hedge_execution(record)["status"] == "unreconciled"
    assert record["error"] == "RuntimeError"
    assert "observed_post_position" not in record


def test_hedge_upsert_sets_updated_at_and_filters_internal_fields(monkeypatch):
    client = Mock()
    table = Mock()
    client.table.return_value = table
    table.upsert.return_value = table
    monkeypatch.setattr(trade_logger, "_get_supabase", lambda: client)
    monkeypatch.setattr(trade_logger, "_write_jsonl", Mock())
    enqueued = []
    monkeypatch.setattr(trade_logger, "_hedge_events", Mock(put_nowait=enqueued.append))
    monkeypatch.setattr(trade_logger.threading, "Thread", Mock())
    monkeypatch.setattr(trade_logger, "_hedge_writer_started", False)
    event = {
        "rebalance_id": "r-1",
        "chain": "base",
        "underlying": "eth",
        "hedge_symbol": "ETH",
        "decision_at": "2026-01-01T00:00:00Z",
        "configured_threshold": 0.001,
        "spot": 2000,
        "decision_reason": "rebalance",
        "execution_mode": "live",
        "side": "BUY",
        "status": "submitted",
        "net_option_delta": 0.1,
        "current_hedge_position": 0,
        "target_hedge_position": -0.1,
        "signed_difference": -0.1,
        "expected_post_position": -0.1,
        "requested_size": 0.1,
        "filled_size": 0,
        "fee": 0,
        "fill_count": 0,
        "order_started_ms": 123,
        "_retry_attempts": 2,
        "_next_retry_at": 999,
        "_fill_cursor_ms": 123,
        "_observed_fills": [{"tid": 7, "sz": "0.1"}],
    }
    trade_logger.log_hedge_execution(event)
    trade_logger.log_hedge_execution(event)
    assert [queued["rebalance_id"] for queued in enqueued] == ["r-1", "r-1"]
    assert table.upsert.call_count == 0
    schema = {
        "rebalance_id",
        "chain",
        "underlying",
        "hedge_symbol",
        "decision_at",
        "configured_threshold",
        "spot",
        "decision_reason",
        "execution_mode",
        "side",
        "status",
        "order_id",
        "net_option_delta",
        "current_hedge_position",
        "target_hedge_position",
        "signed_difference",
        "expected_post_position",
        "requested_size",
        "filled_size",
        "average_fill_price",
        "fee",
        "fee_token",
        "fill_count",
        "observed_post_position",
        "residual_delta",
        "error",
        "created_at",
        "updated_at",
    }
    for event in enqueued:
        row = {
            key: value
            for key, value in event.items()
            if key != "event" and key != "order_started_ms" and not key.startswith("_")
        }
        assert set(row) <= schema
        assert "updated_at" in row
        assert "order_started_ms" not in row
        assert row["rebalance_id"] == "r-1"


def test_local_worker_persists_before_full_supabase_queue(monkeypatch):
    import queue

    class OneEvent:
        def __init__(self, event):
            self.event = event

        def get(self):
            if self.event is not None:
                event, self.event = self.event, None
                return event
            raise RuntimeError("stop")

        def task_done(self):
            pass

    event = {"rebalance_id": "durable", "updated_at": "t"}
    append = Mock()
    monkeypatch.setattr(trade_logger, "_hedge_events", OneEvent(event))
    monkeypatch.setattr(
        trade_logger, "_hedge_upserts", Mock(put_nowait=Mock(side_effect=queue.Full))
    )
    monkeypatch.setattr(trade_logger, "_write_jsonl", append)
    monkeypatch.setattr(trade_logger, "_hedge_queued", set())
    with pytest.raises(RuntimeError, match="stop"):
        trade_logger._hedge_event_worker()
    append.assert_called_once_with(event)
    assert trade_logger._hedge_queued == set()


def test_upsert_worker_filters_internal_fields(monkeypatch):
    class OneItemQueue:
        def __init__(self):
            self.items = [
                {
                    "rebalance_id": "r",
                    "updated_at": "t",
                    "event": "hedge_execution",
                    "order_started_ms": 1,
                    "_retry_attempts": 2,
                }
            ]

        def get(self):
            if self.items:
                return self.items.pop()
            raise RuntimeError("stop")

        def task_done(self):
            pass

    table = Mock()
    table.upsert.return_value.execute.return_value = None
    client = Mock()
    client.table.return_value = table
    monkeypatch.setattr(trade_logger, "_hedge_upserts", OneItemQueue())
    monkeypatch.setattr(trade_logger, "_get_supabase", lambda: client)
    with pytest.raises(RuntimeError, match="stop"):
        trade_logger._hedge_upsert_worker()
    row = table.upsert.call_args.args[0]
    assert set(row) == {"rebalance_id", "updated_at"}


def test_upsert_worker_retries_network_exception_in_background(monkeypatch):
    event = {"rebalance_id": "retry", "updated_at": "time"}

    class OneItemQueue:
        def __init__(self):
            self.items = [event]
            self.retried = []

        def get(self):
            if self.items:
                return self.items.pop(0)
            raise RuntimeError("stop")

        def put_nowait(self, value):
            self.retried.append(value)

        def task_done(self):
            pass

    pending = OneItemQueue()
    table = Mock()
    table.upsert.return_value.execute.side_effect = RuntimeError("network")
    client = Mock()
    client.table.return_value = table
    monkeypatch.setattr(trade_logger, "_hedge_upserts", pending)
    monkeypatch.setattr(trade_logger, "_get_supabase", lambda: client)
    monkeypatch.setattr(trade_logger.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="stop"):
        trade_logger._hedge_upsert_worker()
    assert pending.retried == [event]


@pytest.mark.parametrize(
    "error", ["order_fills_incomplete", "venue_order_rejected", "RuntimeError"]
)
def test_safe_errors_are_preserved_but_messages_are_not(monkeypatch, error):
    queued = []
    monkeypatch.setattr(trade_logger, "_write_jsonl", Mock())
    monkeypatch.setattr(trade_logger, "_hedge_events", Mock(put_nowait=queued.append))
    monkeypatch.setattr(trade_logger, "_hedge_writer_started", True)
    trade_logger.log_hedge_execution({"rebalance_id": "r", "error": error})
    assert queued[0]["error"] == error
    trade_logger.log_hedge_execution({"rebalance_id": "r", "error": "secret token"})
    assert queued[1]["error"] == "reconciliation_error"


def test_jsonl_replay_queues_latest_without_appending(monkeypatch):
    class CaptureQueue:
        def __init__(self):
            self.events = []

        def put(self, event):
            self.events.append(event)

    queue = CaptureQueue()
    event = {"event": "hedge_execution", "rebalance_id": "r", "updated_at": "t"}
    monkeypatch.setattr(trade_logger, "_hedge_upserts", queue)
    monkeypatch.setattr(trade_logger, "read_events", Mock(return_value=[event]))
    monkeypatch.setattr(trade_logger, "_hedge_upserted", {})
    monkeypatch.setattr(trade_logger, "_hedge_queued", set())
    monkeypatch.setattr(trade_logger.time, "sleep", Mock())
    monkeypatch.setattr(trade_logger, "_write_jsonl", Mock())
    trade_logger._replay_hedge_upserts()
    assert queue.events == [event]
    trade_logger.read_events.assert_called_once()
    trade_logger.time.sleep.assert_not_called()
    trade_logger._write_jsonl.assert_not_called()


def test_local_event_queue_is_unbounded_and_enqueue_stays_off_caller_io(monkeypatch):
    from queue import SimpleQueue

    queued = SimpleQueue()
    monkeypatch.setattr(trade_logger, "_hedge_writer_started", True)
    monkeypatch.setattr(trade_logger, "_hedge_events", queued)
    append = Mock(side_effect=AssertionError("caller performed local I/O"))
    monkeypatch.setattr(trade_logger, "_write_jsonl", append)
    for i in range(1100):
        trade_logger.log_hedge_execution({"rebalance_id": str(i)})
    append.assert_not_called()
    assert [queued.get_nowait()["rebalance_id"] for _ in range(1100)] == [
        str(i) for i in range(1100)
    ]


def test_late_fill_is_found_after_jsonl_restart_and_deduplicated(monkeypatch):
    from src.position_tracker import _HEDGE_FILL_WINDOW_MS

    calls = []
    late = {"tid": 2, "oid": 1, "time": 1_500_000, "sz": "1", "px": "20", "fee": "0.2"}

    def fills(_asset, _oid, start, end):
        calls.append((start, end))
        return [late] if start <= late["time"] <= end else []

    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *_: {"order": {"status": "filled", "statusTimestamp": 2_250_000}},
    )
    monkeypatch.setattr(hedge_executor, "get_order_fills", fills)
    monkeypatch.setattr(hedge_executor, "get_positions_strict", lambda _asset: [])
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    row = {
        "order_id": 1,
        "order_started_ms": 1_000,
        "hedge_symbol": "ETH",
        "requested_size": 1,
        "net_option_delta": 0,
    }
    monkeypatch.setattr("src.position_tracker.time.time", lambda: 1_000)
    assert reconcile_hedge_execution(row)["status"] == "unreconciled"
    assert row["fill_count"] == 0
    # Simulate process restart from the latest JSONL record.
    restored = json.loads(json.dumps(row))  # JSONL round-trip across process restart.
    assert reconcile_hedge_execution(restored)["status"] == "filled"
    assert restored["fill_count"] == 1 and restored["filled_size"] == pytest.approx(1)
    assert restored["_fill_cursor_ms"] == 2_250_000 + _HEDGE_FILL_WINDOW_MS
    assert len({(start, end) for start, end in calls}) == len(calls)


def test_terminal_status_waits_for_fill_cursor_to_cover_target(monkeypatch):
    from src.position_tracker import _MAX_FILL_WINDOWS_PER_ATTEMPT

    monkeypatch.setattr("src.position_tracker.time.time", lambda: 1_000_000)
    monkeypatch.setattr(
        hedge_executor,
        "get_order_status",
        lambda *_: {"order": {"status": "canceled", "statusTimestamp": 10_000_000}},
    )
    query = Mock(return_value=[])
    monkeypatch.setattr(hedge_executor, "get_order_fills", query)
    monkeypatch.setattr(trade_logger, "log_hedge_execution", Mock())
    row = {
        "order_id": 1,
        "order_started_ms": 1,
        "hedge_symbol": "ETH",
        "requested_size": 1,
    }
    assert reconcile_hedge_execution(row)["status"] == "unreconciled"
    assert query.call_count == _MAX_FILL_WINDOWS_PER_ATTEMPT
    assert row["_fill_cursor_ms"] < 10_000_000
    assert reconcile_hedge_execution(row)["status"] == "unreconciled"
    assert query.call_count == 2 * _MAX_FILL_WINDOWS_PER_ATTEMPT


def test_replay_loop_uses_sixty_second_interval(monkeypatch):
    from src import trade_logger as logger

    sleeps = []
    monkeypatch.setattr(logger, "_replay_hedge_upserts", Mock())

    def stop(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop")

    monkeypatch.setattr(logger.time, "sleep", stop)
    with pytest.raises(RuntimeError, match="stop"):
        logger._replay_loop()
    assert sleeps == [60]
    logger._replay_hedge_upserts.assert_called_once()


def test_pending_worker_does_not_overwrite_replacement_snapshot(monkeypatch):
    from src import position_tracker

    old = {"rebalance_id": "r", "_next_retry_at": 0}
    replacement = {"rebalance_id": "r", "order_id": 9, "status": "submitted"}

    class StopWorker(Exception):
        pass

    calls = 0

    def sleep(_seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StopWorker

    def reconcile(_record):
        position_tracker._pending_records["r"] = replacement
        return {"status": "filled"}

    monkeypatch.setattr(position_tracker.time, "sleep", sleep)
    monkeypatch.setattr(position_tracker, "_pending_records", {"r": old})
    monkeypatch.setattr(position_tracker, "_pending_lock", threading.Lock())
    monkeypatch.setattr(position_tracker, "reconcile_hedge_execution", reconcile)
    with pytest.raises(StopWorker):
        position_tracker._pending_worker()
    assert position_tracker._pending_records["r"] is replacement


def test_missing_order_id_is_unreconciled_and_not_scheduled(monkeypatch):
    from src import position_tracker

    monkeypatch.setattr(position_tracker, "_pending_records", {})
    row = {"execution_mode": "live", "order_id": None, "status": "unreconciled"}
    position_tracker._schedule_hedge_reconciliation(row)
    assert row["order_id"] is None and row["status"] == "unreconciled"
    assert position_tracker._pending_records == {}


def test_simulation_never_claims_live_observed_position_and_telemetry_failure_is_best_effort(
    monkeypatch,
):
    monkeypatch.setattr(config, "HEDGE_MODE", "simulate")
    monkeypatch.setattr(hedge_executor, "open_hedge", Mock(return_value=None))
    monkeypatch.setattr(
        trade_logger, "_write_jsonl", Mock(side_effect=RuntimeError("sink"))
    )
    monkeypatch.setattr(trade_logger, "_get_supabase", lambda: None)
    tracker = _tracker()
    tracker.rebalance_hedge(2000, "eth", "ETH")
    assert hedge_executor.open_hedge.called
    assert tracker._simulated_hedge["eth"] == pytest.approx(-0.5)
