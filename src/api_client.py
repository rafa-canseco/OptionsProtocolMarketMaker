import logging
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests

from src.config import BACKEND_URL, MM_API_KEY

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"X-API-Key": MM_API_KEY})

_TIMEOUT = 15


def _url(path: str) -> str:
    return f"{BACKEND_URL}{path}"


def ws_url(path: str, **params: str) -> str:
    """Build a WebSocket URL from BACKEND_URL, converting http→ws."""
    parsed = urlparse(f"{BACKEND_URL}{path}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode(params) if params else parsed.query
    return urlunparse((scheme, parsed.netloc, parsed.path, "", query, ""))


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


def submit_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    """POST /mm/quotes — submit signed quotes."""
    resp = _SESSION.post(
        _url("/mm/quotes"),
        json={"quotes": quotes},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


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
