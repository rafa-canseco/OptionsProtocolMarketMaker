"""Fail-closed Base Sepolia allocator for the B1N-341 CSP smoke policy."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from src import api_client, config

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
    "strike_otm_bps": 1500,
    "strike_tick_usd": 25,
    "target_duration_hours": 48,
    "reopen_cadence_hours": 48,
    "target_utilization_bps": 8000,
    "liquid_usdc_reserve_bps": 2000,
    "onchain_minimum_idle_bps": 0,
    "maximum_open_positions": 1,
    "maximum_vault_aum_usdc": None,
    "maximum_collateral_per_position_usdc": None,
    "minimum_net_premium_bps": 0,
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
    strike_otm_bps: int
    strike_tick_usd: int
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
        raw.get("schema_version") != 3
        or raw.get("decision") != "go_testnet_only"
        or raw.get("activation_allowed") is not True
        or raw.get("authority_issue") != "B1N-374"
        or raw.get("scope") != expected_scope
        or raw.get("selection") != _POLICY_EXPECTED
    ):
        raise ValueError(
            f"Allocator policy {policy_path} is not the approved B1N-374 test policy"
        )
    selection = raw["selection"]
    return FundPolicy(
        strike_otm_bps=selection["strike_otm_bps"],
        strike_tick_usd=selection["strike_tick_usd"],
        target_utilization_bps=selection["target_utilization_bps"],
        liquid_usdc_reserve_bps=selection["liquid_usdc_reserve_bps"],
        onchain_minimum_idle_bps=selection["onchain_minimum_idle_bps"],
        maximum_vault_aum=UINT256_MAX,
        maximum_collateral=UINT256_MAX,
        maximum_open_positions=selection["maximum_open_positions"],
        minimum_net_premium_bps=selection["minimum_net_premium_bps"],
        settlement_maximum_loss_bps=selection["settlement_maximum_loss_bps"],
    )


def policy_strike(spot: float, policy: FundPolicy) -> int:
    discounted = (
        Decimal(str(spot)) * Decimal(BPS - policy.strike_otm_bps) / Decimal(BPS)
    )
    ticks = (discounted / Decimal(policy.strike_tick_usd)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return int(ticks * policy.strike_tick_usd)


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


def select_policy_quote(
    quotes: list[dict[str, Any]],
    *,
    spot: float,
    now: int,
    policy: FundPolicy,
    series_validator: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    target_strike = policy_strike(spot, policy)
    candidates = [
        quote
        for quote in quotes
        if quote.get("asset") == "eth"
        and quote.get("is_put") is True
        and int(quote.get("deadline") or 0) > now + 15
        and policy.min_expiry_delay
        <= int(quote.get("expiry") or 0) - now
        <= policy.max_expiry_delay
        and Decimal(str(quote.get("strike_price"))) == Decimal(target_strike)
        and (series_validator is None or series_validator(quote))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda quote: int(quote["deadline"]))


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
    def __init__(self) -> None:
        self.policy = load_testnet_policy(config.FUND_ALLOCATOR_POLICY_PATH)
        self._validate_runtime_config()
        self.w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
        if self.w3.eth.chain_id != 84532:
            raise RuntimeError("CSP allocator is locked to Base Sepolia chain 84532")
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
        self.usdc = Web3.to_checksum_address(
            self.adapter.functions.accountingAsset().call()
        )
        self.weth = Web3.to_checksum_address(self.adapter.functions.weth().call())
        if self.usdc != Web3.to_checksum_address(config.USDC_ADDRESS):
            raise RuntimeError(
                "Configured CSP USDC differs from adapter accounting asset"
            )
        self.valuator_address = Web3.to_checksum_address(
            config.FUND_CSP_VALUATOR_ADDRESS
        )
        self.valuator = self.w3.eth.contract(
            address=self.valuator_address,
            abi=_VALUATOR_ABI,
        )
        for address in (
            self.vault.address,
            self.flow.address,
            self.strategy.address,
            self.adapter.address,
            self.usdc,
            self.weth,
            self.valuator.address,
        ):
            if not self.w3.eth.get_code(address):
                raise RuntimeError(f"Configured fund address has no code: {address}")

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

    def _safe_block(self) -> int:
        latest = self.w3.eth.block_number
        return max(latest - config.FUND_ALLOCATOR_CONFIRMATIONS, 0)

    def _is_compatible_put_series(self, quote: dict[str, Any]) -> bool:
        o_token = self.w3.eth.contract(
            address=Web3.to_checksum_address(quote["otoken_address"]),
            abi=_OTOKEN_ABI,
        )
        return (
            o_token.functions.isPut().call() is True
            and Web3.to_checksum_address(o_token.functions.underlying().call())
            == self.weth
            and Web3.to_checksum_address(o_token.functions.strikeAsset().call())
            == self.usdc
            and Web3.to_checksum_address(o_token.functions.collateralAsset().call())
            == self.usdc
            and int(o_token.functions.expiry().call()) == int(quote["expiry"])
            and int(o_token.functions.strikePrice().call())
            == int(Decimal(str(quote["strike_price"])) * OTOKEN_SCALE)
        )

    def _read_gate_state(self, block: int) -> dict[str, Any]:
        nav = self.vault.functions.activeNavWindow().call(block_identifier=block)
        strategy_hash = self.strategy.functions.positionsHash().call(
            block_identifier=block
        )
        strategy_config = self.strategy.functions.strategyConfig(
            self.adapter_address
        ).call(block_identifier=block)
        adapter_config = self.adapter.functions.adapterConfig().call(
            block_identifier=block
        )
        adapter_state = self.adapter.functions.adapterState().call(
            block_identifier=block
        )
        valuation_policy = (
            self.valuator.functions.interfaceVersion().call(block_identifier=block),
            self.valuator.functions.valuationPolicyVersion().call(
                block_identifier=block
            ),
            self.valuator.functions.requiredModelVersion().call(block_identifier=block),
            self.valuator.functions.liabilityBufferBps().call(block_identifier=block),
            self.valuator.functions.maxObservationDivergenceBps().call(
                block_identifier=block
            ),
            self.valuator.functions.observationQuorum().call(block_identifier=block),
        )
        return {
            "block": block,
            "nav": nav,
            "strategy_hash": strategy_hash,
            "strategy_config": strategy_config,
            "adapter_config": adapter_config,
            "adapter_state": adapter_state,
            "valuation_policy": valuation_policy,
            "total_assets": self.vault.functions.totalAssets().call(
                block_identifier=block
            ),
            "idle_assets": self.vault.functions.accountedIdleAssets().call(
                block_identifier=block
            ),
            "allocated": self.strategy.functions.allocatedToAdapter(
                self.adapter_address, self.usdc
            ).call(block_identifier=block),
            "minimum_idle_bps": self.strategy.functions.minimumIdleBps().call(
                block_identifier=block
            ),
            "processing": self.flow.functions.hasActiveProcessing().call(
                block_identifier=block
            ),
            "pending_shares": self.flow.functions.totalPendingShares().call(
                block_identifier=block
            ),
        }

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

    def _send(self, function: Any) -> str:
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        tx = function.build_transaction(
            {
                "from": self.account.address,
                "chainId": 84532,
                "nonce": nonce,
                "gasPrice": self.w3.eth.gas_price,
            }
        )
        estimate = self.w3.eth.estimate_gas(tx)
        tx["gas"] = estimate * 120 // 100
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"Fund allocator transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def _settle(self, state: dict[str, Any]) -> bool:
        adapter_state = state["adapter_state"]
        if adapter_state[3] == 0:
            return False
        position_id = adapter_state[2]
        position = self.adapter.functions.position(position_id).call()
        lifecycle = position[11]
        otoken = self.w3.eth.contract(address=position[0], abi=_OTOKEN_ABI)
        if (
            lifecycle == 1
            and self.w3.eth.get_block("latest").timestamp
            < otoken.functions.expiry().call()
        ):
            return True
        if lifecycle not in {1, 2}:
            raise RuntimeError(f"Unexpected active CSP lifecycle {lifecycle}")
        target_value = state["allocated"] if state["allocated"] else 1
        data = self.w3.codec.encode(
            ["(uint8,uint256,uint256,uint256)"],
            [(1, position_id, 0, 0)],
        )
        tx_hash = self._send(
            self.strategy.functions.deallocate(
                self.adapter_address,
                target_value,
                0,
                data,
            )
        )
        log.info(
            "CSP allocator decision=%s position_id=%d tx=%s",
            "settle" if lifecycle == 1 else "complete_assignment",
            position_id,
            tx_hash,
        )
        return True

    def _open(self, state: dict[str, Any]) -> None:
        adapter_state = state["adapter_state"]
        if adapter_state[3] != 0 or state["allocated"] != 0:
            return
        if state["pending_shares"] != 0:
            log.info(
                "CSP allocator decision=skip reason=pending_redemptions shares=%d",
                state["pending_shares"],
            )
            return
        latest_pending_shares = self.flow.functions.totalPendingShares().call()
        if latest_pending_shares != 0:
            log.info(
                "CSP allocator decision=skip reason=pending_redemptions_latest "
                "shares=%d",
                latest_pending_shares,
            )
            return
        idle_assets = state["idle_assets"]
        target = liquid_collateral_target(idle_assets, self.policy)
        if target <= 0:
            log.info("CSP allocator decision=skip reason=no_liquid_usdc")
            return
        market = api_client.get_market_data(asset="eth", chain="base")
        quote = select_policy_quote(
            api_client.get_quotes(),
            spot=float(market["spot"]),
            now=int(time.time()),
            policy=self.policy,
            series_validator=self._is_compatible_put_series,
        )
        if quote is None:
            raise RuntimeError("No live signed quote matches the 15%-OTM 48h policy")
        strike_raw = int(Decimal(str(quote["strike_price"])) * OTOKEN_SCALE)
        option_amount = min(
            option_amount_for_collateral(target, strike_raw),
            int(quote["max_amount"]),
        )
        collateral = required_collateral(option_amount, strike_raw)
        if option_amount <= 0 or collateral <= 0 or collateral > target:
            raise RuntimeError(
                "Matching quote cannot fill the bounded collateral target"
            )
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
        tx_hash = self._send(
            self.strategy.functions.allocate(
                self.adapter_address,
                self.usdc,
                collateral,
                open_data,
            )
        )
        log.info(
            "CSP allocator decision=open strike=%s collateral_usdc=%.6f "
            "option_amount=%.8f tx=%s",
            quote["strike_price"],
            collateral / USDC_SCALE,
            option_amount / OTOKEN_SCALE,
            tx_hash,
        )

    def run_once(self) -> None:
        state = self._read_gate_state(self._safe_block())
        self._validate_policy_gates(state)
        if self._settle(state):
            return
        self._open(state)

    def run_forever(self) -> None:
        log.info(
            "CSP fund allocator enabled: address=%s policy=B1N-341 interval=%ds",
            self.account.address,
            config.FUND_ALLOCATOR_INTERVAL_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning("CSP allocator cycle failed closed", exc_info=True)
            time.sleep(config.FUND_ALLOCATOR_INTERVAL_SECONDS)


def start() -> threading.Thread | None:
    if not config.FUND_ALLOCATOR_ENABLED:
        log.info("CSP fund allocator disabled")
        return None
    allocator = CspFundAllocator()
    thread = threading.Thread(
        target=allocator.run_forever,
        name="csp-fund-allocator",
        daemon=True,
    )
    thread.start()
    return thread
