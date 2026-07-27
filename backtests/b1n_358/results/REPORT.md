# B1N-358 — 48-hour ETH Covered Call Policy

- Research decision: `base_sepolia_validation_go`
- Runtime decision: `go_testnet_only`
- Selected on development only: `target_call_delta_0p05__u_0p25__p_10__base`
- Accounting asset: WETH; USDC is transient and normalized only after settlement.
- Premium source: modeled Binary bid, not observed executable liquidity.

## 30/90-day validation

| Scenario | Window | Samples | Median WETH return | Loss probability | Worst drawdown | Call-away | Open rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| base | 30 | 48 | 0.0766% | 22.92% | -1.86% | 4.53% | 63.33% |
| base | 90 | 40 | 0.2698% | 30.00% | -2.52% | 3.55% | 61.11% |
| stressed | 30 | 48 | 0.0253% | 31.25% | -2.56% | 4.79% | 60.00% |
| stressed | 90 | 40 | 0.0327% | 47.50% | -3.81% | 3.76% | 57.78% |

## Interpretation

A call-away is represented physically: locked WETH leaves the strategy, strike proceeds arrive as USDC, and the full transient USDC balance is converted back to WETH before another call can open. NAV subtracts the current fair value of the live short call; locked WETH remains an asset.

The Base Sepolia authorization is functional only. It does not waive the missing executable-liquidity, live physical-settlement, fund-flow, NAV, or normalization evidence needed for an economic/mainnet go.
