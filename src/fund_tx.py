"""Confirmed, replacement-aware transaction submission for fund workers."""

from __future__ import annotations

import time
from collections.abc import Callable
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


def _normalized_hex(value: Any) -> str:
    encoded = value.hex()
    return encoded if encoded.startswith("0x") else f"0x{encoded}"


def _same_receipt(left: Any, right: Any) -> bool:
    return (
        int(left.status) == int(right.status)
        and int(left.blockNumber) == int(right.blockNumber)
        and left.blockHash == right.blockHash
    )


def _wait_for_stable_receipt(
    *, w3: Any, tx_hash: Any, receipt: Any, confirmations: int
) -> Any:
    """Follow a successfully re-included transaction to a stable receipt."""
    candidate = receipt
    deadline = time.monotonic() + 60
    for _ in range(3):
        target_block = int(candidate.blockNumber) + max(confirmations, 1)
        while int(w3.eth.block_number) < target_block:
            if time.monotonic() >= deadline:
                raise TimeExhausted("Timed out waiting for transaction confirmations")
            time.sleep(1)
        try:
            observed = w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound as error:
            raise RuntimeError(
                "Fund worker receipt disappeared after confirmation"
            ) from error
        if int(observed.status) != 1:
            raise RuntimeError("Fund worker transaction reverted after confirmation")
        if _same_receipt(candidate, observed):
            return observed
        candidate = observed
    raise RuntimeError("Fund worker transaction receipt kept changing")


def _rpc_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(tx)
    for key in (
        "chainId",
        "gas",
        "gasPrice",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
        "nonce",
        "value",
    ):
        value = encoded.get(key)
        if isinstance(value, int):
            encoded[key] = hex(value)
    encoded.pop("chainId", None)
    encoded.pop("nonce", None)
    return encoded


def validate_pre_sign_batch(
    *,
    w3: Any,
    tx: dict[str, Any],
    chain_id: int,
    assign_pending_nonce: bool = False,
    replacement_for_nonce: int | None = None,
) -> None:
    """Batch the final chain/code/nonce/simulation guard before signing."""
    target = tx.get("to")
    sender = tx.get("from")
    if not target or not sender:
        raise RuntimeError("Fund worker transaction boundary is absent")
    responses = w3.provider.make_batch_request(
        [
            ("eth_chainId", []),
            ("eth_getTransactionCount", [sender, "latest"]),
            ("eth_getTransactionCount", [sender, "pending"]),
            ("eth_getCode", [target, "latest"]),
            ("eth_call", [_rpc_transaction(tx), "latest"]),
        ]
    )
    if not isinstance(responses, list) or len(responses) != 5:
        raise RuntimeError("Fund worker pre-sign validation batch is incomplete")
    if not all(isinstance(response, dict) for response in responses):
        raise RuntimeError("Fund worker pre-sign validation batch is invalid")
    if any("error" in response for response in responses):
        raise RuntimeError("Fund worker pre-sign validation batch failed")
    try:
        observed_chain_id = int(responses[0]["result"], 16)
        latest_nonce = int(responses[1]["result"], 16)
        pending_nonce = int(responses[2]["result"], 16)
        code = responses[3]["result"]
        simulation = responses[4]["result"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Fund worker pre-sign validation batch is invalid") from exc
    if "nonce" not in tx:
        if not assign_pending_nonce:
            raise RuntimeError("Fund worker transaction nonce is absent")
        tx["nonce"] = pending_nonce
    nonce = tx["nonce"]
    nonce_valid = (
        not isinstance(nonce, bool)
        and isinstance(nonce, int)
        and latest_nonce <= pending_nonce
        and (
            nonce == pending_nonce
            if replacement_for_nonce is None
            else nonce == replacement_for_nonce
            and latest_nonce <= nonce <= pending_nonce
        )
    )
    if (
        not nonce_valid
        or observed_chain_id != chain_id
        or not isinstance(code, str)
        or code in {"", "0x"}
    ):
        raise RuntimeError("Fund worker pre-sign critical state changed")
    if (
        not isinstance(simulation, str)
        or not simulation.startswith("0x")
        or len(simulation) % 2
        or any(
            character not in "0123456789abcdefABCDEF" for character in simulation[2:]
        )
    ):
        raise RuntimeError("Fund worker pre-sign eth_call result is invalid")


def send_confirmed_transaction(
    *,
    w3: Any,
    account: Any,
    function: Any,
    chain_id: int,
    confirmations: int,
    receipt_timeout: int = 180,
    decision_validator: Callable[[], None] | None = None,
) -> ConfirmedTransaction:
    """Send once, replace once on timeout, and reject a reorged receipt."""
    tx = function.build_transaction(
        {
            "from": account.address,
            "chainId": chain_id,
            # Prevent build_transaction from issuing an independent nonce read.
            "nonce": 0,
            "gasPrice": w3.eth.gas_price,
        }
    )
    tx.pop("nonce", None)
    tx["gas"] = w3.eth.estimate_gas(tx) * 120 // 100
    validate_pre_sign_batch(
        w3=w3,
        tx=tx,
        chain_id=chain_id,
        assign_pending_nonce=True,
    )
    nonce = int(tx["nonce"])
    if decision_validator is not None:
        decision_validator()
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
        validate_pre_sign_batch(
            w3=w3,
            tx=replacement,
            chain_id=chain_id,
            replacement_for_nonce=nonce,
        )
        if decision_validator is not None:
            decision_validator()
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

    confirmed = _wait_for_stable_receipt(
        w3=w3,
        tx_hash=final_hash,
        receipt=receipt,
        confirmations=confirmations,
    )
    canonical_block = w3.eth.get_block(int(confirmed.blockNumber))
    if _normalized_hex(canonical_block.hash) != _normalized_hex(confirmed.blockHash):
        raise RuntimeError("Fund worker receipt block is not canonical")
    return ConfirmedTransaction(
        tx_hash=_normalized_hex(final_hash),
        nonce=nonce,
        block_number=int(confirmed.blockNumber),
        block_hash=_normalized_hex(confirmed.blockHash),
        replaced=replaced,
    )
