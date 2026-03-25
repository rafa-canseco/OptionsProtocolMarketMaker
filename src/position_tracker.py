"""Track open positions and portfolio-level net delta."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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
            return "SHORT"
        return "LONG"

    def time_to_expiry_years(self) -> float:
        seconds = self.expiry - int(time.time())
        if seconds <= 0:
            return 0.0
        return seconds / (365 * 86400)

    def is_expired(self) -> bool:
        return int(time.time()) >= self.expiry


HEDGE_REBALANCE_THRESHOLD = 0.001  # minimum ETH diff to trigger rebalance


class PositionTracker:
    def __init__(self) -> None:
        self.positions: list[Position] = []
        self._otoken_cache: dict[str, dict[str, Any]] = {}
        self._simulated_hedge: dict[str, float] = {}

    def cache_otokens(
        self, otokens: list[dict[str, Any]], underlying: str | None = None
    ) -> None:
        for ot in otokens:
            entry = dict(ot)
            if underlying:
                entry["underlying"] = underlying
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
            underlying=underlying,
            hedge_symbol=hedge_symbol,
            current_delta=delta,
            current_gamma=gamma,
            current_vega=vega,
            current_theta=theta,
        )
        self.positions.append(pos)

        spread_usd = pos.premium_paid_usd - theo * pos.num_options
        _log_position_open(pos, spot, theo, spread_usd)

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
        )

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
        self, spot: float, underlying: str, hedge_symbol: str
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
            except Exception:
                log.error(
                    "[AGGREGATE HEDGE] Failed to read HL positions"
                    " for %s, skipping rebalance",
                    hedge_symbol,
                )
                return None
            if not hl_positions and self._simulated_hedge.get(underlying):
                log.warning(
                    "[AGGREGATE HEDGE] HL returned empty positions"
                    " but expected hedge for %s, skipping",
                    hedge_symbol,
                )
                return None
            current_pos = next(
                (p for p in hl_positions if p["coin"] == hedge_symbol),
                None,
            )
            current_size = current_pos["size"] if current_pos else 0.0
        else:
            current_size = self._simulated_hedge.get(underlying, 0.0)

        diff = net_d - current_size

        if abs(diff) < HEDGE_REBALANCE_THRESHOLD:
            return None

        is_buy = bool(diff > 0)
        fill = hedge_executor.open_hedge(hedge_symbol, is_buy, float(abs(diff)))

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
            log.error(
                "[AGGREGATE HEDGE] LIVE HEDGE FAILED %s %s %.4f"
                " (net_delta=%.4f current=%.4f)",
                "BUY" if is_buy else "SELL",
                hedge_symbol,
                abs(diff),
                net_d,
                current_size,
            )
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
            self._simulated_hedge[underlying] = net_d

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
    hedge_size = pos.hedge_fill_size or pos.hedge_size

    if pos.is_put:
        pos.hedge_pnl = (entry - exit_price) * hedge_size
    else:
        pos.hedge_pnl = (exit_price - entry) * hedge_size


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
        pos.hedge_size,
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
