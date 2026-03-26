"""Tests for quote_builder with dynamic max_amount_raw and multi-asset."""

import time
from unittest.mock import patch

from src.pricer import (
    apply_vol_skew,
    calculate_spread,
    check_iv_divergence,
    validate_iv,
)
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
                "strike_price": 45000.0,
                "expiry": int(time.time()) + 7 * 86400,
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


def test_calculate_spread_base_only():
    """No inventory or utilization → base spread returned."""
    result = calculate_spread(200, is_put=True, T=7 / 365)
    assert result == 200


def test_calculate_spread_put_heavy_widens_puts():
    """Put-heavy inventory widens put spread."""
    base = calculate_spread(200, is_put=True, T=7 / 365)
    skewed = calculate_spread(200, is_put=True, T=7 / 365, inventory_imbalance=0.8)
    assert skewed > base


def test_calculate_spread_put_heavy_narrows_calls():
    """Put-heavy inventory narrows call spread to attract balancing."""
    base = calculate_spread(200, is_put=False, T=7 / 365)
    skewed = calculate_spread(200, is_put=False, T=7 / 365, inventory_imbalance=0.8)
    assert skewed < base


def test_calculate_spread_near_expiry_surcharge():
    """Options expiring in < 1 day get extra spread."""
    far = calculate_spread(200, is_put=True, T=7 / 365)
    near = calculate_spread(200, is_put=True, T=0.5 / 365)
    assert near > far


def test_calculate_spread_utilization_surcharge():
    """High utilization (>80%) widens spread."""
    normal = calculate_spread(200, is_put=True, T=7 / 365)
    high_util = calculate_spread(200, is_put=True, T=7 / 365, utilization=0.95)
    assert high_util > normal


def test_calculate_spread_floor():
    """Spread never drops below 50bps even with heavy narrowing."""
    result = calculate_spread(60, is_put=True, T=7 / 365, inventory_imbalance=-1.0)
    assert result >= 50


@patch("src.quote_builder.config")
def test_build_quotes_inventory_widens_put_spread(mock_config):
    """Put-heavy inventory produces higher bid (lower premium for user)."""
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
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ],
    }

    neutral = build_quotes(market, maker_nonce=0)
    skewed = build_quotes(market, maker_nonce=0, inventory_imbalance=0.9)

    # Wider spread = lower bid price (we pay less)
    assert skewed[0]["bidPrice"] < neutral[0]["bidPrice"]


def test_vol_skew_atm_unchanged():
    """ATM options get approximately unchanged IV."""
    result = apply_vol_skew(0.6, S=2000.0, K=2000.0, is_put=True)
    assert abs(result - 0.6) < 0.01


def test_vol_skew_otm_put_higher():
    """OTM puts get higher IV (skew)."""
    atm = apply_vol_skew(0.6, S=2000.0, K=2000.0, is_put=True)
    otm_put = apply_vol_skew(0.6, S=2000.0, K=1800.0, is_put=True)
    assert otm_put > atm


def test_vol_skew_otm_call_higher():
    """OTM calls also get higher IV but less than puts."""
    otm_put = apply_vol_skew(0.6, S=2000.0, K=1800.0, is_put=True)
    otm_call = apply_vol_skew(0.6, S=2000.0, K=2200.0, is_put=False)
    atm = apply_vol_skew(0.6, S=2000.0, K=2000.0, is_put=False)
    assert otm_call > atm
    # Put skew should be stronger than call skew at same distance
    assert otm_put > otm_call


def test_vol_skew_clamped():
    """Extreme moneyness is clamped to max multiplier."""
    result = apply_vol_skew(0.6, S=2000.0, K=500.0, is_put=True)
    assert result <= 0.6 * 1.5  # VOL_SKEW_MAX_MULT


def test_vol_skew_zero_inputs():
    """Zero/invalid inputs return sigma unchanged."""
    assert apply_vol_skew(0.6, S=0, K=2000.0, is_put=True) == 0.6
    assert apply_vol_skew(0.0, S=2000.0, K=2000.0, is_put=True) == 0.0


@patch("src.quote_builder.config")
def test_skip_deep_itm_options(mock_config):
    """Deep ITM options (|delta| > 0.9) are not quoted."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xDEEP_ITM",
                "strike_price": 2500.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            },
            {
                "address": "0xOTM",
                "strike_price": 1800.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            },
        ],
    }

    quotes = build_quotes(market, maker_nonce=0)

    # Deep ITM put (strike 2500 vs spot 2000) should be filtered
    addresses = [q["oToken"] for q in quotes]
    assert "0xDEEP_ITM" not in addresses
    assert "0xOTM" in addresses


@patch("src.quote_builder.config")
def test_skip_very_short_dated(mock_config):
    """Options expiring in < 1 hour are not quoted."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOO_SHORT",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 1800,  # 30 min
                "is_put": True,
            },
            {
                "address": "0xOK",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            },
        ],
    }

    quotes = build_quotes(market, maker_nonce=0)

    addresses = [q["oToken"] for q in quotes]
    assert "0xTOO_SHORT" not in addresses
    assert "0xOK" in addresses


def test_validate_iv_rejects_zero():
    assert not validate_iv(0.0)


def test_validate_iv_rejects_too_low():
    assert not validate_iv(0.01)


def test_validate_iv_accepts_normal():
    assert validate_iv(0.6)


def test_validate_iv_rejects_too_high():
    assert not validate_iv(5.0)


def test_check_iv_divergence_insufficient_data():
    assert check_iv_divergence(0.6, []) is None
    assert check_iv_divergence(0.6, [2000.0]) is None


def test_check_iv_divergence_returns_realized_vol():
    # Stable prices → low realized vol
    spots = [2000.0] * 20
    rv = check_iv_divergence(0.6, spots)
    assert rv is not None
    assert rv < 0.1  # nearly zero realized vol


def test_check_iv_divergence_volatile_prices():
    # Alternating prices → high realized vol
    spots = [2000.0, 2100.0] * 20
    rv = check_iv_divergence(0.6, spots)
    assert rv is not None
    assert rv > 0.5  # significant realized vol
