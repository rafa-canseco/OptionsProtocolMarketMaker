"""Standalone market maker for the b1nary options protocol.

Usage: uv run python -m src.main
"""

import logging
import threading
import time
from dataclasses import dataclass

from eth_account import Account
from web3 import Web3

from src import api_client, config, fill_listener, hedge_executor, trade_logger
from src.capacity import calculate_capacity_internal
from src.position_tracker import PositionTracker
from src.quote_builder import build_quotes, to_api_payload
from src.signer import build_domain, read_maker_nonce, sign_quote
from src.startup_recovery import recover_positions

OTOKEN_DECIMALS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mm")


@dataclass
class MarketSnapshot:
    spot: float = 0.0
    iv: float = 0.0


_tracker = PositionTracker()
_market: dict[str, MarketSnapshot] = {}
_seen_tx_hashes: set[str] = set()
_fill_lock = threading.Lock()


def _get_market(asset: str) -> MarketSnapshot:
    if asset not in _market:
        _market[asset] = MarketSnapshot()
    return _market[asset]


def run_cycle(
    w3: Web3,
    domain: dict,
    mm_address: str,
) -> None:
    """Single quote-refresh cycle across all configured assets."""
    # 1. Delete stale quotes from previous cycle
    try:
        deleted = api_client.delete_quotes()
        log.info("Deleted previous quotes: %s", deleted)
    except Exception:
        log.warning("Failed to delete stale quotes", exc_info=True)

    # 2. Poll fills via REST as fallback (WS may miss events)
    _poll_fills_rest()

    # 3. Check for expired positions (per-asset with correct spot)
    for asset_cfg in config.ASSETS:
        mkt = _get_market(asset_cfg.name)
        if mkt.spot > 0:
            expired = _tracker.check_expiries(mkt.spot, underlying=asset_cfg.name)
            if expired:
                log.info(
                    "Settled %d expired %s positions",
                    len(expired),
                    asset_cfg.name.upper(),
                )

    # 4. Per-asset: fetch market data, quote, capacity
    for asset_cfg in config.ASSETS:
        try:
            _run_asset_cycle(w3, domain, mm_address, asset_cfg)
        except Exception:
            log.error(
                "Asset cycle failed for %s",
                asset_cfg.name.upper(),
                exc_info=True,
            )


def _run_asset_cycle(
    w3: Web3,
    domain: dict,
    mm_address: str,
    asset_cfg: config.AssetConfig,
) -> None:
    """Quote-refresh for a single asset."""
    asset_name = asset_cfg.name
    mkt = _get_market(asset_name)

    # Fetch market data
    market = api_client.get_market_data(asset=asset_name)
    otokens = market.get("available_otokens", [])
    mkt.spot = market["spot"]
    mkt.iv = market["iv"]
    log.info(
        "Market [%s]: spot=%.2f iv=%.4f oTokens=%d",
        asset_name.upper(),
        mkt.spot,
        mkt.iv,
        len(otokens),
    )

    # Cache oToken details for position tracking
    if otokens:
        _tracker.cache_otokens(otokens, underlying=asset_name)

    # Recalculate deltas on open positions for this asset
    asset_positions = _tracker.open_positions(underlying=asset_name)
    if asset_positions:
        _tracker.recalculate_deltas(
            mkt.spot, mkt.iv, config.RISK_FREE_RATE, underlying=asset_name
        )
        _tracker.log_portfolio(mkt.spot)

    # Log capacity snapshot
    _log_capacity_snapshot(asset_cfg)

    if not otokens:
        log.warning("No oTokens for %s, skipping", asset_name.upper())
        return

    # Calculate capacity
    try:
        cap = calculate_capacity_internal(
            w3, mkt.spot, mm_address, _tracker, asset_config=asset_cfg
        )
        is_internal = config.MM_TYPE == "internal"
        cap_payload = cap.to_dict(internal=is_internal)
        log.info(
            "Capacity [%s]: %.2f units ($%.0f) status=%s",
            asset_name.upper(),
            cap.capacity_eth,
            cap.capacity_usd,
            cap.status,
        )
    except Exception:
        log.warning(
            "Failed to calculate capacity for %s, skipping quotes",
            asset_name.upper(),
            exc_info=True,
        )
        return

    # Report capacity to backend
    if cap_payload:
        try:
            api_client.report_capacity(cap_payload)
        except Exception:
            log.warning("Failed to report capacity", exc_info=True)

    # Skip quoting if full
    if cap and cap.status == "full":
        log.warning("Capacity full for %s, skipping quotes", asset_name.upper())
        return

    # Read makerNonce from chain
    nonce = read_maker_nonce(w3, config.BATCH_SETTLER, mm_address)

    # Price and build quotes (dynamic maxAmount)
    max_amount_raw = None
    if cap:
        max_amount_raw = int(cap.capacity_eth * 10**OTOKEN_DECIMALS)
        max_amount_raw = min(max_amount_raw, config.MAX_AMOUNT)
    quotes = build_quotes(
        market, nonce, max_amount_raw=max_amount_raw, asset=asset_name
    )
    if not quotes:
        log.warning("No valid quotes for %s", asset_name.upper())
        return

    # Sign each quote
    payloads = []
    for q in quotes:
        eip712_data = {
            "oToken": q["oToken"],
            "bidPrice": q["bidPrice"],
            "deadline": q["deadline"],
            "quoteId": q["quoteId"],
            "maxAmount": q["maxAmount"],
            "makerNonce": q["makerNonce"],
        }
        sig = sign_quote(config.MM_PRIVATE_KEY, domain, eip712_data)
        payloads.append(to_api_payload(q, sig))

    # Submit quotes
    result = api_client.submit_quotes(payloads)
    log.info(
        "Submitted %d %s quotes: accepted=%s rejected=%s errors=%s",
        len(payloads),
        asset_name.upper(),
        result.get("accepted"),
        result.get("rejected"),
        result.get("errors"),
    )


def log_monitoring() -> None:
    """Log fills and exposure for visibility."""
    try:
        exposure = api_client.get_exposure()
        log.info(
            "Exposure: active_quotes=%s notional=%s premium_earned=%s",
            exposure.get("active_quotes_count"),
            exposure.get("active_quotes_notional"),
            exposure.get("total_premium_earned"),
        )
    except Exception:
        log.warning("Failed to fetch exposure", exc_info=True)

    # Prefer WebSocket fills; fall back to REST if WS is disconnected
    if fill_listener.is_connected():
        fills = fill_listener.get_recent_fills()
        source = "ws"
    else:
        try:
            fills = api_client.get_fills(limit=5)
            source = "rest"
        except Exception:
            log.warning("Failed to fetch fills", exc_info=True)
            return

    if fills:
        log.info("Recent fills (%s): %d", source, len(fills))
        for f in fills[:3]:
            log.info(
                "  fill: otoken=%s amount=%s premium=%s",
                (f.get("otoken_address") or "")[:10] + "...",
                f.get("amount"),
                f.get("gross_premium"),
            )


def _log_capacity_snapshot(asset_cfg: config.AssetConfig) -> None:
    """Log a capacity snapshot for a specific asset."""
    mkt = _get_market(asset_cfg.name)
    try:
        exposure = api_client.get_exposure()
        account_val = hedge_executor.get_account_value()
        hl_positions = hedge_executor.get_positions()
        asset_pos = next(
            (p for p in hl_positions if p["coin"] == asset_cfg.hedge_symbol),
            None,
        )
        hedge_usd = 0.0
        if asset_pos:
            hedge_usd = abs(asset_pos["size"]) * asset_pos["entry_price"]

        premium_usd = float(exposure.get("total_premium_earned", 0))
        has_positions = bool(_tracker.open_positions(underlying=asset_cfg.name))
        status = "active" if has_positions else "idle"
        spot = mkt.spot or 1.0

        trade_logger.log_capacity_snapshot(
            premium_usd=premium_usd,
            hedge_usd=hedge_usd,
            hedge_withdrawable=account_val,
            effective_units=account_val / spot if spot > 0 else 0.0,
            status=status,
            underlying=asset_cfg.name,
        )
    except Exception:
        log.warning("Failed to log capacity snapshot", exc_info=True)


def _poll_fills_rest() -> None:
    """Check for new fills via REST API as WS fallback."""
    # Need at least one asset with market data
    if not any(m.spot > 0 for m in _market.values()):
        return
    try:
        fills = api_client.get_fills(limit=10)
    except Exception:
        log.warning("Failed to poll fills", exc_info=True)
        return
    for fill in fills:
        tx = fill.get("tx_hash", "")
        if tx and tx not in _seen_tx_hashes:
            log.info("New fill via REST poll: %s", tx[:16])
            _handle_fill(fill)


def _resolve_underlying(otoken_addr: str) -> tuple[str, str]:
    """Determine underlying + hedge_symbol from an oToken address."""
    details = _tracker.get_otoken_details(otoken_addr)
    if details and "underlying" in details:
        underlying = details["underlying"]
        asset_cfg = config.ASSET_MAP.get(underlying)
        if asset_cfg:
            return underlying, asset_cfg.hedge_symbol
    # Default to first configured asset (backward compat)
    default = config.ASSETS[0]
    log.warning(
        "Could not resolve underlying for oToken %s, defaulting to %s",
        otoken_addr[:10],
        default.name,
    )
    return default.name, default.hedge_symbol


def _handle_fill(fill: dict) -> None:
    """Called from fill_listener thread or REST poll on each fill."""
    with _fill_lock:
        tx = fill.get("tx_hash", "")
        if tx:
            if any(p.tx_hash == tx for p in _tracker.positions):
                _seen_tx_hashes.add(tx)
                return
            if tx in _seen_tx_hashes:
                return

        otoken_addr = fill.get("otoken_address", "")
        underlying, hedge_symbol = _resolve_underlying(otoken_addr)
        mkt = _get_market(underlying)

        if mkt.spot <= 0 or mkt.iv <= 0:
            log.warning(
                "Fill %s before market data for %s, will retry",
                tx[:16] if tx else "?",
                underlying.upper(),
            )
            return

        _seen_tx_hashes.add(tx)
        _tracker.add_position(
            fill,
            mkt.spot,
            mkt.iv,
            config.RISK_FREE_RATE,
            underlying=underlying,
            hedge_symbol=hedge_symbol,
        )
        _tracker.log_portfolio(mkt.spot)


def main() -> None:
    mm_address = Account.from_key(config.MM_PRIVATE_KEY).address
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    domain = build_domain(config.CHAIN_ID, config.BATCH_SETTLER)

    log.info("b1nary Market Maker starting")
    log.info("  MM address:  %s", mm_address)
    log.info("  Backend:     %s", config.BACKEND_URL)
    log.info("  RPC:         %s", config.RPC_URL)
    log.info("  Assets:      %s", [a.name for a in config.ASSETS])
    log.info("  Spread:      %d bps", config.SPREAD_BPS)
    log.info("  Refresh:     %ds", config.REFRESH_INTERVAL)
    log.info("  Max amount:  %d (raw)", config.MAX_AMOUNT)
    log.info("  Deadline:    %ds", config.DEADLINE_SECONDS)
    log.info("  Hedge mode:  %s", config.HEDGE_MODE)
    log.info("  MM type:     %s", config.MM_TYPE)
    log.info("  Reserve:     %.0f%%", config.CAPACITY_RESERVE_RATIO * 100)
    for a in config.ASSETS:
        log.info(
            "  %s: symbol=%s leverage=%dx max_exposure=%.0f%%",
            a.name.upper(),
            a.hedge_symbol,
            a.leverage,
            a.max_exposure * 100,
        )

    hedge_executor.init()

    # Recover open positions from trade history
    restored = recover_positions(_tracker)
    if restored:
        log.info("Recovered %d open positions from trade history", restored)
        for pos in _tracker.open_positions():
            _seen_tx_hashes.add(pos.tx_hash)

    # Seed seen fills so REST poll doesn't reprocess history
    try:
        existing = api_client.get_fills(limit=50)
        for f in existing:
            tx = f.get("tx_hash", "")
            if tx:
                _seen_tx_hashes.add(tx)
        log.info("Seeded %d existing fills", len(_seen_tx_hashes))
    except Exception:
        log.warning("Failed to seed fills", exc_info=True)

    fill_listener.set_on_fill(_handle_fill)
    fill_listener.start()

    cycle = 0
    while True:
        cycle += 1
        log.info("--- Cycle %d ---", cycle)
        try:
            run_cycle(w3, domain, mm_address)
        except Exception:
            log.error("Cycle %d failed", cycle, exc_info=True)

        # Log monitoring every 5 cycles
        if cycle % 5 == 0:
            log_monitoring()

        log.info("Sleeping %ds...", config.REFRESH_INTERVAL)
        time.sleep(config.REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
