# B1N-346 CSP Fund Policy v1

## Decision

**No-go.** B1N-341 must not activate the allocator. The Base Sepolia override
also fails closed with zero AUM, utilization, and per-position collateral.

## Gates

The gates were fixed in B1N-345 before production returns were inspected:

| Gate | Value |
|---|---:|
| Maximum loss probability | 25% |
| Worst acceptable drawdown | -30% |
| 30-day return hurdle | 0.650% |
| 90-day return hurdle | 1.962% |
| 180-day return hurdle | 3.963% |
| Assignment evidence | Physical WETH assignment required; absent, FAIL |
| Liquidity evidence | Observed executable quote liquidity required; absent, FAIL |

Every tested standalone CSP delta breached the loss-probability gate. The
lowest-risk tested candidate, delta 0.10, had rolling loss probabilities of
36.585%, 52.286%, and 67.686% over 30, 90, and 180 days. Its 90- and 180-day
drawdowns also breached the -30% limit.

| Window | Delta | Median return | Loss probability | ES 5% | Worst DD |
|---:|---:|---:|---:|---:|---:|
| 30d | 0.10 | 0.906% | 36.585% | -20.676% | -29.426% |
| 30d | 0.20 | 1.744% | 35.647% | -22.811% | -33.002% |
| 30d | 0.30 | 2.725% | 35.647% | -23.568% | -34.777% |
| 30d | 0.40 | 3.123% | 36.585% | -23.896% | -36.536% |
| 90d | 0.10 | -0.225% | 52.286% | -26.290% | -36.767% |
| 90d | 0.20 | 3.103% | 41.948% | -27.719% | -39.538% |
| 90d | 0.30 | 7.977% | 39.364% | -27.428% | -38.993% |
| 90d | 0.40 | 10.756% | 35.785% | -28.464% | -38.182% |
| 180d | 0.10 | -3.421% | 67.686% | -30.293% | -36.767% |
| 180d | 0.20 | -1.416% | 51.965% | -36.860% | -41.580% |
| 180d | 0.30 | 5.866% | 43.450% | -41.186% | -46.897% |
| 180d | 0.40 | 12.580% | 38.210% | -43.264% | -50.178% |

Assignment and liquidity criteria are evidence gates rather than invented
numeric thresholds. B1N-345 cash-settles CSP assignments and models premiums,
so neither gate passes. MM profitability, modeled capacity, regime sufficiency,
and the aggregate
`production_ready` field are not allocator gates. The published production
summary is Wheel-only; this decision uses `strategy=csp_only` rows from the raw
result set.

## Traceability

The tested policy family was ETH/USDC, 48-hour cadence, 100% utilization, zero
minimum premium, base cost scenario, and target deltas 0.10, 0.20, 0.30, 0.40.
The machine-readable policy records the delta 0.10 metrics because it is the
lowest assignment-frequency and lowest-drawdown candidate, not because it was
selected. Values in that compact record are rounded to five decimal places; the
table above and raw result rows are authoritative. No strategy parameter is
selected under a no-go.

Evidence is pinned to market maker staging merge `7b8ffbd6bef1cd754c111c7679adde37b39cfa35`:

- `backtests/b1n_345/config.json`
- `backtests/b1n_345/production_results/results.jsonl.gz`
- `backtests/b1n_345/production_results/coverage_probe.json`
- `backtests/b1n_345/production_results/checksums.sha256`

## Missing Evidence

No production value is assigned for quote liquidity, physical WETH inventory,
deallocation loss, withdrawal outflows, reporter freshness, option-liability
haircuts, concentration, or per-position collateral. B1N-345 CSP-only results
cash-settle assignments and therefore do not validate the standalone fund's
physical WETH inventory behavior.

The tested 50%, 25%, and 0% idle levels are hypotheses derived from utilization,
not approved fund-policy values. Market-data age limits are coverage controls,
not NAV reporter bounds.

## Authority Boundary

Curators own the unset risk bounds in `curator_bounds`. The allocator may choose
actions only inside approved bounds, but v1 authorizes no allocator action.
Covered-call cost-basis evidence belongs to a future Wheel milestone and is not
part of this policy.

Changing `decision` to `go`, enabling Base Sepolia, or assigning any selected
parameter requires new validated physical-assignment, liquidity, fund-flow, and
NAV-liability evidence plus a new policy version.
