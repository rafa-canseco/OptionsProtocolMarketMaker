"""Track open positions and portfolio-level net delta."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from src import hedge_executor, trade_logger
from src.pricer import bs_delta, bs_gamma, bs_price, bs_theta, bs_vega

log = logging.getLogger(__name__)

OTOKEN_DECIMALS = 8
USDC_DECIMALS = 6


@dataclass
class Position:
    otoken_address: str
    strike: float
    expiry: int
    is_put: bool
    amount_raw: int
    premium_paid_raw: int
    user_address: str
    tx_hash: str
    open_time: int
    spot_at_open: float
    delta_at_open: float
    chain: str = "base"
    underlying: str = "eth"
    hedge_symbol: str = "ETH"
    current_delta: float = 0.0
    current_gamma: float = 0.0
    current_vega: float = 0.0
    current_theta: float = 0.0
    closed: bool = False
    settlement_pnl: float = 0.0
    hedge_pnl: float = 0.0
    # Live hedge tracking
    hedge_fill_size: float = 0.0
    hedge_fill_price: float = 0.0
    hedge_close_price: float = 0.0

    @property
    def num_options(self) -> float:
        return self.amount_raw / 10**OTOKEN_DECIMALS

    @property
    def premium_paid_usd(self) -> float:
        return self.premium_paid_raw / 10**USDC_DECIMALS

    @property
    def notional_usd(self) -> float:
        return self.num_options * self.spot_at_open

    @property
    def hedge_size(self) -> float:
        return abs(self.current_delta) * self.num_options

    def hedge_size_usd(self, spot: float) -> float:
        return self.hedge_size * spot

    @property
    def hedge_action(self) -> str:
        if self.is_put:
            return "LONG"
        return "SHORT"

    def time_to_expiry_years(self) -> float:
        seconds = self.expiry - int(time.time())
        if seconds <= 0:
            return 0.0
        return seconds / (365 * 86400)

    def is_expired(self) -> bool:
        return int(time.time()) >= self.expiry


HEDGE_REBALANCE_THRESHOLD = 0.001  # minimum ETH diff to trigger rebalance
_HEDGE_FILL_WINDOW_MS = 120_000
_MAX_FILL_WINDOWS_PER_ATTEMPT = 10
_PENDING_STATUSES = {"submitted", "partial", "unreconciled"}
_TERMINAL_VENUE_STATUSES = {"filled", "canceled", "cancelled", "rejected"}
_SAFE_HEDGE_ERRORS = {
    "missing_order_id",
    "order_fills_not_visible",
    "order_fills_incomplete",
    "order_not_terminal",
    "venue_order_rejected",
    "venue_order_canceled",
    "live_hedge_order_failed",
}
_pending_records: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_pending_worker_started = False


def reconcile_hedge_execution(record: dict[str, Any]) -> dict[str, Any]:
    """Update one order from authoritative venue status and all visible fills."""
    record["reconcile_attempts"] = int(record.get("reconcile_attempts", 0)) + 1
    record["last_reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if record.get("order_id") is None:
        record.update(status="unreconciled", error="missing_order_id")
        trade_logger.log_hedge_execution(record)
        return record
    oid = int(record["order_id"])
    try:
        response = hedge_executor.get_order_status(record["hedge_symbol"], oid)
        top_status = response.get("status", "") if isinstance(response, dict) else ""
        order = response.get("order", {}) if isinstance(response, dict) else {}
        venue_status = str(order.get("status", top_status)).lower()
        record["venue_order_status"] = venue_status or "unknown"
        started = int(record.get("order_started_ms", 0))
        now_ms = int(time.time() * 1000)
        terminal = venue_status in _TERMINAL_VENUE_STATUSES or venue_status.endswith(
            ("canceled", "rejected")
        )
        status_time = order.get("statusTimestamp") if isinstance(order, dict) else None
        if status_time is None and isinstance(response, dict):
            status_time = response.get("statusTimestamp")
        target_end = (
            int(status_time) + _HEDGE_FILL_WINDOW_MS
            if terminal and status_time is not None
            else now_ms
        )
        cursor = max(started, int(record.get("_fill_cursor_ms", started)))
        fills_by_id = {}
        for fill in record.get("_observed_fills", []):
            tid = fill.get("tid")
            key = (
                ("tid", str(tid))
                if tid is not None
                else (
                    "tuple",
                    str(
                        (
                            fill.get("oid"),
                            fill.get("time"),
                            fill.get("sz"),
                            fill.get("px"),
                            fill.get("fee"),
                        )
                    ),
                )
            )
            fills_by_id[key] = fill
        windows_scanned = 0
        while cursor < target_end and windows_scanned < _MAX_FILL_WINDOWS_PER_ATTEMPT:
            window_end = min(cursor + _HEDGE_FILL_WINDOW_MS, target_end)
            for fill in hedge_executor.get_order_fills(
                record["hedge_symbol"], oid, cursor, window_end
            ):
                tid = fill.get("tid")
                key = (
                    ("tid", str(tid))
                    if tid is not None
                    else (
                        "tuple",
                        str(
                            (
                                fill.get("oid"),
                                fill.get("time"),
                                fill.get("sz"),
                                fill.get("px"),
                                fill.get("fee"),
                            )
                        ),
                    )
                )
                fills_by_id[key] = {
                    field: fill.get(field)
                    for field in ("tid", "oid", "time", "sz", "px", "fee", "feeToken")
                    if field in fill
                }
            cursor = window_end
            windows_scanned += 1
        record["_fill_cursor_ms"] = cursor
        record["_observed_fills"] = list(fills_by_id.values())
        fills = list(fills_by_id.values())
        fill_history_complete = cursor >= target_end
        qty = sum(float(fill.get("sz", 0)) for fill in fills)
        notional = sum(
            float(fill.get("sz", 0)) * float(fill.get("px", 0)) for fill in fills
        )
        record.update(
            filled_size=qty,
            average_fill_price=notional / qty if qty else None,
            fee=sum(float(fill.get("fee", 0)) for fill in fills),
            fee_token=next(
                (fill.get("feeToken") for fill in fills if fill.get("feeToken")), None
            ),
            fill_count=len(fills),
            error=None,
        )
        terminal = venue_status in _TERMINAL_VENUE_STATUSES or venue_status.endswith(
            ("canceled", "rejected")
        )
        if venue_status == "unknownoid" or not terminal:
            record.update(status="unreconciled", error="order_not_terminal")
        elif not fill_history_complete:
            record.update(status="unreconciled", error="order_fills_incomplete")
        elif venue_status == "filled" and not fills:
            record.update(status="unreconciled", error="order_fills_not_visible")
        elif venue_status == "filled" and qty < float(record["requested_size"]) - 1e-9:
            record.update(status="unreconciled", error="order_fills_incomplete")
        elif venue_status == "filled":
            record.update(status="filled")
        elif qty:
            record.update(status="partial")
        else:
            record.update(
                status="failed",
                error="venue_order_rejected"
                if "reject" in venue_status
                else "venue_order_canceled",
            )
        if fills:
            positions = hedge_executor.get_positions_strict(record["hedge_symbol"])
            observed = next(
                (p["size"] for p in positions if p["coin"] == record["hedge_symbol"]),
                0.0,
            )
            record.update(
                observed_post_position=observed,
                residual_delta=observed + float(record["net_option_delta"]),
            )
    except Exception as exc:
        record.update(status="unreconciled", error=type(exc).__name__)
    trade_logger.log_hedge_execution(record)
    return record


def _is_terminal_partial(record: dict[str, Any]) -> bool:
    return record.get("status") == "partial" and str(
        record.get("venue_order_status", "")
    ).endswith(("canceled", "cancelled", "rejected"))


def _pending_worker() -> None:
    while True:
        time.sleep(0.25)
        with _pending_lock:
            records = [
                (key, current, current.copy())
                for key, current in _pending_records.items()
            ]
        for key, registered, record in records:
            if float(record.get("_next_retry_at", 0)) > time.time():
                continue
            result = reconcile_hedge_execution(record)
            with _pending_lock:
                if _pending_records.get(key) is not registered:
                    continue
                if result["status"] in {"filled", "failed"} or _is_terminal_partial(
                    result
                ):
                    _pending_records.pop(key, None)
                else:
                    attempts = int(result.get("_retry_attempts", 0)) + 1
                    result["_retry_attempts"] = attempts
                    result["_next_retry_at"] = time.time() + min(2**attempts, 60)
                    _pending_records[key] = result
                    trade_logger.log_hedge_execution(result)


def _ensure_pending_worker() -> None:
    global _pending_worker_started
    with _pending_lock:
        if not _pending_worker_started:
            _pending_worker_started = True
            threading.Thread(
                target=_pending_worker, name="hedge-pending-worker", daemon=True
            ).start()


def _schedule_hedge_reconciliation(record: dict[str, Any]) -> None:
    key = str(record.get("rebalance_id", record.get("order_id", id(record))))
    if record.get("execution_mode") != "live" or record.get("order_id") is None:
        return
    pending = record.copy()
    pending.setdefault("_retry_attempts", 0)
    pending.setdefault("_next_retry_at", time.time())
    with _pending_lock:
        existing = _pending_records.get(key)
        if existing is not None and existing.get("order_id") == pending.get("order_id"):
            return
        _pending_records[key] = pending
    _ensure_pending_worker()


def resume_pending_hedge_reconciliations() -> int:
    """Rebuild latest pending executions from the durable JSONL log."""
    trade_logger.start_hedge_upsert_replay()
    latest: dict[str, dict[str, Any]] = {}
    for event in trade_logger.read_events():
        if event.get("event") == "hedge_execution" and event.get("rebalance_id"):
            latest[event["rebalance_id"]] = event
    resumed = [
        event
        for event in latest.values()
        if event.get("execution_mode") == "live"
        and event.get("order_id") is not None
        and event.get("status") in _PENDING_STATUSES
        and not _is_terminal_partial(event)
    ]
    for record in resumed:
        _schedule_hedge_reconciliation(record)
    return len(resumed)


class PositionTracker:
    def __init__(self) -> None:
        self.positions: list[Position] = []
        self._otoken_cache: dict[str, dict[str, Any]] = {}
        self._simulated_hedge: dict[str, float] = {}

    def cache_otokens(
        self,
        otokens: list[dict[str, Any]],
        underlying: str | None = None,
        chain: str | None = None,
    ) -> None:
        for ot in otokens:
            entry = dict(ot)
            if underlying:
                entry["underlying"] = underlying
            if chain:
                entry["chain"] = chain
            self._otoken_cache[entry["address"].lower()] = entry

    def get_otoken_details(self, address: str) -> dict[str, Any] | None:
        return self._otoken_cache.get(address.lower())

    def add_position(
        self,
        fill: dict[str, Any],
        spot: float,
        iv: float,
        risk_free_rate: float,
        underlying: str = "eth",
        hedge_symbol: str = "ETH",
        *,
        decision_validator: Callable[[], None] | None = None,
    ) -> Position | None:
        otoken_addr = fill.get("otoken_address", "")
        details = self.get_otoken_details(otoken_addr)
        if not details:
            log.warning(
                "Unknown oToken %s, cannot track position",
                otoken_addr,
            )
            return None

        strike = details["strike_price"]
        expiry = details["expiry"]
        is_put = details["is_put"]
        chain = details.get("chain", "base")

        T = max((expiry - int(time.time())) / (365 * 86400), 0.0)
        delta = bs_delta(is_put, spot, strike, T, risk_free_rate, iv)
        gamma = bs_gamma(spot, strike, T, risk_free_rate, iv)
        vega = bs_vega(spot, strike, T, risk_free_rate, iv)
        theta = bs_theta(is_put, spot, strike, T, risk_free_rate, iv)
        theo = bs_price(is_put, spot, strike, T, risk_free_rate, iv)

        amount_raw = int(fill.get("amount", 0))
        premium_raw = int(fill.get("gross_premium", 0))

        pos = Position(
            otoken_address=otoken_addr,
            strike=strike,
            expiry=expiry,
            is_put=is_put,
            amount_raw=amount_raw,
            premium_paid_raw=premium_raw,
            user_address=fill.get("user_address", ""),
            tx_hash=fill.get("tx_hash", ""),
            open_time=int(time.time()),
            spot_at_open=spot,
            delta_at_open=delta,
            chain=chain,
            underlying=underlying,
            hedge_symbol=hedge_symbol,
            current_delta=delta,
            current_gamma=gamma,
            current_vega=vega,
            current_theta=theta,
        )
        if decision_validator is not None:
            decision_validator()
        self.positions.append(pos)
        try:
            if decision_validator is not None:
                decision_validator()
            trade_logger.log_position_opened(
                otoken=pos.otoken_address,
                strike=pos.strike,
                expiry=pos.expiry,
                is_put=pos.is_put,
                amount=pos.num_options,
                premium_usd=pos.premium_paid_usd,
                user_address=pos.user_address,
                tx_hash=pos.tx_hash,
                spot=spot,
                delta=pos.current_delta,
                hedge_action=pos.hedge_action,
                hedge_size=pos.hedge_fill_size or pos.hedge_size,
                hedge_fill_price=pos.hedge_fill_price,
                underlying=underlying,
                chain=chain,
            )
        except Exception:
            self.positions.remove(pos)
            raise

        spread_usd = pos.premium_paid_usd - theo * pos.num_options
        _log_position_open(pos, spot, theo, spread_usd)
        return pos

    def recalculate_deltas(
        self,
        spot: float,
        iv: float,
        risk_free_rate: float,
        underlying: str | None = None,
    ) -> None:
        for pos in self.open_positions(underlying=underlying):
            T = pos.time_to_expiry_years()
            old_delta = pos.current_delta
            pos.current_delta = bs_delta(
                pos.is_put, spot, pos.strike, T, risk_free_rate, iv
            )
            pos.current_gamma = bs_gamma(spot, pos.strike, T, risk_free_rate, iv)
            pos.current_vega = bs_vega(spot, pos.strike, T, risk_free_rate, iv)
            pos.current_theta = bs_theta(
                pos.is_put, spot, pos.strike, T, risk_free_rate, iv
            )
            new_hedge = pos.hedge_size
            if abs(pos.current_delta - old_delta) > 0.02:
                old_hedge = abs(old_delta) * pos.num_options
                log.info(
                    "[DELTA CHANGE] %s delta %.3f -> %.3f",
                    _option_label(pos),
                    old_delta,
                    pos.current_delta,
                )
                trade_logger.log_delta_rebalanced(
                    otoken=pos.otoken_address,
                    old_delta=old_delta,
                    new_delta=pos.current_delta,
                    old_hedge=old_hedge,
                    new_hedge=new_hedge,
                    hedge_fill_price=0.0,
                    underlying=pos.underlying,
                    chain=pos.chain,
                )

    def check_expiries(
        self, spot: float, underlying: str | None = None
    ) -> list[Position]:
        expired = []
        for pos in self.open_positions(underlying=underlying):
            if pos.is_expired():
                pos.closed = True
                _calculate_expiry_pnl(pos, spot)
                _log_expiry(pos, spot)

                itm = (pos.is_put and spot < pos.strike) or (
                    not pos.is_put and spot > pos.strike
                )
                net_pnl = -pos.premium_paid_usd + pos.settlement_pnl + pos.hedge_pnl
                trade_logger.log_position_expired(
                    otoken=pos.otoken_address,
                    settlement="ITM" if itm else "OTM",
                    expiry_price=spot,
                    settlement_pnl=pos.settlement_pnl,
                    hedge_pnl=pos.hedge_pnl,
                    hedge_close_price=pos.hedge_close_price,
                    net_pnl=net_pnl,
                    underlying=pos.underlying,
                    chain=pos.chain,
                )

                expired.append(pos)
        return expired

    def open_positions(self, underlying: str | None = None) -> list[Position]:
        positions = [p for p in self.positions if not p.closed]
        if underlying is not None:
            positions = [p for p in positions if p.underlying == underlying]
        return positions

    def net_delta(self, underlying: str | None = None) -> float:
        total = 0.0
        for pos in self.open_positions(underlying=underlying):
            total += pos.current_delta * pos.num_options
        return total

    def net_delta_usd(self, spot: float, underlying: str | None = None) -> float:
        return self.net_delta(underlying=underlying) * spot

    def portfolio_greeks(self, underlying: str | None = None) -> dict[str, float]:
        """Aggregate Greeks across open positions."""
        delta = 0.0
        gamma = 0.0
        vega = 0.0
        theta = 0.0
        for pos in self.open_positions(underlying=underlying):
            n = pos.num_options
            delta += pos.current_delta * n
            gamma += pos.current_gamma * n
            vega += pos.current_vega * n
            theta += pos.current_theta * n
        return {
            "delta": delta,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
        }

    def deployed_usd(self, underlying: str | None = None) -> float:
        return sum(p.notional_usd for p in self.open_positions(underlying=underlying))

    def total_premium_paid(self, underlying: str | None = None) -> float:
        positions = self.positions
        if underlying is not None:
            positions = [p for p in positions if p.underlying == underlying]
        return sum(p.premium_paid_usd for p in positions)

    def log_portfolio(self, spot: float) -> None:
        open_pos = self.open_positions()
        if not open_pos:
            return
        g = self.portfolio_greeks()
        log.info(
            "\n[PORTFOLIO]\n"
            "  Open positions: %d\n"
            "  Delta: %.4f ($%.2f exposure)\n"
            "  Gamma: %.6f | Vega: $%.2f | Theta: $%.2f/day\n"
            "  Total premium paid: $%.2f",
            len(open_pos),
            g["delta"],
            abs(g["delta"]) * spot,
            g["gamma"],
            g["vega"],
            g["theta"],
            self.total_premium_paid(),
        )

    def inventory_imbalance(self, underlying: str | None = None) -> float:
        """Ratio from -1 (all calls) to +1 (all puts). 0 = balanced."""
        put_delta = 0.0
        call_delta = 0.0
        for pos in self.open_positions(underlying=underlying):
            contribution = abs(pos.current_delta * pos.num_options)
            if pos.is_put:
                put_delta += contribution
            else:
                call_delta += contribution
        total = put_delta + call_delta
        if total == 0:
            return 0.0
        return (put_delta - call_delta) / total

    def rebalance_hedge(
        self,
        spot: float,
        underlying: str,
        hedge_symbol: str,
        *,
        decision_validator: Callable[[], None] | None = None,
    ) -> dict | None:
        """Adjust aggregate hedge to match portfolio net delta.

        Instead of hedging each position individually, maintains ONE
        hedge per underlying on Hyperliquid sized to net portfolio delta.
        """
        from src import config

        net_d = self.net_delta(underlying=underlying)

        if config.HEDGE_MODE == "live":
            try:
                hl_positions = hedge_executor.get_positions()
            except Exception as error:
                raise RuntimeError(
                    f"Failed to read live hedge positions for {hedge_symbol}"
                ) from error
            if not hl_positions and self._simulated_hedge.get(underlying):
                raise RuntimeError(
                    f"Live hedge positions are unexpectedly empty for {hedge_symbol}"
                )
            current_pos = next(
                (p for p in hl_positions if p["coin"] == hedge_symbol),
                None,
            )
            current_size = current_pos["size"] if current_pos else 0.0
        else:
            current_size = self._simulated_hedge.get(underlying, 0.0)

        target_size = -net_d
        diff = target_size - current_size
        rebalance_id = str(uuid.uuid4())
        record = {
            "rebalance_id": rebalance_id,
            "chain": next(
                (
                    p.chain
                    for p in self.open_positions(underlying)
                    if p.hedge_symbol == hedge_symbol
                ),
                "base",
            ),
            "underlying": underlying,
            "hedge_symbol": hedge_symbol,
            "decision_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "execution_mode": config.HEDGE_MODE,
            "configured_threshold": HEDGE_REBALANCE_THRESHOLD,
            "spot": spot,
            "decision_reason": "threshold_noop"
            if abs(diff) <= HEDGE_REBALANCE_THRESHOLD
            else "rebalance",
            "side": None
            if abs(diff) <= HEDGE_REBALANCE_THRESHOLD
            else ("BUY" if diff > 0 else "SELL"),
            "status": "noop" if abs(diff) <= HEDGE_REBALANCE_THRESHOLD else "submitted",
            "net_option_delta": net_d,
            "current_hedge_position": current_size,
            "target_hedge_position": target_size,
            "signed_difference": diff,
            "expected_post_position": target_size,
            "requested_size": abs(diff),
            "filled_size": 0.0,
            "fee": 0.0,
            "fill_count": 0,
        }
        if abs(diff) <= HEDGE_REBALANCE_THRESHOLD:
            trade_logger.log_hedge_execution(record)
            return None

        is_buy = bool(diff > 0)
        if decision_validator is not None:
            decision_validator()
        order_started_ms = int(time.time() * 1000)
        try:
            fill = hedge_executor.open_hedge(hedge_symbol, is_buy, float(abs(diff)))
        except Exception as error:
            record.update(status="failed", error=type(error).__name__)
            trade_logger.log_hedge_execution(record)
            raise
        if not fill and config.HEDGE_MODE == "live":
            record.update(status="failed", error="live_hedge_order_failed")
            trade_logger.log_hedge_execution(record)
            raise RuntimeError(f"Live hedge order failed for {hedge_symbol}")
        if fill:
            record.update(
                order_id=fill.get("oid"),
                order_started_ms=order_started_ms,
                status="submitted" if fill.get("oid") is not None else "unreconciled",
                error=None if fill.get("oid") is not None else "missing_order_id",
            )
            trade_logger.log_hedge_execution(record)
            if fill.get("oid") is not None:
                _schedule_hedge_reconciliation(record)

        if fill:
            log.info(
                "[AGGREGATE HEDGE] %s %s %.4f filled @ $%.2f"
                " (net_delta=%.4f current=%.4f)",
                "BUY" if is_buy else "SELL",
                hedge_symbol,
                fill["size"],
                fill["avg_price"],
                net_d,
                current_size,
            )
        elif config.HEDGE_MODE == "live":
            raise RuntimeError(f"Live hedge order failed for {hedge_symbol}")
        else:
            log.info(
                "[AGGREGATE HEDGE] %s %s %.4f (simulated)"
                " (net_delta=%.4f current=%.4f)",
                "BUY" if is_buy else "SELL",
                hedge_symbol,
                abs(diff),
                net_d,
                current_size,
            )
            if decision_validator is not None:
                decision_validator()
            self._simulated_hedge[underlying] = target_size
            record.update(status="submitted")
            trade_logger.log_hedge_execution(record)

        return fill


def _option_label(pos: Position) -> str:
    side = "Put" if pos.is_put else "Call"
    action = "Buy" if pos.is_put else "Sell"
    symbol = pos.underlying.upper()
    return f'"{action} {symbol} at ${pos.strike:,.0f}" ({side})'


def _log_position_open(
    pos: Position, spot: float, theo: float, spread_usd: float
) -> None:
    label = _option_label(pos)
    days = (pos.expiry - pos.open_time) / 86400
    log.info(
        "\n[POSITION TAKEN] User bought %s\n"
        "  Notional: $%.2f | Premium PAID to user: $%.2f\n"
        "  Option type: %s | Strike: $%.0f | Expiry: %.0fd\n"
        "  Theoretical value: $%.2f | Spread: $%.2f\n"
        "  Delta: %.3f\n"
        "\n"
        "[HEDGE REQUIRED]\n"
        "  Action: %s %s\n"
        "  Size: $%.2f (%.4f %s @ $%.2f)\n"
        "  Venue: Hyperliquid",
        label,
        pos.notional_usd,
        pos.premium_paid_usd,
        "PUT" if pos.is_put else "CALL",
        pos.strike,
        days,
        theo * pos.num_options,
        spread_usd,
        pos.current_delta,
        pos.hedge_action,
        pos.underlying.upper(),
        pos.hedge_size_usd(spot),
        pos.hedge_size,
        pos.underlying.upper(),
        spot,
    )


def _calculate_expiry_pnl(pos: Position, spot: float) -> None:
    if pos.is_put:
        intrinsic = max(pos.strike - spot, 0.0)
    else:
        intrinsic = max(spot - pos.strike, 0.0)
    pos.settlement_pnl = intrinsic * pos.num_options

    # Use real fill prices if available, otherwise theoretical
    entry = pos.hedge_fill_price or pos.spot_at_open
    exit_price = pos.hedge_close_price or spot
    hedge_size = abs(pos.hedge_fill_size) or pos.hedge_size

    if pos.is_put:
        pos.hedge_pnl = (exit_price - entry) * hedge_size
    else:
        pos.hedge_pnl = (entry - exit_price) * hedge_size


def _log_expiry(pos: Position, spot: float) -> None:
    label = _option_label(pos)
    itm = (pos.is_put and spot < pos.strike) or (not pos.is_put and spot > pos.strike)
    status = "ITM" if itm else "OTM"

    net_pnl = -pos.premium_paid_usd + pos.settlement_pnl + pos.hedge_pnl

    settle_note = (
        f"{status}, collateral returned"
        if not itm
        else f"{status}, intrinsic value captured"
    )
    expiry_note = (
        "OTM = MM lost premium + hedge costs."
        if not itm
        else "ITM = MM profits from settlement."
    )
    log.info(
        "\n[EXPIRY] %s expired %s\n"
        "  %s price at expiry: $%.2f\n"
        "  Settlement: $%.2f (%s)\n"
        "\n"
        "[CLOSE HEDGE]\n"
        "  Action: CLOSE %s %s\n"
        "  Size: %.4f %s\n"
        "  Entry: $%.2f | Exit: $%.2f\n"
        "  Hedge P&L: %+.2f\n"
        "\n"
        "[POSITION P&L]\n"
        "  Premium paid to user:  -$%.2f\n"
        "  Settlement result:     %+.2f\n"
        "  Hedge P&L:             %+.2f\n"
        "  ----------------------------\n"
        "  Net P&L:               %+.2f\n"
        "  Note: %s",
        label,
        status,
        pos.underlying.upper(),
        spot,
        pos.settlement_pnl,
        settle_note,
        pos.hedge_action,
        pos.underlying.upper(),
        abs(pos.hedge_fill_size) or pos.hedge_size,
        pos.underlying.upper(),
        pos.spot_at_open,
        spot,
        pos.hedge_pnl,
        pos.premium_paid_usd,
        pos.settlement_pnl,
        pos.hedge_pnl,
        net_pnl,
        expiry_note,
    )
