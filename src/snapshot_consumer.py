"""One process-wide, fail-closed consumer for atomic Backend chain snapshots."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)

MAX_SNAPSHOT_AGE_SECONDS = 45.0
POST_TRANSACTION_TIMEOUT_SECONDS = 120.0


class SnapshotUnavailable(RuntimeError):
    """No reconciled, fresh atomic snapshot is available for a decision."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SnapshotBundle:
    environment: str
    chain_id: int
    window_id: int
    generation: int
    snapshot_block: int
    snapshot_block_hash: str
    snapshot_block_timestamp: int
    published_at: str
    common: Mapping[str, Any]
    funds: tuple[Mapping[str, Any], ...]
    fetched_monotonic: float
    expires_monotonic: float
    reconciled: bool
    backend_fresh: bool

    def fund(
        self,
        fund_key: str,
        section: str | None = None,
        *,
        expected_address: str | None = None,
    ) -> Mapping[str, Any]:
        """Return one immutable actual-fund state from the atomic envelope."""
        for fund in self.funds:
            if fund.get("fund_type") != fund_key:
                continue
            state = fund.get("state")
            if (
                expected_address is not None
                and str(fund.get("fund_address", "")).lower()
                != expected_address.lower()
            ):
                raise SnapshotUnavailable(
                    f"Snapshot fund address differs from configuration: {fund_key}"
                )
            if not isinstance(state, Mapping):
                break
            if section is None:
                return state
            selected = state.get(section)
            if isinstance(selected, Mapping):
                return selected
            break
        raise SnapshotUnavailable(
            f"Snapshot fund state is absent: {fund_key}/{section or '*'}"
        )

    def market(self, asset: str) -> Mapping[str, Any]:
        market = self.common.get("market")
        if not isinstance(market, Mapping) or market.get("asset") != asset:
            raise SnapshotUnavailable(f"Snapshot common.market is absent: {asset}")
        return market

    def quotes(self) -> tuple[Mapping[str, Any], ...]:
        quotes = self.common.get("quotes")
        if not isinstance(quotes, tuple) or not all(
            isinstance(quote, Mapping) for quote in quotes
        ):
            raise SnapshotUnavailable("Snapshot common.quotes are absent")
        return quotes

    def market_maker(
        self,
        *,
        expected_address: str,
        expected_usdc_address: str,
        expected_allowance_spender: str,
    ) -> Mapping[str, Any]:
        state = self.common.get("market_maker")
        if not isinstance(state, Mapping):
            raise SnapshotUnavailable("Snapshot common.market_maker state is absent")
        expected = {
            "mm_address": expected_address,
            "usdc_address": expected_usdc_address,
            "allowance_spender": expected_allowance_spender,
        }
        for key, value in expected.items():
            if str(state.get(key, "")).lower() != value.lower():
                raise SnapshotUnavailable(
                    f"Snapshot common.market_maker {key} differs from configuration"
                )
        return state


class SnapshotConsumer:
    """Fetch, validate and atomically publish one immutable bundle per chain."""

    def __init__(
        self,
        fetch_envelope: Callable[[], Mapping[str, Any]],
        *,
        environment: str,
        chain_id: int,
        max_age_seconds: float = MAX_SNAPSHOT_AGE_SECONDS,
        poll_interval_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch_envelope = fetch_envelope
        self.environment = environment
        self.chain_id = chain_id
        self.max_age_seconds = max_age_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._bundle: SnapshotBundle | None = None
        self._stop = threading.Event()

    def ingest(self, raw: Mapping[str, Any]) -> bool:
        """Validate and atomically swap a strictly advancing envelope."""
        required = (
            "environment",
            "chain_id",
            "window_id",
            "generation",
            "snapshot_block",
            "snapshot_block_hash",
            "snapshot_block_timestamp",
            "published_at",
            "chain_data_age_seconds",
            "common",
            "funds",
        )
        if any(key not in raw for key in required):
            raise SnapshotUnavailable("Atomic snapshot envelope is incomplete")
        if (
            str(raw["environment"]) != self.environment
            or int(raw["chain_id"]) != self.chain_id
        ):
            raise SnapshotUnavailable(
                "Atomic snapshot envelope targets another environment/chain"
            )
        common = raw["common"]
        funds = raw["funds"]
        if (
            not isinstance(common, Mapping)
            or not isinstance(common.get("market_maker"), Mapping)
            or not isinstance(common.get("market"), Mapping)
            or not isinstance(common.get("quotes"), (list, tuple))
        ):
            raise SnapshotUnavailable("Atomic snapshot common state is incomplete")
        if not isinstance(funds, (list, tuple)) or len(funds) > 3:
            raise SnapshotUnavailable(
                "Atomic snapshot funds must contain at most three funds"
            )
        fund_types = []
        for fund in funds:
            if not isinstance(fund, Mapping):
                raise SnapshotUnavailable("Atomic snapshot fund must be an object")
            fund_type = fund.get("fund_type")
            if fund_type not in {"csp", "covered_call", "meta_wheel"}:
                raise SnapshotUnavailable("Atomic snapshot contains a non-fund entry")
            if (
                not isinstance(fund.get("fund_key"), str)
                or not isinstance(fund.get("fund_address"), str)
                or not isinstance(fund.get("state"), Mapping)
            ):
                raise SnapshotUnavailable("Atomic snapshot fund shape is invalid")
            fund_types.append(fund_type)
        if len(fund_types) != len(set(fund_types)):
            raise SnapshotUnavailable("Atomic snapshot fund type is duplicated")
        try:
            chain_data_age = float(raw["chain_data_age_seconds"])
        except (TypeError, ValueError) as exc:
            raise SnapshotUnavailable("Backend chain-data age is invalid") from exc
        if not math.isfinite(chain_data_age) or chain_data_age < 0:
            raise SnapshotUnavailable("Backend chain-data age is invalid")
        now = self._monotonic()
        remaining_freshness = max(self.max_age_seconds - chain_data_age, 0.0)
        candidate = SnapshotBundle(
            environment=str(raw["environment"]),
            chain_id=int(raw["chain_id"]),
            window_id=int(raw["window_id"]),
            generation=int(raw["generation"]),
            snapshot_block=int(raw["snapshot_block"]),
            snapshot_block_hash=str(raw["snapshot_block_hash"]).lower(),
            snapshot_block_timestamp=int(raw["snapshot_block_timestamp"]),
            published_at=str(raw["published_at"]),
            common=_freeze(common),
            funds=tuple(_freeze(item) for item in funds),
            fetched_monotonic=now,
            expires_monotonic=now + remaining_freshness,
            reconciled=raw.get("reconciled") is True,
            backend_fresh=raw.get("stale") is False,
        )
        if candidate.generation <= 0 or candidate.snapshot_block <= 0:
            raise SnapshotUnavailable("Atomic snapshot generation/block is invalid")
        with self._condition:
            current = self._bundle
            if current is not None and (
                candidate.generation <= current.generation
                or candidate.snapshot_block < current.snapshot_block
                or (
                    candidate.snapshot_block == current.snapshot_block
                    and candidate.snapshot_block_hash != current.snapshot_block_hash
                )
            ):
                return False
            self._bundle = candidate
            self._condition.notify_all()
        return True

    def current(self) -> SnapshotBundle:
        with self._condition:
            bundle = self._bundle
        if bundle is None:
            raise SnapshotUnavailable("Atomic snapshot is absent")
        if not bundle.reconciled:
            raise SnapshotUnavailable("Atomic snapshot is unreconciled")
        if not bundle.backend_fresh:
            raise SnapshotUnavailable("Backend marked atomic snapshot stale")
        if self._monotonic() >= bundle.expires_monotonic:
            raise SnapshotUnavailable("Atomic snapshot expired locally")
        return bundle

    def require(self, bundle: SnapshotBundle) -> None:
        """Revalidate age and generation immediately before an external decision."""
        if self.current() is not bundle:
            raise SnapshotUnavailable(
                "Atomic snapshot generation changed during decision"
            )

    def wait_after_receipt(
        self,
        *,
        pre_send_generation: int,
        receipt_block: int,
        receipt_block_hash: str,
        timeout_seconds: float = POST_TRANSACTION_TIMEOUT_SECONDS,
    ) -> SnapshotBundle:
        """Wait only for normal snapshots that canonically include a receipt."""
        deadline = self._monotonic() + timeout_seconds
        expected_hash = receipt_block_hash.lower()
        with self._condition:
            while True:
                try:
                    bundle = self.current()
                except SnapshotUnavailable:
                    bundle = None
                if (
                    bundle is not None
                    and bundle.generation > pre_send_generation
                    and bundle.snapshot_block >= receipt_block
                    and (
                        bundle.snapshot_block > receipt_block
                        or bundle.snapshot_block_hash == expected_hash
                    )
                ):
                    return bundle
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise SnapshotUnavailable(
                        "Timed out waiting for post-transaction snapshot"
                    )
                self._condition.wait(remaining)

    def run_forever(self) -> None:
        """Transient Backend/DB failures never terminate process orchestration."""
        while not self._stop.is_set():
            try:
                self.ingest(self._fetch_envelope())
            except Exception:
                log.warning("Atomic snapshot fetch failed closed", exc_info=True)
            self._stop.wait(self.poll_interval_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever,
            name=f"snapshot-consumer-{self.chain_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()


def supervise_worker(
    name: str, factory: Callable[[], Any], retry_seconds: float = 2.0
) -> None:
    """Restart a worker object after transient initialization/loop failures."""
    while True:
        try:
            factory().run_forever()
            return
        except Exception:
            log.warning("%s initialization/loop failed; retrying", name, exc_info=True)
            time.sleep(retry_seconds)
