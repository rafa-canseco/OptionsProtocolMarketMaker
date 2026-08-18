import json
from pathlib import Path

import pytest

from src.allocator_policy import load_allocator_policy


POLICY_PATH = Path(__file__).parents[1] / "policies" / "csp_fund_policy.v1.json"


def _write_policy(tmp_path: Path, mutate) -> Path:
    raw = json.loads(POLICY_PATH.read_text())
    mutate(raw)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    return path


def test_committed_policy_is_a_fail_closed_no_go():
    policy = load_allocator_policy(POLICY_PATH)

    assert policy.decision == "no_go"
    assert policy.activation_allowed is False
    assert set(policy.selected_parameters.values()) == {None}
    assert policy.base_sepolia_overrides["allocator_enabled"] is False
    assert policy.base_sepolia_overrides["maximum_vault_aum_usdc"] == 0


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_rejects_unsupported_schema_version(tmp_path, version):
    path = _write_policy(tmp_path, lambda raw: raw.update(schema_version=version))

    with pytest.raises(ValueError, match="unsupported schema_version"):
        load_allocator_policy(path)


def test_rejects_unknown_fields(tmp_path):
    path = _write_policy(tmp_path, lambda raw: raw.update(unexpected=True))

    with pytest.raises(ValueError, match=r"unknown=\['unexpected'\]"):
        load_allocator_policy(path)


def test_v1_rejects_go_decision(tmp_path):
    path = _write_policy(tmp_path, lambda raw: raw.update(decision="go"))

    with pytest.raises(ValueError, match="v1 supports no-go only"):
        load_allocator_policy(path)


def test_no_go_cannot_select_strategy_parameters(tmp_path):
    def select_delta(raw):
        raw["selection"]["target_put_delta"] = 0.1

    path = _write_policy(tmp_path, select_delta)

    with pytest.raises(ValueError, match="no-go cannot select parameters"):
        load_allocator_policy(path)


def test_no_go_cannot_authorize_testnet_capital(tmp_path):
    def authorize_capital(raw):
        raw["base_sepolia_overrides"]["maximum_vault_aum_usdc"] = 1

    path = _write_policy(tmp_path, authorize_capital)

    with pytest.raises(ValueError, match="testnet override must fail closed"):
        load_allocator_policy(path)


def test_rejects_unvalidated_curator_bound(tmp_path):
    def add_bound(raw):
        raw["curator_bounds"]["maximum_weth_inventory"] = 1

    path = _write_policy(tmp_path, add_bound)

    with pytest.raises(ValueError, match="unvalidated curator bound"):
        load_allocator_policy(path)


def test_rejects_invented_assignment_evidence(tmp_path):
    def pass_assignment(raw):
        raw["decision_gates"]["assignment"]["evidence_present"] = True
        raw["decision_gates"]["assignment"]["passed"] = True

    path = _write_policy(tmp_path, pass_assignment)

    with pytest.raises(ValueError, match="invalid assignment gate"):
        load_allocator_policy(path)


def test_rejects_wheel_metrics_in_csp_policy(tmp_path):
    def use_wheel(raw):
        raw["observed_metrics"][0]["strategy"] = "wheel"

    path = _write_policy(tmp_path, use_wheel)

    with pytest.raises(ValueError, match="invalid metric classification"):
        load_allocator_policy(path)


def test_reports_malformed_json_path(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{")

    with pytest.raises(ValueError, match=str(path)):
        load_allocator_policy(path)
