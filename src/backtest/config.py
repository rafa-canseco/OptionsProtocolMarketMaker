from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.backtest.models import CostScenario


@dataclass(frozen=True)
class CoverageGate:
    minimum_causal_coverage: float
    maximum_missing_fraction: float
    maximum_spot_age_hours: float
    maximum_iv_age_hours: float


@dataclass(frozen=True)
class BacktestSettings:
    initial_usdc: float
    cadence_hours: int
    window_days: tuple[int, ...]
    target_deltas: tuple[float, ...]
    utilizations: tuple[float, ...]
    minimum_premium_bps: tuple[int, ...]
    call_margins_usd: tuple[float, ...]
    protection_modes: tuple[str, ...]
    strike_increment_usd: float
    risk_free_rate: float
    coverage_gate: CoverageGate
    cost_scenarios: tuple[CostScenario, ...]


def load_settings(path: Path) -> BacktestSettings:
    raw = json.loads(path.read_text())
    gate = CoverageGate(**raw["coverage_gate"])
    costs = tuple(CostScenario(**item) for item in raw["cost_scenarios"])
    return BacktestSettings(
        initial_usdc=float(raw["initial_usdc"]),
        cadence_hours=int(raw["cadence_hours"]),
        window_days=tuple(int(v) for v in raw["window_days"]),
        target_deltas=tuple(float(v) for v in raw["target_deltas"]),
        utilizations=tuple(float(v) for v in raw["utilizations"]),
        minimum_premium_bps=tuple(int(v) for v in raw["minimum_premium_bps"]),
        call_margins_usd=tuple(float(v) for v in raw["call_margins_usd"]),
        protection_modes=tuple(raw["protection_modes"]),
        strike_increment_usd=float(raw["strike_increment_usd"]),
        risk_free_rate=float(raw["risk_free_rate"]),
        coverage_gate=gate,
        cost_scenarios=costs,
    )
