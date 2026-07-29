"""Execute hedges on Hyperliquid perpetual futures."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import eth_account
import requests
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
_active_symbols: set[str] = set()
_initialized_dexs: tuple[str, ...] = ("",)
_dex_exchanges: dict[str, Exchange] = {}
_dex_infos: dict[str, Info] = {}
_api_url: str = ""
_account_abstraction: str = "disabled"
_spot_state_cache: dict[str, Any] = {"ts": 0.0, "state": None}

_UNIFIED_ACCOUNT_MODES = {"unifiedAccount", "portfolioMargin"}
_SPOT_STATE_TTL_SECONDS = 2.0


def _dex_for_symbol(symbol: str) -> str:
    """Return the Hyperliquid perp dex for a hedge symbol."""
    if ":" not in symbol:
        return ""
    return symbol.split(":", 1)[0]


def _required_perp_dexs(assets: list[AssetConfig]) -> list[str]:
    """Collect non-default perp dexes required by configured hedge symbols."""
    dexs: list[str] = []
    for asset_cfg in assets:
        dex = _dex_for_symbol(asset_cfg.hedge_symbol)
        if dex and dex not in dexs:
            dexs.append(dex)
    return dexs


def _iter_initialized_dexs() -> tuple[str, ...]:
    return _initialized_dexs or ("",)


def _is_unified_account_mode() -> bool:
    return _account_abstraction in _UNIFIED_ACCOUNT_MODES


def _post_info(payload: dict[str, Any]) -> Any:
    if not _api_url:
        return None
    resp = requests.post(
        f"{_api_url}/info",
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _detect_account_abstraction() -> str:
    configured = config.HYPERLIQUID_ACCOUNT_MODE
    if configured and configured != "auto":
        return configured
    try:
        abstraction = _post_info({"type": "userAbstraction", "user": _address})
    except Exception:
        log.warning("Failed to detect Hyperliquid account abstraction", exc_info=True)
        return "disabled"
    if isinstance(abstraction, str):
        return abstraction
    return "disabled"


def _spot_clearinghouse_state() -> dict[str, Any]:
    if not _is_unified_account_mode():
        return {}

    now = time.time()
    cached = _spot_state_cache.get("state")
    if (
        cached is not None
        and now - float(_spot_state_cache.get("ts", 0.0)) < _SPOT_STATE_TTL_SECONDS
    ):
        return cached

    try:
        state = _post_info({"type": "spotClearinghouseState", "user": _address}) or {}
    except Exception:
        log.warning(
            "Failed to read Hyperliquid spot clearinghouse state",
            exc_info=True,
        )
        return cached or {}

    _spot_state_cache["ts"] = now
    _spot_state_cache["state"] = state
    return state


def _spot_token_balance(coin: str) -> tuple[float, float]:
    state = _spot_clearinghouse_state()
    balances = state.get("balances", [])
    for balance in balances:
        if balance.get("coin") == coin:
            total = float(balance.get("total", 0.0))
            hold = float(balance.get("hold", 0.0))
            return total, hold
    return 0.0, 0.0


def _state_for_dex(dex: str) -> dict[str, Any]:
    if dex:
        info = _dex_infos.get(dex)
        if not info:
            return {}
        return info.user_state(_address, dex=dex)
    if not _info:
        return {}
    return _info.user_state(_address, dex=dex)


def _collect_states() -> list[tuple[str, dict[str, Any]]]:
    if not _info:
        return []
    states: list[tuple[str, dict[str, Any]]] = []
    for dex in _iter_initialized_dexs():
        try:
            states.append((dex, _state_for_dex(dex)))
        except Exception:
            log.warning(
                "Failed to read Hyperliquid state for dex=%s",
                dex or "<default>",
                exc_info=True,
            )
    return states


def _default_assets() -> list[AssetConfig]:
    assets_by_symbol: dict[str, AssetConfig] = {}
    for asset_cfg in [*config.ASSETS, *config.SOLANA_ASSETS]:
        assets_by_symbol.setdefault(asset_cfg.hedge_symbol, asset_cfg)
    return list(assets_by_symbol.values())


def init(assets: list[AssetConfig] | None = None) -> None:
    """Initialize Hyperliquid clients. Call once at startup."""
    global _exchange, _info, _address, _active_symbols, _initialized_dexs
    global _dex_exchanges, _dex_infos
    global _api_url, _account_abstraction

    if config.HEDGE_MODE != "live":
        log.info("Hedge mode=%s, skipping Hyperliquid init", config.HEDGE_MODE)
        return

    if assets is None:
        assets = _default_assets()

    api_url = (
        constants.TESTNET_API_URL
        if config.HYPERLIQUID_TESTNET
        else constants.MAINNET_API_URL
    )
    _api_url = api_url
    wallet = eth_account.Account.from_key(config.MM_PRIVATE_KEY)
    _address = wallet.address
    perp_dexs = _required_perp_dexs(assets)
    _initialized_dexs = ("", *perp_dexs)
    _spot_state_cache["ts"] = 0.0
    _spot_state_cache["state"] = None

    # Empty spot_meta bypasses SDK bug where testnet spot token
    # indices are out of range. Perp metadata still loads fine.
    empty_spot: dict = {"universe": [], "tokens": []}
    _info = Info(api_url, skip_ws=True, spot_meta=empty_spot)
    _exchange = Exchange(wallet, api_url, spot_meta=empty_spot)
    _dex_infos = {}
    _dex_exchanges = {}
    for dex in perp_dexs:
        _dex_infos[dex] = Info(
            api_url,
            skip_ws=True,
            spot_meta=empty_spot,
            perp_dexs=[dex],
        )
        _dex_exchanges[dex] = Exchange(
            wallet,
            api_url,
            spot_meta=empty_spot,
            perp_dexs=[dex],
        )
    _active_symbols = set()
    _account_abstraction = _detect_account_abstraction()
    universe_symbols: set[str] = set()
    universe_symbols.update(asset["name"] for asset in _info.meta()["universe"])
    for dex, info in _dex_infos.items():
        universe_symbols.update(asset["name"] for asset in info.meta(dex)["universe"])

    # Set leverage per configured asset
    for asset_cfg in assets:
        if not asset_cfg.hedge_enabled:
            log.warning(
                "Hedging disabled by config for %s (%s)",
                asset_cfg.name.upper(),
                asset_cfg.hedge_symbol,
            )
            continue
        if asset_cfg.hedge_symbol not in universe_symbols:
            log.error(
                "Hyperliquid symbol unavailable for %s: %s",
                asset_cfg.name.upper(),
                asset_cfg.hedge_symbol,
            )
            continue
        try:
            dex = _dex_for_symbol(asset_cfg.hedge_symbol)
            exchange = _dex_exchanges.get(dex, _exchange)
            if exchange is None:
                raise RuntimeError("Hyperliquid exchange client not initialized")
            exchange.update_leverage(
                asset_cfg.leverage, asset_cfg.hedge_symbol, is_cross=True
            )
            _active_symbols.add(asset_cfg.hedge_symbol)
            log.info(
                "Leverage set: %s=%dx",
                asset_cfg.hedge_symbol,
                asset_cfg.leverage,
            )
        except Exception:
            log.error(
                "Failed to set leverage for %s",
                asset_cfg.hedge_symbol,
                exc_info=True,
            )
            continue

    if not _active_symbols:
        log.error("No Hyperliquid hedge symbols were initialized successfully")
        _exchange = None
        return

    log.info(
        "Hyperliquid ready: %s, assets=%s, testnet=%s abstraction=%s",
        _address,
        sorted(_active_symbols),
        config.HYPERLIQUID_TESTNET,
        _account_abstraction,
    )
    _log_account_state()


def is_hedge_ready(asset: str) -> bool:
    """Whether a hedge symbol is initialized and usable in live mode."""
    if config.HEDGE_MODE != "live":
        return True
    return asset in _active_symbols and _exchange is not None and _info is not None


def _log_account_state() -> None:
    if not _info:
        return
    if _is_unified_account_mode():
        usdc_total, usdc_hold = _spot_token_balance("USDC")
        log.info(
            "Hyperliquid unified account: USDC total=$%.2f hold=$%.2f free=$%.2f",
            usdc_total,
            usdc_hold,
            max(usdc_total - usdc_hold, 0.0),
        )
        return
    states = _collect_states()
    if not states:
        return

    total_value = 0.0
    total_withdrawable = 0.0
    for dex, state in states:
        margin = state["marginSummary"]
        total_value += float(margin["accountValue"])
        total_withdrawable += float(state["withdrawable"])
        log.info(
            "Hyperliquid account [%s]: value=$%s withdrawable=$%s",
            dex or "default",
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
    if len(states) > 1:
        log.info(
            "Hyperliquid total: value=$%.2f withdrawable=$%.2f",
            total_value,
            total_withdrawable,
        )


def _round_size(asset: str, size: float) -> float:
    """Round size to asset's allowed decimal places."""
    try:
        dex = _dex_for_symbol(asset)
        info = _dex_infos.get(dex) if dex else _info
        if info and hasattr(info, "coin_to_asset"):
            coin = info.name_to_coin.get(asset, asset)
            asset_id = info.coin_to_asset.get(coin)
            if isinstance(asset_id, int):
                decimals = info.asset_to_sz_decimals.get(asset_id, 4)
                return round(size, decimals)
    except (AttributeError, TypeError):
        log.warning(
            "Failed to look up size decimals for %s, defaulting to 4",
            asset,
            exc_info=True,
        )
    return round(size, 4)


def _exchange_for_symbol(asset: str) -> Exchange | None:
    dex = _dex_for_symbol(asset)
    if dex:
        return _dex_exchanges.get(dex)
    return _exchange


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

    exchange = _exchange_for_symbol(asset)
    if not exchange:
        log.error("Hyperliquid not initialized, cannot hedge")
        return None

    size = _round_size(asset, size)
    if size <= 0:
        log.warning("[HEDGE] Size rounds to 0, skipping")
        return None

    try:
        result = exchange.market_open(
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

    exchange = _exchange_for_symbol(asset)
    if not exchange:
        log.error("Hyperliquid not initialized, cannot close")
        return None

    try:
        if size is not None:
            size = _round_size(asset, size)
            result = exchange.market_close(
                asset, sz=size, slippage=config.HEDGE_SLIPPAGE
            )
        else:
            result = exchange.market_close(asset, slippage=config.HEDGE_SLIPPAGE)

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


def get_positions(asset: str | None = None) -> list[dict]:
    """Get current Hyperliquid positions."""
    if not _info:
        return []
    try:
        positions = []
        for dex, state in _collect_states():
            for pos in state["assetPositions"]:
                p = pos["position"]
                if asset is not None and p["coin"] != asset:
                    continue
                positions.append(
                    {
                        "coin": p["coin"],
                        "size": float(p["szi"]),
                        "entry_price": float(p["entryPx"]),
                        "unrealized_pnl": float(p["unrealizedPnl"]),
                        "leverage": p["leverage"],
                        "dex": dex,
                    }
                )
        return positions
    except Exception:
        log.warning("Failed to get Hyperliquid positions", exc_info=True)
        return []


def get_account_value(asset: str | None = None) -> float:
    """Get account value in USD for one symbol's dex or all initialized dexs."""
    if not _info:
        return 0.0
    try:
        if _is_unified_account_mode():
            usdc_total, _ = _spot_token_balance("USDC")
            return usdc_total
        if asset is not None:
            state = _state_for_dex(_dex_for_symbol(asset))
            return float(state["marginSummary"]["accountValue"])
        return sum(
            float(state["marginSummary"]["accountValue"])
            for _, state in _collect_states()
        )
    except Exception:
        log.warning("Failed to get account value", exc_info=True)
        return 0.0


def get_withdrawable(asset: str | None = None) -> float:
    """Get withdrawable margin in USD for one symbol's dex or all initialized dexs."""
    if not _info:
        return 0.0
    try:
        if _is_unified_account_mode():
            usdc_total, usdc_hold = _spot_token_balance("USDC")
            return max(usdc_total - usdc_hold, 0.0)
        if asset is not None:
            state = _state_for_dex(_dex_for_symbol(asset))
            return float(state["withdrawable"])
        return sum(float(state["withdrawable"]) for _, state in _collect_states())
    except Exception:
        log.warning("Failed to get withdrawable margin", exc_info=True)
        return 0.0
