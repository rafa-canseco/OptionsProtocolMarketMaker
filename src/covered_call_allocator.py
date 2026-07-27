"""Fail-closed Base Sepolia allocator for the B1N-362 covered-call fund."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3

from src import api_client, config
from src.fund_allocator import (
    _FLOW_ABI,
    _OTOKEN_ABI,
    _STRATEGY_ABI,
    _VAULT_ABI,
    safe_block_has_coherent_nav,
    sign_fund_quote,
)
from src.fund_tx import ConfirmedTransaction, send_confirmed_transaction
from src.pricer import bs_delta

log = logging.getLogger(__name__)

BPS = 10_000
WETH_SCALE = 10**18
USDC_SCALE = 10**6
OTOKEN_SCALE = 10**8
CALL_COLLATERAL_DENOMINATOR = 10**10
USDC_TO_WETH_ORACLE_SCALE = 10**20

_POLICY_EXPECTED = {
    "strike_rule": "target_call_delta",
    "strike_parameter": Decimal("0.05"),
    "maximum_delta_deviation_bps": 150,
    "strike_tick_usd": 25,
    "target_duration_hours": 48,
    "reopen_cadence_hours": 48,
    "target_utilization_bps": 2500,
    "minimum_net_premium_bps": 10,
    "maximum_open_positions": 1,
    "called_away_action": "normalize_all_usdc_to_weth_then_reopen",
    "premium_action": "normalize_all_usdc_to_weth_after_settlement",
    "continuous_operation": "reopen_while_free_weth_and_no_pending_redemptions",
}

_ADAPTER_ABI = [
    {
        "name": "addressBook",
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
                    {"name": "activeCollateral", "type": "uint256"},
                    {"name": "accountedWeth", "type": "uint256"},
                    {"name": "accountedUsdc", "type": "uint256"},
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
                            {"name": "maxUtilizationBps", "type": "uint16"},
                            {"name": "minStrike", "type": "uint256"},
                            {"name": "maxStrike", "type": "uint256"},
                            {"name": "maxCollateralPerPosition", "type": "uint256"},
                            {"name": "maxUsdcPerSwap", "type": "uint256"},
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
                    {"name": "calledAwayUsdc", "type": "uint256"},
                    {"name": "fallbackWethRecovered", "type": "uint256"},
                    {"name": "mmWethPayout", "type": "uint256"},
                    {"name": "usdcBalanceBeforeDelivery", "type": "uint256"},
                    {"name": "openedAt", "type": "uint64"},
                    {"name": "fallbackEligibleAt", "type": "uint64"},
                    {"name": "lifecycle", "type": "uint8"},
                    {"name": "lifecycleHash", "type": "bytes32"},
                ],
            }
        ],
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
        "name": "liabilityBufferBps",
        "type": "function",
        "stateMutability": "view",
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

_ADDRESS_BOOK_ABI = [
    {
        "name": "oracle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    }
]

_ORACLE_ABI = [
    {
        "name": "getPrice",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [{"type": "uint256"}],
    }
]


@dataclass(frozen=True)
class CoveredCallPolicy:
    target_delta: Decimal
    maximum_delta_deviation_bps: int
    strike_tick_usd: int
    target_duration_seconds: int
    target_utilization_bps: int
    minimum_net_premium_bps: int
    maximum_open_positions: int
    maximum_vault_aum: int
    maximum_collateral: int
    maximum_usdc_per_swap: int
    maximum_opened_positions_before_review: int
    maximum_call_aways_before_review: int
    min_expiry_delay: int
    max_expiry_delay: int
    settlement_default_delay: int
    maximum_swap_slippage_bps: int
    min_strike: int
    max_strike: int
    strategy_maximum_loss_bps: int
    onchain_minimum_idle_bps: int
    swap_fee_tier: int
    liability_buffer_bps: int
    observation_quorum: int


def load_covered_call_policy(path: str | Path) -> CoveredCallPolicy:
    policy_path = Path(path)
    try:
        raw = json.loads(policy_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to load covered-call policy: {error}") from error
    expected_scope = {
        "chain_id": 84532,
        "environment": "base_sepolia",
        "strategy": "covered_call",
        "underlying": "ETH",
        "accounting_asset": "WETH",
        "transient_asset": "USDC",
    }
    selection = raw.get("selection", {})
    normalized_selection = {
        **selection,
        "strike_parameter": Decimal(str(selection.get("strike_parameter"))),
    }
    if (
        raw.get("schema_version") != 1
        or raw.get("authority_issue") != "B1N-362"
        or raw.get("decision") != "go_testnet_only"
        or raw.get("activation_allowed") is not True
        or raw.get("mainnet_authorized") is not False
        or raw.get("scope") != expected_scope
        or normalized_selection != _POLICY_EXPECTED
    ):
        raise ValueError("Covered-call allocator policy is not approved for B1N-362")
    bounds = raw["base_sepolia_bounds"]
    valuation = raw["valuation"]
    if bounds.get("mock_assets_only") is not True or bounds.get("chain_id") != 84532:
        raise ValueError("Covered-call policy is not mock-only Base Sepolia")
    return CoveredCallPolicy(
        target_delta=Decimal(str(selection["strike_parameter"])),
        maximum_delta_deviation_bps=int(selection["maximum_delta_deviation_bps"]),
        strike_tick_usd=int(selection["strike_tick_usd"]),
        target_duration_seconds=int(selection["target_duration_hours"]) * 3600,
        target_utilization_bps=int(selection["target_utilization_bps"]),
        minimum_net_premium_bps=int(selection["minimum_net_premium_bps"]),
        maximum_open_positions=int(selection["maximum_open_positions"]),
        maximum_vault_aum=int(
            Decimal(str(bounds["maximum_vault_aum_weth"])) * WETH_SCALE
        ),
        maximum_collateral=int(
            Decimal(str(bounds["maximum_collateral_per_position_weth"])) * WETH_SCALE
        ),
        maximum_usdc_per_swap=int(
            Decimal(str(bounds["maximum_usdc_per_swap"])) * USDC_SCALE
        ),
        maximum_opened_positions_before_review=int(
            bounds["maximum_opened_positions_before_review"]
        ),
        maximum_call_aways_before_review=int(
            bounds["maximum_call_aways_before_review"]
        ),
        min_expiry_delay=int(bounds["minimum_expiry_delay_seconds"]),
        max_expiry_delay=int(bounds["maximum_expiry_delay_seconds"]),
        settlement_default_delay=int(bounds["settlement_default_delay_seconds"]),
        maximum_swap_slippage_bps=int(bounds["maximum_swap_slippage_bps"]),
        min_strike=int(bounds["minimum_strike_usd"]) * OTOKEN_SCALE,
        max_strike=int(bounds["maximum_strike_usd"]) * OTOKEN_SCALE,
        strategy_maximum_loss_bps=int(bounds["strategy_maximum_loss_bps"]),
        onchain_minimum_idle_bps=int(bounds["onchain_minimum_idle_bps"]),
        swap_fee_tier=int(bounds["swap_fee_tier"]),
        liability_buffer_bps=int(valuation["liability_buffer_bps"]),
        observation_quorum=int(valuation["observation_quorum"]),
    )


def call_collateral_target(idle_weth: int, policy: CoveredCallPolicy) -> int:
    return min(
        idle_weth * policy.target_utilization_bps // BPS,
        policy.maximum_collateral,
    )


def option_amount_for_call_collateral(collateral: int) -> int:
    return collateral // CALL_COLLATERAL_DENOMINATOR


def call_collateral_for_option_amount(option_amount: int) -> int:
    return option_amount * CALL_COLLATERAL_DENOMINATOR


def select_covered_call_quote(
    quotes: list[dict[str, Any]],
    *,
    spot: float,
    iv: float,
    now: int,
    risk_free_rate: float,
    policy: CoveredCallPolicy,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, float, int, dict[str, Any]]] = []
    for quote in quotes:
        expiry = int(quote.get("expiry") or 0)
        strike = Decimal(str(quote.get("strike_price") or 0))
        delay = expiry - now
        if (
            quote.get("asset") != "eth"
            or quote.get("chain", "base") != "base"
            or quote.get("is_put") is not False
            or int(quote.get("deadline") or 0) <= now + 15
            or not policy.min_expiry_delay <= delay <= policy.max_expiry_delay
            or strike * OTOKEN_SCALE < policy.min_strike
            or strike * OTOKEN_SCALE > policy.max_strike
            or strike <= Decimal(str(spot))
            or int(quote.get("bid_price") or 0) <= 0
        ):
            continue
        time_years = delay / (365 * 86_400)
        actual_delta = bs_delta(
            False,
            spot,
            float(strike),
            time_years,
            risk_free_rate,
            iv,
        )
        deviation_bps = abs(actual_delta - float(policy.target_delta)) * BPS
        if deviation_bps <= policy.maximum_delta_deviation_bps:
            candidates.append(
                (
                    abs(delay - policy.target_duration_seconds),
                    deviation_bps,
                    -int(quote["deadline"]),
                    quote,
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda value: (value[0], value[1], value[2]))[3]


def normalization_minimum_weth_out(
    usdc_amount: int, spot_price: int, slippage_bps: int
) -> int:
    if usdc_amount <= 0 or spot_price <= 0 or not 0 <= slippage_bps <= BPS:
        raise ValueError("invalid normalization inputs")
    expected = usdc_amount * USDC_TO_WETH_ORACLE_SCALE // spot_price
    return expected * (BPS - slippage_bps) // BPS


def count_called_away(positions: list[tuple[Any, ...]]) -> int:
    return sum(int(position[13]) == 4 for position in positions)


class CoveredCallFundAllocator:
    def __init__(self) -> None:
        policy_path = Path(config.COVERED_CALL_ALLOCATOR_POLICY_PATH)
        self.policy = load_covered_call_policy(policy_path)
        self.policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        self._validate_runtime_config()
        self.w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
        if self.w3.eth.chain_id != 84532:
            raise RuntimeError("Covered-call allocator is locked to Base Sepolia")
        self.account = Account.from_key(config.COVERED_CALL_ALLOCATOR_PRIVATE_KEY)
        self.vault = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.COVERED_CALL_VAULT_ADDRESS),
            abi=_VAULT_ABI,
        )
        self.flow = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.COVERED_CALL_FLOW_MANAGER_ADDRESS),
            abi=_FLOW_ABI,
        )
        self.strategy = self.w3.eth.contract(
            address=Web3.to_checksum_address(
                config.COVERED_CALL_STRATEGY_MANAGER_ADDRESS
            ),
            abi=_STRATEGY_ABI,
        )
        self.adapter_address = Web3.to_checksum_address(
            config.COVERED_CALL_ADAPTER_ADDRESS
        )
        self.adapter = self.w3.eth.contract(
            address=self.adapter_address, abi=_ADAPTER_ABI
        )
        self.valuator_address = Web3.to_checksum_address(
            config.COVERED_CALL_VALUATOR_ADDRESS
        )
        self.valuator = self.w3.eth.contract(
            address=self.valuator_address, abi=_VALUATOR_ABI
        )
        self.weth = Web3.to_checksum_address(config.COVERED_CALL_WETH_ADDRESS)
        address_book = self.adapter.functions.addressBook().call()
        self.address_book = self.w3.eth.contract(
            address=address_book, abi=_ADDRESS_BOOK_ABI
        )
        self.oracle = self.w3.eth.contract(
            address=self.address_book.functions.oracle().call(), abi=_ORACLE_ABI
        )
        for address in (
            self.vault.address,
            self.flow.address,
            self.strategy.address,
            self.adapter.address,
            self.valuator.address,
            self.weth,
            self.oracle.address,
        ):
            if not self.w3.eth.get_code(address):
                raise RuntimeError(
                    f"Configured covered-call address has no code: {address}"
                )

    @staticmethod
    def _validate_runtime_config() -> None:
        required = {
            "COVERED_CALL_ALLOCATOR_PRIVATE_KEY": (
                config.COVERED_CALL_ALLOCATOR_PRIVATE_KEY
            ),
            "COVERED_CALL_VAULT_ADDRESS": config.COVERED_CALL_VAULT_ADDRESS,
            "COVERED_CALL_FLOW_MANAGER_ADDRESS": (
                config.COVERED_CALL_FLOW_MANAGER_ADDRESS
            ),
            "COVERED_CALL_STRATEGY_MANAGER_ADDRESS": (
                config.COVERED_CALL_STRATEGY_MANAGER_ADDRESS
            ),
            "COVERED_CALL_ADAPTER_ADDRESS": config.COVERED_CALL_ADAPTER_ADDRESS,
            "COVERED_CALL_VALUATOR_ADDRESS": config.COVERED_CALL_VALUATOR_ADDRESS,
            "COVERED_CALL_WETH_ADDRESS": config.COVERED_CALL_WETH_ADDRESS,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise RuntimeError(f"Missing covered-call configuration: {missing}")
        if config.CHAIN_ID != 84532:
            raise RuntimeError("Covered-call allocator requires CHAIN_ID=84532")
        environment = config._current_environment()
        if environment and environment not in {"staging", "development", "test"}:
            raise RuntimeError("Covered-call allocator is non-production only")

    def _safe_block(self) -> int:
        return max(
            self.w3.eth.block_number - config.COVERED_CALL_ALLOCATOR_CONFIRMATIONS,
            0,
        )

    def _read_state(self, block: int) -> dict[str, Any]:
        adapter_state = self.adapter.functions.adapterState().call(
            block_identifier=block
        )
        position_count = int(adapter_state[2])
        positions = [
            self.adapter.functions.position(position_id).call(block_identifier=block)
            for position_id in range(1, position_count + 1)
        ]
        return {
            "block": block,
            "nav": self.vault.functions.activeNavWindow().call(block_identifier=block),
            "strategy_hash": self.strategy.functions.positionsHash().call(
                block_identifier=block
            ),
            "strategy_config": self.strategy.functions.strategyConfig(
                self.adapter_address
            ).call(block_identifier=block),
            "adapter_config": self.adapter.functions.adapterConfig().call(
                block_identifier=block
            ),
            "adapter_state": adapter_state,
            "positions": positions,
            "valuation_policy": (
                self.valuator.functions.interfaceVersion().call(block_identifier=block),
                self.valuator.functions.liabilityBufferBps().call(
                    block_identifier=block
                ),
                self.valuator.functions.observationQuorum().call(
                    block_identifier=block
                ),
            ),
            "total_assets": self.vault.functions.totalAssets().call(
                block_identifier=block
            ),
            "idle_assets": self.vault.functions.accountedIdleAssets().call(
                block_identifier=block
            ),
            "allocated": self.strategy.functions.allocatedToAdapter(
                self.adapter_address, self.weth
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

    def _validate_policy_gates(
        self, state: dict[str, Any], *, require_active_nav: bool = True
    ) -> None:
        policy = self.policy
        nav = state["nav"]
        strategy_config = state["strategy_config"]
        risk = state["adapter_config"][0]
        if require_active_nav and (
            not safe_block_has_coherent_nav(nav, state["block"])
            or nav[10] != state["strategy_hash"]
        ):
            raise RuntimeError("No coherent active NAV at the safe block")
        if state["processing"]:
            raise RuntimeError("Fund flow processing is active")
        if tuple(state["valuation_policy"]) != (
            1,
            policy.liability_buffer_bps,
            policy.observation_quorum,
        ):
            raise RuntimeError("Covered-call valuator differs from policy")
        expected_strategy = (
            True,
            policy.target_utilization_bps,
            policy.strategy_maximum_loss_bps,
            0,
            1,
            self.valuator_address,
            policy.maximum_collateral,
        )
        if tuple(strategy_config) != expected_strategy:
            raise RuntimeError("StrategyManager config differs from policy")
        expected_risk = (
            policy.min_expiry_delay,
            policy.max_expiry_delay,
            policy.settlement_default_delay,
            policy.minimum_net_premium_bps,
            policy.maximum_swap_slippage_bps,
            policy.maximum_open_positions,
            policy.target_utilization_bps,
            policy.min_strike,
            policy.max_strike,
            policy.maximum_collateral,
            policy.maximum_usdc_per_swap,
        )
        if tuple(risk) != expected_risk:
            raise RuntimeError("Covered-call adapter config differs from policy")
        if state["adapter_config"][2] != policy.swap_fee_tier:
            raise RuntimeError("Covered-call swap fee tier differs from policy")
        if state["minimum_idle_bps"] != policy.onchain_minimum_idle_bps:
            raise RuntimeError("On-chain minimum idle differs from policy")

    def _send(self, function: Any) -> ConfirmedTransaction:
        return send_confirmed_transaction(
            w3=self.w3,
            account=self.account,
            function=function,
            chain_id=84532,
            confirmations=config.COVERED_CALL_ALLOCATOR_CONFIRMATIONS,
        )

    def _result_state(self, tx: ConfirmedTransaction) -> tuple[Any, ...]:
        return self.adapter.functions.adapterState().call(
            block_identifier=tx.block_number
        )

    def _settle_or_normalize(self, state: dict[str, Any]) -> bool:
        adapter_state = state["adapter_state"]
        active_positions = int(adapter_state[3])
        accounted_weth = int(adapter_state[5])
        accounted_usdc = int(adapter_state[6])
        if active_positions:
            position_id = int(adapter_state[2])
            position = state["positions"][position_id - 1]
            lifecycle = int(position[13])
            if lifecycle == 1:
                expiry = (
                    self.w3.eth.contract(address=position[0], abi=_OTOKEN_ABI)
                    .functions.expiry()
                    .call()
                )
                if self.w3.eth.get_block("latest").timestamp < expiry:
                    return True
            if lifecycle not in {1, 2}:
                raise RuntimeError(f"Unexpected covered-call lifecycle {lifecycle}")
            data = self.w3.codec.encode(
                ["(uint8,uint256,uint256,uint256)"],
                [(1, position_id, 0, 0)],
            )
            tx = self._send(
                self.strategy.functions.deallocate(self.adapter_address, 1, 0, data)
            )
            result = self._result_state(tx)
            log.info(
                "Covered call decision=%s position_id=%d policy_hash=%s "
                "report_nonce=%d report_hash=%s tx=%s tx_nonce=%d "
                "replaced=%s state_nonce=%d positions_hash=%s",
                "settle" if lifecycle == 1 else "complete_physical_delivery",
                position_id,
                self.policy_hash,
                state["nav"][9],
                Web3.to_hex(state["nav"][11]),
                tx.tx_hash,
                tx.nonce,
                tx.replaced,
                result[0],
                Web3.to_hex(result[1]),
            )
            return True

        if accounted_usdc:
            amount = min(accounted_usdc, self.policy.maximum_usdc_per_swap)
            spot_price = self.oracle.functions.getPrice(self.weth).call()
            minimum_weth = normalization_minimum_weth_out(
                amount, spot_price, self.policy.maximum_swap_slippage_bps
            )
            is_last_swap = amount == accounted_usdc
            target_value = max(int(state["allocated"]), 1) if is_last_swap else 1
            data = self.w3.codec.encode(
                ["(uint8,uint256,uint256,uint256)"],
                [(2, 0, amount, minimum_weth)],
            )
            tx = self._send(
                self.strategy.functions.deallocate(
                    self.adapter_address, target_value, 0, data
                )
            )
            result = self._result_state(tx)
            log.info(
                "Covered call decision=normalize_usdc amount=%d min_weth=%d "
                "policy_hash=%s report_nonce=%d report_hash=%s tx=%s "
                "tx_nonce=%d replaced=%s state_nonce=%d positions_hash=%s",
                amount,
                minimum_weth,
                self.policy_hash,
                state["nav"][9],
                Web3.to_hex(state["nav"][11]),
                tx.tx_hash,
                tx.nonce,
                tx.replaced,
                result[0],
                Web3.to_hex(result[1]),
            )
            return True

        if accounted_weth:
            data = self.w3.codec.encode(
                ["(uint8,uint256,uint256,uint256)"],
                [(0, 0, 0, 0)],
            )
            tx = self._send(
                self.strategy.functions.deallocate(
                    self.adapter_address, accounted_weth, accounted_weth, data
                )
            )
            result = self._result_state(tx)
            log.info(
                "Covered call decision=return_idle_weth amount=%d policy_hash=%s "
                "report_nonce=%d report_hash=%s tx=%s tx_nonce=%d replaced=%s "
                "state_nonce=%d positions_hash=%s",
                accounted_weth,
                self.policy_hash,
                state["nav"][9],
                Web3.to_hex(state["nav"][11]),
                tx.tx_hash,
                tx.nonce,
                tx.replaced,
                result[0],
                Web3.to_hex(result[1]),
            )
            return True
        return False

    def _open(self, state: dict[str, Any]) -> None:
        adapter_state = state["adapter_state"]
        if (
            int(adapter_state[3]) != 0
            or int(adapter_state[5]) != 0
            or int(adapter_state[6]) != 0
            or int(state["allocated"]) != 0
        ):
            return
        if state["pending_shares"] != 0:
            log.info("Covered call decision=skip reason=pending_redemptions")
            return
        if self.flow.functions.totalPendingShares().call() != 0:
            log.info("Covered call decision=skip reason=pending_redemptions_latest")
            return
        if state["total_assets"] > self.policy.maximum_vault_aum:
            raise RuntimeError("Covered-call vault exceeds validation AUM cap")
        if int(adapter_state[2]) >= self.policy.maximum_opened_positions_before_review:
            log.info("Covered call decision=skip reason=cycle_review_cap")
            return
        if (
            count_called_away(state["positions"])
            >= self.policy.maximum_call_aways_before_review
        ):
            log.info("Covered call decision=skip reason=call_away_review_cap")
            return

        target = call_collateral_target(state["idle_assets"], self.policy)
        option_amount = option_amount_for_call_collateral(target)
        collateral = call_collateral_for_option_amount(option_amount)
        if option_amount <= 0 or collateral <= 0:
            log.info("Covered call decision=skip reason=no_deployable_weth")
            return
        market = api_client.get_market_data(asset="eth", chain="base")
        now = int(time.time())
        quote = select_covered_call_quote(
            api_client.get_quotes(),
            spot=float(market["spot"]),
            iv=float(market["iv"]),
            now=now,
            risk_free_rate=config.RISK_FREE_RATE,
            policy=self.policy,
        )
        if quote is None:
            raise RuntimeError("No live signed quote matches covered-call policy")
        option_amount = min(option_amount, int(quote["max_amount"]))
        collateral = call_collateral_for_option_amount(option_amount)
        if option_amount <= 0 or collateral > target:
            raise RuntimeError("Covered-call quote cannot fill bounded target")
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
        quote_hash = hashlib.sha256(
            json.dumps(
                {
                    key: quote[key]
                    for key in (
                        "otoken_address",
                        "bid_price",
                        "deadline",
                        "quote_id",
                        "max_amount",
                        "maker_nonce",
                    )
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        tx = self._send(
            self.strategy.functions.allocate(
                self.adapter_address, self.weth, collateral, open_data
            )
        )
        result = self._result_state(tx)
        log.info(
            "Covered call decision=open strike=%s collateral_weth=%.8f "
            "option_amount=%.8f policy_hash=%s report_nonce=%d report_hash=%s "
            "quote_id=%s quote_hash=%s tx=%s tx_nonce=%d replaced=%s "
            "state_nonce=%d positions_hash=%s",
            quote["strike_price"],
            collateral / WETH_SCALE,
            option_amount / OTOKEN_SCALE,
            self.policy_hash,
            state["nav"][9],
            Web3.to_hex(state["nav"][11]),
            quote["quote_id"],
            quote_hash,
            tx.tx_hash,
            tx.nonce,
            tx.replaced,
            result[0],
            Web3.to_hex(result[1]),
        )

    def run_once(self) -> None:
        state = self._read_state(self._safe_block())
        adapter_state = state["adapter_state"]
        awaiting_physical_delivery = False
        if int(adapter_state[3]) != 0:
            position_id = int(adapter_state[2])
            awaiting_physical_delivery = (
                int(state["positions"][position_id - 1][13]) == 2
            )
        # The valuator intentionally rejects AwaitingPhysicalDelivery, so no
        # fresh NAV can exist during that transient state. Completing physical
        # delivery/fallback is bounded by the adapter ledger and safe-block
        # state. Every valuation-sensitive action again requires an active NAV.
        self._validate_policy_gates(
            state, require_active_nav=not awaiting_physical_delivery
        )
        if self._settle_or_normalize(state):
            return
        self._open(state)

    def run_forever(self) -> None:
        log.info(
            "Covered-call allocator enabled address=%s policy=B1N-362 interval=%ds",
            self.account.address,
            config.COVERED_CALL_ALLOCATOR_INTERVAL_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning("Covered-call allocator cycle failed closed", exc_info=True)
            time.sleep(config.COVERED_CALL_ALLOCATOR_INTERVAL_SECONDS)


def start() -> threading.Thread | None:
    if not config.COVERED_CALL_ALLOCATOR_ENABLED:
        log.info("Covered-call allocator disabled")
        return None
    allocator = CoveredCallFundAllocator()
    thread = threading.Thread(
        target=allocator.run_forever,
        name="covered-call-fund-allocator",
        daemon=True,
    )
    thread.start()
    return thread
