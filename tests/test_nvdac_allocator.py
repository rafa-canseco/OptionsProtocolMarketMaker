from dataclasses import replace

import pytest

from src.nvdac_allocator import (
    AERODROME_FACTORY,
    AERODROME_ROUTER,
    BASE_CHAIN_ID,
    NVDAC,
    NVDAC_B20_POLICY,
    NVDAC_DECIMALS,
    NVDAC_FEED,
    NVDAC_FEE,
    NVDAC_POOL,
    NVDAC_TICK_SPACING,
    USDC,
    ZERO_ADDRESS,
    NvdacAllocator,
    Preflight,
    RouteTuple,
    RuntimeHalted,
    SettlementChunk,
    SwapMode,
    VenueQuote,
)

FACADE = "0x1111111111111111111111111111111111111111"
SETTLER = "0x2222222222222222222222222222222222222222"
ADAPTER = "0x3333333333333333333333333333333333333333"
OTHER = "0x4444444444444444444444444444444444444444"
NOW = 2_000_000_000


def _preflight(*, is_put=True, **changes):
    state = Preflight(
        chain_id=BASE_CHAIN_ID,
        token_in=USDC if is_put else NVDAC,
        token_out=NVDAC if is_put else USDC,
        route=RouteTuple(ADAPTER, ZERO_ADDRESS, 0),
        facade=FACADE,
        settler=SETTLER,
        adapter_facade=FACADE,
        adapter_settler=SETTLER,
        venue_router=AERODROME_ROUTER,
        factory=AERODROME_FACTORY,
        pool=NVDAC_POOL,
        tick_spacing=NVDAC_TICK_SPACING,
        fee=NVDAC_FEE,
        pool_liquidity=1,
        token_decimals=NVDAC_DECIMALS,
        b20_paused=False,
        b20_multiplier=10**18,
        registry_multiplier=10**18,
        registry_paused=False,
        b20_policy_ids=(NVDAC_B20_POLICY,) * 3,
        participants_authorized=True,
        oracle_feed=NVDAC_FEED,
        oracle_updated_at=NOW - 60,
    )
    return replace(state, **changes)


class Gateway:
    def __init__(self, states, quote=VenueQuote(100_000_000, 100_000_000, 100_000_000)):
        self.states = list(states)
        self.venue_quote = quote
        self.quote_requests = []
        self.submissions = []

    def preflight(self, _chunk):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def quote(self, request):
        self.quote_requests.append(request)
        return self.venue_quote

    def submit(self, request):
        self.submissions.append(request)
        return f"0x{len(self.submissions):064x}"


def _runtime(gateway, **kwargs):
    return NvdacAllocator(
        gateway,
        expected_facade=FACADE,
        expected_settler=SETTLER,
        expected_adapter=ADAPTER,
        routed_enabled=True,
        nvdac_enabled=True,
        **kwargs,
    )


def _settle(chunks=None, states=None, quote=None, clock=None, **kwargs):
    gateway = Gateway(
        states
        or [_preflight(is_put=c.is_put) for c in (chunks or [SettlementChunk(True, 1)])]
        * 2,
        quote=quote or VenueQuote(100_000_000, 100_000_000, 100_000_000),
    )
    chunks = chunks or [SettlementChunk(True, 1)]
    return _runtime(gateway, **kwargs).settle(chunks, clock=clock)


def test_success_preserves_put_exact_output_call_exact_input_and_30_bps_limits():
    gateway = Gateway(
        [
            _preflight(is_put=True),
            _preflight(is_put=True),
            _preflight(is_put=False),
            _preflight(is_put=False),
        ]
    )

    hashes = _runtime(gateway).settle(
        [SettlementChunk(True, 46_182_038), SettlementChunk(False, 46_182_038)],
        clock=lambda: NOW,
    )

    assert len(hashes) == 2
    put, call = gateway.submissions
    assert (put.mode, put.token_in, put.token_out, put.slippage_limit) == (
        SwapMode.EXACT_OUTPUT,
        USDC,
        NVDAC,
        100_300_000,
    )
    assert (call.mode, call.token_in, call.token_out, call.slippage_limit) == (
        SwapMode.EXACT_INPUT,
        NVDAC,
        USDC,
        99_700_000,
    )
    assert not hasattr(put, "deadline")
    assert not hasattr(put, "route_version")


def test_each_activation_gate_independently_defaults_disabled():
    gateway = Gateway([_preflight()])
    with pytest.raises(RuntimeHalted, match="routed settlement is disabled"):
        NvdacAllocator(
            gateway,
            expected_facade=FACADE,
            expected_settler=SETTLER,
            expected_adapter=ADAPTER,
            nvdac_enabled=True,
        ).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    with pytest.raises(RuntimeHalted, match="NVDAc settlement is disabled"):
        NvdacAllocator(
            gateway,
            expected_facade=FACADE,
            expected_settler=SETTLER,
            expected_adapter=ADAPTER,
            routed_enabled=True,
        ).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.submissions == []


def test_paused_runtime_submits_nothing():
    gateway = Gateway([_preflight(b20_paused=True)])
    with pytest.raises(RuntimeHalted, match="B20 pause"):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.submissions == []


@pytest.mark.parametrize(
    "change,message",
    [
        ({"route": RouteTuple(OTHER, ZERO_ADDRESS, 0)}, "route adapter"),
        ({"route": RouteTuple(ADAPTER, OTHER, 0)}, "route availability"),
        ({"route": RouteTuple(ADAPTER, ZERO_ADDRESS, 1)}, "route availability"),
        ({"chain_id": 1}, "chain"),
        ({"token_in": NVDAC}, "ordered token pair"),
        ({"facade": OTHER}, "facade"),
        ({"settler": OTHER}, "settler"),
        ({"venue_router": OTHER}, "venue router"),
        ({"factory": OTHER}, "factory"),
        ({"pool": OTHER}, "pool"),
        ({"tick_spacing": 1}, "pool parameters"),
        ({"fee": 1}, "pool parameters"),
        ({"pool_liquidity": 0}, "pool liquidity"),
        ({"token_decimals": 6}, "B20 decimals"),
        ({"registry_paused": True}, "B20 pause"),
        ({"b20_multiplier": 1}, "B20 multiplier"),
        ({"registry_multiplier": 1}, "B20 multiplier"),
        ({"b20_policy_ids": (6, 6, 6)}, "B20 policy"),
        ({"participants_authorized": False}, "B20 policy"),
        ({"oracle_feed": OTHER}, "oracle feed"),
        ({"oracle_updated_at": NOW - 3601}, "oracle freshness"),
    ],
)
def test_preflight_gate_fails_closed(change, message):
    gateway = Gateway([_preflight(**change)])
    with pytest.raises(RuntimeHalted, match=message):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.quote_requests == []
    assert gateway.submissions == []


def test_oracle_freshness_is_inclusive_at_one_hour():
    gateway = Gateway([_preflight(oracle_updated_at=NOW - 3600)] * 2)
    _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.submissions


def test_insufficient_liquidity_stops_settlement():
    gateway = Gateway([_preflight(pool_liquidity=0)])
    with pytest.raises(RuntimeHalted, match="pool liquidity"):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.quote_requests == []
    assert gateway.submissions == []


def test_changed_complete_route_tuple_requires_fresh_quote():
    changed = RouteTuple(OTHER, ZERO_ADDRESS, 0)
    gateway = Gateway([_preflight(), _preflight(route=changed)])
    with pytest.raises(RuntimeHalted, match="fresh quote required"):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert len(gateway.quote_requests) == 1
    assert gateway.submissions == []


def test_pool_impact_boundary_is_inclusive_at_30_bps():
    inside = VenueQuote(100_300, 100_000, 100_000)
    gateway = Gateway([_preflight()] * 2, quote=inside)
    _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.submissions

    outside = VenueQuote(100_301, 100_000, 100_000)
    gateway = Gateway([_preflight()], quote=outside)
    with pytest.raises(RuntimeHalted, match="pool impact"):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)


def test_oracle_deviation_boundary_is_inclusive_at_100_bps():
    inside = VenueQuote(101_000, 101_000, 100_000)
    gateway = Gateway([_preflight()] * 2, quote=inside)
    _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)
    assert gateway.submissions

    outside = VenueQuote(101_001, 101_001, 100_000)
    gateway = Gateway([_preflight()], quote=outside)
    with pytest.raises(RuntimeHalted, match="oracle deviation"):
        _runtime(gateway).settle([SettlementChunk(True, 1)], clock=lambda: NOW)


def test_failure_stops_bounded_batch_without_eth_cbbtc_or_venue_fallback():
    alerts = []
    gateway = Gateway([_preflight()], quote=VenueQuote(100_301, 100_000, 100_000))
    runtime = _runtime(gateway, max_chunks=2, alert=alerts.append)

    with pytest.raises(RuntimeHalted, match="pool impact"):
        runtime.settle(
            [SettlementChunk(True, 1), SettlementChunk(False, 1)], clock=lambda: NOW
        )

    assert gateway.submissions == []
    assert len(gateway.quote_requests) == 1
    assert alerts == ["NVDAc pool impact exceeds 30 bps"]
    with pytest.raises(RuntimeHalted, match="chunk count"):
        runtime.settle([SettlementChunk(True, 1)] * 3, clock=lambda: NOW)
