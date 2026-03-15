"""MM capacity calculation based on premium pool and hedge pool."""

import logging
import time
from dataclasses import dataclass, fields

from web3 import Web3

from src import config, hedge_executor

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


def calculate_capacity_internal(
    w3: Web3,
    spot: float,
    mm_address: str,
    tracker,
) -> CapacityReport:
    """Calculate MM capacity from both premium and hedge pools."""
    # Premium pool: min(balance, allowance) - committed premium
    usdc_balance = _read_usdc_balance(w3, mm_address)
    usdc_allowance = _read_usdc_allowance(w3, mm_address)
    committed = tracker.total_premium_paid()
    premium_pool = max(min(usdc_balance, usdc_allowance) - committed, 0.0)

    # Hedge pool (skip when not live — no Hyperliquid connection)
    if config.HEDGE_MODE == "live":
        withdrawable = hedge_executor.get_withdrawable()
        hedge_pool_value = hedge_executor.get_account_value()
        reserve = config.CAPACITY_RESERVE_RATIO
        hedge_notional = withdrawable * config.HEDGE_LEVERAGE * (1.0 - reserve)
        effective_usd = min(premium_pool, hedge_notional)
    else:
        withdrawable = 0.0
        hedge_pool_value = 0.0
        effective_usd = premium_pool

    # Apply MAX_AMOUNT ceiling
    max_eth_ceiling = config.MAX_AMOUNT / 10**OTOKEN_DECIMALS
    effective_eth = effective_usd / spot if spot > 0 else 0.0
    effective_eth = min(effective_eth, max_eth_ceiling)
    effective_usd = min(effective_usd, max_eth_ceiling * spot)

    open_pos = tracker.open_positions()
    open_notional = sum(p.notional_usd for p in open_pos) if open_pos else 0.0

    hedge_live = config.HEDGE_MODE == "live"
    status = capacity_status(effective_eth, premium_pool, hedge_pool_value, hedge_live)

    return CapacityReport(
        mm_address=mm_address,
        asset="ETH",
        capacity_eth=effective_eth,
        capacity_usd=effective_usd,
        premium_pool_usd=premium_pool,
        hedge_pool_usd=hedge_pool_value,
        hedge_pool_withdrawable_usd=withdrawable,
        leverage=config.HEDGE_LEVERAGE,
        open_positions_count=len(open_pos),
        open_positions_notional_usd=open_notional,
        status=status,
        updated_at=int(time.time()),
    )
