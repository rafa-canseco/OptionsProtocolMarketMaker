"""Tests for trade_logger and startup_recovery."""

import json
import os
import time
from unittest.mock import patch

import pytest

from src import trade_logger
from src.position_tracker import PositionTracker
from src.startup_recovery import recover_positions


@pytest.fixture(autouse=True)
def _clean_log(tmp_path, monkeypatch):
    """Use a temp file for every test."""
    log_path = str(tmp_path / "test_history.jsonl")
    monkeypatch.setattr("src.config.TRADE_LOG_PATH", log_path)
    monkeypatch.setattr("src.config.SUPABASE_URL", "")
    monkeypatch.setattr("src.config.SUPABASE_KEY", "")
    trade_logger._supabase_client = None
    yield log_path


def _read_events(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestTradeLogger:
    def test_log_position_opened_writes_jsonl(self, _clean_log):
        trade_logger.log_position_opened(
            otoken="0xabc123",
            strike=2100.0,
            expiry=int(time.time()) + 86400,
            is_put=True,
            amount_eth=0.01,
            premium_usd=0.42,
            user_address="0xuser",
            tx_hash="0xtx",
            spot=2112.75,
            delta=-0.45,
            hedge_action="SHORT",
            hedge_size_eth=0.0098,
            hedge_fill_price=2108.7,
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "position_opened"
        assert ev["otoken"] == "0xabc123"
        assert ev["strike"] == 2100.0
        assert ev["is_put"] is True
        assert ev["hedge_fill_price"] == 2108.7

    def test_log_delta_rebalanced(self, _clean_log):
        trade_logger.log_delta_rebalanced(
            otoken="0xabc",
            old_delta=-0.45,
            new_delta=-0.48,
            old_hedge=0.0098,
            new_hedge=0.0105,
            hedge_fill_price=2100.0,
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "delta_rebalanced"

    def test_log_position_expired(self, _clean_log):
        trade_logger.log_position_expired(
            otoken="0xabc",
            settlement="OTM",
            expiry_price=2150.0,
            settlement_pnl=0.0,
            hedge_pnl=-0.15,
            hedge_close_price=2150.0,
            net_pnl=-0.57,
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "position_expired"
        assert events[0]["result"] == "OTM"

    def test_log_capacity_snapshot(self, _clean_log):
        trade_logger.log_capacity_snapshot(
            premium_usd=40.65,
            hedge_usd=166.25,
            hedge_withdrawable=159.07,
            effective_eth=0.60,
            status="active",
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "capacity_snapshot"

    def test_multiple_events_append(self, _clean_log):
        trade_logger.log_capacity_snapshot(1.0, 2.0, 3.0, 0.1, "active")
        trade_logger.log_capacity_snapshot(4.0, 5.0, 6.0, 0.2, "idle")
        events = _read_events(_clean_log)
        assert len(events) == 2

    def test_read_events(self, _clean_log):
        trade_logger.log_capacity_snapshot(1.0, 2.0, 3.0, 0.1, "active")
        events = trade_logger.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "capacity_snapshot"


class TestStartupRecovery:
    def _write_opened_event(self, path, otoken="0xabc", expiry=None):
        if expiry is None:
            expiry = int(time.time()) + 86400
        event = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": otoken,
            "strike": 2100.0,
            "expiry": expiry,
            "is_put": True,
            "amount_eth": 0.01,
            "premium_usd": 0.42,
            "user_address": "0xuser",
            "tx_hash": "0xtx123",
            "spot": 2112.75,
            "delta": -0.45,
            "hedge_action": "SHORT",
            "hedge_size_eth": 0.0098,
            "hedge_fill_price": 2108.7,
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")

    @patch("src.startup_recovery.hedge_executor")
    def test_recover_open_position(self, mock_hedge, _clean_log):
        self._write_opened_event(_clean_log)
        mock_hedge.get_positions.return_value = [
            {"coin": "ETH", "size": -0.0098, "entry_price": 2108.7}
        ]
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1
        assert len(tracker.open_positions()) == 1
        pos = tracker.open_positions()[0]
        assert pos.strike == 2100.0
        assert pos.is_put is True

    @patch("src.startup_recovery.hedge_executor")
    def test_skip_closed_position(self, mock_hedge, _clean_log):
        self._write_opened_event(_clean_log)
        expired = {
            "event": "position_expired",
            "ts": int(time.time()),
            "otoken": "0xabc",
            "result": "OTM",
            "expiry_price": 2150.0,
            "settlement_pnl": 0,
            "hedge_pnl": -0.15,
            "hedge_close_price": 2150.0,
            "net_pnl": -0.57,
        }
        with open(_clean_log, "a") as f:
            f.write(json.dumps(expired) + "\n")

        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 0
        assert len(tracker.open_positions()) == 0

    @patch("src.startup_recovery.hedge_executor")
    def test_skip_already_expired(self, mock_hedge, _clean_log):
        past_expiry = int(time.time()) - 3600
        self._write_opened_event(_clean_log, expiry=past_expiry)
        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 0

    @patch("src.startup_recovery.hedge_executor")
    def test_drift_detection(self, mock_hedge, _clean_log, caplog):
        self._write_opened_event(_clean_log)
        mock_hedge.get_positions.return_value = [
            {"coin": "ETH", "size": -0.05, "entry_price": 2108.7}
        ]
        tracker = PositionTracker()
        import logging

        with caplog.at_level(logging.WARNING):
            recover_positions(tracker)
        assert any("DRIFT" in r.message for r in caplog.records)

    @patch("src.startup_recovery.api_client")
    @patch("src.startup_recovery.hedge_executor")
    def test_bootstrap_from_hyperliquid(self, mock_hedge, mock_api, _clean_log):
        mock_hedge.get_positions.return_value = [
            {
                "coin": "ETH",
                "size": -0.0101,
                "entry_price": 2108.7,
                "unrealized_pnl": -0.5,
                "leverage": "3x",
            }
        ]
        future_expiry = int(time.time()) + 86400
        mock_api.get_fills.return_value = [
            {
                "otoken_address": "0x151fabc",
                "amount": "1000000",
                "gross_premium": "420000",
                "user_address": "0xuser",
                "tx_hash": "0xtx",
            }
        ]
        mock_api.get_market_data.return_value = {
            "available_otokens": [
                {
                    "address": "0x151fabc",
                    "strike_price": 2100.0,
                    "expiry": future_expiry,
                    "is_put": True,
                }
            ]
        }

        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "position_opened"

    @patch("src.startup_recovery.api_client")
    @patch("src.startup_recovery.hedge_executor")
    def test_bootstrap_no_position_noop(self, mock_hedge, mock_api, _clean_log):
        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 0
