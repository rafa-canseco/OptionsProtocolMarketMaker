"""Fail-closed Base Sepolia allocator for the B1N-341 CSP smoke policy."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from src import api_client, config
from src.fund_tx import ConfirmedTransaction, send_confirmed_transaction
from src.pricer import bs_delta, validate_iv
from src.snapshot_consumer import SnapshotBundle, SnapshotConsumer, supervise_worker

log = logging.getLogger(__name__)

USDC_SCALE = 10**6
OTOKEN_SCALE = 10**8
COLLATERAL_DENOMINATOR = 10**10
WETH_SCALE = 10**18
BPS = 10_000
UINT256_MAX = 2**256 - 1
FAIR_NAV_INTERFACE_VERSION = 1
FAIR_NAV_POLICY_VERSION = 2
FAIR_NAV_MODEL_VERSION = 1
FAIR_NAV_MAX_DIVERGENCE_BPS = 500
FAIR_NAV_OBSERVATION_QUORUM = 2
FAIR_NAV_HANDOFF_BLOCKS = 2

_POLICY_EXPECTED = {
    "strike_rule": "target_absolute_put_delta",
    "target_put_delta_bps": 900,
    "maximum_delta_deviation_bps": 150,
    "strike_tick_usd": 25,
    "target_duration_hours": 48,
    "reopen_cadence_hours": 48,
    "quote_maximum_age_seconds": 60,
    "target_utilization_bps": 8000,
    "liquid_usdc_reserve_bps": 2000,
    "onchain_minimum_idle_bps": 0,
    "maximum_open_positions": 1,
    "maximum_vault_aum_usdc": None,
    "maximum_collateral_per_position_usdc": None,
    "minimum_net_premium_bps": 20,
    "position_sizing_basis": "current_idle_assets",
    "settlement_maximum_loss_bps": 10000,
    "assigned_inventory_action": "hold_weth_and_continue_on_liquid_usdc",
}

_FUND_QUOTE_TYPES = {
    "Quote": [
        {"name": "owner", "type": "address"},
        {"name": "oToken", "type": "address"},
        {"name": "bidPrice", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "quoteId", "type": "uint256"},
        {"name": "maxAmount", "type": "uint256"},
        {"name": "makerNonce", "type": "uint256"},
    ],
}

_VAULT_ABI = [
    {
        "name": "totalAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "accountedIdleAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "activeNavWindow",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "grossAssets", "type": "uint256"},
                    {"name": "liabilities", "type": "uint256"},
                    {"name": "netAssets", "type": "uint256"},
                    {"name": "liquidAccountingAssets", "type": "uint256"},
                    {"name": "baseExitCost", "type": "uint256"},
                    {"name": "snapshotBlock", "type": "uint64"},
                    {"name": "validAfterBlock", "type": "uint64"},
                    {"name": "validUntilBlock", "type": "uint64"},
                    {"name": "reporterSetVersion", "type": "uint64"},
                    {"name": "reportNonce", "type": "uint64"},
                    {"name": "positionsHash", "type": "bytes32"},
                    {"name": "reportHash", "type": "bytes32"},
                    {"name": "signaturesHash", "type": "bytes32"},
                    {"name": "fundFlowNonce", "type": "uint64"},
                    {"name": "idleStateHash", "type": "bytes32"},
                ],
            }
        ],
    },
]

_FLOW_ABI = [
    {
        "name": "hasActiveProcessing",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "totalPendingShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
]

_SETTLER_ABI = [
    {
        "name": "protocolFeeBps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "treasury",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "hashQuoteFor",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner_", "type": "address"},
            {
                "name": "quote",
                "type": "tuple",
                "components": [
                    {"name": "oToken", "type": "address"},
                    {"name": "bidPrice", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "quoteId", "type": "uint256"},
                    {"name": "maxAmount", "type": "uint256"},
                    {"name": "makerNonce", "type": "uint256"},
                ],
            },
        ],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "name": "getQuoteState",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "mm", "type": "address"},
            {"name": "quoteHash", "type": "bytes32"},
        ],
        "outputs": [
            {"name": "filledAmount", "type": "uint256"},
            {"name": "isCancelled", "type": "bool"},
        ],
    },
]

_STRATEGY_ABI = [
    {
        "name": "positionsHash",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "name": "minimumIdleBps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint16"}],
    },
    {
        "name": "strategyConfig",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "active", "type": "bool"},
                    {"name": "maxAllocationBps", "type": "uint16"},
                    {"name": "maxLossBps", "type": "uint16"},
                    {"name": "cooldown", "type": "uint32"},
                    {"name": "interfaceVersion", "type": "uint64"},
                    {"name": "valuator", "type": "address"},
                    {"name": "absoluteCap", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "allocatedToAdapter",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}, {"type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "allocate",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"type": "address"},
            {"type": "address"},
            {"type": "uint256"},
            {"type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "deallocate",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"type": "address"},
            {"type": "uint256"},
            {"type": "uint256"},
            {"type": "bytes"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]

_ADAPTER_ABI = [
    {
        "name": "accountingAsset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "weth",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "adapterState",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "stateNonce", "type": "uint64"},
                    {"name": "positionsHash", "type": "bytes32"},
                    {"name": "positionCount", "type": "uint256"},
                    {"name": "activePositionCount", "type": "uint256"},
                    {"name": "accountedUsdc", "type": "uint256"},
                    {"name": "accountedWeth", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "adapterConfig",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {
                        "name": "riskConfig",
                        "type": "tuple",
                        "components": [
                            {"name": "minExpiryDelay", "type": "uint64"},
                            {"name": "maxExpiryDelay", "type": "uint64"},
                            {"name": "settlementDefaultDelay", "type": "uint64"},
                            {"name": "minPremiumBps", "type": "uint16"},
                            {"name": "maxSwapSlippageBps", "type": "uint16"},
                            {"name": "maxOpenPositions", "type": "uint16"},
                            {"name": "minStrike", "type": "uint256"},
                            {"name": "maxStrike", "type": "uint256"},
                            {
                                "name": "maxCollateralPerPosition",
                                "type": "uint256",
                            },
                            {"name": "maxWethPerSwap", "type": "uint256"},
                        ],
                    },
                    {"name": "swapRouter", "type": "address"},
                    {"name": "swapFeeTier", "type": "uint24"},
                ],
            }
        ],
    },
    {
        "name": "position",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "uint256"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "oToken", "type": "address"},
                    {"name": "marketMaker", "type": "address"},
                    {"name": "protocolVaultId", "type": "uint256"},
                    {"name": "optionAmount", "type": "uint256"},
                    {"name": "collateral", "type": "uint256"},
                    {"name": "premiumEarned", "type": "uint256"},
                    {"name": "collateralReturned", "type": "uint256"},
                    {"name": "assignedWeth", "type": "uint256"},
                    {"name": "wethBalanceBeforeDelivery", "type": "uint256"},
                    {"name": "openedAt", "type": "uint64"},
                    {"name": "fallbackEligibleAt", "type": "uint64"},
                    {"name": "lifecycle", "type": "uint8"},
                    {"name": "lifecycleHash", "type": "bytes32"},
                ],
            }
        ],
    },
]

_OTOKEN_ABI = [
    {
        "name": "expiry",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "isPut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "underlying",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "strikeAsset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "collateralAsset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "strikePrice",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
]

_VALUATOR_ABI = [
    {
        "name": "interfaceVersion",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [],
        "outputs": [{"type": "uint64"}],
    },
    {
        "name": "valuationPolicyVersion",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [],
        "outputs": [{"type": "uint64"}],
    },
    {
        "name": "requiredModelVersion",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [],
        "outputs": [{"type": "uint64"}],
    },
    {
        "name": "liabilityBufferBps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint16"}],
    },
    {
        "name": "maxObservationDivergenceBps",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [],
        "outputs": [{"type": "uint16"}],
    },
    {
        "name": "observationQuorum",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
    },
]


@dataclass(frozen=True)
class FundPolicy:
    policy_id: str
    target_put_delta_bps: int
    maximum_delta_deviation_bps: int
    strike_tick_usd: int
    target_duration_seconds: int
    quote_maximum_age: int
    target_utilization_bps: int
    liquid_usdc_reserve_bps: int
    onchain_minimum_idle_bps: int
    maximum_vault_aum: int
    maximum_collateral: int
    maximum_open_positions: int
    minimum_net_premium_bps: int
    settlement_maximum_loss_bps: int
    min_expiry_delay: int = 36 * 3600
    max_expiry_delay: int = 60 * 3600


@dataclass(frozen=True)
class CspQuoteEvaluation:
    quote: dict[str, Any]
    rejection_reason: str | None
    realized_delta_bps: int | None = None
    delta_deviation_bps: int | None = None
    strike_distance_bps: int | None = None
    gross_premium: int | None = None
    net_premium: int | None = None
    gross_premium_bps: int | None = None
    net_premium_bps: int | None = None
    option_amount: int | None = None
    collateral: int | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection_reason is None


def load_testnet_policy(path: str | Path) -> FundPolicy:
    policy_path = Path(path)
    try:
        raw = json.loads(policy_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Unable to load allocator policy {policy_path}: {error}"
        ) from error
    expected_scope = {
        "chain_id": 84532,
        "collateral": "USDC",
        "environment": "base_sepolia",
        "strategy": "cash_secured_put",
        "underlying": "ETH",
    }
    if (
        raw.get("schema_version") != 4
        or raw.get("policy_id") != "eth_usdc_csp_delta_009_base_sepolia_v4"
        or raw.get("decision") != "go_testnet_only"
        or raw.get("activation_allowed") is not True
        or raw.get("authority_issue") != "B1N-438"
        or raw.get("scope") != expected_scope
        or raw.get("selection") != _POLICY_EXPECTED
    ):
        raise ValueError(
            f"Allocator policy {policy_path} is not the approved B1N-438 test policy"
        )
    selection = raw["selection"]
    return FundPolicy(
        policy_id=str(raw["policy_id"]),
        target_put_delta_bps=selection["target_put_delta_bps"],
        maximum_delta_deviation_bps=selection["maximum_delta_deviation_bps"],
        strike_tick_usd=selection["strike_tick_usd"],
        target_duration_seconds=selection["target_duration_hours"] * 3600,
        quote_maximum_age=selection["quote_maximum_age_seconds"],
        target_utilization_bps=selection["target_utilization_bps"],
        liquid_usdc_reserve_bps=selection["liquid_usdc_reserve_bps"],
        onchain_minimum_idle_bps=selection["onchain_minimum_idle_bps"],
        maximum_vault_aum=UINT256_MAX,
        maximum_collateral=UINT256_MAX,
        maximum_open_positions=selection["maximum_open_positions"],
        minimum_net_premium_bps=selection["minimum_net_premium_bps"],
        settlement_maximum_loss_bps=selection["settlement_maximum_loss_bps"],
    )


def required_collateral(option_amount: int, strike_raw: int) -> int:
    numerator = option_amount * strike_raw
    return (numerator + COLLATERAL_DENOMINATOR - 1) // COLLATERAL_DENOMINATOR


def liquid_collateral_target(idle_assets: int, policy: FundPolicy) -> int:
    """Apply utilization to the current liquid USDC, independently of assigned WETH."""
    return min(
        idle_assets * policy.target_utilization_bps // BPS,
        idle_assets * (BPS - policy.liquid_usdc_reserve_bps) // BPS,
    )


def validate_allocated_exposure(allocated: int, policy: FundPolicy) -> None:
    """Reject impossible uint256 state without imposing a static economic cap."""
    if allocated < 0 or allocated > UINT256_MAX:
        raise RuntimeError("Fund allocation is outside uint256 bounds")


def safe_block_has_coherent_nav(nav: tuple[Any, ...], block: int) -> bool:
    """Accept the bounded verifier handoff while the confirmed head is already active."""
    return nav[6] <= block + FAIR_NAV_HANDOFF_BLOCKS and block <= nav[7]


def option_amount_for_collateral(collateral: int, strike_raw: int) -> int:
    return collateral * COLLATERAL_DENOMINATOR // strike_raw


def validate_fair_nav_policy(
    policy_state: tuple[int, int, int, int, int, int],
) -> None:
    expected = (
        FAIR_NAV_INTERFACE_VERSION,
        FAIR_NAV_POLICY_VERSION,
        FAIR_NAV_MODEL_VERSION,
        0,
        FAIR_NAV_MAX_DIVERGENCE_BPS,
        FAIR_NAV_OBSERVATION_QUORUM,
    )
    if policy_state != expected:
        raise RuntimeError(
            "Configured CSP valuator is not the approved fair-NAV policy"
        )


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean timestamp")
    if isinstance(value, (int, float)):
        timestamp = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        timestamp = (
            int(stripped)
            if stripped.isdecimal()
            else int(
                datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                .astimezone(UTC)
                .timestamp()
            )
        )
    else:
        raise ValueError("missing timestamp")
    if timestamp <= 0:
        raise ValueError("invalid timestamp")
    return timestamp


def validate_market_snapshot(
    market: dict[str, Any], *, now: int, maximum_age: int
) -> tuple[float, float]:
    """Validate the causal spot/IV snapshot used for allocator delta selection."""
    try:
        spot = float(market["spot"])
        iv = float(market["iv"])
        observed_at = _timestamp(market.get("observed_at"))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("Market spot/IV snapshot is missing or invalid") from error
    if (
        not math.isfinite(spot)
        or not math.isfinite(iv)
        or spot <= 0
        or not validate_iv(iv, "ETH CSP allocator")
    ):
        raise RuntimeError("Market spot/IV snapshot is outside approved bounds")
    if observed_at > now + 5 or now - observed_at > maximum_age:
        raise RuntimeError("Market spot/IV snapshot is stale")
    return spot, iv


def premium_after_protocol_fee(gross_premium: int, protocol_fee_bps: int) -> int:
    """Mirror BatchSettler integer fee rounding: gross minus floor(gross * fee/BPS)."""
    if gross_premium < 0:
        raise ValueError("Gross premium cannot be negative")
    if not 0 <= protocol_fee_bps < BPS:
        raise ValueError("Protocol fee must leave a positive net premium")
    return gross_premium - gross_premium * protocol_fee_bps // BPS


def incremental_quote_premium(
    *, filled_amount: int, option_amount: int, bid_price: int, protocol_fee_bps: int
) -> tuple[int, int]:
    """Mirror CspBatchSettler cumulative premium and fee-delta rounding."""
    if filled_amount < 0 or option_amount < 0 or bid_price < 0:
        raise ValueError("Quote amounts and bid price cannot be negative")
    previous_gross = filled_amount * bid_price // OTOKEN_SCALE
    cumulative_gross = (filled_amount + option_amount) * bid_price // OTOKEN_SCALE
    gross_premium = cumulative_gross - previous_gross
    previous_fee = previous_gross * protocol_fee_bps // BPS
    cumulative_fee = cumulative_gross * protocol_fee_bps // BPS
    return gross_premium, gross_premium - (cumulative_fee - previous_fee)


def premium_meets_floor(
    *,
    gross_premium: int,
    collateral: int,
    protocol_fee_bps: int,
    minimum_net_premium_bps: int,
) -> bool:
    """Reproduce the adapter's exact cross-multiplied net-premium floor."""
    if collateral <= 0 or not 0 <= minimum_net_premium_bps <= BPS:
        return False
    net_premium = premium_after_protocol_fee(gross_premium, protocol_fee_bps)
    return net_premium * BPS >= collateral * minimum_net_premium_bps


def _base_quote_rejection(
    quote: dict[str, Any],
    *,
    now: int,
    policy: FundPolicy,
    deployment_statuses: frozenset[str],
) -> str | None:
    try:
        if quote.get("asset") != "eth" or quote.get("chain", "base") != "base":
            return "wrong_market"
        if quote.get("is_put") is not True:
            return "wrong_option_type"
        status = str(quote.get("deployment_status") or "ready").lower()
        if status not in deployment_statuses:
            return "deployment_status"
        created_at = _timestamp(quote.get("created_at"))
        if created_at > now + 5 or now - created_at > policy.quote_maximum_age:
            return "stale_quote"
        if int(quote.get("deadline") or 0) <= now + 15:
            return "insufficient_ttl"
        delay = int(quote.get("expiry") or 0) - now
        if not policy.min_expiry_delay <= delay <= policy.max_expiry_delay:
            return "expiry_outside_48h_window"
        strike = Decimal(str(quote.get("strike_price")))
        if strike <= 0 or strike % Decimal(policy.strike_tick_usd) != 0:
            return "invalid_strike_tick"
        if int(quote.get("bid_price") or 0) <= 0:
            return "invalid_premium"
        if int(quote.get("max_amount") or 0) <= 0:
            return "no_quote_capacity"
    except (ArithmeticError, TypeError, ValueError):
        return "invalid_quote"
    return None


def evaluate_csp_quote(
    quote: dict[str, Any],
    *,
    spot: float,
    iv: float,
    now: int,
    policy: FundPolicy,
    protocol_fee_bps: int,
    collateral_target: int,
    series_validator: Callable[[dict[str, Any]], bool] | None = None,
    deployment_statuses: frozenset[str] = frozenset({"ready"}),
) -> CspQuoteEvaluation:
    reason = _base_quote_rejection(
        quote,
        now=now,
        policy=policy,
        deployment_statuses=deployment_statuses,
    )
    if reason is not None:
        return CspQuoteEvaluation(quote, reason)
    try:
        strike = Decimal(str(quote["strike_price"]))
        expiry = int(quote["expiry"])
        delta = abs(
            bs_delta(
                True,
                spot,
                float(strike),
                (expiry - now) / (365 * 86_400),
                config.RISK_FREE_RATE,
                iv,
            )
        )
        if not 0 < delta < 1:
            return CspQuoteEvaluation(quote, "invalid_delta")
        realized_delta_bps = round(delta * BPS)
        exact_deviation_bps = abs(delta - policy.target_put_delta_bps / BPS) * BPS
        deviation_bps = round(exact_deviation_bps)
        strike_distance_bps = round((spot - float(strike)) * BPS / spot)
        if exact_deviation_bps > policy.maximum_delta_deviation_bps:
            return CspQuoteEvaluation(
                quote,
                "delta_outside_tolerance",
                realized_delta_bps,
                deviation_bps,
                strike_distance_bps,
            )
        strike_raw = int(strike * OTOKEN_SCALE)
        option_amount = min(
            option_amount_for_collateral(collateral_target, strike_raw),
            int(quote.get("_remaining_amount", quote["max_amount"])),
        )
        collateral = required_collateral(option_amount, strike_raw)
        if option_amount <= 0 or collateral <= 0 or collateral > collateral_target:
            return CspQuoteEvaluation(quote, "no_quote_capacity")
        gross_premium, net_premium = incremental_quote_premium(
            filled_amount=int(quote.get("_filled_amount", 0)),
            option_amount=option_amount,
            bid_price=int(quote["bid_price"]),
            protocol_fee_bps=protocol_fee_bps,
        )
        gross_bps = gross_premium * BPS // collateral
        net_bps = net_premium * BPS // collateral
        if net_premium * BPS < collateral * policy.minimum_net_premium_bps:
            return CspQuoteEvaluation(
                quote,
                "net_premium_below_floor",
                realized_delta_bps,
                deviation_bps,
                strike_distance_bps,
                gross_premium,
                net_premium,
                gross_bps,
                net_bps,
                option_amount,
                collateral,
            )
        status = str(quote.get("deployment_status") or "ready").lower()
        if (
            status == "ready"
            and series_validator is not None
            and not series_validator(quote)
        ):
            return CspQuoteEvaluation(quote, "incompatible_series")
        return CspQuoteEvaluation(
            quote,
            None,
            realized_delta_bps,
            deviation_bps,
            strike_distance_bps,
            gross_premium,
            net_premium,
            gross_bps,
            net_bps,
            option_amount,
            collateral,
        )
    except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError):
        return CspQuoteEvaluation(quote, "invalid_delta_or_premium")


def select_policy_quote(
    quotes: list[dict[str, Any]],
    *,
    spot: float,
    iv: float,
    now: int,
    policy: FundPolicy,
    protocol_fee_bps: int,
    collateral_target: int,
    series_validator: Callable[[dict[str, Any]], bool] | None = None,
    deployment_statuses: frozenset[str] = frozenset({"ready"}),
) -> dict[str, Any] | None:
    evaluations = [
        evaluate_csp_quote(
            quote,
            spot=spot,
            iv=iv,
            now=now,
            policy=policy,
            protocol_fee_bps=protocol_fee_bps,
            collateral_target=collateral_target,
            series_validator=series_validator,
            deployment_statuses=deployment_statuses,
        )
        for quote in quotes
    ]
    for evaluation in evaluations:
        if not evaluation.accepted:
            log.info(
                "CSP allocator decision=reject policy=%s quote_id=%s reason=%s "
                "target_delta_bps=%d realized_delta_bps=%s deviation_bps=%s "
                "strike_distance_bps=%s gross_premium_bps=%s net_premium_bps=%s "
                "protocol_fee_bps=%d",
                policy.policy_id,
                evaluation.quote.get("quote_id"),
                evaluation.rejection_reason,
                policy.target_put_delta_bps,
                evaluation.realized_delta_bps,
                evaluation.delta_deviation_bps,
                evaluation.strike_distance_bps,
                evaluation.gross_premium_bps,
                evaluation.net_premium_bps,
                protocol_fee_bps,
            )
    candidates = [evaluation for evaluation in evaluations if evaluation.accepted]
    if not candidates:
        return None
    selected = min(
        candidates,
        key=lambda evaluation: (
            evaluation.delta_deviation_bps or 0,
            abs(int(evaluation.quote["expiry"]) - now - policy.target_duration_seconds),
            evaluation.realized_delta_bps or 0,
            Decimal(str(evaluation.quote["strike_price"])),
            -int(evaluation.quote["deadline"]),
            int(evaluation.quote.get("quote_id") or 0),
        ),
    )
    return selected.quote


def sign_fund_quote(
    quote: dict[str, Any],
    *,
    owner: str,
    chain_id: int,
    settler: str,
    private_key: str,
) -> bytes:
    """Sign the owner-bound quote required by the deployed CSP settler."""
    signable = encode_typed_data(
        domain_data={
            "name": "b1nary",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(settler),
        },
        message_types=_FUND_QUOTE_TYPES,
        message_data={
            "owner": Web3.to_checksum_address(owner),
            "oToken": Web3.to_checksum_address(quote["otoken_address"]),
            "bidPrice": int(quote["bid_price"]),
            "deadline": int(quote["deadline"]),
            "quoteId": int(quote["quote_id"]),
            "maxAmount": int(quote["max_amount"]),
            "makerNonce": int(quote["maker_nonce"]),
        },
    )
    return bytes(Account.sign_message(signable, private_key=private_key).signature)


class CspFundAllocator:
    def __init__(self, snapshots: SnapshotConsumer, transaction_w3: Web3) -> None:
        self.policy = load_testnet_policy(config.FUND_ALLOCATOR_POLICY_PATH)
        self._validate_runtime_config()
        self.snapshots = snapshots
        self.w3 = transaction_w3
        self.account = Account.from_key(config.FUND_ALLOCATOR_PRIVATE_KEY)
        self.vault = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.FUND_VAULT_ADDRESS),
            abi=_VAULT_ABI,
        )
        self.flow = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.FUND_FLOW_MANAGER_ADDRESS),
            abi=_FLOW_ABI,
        )
        self.strategy = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.FUND_STRATEGY_MANAGER_ADDRESS),
            abi=_STRATEGY_ABI,
        )
        self.adapter_address = Web3.to_checksum_address(config.FUND_CSP_ADAPTER_ADDRESS)
        self.adapter = self.w3.eth.contract(
            address=self.adapter_address,
            abi=_ADAPTER_ABI,
        )
        self.settler = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.BATCH_SETTLER),
            abi=_SETTLER_ABI,
        )
        self.usdc = Web3.to_checksum_address(config.USDC_ADDRESS)
        self.weth = ""
        self.valuator_address = Web3.to_checksum_address(
            config.FUND_CSP_VALUATOR_ADDRESS
        )
        self.valuator = self.w3.eth.contract(
            address=self.valuator_address,
            abi=_VALUATOR_ABI,
        )

    @staticmethod
    def _validate_runtime_config() -> None:
        required = {
            "FUND_ALLOCATOR_PRIVATE_KEY": config.FUND_ALLOCATOR_PRIVATE_KEY,
            "FUND_VAULT_ADDRESS": config.FUND_VAULT_ADDRESS,
            "FUND_FLOW_MANAGER_ADDRESS": config.FUND_FLOW_MANAGER_ADDRESS,
            "FUND_STRATEGY_MANAGER_ADDRESS": config.FUND_STRATEGY_MANAGER_ADDRESS,
            "FUND_CSP_ADAPTER_ADDRESS": config.FUND_CSP_ADAPTER_ADDRESS,
            "FUND_CSP_VALUATOR_ADDRESS": config.FUND_CSP_VALUATOR_ADDRESS,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise RuntimeError(f"Missing CSP allocator configuration: {missing}")
        if config.CHAIN_ID != 84532:
            raise RuntimeError("CSP allocator requires CHAIN_ID=84532")
        environment = config._current_environment()
        if environment and environment not in {"staging", "development", "test"}:
            raise RuntimeError(
                "CSP allocator may only run in a non-production environment"
            )

    def _is_compatible_put_series(
        self, quote: dict[str, Any], state: dict[str, Any]
    ) -> bool:
        series = state.get("series", {}).get(str(quote["otoken_address"]).lower())
        if not isinstance(series, Mapping):
            return False
        return (
            series.get("is_put") is True
            and Web3.to_checksum_address(series["underlying"]) == self.weth
            and Web3.to_checksum_address(series["strike_asset"]) == self.usdc
            and Web3.to_checksum_address(series["collateral_asset"]) == self.usdc
            and int(series["expiry"]) == int(quote["expiry"])
            and int(series["strike_price"])
            == int(Decimal(str(quote["strike_price"])) * OTOKEN_SCALE)
        )

    def _quote_fill_state(
        self, quote: dict[str, Any], *, owner: str, signer: str, block: int
    ) -> tuple[int, int]:
        del owner, signer, block
        quote_state = quote.get("_snapshot_quote_state")
        if not isinstance(quote_state, Mapping):
            raise RuntimeError("Atomic CSP quote state is unavailable")
        filled = quote_state["filled_amount"]
        cancelled = quote_state["cancelled"]
        filled_amount = int(filled)
        remaining = 0 if cancelled else max(int(quote["max_amount"]) - filled_amount, 0)
        return filled_amount, remaining

    def _validate_policy_gates(self, state: dict[str, Any]) -> None:
        nav = state["nav"]
        strategy_config = state["strategy_config"]
        risk = state["adapter_config"][0]
        block = state["block"]
        if (
            not safe_block_has_coherent_nav(nav, block)
            or nav[10] != state["strategy_hash"]
        ):
            raise RuntimeError("No coherent active NAV window at the safe block")
        if state["processing"]:
            raise RuntimeError("Fund flow processing is active")
        validate_fair_nav_policy(state["valuation_policy"])
        # Synchronous deposits and strategy P&L can change fund NAV. New rounds
        # size from current idle USDC; there is no static economic cap.
        validate_allocated_exposure(state["allocated"], self.policy)
        expected_strategy = (
            True,
            self.policy.target_utilization_bps,
            self.policy.settlement_maximum_loss_bps,
            0,
            1,
            self.valuator_address,
            self.policy.maximum_collateral,
        )
        normalized_strategy = (
            *strategy_config[:5],
            strategy_config[5],
            strategy_config[6],
        )
        if normalized_strategy != expected_strategy:
            raise RuntimeError("On-chain StrategyManager config differs from policy")
        expected_risk = (
            self.policy.min_expiry_delay,
            self.policy.max_expiry_delay,
            1,
            self.policy.minimum_net_premium_bps,
            500,
            self.policy.maximum_open_positions,
            1000 * OTOKEN_SCALE,
            5000 * OTOKEN_SCALE,
            self.policy.maximum_collateral,
            WETH_SCALE,
        )
        if tuple(risk) != expected_risk:
            raise RuntimeError("On-chain CSP adapter config differs from policy")
        if state["minimum_idle_bps"] != self.policy.onchain_minimum_idle_bps:
            raise RuntimeError("On-chain minimum idle requirement differs from policy")

    def _send(self, function: Any, bundle: SnapshotBundle) -> ConfirmedTransaction:
        self.snapshots.require(bundle)
        tx = send_confirmed_transaction(
            w3=self.w3,
            account=self.account,
            function=function,
            chain_id=84532,
            confirmations=config.FUND_ALLOCATOR_CONFIRMATIONS,
            decision_validator=lambda: self.snapshots.require(bundle),
        )
        self.snapshots.wait_after_receipt(
            pre_send_generation=bundle.generation,
            receipt_block=tx.block_number,
            receipt_block_hash=tx.block_hash,
        )
        return tx

    def _settle(self, state: dict[str, Any], bundle: SnapshotBundle) -> bool:
        adapter_state = state["adapter_state"]
        if adapter_state[3] == 0:
            return False
        position_id = adapter_state[2]
        position = state["position"]
        lifecycle = position[11]
        if lifecycle == 1 and bundle.snapshot_block_timestamp < int(
            state["position_expiry"]
        ):
            return True
        if lifecycle not in {1, 2}:
            raise RuntimeError(f"Unexpected active CSP lifecycle {lifecycle}")
        target_value = state["allocated"] if state["allocated"] else 1
        data = self.w3.codec.encode(
            ["(uint8,uint256,uint256,uint256)"],
            [(1, position_id, 0, 0)],
        )
        tx = self._send(
            self.strategy.functions.deallocate(
                self.adapter_address,
                target_value,
                0,
                data,
            ),
            bundle,
        )
        log.info(
            "CSP allocator decision=%s position_id=%d tx=%s",
            "settle" if lifecycle == 1 else "complete_assignment",
            position_id,
            tx.tx_hash,
        )
        return True

    def _open(
        self, state: dict[str, Any], bundle: SnapshotBundle | None = None
    ) -> None:
        adapter_state = state["adapter_state"]
        if adapter_state[3] != 0 or state["allocated"] != 0:
            return
        if state["pending_shares"] != 0:
            log.info(
                "CSP allocator decision=skip reason=pending_redemptions shares=%d",
                state["pending_shares"],
            )
            return
        idle_assets = state["idle_assets"]
        target = liquid_collateral_target(idle_assets, self.policy)
        if target <= 0:
            log.info("CSP allocator decision=skip reason=no_liquid_usdc")
            return
        if bundle is None:
            raise RuntimeError("Atomic CSP market/quote snapshot is unavailable")
        market = dict(bundle.market("eth"))
        now = int(time.time())
        configured_protocol_fee_bps = int(state["protocol_fee_bps"])
        treasury = Web3.to_checksum_address(state["treasury"])
        protocol_fee_bps = 0 if int(treasury, 16) == 0 else configured_protocol_fee_bps
        try:
            api_client.require_protocol_fee_match(market, configured_protocol_fee_bps)
            premium_after_protocol_fee(0, protocol_fee_bps)
            spot, iv = validate_market_snapshot(
                market,
                now=now,
                maximum_age=self.policy.quote_maximum_age,
            )
        except (RuntimeError, ValueError) as error:
            log.info(
                "CSP allocator decision=reject policy=%s reason=invalid_market_or_fee "
                "protocol_fee_bps=%s detail=%s",
                self.policy.policy_id,
                protocol_fee_bps,
                error,
            )
            raise
        signer = Account.from_key(config.MM_PRIVATE_KEY).address
        quotes: list[dict[str, Any]] = []
        quote_states = state.get("quote_states", {})
        for raw_quote in bundle.quotes():
            bounded_quote = dict(raw_quote)
            quote_state_key = (
                f"{raw_quote.get('maker_nonce')}:{raw_quote.get('quote_id')}:"
                f"{str(raw_quote.get('otoken_address')).lower()}"
            )
            bounded_quote["_snapshot_quote_state"] = quote_states.get(
                quote_state_key, quote_states.get(str(raw_quote.get("quote_id")))
            )
            filled_amount, remaining_amount = self._quote_fill_state(
                bounded_quote,
                owner=self.adapter_address,
                signer=signer,
                block=state["block"],
            )
            bounded_quote["_filled_amount"] = filled_amount
            bounded_quote["_remaining_amount"] = remaining_amount
            quotes.append(bounded_quote)

        def series_validator(candidate: dict[str, Any]) -> bool:
            return self._is_compatible_put_series(candidate, state)

        quote = select_policy_quote(
            quotes,
            spot=spot,
            iv=iv,
            now=now,
            policy=self.policy,
            protocol_fee_bps=protocol_fee_bps,
            collateral_target=target,
            series_validator=series_validator,
        )
        if quote is None:
            quote = select_policy_quote(
                quotes,
                spot=spot,
                iv=iv,
                now=now,
                policy=self.policy,
                protocol_fee_bps=protocol_fee_bps,
                collateral_target=target,
                series_validator=series_validator,
                deployment_statuses=frozenset({"virtual", "creating"}),
            )
            if quote is None:
                log.info(
                    "CSP allocator decision=skip policy=%s reason=no_eligible_delta_quote "
                    "target_delta_bps=%d maximum_deviation_bps=%d "
                    "protocol_fee_bps=%d",
                    self.policy.policy_id,
                    self.policy.target_put_delta_bps,
                    self.policy.maximum_delta_deviation_bps,
                    protocol_fee_bps,
                )
                raise RuntimeError(
                    "No live signed quote matches the 0.09-delta 48h CSP policy"
                )
        evaluation = evaluate_csp_quote(
            quote,
            spot=spot,
            iv=iv,
            now=now,
            policy=self.policy,
            protocol_fee_bps=protocol_fee_bps,
            collateral_target=target,
            series_validator=(
                series_validator
                if str(quote.get("deployment_status") or "ready").lower() == "ready"
                else None
            ),
            deployment_statuses=frozenset(
                {str(quote.get("deployment_status") or "ready").lower()}
            ),
        )
        if not evaluation.accepted:
            raise RuntimeError(
                f"Selected CSP quote failed revalidation: {evaluation.rejection_reason}"
            )
        option_amount = int(evaluation.option_amount or 0)
        collateral = int(evaluation.collateral or 0)
        if option_amount <= 0 or collateral <= 0 or collateral > target:
            raise RuntimeError(
                "Matching quote cannot fill the bounded collateral target"
            )
        if str(quote.get("deployment_status") or "ready").lower() != "ready":
            self.snapshots.require(bundle)
            result = api_client.ensure_fund_series(
                adapter_address=self.adapter_address,
                quote={
                    key: value
                    for key, value in quote.items()
                    if not key.startswith("_")
                },
                amount_raw=option_amount,
            )
            log.info(
                "CSP allocator decision=materialize_series policy=%s status=%s "
                "otoken=%s tx=%s target_delta_bps=%d realized_delta_bps=%d "
                "deviation_bps=%d strike_distance_bps=%d gross_premium_bps=%d "
                "net_premium_bps=%d protocol_fee_bps=%d rejection_reason=none",
                self.policy.policy_id,
                result["status"],
                result["otoken_address"],
                result.get("deployment_tx_hash"),
                self.policy.target_put_delta_bps,
                evaluation.realized_delta_bps,
                evaluation.delta_deviation_bps,
                evaluation.strike_distance_bps,
                evaluation.gross_premium_bps,
                evaluation.net_premium_bps,
                protocol_fee_bps,
            )
            return
        signature = sign_fund_quote(
            quote,
            owner=self.adapter_address,
            chain_id=84532,
            settler=config.BATCH_SETTLER,
            private_key=config.MM_PRIVATE_KEY,
        )
        open_data = self.w3.codec.encode(
            [
                "((address,uint256,uint256,uint256,uint256,uint256),bytes,uint256,uint256)"
            ],
            [
                (
                    (
                        Web3.to_checksum_address(quote["otoken_address"]),
                        int(quote["bid_price"]),
                        int(quote["deadline"]),
                        int(quote["quote_id"]),
                        int(quote["max_amount"]),
                        int(quote["maker_nonce"]),
                    ),
                    signature,
                    option_amount,
                    collateral,
                )
            ],
        )
        tx = self._send(
            self.strategy.functions.allocate(
                self.adapter_address,
                self.usdc,
                collateral,
                open_data,
            ),
            bundle,
        )
        log.info(
            "CSP allocator decision=open policy=%s strike=%s collateral_usdc=%.6f "
            "option_amount=%.8f target_delta_bps=%d realized_delta_bps=%d "
            "deviation_bps=%d strike_distance_bps=%d gross_premium_bps=%d "
            "net_premium_bps=%d protocol_fee_bps=%d rejection_reason=none tx=%s",
            self.policy.policy_id,
            quote["strike_price"],
            collateral / USDC_SCALE,
            option_amount / OTOKEN_SCALE,
            self.policy.target_put_delta_bps,
            evaluation.realized_delta_bps,
            evaluation.delta_deviation_bps,
            evaluation.strike_distance_bps,
            evaluation.gross_premium_bps,
            evaluation.net_premium_bps,
            protocol_fee_bps,
            tx.tx_hash,
        )

    def run_once(self) -> None:
        bundle = self.snapshots.current()
        state = dict(
            bundle.fund(
                "csp",
                "allocator",
                expected_address=config.FUND_VAULT_ADDRESS,
            )
        )
        state["block"] = bundle.snapshot_block
        self.weth = Web3.to_checksum_address(state["weth"])
        self._validate_policy_gates(state)
        if self._settle(state, bundle):
            return
        self._open(state, bundle)

    def run_forever(self) -> None:
        log.info(
            "CSP fund allocator enabled: address=%s policy=%s interval=%ds",
            self.account.address,
            self.policy.policy_id,
            config.FUND_ALLOCATOR_INTERVAL_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning("CSP allocator cycle failed closed", exc_info=True)
            time.sleep(config.FUND_ALLOCATOR_INTERVAL_SECONDS)


def start(snapshots: SnapshotConsumer, transaction_w3: Web3) -> threading.Thread | None:
    if not config.V2_SNAPSHOT_ENABLED or not config.FUND_ALLOCATOR_ENABLED:
        log.info("CSP fund allocator disabled")
        return None
    thread = threading.Thread(
        target=supervise_worker,
        args=(
            "csp-fund-allocator",
            lambda: CspFundAllocator(snapshots, transaction_w3),
        ),
        name="csp-fund-allocator",
        daemon=True,
    )
    thread.start()
    return thread
