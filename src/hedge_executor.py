"""Execute hedges on Hyperliquid perpetual futures."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from src import config

if TYPE_CHECKING:
    from src.config import AssetConfig

log = logging.getLogger(__name__)

_exchange: Exchange | None = None
_info: Info | None = None
_address: str = ""


def init(assets: list[AssetConfig] | None = None) -> None:
    """Initialize Hyperliquid clients. Call once at startup."""
    global _exchange, _info, _address

    if config.HEDGE_MODE != "live":
        log.info("Hedge mode=%s, skipping Hyperliquid init", config.HEDGE_MODE)
        return

    if assets is None:
        assets = config.ASSETS

    api_url = (
        constants.TESTNET_API_URL
        if config.HYPERLIQUID_TESTNET
        else constants.MAINNET_API_URL
    )
    wallet = eth_account.Account.from_key(config.MM_PRIVATE_KEY)
    _address = wallet.address

    # Empty spot_meta bypasses SDK bug where testnet spot token
    # indices are out of range. Perp metadata still loads fine.
    empty_spot: dict = {"universe": [], "tokens": []}
    _info = Info(api_url, skip_ws=True, spot_meta=empty_spot)
    _exchange = Exchange(wallet, api_url, spot_meta=empty_spot)

    # Set leverage per configured asset
    for asset_cfg in assets:
        try:
            _exchange.update_leverage(
                asset_cfg.leverage, asset_cfg.hedge_symbol, is_cross=True
            )
            log.info(
                "Leverage set: %s=%dx",
                asset_cfg.hedge_symbol,
                asset_cfg.leverage,
            )
        except Exception:
            log.error(
                "Failed to set leverage for %s — disabling hedging",
                asset_cfg.hedge_symbol,
                exc_info=True,
            )
            _exchange = None
            return

    log.info(
        "Hyperliquid ready: %s, assets=%s, testnet=%s",
        _address,
        [a.hedge_symbol for a in assets],
        config.HYPERLIQUID_TESTNET,
    )
    _log_account_state()


def _log_account_state() -> None:
    if not _info:
        return
    try:
        state = _info.user_state(_address)
        margin = state["marginSummary"]
        log.info(
            "Hyperliquid account: value=$%s withdrawable=$%s",
            margin["accountValue"],
            state["withdrawable"],
        )
        for pos in state["assetPositions"]:
            p = pos["position"]
            log.info(
                "  Position: %s size=%s entry=%s uPnL=%s",
                p["coin"],
                p["szi"],
                p["entryPx"],
                p["unrealizedPnl"],
            )
    except Exception:
        log.warning("Failed to read Hyperliquid state", exc_info=True)


def _round_size(asset: str, size: float) -> float:
    """Round size to asset's allowed decimal places."""
    try:
        if _info and hasattr(_info, "coin_to_asset"):
            coin = _info.name_to_coin.get(asset, asset)
            asset_id = _info.coin_to_asset.get(coin)
            if isinstance(asset_id, int):
                decimals = _info.asset_to_sz_decimals.get(asset_id, 4)
                return round(size, decimals)
    except (AttributeError, TypeError):
        log.warning(
            "Failed to look up size decimals for %s, defaulting to 4",
            asset,
            exc_info=True,
        )
    return round(size, 4)


def open_hedge(asset: str, is_buy: bool, size: float) -> dict | None:
    """Open a hedge position via market order.

    Args:
        asset: Trading pair (e.g. "ETH").
        is_buy: True for long, False for short.
        size: Position size in asset units (e.g. 1.08 ETH).

    Returns:
        Fill info dict or None on failure.
    """
    if config.HEDGE_MODE != "live":
        log.info(
            "[HEDGE SIMULATED] %s %s %.4f",
            "LONG" if is_buy else "SHORT",
            asset,
            size,
        )
        return None

    if not _exchange:
        log.error("Hyperliquid not initialized, cannot hedge")
        return None

    size = _round_size(asset, size)
    if size <= 0:
        log.warning("[HEDGE] Size rounds to 0, skipping")
        return None

    try:
        result = _exchange.market_open(
            asset, is_buy, size, slippage=config.HEDGE_SLIPPAGE
        )
        if result["status"] == "ok":
            statuses = result["response"]["data"]["statuses"]
            for status in statuses:
                if "filled" in status:
                    filled = status["filled"]
                    log.info(
                        "[HEDGE EXECUTED] %s %s %.4f filled=%s @ $%s",
                        "LONG" if is_buy else "SHORT",
                        asset,
                        size,
                        filled["totalSz"],
                        filled["avgPx"],
                    )
                    return {
                        "size": float(filled["totalSz"]),
                        "avg_price": float(filled["avgPx"]),
                        "oid": filled.get("oid"),
                    }
            log.warning("[HEDGE] Order accepted but no fill: %s", statuses)
        else:
            log.error("[HEDGE FAILED] %s", result)
    except Exception:
        log.error("Hyperliquid market_open failed", exc_info=True)
    return None


def close_hedge(asset: str, size: float | None = None) -> dict | None:
    """Close a hedge position via market order.

    Args:
        asset: Trading pair (e.g. "ETH").
        size: Partial close size, or None to close entire position.

    Returns:
        Fill info dict or None on failure.
    """
    if config.HEDGE_MODE != "live":
        log.info("[HEDGE CLOSE SIMULATED] %s size=%s", asset, size)
        return None

    if not _exchange:
        log.error("Hyperliquid not initialized, cannot close")
        return None

    try:
        if size is not None:
            size = _round_size(asset, size)
            result = _exchange.market_close(
                asset, sz=size, slippage=config.HEDGE_SLIPPAGE
            )
        else:
            result = _exchange.market_close(asset, slippage=config.HEDGE_SLIPPAGE)

        if result and result["status"] == "ok":
            statuses = result["response"]["data"]["statuses"]
            for status in statuses:
                if "filled" in status:
                    filled = status["filled"]
                    log.info(
                        "[HEDGE CLOSED] %s filled=%s @ $%s",
                        asset,
                        filled["totalSz"],
                        filled["avgPx"],
                    )
                    return {
                        "size": float(filled["totalSz"]),
                        "avg_price": float(filled["avgPx"]),
                    }
            log.warning("[HEDGE CLOSE] Accepted but no fill: %s", statuses)
        else:
            log.error("[HEDGE CLOSE FAILED] %s", result)
    except Exception:
        log.error("Hyperliquid market_close failed", exc_info=True)
    return None


def adjust_hedge(
    asset: str, current_size: float, target_size: float, is_buy: bool
) -> dict | None:
    """Adjust an existing hedge to a new target size.

    Calculates the delta between current and target, then opens
    or closes the difference.

    Args:
        asset: Trading pair.
        current_size: Current hedge size in asset units.
        target_size: Desired hedge size in asset units.
        is_buy: Direction of the hedge (True=long, False=short).

    Returns:
        Fill info or None.
    """
    diff = abs(target_size - current_size)
    if diff < 0.0001:
        return None

    if target_size > current_size:
        log.info(
            "[HEDGE ADJUST] %s %s: %.4f -> %.4f (+%.4f)",
            "LONG" if is_buy else "SHORT",
            asset,
            current_size,
            target_size,
            diff,
        )
        return open_hedge(asset, is_buy, diff)
    else:
        log.info(
            "[HEDGE ADJUST] %s %s: %.4f -> %.4f (-%.4f)",
            "LONG" if is_buy else "SHORT",
            asset,
            current_size,
            target_size,
            diff,
        )
        return close_hedge(asset, size=diff)


def get_positions() -> list[dict]:
    """Get current Hyperliquid positions."""
    if not _info:
        return []
    try:
        state = _info.user_state(_address)
        positions = []
        for pos in state["assetPositions"]:
            p = pos["position"]
            positions.append(
                {
                    "coin": p["coin"],
                    "size": float(p["szi"]),
                    "entry_price": float(p["entryPx"]),
                    "unrealized_pnl": float(p["unrealizedPnl"]),
                    "leverage": p["leverage"],
                }
            )
        return positions
    except Exception:
        log.warning("Failed to get Hyperliquid positions", exc_info=True)
        return []


def get_account_value() -> float:
    """Get total account value in USD."""
    if not _info:
        return 0.0
    try:
        state = _info.user_state(_address)
        return float(state["marginSummary"]["accountValue"])
    except Exception:
        log.warning("Failed to get account value", exc_info=True)
        return 0.0


def get_withdrawable() -> float:
    """Get withdrawable (free) margin in USD."""
    if not _info:
        return 0.0
    try:
        state = _info.user_state(_address)
        return float(state["withdrawable"])
    except Exception:
        log.warning("Failed to get withdrawable margin", exc_info=True)
        return 0.0
