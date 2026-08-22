from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3.exceptions import TimeExhausted

from src.fund_tx import send_confirmed_transaction, validate_pre_sign_batch


def _hash(value: int):
    result = MagicMock()
    result.hex.return_value = f"0x{value:064x}"
    return result


def _receipt(block=10, block_hash=None):
    return SimpleNamespace(
        status=1,
        blockNumber=block,
        blockHash=block_hash or _hash(block),
    )


def _runtime():
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = 100
    w3.eth.estimate_gas.return_value = 100_000
    w3.eth.block_number = 12
    account = MagicMock(address="0x" + "11" * 20)
    account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"tx")
    function = MagicMock()
    function.build_transaction.side_effect = lambda params: {
        **params,
        "to": "0x" + "22" * 20,
        "data": "0x1234",
    }
    w3.provider.make_batch_request.return_value = [
        {"result": "0x14a34"},
        {"result": "0x7"},
        {"result": "0x7"},
        {"result": "0x6000"},
        {"result": "0x"},
    ]
    w3.eth.get_block.side_effect = lambda block: SimpleNamespace(hash=_hash(block))
    return w3, account, function


def test_transaction_is_confirmed_and_reorg_checked():
    w3, account, function = _runtime()
    w3.eth.send_raw_transaction.return_value = _hash(1)
    receipt = _receipt()
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.get_transaction_receipt.return_value = receipt

    result = send_confirmed_transaction(
        w3=w3,
        account=account,
        function=function,
        chain_id=84532,
        confirmations=2,
    )

    assert result.nonce == 7
    assert result.replaced is False
    assert result.block_number == 10
    w3.eth.get_transaction_count.assert_not_called()
    batch = w3.provider.make_batch_request.call_args.args[0]
    assert batch[1:3] == [
        ("eth_getTransactionCount", [account.address, "latest"]),
        ("eth_getTransactionCount", [account.address, "pending"]),
    ]
    assert account.sign_transaction.call_args.args[0]["nonce"] == 7


def test_timed_out_transaction_reuses_pending_original_nonce():
    w3, account, function = _runtime()
    initial = [
        dict(response) for response in w3.provider.make_batch_request.return_value
    ]
    initial[1] = {"result": "0x9"}
    initial[2] = {"result": "0x9"}
    pending_original = [dict(response) for response in initial]
    pending_original[2] = {"result": "0xa"}
    w3.provider.make_batch_request.side_effect = [initial, pending_original]
    w3.eth.send_raw_transaction.side_effect = [_hash(1), _hash(2)]
    receipt = _receipt()
    w3.eth.wait_for_transaction_receipt.side_effect = [TimeExhausted(), receipt]
    w3.eth.get_transaction_receipt.return_value = receipt

    result = send_confirmed_transaction(
        w3=w3,
        account=account,
        function=function,
        chain_id=84532,
        confirmations=2,
    )

    assert result.nonce == 9
    assert result.replaced is True
    assert account.sign_transaction.call_count == 2
    assert w3.provider.make_batch_request.call_count == 2
    w3.eth.get_transaction_count.assert_not_called()


def test_replacement_rejects_latest_nonce_advanced_past_original():
    w3, account, function = _runtime()
    initial = list(w3.provider.make_batch_request.return_value)
    advanced = [dict(response) for response in initial]
    advanced[1] = {"result": "0x8"}
    advanced[2] = {"result": "0x8"}
    w3.provider.make_batch_request.side_effect = [initial, advanced]
    w3.eth.send_raw_transaction.return_value = _hash(1)
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()

    with pytest.raises(RuntimeError, match="critical state changed"):
        send_confirmed_transaction(
            w3=w3,
            account=account,
            function=function,
            chain_id=84532,
            confirmations=2,
        )

    assert account.sign_transaction.call_count == 1


def test_changed_receipt_is_reconfirmed_after_reinclusion():
    w3, account, function = _runtime()
    w3.eth.send_raw_transaction.return_value = _hash(1)
    receipt = _receipt(block_hash=_hash(10))
    changed = _receipt(block_hash=_hash(11))
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.get_transaction_receipt.side_effect = [changed, changed]
    w3.eth.get_block.side_effect = None
    w3.eth.get_block.return_value = SimpleNamespace(hash=changed.blockHash)

    result = send_confirmed_transaction(
        w3=w3,
        account=account,
        function=function,
        chain_id=84532,
        confirmations=2,
    )

    assert result.block_hash == changed.blockHash.hex()
    assert w3.eth.get_transaction_receipt.call_count == 2


def test_receipt_that_keeps_changing_is_rejected():
    w3, account, function = _runtime()
    w3.eth.send_raw_transaction.return_value = _hash(1)
    receipt = _receipt(block_hash=_hash(10))
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.get_transaction_receipt.side_effect = [
        _receipt(block_hash=_hash(11)),
        _receipt(block_hash=_hash(12)),
        _receipt(block_hash=_hash(13)),
    ]

    with pytest.raises(RuntimeError, match="kept changing"):
        send_confirmed_transaction(
            w3=w3,
            account=account,
            function=function,
            chain_id=84532,
            confirmations=2,
        )


def test_transaction_hash_is_normalized_with_hex_prefix():
    w3, account, function = _runtime()
    tx_hash = _hash(1)
    tx_hash.hex.return_value = "01"
    w3.eth.send_raw_transaction.return_value = tx_hash
    receipt = _receipt()
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.get_transaction_receipt.return_value = receipt

    result = send_confirmed_transaction(
        w3=w3,
        account=account,
        function=function,
        chain_id=84532,
        confirmations=2,
    )

    assert result.tx_hash == "0x01"


def test_replacement_revalidates_decision_immediately_before_signing():
    w3, account, function = _runtime()
    w3.eth.send_raw_transaction.return_value = _hash(1)
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()
    validations = 0

    def validate_decision():
        nonlocal validations
        validations += 1
        if validations == 2:
            raise RuntimeError("snapshot expired")

    with pytest.raises(RuntimeError, match="snapshot expired"):
        send_confirmed_transaction(
            w3=w3,
            account=account,
            function=function,
            chain_id=84532,
            confirmations=2,
            decision_validator=validate_decision,
        )

    assert account.sign_transaction.call_count == 1
    assert w3.provider.make_batch_request.call_count == 2


@pytest.mark.parametrize(
    ("nonce", "latest", "pending", "replacement_for_nonce"),
    (
        (8, 7, 7, 8),  # future replacement nonce
        (7, 7, 8, None),  # stale initial nonce
        (8, 7, 8, 7),  # arbitrary nonce not associated with the original
    ),
)
def test_pre_sign_batch_rejects_unsafe_nonce_modes(
    nonce, latest, pending, replacement_for_nonce
):
    w3, _, _ = _runtime()
    w3.provider.make_batch_request.return_value[1] = {"result": hex(latest)}
    w3.provider.make_batch_request.return_value[2] = {"result": hex(pending)}
    tx = {
        "from": "0x" + "11" * 20,
        "to": "0x" + "22" * 20,
        "nonce": nonce,
        "data": "0x1234",
    }

    with pytest.raises(RuntimeError, match="critical state changed"):
        validate_pre_sign_batch(
            w3=w3,
            tx=tx,
            chain_id=84532,
            replacement_for_nonce=replacement_for_nonce,
        )


@pytest.mark.parametrize(
    ("eth_call_response", "message"),
    (
        ({}, "validation batch is invalid"),
        ({"error": {"code": -32000}}, "validation batch failed"),
        ({"result": "not-hex"}, "eth_call result is invalid"),
    ),
)
def test_pre_sign_batch_invalid_eth_call_result_prevents_signing(
    eth_call_response, message
):
    w3, account, function = _runtime()
    w3.provider.make_batch_request.return_value[4] = eth_call_response

    with pytest.raises(RuntimeError, match=message):
        send_confirmed_transaction(
            w3=w3,
            account=account,
            function=function,
            chain_id=84532,
            confirmations=2,
        )

    account.sign_transaction.assert_not_called()


def test_pre_sign_batch_mismatch_prevents_signing():
    w3, account, function = _runtime()
    w3.provider.make_batch_request.return_value[0] = {"result": "0x1"}

    with pytest.raises(RuntimeError, match="critical state changed"):
        send_confirmed_transaction(
            w3=w3,
            account=account,
            function=function,
            chain_id=84532,
            confirmations=2,
        )

    account.sign_transaction.assert_not_called()
