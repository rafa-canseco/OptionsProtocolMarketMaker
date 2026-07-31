from src import config, meta_wheel_allocator
from src.covered_call_allocator import (
    call_collateral_target,
    load_covered_call_policy,
)
from src.fund_allocator import liquid_collateral_target, load_testnet_policy


def test_disabled_wheel_does_not_start_or_change_standalone_planning(monkeypatch):
    csp = load_testnet_policy("policies/csp_fund_policy.v3.base-sepolia.json")
    covered_call = load_covered_call_policy(
        "policies/covered_call_fund_policy.v5.base-sepolia.json"
    )
    csp_before = liquid_collateral_target(1_000 * 10**6, csp)
    call_before = call_collateral_target(10**18, covered_call)
    monkeypatch.setattr(config, "META_WHEEL_ALLOCATOR_ENABLED", False)
    monkeypatch.setattr(
        meta_wheel_allocator,
        "_chain_port_factory",
        lambda: (_ for _ in ()).throw(AssertionError("Wheel port must stay unused")),
    )

    assert meta_wheel_allocator.start() is None
    assert liquid_collateral_target(1_000 * 10**6, csp) == csp_before
    assert call_collateral_target(10**18, covered_call) == call_before
