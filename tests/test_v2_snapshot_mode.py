"""B1N-517: V1 quoting stays independent of the opt-in V2 fund snapshot."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call
from urllib.parse import urlsplit

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from src import config, main
from src.capacity import calculate_capacity_internal
from src.position_tracker import PositionTracker
from src.signer import QUOTE_TYPES
from src.snapshot_consumer import SnapshotConsumer

NOW = 1_800_000_000
WORKERS = (
    (main.fund_allocator, "FUND_ALLOCATOR_ENABLED"),
    (main.fund_operations_keeper, "FUND_OPERATIONS_KEEPER_ENABLED"),
    (main.covered_call_allocator, "COVERED_CALL_ALLOCATOR_ENABLED"),
    (main.covered_call_operations_keeper, "COVERED_CALL_OPERATIONS_KEEPER_ENABLED"),
    (main.meta_wheel_allocator, "META_WHEEL_ALLOCATOR_ENABLED"),
)


@pytest.fixture
def runtime(monkeypatch):
    asset = config.AssetConfig("eth", "ETH", leverage=3, max_exposure=0.5)
    for name, value in {
        "V2_SNAPSHOT_ENABLED": False,
        "SOLANA_QUOTE_PUBLISHING_ENABLED": False,
        "ASSETS": [asset],
        "ASSET_MAP": {"eth": asset},
        "CHAINS": [config.ChainConfig("base", (asset,))],
        "HEDGE_MODE": "live",
        "CAPACITY_PREMIUM_RATIO": 0.03,
        "CAPACITY_RESERVE_RATIO": 0.25,
        "CAPACITY_AVG_DELTA": 0.3,
        "MAX_AMOUNT": 200_000_000,
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(main.time, "time", lambda: NOW)
    market = {
        "spot": 2_000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0x0000000000000000000000000000000000000002",
                "strike_price": 1_900.0,
                "expiry": NOW + 7 * 86400,
                "is_put": True,
            }
        ],
    }
    tracker = PositionTracker()
    tracker.cache_otokens(market["available_otokens"], underlying="eth", chain="base")
    monkeypatch.setattr(main, "_tracker", tracker)
    monkeypatch.setattr(main, "_seen_tx_hashes", set())
    monkeypatch.setattr(main, "_market", {"base/eth": main.MarketSnapshot(2_000, 0.6)})
    monkeypatch.setattr(main, "_spot_history", {})
    for name in (
        "log_position_opened",
        "log_delta_rebalanced",
        "log_capacity_snapshot",
    ):
        monkeypatch.setattr(main.trade_logger, name, Mock())
    for name, value in {
        "is_hedge_ready": True,
        "get_account_value": 6_000.0,
        "get_withdrawable": 5_000.0,
        "get_positions": [],
        "open_hedge": {"size": 0.1, "avg_price": 2_000.0},
    }.items():
        monkeypatch.setattr(main.hedge_executor, name, Mock(return_value=value))

    responses = {
        "/mm/market": market,
        "/mm/fills": [],
        "/mm/exposure": {"total_premium_earned": "0"},
    }
    session = MagicMock()
    session.get.side_effect = lambda url, **_: Mock(
        json=Mock(return_value=responses[urlsplit(url).path])
    )
    session.delete.return_value.json.return_value = {}
    session.post.return_value.json.return_value = {"accepted": 1, "rejected": 0}
    monkeypatch.setattr(main.api_client, "_SESSION", session)

    def read_usdc(tx):
        balances = {"0x70a08231": 1_000 * 10**6, "0xdd62ed3e": 300 * 10**6}
        return balances[tx["data"][:10]].to_bytes(32, "big")

    w3 = MagicMock()
    w3.eth.call.side_effect = read_usdc
    w3.eth.contract.return_value.functions.makerNonce.return_value.call.return_value = 7
    return SimpleNamespace(
        asset=asset,
        market=market,
        tracker=tracker,
        responses=responses,
        session=session,
        w3=w3,
        address=Account.from_key(config.MM_PRIVATE_KEY).address,
        domain=main.build_domain(config.CHAIN_ID, config.BATCH_SETTLER),
    )


def _published_quotes(runtime):
    return [
        payload
        for request in runtime.session.post.call_args_list
        if urlsplit(request.args[0]).path == "/mm/quotes"
        for payload in request.kwargs["json"]["quotes"]
    ]


def test_v1_cycle_uses_legacy_market_capacity_nonce_and_real_quote_signature(runtime):
    main.run_cycle(runtime.w3, runtime.domain, runtime.address)

    runtime.session.delete.assert_called_once_with(
        main.api_client._url("/mm/quotes"), params={"chain": "base"}, timeout=15
    )
    assert [
        urlsplit(request.args[0]).path for request in runtime.session.get.call_args_list
    ] == ["/mm/fills", "/mm/exposure", "/mm/market"]
    runtime.session.get.assert_any_call(
        main.api_client._url("/mm/market"), params={"asset": "eth"}, timeout=15
    )
    owner = runtime.address[2:].lower().zfill(64)
    spender = config.MARGIN_POOL_ADDRESS[2:].lower().zfill(64)
    assert runtime.w3.eth.call.call_args_list == [
        call({"to": config.USDC_ADDRESS, "data": "0x70a08231" + owner}),
        call({"to": config.USDC_ADDRESS, "data": "0xdd62ed3e" + owner + spender}),
    ]
    nonce_reader = runtime.w3.eth.contract.return_value.functions.makerNonce
    nonce_reader.assert_called_once_with(runtime.address)
    nonce_reader.return_value.call.assert_called_once_with()
    report = runtime.session.post.call_args_list[0].kwargs["json"]
    assert report["premium_pool_usd"] == 300
    assert report["hedge_pool_withdrawable_usd"] == 5_000
    (quote,) = _published_quotes(runtime)
    assert quote["maker_nonce"] == 7
    assert quote["max_amount"] == 200_000_000
    assert quote["chain"] == "base"
    message = encode_typed_data(
        domain_data=runtime.domain,
        message_types=QUOTE_TYPES,
        message_data={
            "oToken": quote["otoken_address"],
            "bidPrice": quote["bid_price"],
            "deadline": quote["deadline"],
            "quoteId": quote["quote_id"],
            "maxAmount": quote["max_amount"],
            "makerNonce": quote["maker_nonce"],
        },
    )
    assert (
        Account.recover_message(message, signature=quote["signature"])
        == runtime.address
    )


@pytest.mark.parametrize("source", ("rest", "websocket"))
def test_v1_fills_use_cached_market_and_retry_hedging_without_duplicate_positions(
    runtime, source
):
    fill = {
        "tx_hash": "0xfill",
        "otoken_address": runtime.market["available_otokens"][0]["address"],
        "amount": 100_000_000,
        "gross_premium": 1_000_000,
    }
    runtime.responses["/mm/fills"] = [fill]
    dispatch = (
        main._poll_fills_rest if source == "rest" else lambda: main._handle_fill(fill)
    )
    mkt = main._get_market("eth")
    mkt.iv = 0
    dispatch()
    assert runtime.tracker.positions == []
    assert main._seen_tx_hashes == set()

    mkt.iv = 0.6
    hedge = main.hedge_executor.open_hedge
    hedge.side_effect = [
        RuntimeError("venue unavailable"),
        {"size": 0.1, "avg_price": 2_000},
        {"size": 0.1, "avg_price": 2_000},
    ]
    dispatch()
    assert len(runtime.tracker.positions) == 1
    assert main._seen_tx_hashes == set()
    dispatch()
    dispatch()
    assert len(runtime.tracker.positions) == 1
    assert hedge.call_count == 2
    assert hedge.call_args.args[0] == "ETH"
    assert main._seen_tx_hashes == {"0xfill"}
    main.trade_logger.log_position_opened.assert_called_once()

    # Subsequent quote refreshes still recalculate and rebalance the V1 portfolio.
    main.run_cycle(runtime.w3, runtime.domain, runtime.address)
    assert _published_quotes(runtime)
    assert hedge.call_count == 3
    assert all(
        urlsplit(request.args[0]).path != "/mm/snapshot"
        for request in runtime.session.get.call_args_list
    )


@pytest.mark.parametrize("failure", ("market", "capacity", "nonce"))
def test_v1_read_failures_do_not_publish_quotes(runtime, failure):
    error = RuntimeError("V1 dependency unavailable")
    if failure == "market":
        del runtime.responses["/mm/market"]
    elif failure == "capacity":
        runtime.w3.eth.call.side_effect = error
    else:
        runtime.w3.eth.contract.return_value.functions.makerNonce.return_value.call.side_effect = error

    main.run_cycle(runtime.w3, runtime.domain, runtime.address)

    assert _published_quotes(runtime) == []


@pytest.mark.parametrize("enabled", (False, True))
def test_main_constructs_polls_and_starts_v2_only_when_explicitly_enabled(
    runtime, monkeypatch, enabled
):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", enabled)
    constructor = Mock()
    consumer = constructor.return_value
    monkeypatch.setattr(main, "SnapshotConsumer", constructor)
    snapshot_fetch = Mock()
    monkeypatch.setattr(main.api_client, "get_snapshot_envelope", snapshot_fetch)
    monkeypatch.setattr(main, "Web3", Mock(return_value=runtime.w3))
    monkeypatch.setattr(main.hedge_executor, "init", Mock())
    monkeypatch.setattr(main, "recover_positions", Mock(return_value=0))
    monkeypatch.setattr(main.fill_listener, "set_on_fill", Mock())
    monkeypatch.setattr(main.fill_listener, "start", Mock())
    monkeypatch.setattr(main, "_init_solana", Mock())
    starts = []
    for module, flag in WORKERS:
        monkeypatch.setattr(config, flag, True)
        start = Mock()
        monkeypatch.setattr(module, "start", start)
        starts.append(start)
    run = Mock()
    monkeypatch.setattr(main, "run_cycle", run)
    monkeypatch.setattr(main.time, "sleep", Mock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        main.main()

    main._init_solana.assert_not_called()
    main.fill_listener.start.assert_called_once_with()
    if enabled:
        constructor.assert_called_once()
        consumer.start.assert_called_once_with()
        constructor.call_args.args[0]()
        snapshot_fetch.assert_called_once_with(
            environment=config.SNAPSHOT_ENVIRONMENT, chain_id=config.CHAIN_ID
        )
        consumer.current.assert_called_once_with()
        run.assert_called_once_with(
            runtime.w3,
            runtime.domain,
            runtime.address,
            consumer.current.return_value,
            consumer,
        )
        for start in starts:
            start.assert_called_once_with(consumer, runtime.w3)
    else:
        constructor.assert_not_called()
        consumer.start.assert_not_called()
        consumer.current.assert_not_called()
        snapshot_fetch.assert_not_called()
        run.assert_called_once_with(
            runtime.w3, runtime.domain, runtime.address, None, None
        )
        for start in starts:
            start.assert_not_called()


@pytest.mark.parametrize("module,flag", WORKERS, ids=[flag for _, flag in WORKERS])
@pytest.mark.parametrize(
    "snapshot_enabled,worker_enabled", ((False, True), (True, False), (True, True))
)
def test_worker_entrypoints_require_both_gates(
    monkeypatch, module, flag, snapshot_enabled, worker_enabled
):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", snapshot_enabled)
    monkeypatch.setattr(config, flag, worker_enabled)
    thread = Mock()
    monkeypatch.setattr(module.threading, "Thread", thread)
    monkeypatch.setattr("src.meta_wheel_chain.load_runtime_gate_and_signers", Mock())
    monkeypatch.setattr(
        main.meta_wheel_allocator, "persistent_action_journal_path", Mock()
    )

    result = module.start(Mock(), Mock())

    if snapshot_enabled and worker_enabled:
        assert result is thread.return_value
        result.start.assert_called_once_with()
    else:
        assert result is None
        thread.assert_not_called()


@pytest.mark.parametrize("state", ("fresh", "absent", "expired", "unreconciled"))
def test_v2_mode_never_falls_back_to_v1_reads(runtime, monkeypatch, state):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", True)
    raw = json.loads(
        (Path(__file__).parent / "fixtures/rpc_snapshot_envelope.json").read_text()
    )
    raw["common"]["market"] = runtime.market | {"asset": "eth"}
    raw["common"]["market_maker"].update(
        mm_address=runtime.address,
        usdc_address=config.USDC_ADDRESS,
        allowance_spender=config.MARGIN_POOL_ADDRESS,
        maker_nonce=17,
        usdc_balance_raw=900 * 10**6,
        usdc_allowance_raw=120 * 10**6,
    )
    raw["reconciled"] = state != "unreconciled"
    clock = [0.0]
    consumer = SnapshotConsumer(
        lambda: raw,
        environment=raw["environment"],
        chain_id=raw["chain_id"],
        monotonic=lambda: clock[0],
    )
    consumer.ingest(raw)
    # Hold the same decision bundle even after its local freshness expires.
    bundle = consumer._bundle
    if state == "expired":
        clock[0] = 100.0
    if state == "absent":
        bundle = None
    runtime.w3.eth.call.side_effect = AssertionError("V2 used V1 capacity RPC")
    runtime.w3.eth.contract.side_effect = AssertionError("V2 used V1 nonce RPC")
    del runtime.responses["/mm/market"]

    if state == "fresh":
        main.run_cycle(runtime.w3, runtime.domain, runtime.address, bundle, consumer)
        (quote,) = _published_quotes(runtime)
        assert quote["maker_nonce"] == 17
        assert quote["max_amount"] == 100_000_000
    else:
        with pytest.raises(RuntimeError, match="snapshot"):
            main.run_cycle(
                runtime.w3, runtime.domain, runtime.address, bundle, consumer
            )
        assert _published_quotes(runtime) == []
        runtime.session.delete.assert_not_called()
        runtime.session.get.assert_not_called()
    runtime.w3.eth.call.assert_not_called()
    runtime.w3.eth.contract.assert_not_called()


@pytest.mark.parametrize("snapshot", (None, {}, {"usdc_balance_raw": 100 * 10**6}))
def test_v2_missing_capacity_never_uses_rpc_fallback(runtime, monkeypatch, snapshot):
    monkeypatch.setattr(config, "V2_SNAPSHOT_ENABLED", True)
    with pytest.raises(RuntimeError, match="capacity state"):
        calculate_capacity_internal(
            runtime.w3,
            2_000,
            runtime.address,
            runtime.tracker,
            asset_config=runtime.asset,
            base_snapshot=snapshot,
        )
    runtime.w3.eth.call.assert_not_called()
