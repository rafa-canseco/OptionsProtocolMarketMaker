import logging
import sys

from src.main import _RpcEndpointRedactingFormatter


def test_rpc_endpoints_are_redacted_from_messages_and_tracebacks():
    base_url = "https://base.example.invalid/secret-base"
    solana_url = "https://solana.example.invalid/secret-solana"
    formatter = _RpcEndpointRedactingFormatter(
        "%(message)s", endpoints=(base_url, solana_url)
    )

    try:
        raise RuntimeError(f"provider failed at {solana_url}")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            0,
            f"failed at {base_url}",
            (),
            sys.exc_info(),
        )

    rendered = formatter.format(record)

    assert base_url not in rendered
    assert solana_url not in rendered
    assert rendered.count("[REDACTED_RPC_ENDPOINT]") == 2


def test_rpc_endpoint_path_and_query_are_redacted_from_normalized_tracebacks():
    endpoint = "https://rpc.example.invalid/v2/secret-key?api_key=secret-query"
    formatter = _RpcEndpointRedactingFormatter("%(message)s", endpoints=(endpoint,))

    try:
        raise RuntimeError(
            "HTTPSConnectionPool: request url: /v2/secret-key?api_key=secret-query"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 0, "failed", (), sys.exc_info()
        )

    rendered = formatter.format(record)

    assert "/v2/secret-key" not in rendered
    assert "secret-query" not in rendered


def test_rpc_endpoint_hostname_is_redacted_from_provider_tracebacks():
    endpoint = "https://tenant-secret.rpc.example.invalid/v2"
    formatter = _RpcEndpointRedactingFormatter("%(message)s", endpoints=(endpoint,))

    try:
        raise RuntimeError(
            "HTTPSConnectionPool(host='tenant-secret.rpc.example.invalid', port=443)"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 0, "failed", (), sys.exc_info()
        )

    assert "tenant-secret.rpc.example.invalid" not in formatter.format(record)
