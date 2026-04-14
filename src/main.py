"""Standalone market maker for the b1nary options protocol.

Usage: uv run python -m src.main
"""

import json
import logging
import threading
import time
from dataclasses import dataclass

from eth_account import Account
from web3 import Web3

from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src import api_client, config, fill_listener, hedge_executor, trade_logger
from src.capacity import calculate_capacity_internal
from src.position_tracker import PositionTracker
from src.pricer import check_iv_divergence, validate_iv
from src.quote_builder import build_quotes, to_api_payload, to_solana_api_payload
from src.signer import (
    build_domain,
    build_solana_quote_message,
    read_maker_nonce,
    read_maker_nonce_solana,
    sign_quote,
    sign_quote_solana,
)

from src.startup_recovery import recover_positions

OTOKEN_DECIMALS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mm")

# Solana runtime state (populated in main() if configured)
_solana_keypair = None  # solders.Keypair | None
_solana_maker_pubkey: str = ""  # base58 pubkey string

# Per-EVM-chain runtime state: {chain_name: (Web3, domain_dict)}
_evm_chains: dict[str, tuple[Web3, dict]] = {}


@dataclass
class MarketSnapshot:
    spot: float = 0.0
    iv: float = 0.0


_tracker = PositionTracker()
_market: dict[str, MarketSnapshot] = {}
_spot_history: dict[str, list[float]] = {}
_seen_tx_hashes: set[str] = set()
_fill_lock = threading.Lock()

SPOT_HISTORY_MAX = 100


def _get_market(asset: str, chain: str = "base") -> MarketSnapshot:
    key = f"{chain}/{asset}"
    if key not in _market:
        _market[key] = MarketSnapshot()
    return _market[key]


def run_cycle(
    w3: Web3,
    domain: dict,
    mm_address: str,
) -> None:
    """Single quote-refresh cycle across all chains and assets."""
    # 1. Delete stale quotes from previous cycle (per-chain)
    for chain_cfg in config.CHAINS:
        try:
            deleted = api_client.delete_quotes(chain=chain_cfg.name)
            log.info("Deleted previous %s quotes: %s", chain_cfg.name, deleted)
        except Exception:
            log.warning(
                "Failed to delete stale %s quotes",
                chain_cfg.name,
                exc_info=True,
            )

    # 2. Poll fills via REST as fallback (WS may miss events)
    _poll_fills_rest()

    # 3. Check for expired positions (per-asset with correct spot)
    for asset_cfg in config.ASSETS:
        mkt = _get_market(asset_cfg.name)
        if mkt.spot > 0:
            try:
                expired = _tracker.check_expiries(mkt.spot, underlying=asset_cfg.name)
                if expired:
                    log.info(
                        "Settled %d expired %s positions",
                        len(expired),
                        asset_cfg.name.upper(),
                    )
                    _tracker.rebalance_hedge(
                        mkt.spot, asset_cfg.name, asset_cfg.hedge_symbol
                    )
            except Exception:
                log.error(
                    "Expiry check failed for %s",
                    asset_cfg.name.upper(),
                    exc_info=True,
                )

    # 4. Per-chain, per-asset: fetch market data, quote, sign, submit
    for chain_cfg in config.CHAINS:
        evm = _evm_chains.get(chain_cfg.name)
        chain_w3 = evm[0] if evm else None
        chain_domain = evm[1] if evm else None
        for asset_cfg in chain_cfg.assets:
            try:
                _run_asset_cycle(
                    w3=chain_w3,
                    domain=chain_domain,
                    mm_address=mm_address,
                    asset_cfg=asset_cfg,
                    chain=chain_cfg.name,
                )
            except Exception:
                log.error(
                    "Asset cycle failed for %s/%s",
                    chain_cfg.name.upper(),
                    asset_cfg.name.upper(),
                    exc_info=True,
                )


def _track_spot(asset_name: str, spot: float) -> None:
    """Append spot to history and check IV divergence."""
    if asset_name not in _spot_history:
        _spot_history[asset_name] = []
    if spot > 0:
        _spot_history[asset_name].append(spot)
        if len(_spot_history[asset_name]) > SPOT_HISTORY_MAX:
            _spot_history[asset_name] = _spot_history[asset_name][-SPOT_HISTORY_MAX:]


def _compute_utilization(cap) -> float:
    if cap and cap.capacity_usd > 0 and cap.open_positions_notional_usd > 0:
        return cap.open_positions_notional_usd / (
            cap.capacity_usd + cap.open_positions_notional_usd
        )
    return 0.0


def _sign_quotes_base(quotes: list[dict], domain: dict) -> list[dict]:
    """Sign Base quotes with EIP-712 ECDSA and convert to API payloads."""
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
    return payloads


def _sign_quotes_solana(quotes: list[dict]) -> list[dict]:
    """Sign Solana quotes with ed25519 and convert to API payloads."""
    if _solana_keypair is None:
        raise RuntimeError(
            "Solana keypair not initialized — call _init_solana() before signing"
        )
    payloads = []
    for q in quotes:
        otoken_bytes = bytes(Pubkey.from_string(q["oToken"]))
        message = build_solana_quote_message(
            otoken_mint=otoken_bytes,
            bid_price=q["bidPrice"],
            deadline=q["deadline"],
            quote_id=q["quoteId"],
            max_amount=q["maxAmount"],
            maker_nonce=q["makerNonce"],
        )
        sig = sign_quote_solana(_solana_keypair, message)
        payloads.append(to_solana_api_payload(q, sig, _solana_maker_pubkey))
    return payloads


def _run_asset_cycle(
    w3: Web3 | None,
    domain: dict | None,
    mm_address: str,
    asset_cfg: config.AssetConfig,
    chain: str = "base",
) -> None:
    """Quote-refresh for a single asset on a given chain."""
    asset_name = asset_cfg.name
    chain_label = f"{chain}/{asset_name}".upper()
    mkt = _get_market(asset_name, chain)

    market = api_client.get_market_data(asset=asset_name, chain=chain)
    otokens = market.get("available_otokens", [])
    mkt.spot = market["spot"]
    mkt.iv = market["iv"]
    log.info(
        "Market [%s]: spot=%.2f iv=%.4f oTokens=%d",
        chain_label,
        mkt.spot,
        mkt.iv,
        len(otokens),
    )

    _track_spot(asset_name, mkt.spot)

    if not validate_iv(mkt.iv, label=chain_label):
        return
    check_iv_divergence(
        mkt.iv,
        _spot_history.get(asset_name, []),
        label=chain_label,
    )

    if otokens:
        _tracker.cache_otokens(otokens, underlying=asset_name)

    asset_positions = _tracker.open_positions(underlying=asset_name)
    if asset_positions:
        _tracker.recalculate_deltas(
            mkt.spot,
            mkt.iv,
            config.RISK_FREE_RATE,
            underlying=asset_name,
        )
        _tracker.rebalance_hedge(mkt.spot, asset_name, asset_cfg.hedge_symbol)
        _tracker.log_portfolio(mkt.spot)

    _log_capacity_snapshot(asset_cfg)

    if not otokens:
        log.warning("No oTokens for %s, skipping", chain_label)
        return

    _quote_and_submit(w3, domain, mm_address, market, asset_cfg, chain)


def _quote_and_submit(
    w3,
    domain,
    mm_address,
    market,
    asset_cfg,
    chain,
) -> None:
    """Build quotes, sign per-chain, and submit to backend."""
    asset_name = asset_cfg.name
    chain_label = f"{chain}/{asset_name}".upper()

    # Capacity — calculate for all chains
    solana_addr = _solana_maker_pubkey if chain == "solana" else None
    cap = _calculate_and_report_capacity(
        w3,
        _get_market(asset_name, chain),
        solana_addr or mm_address,
        asset_cfg,
        chain,
    )
    if cap is None or cap.status == "full":
        return
    max_amount_raw = min(int(cap.capacity_eth * 10**OTOKEN_DECIMALS), config.MAX_AMOUNT)

    # Read nonce per chain
    if chain == "solana":
        nonce = read_maker_nonce_solana(
            config.SOLANA_RPC_URL,
            config.SOLANA_BATCH_SETTLER,
            _solana_maker_pubkey,
        )
    else:
        evm_cfg = config.EVM_CONFIGS.get(chain)
        settler_addr = evm_cfg.batch_settler if evm_cfg else config.BATCH_SETTLER
        nonce = read_maker_nonce(w3, settler_addr, mm_address)

    quotes = build_quotes(
        market,
        nonce,
        max_amount_raw=max_amount_raw,
        asset=asset_name,
        inventory_imbalance=_tracker.inventory_imbalance(underlying=asset_name),
        utilization=_compute_utilization(cap),
        chain=chain,
    )
    if not quotes:
        log.warning("No valid quotes for %s", chain_label)
        return

    if chain == "solana":
        payloads = _sign_quotes_solana(quotes)
    else:
        payloads = _sign_quotes_base(quotes, domain)

    result = api_client.submit_quotes(payloads)
    log.info(
        "Submitted %d %s quotes: accepted=%s rejected=%s errors=%s",
        len(payloads),
        chain_label,
        result.get("accepted"),
        result.get("rejected"),
        result.get("errors"),
    )


def _calculate_and_report_capacity(w3, mkt, mm_address, asset_cfg, chain="base"):
    """Calculate capacity, report to backend. Returns cap or None."""
    try:
        cap = calculate_capacity_internal(
            w3,
            mkt.spot,
            mm_address,
            _tracker,
            asset_config=asset_cfg,
            chain=chain,
        )
    except Exception:
        log.warning(
            "Failed to calculate capacity for %s, skipping quotes",
            asset_cfg.name.upper(),
            exc_info=True,
        )
        return None

    is_internal = config.MM_TYPE == "internal"
    cap_payload = cap.to_dict(internal=is_internal)
    log.info(
        "Capacity [%s]: %.2f units ($%.0f) status=%s",
        asset_cfg.name.upper(),
        cap.capacity_eth,
        cap.capacity_usd,
        cap.status,
    )

    if cap_payload:
        try:
            api_client.report_capacity(cap_payload)
        except Exception:
            log.warning("Failed to report capacity", exc_info=True)

    if cap.status == "full":
        log.warning(
            "Capacity full for %s, skipping quotes",
            asset_cfg.name.upper(),
        )

    return cap


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
            try:
                _handle_fill(fill)
            except Exception:
                log.error("Failed to handle fill %s", tx[:16], exc_info=True)


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

        try:
            _tracker.add_position(
                fill,
                mkt.spot,
                mkt.iv,
                config.RISK_FREE_RATE,
                underlying=underlying,
                hedge_symbol=hedge_symbol,
            )
            _seen_tx_hashes.add(tx)
            _tracker.rebalance_hedge(mkt.spot, underlying, hedge_symbol)
            _tracker.log_portfolio(mkt.spot)
        except Exception:
            log.error("Failed to process fill %s", tx[:16], exc_info=True)


def _pick_refresh_interval() -> int:
    """Use fast refresh when any position is near expiry."""
    threshold = int(time.time()) + config.FAST_REFRESH_HOURS * 3600
    for pos in _tracker.open_positions():
        if pos.expiry <= threshold:
            return config.REFRESH_INTERVAL_FAST
    return config.REFRESH_INTERVAL


def _init_solana() -> None:
    """Load Solana keypair from SOLANA_PRIVATE_KEY env var."""
    global _solana_keypair, _solana_maker_pubkey  # noqa: PLW0603
    from solders.keypair import Keypair  # type: ignore[import-untyped]

    raw = config.SOLANA_PRIVATE_KEY
    # Support both JSON byte-array and base58 secret key formats
    if raw.strip().startswith("["):
        key_bytes = bytes(json.loads(raw))
        _solana_keypair = Keypair.from_bytes(key_bytes)
    else:
        _solana_keypair = Keypair.from_base58_string(raw)
    _solana_maker_pubkey = str(_solana_keypair.pubkey())


def main() -> None:
    global _evm_chains  # noqa: PLW0603
    mm_address = Account.from_key(config.MM_PRIVATE_KEY).address
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    domain = build_domain(config.CHAIN_ID, config.BATCH_SETTLER)

    # Init all EVM chains (Base + XLayer etc.)
    _evm_chains["base"] = (w3, domain)
    for name, evm_cfg in config.EVM_CONFIGS.items():
        if name == "base":
            continue
        try:
            chain_w3 = Web3(Web3.HTTPProvider(evm_cfg.rpc_url))
            chain_domain = build_domain(evm_cfg.chain_id, evm_cfg.batch_settler)
            _evm_chains[name] = (chain_w3, chain_domain)
            log.info("Initialized EVM chain: %s (id=%d)", name, evm_cfg.chain_id)
        except Exception:
            log.error(
                "Failed to init %s chain, disabling",
                name,
                exc_info=True,
            )
            config.CHAINS = [c for c in config.CHAINS if c.name != name]

    # Init Solana if configured — failure disables Solana, Base continues
    if config.SOLANA_PRIVATE_KEY:
        try:
            _init_solana()
        except Exception:
            log.error(
                "Failed to init Solana keypair. Expected "
                "SOLANA_PRIVATE_KEY as base58 string or JSON "
                "byte array [1,2,...,64]. Solana disabled.",
                exc_info=True,
            )
            config.CHAINS = [c for c in config.CHAINS if c.name != "solana"]

    log.info("b1nary Market Maker starting")
    log.info("  MM address:  %s", mm_address)
    log.info("  Backend:     %s", config.BACKEND_URL)
    log.info("  Chains:      %s", [c.name for c in config.CHAINS])
    for name, evm_cfg in config.EVM_CONFIGS.items():
        log.info(
            "  %s: rpc=%s chainId=%d settler=%s",
            name.upper(),
            evm_cfg.rpc_url,
            evm_cfg.chain_id,
            evm_cfg.batch_settler[:10] + "...",
        )
        chain_assets = {
            "base": config.ASSETS,
            "xlayer": config.XLAYER_ASSETS,
        }.get(name, [])
        log.info("  %s assets: %s", name.upper(), [a.name for a in chain_assets])
    if config.SOLANA_PRIVATE_KEY:
        log.info("  Solana MM:   %s", _solana_maker_pubkey)
        log.info("  Solana RPC:  %s", config.SOLANA_RPC_URL)
        log.info("  Solana assets: %s", [a.name for a in config.SOLANA_ASSETS])
    log.info("  Spread:      %d bps", config.SPREAD_BPS)
    log.info(
        "  Refresh:     %ds (fast=%ds when <%dh to expiry)",
        config.REFRESH_INTERVAL,
        config.REFRESH_INTERVAL_FAST,
        config.FAST_REFRESH_HOURS,
    )
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

        try:
            interval = _pick_refresh_interval()
        except Exception:
            interval = config.REFRESH_INTERVAL
        log.info("Sleeping %ds...", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
