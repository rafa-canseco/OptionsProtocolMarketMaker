"""Tests for quote_builder with dynamic max_amount_raw and multi-asset."""

import time
from unittest.mock import patch

from src.quote_builder import build_quotes


@patch("src.quote_builder.config")
def test_build_quotes_uses_max_amount_raw_param(mock_config):
    """When max_amount_raw is passed, quotes use it instead of config."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000  # 5 ETH default

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 86400,
                "is_put": False,
            }
        ],
    }

    # Pass a custom max_amount_raw
    quotes = build_quotes(market, maker_nonce=0, max_amount_raw=1_200_000_000)

    assert len(quotes) == 1
    assert quotes[0]["maxAmount"] == 1_200_000_000


@patch("src.quote_builder.config")
def test_build_quotes_defaults_to_config_max_amount(mock_config):
    """When max_amount_raw is None, falls back to config.MAX_AMOUNT."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 86400,
                "is_put": False,
            }
        ],
    }

    quotes = build_quotes(market, maker_nonce=0)

    assert len(quotes) == 1
    assert quotes[0]["maxAmount"] == 500_000_000


@patch("src.quote_builder.config")
def test_build_quotes_includes_asset_field(mock_config):
    """Quotes include the asset field for multi-asset support."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 50000.0,
        "iv": 0.5,
        "available_otokens": [
            {
                "address": "0xBTC_TOKEN",
                "strike_price": 55000.0,
                "expiry": int(time.time()) + 86400,
                "is_put": True,
            }
        ],
    }

    quotes = build_quotes(market, maker_nonce=0, asset="btc")

    assert len(quotes) == 1
    assert quotes[0]["asset"] == "btc"


@patch("src.quote_builder.config")
def test_build_quotes_default_asset_is_eth(mock_config):
    """Default asset is 'eth' when not specified."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 86400,
                "is_put": False,
            }
        ],
    }

    quotes = build_quotes(market, maker_nonce=0)

    assert quotes[0]["asset"] == "eth"
