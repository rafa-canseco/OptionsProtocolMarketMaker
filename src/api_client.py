import logging
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from eth_account import Account

from src.config import BACKEND_URL, MM_API_KEY, MM_PRIVATE_KEY

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"X-API-Key": MM_API_KEY})

_TIMEOUT = 15
_MATERIALIZATION_TIMEOUT = 180


def _url(path: str) -> str:
    return f"{BACKEND_URL}{path}"


def ws_url(path: str, **params: str) -> str:
    """Build a WebSocket URL from BACKEND_URL, converting http→ws."""
    parsed = urlparse(f"{BACKEND_URL}{path}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode(params) if params else parsed.query
    return urlunparse((scheme, parsed.netloc, parsed.path, "", query, ""))


def get_snapshot_envelope(*, environment: str, chain_id: int) -> dict[str, Any]:
    """Read one atomic recurrent-state envelope from Backend/Postgres."""
    resp = _SESSION.get(
        _url("/mm/snapshot"),
        params={"environment": environment, "chain_id": chain_id},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Atomic snapshot envelope is not an object")
    return payload


def get_market_data(
    asset: str = "eth",
    chain: str = "base",
) -> dict[str, Any]:
    """GET /mm/market — spot, IV, available oTokens, protocol fee."""
    params: dict[str, str] = {"asset": asset}
    if chain != "base":
        params["chain"] = chain
    resp = _SESSION.get(_url("/mm/market"), params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def require_protocol_fee_match(
    market_data: dict[str, Any], onchain_protocol_fee_bps: int
) -> int:
    """Fail closed when backend pricing and settlement use different fees."""
    try:
        backend_protocol_fee_bps = int(market_data["protocol_fee_bps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Backend market data does not contain a valid protocol fee"
        ) from exc
    if not 0 <= backend_protocol_fee_bps <= 10_000:
        raise RuntimeError("Backend protocol fee is outside the valid BPS range")
    if backend_protocol_fee_bps != onchain_protocol_fee_bps:
        raise RuntimeError(
            "Backend protocol fee does not match the on-chain BatchSettler"
        )
    return backend_protocol_fee_bps


def submit_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    """POST /mm/quotes — submit signed quotes."""
    resp = _SESSION.post(
        _url("/mm/quotes"),
        json={"quotes": quotes},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_quotes() -> list[dict[str, Any]]:
    """GET /mm/quotes — active signed quotes for this market maker."""
    resp = _SESSION.get(_url("/mm/quotes"), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_meta_wheel_nav_observation(
    fund_key: str, *, snapshot_block: int
) -> dict[str, Any]:
    """Return the backend's authoritative, block-bound Meta Wheel NAV evidence."""
    if not fund_key or snapshot_block <= 0:
        raise RuntimeError("Invalid Meta Wheel NAV observation request")
    resp = _SESSION.get(
        _url(f"/v2/vaults/{fund_key}/wheel/nav-observation"),
        params={"snapshot_block": snapshot_block},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Meta Wheel NAV observation is not an object")
    return payload


def delete_quotes(chain: str | None = None) -> dict[str, Any]:
    """DELETE /mm/quotes — cancel all active quotes."""
    params: dict[str, str] = {}
    if chain:
        params["chain"] = chain
    resp = _SESSION.delete(_url("/mm/quotes"), params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_fills(since: int | None = None, limit: int = 100) -> list[dict]:
    """GET /mm/fills — recent fills."""
    params: dict[str, Any] = {"limit": limit}
    if since is not None:
        params["since"] = since
    resp = _SESSION.get(_url("/mm/fills"), params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_exposure() -> dict[str, Any]:
    """GET /mm/exposure — risk summary."""
    resp = _SESSION.get(_url("/mm/exposure"), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def report_capacity(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /mm/capacity — report current capacity to backend."""
    resp = _SESSION.post(
        _url("/mm/capacity"),
        json=payload,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_fund_series(
    *,
    adapter_address: str,
    quote: dict[str, Any],
    amount_raw: int,
) -> dict[str, Any]:
    """Request idempotent materialization of one policy-selected fund series."""
    mm_address = Account.from_key(MM_PRIVATE_KEY).address
    payload = {
        "adapter_address": adapter_address,
        "expected_otoken_address": quote["otoken_address"],
        "amount_raw": str(amount_raw),
        "quote": {
            "otoken_address": quote["otoken_address"],
            "bid_price_raw": str(quote["bid_price"]),
            "deadline": str(quote["deadline"]),
            "quote_id": str(quote["quote_id"]),
            "max_amount_raw": str(quote["max_amount"]),
            "maker_nonce": str(quote["maker_nonce"]),
            "signature": quote["signature"],
            "mm_address": mm_address,
        },
    }
    resp = _SESSION.post(
        _url("/mm/series/ensure"),
        json=payload,
        timeout=_MATERIALIZATION_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
