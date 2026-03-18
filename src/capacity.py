"""MM capacity calculation — shared pool with per-asset max exposure."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from web3 import Web3

from src import config, hedge_executor

if TYPE_CHECKING:
    from src.config import AssetConfig

log = logging.getLogger(__name__)

USDC_DECIMALS = 6
OTOKEN_DECIMALS = 8
FULL_THRESHOLD_ETH = 0.01
DEGRADED_HEDGE_RATIO = 0.4

# ERC-20 function selectors
_BALANCE_OF_SIG = "0x70a08231"
_ALLOWANCE_SIG = "0xdd62ed3e"

_INTERNAL_FIELDS = {
    "premium_pool_usd",
    "hedge_pool_usd",
    "hedge_pool_withdrawable_usd",
    "leverage",
    "open_positions_count",
    "open_positions_notional_usd",
}


@dataclass
class CapacityReport:
    mm_address: str
    asset: str
    capacity_eth: float
    capacity_usd: float
    premium_pool_usd: float
    hedge_pool_usd: float
    hedge_pool_withdrawable_usd: float
    leverage: int
    open_positions_count: int
    open_positions_notional_usd: float
    status: str
    updated_at: int

    def to_dict(self, internal: bool = True) -> dict:
        result = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if not internal and f.name in _INTERNAL_FIELDS:
                continue
            result[f.name] = val
        return result


def capacity_status(
    capacity_eth: float,
    premium_pool_usd: float,
    hedge_pool_usd: float,
    hedge_live: bool = True,
) -> str:
    if capacity_eth < FULL_THRESHOLD_ETH:
        return "full"
    if (
        hedge_live
        and premium_pool_usd > 0
        and hedge_pool_usd < DEGRADED_HEDGE_RATIO * premium_pool_usd
    ):
        return "degraded"
    return "active"


def _read_usdc_balance(w3: Web3, mm_address: str) -> float:
    addr_padded = mm_address.lower().replace("0x", "").zfill(64)
    data = _BALANCE_OF_SIG + addr_padded
    raw = w3.eth.call({"to": config.USDC_ADDRESS, "data": data})
    return int.from_bytes(raw, "big") / 10**USDC_DECIMALS


def _read_usdc_allowance(w3: Web3, mm_address: str) -> float:
    owner = mm_address.lower().replace("0x", "").zfill(64)
    spender = config.MARGIN_POOL_ADDRESS.lower().replace("0x", "").zfill(64)
    data = _ALLOWANCE_SIG + owner + spender
    raw = w3.eth.call({"to": config.USDC_ADDRESS, "data": data})
    return int.from_bytes(raw, "big") / 10**USDC_DECIMALS


def _compute_global_pools(
    w3: Web3,
    mm_address: str,
    total_premium_committed: float,
) -> tuple[float, float, float]:
    """Compute premium pool and hedge pool (global, shared across assets).

    Returns:
        (premium_pool_usd, hedge_pool_value_usd, hedge_withdrawable_usd)
    """
    usdc_balance = _read_usdc_balance(w3, mm_address)
    usdc_allowance = _read_usdc_allowance(w3, mm_address)
    premium_pool = max(min(usdc_balance, usdc_allowance) - total_premium_committed, 0.0)

    if config.HEDGE_MODE == "live":
        withdrawable = hedge_executor.get_withdrawable()
        hedge_pool_value = hedge_executor.get_account_value()
    else:
        withdrawable = 0.0
        hedge_pool_value = 0.0

    return premium_pool, hedge_pool_value, withdrawable


def calculate_capacity_internal(
    w3: Web3,
    spot: float,
    mm_address: str,
    tracker,
    asset_config: AssetConfig | None = None,
) -> CapacityReport:
    """Calculate MM capacity for a specific asset using shared pool model.

    Shared pool with per-asset max exposure:
        total_capital = min(premium_pool, hedge_notional)
        deployed_total = sum(notional across ALL open positions)
        deployed_this = sum(notional for THIS asset)
        available_global = total_capital - deployed_total
        capacity = min(max_exposure * total_capital - deployed_this,
                       available_global)
    """
    if asset_config is None:
        asset_config = config.ASSET_MAP.get("eth", config.ASSETS[0])

    total_premium_committed = tracker.total_premium_paid()
    premium_pool, hedge_pool_value, withdrawable = _compute_global_pools(
        w3, mm_address, total_premium_committed
    )

    # Compute total capital.  Per-asset leverage is intentional:
    # higher-leverage assets see more notional headroom, but
    # max_exposure caps and the global available_global constraint
    # prevent over-allocation across the shared pool.
    if config.HEDGE_MODE == "live":
        reserve = config.CAPACITY_RESERVE_RATIO
        hedge_notional = withdrawable * asset_config.leverage * (1.0 - reserve)
        total_capital = min(premium_pool, hedge_notional)
    else:
        total_capital = premium_pool

    # Shared pool with max exposure cap
    deployed_total = tracker.deployed_usd()
    deployed_this = tracker.deployed_usd(underlying=asset_config.name)
    available_global = max(total_capital - deployed_total, 0.0)
    max_for_asset = max(asset_config.max_exposure * total_capital - deployed_this, 0.0)
    effective_usd = min(max_for_asset, available_global)

    # Apply MAX_AMOUNT ceiling
    max_eth_ceiling = config.MAX_AMOUNT / 10**OTOKEN_DECIMALS
    effective_eth = effective_usd / spot if spot > 0 else 0.0
    effective_eth = min(effective_eth, max_eth_ceiling)
    effective_usd = min(effective_usd, max_eth_ceiling * spot)

    open_pos = tracker.open_positions(underlying=asset_config.name)
    open_notional = sum(p.notional_usd for p in open_pos) if open_pos else 0.0

    hedge_live = config.HEDGE_MODE == "live"
    status = capacity_status(effective_eth, premium_pool, hedge_pool_value, hedge_live)

    return CapacityReport(
        mm_address=mm_address,
        asset=asset_config.name,
        capacity_eth=effective_eth,
        capacity_usd=effective_usd,
        premium_pool_usd=premium_pool,
        hedge_pool_usd=hedge_pool_value,
        hedge_pool_withdrawable_usd=withdrawable,
        leverage=asset_config.leverage,
        open_positions_count=len(open_pos),
        open_positions_notional_usd=open_notional,
        status=status,
        updated_at=int(time.time()),
    )
