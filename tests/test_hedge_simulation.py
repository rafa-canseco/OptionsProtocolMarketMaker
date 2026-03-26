"""End-to-end test for Stage 1 hedge simulation.

Simulates the full flow: fill arrives → position tracked →
hedge calculated → delta recalculated → expiry → P&L report.
No backend/frontend needed.
"""

import logging
import time
from unittest.mock import patch

from src.position_tracker import PositionTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

SPOT = 1974.0
IV = 0.58
RISK_FREE = 0.05


def test_put_position_open():
    """Fill on a put → correct delta, hedge SHORT, simulated output."""
    tracker = PositionTracker()
    strike = 1900.0
    expiry = int(time.time()) + 7 * 86400

    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_OTOKEN",
                "strike_price": strike,
                "expiry": expiry,
                "is_put": True,
            }
        ]
    )

    fill = {
        "otoken_address": "0xPUT_OTOKEN",
        "amount": 50000000,  # 0.5 oTokens (8 dec)
        "gross_premium": 12000000,  # $12 (6 dec)
        "user_address": "0xUSER1",
        "tx_hash": "0xTX1",
    }

    pos = tracker.add_position(fill, SPOT, IV, RISK_FREE)

    assert pos is not None
    assert pos.is_put is True
    assert pos.num_options == 0.5
    assert pos.premium_paid_usd == 12.0
    assert pos.hedge_action == "SHORT"
    assert pos.current_delta < 0
    assert pos.hedge_size > 0
    assert len(tracker.open_positions()) == 1
    assert tracker.net_delta() < 0

    print(f"\n  Put delta: {pos.current_delta:.4f}")
    print(
        f"  Hedge: {pos.hedge_action} {pos.hedge_size:.4f} ETH"
        f" (${pos.hedge_size_usd(SPOT):.2f})"
    )
    print(f"  Net delta: {tracker.net_delta():.4f} ETH")


def test_call_position_open():
    """Fill on a call → correct delta, hedge LONG."""
    tracker = PositionTracker()
    strike = 2100.0
    expiry = int(time.time()) + 7 * 86400

    tracker.cache_otokens(
        [
            {
                "address": "0xCALL_OTOKEN",
                "strike_price": strike,
                "expiry": expiry,
                "is_put": False,
            }
        ]
    )

    fill = {
        "otoken_address": "0xCALL_OTOKEN",
        "amount": 100000000,  # 1.0 oToken
        "gross_premium": 30000000,  # $30
        "user_address": "0xUSER2",
        "tx_hash": "0xTX2",
    }

    pos = tracker.add_position(fill, SPOT, IV, RISK_FREE)

    assert pos is not None
    assert pos.is_put is False
    assert pos.num_options == 1.0
    assert pos.hedge_action == "LONG"
    assert pos.current_delta > 0
    assert len(tracker.open_positions()) == 1

    print(f"\n  Call delta: {pos.current_delta:.4f}")
    print(
        f"  Hedge: {pos.hedge_action} {pos.hedge_size:.4f} ETH"
        f" (${pos.hedge_size_usd(SPOT):.2f})"
    )


def test_delta_recalculation():
    """Delta changes when spot moves."""
    tracker = PositionTracker()
    strike = 1900.0
    expiry = int(time.time()) + 7 * 86400

    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_OTOKEN",
                "strike_price": strike,
                "expiry": expiry,
                "is_put": True,
            }
        ]
    )

    fill = {
        "otoken_address": "0xPUT_OTOKEN",
        "amount": 100000000,
        "gross_premium": 20000000,
        "user_address": "0xUSER",
        "tx_hash": "0xTX",
    }

    pos = tracker.add_position(fill, SPOT, IV, RISK_FREE)
    delta_before = pos.current_delta

    # Price drops toward strike → delta gets more negative
    tracker.recalculate_deltas(1920.0, IV, RISK_FREE)
    delta_after = pos.current_delta

    assert delta_after < delta_before, (
        f"Put delta should be more negative when price drops: "
        f"{delta_before:.4f} -> {delta_after:.4f}"
    )
    print(f"\n  Delta before (spot={SPOT}): {delta_before:.4f}")
    print(f"  Delta after  (spot=1920): {delta_after:.4f}")


def test_portfolio_net_delta():
    """Multiple positions → correct portfolio net delta."""
    tracker = PositionTracker()
    expiry = int(time.time()) + 7 * 86400

    tracker.cache_otokens(
        [
            {
                "address": "0xPUT1",
                "strike_price": 1900.0,
                "expiry": expiry,
                "is_put": True,
            },
            {
                "address": "0xCALL1",
                "strike_price": 2100.0,
                "expiry": expiry,
                "is_put": False,
            },
        ]
    )

    tracker.add_position(
        {
            "otoken_address": "0xPUT1",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU1",
            "tx_hash": "0xT1",
        },
        SPOT,
        IV,
        RISK_FREE,
    )

    tracker.add_position(
        {
            "otoken_address": "0xCALL1",
            "amount": 100000000,
            "gross_premium": 30000000,
            "user_address": "0xU2",
            "tx_hash": "0xT2",
        },
        SPOT,
        IV,
        RISK_FREE,
    )

    assert len(tracker.open_positions()) == 2

    # Put delta is negative, call delta is positive — they offset
    net = tracker.net_delta()
    put_d = tracker.positions[0].current_delta
    call_d = tracker.positions[1].current_delta
    expected = put_d + call_d
    assert abs(net - expected) < 1e-6

    print(f"\n  Put delta:  {put_d:.4f}")
    print(f"  Call delta: {call_d:.4f}")
    print(f"  Net delta:  {net:.4f} ETH (${abs(net) * SPOT:.2f})")
    tracker.log_portfolio(SPOT)


def test_expiry_otm():
    """Put expires OTM → settlement $0, MM lost premium."""
    tracker = PositionTracker()
    # Already expired
    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_OTM",
                "strike_price": 1800.0,
                "expiry": int(time.time()) + 1,
                "is_put": True,
            }
        ]
    )

    pos = tracker.add_position(
        {
            "otoken_address": "0xPUT_OTM",
            "amount": 100000000,
            "gross_premium": 10000000,  # $10
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )

    time.sleep(2)

    expired = tracker.check_expiries(spot=2000.0)
    assert len(expired) == 1
    assert expired[0].closed is True
    assert expired[0].settlement_pnl == 0.0  # OTM

    net_pnl = -pos.premium_paid_usd + pos.settlement_pnl + pos.hedge_pnl
    assert net_pnl < 0  # MM lost money on OTM

    print(f"\n  Premium paid: -${pos.premium_paid_usd:.2f}")
    print(f"  Settlement:    ${pos.settlement_pnl:.2f}")
    print(f"  Hedge P&L:     ${pos.hedge_pnl:.2f}")
    print(f"  Net P&L:       ${net_pnl:.2f}")


def test_expiry_itm():
    """Put expires ITM → MM captures intrinsic value."""
    tracker = PositionTracker()
    strike = 2000.0
    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_ITM",
                "strike_price": strike,
                "expiry": int(time.time()) + 1,
                "is_put": True,
            }
        ]
    )

    pos = tracker.add_position(
        {
            "otoken_address": "0xPUT_ITM",
            "amount": 100000000,  # 1 oToken
            "gross_premium": 15000000,  # $15
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        spot=2020.0,
        iv=IV,
        risk_free_rate=RISK_FREE,
    )

    time.sleep(2)

    # Price dropped to 1800 → ITM by $200
    expired = tracker.check_expiries(spot=1800.0)
    assert len(expired) == 1
    assert expired[0].settlement_pnl == 200.0  # strike - spot

    net_pnl = -pos.premium_paid_usd + pos.settlement_pnl + pos.hedge_pnl
    assert net_pnl > 0  # MM profits on ITM

    print(f"\n  Premium paid: -${pos.premium_paid_usd:.2f}")
    print(f"  Settlement:   +${pos.settlement_pnl:.2f}")
    print(f"  Hedge P&L:    +${pos.hedge_pnl:.2f}")
    print(f"  Net P&L:      +${net_pnl:.2f}")


def test_unknown_otoken_ignored():
    """Fill for unknown oToken is silently ignored."""
    tracker = PositionTracker()
    pos = tracker.add_position(
        {
            "otoken_address": "0xUNKNOWN",
            "amount": 100000000,
            "gross_premium": 10000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )

    assert pos is None
    assert len(tracker.positions) == 0


def test_greeks_computed_on_open():
    """Position has gamma, vega, theta set after add_position."""
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_G",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ]
    )
    pos = tracker.add_position(
        {
            "otoken_address": "0xPUT_G",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    assert pos.current_gamma > 0
    assert pos.current_vega > 0
    assert pos.current_theta < 0  # theta is negative (time decay)


def test_portfolio_greeks_aggregation():
    """Portfolio Greeks aggregate across positions."""
    tracker = PositionTracker()
    expiry = int(time.time()) + 7 * 86400
    tracker.cache_otokens(
        [
            {
                "address": "0xPUT_A",
                "strike_price": 1900.0,
                "expiry": expiry,
                "is_put": True,
            },
            {
                "address": "0xCALL_A",
                "strike_price": 2100.0,
                "expiry": expiry,
                "is_put": False,
            },
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xPUT_A",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU1",
            "tx_hash": "0xT1",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    tracker.add_position(
        {
            "otoken_address": "0xCALL_A",
            "amount": 100000000,
            "gross_premium": 30000000,
            "user_address": "0xU2",
            "tx_hash": "0xT2",
        },
        SPOT,
        IV,
        RISK_FREE,
    )

    g = tracker.portfolio_greeks()
    assert "delta" in g
    assert "gamma" in g
    assert "vega" in g
    assert "theta" in g
    assert g["gamma"] > 0  # gamma is always positive
    assert g["vega"] > 0
    assert g["theta"] < 0  # selling options: theta works against us


def test_inventory_imbalance_all_puts():
    """All-put portfolio returns positive imbalance."""
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xP",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xP",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    assert tracker.inventory_imbalance() > 0.9


def test_inventory_imbalance_balanced():
    """Balanced put+call portfolio returns near zero."""
    tracker = PositionTracker()
    expiry = int(time.time()) + 7 * 86400
    tracker.cache_otokens(
        [
            {
                "address": "0xP",
                "strike_price": 1900.0,
                "expiry": expiry,
                "is_put": True,
            },
            {
                "address": "0xC",
                "strike_price": 2100.0,
                "expiry": expiry,
                "is_put": False,
            },
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xP",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT1",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    tracker.add_position(
        {
            "otoken_address": "0xC",
            "amount": 100000000,
            "gross_premium": 30000000,
            "user_address": "0xU",
            "tx_hash": "0xT2",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    assert abs(tracker.inventory_imbalance()) < 0.5


def test_inventory_imbalance_empty():
    """Empty portfolio returns 0."""
    assert PositionTracker().inventory_imbalance() == 0.0


@patch("src.config.HEDGE_MODE", "simulate")
def test_rebalance_hedge_simulated_mode():
    """Simulated mode tracks hedge state internally."""
    from src import hedge_executor

    hedge_executor._exchange = None
    hedge_executor._info = None
    hedge_executor._address = ""

    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xP",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xP",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    tracker.rebalance_hedge(SPOT, "eth", "ETH")
    assert "eth" in tracker._simulated_hedge
    expected = tracker.net_delta(underlying="eth")
    assert abs(tracker._simulated_hedge["eth"] - expected) < 0.001


def test_rebalance_hedge_threshold_skips_tiny():
    """Diff below threshold does not trigger hedge."""
    tracker = PositionTracker()
    tracker._simulated_hedge["eth"] = -0.001
    tracker.rebalance_hedge(SPOT, "eth", "ETH")
    assert tracker._simulated_hedge["eth"] == -0.001


@patch("src.main.config")
def test_pick_refresh_interval_no_positions(mock_config):
    """No positions → normal interval."""
    mock_config.REFRESH_INTERVAL = 60
    mock_config.REFRESH_INTERVAL_FAST = 30
    mock_config.FAST_REFRESH_HOURS = 6
    from src.main import _pick_refresh_interval, _tracker

    _tracker.positions.clear()
    assert _pick_refresh_interval() == 60


@patch("src.main.config")
def test_pick_refresh_interval_near_expiry(mock_config):
    """Position near expiry → fast interval."""
    mock_config.REFRESH_INTERVAL = 60
    mock_config.REFRESH_INTERVAL_FAST = 30
    mock_config.FAST_REFRESH_HOURS = 6
    from src.main import _pick_refresh_interval, _tracker

    _tracker.positions.clear()
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xNEAR",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 3600,  # 1h away
                "is_put": True,
            }
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xNEAR",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    _tracker.positions.extend(tracker.positions)
    assert _pick_refresh_interval() == 30
    _tracker.positions.clear()


@patch("src.main.config")
def test_pick_refresh_interval_far_expiry(mock_config):
    """Position far from expiry → normal interval."""
    mock_config.REFRESH_INTERVAL = 60
    mock_config.REFRESH_INTERVAL_FAST = 30
    mock_config.FAST_REFRESH_HOURS = 6
    from src.main import _pick_refresh_interval, _tracker

    _tracker.positions.clear()
    tracker = PositionTracker()
    tracker.cache_otokens(
        [
            {
                "address": "0xFAR",
                "strike_price": 1900.0,
                "expiry": int(time.time()) + 30 * 86400,  # 30d away
                "is_put": True,
            }
        ]
    )
    tracker.add_position(
        {
            "otoken_address": "0xFAR",
            "amount": 100000000,
            "gross_premium": 20000000,
            "user_address": "0xU",
            "tx_hash": "0xT",
        },
        SPOT,
        IV,
        RISK_FREE,
    )
    _tracker.positions.extend(tracker.positions)
    assert _pick_refresh_interval() == 60
    _tracker.positions.clear()
