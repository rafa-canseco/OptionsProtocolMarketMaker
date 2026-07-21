from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostScenario:
    name: str
    base_spread_bps: int
    execution_slippage_bps: int
    fee_bps_notional: int
    gas_usdc: float
    operational_delay_minutes: int
    mm_hedge_cost_bps: float = 0.0


@dataclass(frozen=True)
class StrategyConfig:
    target_delta: float
    utilization: float
    minimum_premium_bps: int
    call_margin_usd: float
    protection_mode: str
    costs: CostScenario


@dataclass
class AssignmentLot:
    lot_id: int
    eth_amount: float
    gross_basis: float
    net_basis: float
    assigned_at: int
    source_put_strike: float
    source_put_premium_usdc: float


@dataclass
class OptionPosition:
    position_id: int
    is_put: bool
    strike: float
    amount_eth: float
    opened_at: int
    expiry: int
    premium_gross_usdc: float
    premium_net_usdc: float
    pricing_source: str = "modeled"
    lot_id: int | None = None


@dataclass
class Ledger:
    cash_usdc: float
    lots: list[AssignmentLot] = field(default_factory=list)
    premium_gross_usdc: float = 0.0
    premium_net_usdc: float = 0.0
    premium_modeled_usdc: float = 0.0
    estimated_costs_usdc: float = 0.0
    turnover_usdc: float = 0.0
    realized_low_high_pnl_usdc: float = 0.0
    csp_opened: int = 0
    csp_settled: int = 0
    assignments: int = 0
    calls_opened: int = 0
    calls_called: int = 0
    complete_cycles: int = 0
    skipped_minimum_premium: int = 0
    missing_market_events: int = 0
    lot_floor_breach_opportunities: int = 0
    eth_exposure_hours: float = 0.0
    eth_idle_hours: float = 0.0
    eth_amount_hours: float = 0.0
    eth_idle_amount_hours: float = 0.0
    idle_usdc_hours: float = 0.0
    total_usdc_hours: float = 0.0
    mm_premium_paid_usdc: float = 0.0
    mm_option_payoff_usdc: float = 0.0
    mm_hedge_pnl_usdc: float = 0.0
    mm_hedge_cost_usdc: float = 0.0
    mm_hedge_turnover_usdc: float = 0.0

    @property
    def eth_amount(self) -> float:
        return sum(lot.eth_amount for lot in self.lots)
