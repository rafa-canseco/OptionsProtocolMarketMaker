import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.backtest.config import (
    AssetSettings,
    BacktestSettings,
    CoverageGate,
)
from src.backtest.csp_fund_policy import (
    FundRiskSettings,
    PhysicalCspCandidate,
    build_candidates,
    build_policy,
    candidate_put_strike,
    fixed_moneyness_put_strike,
    run_physical_csp_window,
    selection_sort_key,
    summarize_candidate,
)
from src.backtest.data import MarketSeries
from src.backtest.models import CostScenario


def _settings(window_days=(4,)) -> BacktestSettings:
    costs = (
        CostScenario("base", 200, 0, 0, 0, 0, 5),
        CostScenario("stressed", 300, 0, 0, 0, 15, 10),
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
        cost_scenarios=costs,
        assets=(AssetSettings("ETH", "ETH", "eth_usd", "ETH-PERPETUAL", 5, (0,)),),
    )


def _series(
    tmp_path: Path,
    *,
    start: datetime,
    end: datetime,
    spot_points: dict[datetime, float],
    iv: float = 0.6,
) -> MarketSeries:
    rows = []
    last = spot_points[min(spot_points)]
    timestamp = start
    while timestamp <= end:
        last = spot_points.get(timestamp, last)
        rows.append(
            {
                "timestamp_ms": int(timestamp.timestamp() * 1000),
                "value": last,
                "source": "observed",
            }
        )
        timestamp += timedelta(hours=1)
    payload = {
        "asset": "ETH",
        "start": start.isoformat(),
        "cutoff": end.isoformat(),
        "spot": rows,
        "iv": [dict(row, value=iv) for row in rows],
    }
    path = tmp_path / "market.json"
    path.write_text(json.dumps(payload))
    return MarketSeries(path)


def _candidate(settings: BacktestSettings, utilization=0.5) -> PhysicalCspCandidate:
    return PhysicalCspCandidate(
        strike_rule="fixed_moneyness_below_spot",
        strike_parameter=0.1,
        utilization=utilization,
        minimum_net_premium_bps=0,
        costs=settings.cost_scenarios[0],
    )


def test_itm_put_physically_exchanges_usdc_for_weth(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    end = start + timedelta(days=2)
    series = _series(
        tmp_path,
        start=start,
        end=end,
        spot_points={start: 2000, end: 1500},
    )
    settings = _settings(window_days=(2,))
    result = run_physical_csp_window(
        series=series,
        settings=settings,
        window_days=2,
        end=end,
        candidate=_candidate(settings),
        fund_risk=FundRiskSettings(1.0, 0.01),
    )

    assert result["assignments"] == 1
    assert result["ending_weth"] > 0
    assert result["collateral_assigned_usdc"] > 0
    assert result["cash_usdc"] < settings.initial_usdc
    assert result["assignment_settlement"] == "physical_weth_inventory"
    assert result["cash_settlement_used"] is False
    assert result["covered_calls_used"] is False
    assert result["final_nav_reconciliation_error_usdc"] == pytest.approx(0)


def test_weth_inventory_cap_stops_the_next_entry(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    midpoint = start + timedelta(days=2)
    end = start + timedelta(days=4)
    series = _series(
        tmp_path,
        start=start,
        end=end,
        spot_points={start: 2000, midpoint: 1500, end: 1500},
    )
    settings = _settings()
    result = run_physical_csp_window(
        series=series,
        settings=settings,
        window_days=4,
        end=end,
        candidate=_candidate(settings),
        fund_risk=FundRiskSettings(0.25, 0.01),
    )

    assert result["positions_opened"] == 1
    assert result["assignments"] == 1
    assert result["skipped_weth_inventory"] == 1
    assert result["peak_weth_nav_fraction"] >= 0.25


@pytest.mark.parametrize(
    ("distance", "expected"),
    ((0.1, 1800.0), (0.15, 1700.0)),
)
def test_fixed_moneyness_strike_is_the_requested_distance(distance, expected):
    assert (
        fixed_moneyness_put_strike(
            spot=2000,
            moneyness_below_spot=distance,
            strike_increment=5,
        )
        == expected
    )


def test_fixed_moneyness_strike_rounds_away_from_spot():
    strike = fixed_moneyness_put_strike(
        spot=2003,
        moneyness_below_spot=0.1,
        strike_increment=5,
    )
    assert strike == 1800
    assert strike <= 2003 * 0.9


def test_delta_strike_moves_with_implied_volatility():
    settings = _settings()
    candidate = PhysicalCspCandidate(
        strike_rule="target_put_delta",
        strike_parameter=0.1,
        utilization=0.25,
        minimum_net_premium_bps=0,
        costs=settings.cost_scenarios[0],
    )
    lower_iv = candidate_put_strike(
        candidate=candidate,
        spot=2000,
        iv=0.4,
        time_years=2 / 365,
        risk_free_rate=0.05,
        strike_increment=5,
    )
    higher_iv = candidate_put_strike(
        candidate=candidate,
        spot=2000,
        iv=1.0,
        time_years=2 / 365,
        risk_free_rate=0.05,
        strike_increment=5,
    )
    assert lower_iv is not None and higher_iv is not None
    assert higher_iv < lower_iv


def test_candidate_family_is_closed_and_contains_primary():
    root = Path(__file__).parents[1]
    config = json.loads((root / "backtests" / "b1n_356" / "config.json").read_text())
    candidates = build_candidates(
        config=config,
        settings=_settings(),
        cost_name="base",
    )
    assert len(candidates) == 30
    assert any(
        candidate.strike_rule == "fixed_moneyness_below_spot"
        and candidate.strike_parameter == 0.1
        and candidate.utilization == 0.25
        and candidate.minimum_net_premium_bps == 25
        for candidate in candidates
    )
    assert any(
        candidate.strike_rule == "target_put_delta"
        and candidate.strike_parameter == 0.15
        and candidate.utilization == 0.5
        and candidate.minimum_net_premium_bps == 0
        for candidate in candidates
    )


def test_summary_requires_returns_risk_activity_and_coverage():
    row = {
        "candidate_id": "candidate",
        "strike_rule": "target_put_delta",
        "strike_parameter": 0.1,
        "utilization": 0.25,
        "minimum_net_premium_bps": 25,
        "costs": "base",
        "window_days": 30,
        "absolute_return": 0.02,
        "maximum_drawdown": -0.1,
        "open_rate": 0.5,
        "assignment_frequency": 0.1,
        "assignments": 1,
        "positions_settled": 10,
        "missing_market_fraction": 0.0,
    }
    gates = {
        "window_days": [30],
        "benchmark_usdc_apy": 0.032,
        "minimum_risk_premium_apy": 0.05,
        "maximum_loss_probability": 0.25,
        "maximum_worst_drawdown": -0.3,
        "minimum_open_rate": 0.1,
        "maximum_missing_market_fraction": 0.05,
    }
    summary = summarize_candidate(rows=[row], decision_gates=gates)
    assert summary["all_economic_gates_pass"] is True

    loss = dict(row, absolute_return=-0.01)
    failed = summarize_candidate(rows=[loss], decision_gates=gates)
    assert failed["all_economic_gates_pass"] is False
    assert selection_sort_key(summary) < selection_sort_key(failed)


def test_policy_can_authorize_capped_testnet_validation_without_economic_go():
    candidate = {
        "strike_rule": "fixed_moneyness_below_spot",
        "strike_parameter": 0.15,
        "utilization": 0.25,
        "minimum_net_premium_bps": 25,
        "costs": "base",
    }
    checks = {
        "median_return_above_hurdle": False,
        "loss_probability_within_limit": False,
        "worst_drawdown_within_limit": True,
        "minimum_open_rate_met": True,
        "market_coverage_within_limit": True,
    }
    summary = {
        "candidate": candidate,
        "all_economic_gates_pass": False,
        "all_activity_gates_pass": True,
        "windows": [{"checks": checks}],
    }
    config = {
        "scope": {"cadence_hours": 48},
        "issue": "B1N-356",
        "fund_policy": {"maximum_weth_nav_fraction_for_new_entry": 0.25},
        "evidence_gates": {
            "observed_executable_quote_liquidity": False,
            "observed_onchain_physical_assignment": False,
            "observed_fund_flow_reconciliation": False,
            "observed_nav_liability_reconciliation": False,
        },
        "base_sepolia_validation_bounds": {
            "chain_id": 84532,
            "mock_assets_only": True,
            "maximum_vault_aum_usdc": 25,
            "maximum_positions": 1,
            "maximum_opened_positions_before_review": 10,
            "maximum_assignments_before_review": 1,
            "maximum_utilization": 0.25,
            "maximum_collateral_per_position_usdc": 6.25,
            "maximum_weth_nav_fraction": 0.25,
            "mainnet_authorized": False,
        },
    }

    policy = build_policy(
        config=config,
        development=summary,
        validation_base=summary,
        validation_stressed=summary,
        source_digest="digest",
    )

    assert policy["decision"] == "base_sepolia_validation_go"
    assert policy["economic_decision"] == "no_go"
    assert policy["activation_allowed"] is True
    assert policy["mainnet_authorized"] is False
    assert policy["base_sepolia_overrides"]["maximum_vault_aum_usdc"] == 25
    assert (
        policy["base_sepolia_overrides"]["maximum_collateral_per_position_usdc"] == 6.25
    )
