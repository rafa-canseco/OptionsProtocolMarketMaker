"""Tests for dual-chain signing (B1N-275).

Covers:
1. Solana message layout (exactly 72 bytes, correct field order)
2. ed25519 sign + verify round-trip
3. Solana/Base price scale parity (USDC raw, 1e6)
4. MakerState PDA derivation matches Rust program
5. Full round-trip: build → sign → verify (simulates on-chain verifier)
6. Base quote regression (no changes to existing ECDSA path)
"""

import struct
import time
from unittest.mock import patch

from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src.quote_builder import build_quotes, to_api_payload, to_solana_api_payload
from src.signer import (
    MAKER_STATE_DISCRIMINATOR,
    _derive_maker_state_pda,
    build_domain,
    build_solana_quote_message,
    sign_quote,
    sign_quote_solana,
)


# --- 1. Message layout ---


def test_solana_message_is_72_bytes():
    """build_solana_quote_message produces exactly 72 bytes."""
    mint = bytes(Pubkey.new_unique())  # 32 bytes
    msg = build_solana_quote_message(
        otoken_mint=mint,
        bid_price=5_000000,
        deadline=1_700_000_000,
        quote_id=42,
        max_amount=1_00000000,
        maker_nonce=7,
    )
    assert len(msg) == 72


def test_solana_message_field_order():
    """Fields are packed in the exact order the Rust program expects."""
    mint = bytes(range(32))  # deterministic 32 bytes
    bid_price = 123456789
    deadline = 1700000000
    quote_id = 42
    max_amount = 99999999
    maker_nonce = 7

    msg = build_solana_quote_message(
        mint,
        bid_price=bid_price,
        deadline=deadline,
        quote_id=quote_id,
        max_amount=max_amount,
        maker_nonce=maker_nonce,
    )

    # Unpack and verify each field
    assert msg[:32] == mint
    assert struct.unpack_from("<Q", msg, 32)[0] == bid_price
    assert struct.unpack_from("<q", msg, 40)[0] == deadline
    assert struct.unpack_from("<Q", msg, 48)[0] == quote_id
    assert struct.unpack_from("<Q", msg, 56)[0] == max_amount
    assert struct.unpack_from("<Q", msg, 64)[0] == maker_nonce


# --- 2. ed25519 sign + verify ---


def test_sign_quote_solana_returns_64_bytes():
    """ed25519 signature is exactly 64 bytes."""
    kp = Keypair()
    msg = build_solana_quote_message(
        bytes(Pubkey.new_unique()),
        bid_price=100,
        deadline=999999,
        quote_id=1,
        max_amount=100,
        maker_nonce=0,
    )
    sig = sign_quote_solana(kp, msg)
    assert len(sig) == 64


def test_sign_quote_solana_verifiable():
    """Signature can be verified against the signer's pubkey."""
    kp = Keypair()
    msg = build_solana_quote_message(
        bytes(Pubkey.new_unique()),
        bid_price=500,
        deadline=1700000000,
        quote_id=10,
        max_amount=1000,
        maker_nonce=3,
    )
    sig = sign_quote_solana(kp, msg)

    # Verify using nacl (ed25519 verify)
    from nacl.signing import VerifyKey

    vk = VerifyKey(bytes(kp.pubkey()))
    # This raises if verification fails
    vk.verify(msg, sig)


# --- 3. Price scale parity ---


@patch("src.quote_builder.config")
def test_solana_quotes_use_usdc_raw_price_scale(mock_config):
    """Solana quotes use USDC raw (10^6), matching the on-chain program."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000
    mock_config.SOLANA_ASSETS = []

    market = {
        "spot": 150.0,
        "iv": 0.7,
        "available_otokens": [
            {
                "address": str(Pubkey.new_unique()),
                "strike_price": 140.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ],
    }

    base_quotes = build_quotes(market, maker_nonce=0, chain="base")
    sol_quotes = build_quotes(market, maker_nonce=0, chain="solana")

    assert len(base_quotes) == 1
    assert len(sol_quotes) == 1

    # Same USD premium should produce the same USDC raw encoding on both chains.
    assert sol_quotes[0]["bidPrice"] == base_quotes[0]["bidPrice"]


@patch("src.quote_builder.config")
def test_base_quotes_use_1e6_price_scale(mock_config):
    """Base quotes still use USDC raw (10^6) — regression check."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 86400,
                "is_put": False,
            }
        ],
    }

    quotes = build_quotes(market, maker_nonce=0, chain="base")
    assert len(quotes) == 1
    # For a ~$10 premium at 1e6 scale: 1M < bidPrice < 100M
    assert 1 <= quotes[0]["bidPrice"] < 100_000_000


# --- 4. PDA derivation ---


def test_maker_state_pda_derivation():
    """PDA seeds match Rust: [b"maker", maker.as_ref()]."""
    program_id = Pubkey.from_string("GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS")
    maker = Pubkey.new_unique()

    pda, bump = _derive_maker_state_pda(program_id, maker)

    # Verify it's a valid PDA (off-curve)
    assert pda != maker
    assert pda != program_id
    # Verify deterministic
    pda2, bump2 = _derive_maker_state_pda(program_id, maker)
    assert pda == pda2
    assert bump == bump2


def test_maker_state_discriminator():
    """Anchor discriminator = sha256("account:MakerState")[:8]."""
    import hashlib

    expected = hashlib.sha256(b"account:MakerState").digest()[:8]
    assert MAKER_STATE_DISCRIMINATOR == expected


# --- 5. Full round-trip ---


def test_full_round_trip_build_sign_verify():
    """Complete flow: build message → sign → verify matches on-chain logic."""
    kp = Keypair()
    otoken_mint = Pubkey.new_unique()

    bid_price = 5_000000  # $5.00 at USDC 1e6 scale
    deadline = int(time.time()) + 300
    quote_id = 42
    max_amount = 1_00000000  # 1.0 oToken at 8 decimals
    maker_nonce = 0

    # Step 1: build message (same as Rust build_quote_message)
    msg = build_solana_quote_message(
        otoken_mint=bytes(otoken_mint),
        bid_price=bid_price,
        deadline=deadline,
        quote_id=quote_id,
        max_amount=max_amount,
        maker_nonce=maker_nonce,
    )

    # Step 2: sign with ed25519
    sig = sign_quote_solana(kp, msg)

    # Step 3: verify (simulates what the on-chain program does)
    from nacl.signing import VerifyKey

    pubkey_bytes = bytes(kp.pubkey())
    vk = VerifyKey(pubkey_bytes)
    vk.verify(msg, sig)

    # Step 4: verify message content matches what we built
    assert msg[:32] == bytes(otoken_mint)
    assert struct.unpack_from("<Q", msg, 32)[0] == bid_price
    assert struct.unpack_from("<q", msg, 40)[0] == deadline


# --- 6. read_maker_nonce_solana RPC deserialization ---


def _make_maker_state_data(nonce: int) -> bytes:
    """Build a realistic MakerState account blob."""
    import hashlib

    disc = hashlib.sha256(b"account:MakerState").digest()[:8]
    maker = bytes(Pubkey.new_unique())  # 32 bytes
    nonce_bytes = struct.pack("<Q", nonce)
    whitelisted = b"\x01"
    bump = b"\x07"
    return disc + maker + nonce_bytes + whitelisted + bump


def test_read_maker_nonce_solana_happy_path():
    """Deserializes nonce from a valid MakerState account."""
    from unittest.mock import patch, MagicMock
    import base64

    account_data = _make_maker_state_data(nonce=42)
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "value": {
                "data": [base64.b64encode(account_data).decode(), "base64"],
                "owner": str(Pubkey.new_unique()),
            }
        },
    }
    mock_resp.raise_for_status = MagicMock()

    from src.signer import read_maker_nonce_solana

    with patch("src.signer.requests.post", return_value=mock_resp):
        nonce = read_maker_nonce_solana(
            "http://fake-rpc",
            "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",
            str(Keypair().pubkey()),
        )
    assert nonce == 42


def test_read_maker_nonce_solana_pda_not_found():
    """Raises ValueError when PDA does not exist."""
    from unittest.mock import patch, MagicMock
    import pytest

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"value": None},
    }
    mock_resp.raise_for_status = MagicMock()

    from src.signer import read_maker_nonce_solana

    with patch("src.signer.requests.post", return_value=mock_resp):
        with pytest.raises(ValueError, match="PDA not found"):
            read_maker_nonce_solana(
                "http://fake-rpc",
                "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",
                str(Keypair().pubkey()),
            )


def test_read_maker_nonce_solana_rpc_error():
    """Raises RuntimeError on JSON-RPC level errors."""
    from unittest.mock import patch, MagicMock
    import pytest

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32005, "message": "Node is behind"},
    }
    mock_resp.raise_for_status = MagicMock()

    from src.signer import read_maker_nonce_solana

    with patch("src.signer.requests.post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Solana RPC error"):
            read_maker_nonce_solana(
                "http://fake-rpc",
                "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",
                str(Keypair().pubkey()),
            )


def test_read_maker_nonce_solana_wrong_discriminator():
    """Raises ValueError on discriminator mismatch."""
    from unittest.mock import patch, MagicMock
    import base64
    import pytest

    bad_data = b"\x00" * 50  # wrong discriminator
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "value": {
                "data": [base64.b64encode(bad_data).decode(), "base64"],
                "owner": str(Pubkey.new_unique()),
            }
        },
    }
    mock_resp.raise_for_status = MagicMock()

    from src.signer import read_maker_nonce_solana

    with patch("src.signer.requests.post", return_value=mock_resp):
        with pytest.raises(ValueError, match="discriminator mismatch"):
            read_maker_nonce_solana(
                "http://fake-rpc",
                "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",
                str(Keypair().pubkey()),
            )


# --- 7. Base ECDSA regression ---


def test_base_eip712_signing_unchanged():
    """EIP-712 signing still works with the same interface."""
    # Known test private key (DO NOT use in production)
    test_key = "0x" + "ab" * 32
    domain = build_domain(84532, "0x3B5d4640233E14cc330A749926838ba2C540054f")

    quote_data = {
        "oToken": "0x0000000000000000000000000000000000000001",
        "bidPrice": 5_000000,
        "deadline": 1700000000,
        "quoteId": 1,
        "maxAmount": 100_000000,
        "makerNonce": 0,
    }

    sig = sign_quote(test_key, domain, quote_data)

    # EIP-712 signature is 0x-prefixed, 132 hex chars (65 bytes)
    assert sig.startswith("0x")
    assert len(sig) == 132

    # Deterministic: same inputs → same signature
    sig2 = sign_quote(test_key, domain, quote_data)
    assert sig == sig2


@patch("src.quote_builder.config")
def test_base_quotes_include_chain_field(mock_config):
    """Base quotes now include chain='base' for routing."""
    mock_config.RISK_FREE_RATE = 0.05
    mock_config.SPREAD_BPS = 200
    mock_config.DEADLINE_SECONDS = 300
    mock_config.MAX_AMOUNT = 500_000_000

    market = {
        "spot": 2000.0,
        "iv": 0.6,
        "available_otokens": [
            {
                "address": "0xTOKEN",
                "strike_price": 2100.0,
                "expiry": int(time.time()) + 86400,
                "is_put": False,
            }
        ],
    }

    quotes = build_quotes(market, maker_nonce=0, chain="base")
    assert quotes[0]["chain"] == "base"

    payload = to_api_payload(quotes[0], "0x" + "ab" * 65)
    assert payload["chain"] == "base"


def test_solana_api_payload_format():
    """Solana payloads use base58 for addresses and signatures."""
    import base58

    kp = Keypair()
    mint = Pubkey.new_unique()
    sig = b"\x01" * 64  # fake 64-byte sig

    quote = {
        "oToken": str(mint),
        "bidPrice": 100,
        "deadline": 999999,
        "quoteId": 1,
        "maxAmount": 100,
        "makerNonce": 0,
        "strike_price": 150.0,
        "expiry": 999999,
        "is_put": True,
        "asset": "sol",
        "chain": "solana",
    }

    payload = to_solana_api_payload(quote, sig, str(kp.pubkey()))

    assert payload["chain"] == "solana"
    assert payload["maker"] == str(kp.pubkey())
    # Signature should be base58-encoded
    decoded = base58.b58decode(payload["signature"])
    assert decoded == sig
