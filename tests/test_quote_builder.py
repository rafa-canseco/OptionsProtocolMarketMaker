"""Tests for quote_builder with dynamic max_amount_raw and multi-asset."""

import time
from unittest.mock import patch

from src.pricer import (
    IV_CALIBRATION_CAP,
    IV_CALIBRATION_THRESHOLD,
    IV_MIN_VALID,
    MIN_SPREAD_BPS,
    VOL_SKEW_MAX_MULT,
    apply_vol_skew,
    calculate_spread,
    calibrate_iv,
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
    mock_config.REFRESH_INTERVAL = 60

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
    mock_config.REFRESH_INTERVAL = 60

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
    mock_config.REFRESH_INTERVAL = 60

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
    mock_config.REFRESH_INTERVAL = 60

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


def test_calculate_spread_floor_pins_min_spread_bps():
    """Heavy narrowing clamps to MIN_SPREAD_BPS, not a looser bound."""
    result = calculate_spread(60, is_put=True, T=7 / 365, inventory_imbalance=-1.0)
    assert result == MIN_SPREAD_BPS


def test_calculate_spread_narrowing_from_production_base():
    """With SPREAD_BPS=400 and a full-narrowing put, result sits above floor."""
    # base 400, imbalance +1.0 put-heavy, is_put=False → narrow calls by 0.5*200=100
    # 400 - 100 = 300, above MIN_SPREAD_BPS=150
    result = calculate_spread(400, is_put=False, T=7 / 365, inventory_imbalance=1.0)
    assert result == 300


@patch("src.quote_builder.config")
def test_build_quotes_inventory_widens_put_spread(mock_config):
    """Put-heavy inventory produces higher bid (lower premium for user)."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000
    mock_config.REFRESH_INTERVAL = 60

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
    """Extreme moneyness is clamped to VOL_SKEW_MAX_MULT."""
    # K/S = 0.05 drives the raw multiplier well above the cap.
    result = apply_vol_skew(0.6, S=2000.0, K=100.0, is_put=True)
    assert result == 0.6 * VOL_SKEW_MAX_MULT


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
    mock_config.REFRESH_INTERVAL = 60

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
    mock_config.REFRESH_INTERVAL = 60

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


def test_check_iv_divergence_flat_prices_returns_none():
    """Stable prices produce zero variance — helper returns None."""
    spots = [2000.0] * 20
    assert check_iv_divergence(0.6, spots) is None


def test_check_iv_divergence_volatile_prices():
    """Alternating prices yield a defined realized vol."""
    spots = [2000.0, 2100.0] * 20
    # Use daily cadence (default) to preserve the pre-existing scale.
    rv = check_iv_divergence(0.6, spots)
    assert rv is not None
    assert rv > 0.5  # significant realized vol at daily cadence


# ------- calibrate_iv tests -------

_DAILY = 86400  # daily cadence, matches pre-change annualization
_MINUTE = 60  # MM production cadence


def _low_vol_spot_history(n: int) -> list[float]:
    """n+1 spots producing very small realized vol — triggers high ratio."""
    # Tiny alternating moves: log-return ~= 5e-5 per step
    return [2000.0 + (i % 2) * 0.1 for i in range(n + 1)]


def _high_vol_spot_history(n: int) -> list[float]:
    """n+1 spots producing realized vol similar to typical IV."""
    return [2000.0, 2100.0] * ((n + 1) // 2 + 1)


def test_calibrate_iv_zero_iv_passthrough():
    history = _low_vol_spot_history(40)
    assert calibrate_iv(0.0, history, sample_seconds=_DAILY) == 0.0


def test_calibrate_iv_negative_iv_passthrough():
    history = _low_vol_spot_history(40)
    assert calibrate_iv(-0.1, history, sample_seconds=_DAILY) == -0.1


def test_calibrate_iv_empty_history_passthrough():
    assert calibrate_iv(0.6, [], sample_seconds=_DAILY) == 0.6


def test_calibrate_iv_single_entry_passthrough():
    assert calibrate_iv(0.6, [2000.0], sample_seconds=_DAILY) == 0.6


def test_calibrate_iv_warmup_passthrough():
    """Below IV_CALIBRATION_MIN_RETURNS samples, cap is inactive."""
    # 5 valid returns — well below the warmup threshold.
    history = _high_vol_spot_history(5)
    assert calibrate_iv(2.0, history, sample_seconds=_DAILY) == 2.0


def test_calibrate_iv_flat_history_passthrough():
    """Zero variance → realized_vol helper returns None → raw iv returned."""
    history = [2000.0] * 50
    assert calibrate_iv(0.6, history, sample_seconds=_DAILY) == 0.6


def test_calibrate_iv_zero_spots_skipped():
    """Zero spots are dropped from the returns series."""
    # 40 zeros interspersed with two real samples — no valid return pairs.
    history = [0.0] * 40 + [2000.0, 2001.0]
    # Only 1 valid pair (last two), below minimum. Returns raw IV.
    assert calibrate_iv(0.6, history, sample_seconds=_DAILY) == 0.6


def test_calibrate_iv_ratio_below_threshold_passthrough():
    """When iv / realized <= threshold, IV is returned unchanged."""
    history = _high_vol_spot_history(40)
    # realized vol for this history at daily cadence ~ 0.93
    # pick iv s.t. ratio = 1.2 (< 1.30)
    iv = 0.93 * 1.2
    result = calibrate_iv(iv, history, sample_seconds=_DAILY)
    assert result == iv


def test_calibrate_iv_ratio_above_threshold_caps():
    """When iv / realized > threshold, IV is capped at realized * CAP."""
    history = _low_vol_spot_history(50)
    iv = 2.0  # very high relative to realized vol of this history
    result = calibrate_iv(iv, history, sample_seconds=_DAILY)
    # Result is either realized_vol * CAP, or floored at IV_MIN_VALID.
    assert result < iv
    assert result >= IV_MIN_VALID


def test_calibrate_iv_floors_at_iv_min_valid():
    """Capped value is never below IV_MIN_VALID so downstream BS stays sane."""
    # Micro-oscillations: enough valid returns but realized vol is tiny.
    history = [2000.0 + 0.0001 * (i % 2) for i in range(50)]
    iv = 2.0
    result = calibrate_iv(iv, history, sample_seconds=_DAILY)
    # Uncapped formula would produce a value far below IV_MIN_VALID;
    # floor guarantees downstream pricing stays usable.
    assert result >= IV_MIN_VALID


def test_calibrate_iv_honors_sample_cadence():
    """Same history, different cadence → different realized vol.

    Per-minute sampling annualizes ~sqrt(1440)x higher than daily.
    """
    history = _high_vol_spot_history(60)
    iv = 1.5
    daily = calibrate_iv(iv, history, sample_seconds=_DAILY)
    minute = calibrate_iv(iv, history, sample_seconds=_MINUTE)
    # With minute cadence realized vol is much higher so the ratio is
    # below threshold → passthrough. With daily cadence realized vol is
    # much lower → ratio is above threshold → capped.
    assert minute == iv
    assert daily < iv


# IV_CALIBRATION_CAP and IV_CALIBRATION_THRESHOLD sanity — guard against
# accidental edits.
def test_calibration_constants_sane():
    assert IV_CALIBRATION_THRESHOLD > 1.0
    assert IV_CALIBRATION_CAP > 0.0
    assert IV_CALIBRATION_CAP <= IV_CALIBRATION_THRESHOLD


# ------- build_quotes spot_history wiring -------


@patch("src.quote_builder.config")
def test_build_quotes_no_spot_history_still_produces_quotes(mock_config):
    """Without spot_history, calibration no-ops but quotes still build."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 400
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000
    mock_config.REFRESH_INTERVAL = 60

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": False,
            }
        ],
    }
    quotes = build_quotes(market, maker_nonce=0)
    assert len(quotes) == 1


@patch("src.quote_builder.config")
def test_build_quotes_calibration_lowers_bid_when_iv_inflated(mock_config):
    """Passing a spot history whose realized vol is well below raw IV
    triggers the cap and results in a lower premium bid than the
    uncalibrated path."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 400
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000
    mock_config.REFRESH_INTERVAL = 60
    # Force daily cadence so the low-vol history meaningfully triggers the cap.
    mock_config.REFRESH_INTERVAL = _DAILY

    market = {
        "spot": 2000.0,
        "iv": 2.0,  # deliberately inflated
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": False,
            }
        ],
    }
    no_history = build_quotes(market, maker_nonce=0)
    with_history = build_quotes(
        market,
        maker_nonce=0,
        spot_history=_low_vol_spot_history(50),
    )
    # Calibration lowers IV, which lowers BS price, which lowers the bid.
    assert with_history[0]["bidPrice"] < no_history[0]["bidPrice"]
