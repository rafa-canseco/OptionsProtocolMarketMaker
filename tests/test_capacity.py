"""Tests for capacity calculation module."""

from unittest.mock import MagicMock, patch

import pytest

from src.capacity import (
    CapacityReport,
    calculate_capacity_internal,
    capacity_status,
)


SPOT = 2000.0
LEVERAGE = 3


def _mock_w3(usdc_balance: int, usdc_allowance: int):
    """Build a mock Web3 instance that returns given USDC balance/allowance."""
    w3 = MagicMock()

    def mock_call(tx):
        data = tx["data"]
        # balanceOf selector: 0x70a08231
        if data.startswith("0x70a08231"):
            return usdc_balance.to_bytes(32, "big")
        # allowance selector: 0xdd62ed3e
        if data.startswith("0xdd62ed3e"):
            return usdc_allowance.to_bytes(32, "big")
        return b"\x00" * 32

    w3.eth.call = mock_call
    return w3


class TestCapacityReport:
    def test_dataclass_fields(self):
        report = CapacityReport(
            mm_address="0xABC",
            asset="ETH",
            capacity_eth=10.0,
            capacity_usd=20000.0,
            premium_pool_usd=25000.0,
            hedge_pool_usd=30000.0,
            hedge_pool_withdrawable_usd=10000.0,
            leverage=3,
            open_positions_count=2,
            open_positions_notional_usd=5000.0,
            status="active",
            updated_at=1700000000,
        )
        assert report.capacity_eth == 10.0
        assert report.status == "active"

    def test_to_dict_internal(self):
        report = CapacityReport(
            mm_address="0xABC",
            asset="ETH",
            capacity_eth=10.0,
            capacity_usd=20000.0,
            premium_pool_usd=25000.0,
            hedge_pool_usd=30000.0,
            hedge_pool_withdrawable_usd=10000.0,
            leverage=3,
            open_positions_count=2,
            open_positions_notional_usd=5000.0,
            status="active",
            updated_at=1700000000,
        )
        d = report.to_dict(internal=True)
        assert "premium_pool_usd" in d
        assert "hedge_pool_usd" in d
        assert d["mm_address"] == "0xABC"

    def test_to_dict_external_excludes_internal_fields(self):
        report = CapacityReport(
            mm_address="0xABC",
            asset="ETH",
            capacity_eth=10.0,
            capacity_usd=20000.0,
            premium_pool_usd=25000.0,
            hedge_pool_usd=30000.0,
            hedge_pool_withdrawable_usd=10000.0,
            leverage=3,
            open_positions_count=2,
            open_positions_notional_usd=5000.0,
            status="active",
            updated_at=1700000000,
        )
        d = report.to_dict(internal=False)
        assert "premium_pool_usd" not in d
        assert "hedge_pool_usd" not in d
        assert d["capacity_eth"] == 10.0
        assert d["status"] == "active"


class TestCapacityStatus:
    def test_active_when_capacity_above_threshold(self):
        assert capacity_status(5.0, 10000.0, 8000.0) == "active"

    def test_full_when_capacity_below_threshold(self):
        assert capacity_status(0.05, 10000.0, 8000.0) == "full"

    def test_full_at_zero(self):
        assert capacity_status(0.0, 10000.0, 8000.0) == "full"

    def test_degraded_when_hedge_pool_low(self):
        # hedge_pool < 40% of premium_pool → degraded
        assert capacity_status(5.0, 10000.0, 3000.0) == "degraded"

    def test_degraded_when_hedge_pool_zero(self):
        assert capacity_status(5.0, 10000.0, 0.0) == "degraded"

    def test_not_degraded_when_hedge_not_live(self):
        # simulate mode: hedge_pool=0 should NOT trigger degraded
        assert capacity_status(5.0, 10000.0, 0.0, hedge_live=False) == "active"


class TestCalculateCapacityInternal:
    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_min_of_premium_and_hedge(self, mock_config, mock_hedge):
        """Effective capacity is min(premium_pool, hedge_notional)."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8  # 100 ETH ceiling

        # 50k USDC balance, 50k allowance → premium pool = 50k
        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)

        # Hedge pool: withdrawable $20k, leverage 3 → notional $60k × 0.75 = $45k
        mock_hedge.get_withdrawable.return_value = 20_000.0
        mock_hedge.get_account_value.return_value = 30_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        # min(50000, 45000) = 45000 → 45000/2000 = 22.5 ETH
        assert report.capacity_usd == pytest.approx(45_000.0, rel=0.01)
        assert report.capacity_eth == pytest.approx(22.5, rel=0.01)
        assert report.premium_pool_usd == pytest.approx(50_000.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_premium_pool_is_bottleneck(self, mock_config, mock_hedge):
        """When premium pool < hedge notional, premium pool limits."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        # 10k USDC → premium pool = 10k
        w3 = _mock_w3(10_000 * 10**6, 10_000 * 10**6)

        # Hedge: withdrawable $50k × 3 × 0.75 = $112.5k
        mock_hedge.get_withdrawable.return_value = 50_000.0
        mock_hedge.get_account_value.return_value = 60_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        assert report.capacity_usd == pytest.approx(10_000.0, rel=0.01)
        assert report.capacity_eth == pytest.approx(5.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_allowance_limits_premium_pool(self, mock_config, mock_hedge):
        """Allowance < balance → allowance is the premium pool."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        # Balance 100k, allowance 5k → premium pool = 5k
        w3 = _mock_w3(100_000 * 10**6, 5_000 * 10**6)

        mock_hedge.get_withdrawable.return_value = 50_000.0
        mock_hedge.get_account_value.return_value = 60_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        assert report.premium_pool_usd == pytest.approx(5_000.0, rel=0.01)
        assert report.capacity_usd == pytest.approx(5_000.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_committed_premium_subtracted(self, mock_config, mock_hedge):
        """Open positions' premium is subtracted from premium pool."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        # 20k USDC
        w3 = _mock_w3(20_000 * 10**6, 20_000 * 10**6)

        mock_hedge.get_withdrawable.return_value = 50_000.0
        mock_hedge.get_account_value.return_value = 60_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = [MagicMock(), MagicMock()]
        tracker.total_premium_paid.return_value = 8_000.0  # $8k committed

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        # 20k - 8k = 12k premium pool
        assert report.premium_pool_usd == pytest.approx(12_000.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_max_amount_ceiling(self, mock_config, mock_hedge):
        """capacity_eth capped by MAX_AMOUNT."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 5 * 10**8  # 5 ETH cap

        # Huge pools → 500 ETH capacity before cap
        w3 = _mock_w3(1_000_000 * 10**6, 1_000_000 * 10**6)
        mock_hedge.get_withdrawable.return_value = 500_000.0
        mock_hedge.get_account_value.return_value = 600_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        assert report.capacity_eth == pytest.approx(5.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_zero_hedge_withdrawable(self, mock_config, mock_hedge):
        """Zero withdrawable on Hyperliquid → capacity limited to 0."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)
        mock_hedge.get_withdrawable.return_value = 0.0
        mock_hedge.get_account_value.return_value = 10_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        assert report.capacity_usd == pytest.approx(0.0)
        assert report.capacity_eth == pytest.approx(0.0)
        assert report.status == "full"

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_status_degraded_when_hedge_low(self, mock_config, mock_hedge):
        """Status is degraded when hedge pool < 40% of premium pool."""
        mock_config.HEDGE_MODE = "live"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)
        # withdrawable $2k × 3 × 0.75 = $4.5k notional
        # account value $3k → hedge_pool_usd = $3k
        # 3k < 40% of 50k → degraded
        mock_hedge.get_withdrawable.return_value = 2_000.0
        mock_hedge.get_account_value.return_value = 3_000.0

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        assert report.status == "degraded"

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_simulate_mode_uses_premium_only(self, mock_config, mock_hedge):
        """In simulate mode, capacity uses only premium pool (no hedge)."""
        mock_config.HEDGE_MODE = "simulate"
        mock_config.HEDGE_LEVERAGE = 3
        mock_config.CAPACITY_RESERVE_RATIO = 0.25
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.MAX_AMOUNT = 100 * 10**8

        # 50k USDC available
        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)

        # Hedge executor should NOT be called
        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.total_premium_paid.return_value = 0.0

        report = calculate_capacity_internal(w3, SPOT, "0xMM", tracker)

        mock_hedge.get_withdrawable.assert_not_called()
        mock_hedge.get_account_value.assert_not_called()
        assert report.capacity_usd == pytest.approx(50_000.0, rel=0.01)
        assert report.capacity_eth == pytest.approx(25.0, rel=0.01)
        assert report.status == "active"
