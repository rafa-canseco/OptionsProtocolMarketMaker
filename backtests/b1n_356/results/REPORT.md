# B1N-356 CSP Fund policy v2 validation

**Decision: `base_sepolia_validation_go`.**

Economic decision: **`no_go`**. Base Sepolia validation decision: **`go`**.

This research models premiums and does not claim observed Binary fills. ITM puts settle into physical WETH inventory; covered calls and automatic WETH deallocation are disabled.

## Selected candidate

| Parameter | Value |
|---|---:|
| Target delta | 0.150 |
| Utilization | 25% |
| Minimum net premium | 25 bps |
| Entry filter | none |
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

## Validation

| Cost | Window | N | Median | Hurdle | Loss probability | Worst DD | Open rate | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| base | 30d | 48 | 1.20% | 0.65% | 35.4% | -16.59% | 60.0% | FAIL |
| base | 90d | 40 | -7.42% | 1.96% | 80.0% | -21.03% | 23.3% | FAIL |
| base | 180d | 27 | -17.53% | 3.96% | 96.3% | -29.08% | 13.3% | FAIL |
| stressed | 30d | 48 | 1.20% | 0.65% | 35.4% | -16.59% | 60.0% | FAIL |
| stressed | 90d | 40 | -7.44% | 1.96% | 80.0% | -21.03% | 23.3% | FAIL |
| stressed | 180d | 27 | -17.54% | 3.96% | 96.3% | -29.08% | 13.3% | FAIL |

## Evidence boundary

The backtest can validate causal accounting and risk behavior, but it cannot turn modeled premiums into observed executable liquidity. The following evidence remains explicit:

- `observed_executable_quote_liquidity`: missing
- `observed_onchain_physical_assignment`: missing
- `observed_fund_flow_reconciliation`: missing
- `observed_nav_liability_reconciliation`: missing

A `base_sepolia_validation_go`, if issued, is testnet-only and capped. It is intended to gather the missing evidence and is not an economic or mainnet recommendation. It does not waive failed return or loss-probability gates.
