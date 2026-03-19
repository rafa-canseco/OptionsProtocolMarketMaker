"""Append-only trade event logger (JSONL file + Supabase)."""

import json
import logging
import os
import time
from typing import Any

from src import config

log = logging.getLogger(__name__)

_supabase_client: Any = None


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


def _write_jsonl(event: dict[str, Any]) -> None:
    """Append one JSON line to the local log file."""
    path = config.TRADE_LOG_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        log.warning("Failed to write JSONL event", exc_info=True)


_SUPABASE_KEY_MAP = {
    "amount": "amount_eth",
    "hedge_size": "hedge_size_eth",
}


def _write_supabase(event: dict[str, Any]) -> None:
    """Insert event row into mm_trade_history table."""
    client = _get_supabase()
    if not client:
        return
    mapped = {_SUPABASE_KEY_MAP.get(k, k): v for k, v in event.items()}
    try:
        client.table("mm_trade_history").insert(mapped).execute()
    except Exception:
        log.warning("Failed to write event to Supabase", exc_info=True)


def _emit(event: dict[str, Any]) -> None:
    """Write event to all configured sinks."""
    _write_jsonl(event)
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
) -> None:
    _emit(
        {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": otoken,
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
        }
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
) -> None:
    _emit(
        {
            "event": "delta_rebalanced",
            "ts": int(time.time()),
            "otoken": otoken,
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
) -> None:
    _emit(
        {
            "event": "position_expired",
            "ts": int(time.time()),
            "otoken": otoken,
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


def write_bootstrap_event(event: dict[str, Any]) -> None:
    """Write a single bootstrap event (used during startup recovery)."""
    _emit(event)
