"""Standalone market maker for the b1nary options protocol.

Usage: uv run python -m src.main
"""

import logging
import time

from eth_account import Account
from web3 import Web3

from src import api_client, config, fill_listener
from src.quote_builder import build_quotes, to_api_payload
from src.signer import build_domain, read_maker_nonce, sign_quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mm")


def run_cycle(
    w3: Web3,
    domain: dict,
    mm_address: str,
) -> None:
    """Single quote-refresh cycle."""
    # 1. Delete stale quotes from previous cycle
    try:
        deleted = api_client.delete_quotes()
        log.info("Deleted previous quotes: %s", deleted)
    except Exception:
        log.warning("Failed to delete stale quotes", exc_info=True)

    # 2. Fetch market data
    market = api_client.get_market_data()
    otokens = market.get("available_otokens", [])
    log.info(
        "Market: spot=%.2f iv=%.4f oTokens=%d",
        market["eth_spot"],
        market["eth_iv"],
        len(otokens),
    )
    if not otokens:
        log.warning("No oTokens available, skipping cycle")
        return

    # 3. Read makerNonce from chain
    nonce = read_maker_nonce(w3, config.BATCH_SETTLER, mm_address)

    # 4. Price and build quotes
    quotes = build_quotes(market, nonce)
    if not quotes:
        log.warning("All oTokens expired, nothing to quote")
        return

    # 5. Sign each quote
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

    # 6. Submit quotes
    result = api_client.submit_quotes(payloads)
    log.info(
        "Submitted %d quotes: accepted=%s rejected=%s errors=%s",
        len(payloads),
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


def main() -> None:
    mm_address = Account.from_key(config.MM_PRIVATE_KEY).address
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    domain = build_domain(config.CHAIN_ID, config.BATCH_SETTLER)

    log.info("b1nary Market Maker starting")
    log.info("  MM address:  %s", mm_address)
    log.info("  Backend:     %s", config.BACKEND_URL)
    log.info("  RPC:         %s", config.RPC_URL)
    log.info("  Spread:      %d bps", config.SPREAD_BPS)
    log.info("  Refresh:     %ds", config.REFRESH_INTERVAL)
    log.info("  Max amount:  %d (raw)", config.MAX_AMOUNT)
    log.info("  Deadline:    %ds", config.DEADLINE_SECONDS)

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
