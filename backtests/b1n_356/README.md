# B1N-356 CSP Fund policy v2

This experiment evaluates a standalone ETH/USDC cash-secured-put fund with
physical WETH assignment. It does not reuse the B1N-345 cash-settlement shortcut:
an ITM put spends its USDC collateral, adds WETH inventory, marks that inventory
in NAV, and reduces the cash available to future positions.

## Reproduce

```bash
uv run python scripts/run_b1n_356_policy.py --root .
```

The runner verifies the immutable source digest before producing results:

- Source: `backtests/b1n_345/production_data/ETH/market.json`
- SHA-256: `2313ba17f1466fb92f7863e74476e18b6e9c62bd443094487dcf5116812814c1`
- Observations: Deribit ETH perpetual hourly closes and DVOL, ending
  `2026-07-15T08:00:00+00:00`
- Premiums: modeled by the Binary pricing implementation, never labeled as
  observed fills

## Method

- Candidate family is fixed in `config.json` before result generation.
- Development uses `2023-08-15` through `2025-07-15`.
- Validation uses the untouched final year through `2026-07-15`.
- Validation covers rolling 30/90/180-day windows under base and stressed costs.
- Covered calls and automatic WETH-to-USDC deallocation are disabled.
- New entries stop when WETH reaches 25% of NAV.
- The `options-scenarios` holdout boundary is not accessed.

## Interpretation

The result distinguishes two decisions:

- Economic/mainnet decision: requires all return/risk gates and observed
  liquidity, assignment, fund-flow, and NAV-liability evidence.
- Base Sepolia validation decision: may authorize a tightly capped mock-asset
  run solely to gather that missing operational evidence.

See `results/REPORT.md` for the decision and validation table, `summary.json`
for candidate ranking, and `policies/csp_fund_policy.v2.json` for the
machine-readable authorization.
