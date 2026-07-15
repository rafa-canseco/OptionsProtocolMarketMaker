import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.backtest.config import BacktestSettings, CoverageGate
from src.backtest.data import MarketSeries
from src.backtest.engine import (
    binary_bid_premium,
    protected_call_floor,
    run_strategy,
    select_protected_call_strike,
)
from src.backtest.models import AssignmentLot, CostScenario, StrategyConfig
from src.backtest.probe import run_coverage_probe
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
        "cutoff": cutoff.isoformat(),
        "spot": rows,
        "iv": [dict(row, value=0.6) for row in rows],
    }
    path = tmp_path / "market.json"
    path.write_text(json.dumps(payload))
    return MarketSeries(path)


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
