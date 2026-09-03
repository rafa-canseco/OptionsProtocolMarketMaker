"""Opt-in, fail-closed NVDAc settlement allocator for Base.

The runtime owns pricing and gating only. Settlement submission is the
backend redeem boundary; see ``Web3NvdacGateway.submit``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from web3 import Web3

BASE_CHAIN_ID = 8453
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NVDAC = "0xb20000000000000000000078ee7ce2fE4908108C"
AERODROME_ROUTER = "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F"
AERODROME_QUOTER = "0x514c8B5f54112481E28028F1166Bd78501089259"
AERODROME_FACTORY = "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef"
NVDAC_POOL = "0x853F5f1B92b16714Fe6CDA67CAad0856B83C7ab9"
NVDAC_FEED = "0x04689a41629776563E6822F76f2e57D148d28513"
ORACLE_REGISTRY = "0x3f3E8cf41cdd3b1D118c16471aB0113DfDDd5CaD"
POLICY_REGISTRY = "0x8453000000000000000000000000000000000002"
NVDAC_DECIMALS = 8
NVDAC_TICK_SPACING = 10
NVDAC_FEE = 500
NVDAC_B20_POLICY = 5
ORACLE_MAX_AGE_SECONDS = 3600
POOL_IMPACT_BPS = 30
ORACLE_DEVIATION_BPS = 100
EXECUTION_SLIPPAGE_BPS = 30
WAD = 10**18
Q192 = 1 << 192

_FACTORY_ABI = [
    {
        "type": "function",
        "name": "factory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    }
]

_SETTLER_ABI = [
    {
        "type": "function",
        "name": "swapRouter",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    }
]

_FACADE_ABI = [
    {
        "type": "function",
        "name": "settler",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "name": "routeKey",
        "stateMutability": "view",
        "inputs": [
            {"type": "address", "name": "tokenIn"},
            {"type": "address", "name": "tokenOut"},
            {"type": "uint8", "name": "kind"},
        ],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "routes",
        "stateMutability": "view",
        "inputs": [{"type": "bytes32", "name": "key"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "adapter", "type": "address"},
                    {"name": "pendingAdapter", "type": "address"},
                    {"name": "activateAfter", "type": "uint48"},
                ],
            }
        ],
    },
]

_ADAPTER_ABI = [
    {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": output}],
    }
    for name, output in (
        ("facade", "address"),
        ("settler", "address"),
        ("venueRouter", "address"),
        ("factory", "address"),
        ("pool", "address"),
        ("tokenA", "address"),
        ("tokenB", "address"),
        ("tickSpacing", "int24"),
        ("effectiveFee", "uint24"),
    )
]

_POOL_ABI = [
    {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": output}],
    }
    for name, output in (
        ("token0", "address"),
        ("token1", "address"),
        ("factory", "address"),
        ("tickSpacing", "int24"),
        ("fee", "uint24"),
        ("liquidity", "uint128"),
    )
]

_B20_ABI = [
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
    },
    {
        "type": "function",
        "name": "isPaused",
        "stateMutability": "view",
        "inputs": [{"type": "uint8"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "multiplier",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "TRANSFER_SENDER_POLICY",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "TRANSFER_RECEIVER_POLICY",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "TRANSFER_EXECUTOR_POLICY",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "policyId",
        "stateMutability": "view",
        "inputs": [{"type": "bytes32"}],
        "outputs": [{"type": "uint64"}],
    },
]

_ORACLE_REGISTRY_ABI = [
    {
        "type": "function",
        "name": "getOracleParams",
        "stateMutability": "view",
        "inputs": [{"type": "address", "name": "token"}],
        "outputs": [
            {"type": "uint256", "name": "multiplier"},
            {"type": "bool", "name": "paused"},
        ],
    }
]

_POLICY_REGISTRY_ABI = [
    {
        "type": "function",
        "name": "policyExists",
        "stateMutability": "view",
        "inputs": [{"type": "uint64", "name": "policyId"}],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "isAuthorized",
        "stateMutability": "view",
        "inputs": [
            {"type": "uint64", "name": "policyId"},
            {"type": "address", "name": "account"},
        ],
        "outputs": [{"type": "bool"}],
    },
]

_AGGREGATOR_ABI = [
    {
        "type": "function",
        "name": "latestRoundData",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"type": "uint80", "name": "roundId"},
            {"type": "int256", "name": "answer"},
            {"type": "uint256", "name": "startedAt"},
            {"type": "uint256", "name": "updatedAt"},
            {"type": "uint80", "name": "answeredInRound"},
        ],
    }
]

_QUOTER_ABI = [
    *_FACTORY_ABI,
    {
        "type": "function",
        "name": "quoteExactInputSingle",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "type": "tuple",
                "name": "params",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"type": "uint256"},
            {"type": "uint160"},
            {"type": "uint32"},
            {"type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "quoteExactOutputSingle",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "type": "tuple",
                "name": "params",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"type": "uint256"},
            {"type": "uint160"},
            {"type": "uint32"},
            {"type": "uint256"},
        ],
    },
]


class SwapMode(StrEnum):
    EXACT_OUTPUT = "exact_output"
    EXACT_INPUT = "exact_input"


@dataclass(frozen=True)
class RouteTuple:
    adapter: str
    pending_adapter: str
    activate_after: int


@dataclass(frozen=True)
class Preflight:
    chain_id: int
    token_in: str
    token_out: str
    route: RouteTuple
    facade: str
    settler: str
    adapter_facade: str
    adapter_settler: str
    venue_router: str
    factory: str
    pool: str
    tick_spacing: int
    fee: int
    pool_liquidity: int
    token_decimals: int
    b20_paused: bool
    b20_multiplier: int
    registry_multiplier: int
    registry_paused: bool
    b20_policy_ids: tuple[int, int, int]
    participants_authorized: bool
    oracle_feed: str
    oracle_updated_at: int


@dataclass(frozen=True)
class VenueQuote:
    amount: int
    pool_spot_amount: int
    oracle_amount: int


@dataclass(frozen=True)
class SettlementChunk:
    is_put: bool
    asset_amount: int


@dataclass(frozen=True)
class SettlementRequest:
    mode: SwapMode
    token_in: str
    token_out: str
    amount: int
    slippage_limit: int


class SettlementGateway(Protocol):
    def preflight(self, chunk: SettlementChunk) -> Preflight: ...

    def quote(self, request: SettlementRequest) -> VenueQuote: ...

    def submit(self, request: SettlementRequest) -> str: ...


class RuntimeHalted(RuntimeError):
    """A settlement guard stopped the isolated runtime."""


class NvdacAllocator:
    def __init__(
        self,
        gateway: SettlementGateway,
        *,
        expected_facade: str,
        expected_settler: str,
        expected_adapter: str,
        routed_enabled: bool = False,
        nvdac_enabled: bool = False,
        max_chunks: int = 16,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        if not 1 <= max_chunks <= 16:
            raise ValueError("max_chunks must be between 1 and 16")
        self.gateway = gateway
        self.expected_facade = self._address(expected_facade, "facade")
        self.expected_settler = self._address(expected_settler, "settler")
        self.expected_adapter = self._address(expected_adapter, "adapter")
        self.routed_enabled = routed_enabled
        self.nvdac_enabled = nvdac_enabled
        self.max_chunks = max_chunks
        self.alert = alert or (lambda _message: None)

    def settle(
        self,
        chunks: list[SettlementChunk],
        *,
        clock: Callable[[], int] | None = None,
    ) -> list[str]:
        if not self.routed_enabled:
            return self._halt("routed settlement is disabled")
        if not self.nvdac_enabled:
            return self._halt("NVDAc settlement is disabled")
        if not chunks or len(chunks) > self.max_chunks:
            return self._halt("NVDAc settlement chunk count is outside its bound")

        get_now = clock or (lambda: int(time.time()))
        tx_hashes: list[str] = []
        for chunk in chunks:
            try:
                tx_hashes.append(self._settle_chunk(chunk, get_now()))
            except Exception as exc:
                if isinstance(exc, RuntimeHalted):
                    raise
                self._halt(str(exc))
        return tx_hashes

    def _settle_chunk(self, chunk: SettlementChunk, now: int) -> str:
        if chunk.asset_amount <= 0:
            return self._halt("NVDAc chunk amount must be positive")

        before = self.gateway.preflight(chunk)
        self._validate_preflight(before, chunk, now)
        request = self._request(chunk, 0)
        quote = self.gateway.quote(request)
        self._validate_quote(quote)

        after = self.gateway.preflight(chunk)
        if after.route != before.route:
            return self._halt("NVDAc route changed; fresh quote required")
        self._validate_preflight(after, chunk, now)

        request = self._request(chunk, self._slippage_limit(quote.amount, chunk.is_put))
        return self.gateway.submit(request)

    def _validate_preflight(
        self, state: Preflight, chunk: SettlementChunk, now: int
    ) -> None:
        token_in = USDC if chunk.is_put else NVDAC
        token_out = NVDAC if chunk.is_put else USDC
        expected = {
            "chain": state.chain_id == BASE_CHAIN_ID,
            "ordered token pair": self._same(state.token_in, token_in)
            and self._same(state.token_out, token_out),
            "route adapter": self._same(state.route.adapter, self.expected_adapter),
            "route availability": self._same(state.route.pending_adapter, ZERO_ADDRESS)
            and state.route.activate_after == 0,
            "facade": self._same(state.facade, self.expected_facade),
            "settler": self._same(state.settler, self.expected_settler),
            "adapter binding": self._same(state.adapter_facade, self.expected_facade)
            and self._same(state.adapter_settler, self.expected_settler),
            "venue router": self._same(state.venue_router, AERODROME_ROUTER),
            "factory": self._same(state.factory, AERODROME_FACTORY),
            "pool": self._same(state.pool, NVDAC_POOL),
            "pool parameters": state.tick_spacing == NVDAC_TICK_SPACING
            and state.fee == NVDAC_FEE,
            "pool liquidity": state.pool_liquidity > 0,
            "B20 decimals": state.token_decimals == NVDAC_DECIMALS,
            "B20 pause": not state.b20_paused and not state.registry_paused,
            "B20 multiplier": state.b20_multiplier > 0
            and state.b20_multiplier == state.registry_multiplier,
            "B20 policy": len(set(state.b20_policy_ids)) == 1
            and state.b20_policy_ids[0] == NVDAC_B20_POLICY
            and state.participants_authorized,
            "oracle feed": self._same(state.oracle_feed, NVDAC_FEED),
            "oracle freshness": 0
            <= now - state.oracle_updated_at
            <= ORACLE_MAX_AGE_SECONDS,
        }
        for name, valid in expected.items():
            if not valid:
                self._halt(f"NVDAc {name} preflight failed")

    def _validate_quote(self, quote: VenueQuote) -> None:
        if min(quote.amount, quote.pool_spot_amount, quote.oracle_amount) <= 0:
            self._halt("NVDAc venue has insufficient liquidity")
        if not self._within_bps(quote.amount, quote.pool_spot_amount, POOL_IMPACT_BPS):
            self._halt("NVDAc pool impact exceeds 30 bps")
        if not self._within_bps(
            quote.amount, quote.oracle_amount, ORACLE_DEVIATION_BPS
        ):
            self._halt("NVDAc oracle deviation exceeds 100 bps")

    @staticmethod
    def _request(chunk: SettlementChunk, slippage_limit: int) -> SettlementRequest:
        return SettlementRequest(
            mode=SwapMode.EXACT_OUTPUT if chunk.is_put else SwapMode.EXACT_INPUT,
            token_in=USDC if chunk.is_put else NVDAC,
            token_out=NVDAC if chunk.is_put else USDC,
            amount=chunk.asset_amount,
            slippage_limit=slippage_limit,
        )

    @staticmethod
    def _slippage_limit(quote: int, is_put: bool) -> int:
        numerator = quote * (
            10_000 + EXECUTION_SLIPPAGE_BPS
            if is_put
            else 10_000 - EXECUTION_SLIPPAGE_BPS
        )
        return (numerator + 9_999) // 10_000 if is_put else numerator // 10_000

    @staticmethod
    def _within_bps(observed: int, expected: int, limit: int) -> bool:
        return expected > 0 and abs(observed - expected) * 10_000 <= expected * limit

    @staticmethod
    def _address(value: str, label: str) -> str:
        if not Web3.is_address(value) or value.lower() == ZERO_ADDRESS:
            raise ValueError(f"Invalid NVDAc {label} address")
        return Web3.to_checksum_address(value)

    @staticmethod
    def _same(left: str, right: str) -> bool:
        return (
            Web3.is_address(left)
            and Web3.is_address(right)
            and left.lower() == right.lower()
        )

    def _halt(self, message: str):
        self.alert(message)
        raise RuntimeHalted(message)


class Web3NvdacGateway:
    """Read-only route, oracle, B20, and venue quote source for NVDAc."""

    def __init__(self, w3: Web3, *, facade: str, settler: str, adapter: str) -> None:
        self.w3 = w3
        self.facade = Web3.to_checksum_address(facade)
        self.settler = Web3.to_checksum_address(settler)
        self.adapter = Web3.to_checksum_address(adapter)
        self.facade_c = w3.eth.contract(self.facade, abi=_FACADE_ABI)
        self.settler_c = w3.eth.contract(self.settler, abi=_SETTLER_ABI)
        self.adapter_c = w3.eth.contract(self.adapter, abi=_ADAPTER_ABI)
        self.pool_c = w3.eth.contract(
            Web3.to_checksum_address(NVDAC_POOL), abi=_POOL_ABI
        )
        self.token_c = w3.eth.contract(Web3.to_checksum_address(NVDAC), abi=_B20_ABI)
        self.oracle_registry_c = w3.eth.contract(
            Web3.to_checksum_address(ORACLE_REGISTRY), abi=_ORACLE_REGISTRY_ABI
        )
        self.policy_registry_c = w3.eth.contract(
            Web3.to_checksum_address(POLICY_REGISTRY), abi=_POLICY_REGISTRY_ABI
        )
        self.feed_c = w3.eth.contract(
            Web3.to_checksum_address(NVDAC_FEED), abi=_AGGREGATOR_ABI
        )
        self.quoter_c = w3.eth.contract(
            Web3.to_checksum_address(AERODROME_QUOTER), abi=_QUOTER_ABI
        )

    def preflight(self, chunk: SettlementChunk) -> Preflight:
        token_in = Web3.to_checksum_address(USDC if chunk.is_put else NVDAC)
        token_out = Web3.to_checksum_address(NVDAC if chunk.is_put else USDC)
        kind = 1 if chunk.is_put else 0

        if self.w3.eth.chain_id != BASE_CHAIN_ID:
            raise RuntimeHalted("NVDAc Base chain mismatch")
        if (
            Web3.to_checksum_address(self.settler_c.functions.swapRouter().call())
            != self.facade
        ):
            raise RuntimeHalted(
                "NVDAc settler is not bound to the configured pair router"
            )
        if (
            Web3.to_checksum_address(self.facade_c.functions.settler().call())
            != self.settler
        ):
            raise RuntimeHalted("NVDAc pair router settler binding mismatch")

        key = self.facade_c.functions.routeKey(token_in, token_out, kind).call()
        adapter, pending, activate_after = self.facade_c.functions.routes(key).call()

        adapter_pair = {
            self.adapter_c.functions.tokenA().call().lower(),
            self.adapter_c.functions.tokenB().call().lower(),
        }
        if (
            self.adapter_c.functions.facade().call().lower() != self.facade.lower()
            or self.adapter_c.functions.settler().call().lower() != self.settler.lower()
            or self.adapter_c.functions.venueRouter().call().lower()
            != AERODROME_ROUTER.lower()
            or self.adapter_c.functions.factory().call().lower()
            != AERODROME_FACTORY.lower()
            or self.adapter_c.functions.pool().call().lower() != NVDAC_POOL.lower()
            or adapter_pair != {USDC.lower(), NVDAC.lower()}
            or int(self.adapter_c.functions.tickSpacing().call()) != NVDAC_TICK_SPACING
            or int(self.adapter_c.functions.effectiveFee().call()) != NVDAC_FEE
        ):
            raise RuntimeHalted("NVDAc Aerodrome adapter immutable identity mismatch")

        if (
            self.pool_c.functions.token0().call().lower() != USDC.lower()
            or self.pool_c.functions.token1().call().lower() != NVDAC.lower()
            or self.pool_c.functions.factory().call().lower()
            != AERODROME_FACTORY.lower()
            or int(self.pool_c.functions.tickSpacing().call()) != NVDAC_TICK_SPACING
            or int(self.pool_c.functions.fee().call()) != NVDAC_FEE
            or int(self.pool_c.functions.liquidity().call()) <= 0
        ):
            raise RuntimeHalted("NVDAc pool identity/state mismatch")

        decimals = int(self.token_c.functions.decimals().call())
        paused = bool(self.token_c.functions.isPaused(0).call())
        multiplier = int(self.token_c.functions.multiplier().call())
        registry_multiplier, registry_paused = (
            self.oracle_registry_c.functions.getOracleParams(
                Web3.to_checksum_address(NVDAC)
            ).call()
        )

        scopes = (
            self.token_c.functions.TRANSFER_SENDER_POLICY().call(),
            self.token_c.functions.TRANSFER_RECEIVER_POLICY().call(),
            self.token_c.functions.TRANSFER_EXECUTOR_POLICY().call(),
        )
        policy_ids = tuple(
            int(self.token_c.functions.policyId(scope).call()) for scope in scopes
        )
        participants = (
            self.settler,
            self.facade,
            self.adapter,
            Web3.to_checksum_address(AERODROME_ROUTER),
            Web3.to_checksum_address(NVDAC_POOL),
        )
        authorized = all(
            self.policy_registry_c.functions.policyExists(policy).call()
            and all(
                self.policy_registry_c.functions.isAuthorized(
                    policy, participant
                ).call()
                for participant in participants
            )
            for policy in policy_ids
        )

        _, answer, _, updated_at, _ = self.feed_c.functions.latestRoundData().call()
        if answer <= 0:
            raise RuntimeHalted("NVDAc oracle answer is not positive")

        return Preflight(
            chain_id=self.w3.eth.chain_id,
            token_in=token_in,
            token_out=token_out,
            route=RouteTuple(adapter.lower(), pending.lower(), int(activate_after)),
            facade=self.facade,
            settler=self.settler,
            adapter_facade=self.adapter_c.functions.facade().call(),
            adapter_settler=self.adapter_c.functions.settler().call(),
            venue_router=self.adapter_c.functions.venueRouter().call(),
            factory=self.adapter_c.functions.factory().call(),
            pool=self.adapter_c.functions.pool().call(),
            tick_spacing=int(self.adapter_c.functions.tickSpacing().call()),
            fee=int(self.adapter_c.functions.effectiveFee().call()),
            pool_liquidity=int(self.pool_c.functions.liquidity().call()),
            token_decimals=decimals,
            b20_paused=paused,
            b20_multiplier=multiplier,
            registry_multiplier=int(registry_multiplier),
            registry_paused=bool(registry_paused),
            b20_policy_ids=policy_ids,
            participants_authorized=authorized,
            oracle_feed=Web3.to_checksum_address(NVDAC_FEED),
            oracle_updated_at=int(updated_at),
        )

    def quote(self, request: SettlementRequest) -> VenueQuote:
        token_in = Web3.to_checksum_address(request.token_in)
        token_out = Web3.to_checksum_address(request.token_out)
        if request.mode is SwapMode.EXACT_OUTPUT:
            amount = int(
                self.quoter_c.functions.quoteExactOutputSingle(
                    (token_in, token_out, request.amount, NVDAC_TICK_SPACING, 0)
                ).call()[0]
            )
        else:
            amount = int(
                self.quoter_c.functions.quoteExactInputSingle(
                    (token_in, token_out, request.amount, NVDAC_TICK_SPACING, 0)
                ).call()[0]
            )
        sqrt_price = self._read_sqrt_price_x96(NVDAC_POOL)
        pool_spot = _spot_quote(
            sqrt_price, request.amount, request.mode is SwapMode.EXACT_OUTPUT
        )

        _, answer, _, _, _ = self.feed_c.functions.latestRoundData().call()
        price_8 = int(answer)
        oracle = _oracle_quote(
            price_8,
            request.amount,
            NVDAC_DECIMALS,
            request.mode is SwapMode.EXACT_OUTPUT,
        )
        return VenueQuote(amount, pool_spot, oracle)

    def submit(self, _request: SettlementRequest) -> str:
        raise RuntimeHalted(
            "NVDAc settlement submission is owned by the backend redeem boundary"
        )

    def _read_sqrt_price_x96(self, pool_address: str) -> int:
        raw = self.w3.eth.call(
            {"to": Web3.to_checksum_address(pool_address), "data": "0x3850c7bd"}
        )
        data = bytes(raw)
        if len(data) < 32:
            raise RuntimeHalted("NVDAc pool slot0 returned fewer than 32 bytes")
        sqrt_price = int.from_bytes(data[:32], byteorder="big")
        if sqrt_price <= 0 or sqrt_price >= 1 << 160:
            raise RuntimeHalted("NVDAc pool sqrtPriceX96 is invalid")
        return sqrt_price


def _spot_quote(sqrt_price_x96: int, amount: int, is_put: bool) -> int:
    ratio = sqrt_price_x96 * sqrt_price_x96
    if is_put:
        return (amount * Q192 + ratio - 1) // ratio
    return amount * Q192 // ratio


def _oracle_quote(price_8: int, amount: int, decimals: int, is_put: bool) -> int:
    divisor = 10 ** (decimals + 2)
    numerator = amount * price_8
    return (numerator + divisor - 1) // divisor if is_put else numerator // divisor


def make_nvdac_runtime() -> NvdacAllocator | None:
    """Build the isolated runtime from config; returns None while disabled."""
    from src import config

    if not config.ROUTED_SETTLEMENT_ENABLED:
        return None
    if not config.NVDAC_SETTLEMENT_ENABLED:
        return None
    facade = config.PAIR_ROUTING_SWAP_ROUTER_ADDRESS
    adapter = config.NVDAC_SETTLEMENT_ADAPTER_ADDRESS
    if not facade or not adapter:
        raise RuntimeError(
            "NVDAc settlement requires PAIR_ROUTING_SWAP_ROUTER_ADDRESS "
            "and NVDAC_SETTLEMENT_ADAPTER_ADDRESS"
        )
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    gateway = Web3NvdacGateway(
        w3, facade=facade, settler=config.BATCH_SETTLER, adapter=adapter
    )
    return NvdacAllocator(
        gateway,
        expected_facade=facade,
        expected_settler=config.BATCH_SETTLER,
        expected_adapter=adapter,
        routed_enabled=True,
        nvdac_enabled=True,
    )
