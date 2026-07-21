import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.backtest.config import (
    AssetSettings,
    BacktestSettings,
    CapacityPoint,
    CoverageGate,
    ProductionValidationSettings,
    load_settings,
)
from src.backtest import production
from src.backtest.data import MarketSeries, extract_asset_market_snapshot
from src.backtest.engine import (
    _fair_option_liability,
    _vectorized_option_liability,
    binary_bid_premium,
    protected_call_floor,
    run_strategy,
    select_protected_call_strike,
)
from src.backtest.models import (
    AssignmentLot,
    CostScenario,
    OptionPosition,
    StrategyConfig,
)
from src.backtest.probe import run_coverage_probe
from src.backtest.production import (
    build_production_summary,
    rolling_end_times,
    run_rolling_validation,
)
from src.pricer import apply_vol_skew, calculate_spread, price_with_spread


def _settings(window_days=(4,)) -> BacktestSettings:
    costs = CostScenario(
        name="base",
        base_spread_bps=200,
        execution_slippage_bps=0,
        fee_bps_notional=0,
        gas_usdc=0,
        operational_delay_minutes=0,
    )
    return BacktestSettings(
        initial_usdc=100_000,
        cadence_hours=48,
        window_days=window_days,
        target_deltas=(0.4,),
        utilizations=(0.5,),
        minimum_premium_bps=(0,),
        call_margins_usd=(0,),
        protection_modes=("lot_gross",),
        strike_increment_usd=5,
        risk_free_rate=0.05,
        coverage_gate=CoverageGate(0.95, 0.05, 8, 6),
        cost_scenarios=(costs,),
    )


def _series(tmp_path: Path, spots: list[tuple[datetime, float]]) -> MarketSeries:
    cutoff = max(timestamp for timestamp, _ in spots)
    start = min(timestamp for timestamp, _ in spots)
    hours = int((cutoff - start).total_seconds() / 3600)
    spot_map = {timestamp: value for timestamp, value in spots}
    rows = []
    last_value = spots[0][1]
    for hour in range(hours + 1):
        timestamp = start + timedelta(hours=hour)
        last_value = spot_map.get(timestamp, last_value)
        rows.append(
            {
                "timestamp_ms": int(timestamp.timestamp() * 1000),
                "value": last_value,
                "source": "observed",
            }
        )
    payload = {
        "asset": "ETH",
        "start": start.isoformat(),
        "cutoff": cutoff.isoformat(),
        "spot": rows,
        "iv": [dict(row, value=0.6) for row in rows],
    }
    path = tmp_path / "market.json"
    path.write_text(json.dumps(payload))
    return MarketSeries(path)


class _HourlyCandleClient:
    def __init__(self, timestamp_ms: int) -> None:
        self.timestamp_ms = timestamp_ms

    def get(self, method: str, params: dict) -> dict:
        if method == "get_tradingview_chart_data":
            return {
                "result": {
                    "ticks": [self.timestamp_ms],
                    "close": [2000.0],
                }
            }
        if method == "get_volatility_index_data":
            return {
                "result": {
                    "data": [[self.timestamp_ms, 0, 0, 0, 60.0]],
                    "continuation": None,
                }
            }
        raise AssertionError(f"Unexpected method: {method}")


def test_binary_premium_replays_production_pricer():
    premium, spread, skew = binary_bid_premium(
        is_put=True,
        spot=2000,
        strike=1900,
        time_years=2 / 365,
        iv=0.6,
        risk_free_rate=0.05,
        base_spread_bps=200,
        utilization=0.75,
    )
    expected_spread = calculate_spread(200, True, 2 / 365, utilization=0.75)
    expected_skew = apply_vol_skew(0.6, 2000, 1900, True)
    expected = price_with_spread(
        True, 2000, 1900, 2 / 365, 0.05, expected_skew, expected_spread
    )
    assert spread == expected_spread
    assert skew == pytest.approx(expected_skew)
    assert premium == pytest.approx(expected)


def test_call_floor_never_relaxes_an_individual_lot_basis():
    low = AssignmentLot(1, 1, 1800, 1750, 0, 1800, 50)
    high = AssignmentLot(2, 1, 2200, 2100, 0, 2200, 100)
    lots = [low, high]
    assert protected_call_floor(high, lots, "lot_gross", 50) == 2250
    assert protected_call_floor(high, lots, "lot_gross_plus_weighted_gross", 50) == 2250
    assert protected_call_floor(high, lots, "lot_gross_plus_weighted_net", 50) == 2250


def test_call_uses_nearest_five_dollar_strike_strictly_above_basis():
    assert select_protected_call_strike(2000, 5) == 2005
    assert select_protected_call_strike(2050, 5) == 2055


def test_call_strike_does_not_move_with_spot_or_target_delta():
    lot = AssignmentLot(1, 1, 2000, 1950, 0, 2000, 50)
    floor = protected_call_floor(lot, [lot], "lot_gross", 0)
    strike = select_protected_call_strike(floor, 5)
    assert strike == 2005
    assert strike < 2100  # ITM is valid when it still protects gross basis.


def test_assignment_lot_is_called_only_above_gross_basis(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [
            (start, 2000),
            (start + timedelta(days=2), 1500),
            (start + timedelta(days=4), 2600),
        ],
    )
    settings = _settings()
    config = StrategyConfig(0.4, 0.5, 0, 0, "lot_gross", settings.cost_scenarios[0])
    result = run_strategy(
        series=series,
        settings=settings,
        window_days=4,
        config=config,
    )
    assert result["assignments"] >= 1
    assert result["covered_calls_called"] >= 1
    assert result["complete_cycles"] >= 1
    assert result["realized_low_high_pnl_usdc"] > 0


def test_probe_gate_is_fixed_and_passes_complete_causal_data(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [(start, 2000), (start + timedelta(days=4), 2100)],
    )
    output = tmp_path / "probe.json"
    result = run_coverage_probe(series, _settings(), output)
    assert result["gate_defined_before_return_inspection"] is True
    assert result["passed"] is True
    assert result["windows"][0]["missing_rows"] == 0


def test_market_lookup_is_causal_when_future_rows_are_added(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    decision = start + timedelta(hours=2)
    first = _series(tmp_path, [(start, 2000), (start + timedelta(days=1), 9999)])
    before = first.spot_at(int(decision.timestamp() * 1000), 8)
    assert before is not None
    assert before.value == 2000


def test_hourly_close_is_available_only_after_candle_ends(tmp_path):
    cutoff = datetime(2026, 1, 2, 20, tzinfo=UTC)
    raw_timestamp = cutoff - timedelta(days=1, hours=12)
    path = extract_asset_market_snapshot(
        output_dir=tmp_path / "ETH",
        cutoff=cutoff,
        symbol="ETH",
        deribit_currency="ETH",
        deribit_index_name="eth_usd",
        deribit_perpetual="ETH-PERPETUAL",
        lookback_days=1,
        client=_HourlyCandleClient(int(raw_timestamp.timestamp() * 1000)),
    )
    series = MarketSeries(path)

    unavailable = series.spot_at(int(raw_timestamp.timestamp() * 1000), 8)
    available_at = raw_timestamp + timedelta(hours=1)
    available = series.spot_at(int(available_at.timestamp() * 1000), 8)

    assert unavailable is None
    assert available is not None
    assert available.value == 2000
    assert available.age_hours == 0


def test_maximum_drawdown_marks_intracycle_option_liability(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [
            (start, 2000),
            (start + timedelta(hours=10), 100),
            (start + timedelta(hours=11), 2000),
            (start + timedelta(days=2), 2000),
        ],
    )
    settings = _settings(window_days=(2,))
    config = StrategyConfig(0.4, 0.5, 0, 0, "lot_gross", settings.cost_scenarios[0])

    result = run_strategy(
        series=series,
        settings=settings,
        window_days=2,
        config=config,
    )

    assert result["maximum_drawdown"] < -0.4
    assert result["daily_volatility"] < 0.01


def test_vectorized_liability_matches_scalar_black_scholes():
    opened_at = int(datetime(2026, 1, 1, 8, tzinfo=UTC).timestamp())
    position = OptionPosition(
        position_id=1,
        is_put=True,
        strike=1900,
        amount_eth=10,
        opened_at=opened_at,
        expiry=opened_at + 48 * 3600,
        premium_gross_usdc=100,
        premium_net_usdc=100,
    )
    timestamps = np.array(
        [
            opened_at * 1000,
            (opened_at + 10 * 3600) * 1000,
            position.expiry * 1000,
        ],
        dtype=np.float64,
    )
    spots = np.array([2000.0, 1700.0, 2100.0])
    ivs = np.array([0.6, 0.0, 0.8])

    for is_put in (True, False):
        candidate = replace(position, is_put=is_put)
        vectorized = _vectorized_option_liability(
            candidate, timestamps, spots, ivs, 0.05
        )
        scalar = [
            _fair_option_liability(candidate, int(timestamp), spot, iv, 0.05)
            for timestamp, spot, iv in zip(timestamps, spots, ivs, strict=True)
        ]
        assert vectorized == pytest.approx(scalar)


def test_missing_opening_market_preserves_assigned_eth_exposure(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [
            (start, 2000),
            (start + timedelta(days=2), 1500),
            (start + timedelta(days=3), 100),
            (start + timedelta(days=3, hours=1), 1500),
            (start + timedelta(days=4), 1500),
        ],
    )
    payload = json.loads((tmp_path / "market.json").read_text())
    missing_start = int((start + timedelta(hours=42)).timestamp() * 1000)
    missing_end = int((start + timedelta(days=4)).timestamp() * 1000)
    payload["iv"] = [
        row
        for row in payload["iv"]
        if not missing_start <= row["timestamp_ms"] <= missing_end
    ]
    (tmp_path / "market.json").write_text(json.dumps(payload))
    series = MarketSeries(tmp_path / "market.json")
    settings = _settings()
    config = StrategyConfig(0.4, 0.5, 0, 0, "lot_gross", settings.cost_scenarios[0])

    result = run_strategy(
        series=series,
        settings=settings,
        window_days=4,
        config=config,
    )

    assert result["ending_eth"] > 0
    assert result["eth_exposure_hours"] == 48
    assert result["missing_market_events"] == 1
    assert result["maximum_drawdown"] < -0.2


@pytest.mark.parametrize(
    ("window_days", "raw_count", "expected"),
    [(30, 91, 7), (90, 91, 3), (180, 91, 2)],
)
def test_effective_sample_count_discounts_overlapping_windows(
    window_days, raw_count, expected
):
    assert production.effective_sample_count(raw_count, window_days, 2) == expected


def test_mm_counterparty_option_transfer_reconciles(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [(start, 2000), (start + timedelta(days=4), 2100)],
    )
    settings = _settings()
    config = StrategyConfig(0.4, 0.5, 0, 0, "lot_gross", settings.cost_scenarios[0])
    result = run_strategy(
        series=series,
        settings=settings,
        window_days=4,
        config=config,
    )
    assert result["mm_premium_paid_usdc"] > 0
    assert result["option_transfer_reconciliation_usdc"] == pytest.approx(0)
    assert result["mm_hedged_pnl_usdc"] == pytest.approx(
        result["mm_unhedged_pnl_usdc"]
        + result["mm_hedge_pnl_usdc"]
        - result["mm_hedge_cost_usdc"]
    )


def test_multiyear_summary_has_percentiles_regimes_capacity_and_btc_config(tmp_path):
    config_path = Path(__file__).parents[1] / "backtests" / "b1n_345" / "config.json"
    loaded = load_settings(config_path)
    assert {asset.symbol for asset in loaded.assets} == {"ETH", "BTC"}
    assert loaded.asset("BTC").strike_increment_usd == 25

    start = datetime(2025, 1, 1, 8, tzinfo=UTC)
    series = _series(
        tmp_path,
        [(start, 2000), (start + timedelta(days=220), 2600)],
    )
    validation = ProductionValidationSettings(
        lookback_days=220,
        rolling_step_days=30,
        target_deltas=(0.2,),
        utilization=1.0,
        minimum_premium_bps=0,
        call_margin_usd=0,
        protection_mode="lot_gross",
        cost_scenario="base",
        benchmark_usdc_apy=0.03,
        minimum_risk_premium_apy=0.05,
        regime_return_thresholds={30: 0.05},
        crash_drawdown_threshold=-0.2,
        crash_iv_spike_threshold=0.15,
        maximum_loss_probability=1,
        maximum_worst_drawdown=-1,
        minimum_mm_hedged_return=-1,
        minimum_regime_samples=0,
        capacity_curve=(CapacityPoint(100_000, 0, 0),),
    )
    settings = replace(
        _settings(window_days=(30,)),
        assets=(AssetSettings("ETH", "ETH", "eth_usd", "ETH-PERPETUAL", 5, (0,)),),
        production_validation=validation,
    )
    assert len(rolling_end_times(series, 30, 30)) > 1
    rows = run_rolling_validation({"ETH": series}, settings)
    summary = build_production_summary(rows, settings)
    policy = summary["policies"][0]
    assert policy["effective_sample_count"] == policy["sample_count"]
    assert set(policy["return_distribution"]) == {
        "mean",
        "p5",
        "p25",
        "p50",
        "p75",
        "p95",
        "worst",
    }
    assert {item["regime"] for item in policy["regimes"]} == {
        "bull",
        "bear",
        "sideways",
        "volatility_crash",
    }
    assert policy["capacity"][0]["observation_class"] == "modeled"

    wheel_row = next(row for row in rows if row["strategy"] == "wheel")
    overlapping_validation = replace(
        validation,
        rolling_step_days=2,
        minimum_regime_samples=2,
    )
    overlapping_settings = replace(
        settings,
        production_validation=overlapping_validation,
    )
    overlapping = build_production_summary(
        [dict(wheel_row) for _ in range(15)],
        overlapping_settings,
    )
    row_regime = wheel_row["regime"]
    regime = next(
        item
        for item in overlapping["policies"][0]["regimes"]
        if item["regime"] == row_regime
    )
    assert regime["sample_count"] == 15
    assert regime["effective_sample_count"] == 1
    assert regime["sufficient_sample"] is False
