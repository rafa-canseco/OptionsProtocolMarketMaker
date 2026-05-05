"""Recover open positions from trade log on startup."""

import json
import logging
import time
from typing import Any

from eth_account import Account

from src import api_client, config, hedge_executor, trade_logger
from src.position_tracker import Position, PositionTracker
from src.pricer import bs_delta

log = logging.getLogger(__name__)


def _asset_maps_by_chain() -> dict[str, dict[str, config.AssetConfig]]:
    return {
        "base": config.ASSET_MAP,
        "solana": config.SOLANA_ASSET_MAP,
    }


def _configured_assets() -> list[tuple[str, config.AssetConfig]]:
    assets: list[tuple[str, config.AssetConfig]] = [
        ("base", asset) for asset in config.ASSETS
    ]
    assets.extend(("solana", asset) for asset in config.SOLANA_ASSETS)
    return assets


def _resolve_asset(
    underlying: str, chain: str | None
) -> tuple[str, config.AssetConfig | None]:
    if chain:
        asset_cfg = _asset_maps_by_chain().get(chain, {}).get(underlying)
        if asset_cfg:
            return chain, asset_cfg
    for chain_name, asset_map in _asset_maps_by_chain().items():
        asset_cfg = asset_map.get(underlying)
        if asset_cfg:
            return chain_name, asset_cfg
    return chain or "base", None


def _signed_hedge_size(event: dict[str, Any], hedge_size: float) -> float:
    action = str(event.get("hedge_action", "")).upper()
    if action == "SHORT":
        return -abs(hedge_size)
    if action == "LONG":
        return abs(hedge_size)
    return hedge_size


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
        restored = _bootstrap_from_live_state(tracker)
        restored += _recover_missing_order_events(tracker)
        if restored:
            _verify_hedges(tracker)
        return restored

    log.info("Loaded %d events from %s", len(events), source)

    opens_by_otoken: dict[str, list[dict[str, Any]]] = {}
    close_counts: dict[str, int] = {}
    seen_tx: set[str] = set()

    for ev in events:
        event_type = ev.get("event")
        otoken = ev.get("otoken", "")
        if event_type == "position_opened":
            tx = ev.get("tx_hash", "")
            if tx and tx in seen_tx:
                continue
            if tx:
                seen_tx.add(tx)
            opens_by_otoken.setdefault(otoken, []).append(ev)
        elif event_type == "position_expired":
            close_counts[otoken] = close_counts.get(otoken, 0) + 1

    restored = 0
    for otoken, opens in opens_by_otoken.items():
        n_closed = close_counts.get(otoken, 0)
        still_open = opens[n_closed:]
        for ev in still_open:
            if ev.get("expiry", 0) < int(time.time()):
                log.info(
                    "Skipping expired-but-unlogged position: %s",
                    otoken[:10],
                )
                continue
            pos = _event_to_position(ev)
            tracker.positions.append(pos)
            restored += 1
            log.info(
                "[RECOVERED] %s strike=%.0f hedge=%.4f %s",
                otoken[:10],
                pos.strike,
                pos.hedge_fill_size,
                pos.underlying.upper(),
            )

    restored += _recover_missing_order_events(tracker)

    if restored:
        _verify_hedges(tracker)

    return restored


def _solana_maker_pubkey() -> str | None:
    raw = config.SOLANA_PRIVATE_KEY
    if not raw:
        return None
    try:
        from solders.keypair import Keypair  # type: ignore[import-untyped]

        if raw.strip().startswith("["):
            key_bytes = bytes(json.loads(raw))
            return str(Keypair.from_bytes(key_bytes).pubkey())
        return str(Keypair.from_base58_string(raw).pubkey())
    except Exception:
        log.warning("Failed to derive Solana maker pubkey for recovery", exc_info=True)
        return None


def _mm_addresses_by_chain() -> dict[str, str]:
    """Return the MM addresses that should own recoverable positions."""
    identities = {"base": Account.from_key(config.MM_PRIVATE_KEY).address.lower()}
    solana_pubkey = _solana_maker_pubkey()
    if solana_pubkey:
        identities["solana"] = solana_pubkey
    return identities


def _market_snapshot(
    cache: dict[tuple[str, str], dict[str, Any]],
    chain: str,
    asset_cfg: config.AssetConfig,
) -> dict[str, Any] | None:
    key = (chain, asset_cfg.name)
    if key in cache:
        return cache[key]
    try:
        market = api_client.get_market_data(asset=asset_cfg.name, chain=chain)
    except Exception:
        log.warning(
            "Failed to fetch market data for recovery %s/%s",
            chain,
            asset_cfg.name,
            exc_info=True,
        )
        cache[key] = None
        return None
    cache[key] = market
    return market


def _recover_missing_order_events(tracker: PositionTracker) -> int:
    """Backfill open order_events that are missing from trade history."""
    if config.HEDGE_MODE != "live":
        return 0

    tracked_tx_hashes = {p.tx_hash for p in tracker.open_positions() if p.tx_hash}
    tracked_otokens = {
        (p.otoken_address.lower(), p.user_address)
        for p in tracker.open_positions()
    }
    rows = trade_logger.read_open_order_events_from_supabase(_mm_addresses_by_chain())
    if not rows:
        return 0

    market_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    restored = 0
    now = int(time.time())
    for row in rows:
        tx_hash = str(row.get("tx_hash", ""))
        if tx_hash and tx_hash in tracked_tx_hashes:
            continue

        otoken_addr = str(row.get("otoken_address", ""))
        user_address = str(row.get("user_address", ""))
        if not otoken_addr:
            continue
        if (otoken_addr.lower(), user_address) in tracked_otokens:
            continue

        underlying = str(row.get("asset", "")).lower()
        chain, asset_cfg = _resolve_asset(underlying, row.get("chain"))
        if not asset_cfg:
            log.warning(
                "Skipping recovery row for unknown asset %s/%s",
                row.get("chain"),
                underlying,
            )
            continue

        expiry = int(row.get("expiry") or 0)
        if expiry < now:
            continue

        market = _market_snapshot(market_cache, chain, asset_cfg)
        if not market:
            continue

        spot = float(market.get("spot") or 0.0)
        iv = float(market.get("iv") or 0.80)
        strike = float(row.get("strike_price") or row.get("strike") or 0.0)
        is_put = bool(row.get("is_put", False))
        amount_raw = int(row.get("amount", 0))
        premium_raw = int(row.get("gross_premium", 0))
        amount = amount_raw / 10**8
        premium_usd = premium_raw / 10**6
        T = max((expiry - now) / (365 * 86400), 0.0)
        delta = bs_delta(is_put, spot, strike, T, config.RISK_FREE_RATE, iv)

        event = {
            "event": "position_opened",
            "ts": now,
            "otoken": otoken_addr,
            "chain": chain,
            "underlying": asset_cfg.name,
            "strike": strike,
            "expiry": expiry,
            "is_put": is_put,
            "amount": amount,
            "premium_usd": premium_usd,
            "user_address": user_address,
            "tx_hash": tx_hash or f"recovery:{chain}:{otoken_addr}",
            "spot": spot,
            "delta": delta,
            "hedge_action": "LONG" if is_put else "SHORT",
            "hedge_size": abs(delta) * amount,
            "hedge_fill_price": 0.0,
        }
        trade_logger.write_bootstrap_event(event)
        tracker.positions.append(_event_to_position(event))
        tracked_tx_hashes.add(event["tx_hash"])
        tracked_otokens.add((otoken_addr.lower(), user_address))
        restored += 1
        log.info(
            "[ORDER_EVENTS RECOVERY] Restored %s/%s %s strike=%.0f amt=%.8f",
            chain.upper(),
            asset_cfg.name.upper(),
            otoken_addr[:10],
            strike,
            amount,
        )

    return restored


def _event_to_position(ev: dict[str, Any]) -> Position:
    """Convert a position_opened event back into a Position object."""
    underlying = ev.get("underlying") or "eth"
    chain, asset_cfg = _resolve_asset(underlying, ev.get("chain"))
    hedge_symbol = asset_cfg.hedge_symbol if asset_cfg else underlying.upper()

    # Support both old (amount_eth/hedge_size_eth) and new field names
    amount = ev.get("amount", ev.get("amount_eth", 0))
    hedge_size = ev.get("hedge_size", ev.get("hedge_size_eth", 0.0))
    signed_hedge_size = _signed_hedge_size(ev, float(hedge_size))

    return Position(
        otoken_address=ev["otoken"],
        strike=ev["strike"],
        expiry=ev["expiry"],
        is_put=ev["is_put"],
        amount_raw=int(amount * 10**8),
        premium_paid_raw=int(ev["premium_usd"] * 10**6),
        user_address=ev.get("user_address", ""),
        tx_hash=ev.get("tx_hash", ""),
        open_time=ev.get("ts", 0),
        spot_at_open=ev["spot"],
        delta_at_open=ev["delta"],
        chain=chain,
        underlying=underlying,
        hedge_symbol=hedge_symbol,
        current_delta=ev["delta"],
        hedge_fill_size=signed_hedge_size,
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

    hl_by_coin = {p["coin"]: p for p in hl_positions}

    for _, asset_cfg in _configured_assets():
        symbol = asset_cfg.hedge_symbol
        asset_positions = tracker.open_positions(underlying=asset_cfg.name)
        expected = sum(p.hedge_fill_size for p in asset_positions)

        hl_pos = hl_by_coin.get(symbol)
        hl_size = hl_pos["size"] if hl_pos else 0.0

        if expected == 0.0 and hl_size == 0.0:
            continue

        drift = abs(hl_size - expected)
        if drift > 0.001:
            log.warning(
                "[DRIFT] %s: Hyperliquid=%.4f, expected=%.4f, diff=%.4f",
                symbol,
                hl_size,
                expected,
                drift,
            )
        else:
            log.info(
                "[HEDGE OK] %s: Hyperliquid=%.4f matches expected=%.4f",
                symbol,
                hl_size,
                expected,
            )


def _bootstrap_from_live_state(tracker: PositionTracker) -> int:
    """Create initial log entries from Hyperliquid + backend fills.

    Used when deploying for the first time with an existing position.
    """
    hl_positions = hedge_executor.get_positions()
    if not hl_positions:
        log.info("No existing Hyperliquid positions, nothing to bootstrap")
        return 0

    # Build a map of HL coin → asset config
    coin_to_asset = {
        asset.hedge_symbol: (chain, asset) for chain, asset in _configured_assets()
    }

    bootstrapped = 0
    for hl_pos in hl_positions:
        coin = hl_pos["coin"]
        asset_ref = coin_to_asset.get(coin)
        if not asset_ref:
            continue
        chain, asset_cfg = asset_ref

        log.info(
            "[BOOTSTRAP] Found %s position: size=%.4f entry=$%.2f",
            coin,
            hl_pos["size"],
            hl_pos["entry_price"],
        )

        try:
            fills = api_client.get_fills(limit=50)
        except Exception:
            log.warning("Failed to fetch fills for bootstrap", exc_info=True)
            fills = []

        if not fills:
            log.warning(
                "[BOOTSTRAP] %s position exists but no backend fills",
                coin,
            )
            continue

        try:
            market = api_client.get_market_data(asset=asset_cfg.name, chain=chain)
        except Exception:
            log.warning("Failed to fetch market data for bootstrap", exc_info=True)
            market = {}

        spot = market.get("spot", hl_pos["entry_price"])
        iv = market.get("iv", 0.80)
        otoken_map = {
            ot["address"].lower(): ot for ot in market.get("available_otokens", [])
        }

        hl_size = abs(hl_pos["size"])
        is_short = hl_pos["size"] < 0

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
            amount = amount_raw / 10**8
            premium_usd = premium_raw / 10**6

            is_put = details.get("is_put", is_short)
            strike = details.get("strike_price", 0)
            T = max((expiry - int(time.time())) / (365 * 86400), 0.0)
            delta = bs_delta(is_put, spot, strike, T, config.RISK_FREE_RATE, iv)

            event = {
                "event": "position_opened",
                "ts": int(time.time()),
                "otoken": otoken_addr,
                "chain": chain,
                "underlying": asset_cfg.name,
                "strike": strike,
                "expiry": expiry,
                "is_put": is_put,
                "amount": amount,
                "premium_usd": premium_usd,
                "user_address": fill.get("user_address", ""),
                "tx_hash": fill.get("tx_hash", ""),
                "spot": spot,
                "delta": delta,
                "hedge_action": "SHORT" if is_short else "LONG",
                "hedge_size": hl_size,
                "hedge_fill_price": hl_pos["entry_price"],
            }
            trade_logger.write_bootstrap_event(event)

            pos = _event_to_position(event)
            tracker.positions.append(pos)
            bootstrapped += 1
            log.info(
                "[BOOTSTRAP] Restored %s position: %s strike=%.0f delta=%.3f",
                asset_cfg.name.upper(),
                otoken_addr[:10],
                pos.strike,
                delta,
            )
            break

    if bootstrapped:
        _verify_hedges(tracker)

    return bootstrapped
