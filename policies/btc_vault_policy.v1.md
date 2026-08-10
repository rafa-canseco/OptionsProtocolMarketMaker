# B1N-440 BTC product and risk decision — v1

## Decision and authority boundary

**Functional-go for Binary's existing Base Sepolia LBTC test asset; no-go for
standalone BTC CSP, standalone BTC Covered Call, and BTC Wheel activation.**
`test_asset_functional_go=true` proves that the existing token can be used for
functional plumbing, while `strategy_activation_go=false`,
`activation_allowed=false`, BTC runtime is disabled by default, and
`mainnet_authorized=false`. This package deploys nothing and does not make or
sign transactions.

The bound test asset is Loot BTC (`LBTC`) at
`0x39fA11EbBE82699Fd9F79C566D7384064571d2b4` on Base Sepolia (chain 84532),
with 8 decimals and runtime codehash
`0x599a6b80cccf2c7082103129c3725529a49d37b569dc0ecc031f0444b0ce0fff`.
Its verified `MockERC20` source exposes unrestricted
`mint(address,uint256)`. LBTC is therefore **testnet-only**, is not cbBTC, and
must never be represented as production-backed BTC.

The machine-readable authority is
`btc_vault_policy.v1.base-sepolia.json`; its exact bytes are pinned by
`btc_vault_policy.v1.base-sepolia.sha256`. Research cutoff is
`2026-08-08T15:04:52Z`. Historical B1N-345 inputs end at
`2026-07-15T08:00:00Z`.

## BITA public operating profile

Primary iShares/BlackRock sources establish the following operating analog:

- gross call overwrite is approximately **25-35% of NAV**, centered around 30%;
- four weekly expiry buckets target approximately **7.5% each**;
- calls are at-the-money IBIT calls and must be covered by optionable IBIT;
- collateral, liquidity, creations/redemptions, and option obligations are
  monitored daily;
- distributions are intended monthly and depend on premiums collected, but the
  Trust is not obligated to make distributions.

BITA publishes **no numeric target delta and no CSP leg**. Those fields remain
`null`; neither may be inferred from one holdings date.

### Official 2026-08-06 holdings reproduction

The exact downloaded CSV is pinned in
`backtests/b1n_440/fixtures/BITA_holdings_2026-08-06.csv`.

| Calculation | Result |
|---|---:|
| Equal call rows | 4 × 1,236 contracts |
| Strike multiset | 35.5 / 36.5 / 36.5 / 37.5 USD |
| Optionable IBIT shares | 522,788 |
| IBIT shares covered by calls | 494,400 |
| Covered share ratio | 94.5699% |
| Derived holdings NAV | $59,886,389.06 |
| Gross strike notional | $18,045,600.00 |
| Gross call overwrite / NAV | 30.1331% |
| Observed weighted delta | 0.50457646 |

BlackRock states that displayed option notional is delta-adjusted. The weighted
observation is reproduced as:

`sum(abs(option notional)) / (covered shares × IBIT unit value)`

where the official snapshot implies IBIT unit value `$19,076,534.12 / 522,788 =
$36.49`. **0.50457646 is an observed, date-specific derived value, never a target.**
The public CSV identifies only `AUG26`; exact expiries and DTEs are unknown and
are not invented.

## Base Sepolia qualification

All checks fail closed. LBTC identity is a positive test-asset observation; it
cannot substitute for any current-stack prerequisite.

| Prerequisite | Result | Evidence / blocker |
|---|---|---|
| Binary LBTC test asset | **FUNCTIONAL-GO** | At pinned Base Sepolia block 45,216,602, read-only calls returned Loot BTC / LBTC / 8 decimals and exact runtime codehash `0x599a…fff`; an `eth_call` from a non-deployer caller reached unrestricted `mint(address,uint256)` without broadcasting state. |
| Production-backed BTC / canonical cbBTC | **NO-GO** | LBTC is an unrestricted-mint mock. It neither establishes cbBTC identity nor satisfies peg, reserve, redemption, or production custody requirements. |
| BTC/USD price feed evidence | Observed at cutoff | Official Chainlink proxy `0x0FB99723Aee6f420beAD13e6bBB79b7E6F034298`; at Base Sepolia block 45,180,459 it returned `BTC / USD`, 8 decimals, a positive answer, and age 446 seconds inside the official 1,200-second heartbeat. This does not attest a current v2 Oracle asset binding. |
| Current v2 Oracle LBTC binding | **NO-GO** | No current Oracle address or exact LBTC/BTC-USD binding was attested. |
| Current v2 Whitelist BTC products | **NO-GO** | No current Whitelist address, LBTC PUT/CALL product identities, expiry calendar, strike tick, or exercise style was attested. |
| Current v2 router and pool | **NO-GO** | No current router/pool identity or LBTC route/custody readback was attested. |
| Physical settlement | **NO-GO** | No CSP LBTC assignment, CC LBTC delivery, custody/decimal reconciliation, or Wheel asset transition was observed. |
| Executable liquidity | **NO-GO** | No executable BTC option bids, size, spread, slippage, or repeatable capacity were observed. |

The read-only feed observation is pinned to block hash
`0x1f641d55b7fb27eab500346db35488ca8476dd97605b427a9f2261e8625f08e7`.
Every feed read used that block. `updatedAt` is required to be positive, no
later than the block, and within the directory heartbeat. It is BTC/USD price
evidence only—not a current v2 Oracle binding, product, settlement, premium, or
liquidity attestation. LBTC identity reads are independently pinned to block
`0x0975c10ab5feb466284f29c2d34a38cc3131024c3d5417446ad029076b285598`.
The mint proof is an `eth_call`, not a transaction.

## Independent strategy decisions

### Standalone CSP — no-go

BITA supplies no CSP leg. The pinned B1N-345 BTC CSP cohort independently tests
48-hour cadence, 100% utilization, zero modeled minimum premium, and put deltas
0.10/0.20/0.30/0.40 over 30/90/180-day windows. All 12 rows fail at least one
pre-existing historical gate; all also lack executable premium/liquidity and an
attested current v2 LBTC product. No BTC CSP delta, DTE, premium floor, or capacity
is selected for activation.

### Standalone Covered Call — no-go

The public operating analog is retained only as a research profile: 25-35% gross
overwrite, approximately 30% center, four approximately 7.5% weekly ATM buckets,
fully covered, and daily monitoring. It does not establish LBTC DTE, target
delta, executable premium, or on-chain capacity. B1N-345 has no independent
standalone BTC Covered Call cohort; contingent Wheel call rows are not relabeled.
No activation parameters are selected.

### Wheel — no-go

The pinned B1N-345 BTC Wheel cohort independently tests 48-hour cadence, 100%
utilization, zero modeled minimum premium, put deltas 0.10/0.20/0.30/0.40, zero
call margin, and literal-lot protection over 30/90/180-day windows. All 12 rows
fail at least one pre-existing historical gate. Wheel also depends on both
standalone legs plus observed physical LBTC transition reconciliation, all of
which are no-go. No activation parameters are selected.

## Evidence classification and prior-policy boundary

B1N-345 spot/perpetual closes and DVOL are observed. Its option premiums, hedge
PnL, capacity, and liquidity are modeled. **Modeled or testnet premiums are not
executable yield.** `parameter_matrix.json` retains this distinction on every
row.

B1N-413 fee semantics and functional Wheel protections were reviewed but do not
make BTC production-ready. B1N-438's 0.09 put delta is an ETH testnet policy and
is not transferred to BTC. B1N-436's ETH tenor/delta near-feasible points are
explicit no-go research and likewise are not transferred. No ETH parameter is
silently inherited as a BTC target.

## Unknowns that keep strategy activation closed

1. Current v2 Oracle address and exact LBTC/BTC-USD binding.
2. Current v2 Whitelist BTC PUT/CALL identities and product semantics.
3. Current v2 router/pool identities and LBTC route/custody readbacks.
4. Physical assignment/delivery, custody, decimal, and Wheel transition evidence.
5. Executable CSP and CC bids, premiums, spreads, slippage, size, and capacity.
6. Independent standalone BTC Covered Call historical outcomes.
7. BITA's exact 2026-08-06 expiries/DTEs and any approved BTC target delta/DTE.
8. A separately qualified production-backed BTC identity, peg, reserve, and
   redemption model if production is ever considered.

None of these may be replaced by LBTC's functional identity, a guessed current
contract address, inferred DTE, a snapshot delta, an ETH policy parameter, or
modeled liquidity.

## Reproduction

Offline artifact rebuild:

```bash
uv run --frozen python scripts/run_b1n_440_btc_research.py
```

Focused tests:

```bash
uv run --frozen pytest -q tests/test_btc_policy_research.py
```

Source URLs, retrieval times, response hashes, causal rules, and local fixture
hashes are in `backtests/b1n_440/fixtures/source_manifest.json`. Generated
artifact hashes are in `backtests/b1n_440/results/checksums.sha256`.
