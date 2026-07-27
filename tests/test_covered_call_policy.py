import pytest

from src.backtest.config import BacktestSettings, CoverageGate
from src.backtest.covered_call_policy import (
    CoveredCallCandidate,
    build_policy,
    candidate_call_strike,
    fixed_moneyness_call_strike,
)
from src.backtest.models import CostScenario


def _candidate(rule="fixed_moneyness_above_spot", parameter=0.15):
    return CoveredCallCandidate(
        strike_rule=rule,
        strike_parameter=parameter,
        utilization=0.25,
        minimum_net_premium_bps=0,
        normalization_slippage_bps=50,
        costs=CostScenario("base", 200, 0, 0, 0, 5),
    )


def _settings():
    return BacktestSettings(
        initial_usdc=100_000,
        cadence_hours=48,
        window_days=(30, 90),
        target_deltas=(0.05, 0.1, 0.15),
        utilizations=(0.25,),
        minimum_premium_bps=(0,),
        call_margins_usd=(0,),
        protection_modes=("lot_gross",),
        strike_increment_usd=25,
        risk_free_rate=0.05,
        coverage_gate=CoverageGate(0.95, 0.05, 8, 6),
        cost_scenarios=(_candidate().costs,),
    )


def test_fixed_call_strike_is_rounded_up_and_above_target():
    assert fixed_moneyness_call_strike(2000, 0.15, 25) == 2300
    assert fixed_moneyness_call_strike(2010, 0.15, 25) == 2325


@pytest.mark.parametrize("delta", [0.05, 0.1, 0.15])
def test_delta_call_strike_is_otm(delta):
    strike = candidate_call_strike(
        candidate=_candidate("target_call_delta", delta),
        spot=2000,
        iv=0.6,
        time_years=2 / 365,
        risk_free_rate=0.05,
        strike_increment=25,
    )
    assert strike is not None
    assert strike > 2000


def test_policy_never_grants_mainnet_without_observed_evidence():
    config = {
        "scope": {"cadence_hours": 48, "strike_tick_usd": 25},
        "candidate_family": {"maximum_delta_deviation_bps": 150},
        "evidence_gates": {"quotes": False},
        "valuation": {
            "interface_version": 1,
            "liability_buffer_bps": 1000,
            "observation_quorum": 2,
        },
        "base_sepolia_validation_bounds": {"mainnet_authorized": False},
    }
    window = {
        "window_days": 30,
        "checks": {
            "worst_drawdown_within_limit": True,
            "minimum_open_rate_met": True,
            "market_coverage_within_limit": True,
        },
    }
    summary = {
        "all_economic_gates_pass": True,
        "candidate": {
            "strike_rule": "fixed_moneyness_above_spot",
            "strike_parameter": 0.15,
            "utilization": 0.25,
            "minimum_net_premium_bps": 0,
        },
        "windows": [window],
    }
    policy = build_policy(
        config=config,
        development=summary,
        validation_base=summary,
        validation_stressed=summary,
        source_digest="abc",
    )
    assert policy["decision"] == "go_testnet_only"
    assert policy["mainnet_authorized"] is False
    assert policy["selection"]["called_away_action"].startswith("normalize_all")
