from types import SimpleNamespace
from unittest.mock import Mock

from src import main


def test_capacity_report_includes_validated_market_observation(monkeypatch):
    cap = SimpleNamespace(
        capacity_eth=1.0,
        capacity_usd=100.0,
        status="active",
        to_dict=Mock(return_value={"asset": "cbzec", "status": "active"}),
    )
    report = Mock()
    monkeypatch.setattr(main, "calculate_capacity_internal", Mock(return_value=cap))
    monkeypatch.setattr(main.api_client, "report_capacity", report)

    result = main._calculate_and_report_capacity(
        None,
        main.MarketSnapshot(spot=42.0, iv=0.5, observed_at=1234),
        "0xmaker",
        SimpleNamespace(name="cbzec"),
    )

    assert result is cap
    report.assert_called_once_with(
        {
            "asset": "cbzec",
            "status": "active",
            "spot": 42.0,
            "spot_observed_at": 1234,
        }
    )
