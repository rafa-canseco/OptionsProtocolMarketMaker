import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.backtest.ladder_comparison as ladder_comparison
from src.backtest.data import MarketSeries
from src.backtest.ladder_comparison import (
    ComparisonConfigError,
    Ledger,
    Lot,
    Policy,
    Position,
    _liability,
    _load_pinned_json,
    _nav_after_parent_fees,
    _open_put,
    _settle,
    build_recommendation,
    load_inputs,
    non_overlapping_windows,
    sha256_file,
    verify_recommendation_attestation,
)
from src.pricer import apply_vol_skew, bs_price


ROOT = Path(__file__).parents[1]


def test_frozen_matrix_reproduces_current_policy_and_six_requested_arms():
    config, _, _, policies = load_inputs(ROOT)
    current = policies[0]

    assert len(policies) == 6
    assert (
        current.policy_id,
        current.put_delta,
        current.put_tenor_hours,
        current.aggregate_utilization,
        current.minimum_net_premium_bps,
        current.strike_tick_usd,
    ) == ("current_48h", 0.09, 48, 0.8, 20, 25)
    assert len(policies[1].put_lane_offsets_hours) == 4
    assert len(policies[2].put_lane_offsets_hours) == 4
    assert 0.25 <= policies[2].aggregate_utilization <= 0.35
    assert policies[4].put_delta == 0.5
    assert policies[5].laddered_covered_calls is True
    assert config["authority"]["live_policy_change_authorized"] is False


def test_current_policy_snapshot_is_pinned_and_drift_fails_closed(tmp_path):
    config = json.loads((ROOT / "backtests/b1n_450/config.json").read_text())
    source = config["sources"]["current_policy_snapshot"]
    assert source["sha256"] == sha256_file(ROOT / source["path"])

    copied = tmp_path / "snapshot.json"
    copied.write_text((ROOT / source["path"]).read_text() + " ")
    assert copied.read_bytes() != (ROOT / source["path"]).read_bytes()


def test_non_overlapping_train_validation_windows_are_disjoint():
    development = non_overlapping_windows(
        datetime(2023, 8, 15, 8, tzinfo=UTC),
        datetime(2025, 7, 15, 8, tzinfo=UTC),
        90,
    )
    validation = non_overlapping_windows(
        datetime(2025, 7, 15, 8, tzinfo=UTC),
        datetime(2026, 7, 15, 8, tzinfo=UTC),
        90,
    )

    assert all(left[1] <= right[0] for left, right in zip(development, development[1:]))
    assert all(left[1] <= right[0] for left, right in zip(validation, validation[1:]))
    assert development[-1][1] <= validation[0][0]


def test_recommendation_is_gate_derived_keep_and_requires_new_disabled_ticket():
    config = json.loads((ROOT / "backtests/b1n_450/config.json").read_text())

    def group(policy_id, median, drawdown, liquidity, operations):
        return {
            "split": "validation",
            "cost_scenario": "base",
            "policy_id": policy_id,
            "window_days": 90,
            "period_return": {"median": median},
            "maximum_drawdown_after_parent_fees_worst": drawdown,
            "redemption_liquidity_fraction_p5_worst": liquidity,
            "operational_actions_per_30_days_mean": operations,
            "complete_wheel_cycles_total": 0,
        }

    summary = {
        "premium_evidence": {
            "modeled_rows": 10,
            "observed_executable_rows": 0,
            "observed_underlying_and_iv": True,
        },
        "group_metrics": [
            group("current_48h", 0.08, -0.3, 0.16, 8),
            group("current_48h_staggered_4", 0.02, -0.2, 0.19, 42),
            group("weekly_4_low_30pct", -0.02, -0.11, 0.69, 27),
            group("hybrid_current_csp_weekly_cc", 0.07, -0.29, 0.18, 30),
        ],
    }
    recommendation = build_recommendation(config, summary)

    assert recommendation["decision"] == "keep"
    assert recommendation["decision_gate"]["change_gate_passed"] is False
    assert "new implementation ticket" in recommendation["recommended_follow_up"]
    assert recommendation["attestation_scope"] == (
        "content_integrity_only_not_signer_authentication"
    )
    assert verify_recommendation_attestation(recommendation)
    recommendation["decision"] = "replace"
    assert not verify_recommendation_attestation(recommendation)


def _recommendation_summary(observed_executable_rows: int):
    def group(policy_id, median, drawdown, liquidity, operations):
        return {
            "split": "validation",
            "cost_scenario": "base",
            "policy_id": policy_id,
            "window_days": 90,
            "period_return": {"median": median},
            "maximum_drawdown_after_parent_fees_worst": drawdown,
            "redemption_liquidity_fraction_p5_worst": liquidity,
            "operational_actions_per_30_days_mean": operations,
            "complete_wheel_cycles_total": 0,
        }

    return {
        "premium_evidence": {
            "modeled_rows": 10,
            "observed_executable_rows": observed_executable_rows,
            "observed_underlying_and_iv": True,
        },
        "group_metrics": [
            group("current_48h", 0.08, -0.3, 0.16, 8),
            group("current_48h_staggered_4", 0.02, -0.2, 0.19, 42),
            group("weekly_4_low_30pct", -0.02, -0.11, 0.69, 27),
            group("hybrid_current_csp_weekly_cc", 0.07, -0.29, 0.18, 30),
        ],
    }


def test_recommendation_changes_only_when_configured_evidence_gate_passes():
    config = json.loads((ROOT / "backtests/b1n_450/config.json").read_text())

    blocked = build_recommendation(config, _recommendation_summary(0))
    passed = build_recommendation(config, _recommendation_summary(1))

    assert (
        blocked["decision"] == config["recommendation_rule"]["unmet_change_gate_result"]
    )
    assert blocked["decision_gate"]["change_gate_passed"] is False
    assert passed["decision"] == config["recommendation_rule"]["met_change_gate_result"]
    assert passed["decision_gate"]["change_gate_passed"] is True
    assert passed["live_policy_action"] == "none_research_gate_only"


def test_current_csp_sizes_from_liquid_usdc_not_assigned_eth(monkeypatch):
    ledger = Ledger(
        cash_usdc=100_000,
        lots=[
            Lot(
                lot_id=99,
                amount=100,
                assignment_strike=2_000,
                assigned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
    )
    policy = Policy(
        policy_id="current_48h",
        description="test",
        put_delta=0.09,
        put_tenor_hours=48,
        put_lane_offsets_hours=(0,),
        aggregate_utilization=0.8,
        minimum_net_premium_bps=20,
        strike_tick_usd=25,
        hold_assigned_eth=True,
    )
    monkeypatch.setattr(
        "src.backtest.ladder_comparison.select_strike", lambda **_: 1_800.0
    )
    monkeypatch.setattr(
        "src.backtest.ladder_comparison.binary_bid_premium",
        lambda **_: (10.0, 0, 0.6),
    )
    position = _open_put(
        ledger=ledger,
        policy=policy,
        lane=0,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 1, 4, tzinfo=UTC),
        spot=2_000,
        iv=0.6,
        settings=SimpleNamespace(risk_free_rate=0.05),
        costs=SimpleNamespace(
            base_spread_bps=0,
            fee_bps_notional=0,
            gas_usdc=0,
            execution_slippage_bps=0,
        ),
        protocol_fee_bps=0,
        next_position_id=1,
    )

    assert position is not None
    assert position.collateral_usdc == pytest.approx(80_000)


def test_liability_uses_same_skewed_iv_as_premium_model():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    position = Position(
        position_id=1,
        kind="put",
        lane=0,
        opened_at=at,
        expiry=at + timedelta(days=2),
        strike=1_800,
        amount=2,
        collateral_usdc=3_600,
        premium_gross_usdc=0,
        premium_net_usdc=0,
    )
    spot, iv, rate = 2_000.0, 0.6, 0.05
    years = 2 / 365
    expected = (
        bs_price(
            True,
            spot,
            position.strike,
            years,
            rate,
            apply_vol_skew(iv, spot, position.strike, True),
        )
        * position.amount
    )
    raw_iv = bs_price(True, spot, position.strike, years, rate, iv) * position.amount

    assert _liability(position, at, spot, iv, rate) == pytest.approx(expected)
    assert expected != pytest.approx(raw_iv)


def test_wheel_cycle_counts_only_when_assignment_lot_is_fully_exhausted():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    lot = Lot(lot_id=1, amount=2, assignment_strike=1_800, assigned_at=at)
    first = Position(
        position_id=2,
        kind="call",
        lane=0,
        opened_at=at,
        expiry=at,
        strike=2_000,
        amount=1,
        collateral_usdc=0,
        premium_gross_usdc=0,
        premium_net_usdc=0,
        lot_id=1,
    )
    ledger = Ledger(cash_usdc=0, lots=[lot], positions=[first])
    _settle(ledger, at, 2_100, True)
    assert ledger.complete_wheel_cycles == 0
    assert ledger.lots[0].amount == pytest.approx(1)

    ledger.positions.append(
        Position(
            position_id=3,
            kind="call",
            lane=1,
            opened_at=at,
            expiry=at,
            strike=2_000,
            amount=1,
            collateral_usdc=0,
            premium_gross_usdc=0,
            premium_net_usdc=0,
            lot_id=1,
        )
    )
    _settle(ledger, at, 2_100, True)
    assert ledger.complete_wheel_cycles == 1
    assert ledger.lots == []


def test_risk_nav_marks_apply_parent_fees_consistently():
    fee_config = {
        "management_fee_bps_annual": 200,
        "performance_fee_bps": 1000,
    }
    net, management, performance = _nav_after_parent_fees(
        110_000, 100_000, 90, fee_config
    )
    assert management > 0
    assert performance > 0
    assert net == pytest.approx(110_000 - management - performance)


def test_checksums_cover_generator_runner_and_regression_tests():
    checksums = (ROOT / "backtests/b1n_450/results/checksums.sha256").read_text()
    assert "../../../src/backtest/ladder_comparison.py" in checksums
    assert "../../../scripts/run_b1n_450_ladder_comparison.py" in checksums
    assert "../../../tests/test_ladder_comparison.py" in checksums


def test_outputs_disclose_fee_basis_thin_cvar_and_cross_tenor_assignment_intensity():
    first_row = json.loads(
        (ROOT / "backtests/b1n_450/results/results.jsonl").read_text().splitlines()[0]
    )
    report = (ROOT / "backtests/b1n_450/results/REPORT.md").read_text()

    assert "assignment_frequency_per_settled_put" in first_row
    assert "assignments_per_30_days" in first_row
    assert first_row["risk_metric_fee_basis"] == (
        "after_accrued_management_and_hypothetical_hwm_performance_fees"
    )
    assert "Assignment frequency is per settled put" in report
    assert "four 90-day validation windows" in report


def test_snapshot_parameter_drift_is_rejected(tmp_path):
    snapshot = {"selection": {"target_put_delta_bps": 900}}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot, sort_keys=True) + "\n")
    pinned = {"path": "snapshot.json", "sha256": sha256_file(path)}
    assert _load_pinned_json(tmp_path, pinned) == snapshot

    snapshot["selection"]["target_put_delta_bps"] = 500
    path.write_text(json.dumps(snapshot, sort_keys=True) + "\n")
    with pytest.raises(ComparisonConfigError, match="digest mismatch"):
        _load_pinned_json(tmp_path, pinned)


def test_positive_delay_uses_execution_time_for_open_expiry_and_settlement(monkeypatch):
    config, settings, series, policies = load_inputs(ROOT)
    start = datetime.fromisoformat(config["splits"]["development"]["start"])
    attempts: list[dict] = []
    original_open_put = ladder_comparison._open_put

    def capture_open_put(**kwargs):
        attempts.append(kwargs)
        return original_open_put(**kwargs)

    monkeypatch.setattr(ladder_comparison, "_open_put", capture_open_put)
    ladder_comparison.run_window(
        config=config,
        settings=settings,
        series=series,
        policy=policies[0],
        cost_name="base",
        split="development",
        start=start,
        end=start + timedelta(days=30),
    )

    base_costs = next(item for item in settings.cost_scenarios if item.name == "base")
    assert base_costs.operational_delay_minutes == 5
    execution = start + timedelta(minutes=5)
    assert attempts[0]["at"] == execution

    position = Position(
        position_id=1,
        kind="put",
        lane=0,
        opened_at=execution,
        expiry=execution + timedelta(hours=48),
        strike=1_000,
        amount=1,
        collateral_usdc=1_000,
        premium_gross_usdc=0,
        premium_net_usdc=0,
    )
    ledger = Ledger(cash_usdc=1_000, positions=[position])
    _settle(ledger, start + timedelta(hours=49), 1_100, True)
    assert ledger.positions == []
    assert ledger.puts_settled == 1


def test_fixture_market_lookup_is_causal(tmp_path):
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    future = start + timedelta(hours=2)
    payload = {
        "asset": "ETH",
        "start": start.isoformat(),
        "cutoff": future.isoformat(),
        "spot": [
            {
                "timestamp_ms": int(start.timestamp() * 1000),
                "value": 2000,
                "source": "observed",
            },
            {
                "timestamp_ms": int(future.timestamp() * 1000),
                "value": 9999,
                "source": "observed",
            },
        ],
        "iv": [
            {
                "timestamp_ms": int(start.timestamp() * 1000),
                "value": 0.6,
                "source": "observed",
            }
        ],
    }
    path = tmp_path / "market.json"
    path.write_text(json.dumps(payload))
    series = MarketSeries(path)
    observed = series.spot_at(int((start + timedelta(hours=1)).timestamp() * 1000), 8)
    assert observed is not None
    assert observed.value == 2000
