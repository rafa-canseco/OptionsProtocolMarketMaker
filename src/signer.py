"""EIP-712 (Base) and ed25519 (Solana) quote signing + nonce reading."""

import base64
import hashlib
import logging
import struct
from typing import Any

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
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


# ============================================================
# Solana ed25519 signing
# ============================================================

# Anchor account discriminator for MakerState
# = sha256("account:MakerState")[:8]
MAKER_STATE_DISCRIMINATOR = hashlib.sha256(b"account:MakerState").digest()[:8]
# MakerState layout after 8-byte discriminator:
#   maker: Pubkey (32 bytes) | nonce: u64 (8) | whitelisted: bool (1) | bump: u8 (1)
_NONCE_OFFSET = 8 + 32  # discriminator + maker pubkey


def build_solana_quote_message(
    otoken_mint: bytes,
    *,
    bid_price: int,
    deadline: int,
    quote_id: int,
    max_amount: int,
    maker_nonce: int,
) -> bytes:
    """Build the 72-byte message matching the Rust build_quote_message.

    Layout: otoken_mint (32) + bid_price (u64 LE) + deadline (i64 LE)
            + quote_id (u64 LE) + max_amount (u64 LE) + maker_nonce (u64 LE)
    """
    return (
        otoken_mint
        + struct.pack("<Q", bid_price)
        + struct.pack("<q", deadline)
        + struct.pack("<Q", quote_id)
        + struct.pack("<Q", max_amount)
        + struct.pack("<Q", maker_nonce)
    )


def sign_quote_solana(keypair: Keypair, message: bytes) -> bytes:
    """Sign a quote message with ed25519. Returns raw 64-byte signature."""
    sig = keypair.sign_message(message)
    return bytes(sig)


def _derive_maker_state_pda(
    program_id: Pubkey,
    maker: Pubkey,
) -> tuple[Pubkey, int]:
    """Derive MakerState PDA: seeds = [b"maker", maker.as_ref()]."""
    return Pubkey.find_program_address(
        [b"maker", bytes(maker)],
        program_id,
    )


def read_maker_nonce_solana(
    rpc_url: str,
    program_id: str,
    maker_pubkey: str,
) -> int:
    """Read maker nonce from the Solana MakerState PDA."""
    prog = Pubkey.from_string(program_id)
    maker = Pubkey.from_string(maker_pubkey)
    pda, _bump = _derive_maker_state_pda(prog, maker)

    resp = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                str(pda),
                {"encoding": "base64"},
            ],
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()

    # Check for JSON-RPC level errors (HTTP 200 but RPC failure)
    if "error" in body:
        rpc_err = body["error"]
        raise RuntimeError(
            f"Solana RPC error {rpc_err.get('code')}: "
            f"{rpc_err.get('message')} "
            f"(method=getAccountInfo, account={pda})"
        )

    value = body.get("result", {}).get("value")
    if value is None:
        raise ValueError(
            f"MakerState PDA not found for maker {maker_pubkey} "
            f"under program {program_id}. Maker may not be "
            f"registered on-chain. Check SOLANA_BATCH_SETTLER "
            f"and SOLANA_PRIVATE_KEY."
        )

    data_field = value.get("data")
    if not isinstance(data_field, list) or len(data_field) < 1:
        raise ValueError(
            f"Unexpected data format from getAccountInfo for PDA {pda}: {data_field}"
        )
    data = base64.b64decode(data_field[0])

    # Validate discriminator
    if data[:8] != MAKER_STATE_DISCRIMINATOR:
        raise ValueError(
            f"MakerState discriminator mismatch: "
            f"expected {MAKER_STATE_DISCRIMINATOR.hex()}, "
            f"got {data[:8].hex()}"
        )

    # Bounds check before struct unpacking
    min_size = _NONCE_OFFSET + 8
    if len(data) < min_size:
        raise ValueError(
            f"MakerState data too short: {len(data)} bytes, "
            f"expected >= {min_size}. Account {pda} may "
            f"belong to a different program."
        )

    nonce = struct.unpack_from("<Q", data, _NONCE_OFFSET)[0]
    log.info("Solana makerNonce=%d for %s", nonce, maker_pubkey)
    return nonce
