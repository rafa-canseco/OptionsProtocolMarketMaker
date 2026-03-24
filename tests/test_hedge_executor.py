"""Test hedge executor with mocked Hyperliquid API.

Verifies the live execution path: open, close, adjust hedges.
"""

import logging
import time
from unittest.mock import MagicMock, patch

from src import hedge_executor
from src.position_tracker import PositionTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

SPOT = 1972.0
IV = 0.59
RF = 0.05

MOCK_OPEN_RESULT = {
    "status": "ok",
    "response": {
        "data": {
            "statuses": [
                {
                    "filled": {
                        "totalSz": "1.0800",
                        "avgPx": "1973.50",
                        "oid": 12345,
                    }
                }
            ]
        }
    },
}

MOCK_CLOSE_RESULT = {
    "status": "ok",
    "response": {
        "data": {
            "statuses": [
                {
                    "filled": {
                        "totalSz": "1.0800",
                        "avgPx": "1850.25",
                    }
                }
            ]
        }
    },
}


def _setup_live_mode():
    """Patch config to live mode and set up mock exchange."""
    hedge_executor._exchange = MagicMock()
    hedge_executor._info = MagicMock()
    hedge_executor._address = "0xTEST"


@patch("src.config.HEDGE_MODE", "live")
def test_open_hedge_calls_market_open():
    """Live mode calls exchange.market_open with correct args."""
    _setup_live_mode()
    hedge_executor._exchange.market_open.return_value = MOCK_OPEN_RESULT

    result = hedge_executor.open_hedge("ETH", True, 1.08)

    hedge_executor._exchange.market_open.assert_called_once_with(
        "ETH", True, 1.08, slippage=0.01
    )
    assert result is not None
    assert result["size"] == 1.08
    assert result["avg_price"] == 1973.50


@patch("src.config.HEDGE_MODE", "live")
def test_close_hedge_calls_market_close():
    """Live mode calls exchange.market_close."""
    _setup_live_mode()
    hedge_executor._exchange.market_close.return_value = MOCK_CLOSE_RESULT

    result = hedge_executor.close_hedge("ETH")

    hedge_executor._exchange.market_close.assert_called_once_with("ETH", slippage=0.01)
    assert result is not None
    assert result["avg_price"] == 1850.25


@patch("src.config.HEDGE_MODE", "live")
def test_close_hedge_partial():
    """Partial close passes size to market_close."""
    _setup_live_mode()
    hedge_executor._exchange.market_close.return_value = MOCK_CLOSE_RESULT

    hedge_executor.close_hedge("ETH", size=0.5)

    hedge_executor._exchange.market_close.assert_called_once_with(
        "ETH", sz=0.5, slippage=0.01
    )


@patch("src.config.HEDGE_MODE", "live")
def test_adjust_hedge_increases():
    """Adjust up opens additional hedge."""
    _setup_live_mode()
    hedge_executor._exchange.market_open.return_value = MOCK_OPEN_RESULT

    result = hedge_executor.adjust_hedge("ETH", 1.0, 1.5, True)

    hedge_executor._exchange.market_open.assert_called_once_with(
        "ETH", True, 0.5, slippage=0.01
    )
    assert result is not None


@patch("src.config.HEDGE_MODE", "live")
def test_adjust_hedge_decreases():
    """Adjust down partially closes hedge."""
    _setup_live_mode()
    hedge_executor._exchange.market_close.return_value = MOCK_CLOSE_RESULT

    result = hedge_executor.adjust_hedge("ETH", 1.5, 1.0, True)

    hedge_executor._exchange.market_close.assert_called_once_with(
        "ETH", sz=0.5, slippage=0.01
    )
    assert result is not None


@patch("src.config.HEDGE_MODE", "live")
def test_adjust_hedge_skip_tiny():
    """Skip adjustment if diff < 0.0001."""
    _setup_live_mode()

    result = hedge_executor.adjust_hedge("ETH", 1.0, 1.00005, True)

    hedge_executor._exchange.market_open.assert_not_called()
    hedge_executor._exchange.market_close.assert_not_called()
    assert result is None


@patch("src.config.HEDGE_MODE", "live")
def test_full_lifecycle_with_mock_hyperliquid():
    """Full flow: fill → aggregate hedge → expiry → hedge closed.

    Verifies aggregate hedging opens/closes via rebalance_hedge.
    """
    _setup_live_mode()
    hedge_executor._exchange.market_open.return_value = MOCK_OPEN_RESULT
    hedge_executor._exchange.market_close.return_value = MOCK_CLOSE_RESULT
    # No existing HL positions initially
    hedge_executor._info.user_state.return_value = {
        "marginSummary": {"accountValue": "30000.0"},
        "withdrawable": "12500.50",
        "assetPositions": [],
    }

    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_TEST",
                "strike_price": 2000.0,
                "expiry": int(time.time()) + 2,
                "is_put": True,
            }
        ]
    )

    pos = tracker.add_position(
        {
            "otoken_address": "0xPUT_TEST",
            "amount": 100000000,
            "gross_premium": 50000000,
            "user_address": "0xUSER",
            "tx_hash": "0xTX",
        },
        SPOT,
        IV,
        RF,
    )

    # add_position no longer hedges individually
    assert pos is not None
    hedge_executor._exchange.market_open.assert_not_called()

    # Aggregate rebalance opens the hedge
    tracker.rebalance_hedge(SPOT, "eth", "ETH")
    hedge_executor._exchange.market_open.assert_called_once()
    call_args = hedge_executor._exchange.market_open.call_args
    assert call_args[0][0] == "ETH"
    assert not call_args[0][1]  # SHORT for negative net delta

    # Wait for expiry
    time.sleep(3)

    # Simulate HL having the short position
    hedge_size = pos.hedge_size
    hedge_executor._info.user_state.return_value = {
        "marginSummary": {"accountValue": "30000.0"},
        "withdrawable": "12500.50",
        "assetPositions": [
            {
                "position": {
                    "coin": "ETH",
                    "szi": str(-hedge_size),
                    "entryPx": "1973.50",
                    "unrealizedPnl": "100.0",
                    "leverage": {"type": "cross", "value": 3},
                }
            }
        ],
    }
    hedge_executor._exchange.market_open.reset_mock()

    expired = tracker.check_expiries(spot=1850.0)
    assert len(expired) == 1

    # Aggregate rebalance after expiry closes the hedge
    # net_delta=0, current=-hedge_size → buy to close
    tracker.rebalance_hedge(1850.0, "eth", "ETH")
    hedge_executor._exchange.market_open.assert_called_once()
    close_args = hedge_executor._exchange.market_open.call_args
    assert close_args[0][1]  # BUY to close short


@patch("src.config.HEDGE_MODE", "live")
def test_open_hedge_handles_failure():
    """Failed Hyperliquid call doesn't crash, returns None."""
    _setup_live_mode()
    hedge_executor._exchange.market_open.side_effect = Exception("Connection refused")

    result = hedge_executor.open_hedge("ETH", True, 1.0)

    assert result is None


def test_simulate_mode_no_api_calls():
    """Simulate mode never touches Hyperliquid API."""
    hedge_executor._exchange = None
    hedge_executor._info = None
    hedge_executor._address = ""

    result = hedge_executor.open_hedge("ETH", True, 1.0)
    assert result is None

    result = hedge_executor.close_hedge("ETH")
    assert result is None


def test_get_withdrawable_returns_value():
    """get_withdrawable reads user_state.withdrawable."""
    _setup_live_mode()
    hedge_executor._info.user_state.return_value = {
        "marginSummary": {"accountValue": "30000.0"},
        "withdrawable": "12500.50",
        "assetPositions": [],
    }

    result = hedge_executor.get_withdrawable()
    assert result == 12500.50


def test_get_withdrawable_no_info():
    """Returns 0.0 when Hyperliquid not initialized."""
    hedge_executor._info = None
    assert hedge_executor.get_withdrawable() == 0.0


def test_get_withdrawable_handles_error():
    """Returns 0.0 on API error."""
    _setup_live_mode()
    hedge_executor._info.user_state.side_effect = Exception("timeout")
    assert hedge_executor.get_withdrawable() == 0.0
