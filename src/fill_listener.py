"""Real-time fill listener via WebSocket.

Connects to the backend /mm/stream endpoint and logs fills as they
arrive. Runs in a daemon thread so the main quote loop is not blocked.

Auto-reconnects with exponential backoff on disconnect.
"""

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import websocket

from src.api_client import ws_url
from src.config import MM_API_KEY

log = logging.getLogger(__name__)

_MAX_RECENT = 50
_INITIAL_BACKOFF = 2
_MAX_BACKOFF = 60

_recent_fills: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT)
_connected = threading.Event()
_on_fill_callback: Callable[[dict[str, Any]], None] | None = None


def get_recent_fills() -> list[dict[str, Any]]:
    """Return up to the last 50 fills received via WebSocket."""
    return list(_recent_fills)


def is_connected() -> bool:
    return _connected.is_set()


def set_on_fill(
    callback: Callable[[dict[str, Any]], None],
) -> None:
    """Register a callback invoked on each fill: callback(fill_dict)."""
    global _on_fill_callback
    _on_fill_callback = callback


def _on_message(ws: websocket.WebSocketApp, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("WS: non-JSON message: %s", raw[:200])
        return

    msg_type = msg.get("type")

    if msg_type == "auth":
        if msg.get("status") == "ok":
            log.info("WS authenticated as %s", msg.get("mm_address"))
        else:
            log.error("WS auth failed: %s", msg)
        return

    if msg_type == "fill":
        fill = msg.get("data", {})
        _recent_fills.appendleft(fill)
        log.info(
            "FILL: otoken=%s amount=%s premium=%s user=%s tx=%s",
            _short(fill.get("otoken_address")),
            fill.get("amount"),
            fill.get("gross_premium"),
            _short(fill.get("user_address")),
            _short(fill.get("tx_hash")),
        )
        if _on_fill_callback:
            try:
                _on_fill_callback(fill)
            except Exception:
                log.error(
                    "Fill callback failed for tx=%s",
                    fill.get("tx_hash", "?")[:16],
                    exc_info=True,
                )
        return

    if msg_type == "error":
        log.error("WS error from server: %s", msg.get("message"))
        return

    log.debug("WS unknown message type: %s", msg_type)


def _on_open(ws: websocket.WebSocketApp) -> None:
    _connected.set()
    log.info("WS connected to /mm/stream")


def _on_close(ws: websocket.WebSocketApp, code: int | None, reason: str | None) -> None:
    _connected.clear()
    log.warning("WS closed: code=%s reason=%s", code, reason)


def _on_error(ws: websocket.WebSocketApp, error: Exception) -> None:
    _connected.clear()
    log.warning("WS error: %s", error)


def _short(val: str | None, n: int = 10) -> str:
    if not val:
        return "?"
    return val[:n] + "..." if len(val) > n else val


def _run_forever() -> None:
    """Connect with auto-reconnect and exponential backoff."""
    backoff = _INITIAL_BACKOFF
    url = ws_url("/mm/stream", api_key=MM_API_KEY)

    while True:
        ws = websocket.WebSocketApp(
            url,
            on_open=_on_open,
            on_message=_on_message,
            on_close=_on_close,
            on_error=_on_error,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

        # If we get here, connection dropped — reconnect
        _connected.clear()
        log.info("WS reconnecting in %ds...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


def start() -> threading.Thread:
    """Start the fill listener in a daemon thread. Returns the thread."""
    t = threading.Thread(target=_run_forever, name="fill-listener", daemon=True)
    t.start()
    log.info("Fill listener thread started")
    return t
