"""Tests for authenticated backend API calls."""

from unittest.mock import MagicMock, patch

import pytest


def test_protocol_fee_policy_must_match_backend_and_batch_settler():
    from src import api_client

    assert (
        api_client.require_protocol_fee_match({"protocol_fee_bps": 1_000}, 1_000)
        == 1_000
    )

    with pytest.raises(RuntimeError, match="does not match"):
        api_client.require_protocol_fee_match({"protocol_fee_bps": 400}, 1_000)


@pytest.mark.parametrize("market", [{}, {"protocol_fee_bps": "invalid"}])
def test_protocol_fee_policy_requires_valid_backend_value(market):
    from src import api_client

    with pytest.raises(RuntimeError, match="valid protocol fee"):
        api_client.require_protocol_fee_match(market, 1_000)


def test_report_capacity_posts_payload():
    """report_capacity POSTs to /mm/capacity."""
    from src import api_client

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "ok"}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(api_client._SESSION, "post", return_value=mock_resp) as mock_post:
        payload = {
            "mm_address": "0xABC",
            "asset": "ETH",
            "capacity_eth": 10.0,
            "capacity_usd": 20000.0,
            "status": "active",
            "updated_at": 1700000000,
        }
        result = api_client.report_capacity(payload)

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "/mm/capacity" in call_kwargs[0][0]
    assert call_kwargs[1]["json"] == payload
    assert result == {"status": "ok"}


def test_get_meta_wheel_nav_observation_uses_authenticated_session():
    from src import api_client

    payload = {"fundKey": "base-sepolia:wheel", "snapshotBlock": 123}
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    with patch.object(api_client._SESSION, "get", return_value=mock_resp) as get:
        result = api_client.get_meta_wheel_nav_observation(
            "base-sepolia:wheel", snapshot_block=123
        )

    assert result == payload
    get.assert_called_once_with(
        api_client._url("/v2/vaults/base-sepolia:wheel/wheel/nav-observation"),
        params={"snapshot_block": 123},
        timeout=api_client._TIMEOUT,
    )
    mock_resp.raise_for_status.assert_called_once_with()


def test_ensure_fund_series_posts_complete_quote_snapshot(monkeypatch):
    from eth_account import Account

    from src import api_client

    private_key = "0x" + "11" * 32
    monkeypatch.setattr(api_client, "MM_PRIVATE_KEY", private_key)
    quote = {
        "otoken_address": "0x" + "12" * 20,
        "bid_price": 10,
        "deadline": 1_900_000_000,
        "quote_id": 7,
        "max_amount": 100_000_000,
        "maker_nonce": 3,
        "signature": "0x" + "ab" * 65,
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "creating",
        "otoken_address": quote["otoken_address"],
    }

    with patch.object(api_client._SESSION, "post", return_value=mock_resp) as post:
        result = api_client.ensure_fund_series(
            adapter_address="0x" + "34" * 20,
            quote=quote,
            amount_raw=25_000_000,
        )

    post.assert_called_once_with(
        api_client._url("/mm/series/ensure"),
        json={
            "adapter_address": "0x" + "34" * 20,
            "expected_otoken_address": quote["otoken_address"],
            "amount_raw": "25000000",
            "quote": {
                "otoken_address": quote["otoken_address"],
                "bid_price_raw": "10",
                "deadline": "1900000000",
                "quote_id": "7",
                "max_amount_raw": "100000000",
                "maker_nonce": "3",
                "signature": quote["signature"],
                "mm_address": Account.from_key(private_key).address,
            },
        },
        timeout=api_client._MATERIALIZATION_TIMEOUT,
    )
    mock_resp.raise_for_status.assert_called_once_with()
    assert result["status"] == "creating"
