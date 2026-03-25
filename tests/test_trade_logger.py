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
            amount=0.01,
            premium_usd=0.42,
            user_address="0xuser",
            tx_hash="0xtx",
            spot=2112.75,
            delta=-0.45,
            hedge_action="SHORT",
            hedge_size=0.0098,
            hedge_fill_price=2108.7,
            underlying="eth",
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "position_opened"
        assert ev["otoken"] == "0xabc123"
        assert ev["strike"] == 2100.0
        assert ev["is_put"] is True
        assert ev["hedge_fill_price"] == 2108.7
        assert ev["underlying"] == "eth"
        assert ev["amount"] == 0.01
        assert ev["hedge_size"] == 0.0098

    def test_log_position_opened_btc(self, _clean_log):
        trade_logger.log_position_opened(
            otoken="0xbtc123",
            strike=55000.0,
            expiry=int(time.time()) + 86400,
            is_put=False,
            amount=0.001,
            premium_usd=5.0,
            user_address="0xuser",
            tx_hash="0xtx",
            spot=50000.0,
            delta=0.65,
            hedge_action="LONG",
            hedge_size=0.0008,
            hedge_fill_price=50100.0,
            underlying="btc",
        )
        events = _read_events(_clean_log)
        assert events[0]["underlying"] == "btc"

    def test_log_delta_rebalanced(self, _clean_log):
        trade_logger.log_delta_rebalanced(
            otoken="0xabc",
            old_delta=-0.45,
            new_delta=-0.48,
            old_hedge=0.0098,
            new_hedge=0.0105,
            hedge_fill_price=2100.0,
            underlying="eth",
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "delta_rebalanced"
        assert events[0]["underlying"] == "eth"

    def test_log_position_expired(self, _clean_log):
        trade_logger.log_position_expired(
            otoken="0xabc",
            settlement="OTM",
            expiry_price=2150.0,
            settlement_pnl=0.0,
            hedge_pnl=-0.15,
            hedge_close_price=2150.0,
            net_pnl=-0.57,
            underlying="eth",
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "position_expired"
        assert events[0]["result"] == "OTM"
        assert events[0]["underlying"] == "eth"

    def test_log_capacity_snapshot(self, _clean_log):
        trade_logger.log_capacity_snapshot(
            premium_usd=40.65,
            hedge_usd=166.25,
            hedge_withdrawable=159.07,
            effective_units=0.60,
            status="active",
            underlying="eth",
        )
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "capacity_snapshot"
        assert events[0]["underlying"] == "eth"
        assert events[0]["effective_units"] == 0.6

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
    def _write_opened_event(self, path, otoken="0xabc", expiry=None, underlying="eth"):
        if expiry is None:
            expiry = int(time.time()) + 86400
        event = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": otoken,
            "underlying": underlying,
            "strike": 2100.0,
            "expiry": expiry,
            "is_put": True,
            "amount": 0.01,
            "premium_usd": 0.42,
            "user_address": "0xuser",
            "tx_hash": "0xtx123",
            "spot": 2112.75,
            "delta": -0.45,
            "hedge_action": "SHORT",
            "hedge_size": 0.0098,
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
        assert pos.underlying == "eth"

    @patch("src.startup_recovery.hedge_executor")
    def test_skip_closed_position(self, mock_hedge, _clean_log):
        self._write_opened_event(_clean_log)
        expired = {
            "event": "position_expired",
            "ts": int(time.time()),
            "otoken": "0xabc",
            "underlying": "eth",
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
            "spot": 2100.0,
            "iv": 0.6,
            "available_otokens": [
                {
                    "address": "0x151fabc",
                    "strike_price": 2100.0,
                    "expiry": future_expiry,
                    "is_put": True,
                }
            ],
        }

        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1
        events = _read_events(_clean_log)
        assert len(events) == 1
        assert events[0]["event"] == "position_opened"
        assert events[0]["underlying"] == "eth"

    @patch("src.startup_recovery.api_client")
    @patch("src.startup_recovery.hedge_executor")
    def test_bootstrap_no_position_noop(self, mock_hedge, mock_api, _clean_log):
        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 0

    @patch("src.startup_recovery.hedge_executor")
    def test_recover_old_format_events(self, mock_hedge, _clean_log):
        """Old events with amount_eth/hedge_size_eth are still readable."""
        old_event = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": "0xold",
            "strike": 2000.0,
            "expiry": int(time.time()) + 86400,
            "is_put": True,
            "amount_eth": 0.5,
            "premium_usd": 10.0,
            "user_address": "0xuser",
            "tx_hash": "0xoldtx",
            "spot": 2000.0,
            "delta": -0.5,
            "hedge_action": "SHORT",
            "hedge_size_eth": 0.25,
            "hedge_fill_price": 2000.0,
        }
        with open(_clean_log, "w") as f:
            f.write(json.dumps(old_event) + "\n")

        mock_hedge.get_positions.return_value = [
            {"coin": "ETH", "size": -0.25, "entry_price": 2000.0}
        ]
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1

    @patch("src.startup_recovery.hedge_executor")
    def test_recover_multiple_positions_same_otoken(self, mock_hedge, _clean_log):
        """Multiple buys of the same otoken are all recovered."""
        self._write_opened_event(_clean_log, otoken="0xsame")
        event2 = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": "0xsame",
            "strike": 2100.0,
            "expiry": int(time.time()) + 86400,
            "is_put": True,
            "amount": 0.02,
            "premium_usd": 0.50,
            "user_address": "0xuser2",
            "tx_hash": "0xtx456",
            "spot": 2112.75,
            "delta": -0.45,
            "hedge_action": "SHORT",
            "hedge_size": 0.009,
            "hedge_fill_price": 2108.7,
        }
        with open(_clean_log, "a") as f:
            f.write(json.dumps(event2) + "\n")

        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 2

    @patch("src.startup_recovery.hedge_executor")
    def test_partial_close_same_otoken(self, mock_hedge, _clean_log):
        """2 opens + 1 expire -> 1 position restored."""
        self._write_opened_event(_clean_log, otoken="0xpartial")
        event2 = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": "0xpartial",
            "strike": 2100.0,
            "expiry": int(time.time()) + 86400,
            "is_put": True,
            "amount": 0.02,
            "premium_usd": 0.50,
            "user_address": "0xuser2",
            "tx_hash": "0xtx789",
            "spot": 2112.75,
            "delta": -0.45,
            "hedge_action": "SHORT",
            "hedge_size": 0.009,
            "hedge_fill_price": 2108.7,
        }
        expired = {
            "event": "position_expired",
            "ts": int(time.time()),
            "otoken": "0xpartial",
            "result": "OTM",
            "expiry_price": 2200.0,
            "settlement_pnl": 0,
            "hedge_pnl": 0,
            "net_pnl": -0.42,
        }
        with open(_clean_log, "a") as f:
            f.write(json.dumps(event2) + "\n")
            f.write(json.dumps(expired) + "\n")

        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1

    @patch("src.startup_recovery.hedge_executor")
    def test_dedup_same_tx_hash(self, mock_hedge, _clean_log):
        """Duplicate opens with same tx_hash are deduped to one."""
        self._write_opened_event(_clean_log, otoken="0xdup")
        self._write_opened_event(_clean_log, otoken="0xdup")

        mock_hedge.get_positions.return_value = []
        tracker = PositionTracker()
        restored = recover_positions(tracker)
        assert restored == 1
        assert len(tracker.open_positions()) == 1
