from __future__ import annotations

import threading
import time

import pytest

from src.snapshot_consumer import SnapshotConsumer, SnapshotUnavailable


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def envelope(
    generation: int = 7,
    block: int = 100,
    block_hash: str = "0xabc",
    *,
    age: float = 1,
    stale: bool = False,
    reconciled: bool = True,
):
    return {
        "environment": "staging",
        "chain_id": 84532,
        "window_id": generation,
        "generation": generation,
        "snapshot_block": block,
        "snapshot_block_hash": block_hash,
        "snapshot_block_timestamp": 1_700_000_000,
        "published_at": "2026-08-22T00:00:00Z",
        "chain_data_age_seconds": age,
        "stale": stale,
        "reconciled": reconciled,
        "common": {
            "market": {
                "asset": "eth",
                "spot": 2000.0,
                "iv": 0.5,
                "iv_source": "test",
                "observed_at": 1700000000,
                "protocol_fee_bps": 1000,
                "available_otokens": [],
            },
            "quotes": [],
            "market_maker": {
                "mm_address": "0x111",
                "usdc_address": "0x222",
                "allowance_spender": "0x333",
                "usdc_balance_raw": 1_000_000,
                "usdc_allowance_raw": 1_000_000,
                "maker_nonce": 9,
            },
        },
        "funds": [
            {
                "fund_key": "csp",
                "fund_type": "csp",
                "fund_address": "0xc5p",
                "state": {"allocator": {"value": 1}},
            },
            {
                "fund_key": "covered_call",
                "fund_type": "covered_call",
                "fund_address": "0xca11",
                "state": {"allocator": {"value": 2}},
            },
            {
                "fund_key": "meta_wheel",
                "fund_type": "meta_wheel",
                "fund_address": "0xwheel",
                "state": {"allocator": {"value": 3}},
            },
        ],
    }


def consumer(clock: Clock) -> SnapshotConsumer:
    return SnapshotConsumer(
        lambda: envelope(),
        environment="staging",
        chain_id=84532,
        monotonic=clock,
    )


def test_all_recurrent_workers_share_one_immutable_generation_without_provider_reads():
    clock = Clock()
    fetches = 0

    def fetch():
        nonlocal fetches
        fetches += 1
        return envelope()

    snapshots = SnapshotConsumer(
        fetch, environment="staging", chain_id=84532, monotonic=clock
    )
    snapshots.ingest(fetch())
    observed = [snapshots.current() for _ in range(6)]

    assert fetches == 1
    assert len({id(item) for item in observed}) == 1
    assert {item.generation for item in observed} == {7}
    with pytest.raises(TypeError):
        observed[0].common["market_maker"]["maker_nonce"] = 10


def test_fund_and_common_state_are_bound_to_configured_addresses():
    snapshots = consumer(Clock())
    snapshots.ingest(envelope())
    bundle = snapshots.current()
    with pytest.raises(SnapshotUnavailable, match="differs from configuration"):
        bundle.fund("csp", expected_address="0xdef")
    with pytest.raises(SnapshotUnavailable, match="differs from configuration"):
        bundle.market_maker(
            expected_address="0xwrong",
            expected_usdc_address="0x222",
            expected_allowance_spender="0x333",
        )


def test_envelope_rejects_pseudo_or_more_than_three_funds():
    snapshots = consumer(Clock())
    raw = envelope()
    raw["funds"].append(
        {
            "fund_key": "market_maker",
            "fund_type": "market_maker",
            "fund_address": "0x111",
            "state": {},
        }
    )
    with pytest.raises(SnapshotUnavailable, match="at most three"):
        snapshots.ingest(raw)

    raw = envelope()
    raw["funds"][0]["fund_type"] = "market_maker"
    with pytest.raises(SnapshotUnavailable, match="non-fund"):
        snapshots.ingest(raw)


def test_fund_lookup_uses_only_fund_type_and_rejects_flattened_state():
    snapshots = consumer(Clock())
    raw = envelope()
    raw["funds"][0]["fund_key"] = "covered_call"
    snapshots.ingest(raw)
    assert snapshots.current().fund("csp", "allocator")["value"] == 1
    with pytest.raises(SnapshotUnavailable, match="absent"):
        snapshots.current().fund("covered_call-key-alias")

    raw = envelope(generation=8)
    raw["funds"][0].pop("state")
    raw["funds"][0]["allocator"] = {"value": 1}
    with pytest.raises(SnapshotUnavailable, match="shape is invalid"):
        snapshots.ingest(raw)


def test_absent_unreconciled_stale_and_db_outage_fail_closed_after_expiry():
    clock = Clock()
    snapshots = consumer(clock)
    with pytest.raises(SnapshotUnavailable, match="absent"):
        snapshots.current()

    snapshots.ingest(envelope(reconciled=False))
    with pytest.raises(SnapshotUnavailable, match="unreconciled"):
        snapshots.current()

    snapshots = consumer(clock)
    snapshots.ingest(envelope(stale=True))
    with pytest.raises(SnapshotUnavailable, match="marked.*stale"):
        snapshots.current()

    snapshots = consumer(clock)
    snapshots.ingest(envelope(age=5))
    clock.now += 39.9
    assert snapshots.current().generation == 7
    # A Backend/DB outage leaves the old bundle in memory but never extends it.
    clock.now += 0.1
    with pytest.raises(SnapshotUnavailable, match="expired"):
        snapshots.current()


@pytest.mark.parametrize("age", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_backend_age_is_rejected(age):
    snapshots = consumer(Clock())
    with pytest.raises(SnapshotUnavailable, match="age is invalid"):
        snapshots.ingest(envelope(age=age))


def test_backend_age_limits_local_monotonic_expiry_to_residual_freshness():
    clock = Clock()
    snapshots = consumer(clock)
    snapshots.ingest(envelope(age=44))
    clock.now += 0.99
    assert snapshots.current().generation == 7
    clock.now += 0.01
    with pytest.raises(SnapshotUnavailable, match="expired"):
        snapshots.current()


def test_decision_recheck_rejects_expiry_or_generation_change():
    clock = Clock()
    snapshots = consumer(clock)
    snapshots.ingest(envelope(generation=7, age=44))
    decision = snapshots.current()
    clock.now += 1
    with pytest.raises(SnapshotUnavailable, match="expired"):
        snapshots.require(decision)

    clock.now = 200
    snapshots = consumer(clock)
    snapshots.ingest(envelope(generation=7))
    decision = snapshots.current()
    snapshots.ingest(envelope(generation=8, block=101, block_hash="0xbbb"))
    with pytest.raises(SnapshotUnavailable, match="generation changed"):
        snapshots.require(decision)


def test_generation_block_and_hash_never_regress():
    clock = Clock()
    snapshots = consumer(clock)
    assert snapshots.ingest(envelope(generation=7, block=100, block_hash="0xaaa"))
    assert not snapshots.ingest(envelope(generation=7, block=101, block_hash="0xbbb"))
    assert not snapshots.ingest(envelope(generation=8, block=99, block_hash="0xccc"))
    assert not snapshots.ingest(envelope(generation=8, block=100, block_hash="0xddd"))
    assert snapshots.ingest(envelope(generation=8, block=100, block_hash="0xaaa"))


def test_post_transaction_gate_requires_new_generation_block_and_equal_height_hash():
    clock = Clock()
    snapshots = consumer(clock)
    snapshots.ingest(envelope(generation=7, block=100, block_hash="0xaaa"))

    snapshots.ingest(envelope(generation=8, block=101, block_hash="0xbbb"))
    result = snapshots.wait_after_receipt(
        pre_send_generation=7,
        receipt_block=101,
        receipt_block_hash="0xbbb",
        timeout_seconds=0,
    )
    assert result.generation == 8

    snapshots = consumer(clock)
    snapshots.ingest(envelope(generation=8, block=101, block_hash="0xwrong"))
    with pytest.raises(SnapshotUnavailable, match="Timed out"):
        snapshots.wait_after_receipt(
            pre_send_generation=7,
            receipt_block=101,
            receipt_block_hash="0xbbb",
            timeout_seconds=0,
        )
    snapshots.ingest(envelope(generation=9, block=102, block_hash="0xlater"))
    assert (
        snapshots.wait_after_receipt(
            pre_send_generation=7,
            receipt_block=101,
            receipt_block_hash="0xbbb",
            timeout_seconds=0,
        ).generation
        == 9
    )


def test_transient_initial_fetch_error_does_not_terminate_consumer():
    attempts = 0
    published = threading.Event()

    def fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary DB timeout")
        published.set()
        return envelope()

    snapshots = SnapshotConsumer(
        fetch,
        environment="staging",
        chain_id=84532,
        poll_interval_seconds=0.01,
    )
    thread = snapshots.start()
    try:
        assert published.wait(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                assert snapshots.current().generation == 7
                break
            except SnapshotUnavailable:
                time.sleep(0.01)
        else:
            pytest.fail("consumer did not recover from transient initialization error")
        assert thread.is_alive()
    finally:
        snapshots.stop()
        thread.join(1)
