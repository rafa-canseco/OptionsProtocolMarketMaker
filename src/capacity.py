"""MM capacity calculation — shared pool with per-asset max exposure."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

import requests

from web3 import Web3

from src import config, hedge_executor

if TYPE_CHECKING:
    from src.config import AssetConfig

log = logging.getLogger(__name__)

USDC_DECIMALS = 6
OTOKEN_DECIMALS = 8
FULL_THRESHOLD_USD = 10.0
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
    capacity_usd: float,
    premium_pool_usd: float,
    hedge_pool_usd: float,
    hedge_live: bool = True,
) -> str:
    if capacity_usd < FULL_THRESHOLD_USD:
        return "full"
    if premium_pool_usd < FULL_THRESHOLD_USD:
        return "full"
    if (
        hedge_live
        and premium_pool_usd > 0
        and hedge_pool_usd < DEGRADED_HEDGE_RATIO * premium_pool_usd
    ):
        return "degraded"
    return "active"


def _read_usdc_balance(
    w3: Web3,
    mm_address: str,
    usdc_address: str | None = None,
) -> float:
    usdc = usdc_address or config.USDC_ADDRESS
    addr_padded = mm_address.lower().replace("0x", "").zfill(64)
    data = _BALANCE_OF_SIG + addr_padded
    raw = w3.eth.call({"to": usdc, "data": data})
    return int.from_bytes(raw, "big") / 10**USDC_DECIMALS


def _read_usdc_allowance(
    w3: Web3,
    mm_address: str,
    usdc_address: str | None = None,
    margin_pool_address: str | None = None,
) -> float:
    usdc = usdc_address or config.USDC_ADDRESS
    pool = margin_pool_address or config.MARGIN_POOL_ADDRESS
    owner = mm_address.lower().replace("0x", "").zfill(64)
    spender = pool.lower().replace("0x", "").zfill(64)
    data = _ALLOWANCE_SIG + owner + spender
    raw = w3.eth.call({"to": usdc, "data": data})
    return int.from_bytes(raw, "big") / 10**USDC_DECIMALS


def _read_pools(
    w3: Web3,
    mm_address: str,
    *,
    chain: str = "base",
) -> tuple[float, float, float]:
    """Read on-chain USDC and hedge pool state.

    Returns:
        (usdc_available, hedge_pool_value_usd, hedge_withdrawable_usd)
    """
    evm_cfg = config.EVM_CONFIGS.get(chain)
    usdc_addr = evm_cfg.usdc_address if evm_cfg else None
    pool_addr = evm_cfg.margin_pool_address if evm_cfg else None

    usdc_balance = _read_usdc_balance(w3, mm_address, usdc_addr)
    usdc_allowance = _read_usdc_allowance(w3, mm_address, usdc_addr, pool_addr)
    usdc_available = min(usdc_balance, usdc_allowance)

    if config.HEDGE_MODE == "live":
        withdrawable = hedge_executor.get_withdrawable()
        hedge_pool_value = hedge_executor.get_account_value()
    else:
        withdrawable = 0.0
        hedge_pool_value = 0.0

    return usdc_available, hedge_pool_value, withdrawable


def _read_solana_usdc_balance(
    rpc_url: str,
    maker_pubkey: str,
    usdc_mint: str,
) -> float:
    """Read USDC SPL token balance for a Solana wallet."""
    resp = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                maker_pubkey,
                {"mint": usdc_mint},
                {"encoding": "jsonParsed"},
            ],
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()

    if "error" in body:
        rpc_err = body["error"]
        raise RuntimeError(
            f"Solana RPC error {rpc_err.get('code')}: {rpc_err.get('message')}"
        )

    accounts = body.get("result", {}).get("value", [])
    if not accounts:
        log.warning(
            "No USDC token account for %s (mint %s)",
            maker_pubkey,
            usdc_mint,
        )
        return 0.0

    info = accounts[0]["account"]["data"]["parsed"]["info"]
    return float(info["tokenAmount"]["uiAmount"] or 0)


def _read_pools_solana(
    rpc_url: str,
    maker_pubkey: str,
    usdc_mint: str,
) -> tuple[float, float, float]:
    """Read Solana USDC balance and shared hedge pool state.

    Returns same shape as _read_pools:
        (usdc_available, hedge_pool_value_usd, hedge_withdrawable_usd)
    """
    usdc_available = _read_solana_usdc_balance(rpc_url, maker_pubkey, usdc_mint)

    if config.HEDGE_MODE == "live":
        withdrawable = hedge_executor.get_withdrawable()
        hedge_pool_value = hedge_executor.get_account_value()
    else:
        withdrawable = 0.0
        hedge_pool_value = 0.0

    return usdc_available, hedge_pool_value, withdrawable


def _live_capacity(
    premium_pool: float,
    withdrawable: float,
    spot: float,
    leverage: int,
    max_exposure: float,
) -> tuple[float, float]:
    """Compute capacity in live mode using premium-ratio conversion.

    In live mode both pools self-track: USDC balance already reflects
    premium paid and Hyperliquid withdrawable already reflects hedge
    margin locked. We convert premium dollars to ETH capacity using the
    premium/collateral ratio from MM-ECONOMICS.md.

    Returns:
        (effective_eth, effective_usd)
    """
    premium_per_eth = config.CAPACITY_PREMIUM_RATIO * spot
    max_eth_premium = premium_pool / premium_per_eth if premium_per_eth > 0 else 0.0

    reserve = config.CAPACITY_RESERVE_RATIO
    usable_hedge = withdrawable * (1.0 - reserve)
    hedge_margin_per_eth = config.CAPACITY_AVG_DELTA * spot / leverage
    max_eth_hedge = (
        usable_hedge / hedge_margin_per_eth if hedge_margin_per_eth > 0 else 0.0
    )

    capacity_eth = min(max_eth_premium, max_eth_hedge)
    effective_eth = capacity_eth * max_exposure
    return effective_eth, effective_eth * spot


def _simulate_capacity(
    usdc_available: float,
    spot: float,
    max_exposure: float,
    tracker,
    asset_name: str,
) -> tuple[float, float, float]:
    """Compute capacity in simulate mode (no self-tracking).

    Returns:
        (premium_pool, effective_eth, effective_usd)
    """
    total_premium = sum(p.premium_paid_usd for p in tracker.open_positions())
    premium_pool = max(usdc_available - total_premium, 0.0)
    total_capital = premium_pool

    deployed_total = tracker.deployed_usd()
    deployed_this = tracker.deployed_usd(underlying=asset_name)
    available_global = max(total_capital - deployed_total, 0.0)
    max_for_asset = max(max_exposure * total_capital - deployed_this, 0.0)
    effective_usd = min(max_for_asset, available_global)
    effective_eth = effective_usd / spot if spot > 0 else 0.0
    return premium_pool, effective_eth, effective_usd


def calculate_capacity_internal(
    w3: Web3 | None,
    spot: float,
    mm_address: str,
    tracker,
    asset_config: AssetConfig | None = None,
    *,
    chain: str = "base",
) -> CapacityReport:
    """Calculate MM capacity for a specific asset.

    Live mode: pools self-track (USDC balance and Hyperliquid
    withdrawable already reflect open positions). Premium dollars
    are converted to ETH capacity using the premium/collateral ratio.

    Simulate mode: pools don't self-track, so deployed notional
    is subtracted manually.
    """
    if asset_config is None:
        asset_config = config.ASSET_MAP.get("eth", config.ASSETS[0])

    if chain == "solana":
        usdc_available, hedge_pool_value, withdrawable = _read_pools_solana(
            config.SOLANA_RPC_URL,
            mm_address,
            config.SOLANA_USDC_MINT,
        )
    else:
        usdc_available, hedge_pool_value, withdrawable = _read_pools(
            w3, mm_address, chain=chain
        )
    leverage = max(asset_config.leverage, 1)

    if config.HEDGE_MODE == "live":
        premium_pool = usdc_available
        effective_eth, effective_usd = _live_capacity(
            premium_pool,
            withdrawable,
            spot,
            leverage,
            asset_config.max_exposure,
        )
    else:
        premium_pool, effective_eth, effective_usd = _simulate_capacity(
            usdc_available,
            spot,
            asset_config.max_exposure,
            tracker,
            asset_config.name,
        )

    # Apply MAX_AMOUNT ceiling
    max_eth_ceiling = config.MAX_AMOUNT / 10**OTOKEN_DECIMALS
    effective_eth = min(effective_eth, max_eth_ceiling)
    effective_usd = min(effective_usd, max_eth_ceiling * spot)

    open_pos = tracker.open_positions(underlying=asset_config.name)
    open_notional = sum(p.notional_usd for p in open_pos) if open_pos else 0.0

    hedge_live = config.HEDGE_MODE == "live"
    status = capacity_status(effective_usd, premium_pool, hedge_pool_value, hedge_live)

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
