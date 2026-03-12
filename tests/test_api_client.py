"""Tests for api_client capacity reporting."""

from unittest.mock import MagicMock, patch


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
