# B1N-356 CSP Fund policy v2 validation

**Decision: `base_sepolia_validation_go`.**

Economic decision: **`no_go`**. Base Sepolia validation decision: **`go`**.

This research models premiums and does not claim observed Binary fills. ITM puts settle into physical WETH inventory; covered calls and automatic WETH deallocation are disabled.

## Selected candidate

| Parameter | Value |
|---|---:|
| Strike rule | fixed_moneyness_below_spot |
| Strike parameter | 15% |
| Utilization | 25% |
| Minimum net premium | 0 bps |
| Cadence | 48 hours |
| WETH inventory cap for new entries | 25% NAV |

## Authorization scope

The selected candidate remains an economic `no_go`. The authorization is limited to Base Sepolia operational validation with mock assets:

| Bound | Value |
|---|---:|
| Network | Base Sepolia only |
| Chain ID | 84532 |
| Assets | Mock only |
| Maximum vault AUM | 25.00 USDC |
| Maximum collateral per position | 6.25 USDC |
| Maximum simultaneous positions | 1 |
| Maximum positions before review | 10 |
| Maximum assignments before review | 1 |
| Mainnet | Not authorized |

## Validation comparison

Each row uses the development winner for its exact strike variant.

| Rule | Parameter | Actual distance | Actual delta | Cost | Window | N | Median | Loss probability | Worst DD | Open rate | Assignment | Gate |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| fixed_moneyness_below_spot | 10% | 10.1% | 0.014 | base | 30d | 48 | 0.11% | 25.0% | -8.26% | 100.0% | 3.7% | FAIL |
| fixed_moneyness_below_spot | 10% | 10.1% | 0.015 | base | 90d | 40 | -1.47% | 62.5% | -21.65% | 83.3% | 4.3% | FAIL |
| fixed_moneyness_below_spot | 10% | 10.1% | 0.014 | stressed | 30d | 48 | 0.11% | 25.0% | -8.26% | 100.0% | 3.7% | FAIL |
| fixed_moneyness_below_spot | 10% | 10.1% | 0.014 | stressed | 90d | 40 | -1.47% | 62.5% | -21.65% | 83.3% | 4.3% | FAIL |
| fixed_moneyness_below_spot | 15% | 15.1% | 0.000 | base | 30d | 48 | 0.00% | 4.2% | -6.04% | 100.0% | 0.3% | FAIL |
| fixed_moneyness_below_spot | 15% | 15.1% | 0.001 | base | 90d | 40 | 0.02% | 15.0% | -6.04% | 100.0% | 0.4% | FAIL |
| fixed_moneyness_below_spot | 15% | 15.1% | 0.000 | stressed | 30d | 48 | 0.00% | 4.2% | -6.04% | 100.0% | 0.3% | FAIL |
| fixed_moneyness_below_spot | 15% | 15.1% | 0.001 | stressed | 90d | 40 | 0.01% | 15.0% | -6.04% | 100.0% | 0.4% | FAIL |
| target_put_delta | 5% | 7.8% | 0.050 | base | 30d | 48 | 0.39% | 22.9% | -9.79% | 70.0% | 6.1% | FAIL |
| target_put_delta | 5% | 7.8% | 0.050 | base | 90d | 40 | -1.44% | 57.5% | -21.44% | 35.6% | 8.3% | FAIL |
| target_put_delta | 5% | 7.8% | 0.050 | stressed | 30d | 48 | 0.38% | 22.9% | -9.79% | 73.3% | 5.8% | FAIL |
| target_put_delta | 5% | 7.9% | 0.050 | stressed | 90d | 40 | -0.17% | 50.0% | -21.44% | 35.6% | 7.9% | FAIL |
| target_put_delta | 10% | 6.3% | 0.101 | base | 30d | 48 | 0.28% | 16.7% | -7.91% | 43.3% | 7.7% | FAIL |
| target_put_delta | 10% | 6.4% | 0.100 | base | 90d | 40 | 0.01% | 50.0% | -21.20% | 33.3% | 8.2% | FAIL |
| target_put_delta | 10% | 6.4% | 0.100 | stressed | 30d | 48 | 0.36% | 10.4% | -7.94% | 40.0% | 7.1% | FAIL |
| target_put_delta | 10% | 6.4% | 0.100 | stressed | 90d | 40 | 0.13% | 42.5% | -21.19% | 33.3% | 8.2% | FAIL |
| target_put_delta | 15% | 4.9% | 0.150 | base | 30d | 48 | 1.20% | 35.4% | -16.59% | 60.0% | 14.2% | FAIL |
| target_put_delta | 15% | 5.0% | 0.150 | base | 90d | 40 | -7.42% | 80.0% | -21.03% | 23.3% | 15.6% | FAIL |
| target_put_delta | 15% | 4.9% | 0.150 | stressed | 30d | 48 | 1.20% | 35.4% | -16.59% | 60.0% | 14.2% | FAIL |
| target_put_delta | 15% | 5.0% | 0.150 | stressed | 90d | 40 | -7.44% | 80.0% | -21.03% | 23.3% | 15.6% | FAIL |

## Evidence boundary

The backtest can validate causal accounting and risk behavior, but it cannot turn modeled premiums into observed executable liquidity. The following evidence remains explicit:

- `observed_executable_quote_liquidity`: missing
- `observed_onchain_physical_assignment`: missing
- `observed_fund_flow_reconciliation`: missing
- `observed_nav_liability_reconciliation`: missing

A `base_sepolia_validation_go`, if issued, is testnet-only and capped. It is intended to gather the missing evidence and is not an economic or mainnet recommendation. It does not waive failed return or loss-probability gates.
