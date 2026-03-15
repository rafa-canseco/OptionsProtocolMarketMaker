"""Track open positions and portfolio-level net delta."""

import logging
import time
from dataclasses import dataclass
from typing import Any

from src import hedge_executor, trade_logger
from src.pricer import bs_delta, bs_price

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
    current_delta: float = 0.0
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
    def hedge_size_eth(self) -> float:
        return abs(self.current_delta) * self.num_options

    def hedge_size_usd(self, spot: float) -> float:
        return self.hedge_size_eth * spot

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


class PositionTracker:
    def __init__(self) -> None:
        self.positions: list[Position] = []
        self._otoken_cache: dict[str, dict[str, Any]] = {}

    def cache_otokens(self, otokens: list[dict[str, Any]]) -> None:
        for ot in otokens:
            self._otoken_cache[ot["address"].lower()] = ot

    def get_otoken_details(self, address: str) -> dict[str, Any] | None:
        return self._otoken_cache.get(address.lower())

    def add_position(
        self,
        fill: dict[str, Any],
        spot: float,
        iv: float,
        risk_free_rate: float,
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
            current_delta=delta,
        )
        self.positions.append(pos)

        spread_usd = pos.premium_paid_usd - theo * pos.num_options
        _log_position_open(pos, spot, theo, spread_usd)

        # Execute hedge
        is_buy = not pos.is_put  # long for calls, short for puts
        hedge_fill = hedge_executor.open_hedge("ETH", is_buy, pos.hedge_size_eth)
        if hedge_fill:
            pos.hedge_fill_size = hedge_fill["size"]
            pos.hedge_fill_price = hedge_fill["avg_price"]

        trade_logger.log_position_opened(
            otoken=pos.otoken_address,
            strike=pos.strike,
            expiry=pos.expiry,
            is_put=pos.is_put,
            amount_eth=pos.num_options,
            premium_usd=pos.premium_paid_usd,
            user_address=pos.user_address,
            tx_hash=pos.tx_hash,
            spot=spot,
            delta=pos.current_delta,
            hedge_action=pos.hedge_action,
            hedge_size_eth=pos.hedge_fill_size or pos.hedge_size_eth,
            hedge_fill_price=pos.hedge_fill_price,
        )

        return pos

    def recalculate_deltas(self, spot: float, iv: float, risk_free_rate: float) -> None:
        for pos in self.open_positions():
            T = pos.time_to_expiry_years()
            old_delta = pos.current_delta
            old_hedge = abs(old_delta) * pos.num_options
            pos.current_delta = bs_delta(
                pos.is_put, spot, pos.strike, T, risk_free_rate, iv
            )
            new_hedge = pos.hedge_size_eth
            if abs(pos.current_delta - old_delta) > 0.02:
                log.info(
                    "[DELTA CHANGE] %s delta %.3f -> %.3f",
                    _option_label(pos),
                    old_delta,
                    pos.current_delta,
                )
                # Adjust hedge if live
                is_buy = not pos.is_put
                adj_fill = hedge_executor.adjust_hedge(
                    "ETH", old_hedge, new_hedge, is_buy
                )
                fill_price = 0.0
                if adj_fill:
                    pos.hedge_fill_size = new_hedge
                    pos.hedge_fill_price = adj_fill["avg_price"]
                    fill_price = adj_fill["avg_price"]

                trade_logger.log_delta_rebalanced(
                    otoken=pos.otoken_address,
                    old_delta=old_delta,
                    new_delta=pos.current_delta,
                    old_hedge=old_hedge,
                    new_hedge=new_hedge,
                    hedge_fill_price=fill_price,
                )

    def check_expiries(self, spot: float) -> list[Position]:
        expired = []
        for pos in self.open_positions():
            if pos.is_expired():
                pos.closed = True
                # Close hedge on Hyperliquid
                close_fill = hedge_executor.close_hedge(
                    "ETH", size=pos.hedge_fill_size or None
                )
                if close_fill:
                    pos.hedge_close_price = close_fill["avg_price"]
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
                )

                expired.append(pos)
        return expired

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if not p.closed]

    def net_delta_eth(self) -> float:
        total = 0.0
        for pos in self.open_positions():
            total += pos.current_delta * pos.num_options
        return total

    def net_delta_usd(self, spot: float) -> float:
        return self.net_delta_eth() * spot

    def total_premium_paid(self) -> float:
        return sum(p.premium_paid_usd for p in self.positions)

    def log_portfolio(self, spot: float) -> None:
        open_pos = self.open_positions()
        if not open_pos:
            return
        net_d = self.net_delta_eth()
        log.info(
            "\n[PORTFOLIO]\n"
            "  Open positions: %d\n"
            "  Net delta: %.4f ETH ($%.2f exposure)\n"
            "  Total premium paid: $%.2f",
            len(open_pos),
            net_d,
            abs(net_d) * spot,
            self.total_premium_paid(),
        )


def _option_label(pos: Position) -> str:
    side = "Put" if pos.is_put else "Call"
    action = "Buy" if pos.is_put else "Sell"
    return f'"{action} ETH at ${pos.strike:,.0f}" ({side})'


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
        "  Action: %s ETH\n"
        "  Size: $%.2f (%.4f ETH @ $%.2f)\n"
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
        pos.hedge_size_usd(spot),
        pos.hedge_size_eth,
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
    hedge_size = pos.hedge_fill_size or pos.hedge_size_eth

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
        "  ETH price at expiry: $%.2f\n"
        "  Settlement: $%.2f (%s)\n"
        "\n"
        "[CLOSE HEDGE]\n"
        "  Action: CLOSE %s ETH\n"
        "  Size: %.4f ETH\n"
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
        spot,
        pos.settlement_pnl,
        settle_note,
        pos.hedge_action,
        pos.hedge_size_eth,
        pos.spot_at_open,
        spot,
        pos.hedge_pnl,
        pos.premium_paid_usd,
        pos.settlement_pnl,
        pos.hedge_pnl,
        net_pnl,
        expiry_note,
    )
