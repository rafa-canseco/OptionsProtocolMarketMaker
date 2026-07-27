"""Confirmed, replacement-aware transaction submission for fund workers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from web3.exceptions import TimeExhausted, TransactionNotFound


@dataclass(frozen=True)
class ConfirmedTransaction:
    tx_hash: str
    nonce: int
    block_number: int
    block_hash: str
    replaced: bool


def send_confirmed_transaction(
    *,
    w3: Any,
    account: Any,
    function: Any,
    chain_id: int,
    confirmations: int,
    receipt_timeout: int = 180,
) -> ConfirmedTransaction:
    """Send once, replace once on timeout, and reject a reorged receipt."""
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = function.build_transaction(
        {
            "from": account.address,
            "chainId": chain_id,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
        }
    )
    tx["gas"] = w3.eth.estimate_gas(tx) * 120 // 100
    signed = account.sign_transaction(tx)
    original_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    final_hash = original_hash
    replaced = False
    try:
        receipt = w3.eth.wait_for_transaction_receipt(
            original_hash, timeout=receipt_timeout
        )
    except TimeExhausted:
        replacement = dict(tx)
        replacement["gasPrice"] = max(
            int(tx["gasPrice"]) * 125 // 100,
            int(w3.eth.gas_price) * 1125 // 1000,
        )
        signed_replacement = account.sign_transaction(replacement)
        try:
            final_hash = w3.eth.send_raw_transaction(signed_replacement.raw_transaction)
            replaced = True
            receipt = w3.eth.wait_for_transaction_receipt(
                final_hash, timeout=receipt_timeout
            )
        except ValueError:
            # The original may have landed while the replacement was submitted.
            receipt = w3.eth.get_transaction_receipt(original_hash)
            final_hash = original_hash

    if int(receipt.status) != 1:
        raise RuntimeError(f"Fund worker transaction reverted: {final_hash.hex()}")

    target_block = int(receipt.blockNumber) + max(confirmations, 1)
    deadline = time.monotonic() + 60
    while int(w3.eth.block_number) < target_block:
        if time.monotonic() >= deadline:
            raise TimeExhausted("Timed out waiting for transaction confirmations")
        time.sleep(1)
    try:
        confirmed = w3.eth.get_transaction_receipt(final_hash)
    except TransactionNotFound as error:
        raise RuntimeError(
            "Fund worker receipt disappeared after confirmation"
        ) from error
    if (
        int(confirmed.status) != 1
        or int(confirmed.blockNumber) != int(receipt.blockNumber)
        or confirmed.blockHash != receipt.blockHash
    ):
        raise RuntimeError("Fund worker transaction receipt changed after confirmation")
    return ConfirmedTransaction(
        tx_hash=final_hash.hex(),
        nonce=nonce,
        block_number=int(confirmed.blockNumber),
        block_hash=confirmed.blockHash.hex(),
        replaced=replaced,
    )
