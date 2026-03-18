"""EIP-712 quote signing and on-chain nonce reading."""

import logging
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

log = logging.getLogger(__name__)

QUOTE_TYPES = {
    "Quote": [
        {"name": "oToken", "type": "address"},
        {"name": "bidPrice", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "quoteId", "type": "uint256"},
        {"name": "maxAmount", "type": "uint256"},
        {"name": "makerNonce", "type": "uint256"},
    ],
}

SETTLER_ABI = [
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "makerNonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def build_domain(chain_id: int, settler_address: str) -> dict[str, Any]:
    return {
        "name": "b1nary",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": settler_address,
    }


def sign_quote(
    private_key: str,
    domain: dict[str, Any],
    quote: dict[str, Any],
) -> str:
    """Sign an EIP-712 quote. Returns 0x-prefixed hex signature."""
    signable = encode_typed_data(
        domain_data=domain,
        message_types=QUOTE_TYPES,
        message_data=quote,
    )
    signed = Account.sign_message(signable, private_key=private_key)
    return "0x" + signed.signature.hex()


def read_maker_nonce(w3: Web3, settler_address: str, mm_address: str) -> int:
    """Read makerNonce from the BatchSettler contract."""
    settler = w3.eth.contract(
        address=Web3.to_checksum_address(settler_address),
        abi=SETTLER_ABI,
    )
    nonce = settler.functions.makerNonce(Web3.to_checksum_address(mm_address)).call()
    log.info("makerNonce=%d for %s", nonce, mm_address)
    return nonce
