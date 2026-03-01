import logging
from typing import Any

import requests

from src.config import BACKEND_URL, MM_API_KEY

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"X-API-Key": MM_API_KEY})

_TIMEOUT = 15


def _url(path: str) -> str:
    return f"{BACKEND_URL}{path}"


def get_market_data() -> dict[str, Any]:
    """GET /mm/market — spot, IV, available oTokens, protocol fee."""
    resp = _SESSION.get(_url("/mm/market"), timeout=_TIMEOUT)
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


def delete_quotes() -> dict[str, Any]:
    """DELETE /mm/quotes — cancel all active quotes."""
    resp = _SESSION.delete(_url("/mm/quotes"), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_fills(since: int | None = None, limit: int = 100) -> list[dict]:
    """GET /mm/fills — recent fills."""
    params: dict[str, Any] = {"limit": limit}
    if since is not None:
        params["since"] = since
    resp = _SESSION.get(
        _url("/mm/fills"), params=params, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def get_exposure() -> dict[str, Any]:
    """GET /mm/exposure — risk summary."""
    resp = _SESSION.get(_url("/mm/exposure"), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
