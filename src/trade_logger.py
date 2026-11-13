"""Append-only trade event logger (JSONL file + Supabase)."""

import json
import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime
from typing import Any

from src import config

log = logging.getLogger(__name__)

_supabase_client: Any = None
_supabase_missing_columns: set[str] = set()
_hedge_events: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
_hedge_upserts: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
_hedge_event_lock = threading.Lock()
_hedge_replay_interval = 60
_hedge_writer_started = False
_hedge_replay_started = False
_hedge_upserted: dict[str, str] = {}
_hedge_queued: set[tuple[str, str]] = set()
_SAFE_HEDGE_ERRORS = {
    "missing_order_id",
    "order_fills_not_visible",
    "order_fills_incomplete",
    "order_not_terminal",
    "venue_order_rejected",
    "venue_order_canceled",
    "live_hedge_order_failed",
}


def _get_supabase() -> Any:
    """Lazy-init Supabase client."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        return None
    try:
        from supabase import create_client

        _supabase_client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        log.info("Supabase client initialized for trade logging")
        return _supabase_client
    except Exception:
        log.warning("Failed to init Supabase client", exc_info=True)
        return None


def _write_jsonl(event: dict[str, Any], *, raise_on_error: bool = False) -> None:
    """Append one JSON line to the local durability log."""
    path = config.TRADE_LOG_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        if raise_on_error:
            raise
        log.warning("Failed to write JSONL event", exc_info=True)


_SUPABASE_KEY_MAP = {
    "amount": "amount_eth",
    "hedge_size": "hedge_size_eth",
}


def _map_supabase_event(event: dict[str, Any]) -> dict[str, Any]:
    mapped = {_SUPABASE_KEY_MAP.get(k, k): v for k, v in event.items()}
    for column in _supabase_missing_columns:
        mapped.pop(column, None)
    return mapped


def _is_unknown_column_error(exc: Exception, column: str) -> bool:
    message = str(exc).lower()
    return (
        column.lower() in message
        and "column" in message
        and ("does not exist" in message or "could not find" in message)
    )


def _write_supabase(event: dict[str, Any]) -> None:
    """Insert event row into mm_trade_history table."""
    client = _get_supabase()
    if not client:
        return
    mapped = _map_supabase_event(event)
    try:
        client.table("mm_trade_history").insert(mapped).execute()
    except Exception as exc:
        if "chain" in mapped and _is_unknown_column_error(exc, "chain"):
            legacy = dict(mapped)
            legacy.pop("chain", None)
            try:
                client.table("mm_trade_history").insert(legacy).execute()
                _supabase_missing_columns.add("chain")
                log.warning(
                    "Supabase mm_trade_history missing chain column;"
                    " wrote legacy row without chain"
                )
                return
            except Exception:
                log.warning("Failed to write legacy event to Supabase", exc_info=True)
                return
        log.warning("Failed to write event to Supabase", exc_info=True)


def _emit(event: dict[str, Any], *, require_local: bool = False) -> None:
    """Write event to all sinks; optionally require the local durability log."""
    _write_jsonl(event, raise_on_error=require_local)
    _write_supabase(event)


def log_position_opened(
    otoken: str,
    strike: float,
    expiry: int,
    is_put: bool,
    amount: float,
    premium_usd: float,
    user_address: str,
    tx_hash: str,
    spot: float,
    delta: float,
    hedge_action: str,
    hedge_size: float,
    hedge_fill_price: float,
    underlying: str = "eth",
    chain: str = "base",
) -> None:
    _emit(
        {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": otoken,
            "chain": chain,
            "underlying": underlying,
            "strike": strike,
            "expiry": expiry,
            "is_put": is_put,
            "amount": amount,
            "premium_usd": premium_usd,
            "user_address": user_address,
            "tx_hash": tx_hash,
            "spot": spot,
            "delta": delta,
            "hedge_action": hedge_action,
            "hedge_size": hedge_size,
            "hedge_fill_price": hedge_fill_price,
        },
        require_local=True,
    )
    log.info(
        "[TRADE LOG] position_opened otoken=%s strike=%.0f underlying=%s",
        otoken[:10],
        strike,
        underlying,
    )


def log_delta_rebalanced(
    otoken: str,
    old_delta: float,
    new_delta: float,
    old_hedge: float,
    new_hedge: float,
    hedge_fill_price: float,
    underlying: str = "eth",
    chain: str = "base",
) -> None:
    _emit(
        {
            "event": "delta_rebalanced",
            "ts": int(time.time()),
            "otoken": otoken,
            "chain": chain,
            "underlying": underlying,
            "old_delta": round(old_delta, 6),
            "new_delta": round(new_delta, 6),
            "old_hedge": round(old_hedge, 6),
            "new_hedge": round(new_hedge, 6),
            "hedge_fill_price": hedge_fill_price,
        }
    )


def log_position_expired(
    otoken: str,
    settlement: str,
    expiry_price: float,
    settlement_pnl: float,
    hedge_pnl: float,
    hedge_close_price: float,
    net_pnl: float,
    underlying: str = "eth",
    chain: str = "base",
) -> None:
    _emit(
        {
            "event": "position_expired",
            "ts": int(time.time()),
            "otoken": otoken,
            "chain": chain,
            "underlying": underlying,
            "result": settlement,
            "expiry_price": expiry_price,
            "settlement_pnl": round(settlement_pnl, 4),
            "hedge_pnl": round(hedge_pnl, 4),
            "hedge_close_price": hedge_close_price,
            "net_pnl": round(net_pnl, 4),
        }
    )
    log.info(
        "[TRADE LOG] position_expired otoken=%s result=%s pnl=%.4f",
        otoken[:10],
        settlement,
        net_pnl,
    )


def log_capacity_snapshot(
    premium_usd: float,
    hedge_usd: float,
    hedge_withdrawable: float,
    effective_units: float,
    status: str,
    underlying: str = "eth",
) -> None:
    _emit(
        {
            "event": "capacity_snapshot",
            "ts": int(time.time()),
            "underlying": underlying,
            "premium_usd": round(premium_usd, 2),
            "hedge_usd": round(hedge_usd, 2),
            "hedge_withdrawable": round(hedge_withdrawable, 2),
            "effective_units": round(effective_units, 4),
            "status": status,
        }
    )


def _hedge_upsert_worker() -> None:
    while True:
        event = _hedge_upserts.get()
        key = (str(event.get("rebalance_id", "")), str(event.get("updated_at", "")))
        retry_queued = False
        try:
            client = _get_supabase()
            if client:
                row = {
                    k: v
                    for k, v in event.items()
                    if k != "event"
                    and k != "order_started_ms"
                    and not k.startswith("_")
                }
                client.table("mm_hedge_executions").upsert(
                    row, on_conflict="rebalance_id"
                ).execute()
                _hedge_upserted[key[0]] = key[1]
        except Exception as exc:
            log.warning("Hedge telemetry upsert failed (%s)", type(exc).__name__)
            time.sleep(1)
            try:
                _hedge_upserts.put_nowait(event)
                _hedge_queued.add(key)
                retry_queued = True
            except queue.Full:
                pass  # JSONL replay will re-enqueue the latest event.
        finally:
            if not retry_queued:
                _hedge_queued.discard(key)
            _hedge_upserts.task_done()


def _replay_hedge_upserts() -> None:
    latest = {
        event["rebalance_id"]: event
        for event in read_events()
        if event.get("event") == "hedge_execution" and event.get("rebalance_id")
    }
    for rebalance_id, event in latest.items():
        updated_at = str(event.get("updated_at", ""))
        key = (rebalance_id, updated_at)
        if _hedge_upserted.get(rebalance_id) == updated_at or key in _hedge_queued:
            continue
        _hedge_upserts.put(event)
        _hedge_queued.add(key)


def _hedge_event_worker() -> None:
    while True:
        event = _hedge_events.get()
        try:
            _write_jsonl(event)
            key = (str(event.get("rebalance_id", "")), str(event.get("updated_at", "")))
            try:
                _hedge_upserts.put_nowait(event)
                _hedge_queued.add(key)
            except queue.Full:
                pass  # JSONL is durable; periodic replay will enqueue the latest row.
        except Exception as exc:
            log.warning("Failed to process hedge telemetry (%s)", type(exc).__name__)


def _replay_loop() -> None:
    while True:
        try:
            _replay_hedge_upserts()
        except Exception as exc:
            log.warning("Hedge telemetry replay failed (%s)", type(exc).__name__)
        time.sleep(_hedge_replay_interval)


def start_hedge_upsert_replay() -> None:
    """Start writer and periodic recovery of latest JSONL rows."""
    global _hedge_replay_started, _hedge_writer_started
    if not _hedge_writer_started:
        _hedge_writer_started = True
        threading.Thread(
            target=_hedge_event_worker, name="hedge-telemetry-local-writer", daemon=True
        ).start()
        threading.Thread(
            target=_hedge_upsert_worker, name="hedge-telemetry-writer", daemon=True
        ).start()
    if config.SUPABASE_URL and config.SUPABASE_KEY and not _hedge_replay_started:
        _hedge_replay_started = True
        threading.Thread(
            target=_replay_loop, name="hedge-telemetry-replay", daemon=True
        ).start()


def log_hedge_execution(event: dict[str, Any]) -> None:
    """Sanitize and enqueue hedge telemetry without performing I/O."""
    global _hedge_writer_started
    try:
        safe = {**event}
        error = safe.get("error")
        if error:
            safe["error"] = (
                error
                if error in _SAFE_HEDGE_ERRORS
                or isinstance(error, str)
                and error.isidentifier()
                and error.endswith(("Error", "Exception"))
                else type(error).__name__
                if isinstance(error, Exception)
                else "reconciliation_error"
            )
        event = {
            "event": "hedge_execution",
            **safe,
            "updated_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        }
        if not _hedge_writer_started:
            start_hedge_upsert_replay()
        _hedge_events.put_nowait(event)
    except Exception as exc:
        log.warning("Failed to enqueue hedge telemetry (%s)", type(exc).__name__)


def read_events() -> list[dict[str, Any]]:
    """Read all events from the JSONL file."""
    path = config.TRADE_LOG_PATH
    if not os.path.exists(path):
        return []
    events = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("Bad JSON at line %d in %s", line_num, path)
    return events


def read_events_from_supabase() -> list[dict[str, Any]]:
    """Read position events from Supabase mm_trade_history table."""
    client = _get_supabase()
    if not client:
        return []
    try:
        resp = (
            client.table("mm_trade_history")
            .select("*")
            .in_("event", ["position_opened", "position_expired"])
            .order("ts", desc=False)
            .execute()
        )
        return resp.data or []
    except Exception:
        log.warning("Failed to read events from Supabase", exc_info=True)
        return []


def read_open_order_events_from_supabase(
    mm_addresses_by_chain: dict[str, str],
) -> list[dict[str, Any]]:
    """Read unsettled order_events for the configured MM addresses."""
    client = _get_supabase()
    if not client or not mm_addresses_by_chain:
        return []

    rows: list[dict[str, Any]] = []
    for chain, mm_address in mm_addresses_by_chain.items():
        try:
            resp = (
                client.table("order_events")
                .select(
                    "chain,asset,otoken_address,amount,gross_premium,"
                    "user_address,tx_hash,strike_price,expiry,is_put,"
                    "collateral,mm_address,is_settled"
                )
                .eq("chain", chain)
                .eq("mm_address", mm_address)
                .eq("is_settled", False)
                .order("expiry", desc=False)
                .execute()
            )
        except Exception:
            log.warning(
                "Failed to read %s order_events from Supabase",
                chain,
                exc_info=True,
            )
            continue
        rows.extend(resp.data or [])
    return rows


def write_bootstrap_event(event: dict[str, Any]) -> None:
    """Write a single bootstrap event (used during startup recovery)."""
    _emit(event)
