"""Mint LUSD to MM wallet and approve BatchSettler to spend it.

Usage: uv run python scripts/fund_mm.py
"""

import sys
import time

from eth_account import Account
from web3 import Web3

from src.config import BATCH_SETTLER, MM_PRIVATE_KEY, RPC_URL

LUSD = "0x5A2972d3390ABe3E57010272c8032BfC84E2077b"

# Mint 100,000 LUSD (6 decimals)
MINT_AMOUNT = 100_000 * 10**6

ERC20_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def send_tx(w3: Web3, tx: dict) -> str:
    signed = w3.eth.account.sign_transaction(tx, MM_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        print("  REVERTED!", file=sys.stderr)
        sys.exit(1)
    print(f"  confirmed in block {receipt['blockNumber']}")
    return tx_hash.hex()


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    mm = Account.from_key(MM_PRIVATE_KEY)
    mm_addr = mm.address
    lusd = w3.eth.contract(
        address=Web3.to_checksum_address(LUSD), abi=ERC20_ABI
    )
    settler = Web3.to_checksum_address(BATCH_SETTLER)

    bal_before = lusd.functions.balanceOf(mm_addr).call()
    print(f"MM address: {mm_addr}")
    print(f"LUSD balance: {bal_before / 1e6:.2f}")

    # 1. Mint LUSD
    print(f"\nMinting {MINT_AMOUNT / 1e6:.0f} LUSD...")
    nonce = w3.eth.get_transaction_count(mm_addr)
    tx = lusd.functions.mint(mm_addr, MINT_AMOUNT).build_transaction({
        "from": mm_addr,
        "nonce": nonce,
        "gas": 100_000,
    })
    send_tx(w3, tx)

    bal_after = lusd.functions.balanceOf(mm_addr).call()
    print(f"LUSD balance after mint: {bal_after / 1e6:.2f}")

    # 2. Approve BatchSettler for max
    allowance = lusd.functions.allowance(mm_addr, settler).call()
    if allowance < MINT_AMOUNT:
        print(f"\nApproving BatchSettler ({settler[:10]}...) for max LUSD...")
        nonce = w3.eth.get_transaction_count(mm_addr)
        tx = lusd.functions.approve(
            settler, 2**256 - 1
        ).build_transaction({
            "from": mm_addr,
            "nonce": nonce,
            "gas": 100_000,
        })
        send_tx(w3, tx)
        new_allowance = lusd.functions.allowance(mm_addr, settler).call()
        print(f"Allowance: {new_allowance}")
    else:
        print(f"\nBatchSettler already approved (allowance: {allowance})")

    print("\nDone! MM is funded and approved.")


if __name__ == "__main__":
    main()
