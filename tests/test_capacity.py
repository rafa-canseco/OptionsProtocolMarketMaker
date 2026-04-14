"""Tests for capacity calculation module."""

from unittest.mock import MagicMock, patch

import pytest

from src.capacity import (
    CapacityReport,
    calculate_capacity_internal,
    capacity_status,
)
from src.config import AssetConfig


SPOT = 2000.0

ETH_CONFIG = AssetConfig(name="eth", hedge_symbol="ETH", leverage=3, max_exposure=0.8)
BTC_CONFIG = AssetConfig(name="btc", hedge_symbol="BTC", leverage=2, max_exposure=0.8)


def _mock_w3(usdc_balance: int, usdc_allowance: int):
    """Build a mock Web3 returning given USDC balance/allowance."""
    w3 = MagicMock()

    def mock_call(tx):
        data = tx["data"]
        if data.startswith("0x70a08231"):
            return usdc_balance.to_bytes(32, "big")
        if data.startswith("0xdd62ed3e"):
            return usdc_allowance.to_bytes(32, "big")
        return b"\x00" * 32

    w3.eth.call = mock_call
    return w3


def _live_config(mock_config, max_amount=100 * 10**8):
    """Apply common live-mode config to a mock."""
    mock_config.HEDGE_MODE = "live"
    mock_config.CAPACITY_RESERVE_RATIO = 0.25
    mock_config.CAPACITY_PREMIUM_RATIO = 0.03
    mock_config.CAPACITY_AVG_DELTA = 0.3
    mock_config.USDC_ADDRESS = "0xUSDC"
    mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
    mock_config.EVM_CONFIGS = {}
    mock_config.MAX_AMOUNT = max_amount


def _empty_tracker():
    tracker = MagicMock()
    tracker.open_positions.return_value = []
    tracker.deployed_usd.return_value = 0.0
    return tracker


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
        assert capacity_status(500.0, 10000.0, 8000.0) == "active"

    def test_full_when_capacity_below_threshold(self):
        assert capacity_status(5.0, 10000.0, 8000.0) == "full"

    def test_full_at_zero(self):
        assert capacity_status(0.0, 10000.0, 8000.0) == "full"

    def test_full_when_premium_pool_depleted(self):
        assert capacity_status(500.0, 5.0, 8000.0) == "full"

    def test_degraded_when_hedge_pool_low(self):
        assert capacity_status(500.0, 10000.0, 3000.0) == "degraded"

    def test_degraded_when_hedge_pool_zero(self):
        assert capacity_status(500.0, 10000.0, 0.0) == "degraded"

    def test_not_degraded_when_hedge_not_live(self):
        assert capacity_status(500.0, 10000.0, 0.0, hedge_live=False) == "active"


class TestLiveModeCapacity:
    """Live mode: pools self-track, premium-ratio conversion.

    With SPOT=2000, PREMIUM_RATIO=0.03, AVG_DELTA=0.3, leverage=3:
      premium_per_eth  = 0.03 × 2000 = $60
      hedge_margin/eth = 0.3 × 2000 / 3 = $200
    """

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_hedge_is_bottleneck(self, mock_config, mock_hedge):
        """When hedge runs out first, hedge limits capacity."""
        _live_config(mock_config)
        # $600 USDC → max_eth_premium = 600/60 = 10 ETH
        w3 = _mock_w3(600 * 10**6, 600 * 10**6)
        # withdrawable $2000 → usable $1500 → max_eth_hedge = 7.5
        mock_hedge.get_withdrawable.return_value = 2_000.0
        mock_hedge.get_account_value.return_value = 3_000.0

        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=ETH_CONFIG,
        )

        # min(10, 7.5) × 0.8 = 6.0 ETH → $12,000
        assert report.capacity_eth == pytest.approx(6.0, rel=0.01)
        assert report.capacity_usd == pytest.approx(12_000.0, rel=0.01)
        assert report.premium_pool_usd == pytest.approx(600.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_premium_is_bottleneck(self, mock_config, mock_hedge):
        """When premium pool runs out first, premium limits."""
        _live_config(mock_config)
        # $180 USDC → max_eth_premium = 180/60 = 3.0 ETH
        w3 = _mock_w3(180 * 10**6, 180 * 10**6)
        # withdrawable $5000 → usable $3750 → max_eth_hedge = 18.75
        mock_hedge.get_withdrawable.return_value = 5_000.0
        mock_hedge.get_account_value.return_value = 6_000.0

        full_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=1.0,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=full_exposure,
        )

        # min(3.0, 18.75) × 1.0 = 3.0 ETH → $6,000
        assert report.capacity_eth == pytest.approx(3.0, rel=0.01)
        assert report.capacity_usd == pytest.approx(6_000.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_allowance_limits_premium_pool(self, mock_config, mock_hedge):
        """Allowance < balance → allowance caps the premium pool."""
        _live_config(mock_config)
        # Balance $1000, allowance $300 → premium_pool = $300
        w3 = _mock_w3(1_000 * 10**6, 300 * 10**6)
        mock_hedge.get_withdrawable.return_value = 5_000.0
        mock_hedge.get_account_value.return_value = 6_000.0

        full_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=1.0,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=full_exposure,
        )

        assert report.premium_pool_usd == pytest.approx(300.0, rel=0.01)
        # max_eth_premium = 300/60 = 5.0 ETH (bottleneck)
        assert report.capacity_eth == pytest.approx(5.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_live_mode_self_tracking(self, mock_config, mock_hedge):
        """USDC balance already reflects premium paid; no subtraction."""
        _live_config(mock_config)
        # $500 USDC (already reduced by on-chain trades)
        w3 = _mock_w3(500 * 10**6, 500 * 10**6)
        mock_hedge.get_withdrawable.return_value = 5_000.0
        mock_hedge.get_account_value.return_value = 6_000.0

        pos1 = MagicMock(premium_paid_usd=200.0)
        tracker = MagicMock()
        tracker.open_positions.return_value = [pos1]

        full_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=1.0,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            tracker,
            asset_config=full_exposure,
        )

        # premium_pool = 500 (NOT 500-200=300)
        assert report.premium_pool_usd == pytest.approx(500.0, rel=0.01)
        # max_eth_premium = 500/60 = 8.33 ETH
        assert report.capacity_eth == pytest.approx(8.333, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_max_amount_ceiling(self, mock_config, mock_hedge):
        """capacity_eth capped by MAX_AMOUNT."""
        _live_config(mock_config, max_amount=5 * 10**8)  # 5 ETH cap
        w3 = _mock_w3(100_000 * 10**6, 100_000 * 10**6)
        mock_hedge.get_withdrawable.return_value = 100_000.0
        mock_hedge.get_account_value.return_value = 100_000.0

        full_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=1.0,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=full_exposure,
        )

        assert report.capacity_eth == pytest.approx(5.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_zero_hedge_withdrawable(self, mock_config, mock_hedge):
        """Zero withdrawable on Hyperliquid → capacity 0."""
        _live_config(mock_config)
        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)
        mock_hedge.get_withdrawable.return_value = 0.0
        mock_hedge.get_account_value.return_value = 10_000.0

        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=ETH_CONFIG,
        )

        assert report.capacity_usd == pytest.approx(0.0)
        assert report.capacity_eth == pytest.approx(0.0)
        assert report.status == "full"

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_status_degraded_when_hedge_low(self, mock_config, mock_hedge):
        """Status is degraded when hedge pool < 40% of premium pool."""
        _live_config(mock_config)
        # $600 USDC, withdrawable $100 → usable $75
        # hedge_margin/eth = $200 → max_eth_hedge = 0.375
        # account_value $50 → hedge_pool < 40% of 600 → degraded
        w3 = _mock_w3(600 * 10**6, 600 * 10**6)
        mock_hedge.get_withdrawable.return_value = 100.0
        mock_hedge.get_account_value.return_value = 50.0

        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=ETH_CONFIG,
        )

        assert report.status == "degraded"

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_max_exposure_limits_per_asset(self, mock_config, mock_hedge):
        """max_exposure caps per-asset fraction of total capacity."""
        _live_config(mock_config)
        # $600 USDC → max_eth_premium = 10
        # withdrawable $4000 → usable $3000 → max_eth_hedge = 15
        # total = min(10, 15) = 10 ETH
        w3 = _mock_w3(600 * 10**6, 600 * 10**6)
        mock_hedge.get_withdrawable.return_value = 4_000.0
        mock_hedge.get_account_value.return_value = 5_000.0

        # max_exposure=0.5
        half_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=0.5,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=half_exposure,
        )

        # 10 × 0.5 = 5.0 ETH → $10,000
        assert report.capacity_eth == pytest.approx(5.0, rel=0.01)
        assert report.capacity_usd == pytest.approx(10_000.0, rel=0.01)


class TestSimulateModeCapacity:
    """Simulate mode: premium pool only, manual deployed subtraction."""

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_uses_premium_only(self, mock_config, mock_hedge):
        """In simulate mode, capacity uses only premium pool."""
        mock_config.HEDGE_MODE = "simulate"
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.EVM_CONFIGS = {}
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(50_000 * 10**6, 50_000 * 10**6)

        full_exposure = AssetConfig(
            name="eth",
            hedge_symbol="ETH",
            leverage=3,
            max_exposure=1.0,
        )
        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=full_exposure,
        )

        mock_hedge.get_withdrawable.assert_not_called()
        mock_hedge.get_account_value.assert_not_called()
        assert report.capacity_usd == pytest.approx(50_000.0, rel=0.01)
        assert report.capacity_eth == pytest.approx(25.0, rel=0.01)
        assert report.status == "active"


class TestSharedPoolMaxExposure:
    """Test shared pool with per-asset max exposure (simulate mode)."""

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_max_exposure_caps_asset(self, mock_config, mock_hedge):
        """Asset capacity is capped by max_exposure * total_capital."""
        mock_config.HEDGE_MODE = "simulate"
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.EVM_CONFIGS = {}
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(100_000 * 10**6, 100_000 * 10**6)

        report = calculate_capacity_internal(
            w3,
            SPOT,
            "0xMM",
            _empty_tracker(),
            asset_config=ETH_CONFIG,
        )

        # max_exposure=0.8 * 100k = 80k → 40 ETH
        assert report.capacity_usd == pytest.approx(80_000.0, rel=0.01)
        assert report.capacity_eth == pytest.approx(40.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_deployed_reduces_available(self, mock_config, mock_hedge):
        """Deployed capital across all assets reduces available."""
        mock_config.HEDGE_MODE = "simulate"
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.EVM_CONFIGS = {}
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(100_000 * 10**6, 100_000 * 10**6)

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.deployed_usd.side_effect = lambda underlying=None: (
            30_000.0 if underlying == "eth" else 40_000.0
        )

        report = calculate_capacity_internal(
            w3, SPOT, "0xMM", tracker, asset_config=ETH_CONFIG
        )

        # max_for_eth = 0.8 * 100k - 30k = 50k
        # available_global = 100k - 40k = 60k
        # min(50k, 60k) = 50k
        assert report.capacity_usd == pytest.approx(50_000.0, rel=0.01)

    @patch("src.capacity.hedge_executor")
    @patch("src.capacity.config")
    def test_global_constraint_limits_asset(self, mock_config, mock_hedge):
        """When available_global < asset cap, global wins."""
        mock_config.HEDGE_MODE = "simulate"
        mock_config.USDC_ADDRESS = "0xUSDC"
        mock_config.MARGIN_POOL_ADDRESS = "0xMARGIN"
        mock_config.EVM_CONFIGS = {}
        mock_config.MAX_AMOUNT = 100 * 10**8

        w3 = _mock_w3(100_000 * 10**6, 100_000 * 10**6)

        tracker = MagicMock()
        tracker.open_positions.return_value = []
        tracker.deployed_usd.side_effect = lambda underlying=None: (
            10_000.0 if underlying == "btc" else 90_000.0
        )

        report = calculate_capacity_internal(
            w3, SPOT, "0xMM", tracker, asset_config=BTC_CONFIG
        )

        # max_for_btc = 0.8 * 100k - 10k = 70k
        # available_global = 100k - 90k = 10k
        # min(70k, 10k) = 10k
        assert report.capacity_usd == pytest.approx(10_000.0, rel=0.01)
