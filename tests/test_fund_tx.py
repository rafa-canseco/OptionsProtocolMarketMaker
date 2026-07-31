from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3.exceptions import TimeExhausted

from src.fund_tx import send_confirmed_transaction


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
    function.build_transaction.side_effect = lambda params: dict(params)
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


def test_timed_out_transaction_is_replaced_with_same_nonce():
    w3, account, function = _runtime()
    w3.eth.get_transaction_count.return_value = 9
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


def test_changed_receipt_is_reconfirmed_after_reinclusion():
    w3, account, function = _runtime()
    w3.eth.send_raw_transaction.return_value = _hash(1)
    receipt = _receipt(block_hash=_hash(10))
    changed = _receipt(block_hash=_hash(11))
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.get_transaction_receipt.side_effect = [changed, changed]

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
