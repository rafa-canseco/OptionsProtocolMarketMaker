"""Recover open positions from trade log on startup."""

import logging
import time
from typing import Any

from src import api_client, config, hedge_executor, trade_logger
from src.position_tracker import Position, PositionTracker
from src.pricer import bs_delta

log = logging.getLogger(__name__)


def recover_positions(tracker: PositionTracker) -> int:
    """Restore open positions from persisted events.

    Reads from Supabase first (production), falls back to JSONL (local).
    Returns number of positions restored.
    """
    events = trade_logger.read_events_from_supabase()
    source = "supabase"
    if not events:
        events = trade_logger.read_events()
        source = "jsonl"

    if not events:
        log.info("No trade history found, checking for bootstrap")
        return _bootstrap_from_live_state(tracker)

    log.info("Loaded %d events from %s", len(events), source)

    opened: dict[str, dict[str, Any]] = {}
    closed: set[str] = set()

    for ev in events:
        event_type = ev.get("event")
        otoken = ev.get("otoken", "")
        if event_type == "position_opened":
            opened[otoken] = ev
        elif event_type == "position_expired":
            closed.add(otoken)

    restored = 0
    for otoken, ev in opened.items():
        if otoken in closed:
            continue
        if ev.get("expiry", 0) < int(time.time()):
            log.info("Skipping expired-but-unlogged position: %s", otoken[:10])
            continue
        pos = _event_to_position(ev)
        tracker.positions.append(pos)
        restored += 1
        log.info(
            "[RECOVERED] %s strike=%.0f hedge=%.4f ETH",
            otoken[:10],
            pos.strike,
            pos.hedge_fill_size,
        )

    if restored:
        _verify_hedges(tracker)

    return restored


def _event_to_position(ev: dict[str, Any]) -> Position:
    """Convert a position_opened event back into a Position object."""
    return Position(
        otoken_address=ev["otoken"],
        strike=ev["strike"],
        expiry=ev["expiry"],
        is_put=ev["is_put"],
        amount_raw=int(ev["amount_eth"] * 10**8),
        premium_paid_raw=int(ev["premium_usd"] * 10**6),
        user_address=ev.get("user_address", ""),
        tx_hash=ev.get("tx_hash", ""),
        open_time=ev.get("ts", 0),
        spot_at_open=ev["spot"],
        delta_at_open=ev["delta"],
        current_delta=ev["delta"],
        hedge_fill_size=ev.get("hedge_size_eth", 0.0),
        hedge_fill_price=ev.get("hedge_fill_price", 0.0),
    )


def _verify_hedges(tracker: PositionTracker) -> None:
    """Compare recovered positions against Hyperliquid state."""
    hl_positions = hedge_executor.get_positions()
    if not hl_positions:
        if tracker.open_positions():
            log.warning(
                "[DRIFT] %d recovered positions but NO Hyperliquid hedge",
                len(tracker.open_positions()),
            )
        return

    eth_pos = next((p for p in hl_positions if p["coin"] == "ETH"), None)
    if not eth_pos:
        log.warning("[DRIFT] No ETH position on Hyperliquid")
        return

    hl_size = abs(eth_pos["size"])
    expected = sum(p.hedge_fill_size for p in tracker.open_positions())

    drift = abs(hl_size - expected)
    if drift > 0.001:
        log.warning(
            "[DRIFT] Hyperliquid ETH=%.4f, expected=%.4f, diff=%.4f",
            hl_size,
            expected,
            drift,
        )
    else:
        log.info(
            "[HEDGE OK] Hyperliquid ETH=%.4f matches expected=%.4f",
            hl_size,
            expected,
        )


def _bootstrap_from_live_state(tracker: PositionTracker) -> int:
    """Create initial log entries from Hyperliquid + backend fills.

    Used when deploying for the first time with an existing position.
    """
    hl_positions = hedge_executor.get_positions()
    eth_pos = next((p for p in hl_positions if p["coin"] == "ETH"), None)
    if not eth_pos:
        log.info("No existing Hyperliquid position, nothing to bootstrap")
        return 0

    log.info(
        "[BOOTSTRAP] Found Hyperliquid position: size=%.4f entry=$%.2f",
        eth_pos["size"],
        eth_pos["entry_price"],
    )

    try:
        fills = api_client.get_fills(limit=50)
    except Exception:
        log.warning("Failed to fetch fills for bootstrap", exc_info=True)
        fills = []

    if not fills:
        log.warning(
            "[BOOTSTRAP] Hyperliquid position exists but no backend fills. "
            "Cannot reconstruct option details."
        )
        return 0

    # Fetch market data once for spot, IV, and otoken lookup
    try:
        market = api_client.get_market_data()
    except Exception:
        log.warning("Failed to fetch market data for bootstrap", exc_info=True)
        market = {}

    spot = market.get("spot_price", eth_pos["entry_price"])
    iv = market.get("iv", 0.80)
    otoken_map = {
        ot["address"].lower(): ot for ot in market.get("available_otokens", [])
    }

    hl_size = abs(eth_pos["size"])
    is_short = eth_pos["size"] < 0

    bootstrapped = 0
    for fill in fills:
        otoken_addr = fill.get("otoken_address", "")
        if not otoken_addr:
            continue

        details = otoken_map.get(otoken_addr.lower())
        if not details:
            continue

        expiry = details.get("expiry", 0)
        if expiry < int(time.time()):
            continue

        amount_raw = int(fill.get("amount", 0))
        premium_raw = int(fill.get("gross_premium", 0))
        amount_eth = amount_raw / 10**8
        premium_usd = premium_raw / 10**6

        is_put = details.get("is_put", is_short)
        strike = details.get("strike_price", 0)
        T = max((expiry - int(time.time())) / (365 * 86400), 0.0)
        delta = bs_delta(is_put, spot, strike, T, config.RISK_FREE_RATE, iv)

        event = {
            "event": "position_opened",
            "ts": int(time.time()),
            "otoken": otoken_addr,
            "strike": strike,
            "expiry": expiry,
            "is_put": is_put,
            "amount_eth": amount_eth,
            "premium_usd": premium_usd,
            "user_address": fill.get("user_address", ""),
            "tx_hash": fill.get("tx_hash", ""),
            "spot": spot,
            "delta": delta,
            "hedge_action": "SHORT" if is_put else "LONG",
            "hedge_size_eth": hl_size,
            "hedge_fill_price": eth_pos["entry_price"],
        }
        trade_logger.write_bootstrap_event(event)

        pos = _event_to_position(event)
        tracker.positions.append(pos)
        bootstrapped += 1
        log.info(
            "[BOOTSTRAP] Restored position: %s strike=%.0f delta=%.3f",
            otoken_addr[:10],
            pos.strike,
            delta,
        )
        break

    if bootstrapped:
        _verify_hedges(tracker)

    return bootstrapped
