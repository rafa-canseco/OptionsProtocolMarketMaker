"""Standalone market maker for the b1nary options protocol.

Usage: uv run python -m src.main
"""

import json
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from eth_account import Account
from web3 import Web3

from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src import (
    api_client,
    config,
    covered_call_allocator,
    covered_call_operations_keeper,
    fill_listener,
    fund_allocator,
    fund_operations_keeper,
    hedge_executor,
    meta_wheel_allocator,
    trade_logger,
)
from src.capacity import calculate_capacity_internal, solana_call_capacity_raw
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

from src.snapshot_consumer import SnapshotBundle, SnapshotConsumer
from src.startup_recovery import recover_positions

OTOKEN_DECIMALS = 8

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class _RpcEndpointRedactingFormatter(logging.Formatter):
    def __init__(self, *args, endpoints: tuple[str | None, ...], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        sensitive_parts = []
        for endpoint in endpoints:
            if not endpoint:
                continue
            parsed = urlsplit(endpoint)
            sensitive_parts.append(endpoint)
            if parsed.netloc:
                sensitive_parts.append(parsed.netloc)
            if parsed.hostname:
                sensitive_parts.append(parsed.hostname)
            if parsed.path not in ("", "/"):
                sensitive_parts.append(parsed.path)
            if parsed.query:
                sensitive_parts.append(f"?{parsed.query}")
        self.sensitive_parts = tuple(
            sorted(set(sensitive_parts), key=len, reverse=True)
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for endpoint in self.sensitive_parts:
            rendered = rendered.replace(endpoint, "[REDACTED_RPC_ENDPOINT]")
        return rendered


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt="%H:%M:%S")
    formatter = _RpcEndpointRedactingFormatter(
        _LOG_FORMAT,
        datefmt="%H:%M:%S",
        endpoints=(config.RPC_URL, config.SOLANA_RPC_URL),
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


_configure_logging()
log = logging.getLogger("mm")

# Solana runtime state (populated in main() if configured)
_solana_keypair = None  # solders.Keypair | None
_solana_maker_pubkey: str = ""  # base58 pubkey string


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


def _asset_map_for_chain(chain: str) -> dict[str, config.AssetConfig]:
    if chain == "solana":
        return config.SOLANA_ASSET_MAP
    return config.ASSET_MAP


def _asset_is_hedge_ready(asset_cfg: config.AssetConfig, chain: str) -> bool:
    if config.HEDGE_MODE != "live":
        return True
    if not asset_cfg.hedge_enabled:
        log.warning(
            "Skipping %s/%s: hedge disabled by config",
            chain.upper(),
            asset_cfg.name.upper(),
        )
        return False
    if not hedge_executor.is_hedge_ready(asset_cfg.hedge_symbol):
        log.error(
            "Skipping %s/%s: Hyperliquid hedge symbol not ready (%s)",
            chain.upper(),
            asset_cfg.name.upper(),
            asset_cfg.hedge_symbol,
        )
        return False
    return True


def run_cycle(
    w3: Web3,
    domain: dict,
    mm_address: str,
    snapshot_bundle: SnapshotBundle | None = None,
    snapshot_consumer: SnapshotConsumer | None = None,
) -> dict | None:
    """Single quote-refresh cycle across all chains and assets."""
    v2_base_enabled = config.V2_SNAPSHOT_ENABLED and any(
        chain.name == "base" for chain in config.CHAINS
    )
    base_snapshot = None
    if v2_base_enabled:
        if snapshot_bundle is None or snapshot_consumer is None:
            raise RuntimeError("Fresh atomic Base snapshot is unavailable")
        snapshot_consumer.require(snapshot_bundle)
        base_snapshot = snapshot_bundle.market_maker(
            expected_address=mm_address,
            expected_usdc_address=config.USDC_ADDRESS,
            expected_allowance_spender=config.MARGIN_POOL_ADDRESS,
        )
    # 1. Delete stale quotes from previous cycle (per-chain)
    for chain_cfg in config.CHAINS:
        if v2_base_enabled and chain_cfg.name == "base":
            snapshot_consumer.require(snapshot_bundle)
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
    if v2_base_enabled:
        snapshot_consumer.require(snapshot_bundle)
    _poll_fills_rest(snapshot_bundle, snapshot_consumer)

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

    # 4. Fetch exposure once for cycle-scoped telemetry. Exposure is deliberately
    # not cached across cycles or used as a quote/capacity risk input. Because this
    # remains between stale-quote deletion and publication, active_quotes_count is
    # transitional telemetry; post-publication semantics are deferred.
    exposure_snapshot = _fetch_cycle_exposure()

    # 5. Per-chain, per-asset: fetch market data, quote, sign, submit
    for chain_cfg in config.CHAINS:
        for asset_cfg in chain_cfg.assets:
            try:
                _run_asset_cycle(
                    w3=w3 if chain_cfg.name == "base" else None,
                    domain=domain if chain_cfg.name == "base" else None,
                    mm_address=mm_address,
                    asset_cfg=asset_cfg,
                    chain=chain_cfg.name,
                    exposure_snapshot=exposure_snapshot,
                    base_snapshot=base_snapshot if chain_cfg.name == "base" else None,
                    snapshot_bundle=(
                        snapshot_bundle if chain_cfg.name == "base" else None
                    ),
                    snapshot_consumer=(
                        snapshot_consumer if chain_cfg.name == "base" else None
                    ),
                )
            except Exception:
                log.error(
                    "Asset cycle failed for %s/%s",
                    chain_cfg.name.upper(),
                    asset_cfg.name.upper(),
                    exc_info=True,
                )

    return exposure_snapshot


def _fetch_cycle_exposure() -> dict | None:
    """Fetch one exposure snapshot without inventing a fallback value."""
    try:
        exposure = api_client.get_exposure()
        if not isinstance(exposure, dict):
            raise TypeError("Exposure response must be an object")

        premium = exposure.get("total_premium_earned")
        if isinstance(premium, bool):
            raise TypeError("Exposure total_premium_earned must be numeric")
        try:
            premium_value = float(premium)
        except (TypeError, ValueError) as exc:
            raise TypeError("Exposure total_premium_earned must be numeric") from exc
        if not math.isfinite(premium_value):
            raise ValueError("Exposure total_premium_earned must be finite")

        return exposure
    except Exception:
        log.warning(
            "Failed to fetch cycle exposure; exposure telemetry is unavailable",
            exc_info=True,
        )
        return None


def _track_spot(asset_name: str, spot: float, chain: str = "base") -> None:
    """Append spot to history and check IV divergence."""
    key = f"{chain}/{asset_name}"
    if key not in _spot_history:
        _spot_history[key] = []
    if spot > 0:
        _spot_history[key].append(spot)
        if len(_spot_history[key]) > SPOT_HISTORY_MAX:
            _spot_history[key] = _spot_history[key][-SPOT_HISTORY_MAX:]


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
    if not config.SOLANA_USDC_MINT:
        raise RuntimeError(
            "SOLANA_USDC_MINT not set — required to sign Solana quotes "
            "(USDC is the premium asset for every Solana option)"
        )
    premium_mint_bytes = bytes(Pubkey.from_string(config.SOLANA_USDC_MINT))

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
            premium_mint=premium_mint_bytes,
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
    exposure_snapshot: dict | None = None,
    base_snapshot: dict | None = None,
    snapshot_bundle: SnapshotBundle | None = None,
    snapshot_consumer: SnapshotConsumer | None = None,
) -> None:
    """Quote-refresh for a single asset on a given chain."""
    asset_name = asset_cfg.name
    chain_label = f"{chain}/{asset_name}".upper()
    mkt = _get_market(asset_name, chain)

    if chain == "base" and config.V2_SNAPSHOT_ENABLED:
        if snapshot_bundle is None or snapshot_consumer is None:
            raise RuntimeError("Fresh atomic Base market snapshot is unavailable")
        snapshot_consumer.require(snapshot_bundle)
        market = dict(snapshot_bundle.market(asset_name))
    else:
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

    _track_spot(asset_name, mkt.spot, chain)

    if not validate_iv(mkt.iv, label=chain_label):
        return
    check_iv_divergence(
        mkt.iv,
        _spot_history.get(f"{chain}/{asset_name}", []),
        label=chain_label,
    )

    if otokens:
        _tracker.cache_otokens(otokens, underlying=asset_name, chain=chain)

    hedge_ready = _asset_is_hedge_ready(asset_cfg, chain)

    asset_positions = _tracker.open_positions(underlying=asset_name)
    if asset_positions and hedge_ready:
        _tracker.recalculate_deltas(
            mkt.spot,
            mkt.iv,
            config.RISK_FREE_RATE,
            underlying=asset_name,
        )
        _tracker.rebalance_hedge(mkt.spot, asset_name, asset_cfg.hedge_symbol)
        _tracker.log_portfolio(mkt.spot)
    elif asset_positions and not hedge_ready:
        log.error(
            "Open positions exist for %s/%s but live hedge is not ready",
            chain.upper(),
            asset_name.upper(),
        )

    _log_capacity_snapshot(asset_cfg, chain, exposure_snapshot)

    if not otokens:
        log.warning("No oTokens for %s, skipping", chain_label)
        return
    if not hedge_ready:
        return

    _quote_and_submit(
        w3,
        domain,
        mm_address,
        market,
        asset_cfg,
        chain,
        base_snapshot,
        snapshot_bundle,
        snapshot_consumer,
    )


def _quote_and_submit(
    w3,
    domain,
    mm_address,
    market,
    asset_cfg,
    chain,
    base_snapshot=None,
    snapshot_bundle=None,
    snapshot_consumer=None,
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
        base_snapshot,
    )
    if cap is None or cap.status == "full":
        return
    max_amount_raw = min(int(cap.capacity_eth * 10**OTOKEN_DECIMALS), config.MAX_AMOUNT)
    max_call_amount_raw = None
    if chain == "solana":
        max_call_amount_raw = _solana_call_capacity(asset_cfg)

    # Read nonce per chain
    if chain == "solana":
        nonce = read_maker_nonce_solana(
            config.SOLANA_RPC_URL,
            config.SOLANA_BATCH_SETTLER,
            _solana_maker_pubkey,
        )
    elif config.V2_SNAPSHOT_ENABLED:
        try:
            nonce = int(base_snapshot["maker_nonce"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Atomic Base quote nonce is unavailable") from exc
    else:
        nonce = read_maker_nonce(w3, config.BATCH_SETTLER, mm_address)

    quotes = build_quotes(
        market,
        nonce,
        max_amount_raw=max_amount_raw,
        max_call_amount_raw=max_call_amount_raw,
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
        if config.V2_SNAPSHOT_ENABLED:
            if snapshot_consumer is None or snapshot_bundle is None:
                raise RuntimeError("Fresh atomic Base snapshot is unavailable")
            snapshot_consumer.require(snapshot_bundle)
        payloads = _sign_quotes_base(quotes, domain)
        if config.V2_SNAPSHOT_ENABLED:
            snapshot_consumer.require(snapshot_bundle)

    result = api_client.submit_quotes(payloads)
    log.info(
        "Submitted %d %s quotes: accepted=%s rejected=%s errors=%s",
        len(payloads),
        chain_label,
        result.get("accepted"),
        result.get("rejected"),
        result.get("errors"),
    )


def _calculate_and_report_capacity(
    w3, mkt, mm_address, asset_cfg, chain="base", base_snapshot=None
):
    """Calculate capacity, report to backend. Returns cap or None."""
    try:
        cap = calculate_capacity_internal(
            w3,
            mkt.spot,
            mm_address,
            _tracker,
            asset_config=asset_cfg,
            chain=chain,
            base_snapshot=base_snapshot,
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
        "Capacity [%s/%s]: %.2f units ($%.0f) status=%s",
        chain.upper(),
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
            "Capacity full for %s/%s, skipping quotes",
            chain.upper(),
            asset_cfg.name.upper(),
        )

    return cap


def log_monitoring(exposure_snapshot: dict | None) -> None:
    """Log fills and the current cycle's exposure snapshot for visibility."""
    if exposure_snapshot is not None:
        log.info(
            "Exposure: active_quotes=%s notional=%s premium_earned=%s",
            exposure_snapshot.get("active_quotes_count"),
            exposure_snapshot.get("active_quotes_notional"),
            exposure_snapshot.get("total_premium_earned"),
        )
    else:
        log.warning("Exposure monitoring unavailable for this cycle")

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


def _solana_call_capacity(asset_cfg: config.AssetConfig) -> int | None:
    """Return covered-call capacity for Solana assets with configured mints."""
    try:
        call_capacity = solana_call_capacity_raw(
            _solana_maker_pubkey,
            asset_cfg.name,
            _tracker,
        )
    except Exception:
        log.warning(
            "Failed to read Solana call collateral for %s; disabling calls",
            asset_cfg.name.upper(),
            exc_info=True,
        )
        return 0
    if call_capacity is None:
        return None
    log.info(
        "Call collateral [%s/%s]: max_call_amount=%d raw",
        "SOLANA",
        asset_cfg.name.upper(),
        call_capacity,
    )
    return call_capacity


def _log_capacity_snapshot(
    asset_cfg: config.AssetConfig,
    chain: str,
    exposure_snapshot: dict | None,
) -> None:
    """Log a capacity snapshot for a specific asset."""
    if exposure_snapshot is None:
        # Never turn a failed exposure read into zero-premium or stale telemetry.
        return

    mkt = _get_market(asset_cfg.name, chain)
    try:
        account_val = hedge_executor.get_account_value(asset_cfg.hedge_symbol)
        hl_positions = hedge_executor.get_positions(asset_cfg.hedge_symbol)
        asset_pos = next(
            (p for p in hl_positions if p["coin"] == asset_cfg.hedge_symbol),
            None,
        )
        hedge_usd = 0.0
        if asset_pos:
            hedge_usd = abs(asset_pos["size"]) * asset_pos["entry_price"]

        premium_usd = float(exposure_snapshot["total_premium_earned"])
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


def _poll_fills_rest(
    snapshot_bundle: SnapshotBundle | None = None,
    snapshot_consumer: SnapshotConsumer | None = None,
) -> None:
    """Check for new fills via REST API as WS fallback."""
    # Need at least one asset with market data
    if not any(m.spot > 0 for m in _market.values()):
        return
    try:
        fills = api_client.get_fills(limit=10)
    except Exception:
        log.warning("Failed to poll fills", exc_info=True)
        return
    if config.V2_SNAPSHOT_ENABLED:
        if snapshot_bundle is None or snapshot_consumer is None:
            raise RuntimeError("Fresh atomic fill snapshot is unavailable")
        snapshot_consumer.require(snapshot_bundle)
    for fill in fills:
        tx = fill.get("tx_hash", "")
        if tx and tx not in _seen_tx_hashes:
            log.info("New fill via REST poll: %s", tx[:16])
            try:
                _handle_fill(
                    fill,
                    snapshot_bundle=snapshot_bundle,
                    snapshot_consumer=snapshot_consumer,
                )
            except Exception:
                log.error("Failed to handle fill %s", tx[:16], exc_info=True)


def _resolve_underlying(otoken_addr: str) -> tuple[str, str, str]:
    """Determine underlying + hedge_symbol from an oToken address."""
    details = _tracker.get_otoken_details(otoken_addr)
    if details and "underlying" in details:
        underlying = details["underlying"]
        chain = details.get("chain", "base")
        asset_cfg = _asset_map_for_chain(chain).get(underlying)
        if asset_cfg:
            return underlying, asset_cfg.hedge_symbol, chain
    raise RuntimeError(f"Could not resolve underlying for oToken {otoken_addr[:10]}")


def _handle_fill(
    fill: dict,
    *,
    snapshot_consumer: SnapshotConsumer | None = None,
    snapshot_bundle: SnapshotBundle | None = None,
) -> None:
    """Process a fill using V1 market state or a required current V2 snapshot."""
    bundle = None
    decision_validator = None
    if config.V2_SNAPSHOT_ENABLED:
        if snapshot_consumer is None:
            raise RuntimeError("Fresh atomic fill snapshot is unavailable")
        bundle = (
            snapshot_bundle
            if snapshot_bundle is not None
            else snapshot_consumer.current()
        )

        def decision_validator():
            snapshot_consumer.require(bundle)

        decision_validator()
    with _fill_lock:
        tx = fill.get("tx_hash", "")
        existing_position = next(
            (position for position in _tracker.positions if position.tx_hash == tx),
            None,
        )
        if tx and tx in _seen_tx_hashes:
            return

        otoken_addr = fill.get("otoken_address", "")
        underlying, hedge_symbol, chain = _resolve_underlying(otoken_addr)
        if chain == "base" and bundle is not None:
            market = bundle.market(underlying)
            mkt = MarketSnapshot(spot=float(market["spot"]), iv=float(market["iv"]))
        else:
            mkt = _get_market(underlying, chain)
        asset_cfg = _asset_map_for_chain(chain).get(underlying)

        if mkt.spot <= 0 or mkt.iv <= 0:
            log.warning(
                "Fill %s before market data for %s/%s, will retry",
                tx[:16] if tx else "?",
                chain.upper(),
                underlying.upper(),
            )
            return

        try:
            if existing_position is None:
                if decision_validator is not None:
                    decision_validator()
                added = _tracker.add_position(
                    fill,
                    mkt.spot,
                    mkt.iv,
                    config.RISK_FREE_RATE,
                    underlying=underlying,
                    hedge_symbol=hedge_symbol,
                    decision_validator=decision_validator,
                )
                if added is None:
                    return
            if not asset_cfg or not _asset_is_hedge_ready(asset_cfg, chain):
                log.error(
                    "Recorded fill for %s/%s without live hedge readiness",
                    chain.upper(),
                    underlying.upper(),
                )
                return
            if decision_validator is not None:
                decision_validator()
            _tracker.rebalance_hedge(
                mkt.spot,
                underlying,
                hedge_symbol,
                decision_validator=decision_validator,
            )
            if decision_validator is not None:
                decision_validator()
            _tracker.log_portfolio(mkt.spot)
            if tx:
                _seen_tx_hashes.add(tx)
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
    """Load Solana keypair used to publish Solana quotes."""
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
    mm_address = Account.from_key(config.MM_PRIVATE_KEY).address
    transaction_w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    domain = build_domain(config.CHAIN_ID, config.BATCH_SETTLER)
    snapshot_consumer = None
    if config.V2_SNAPSHOT_ENABLED:
        snapshot_consumer = SnapshotConsumer(
            lambda: api_client.get_snapshot_envelope(
                environment=config.SNAPSHOT_ENVIRONMENT,
                chain_id=config.CHAIN_ID,
            ),
            environment=config.SNAPSHOT_ENVIRONMENT,
            chain_id=config.CHAIN_ID,
            poll_interval_seconds=config.SNAPSHOT_POLL_INTERVAL_SECONDS,
        )
        snapshot_consumer.start()

    # Explicit opt-in: fail fast if the operator enabled publishing but init fails.
    if config.SOLANA_QUOTE_PUBLISHING_ENABLED:
        try:
            _init_solana()
        except Exception:
            log.exception(
                "FATAL: SOLANA_QUOTE_PUBLISHING_ENABLED=true but Solana "
                "keypair init failed. Expected SOLANA_PRIVATE_KEY as "
                "base58 string or JSON byte array [1,2,...,64]."
            )
            sys.exit(1)

    log.info("b1nary Market Maker starting")
    log.info("  MM address:  %s", mm_address)
    log.info("  Backend:     %s", config.BACKEND_URL)
    log.info("  Base RPC:    configured")
    log.info("  Chains:      %s", [c.name for c in config.CHAINS])
    log.info("  Base assets: %s", [a.name for a in config.ASSETS])
    log.info("  V2 snapshot mode: %s", config.V2_SNAPSHOT_ENABLED)
    log.info(
        "  Solana quote publishing: %s",
        "enabled" if config.SOLANA_QUOTE_PUBLISHING_ENABLED else "disabled",
    )
    if config.SOLANA_QUOTE_PUBLISHING_ENABLED:
        log.info("  Solana MM:   %s", _solana_maker_pubkey)
        log.info("  Solana RPC:  configured")
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
            "  %s: symbol=%s hedge_enabled=%s leverage=%dx max_exposure=%.0f%%",
            a.name.upper(),
            a.hedge_symbol,
            a.hedge_enabled,
            a.leverage,
            a.max_exposure * 100,
        )
    for a in config.SOLANA_ASSETS:
        log.info(
            "  SOLANA/%s: symbol=%s hedge_enabled=%s leverage=%dx max_exposure=%.0f%%",
            a.name.upper(),
            a.hedge_symbol,
            a.hedge_enabled,
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

    fill_listener.set_on_fill(
        lambda fill: _handle_fill(fill, snapshot_consumer=snapshot_consumer)
    )
    fill_listener.start()
    if config.V2_SNAPSHOT_ENABLED:
        fund_allocator.start(snapshot_consumer, transaction_w3)
        fund_operations_keeper.start(snapshot_consumer, transaction_w3)
        covered_call_allocator.start(snapshot_consumer, transaction_w3)
        covered_call_operations_keeper.start(snapshot_consumer, transaction_w3)
        meta_wheel_allocator.start(snapshot_consumer, transaction_w3)

    cycle = 0
    while True:
        cycle += 1
        log.info("--- Cycle %d ---", cycle)
        exposure_snapshot = None
        try:
            exposure_snapshot = run_cycle(
                transaction_w3,
                domain,
                mm_address,
                snapshot_consumer.current() if snapshot_consumer is not None else None,
                snapshot_consumer,
            )
        except Exception:
            log.error("Cycle %d failed", cycle, exc_info=True)

        # Log monitoring every 5 cycles
        if cycle % 5 == 0:
            log_monitoring(exposure_snapshot)

        try:
            interval = _pick_refresh_interval()
        except Exception:
            interval = config.REFRESH_INTERVAL
        log.info("Sleeping %ds...", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
