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
class AssetSettings:
    symbol: str
    deribit_currency: str
    deribit_index_name: str
    deribit_perpetual: str
    strike_increment_usd: float
    call_margins_usd: tuple[float, ...]


@dataclass(frozen=True)
class CapacityPoint:
    aum_usdc: float
    premium_haircut_bps: float
    hedge_cost_bps: float


@dataclass(frozen=True)
class ProductionValidationSettings:
    lookback_days: int
    rolling_step_days: int
    target_deltas: tuple[float, ...]
    utilization: float
    minimum_premium_bps: int
    call_margin_usd: float
    protection_mode: str
    cost_scenario: str
    benchmark_usdc_apy: float
    minimum_risk_premium_apy: float
    regime_return_thresholds: dict[int, float]
    crash_drawdown_threshold: float
    crash_iv_spike_threshold: float
    maximum_loss_probability: float
    maximum_worst_drawdown: float
    minimum_mm_hedged_return: float
    minimum_regime_samples: int
    capacity_curve: tuple[CapacityPoint, ...]


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
    assets: tuple[AssetSettings, ...] = ()
    production_validation: ProductionValidationSettings | None = None

    def asset(self, symbol: str) -> AssetSettings:
        for asset in self.assets:
            if asset.symbol == symbol:
                return asset
        return AssetSettings(
            symbol=symbol,
            deribit_currency=symbol,
            deribit_index_name=f"{symbol.lower()}_usd",
            deribit_perpetual=f"{symbol}-PERPETUAL",
            strike_increment_usd=self.strike_increment_usd,
            call_margins_usd=self.call_margins_usd,
        )


def load_settings(path: Path) -> BacktestSettings:
    raw = json.loads(path.read_text())
    gate = CoverageGate(**raw["coverage_gate"])
    costs = tuple(CostScenario(**item) for item in raw["cost_scenarios"])
    assets = tuple(
        AssetSettings(
            **{key: value for key, value in item.items() if key != "call_margins_usd"},
            call_margins_usd=tuple(float(value) for value in item["call_margins_usd"]),
        )
        for item in raw.get("assets", [])
    )
    validation_raw = raw.get("production_validation")
    validation = None
    if validation_raw:
        validation = ProductionValidationSettings(
            **{
                key: value
                for key, value in validation_raw.items()
                if key
                not in ("target_deltas", "regime_return_thresholds", "capacity_curve")
            },
            target_deltas=tuple(
                float(value) for value in validation_raw["target_deltas"]
            ),
            regime_return_thresholds={
                int(key): float(value)
                for key, value in validation_raw["regime_return_thresholds"].items()
            },
            capacity_curve=tuple(
                CapacityPoint(**item) for item in validation_raw["capacity_curve"]
            ),
        )
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
        assets=assets,
        production_validation=validation,
    )
