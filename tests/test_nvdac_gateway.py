from unittest.mock import MagicMock

from src.nvdac_allocator import (
    AERODROME_FACTORY,
    AERODROME_QUOTER,
    AERODROME_ROUTER,
    NVDAC,
    NVDAC_FEED,
    NVDAC_POOL,
    ORACLE_REGISTRY,
    POLICY_REGISTRY,
    USDC,
    ZERO_ADDRESS,
    SettlementChunk,
    SettlementRequest,
    SwapMode,
    Web3NvdacGateway,
    _oracle_quote,
    _spot_quote,
)

FACADE = "0x1111111111111111111111111111111111111111"
SETTLER = "0x2222222222222222222222222222222222222222"
ADAPTER = "0x3333333333333333333333333333333333333333"
NOW = 2_000_000_000


def _contract(calls):
    mock = MagicMock()
    for name, value in calls.items():
        getattr(mock.functions, name).return_value.call.return_value = value
    return mock


def _w3(**contracts):
    w3 = MagicMock()
    w3.eth.chain_id = 8453

    def contract(address, abi=None):
        return contracts[address.lower()]

    w3.eth.contract.side_effect = contract
    return w3


def _gateway():
    policy_scope = {
        "TRANSFER_SENDER_POLICY": b"\x01" * 32,
        "TRANSFER_RECEIVER_POLICY": b"\x02" * 32,
        "TRANSFER_EXECUTOR_POLICY": b"\x03" * 32,
    }
    contracts = {
        FACADE.lower(): _contract(
            {
                "settler": SETTLER,
                "routeKey": b"\x01" * 32,
                "routes": (ADAPTER, ZERO_ADDRESS, 0),
            }
        ),
        SETTLER.lower(): _contract({"swapRouter": FACADE}),
        ADAPTER.lower(): _contract(
            {
                "facade": FACADE,
                "settler": SETTLER,
                "venueRouter": AERODROME_ROUTER,
                "factory": AERODROME_FACTORY,
                "pool": NVDAC_POOL,
                "tokenA": NVDAC,
                "tokenB": USDC,
                "tickSpacing": 10,
                "effectiveFee": 500,
            }
        ),
        NVDAC_POOL.lower(): _contract(
            {
                "token0": USDC,
                "token1": NVDAC,
                "factory": AERODROME_FACTORY,
                "tickSpacing": 10,
                "fee": 500,
                "liquidity": 100,
            }
        ),
        NVDAC.lower(): _contract(
            {
                "decimals": 8,
                "isPaused": False,
                "multiplier": 10**18,
                "policyId": 5,
                **policy_scope,
            }
        ),
        ORACLE_REGISTRY.lower(): _contract({"getOracleParams": (10**18, False)}),
        POLICY_REGISTRY.lower(): _contract(
            {"policyExists": True, "isAuthorized": True}
        ),
        NVDAC_FEED.lower(): _contract(
            {"latestRoundData": (1, 10**8, NOW - 60, NOW - 60, 1)}
        ),
        AERODROME_QUOTER.lower(): _contract(
            {
                "quoteExactOutputSingle": (100_000_000, 0, 1, 0),
                "quoteExactInputSingle": (99_700_000, 0, 1, 0),
            }
        ),
    }
    w3 = _w3(**contracts)
    w3.eth.call.return_value = (2**96).to_bytes(32, "big")
    return Web3NvdacGateway(w3, facade=FACADE, settler=SETTLER, adapter=ADAPTER)


def test_preflight_assembles_canonical_nvdac_route_tuple():
    state = _gateway().preflight(SettlementChunk(True, 46_182_038))

    assert state.chain_id == 8453
    assert state.route.adapter == ADAPTER.lower()
    assert state.route.pending_adapter == ZERO_ADDRESS
    assert state.route.activate_after == 0
    assert state.token_in.lower() == USDC.lower()
    assert state.token_out.lower() == NVDAC.lower()
    assert state.pool.lower() == NVDAC_POOL.lower()
    assert state.oracle_feed.lower() == NVDAC_FEED.lower()
    assert state.b20_policy_ids == (5, 5, 5)
    assert state.participants_authorized is True


def test_quote_returns_consistent_usdc_denominated_amounts():
    gateway = _gateway()

    put = gateway.quote(
        SettlementRequest(
            SwapMode.EXACT_OUTPUT, USDC, NVDAC, 46_182_038, slippage_limit=0
        )
    )
    call = gateway.quote(
        SettlementRequest(
            SwapMode.EXACT_INPUT, NVDAC, USDC, 46_182_038, slippage_limit=0
        )
    )

    assert put.amount == 100_000_000
    assert put.pool_spot_amount == 46_182_038
    assert put.oracle_amount == 461_821
    assert call.amount == 99_700_000
    assert call.pool_spot_amount == 46_182_038
    assert call.oracle_amount == 461_820


def test_quote_helpers_round_like_the_backend():
    sqrt = 2**96  # price ratio 1.0
    assert _spot_quote(sqrt, 12_345, True) == 12_345
    assert _spot_quote(sqrt, 12_345, False) == 12_345
    assert _oracle_quote(10**8, 12_345_678, 8, True) == 123_457
    assert _oracle_quote(10**8, 12_345_678, 8, False) == 123_456
