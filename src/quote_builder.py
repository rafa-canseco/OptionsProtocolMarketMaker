"""Build quote structs from market data and BS prices."""

import logging
import time
from typing import Any

from src import config
from src.pricer import (
    apply_vol_skew,
    bs_delta,
    calculate_spread,
    price_with_spread,
)

log = logging.getLogger(__name__)

SKIP_DELTA_THRESHOLD = 0.90
MIN_HOURS_TO_EXPIRY = 1

# All chains use USDC (6 decimals) for bidPrice.
PRICE_SCALE_BASE = 1_000_000
PRICE_SCALE_SOLANA = 1_000_000
PRICE_SCALE_XLAYER = 1_000_000

# Chain index offset to avoid quote ID collisions across chains
_CHAIN_OFFSET = {"base": 0, "solana": 100_000, "xlayer": 200_000}


def build_quotes(
    market_data: dict[str, Any],
    maker_nonce: int,
    *,
    max_amount_raw: int | None = None,
    asset: str = "eth",
    inventory_imbalance: float = 0.0,
    utilization: float = 0.0,
    chain: str = "base",
) -> list[dict[str, Any]]:
    """Price each oToken and build a list of quote dicts ready for signing.

    Returns:
        List of dicts with signing fields + metadata
        (strike_price, expiry, is_put, asset, chain).
    """
    spot: float = market_data["spot"]
    iv: float = market_data["iv"]
    otokens: list[dict] = market_data["available_otokens"]
    now = int(time.time())
    effective_max = max_amount_raw if max_amount_raw is not None else config.MAX_AMOUNT

    # Pick price scale by chain
    _price_scales = {
        "base": PRICE_SCALE_BASE,
        "solana": PRICE_SCALE_SOLANA,
        "xlayer": PRICE_SCALE_XLAYER,
    }
    price_scale = _price_scales.get(chain, PRICE_SCALE_BASE)

    # Offset quote_ids per chain + asset so quotes don't collide
    _chain_asset_map = {
        "base": config.ASSETS,
        "solana": config.SOLANA_ASSETS,
        "xlayer": config.XLAYER_ASSETS,
    }
    all_assets = _chain_asset_map.get(chain, config.ASSETS)
    asset_index = next((i for i, a in enumerate(all_assets) if a.name == asset), 0)
    quote_id_offset = _CHAIN_OFFSET.get(chain, 0) + asset_index * 1000

    quotes: list[dict[str, Any]] = []
    for idx, ot in enumerate(otokens):
        strike: float = ot["strike_price"]
        expiry: int = ot["expiry"]
        is_put: bool = ot["is_put"]

        seconds_to_expiry = expiry - now
        if seconds_to_expiry <= 0:
            continue

        T = seconds_to_expiry / (365 * 86400)

        # Skip very short-dated options (high gamma, hard to hedge)
        hours_left = seconds_to_expiry / 3600
        if hours_left < MIN_HOURS_TO_EXPIRY:
            log.debug("Skip %s: %.1fh to expiry", ot["address"][:10], hours_left)
            continue

        # Skip deep ITM options (unstable delta, high gamma)
        delta = bs_delta(is_put, spot, strike, T, config.RISK_FREE_RATE, iv)
        if abs(delta) > SKIP_DELTA_THRESHOLD:
            log.debug(
                "Skip %s: |delta|=%.2f > %.2f",
                ot["address"][:10],
                abs(delta),
                SKIP_DELTA_THRESHOLD,
            )
            continue

        spread_bps = calculate_spread(
            base_bps=config.SPREAD_BPS,
            is_put=is_put,
            T=T,
            inventory_imbalance=inventory_imbalance,
            utilization=utilization,
        )

        skewed_iv = apply_vol_skew(iv, spot, strike, is_put)

        log.debug(
            "Quote %s K=%.0f %s: spread=%dbps iv=%.4f->%.4f T=%.2fd",
            ot["address"][:10],
            strike,
            "PUT" if is_put else "CALL",
            spread_bps,
            iv,
            skewed_iv,
            T * 365,
        )

        bid_usd = price_with_spread(
            is_put=is_put,
            S=spot,
            K=strike,
            T=T,
            r=config.RISK_FREE_RATE,
            sigma=skewed_iv,
            spread_bps=spread_bps,
        )

        bid_price_raw = max(int(bid_usd * price_scale), 1)

        quotes.append(
            {
                "oToken": ot["address"],
                "bidPrice": bid_price_raw,
                "deadline": now + config.DEADLINE_SECONDS,
                "quoteId": quote_id_offset + idx,
                "maxAmount": effective_max,
                "makerNonce": maker_nonce,
                # Metadata
                "strike_price": strike,
                "expiry": expiry,
                "is_put": is_put,
                "asset": asset,
                "chain": chain,
            }
        )

    return quotes


def to_api_payload(quote: dict[str, Any], signature: str) -> dict[str, Any]:
    """Convert a Base quote dict + signature into the POST /mm/quotes format."""
    return {
        "otoken_address": quote["oToken"],
        "bid_price": quote["bidPrice"],
        "deadline": quote["deadline"],
        "quote_id": quote["quoteId"],
        "max_amount": quote["maxAmount"],
        "maker_nonce": quote["makerNonce"],
        "signature": signature,
        "strike_price": quote["strike_price"],
        "expiry": quote["expiry"],
        "is_put": quote["is_put"],
        "asset": quote.get("asset", "eth"),
        "chain": quote.get("chain", "base"),
    }


def to_solana_api_payload(
    quote: dict[str, Any],
    signature: bytes,
    maker_pubkey: str,
) -> dict[str, Any]:
    """Convert a Solana quote dict + ed25519 signature to API format."""
    import base58

    return {
        "otoken_address": quote["oToken"],
        "bid_price": quote["bidPrice"],
        "deadline": quote["deadline"],
        "quote_id": quote["quoteId"],
        "max_amount": quote["maxAmount"],
        "maker_nonce": quote["makerNonce"],
        "signature": base58.b58encode(signature).decode(),
        "maker": maker_pubkey,
        "strike_price": quote["strike_price"],
        "expiry": quote["expiry"],
        "is_put": quote["is_put"],
        "asset": quote.get("asset", "sol"),
        "chain": "solana",
    }
