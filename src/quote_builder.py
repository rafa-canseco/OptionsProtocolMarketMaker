"""Build quote structs from market data and BS prices."""

import time
from typing import Any

from src import config
from src.pricer import apply_vol_skew, calculate_spread, price_with_spread


def build_quotes(
    market_data: dict[str, Any],
    maker_nonce: int,
    max_amount_raw: int | None = None,
    asset: str = "eth",
    inventory_imbalance: float = 0.0,
    utilization: float = 0.0,
) -> list[dict[str, Any]]:
    """Price each oToken and build a list of quote dicts ready for signing.

    Returns:
        List of dicts with keys matching the EIP-712 Quote struct
        plus metadata fields for the API (strike_price, expiry, is_put, asset).
    """
    spot: float = market_data["spot"]
    iv: float = market_data["iv"]
    otokens: list[dict] = market_data["available_otokens"]
    now = int(time.time())
    effective_max = max_amount_raw if max_amount_raw is not None else config.MAX_AMOUNT

    # Offset quote_ids per asset so multi-asset quotes don't collide
    # in the backend's upsert (on_conflict=mm_address,quote_id)
    asset_index = next((i for i, a in enumerate(config.ASSETS) if a.name == asset), 0)
    quote_id_offset = asset_index * 1000

    quotes: list[dict[str, Any]] = []
    for idx, ot in enumerate(otokens):
        strike: float = ot["strike_price"]
        expiry: int = ot["expiry"]
        is_put: bool = ot["is_put"]

        seconds_to_expiry = expiry - now
        if seconds_to_expiry <= 0:
            continue

        T = seconds_to_expiry / (365 * 86400)

        spread_bps = calculate_spread(
            base_bps=config.SPREAD_BPS,
            is_put=is_put,
            T=T,
            inventory_imbalance=inventory_imbalance,
            utilization=utilization,
        )

        skewed_iv = apply_vol_skew(iv, spot, strike, is_put)

        bid_usd = price_with_spread(
            is_put=is_put,
            S=spot,
            K=strike,
            T=T,
            r=config.RISK_FREE_RATE,
            sigma=skewed_iv,
            spread_bps=spread_bps,
        )

        # Convert to USDC raw (6 decimals), floor at 1
        bid_price_raw = max(int(bid_usd * 1e6), 1)

        quotes.append(
            {
                # EIP-712 fields
                "oToken": ot["address"],
                "bidPrice": bid_price_raw,
                "deadline": now + config.DEADLINE_SECONDS,
                "quoteId": quote_id_offset + idx,
                "maxAmount": effective_max,
                "makerNonce": maker_nonce,
                # API metadata
                "strike_price": strike,
                "expiry": expiry,
                "is_put": is_put,
                "asset": asset,
            }
        )

    return quotes


def to_api_payload(quote: dict[str, Any], signature: str) -> dict[str, Any]:
    """Convert a quote dict + signature into the POST /mm/quotes format."""
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
    }
