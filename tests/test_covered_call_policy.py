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
            "valuation_policy_version": 2,
            "model_name": "b1nary-european-bs-call-v1",
            "model_version": 1,
            "methodology": "european_black_scholes",
            "exercise_style": "european",
            "accounting_asset": "WETH",
            "liability_formula": (
                "ceil(call_price_usd8 * option_amount_8 * 1e10 / "
                "spot_price_8), capped_at_collateral_weth"
            ),
            "stress_liability": "full_collateral_weth_api_telemetry_only",
            "liability_buffer_bps": 0,
            "max_observation_divergence_bps": 500,
            "observation_quorum": 2,
            "approved_observers": [
                "0x3b7f3e42eaCB2E0361aE41e426ea65C6f7896D1e",
                "0x62A7e8c11E4eFc8ed696b2A08D9ccfC339424754",
            ],
            "maximum_observation_window_blocks": 120,
            "source_quality": "single_model_multi_signer",
            "nonce": {
                "model_version_bits": 64,
                "sequence_bits": 192,
                "sequence_must_be_nonzero": True,
            },
            "spot": {
                "asset_pair": "ETH/USD",
                "feed_type": "valuator_configured_chainlink",
                "feed_address": "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1",
                "feed_address_source": "manifest",
                "feed_decimals": 8,
                "maximum_staleness_seconds": 3600,
            },
            "implied_volatility": {
                "bps": 4200,
                "source": "approved",
                "scope": "covered_call_testnet_fair_nav",
            },
            "risk_free_rate_bps": 500,
            "settlement_cost_bps": 0,
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
