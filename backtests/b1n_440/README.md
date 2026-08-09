# B1N-440 BTC product and risk research

This package reproduces the official BITA 2026-08-06 holdings calculations and
applies the pre-existing B1N-345 production gates independently to published BTC
standalone-CSP and Wheel rows. It does not provide allocator runtime code.

Binary's existing Base Sepolia Loot BTC (`LBTC`) is bound as a functional-go
test asset at `0x39fA11EbBE82699Fd9F79C566D7384064571d2b4`. Its pinned
identity, 8 decimals, runtime codehash, verified unrestricted-mint `MockERC20`
source, and non-broadcast mint `eth_call` are reproduced separately from strategy
authority. LBTC is testnet-only, is not cbBTC, and is not production-backed BTC.

All BTC strategy decisions remain no-go. Current v2 Oracle, Whitelist, router,
pool, BTC products, physical settlement, and executable liquidity remain
unattested. Premiums, hedges, capacity, and liquidity in B1N-345 are modeled;
they are not executable quotes or yield. B1N-345 has no independent standalone
BTC Covered Call cohort, so none is invented or derived by relabeling contingent
Wheel calls.

Rebuild offline:

```bash
uv run --frozen python scripts/run_b1n_440_btc_research.py
```

Inputs are SHA-256 pinned in `fixtures/source_manifest.json`. The rebuild rejects
source-path or hash substitutions, rows after the pinned historical cutoff,
contradictory feed/directory/policy evidence, mutated LBTC identity/codehash/
mintability or activation claims, and malformed BITA call semantics. Generated
outputs and fixture hashes are listed in `results/checksums.sha256`.
The authoritative decision is
`policies/btc_vault_policy.v1.base-sepolia.json`.
