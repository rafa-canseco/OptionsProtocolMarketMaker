# B1N-345 production validation — ETH and BTC

> Research only. BTC does not change the ETH/USDC v2 Milestone 1 scope.

All option premiums, MM hedging and capacity curves are modeled. Spot/perpetual
closes and DVOL inputs are observed Deribit data. No Binary fills are claimed.

## Coverage

| Asset | Window | Rolling samples | Coverage | Gate |
|---|---:|---:|---:|---|
| BTC | 30d | 533 | 100.00% | PASS |
| BTC | 90d | 503 | 100.00% | PASS |
| BTC | 180d | 458 | 100.00% | PASS |
| ETH | 30d | 533 | 100.00% | PASS |
| ETH | 90d | 503 | 100.00% | PASS |
| ETH | 180d | 458 | 100.00% | PASS |

## Executive verdict

Production-ready fixed policies: **0**.
A zero count means the research does not authorize allocator activation.
The most common blocking checks are reported per policy in `summary.json`.

## Fixed-policy distributions

| Asset | Window | Delta | N raw | N effective | P5 | Median | P95 | Loss prob. | Worst DD | MM median | Capacity | Ready |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| BTC | 30d | 0.10 | 533 | 36 | -10.54% | 3.09% | 5.69% | 16.7% | -25.81% | -1.43% | $0 | FAIL |
| BTC | 30d | 0.20 | 533 | 36 | -14.15% | 5.68% | 10.20% | 26.5% | -31.51% | -2.60% | $0 | FAIL |
| BTC | 30d | 0.30 | 533 | 36 | -16.41% | 6.23% | 13.06% | 28.1% | -33.53% | -3.13% | $0 | FAIL |
| BTC | 30d | 0.40 | 533 | 36 | -16.73% | 5.75% | 17.37% | 29.8% | -33.89% | -3.12% | $0 | FAIL |
| BTC | 90d | 0.10 | 503 | 12 | -18.85% | 10.06% | 15.40% | 22.3% | -35.05% | -2.87% | $0 | FAIL |
| BTC | 90d | 0.20 | 503 | 12 | -21.37% | 14.29% | 28.91% | 25.2% | -38.76% | -7.21% | $0 | FAIL |
| BTC | 90d | 0.30 | 503 | 12 | -22.52% | 14.77% | 36.66% | 31.2% | -39.30% | -8.08% | $0 | FAIL |
| BTC | 90d | 0.40 | 503 | 12 | -23.06% | 14.52% | 47.83% | 33.6% | -39.88% | -8.07% | $0 | FAIL |
| BTC | 180d | 0.10 | 458 | 6 | -37.60% | 23.16% | 32.24% | 25.5% | -45.47% | -3.19% | $0 | FAIL |
| BTC | 180d | 0.20 | 458 | 6 | -37.08% | 33.98% | 57.56% | 25.8% | -46.68% | -17.95% | $0 | FAIL |
| BTC | 180d | 0.30 | 458 | 6 | -37.24% | 30.22% | 75.15% | 26.4% | -48.08% | -18.09% | $0 | FAIL |
| BTC | 180d | 0.40 | 458 | 6 | -37.53% | 28.69% | 93.25% | 27.3% | -48.73% | -14.44% | $0 | FAIL |
| ETH | 30d | 0.10 | 533 | 36 | -21.97% | 3.69% | 7.99% | 29.5% | -39.04% | -0.65% | $0 | FAIL |
| ETH | 30d | 0.20 | 533 | 36 | -23.78% | 4.88% | 11.01% | 35.1% | -40.43% | -1.39% | $0 | FAIL |
| ETH | 30d | 0.30 | 533 | 36 | -24.78% | 3.66% | 17.02% | 38.1% | -42.60% | -1.62% | $0 | FAIL |
| ETH | 30d | 0.40 | 533 | 36 | -25.47% | 2.50% | 22.07% | 41.8% | -44.32% | -1.89% | $0 | FAIL |
| ETH | 90d | 0.10 | 503 | 12 | -35.39% | 6.57% | 20.81% | 39.0% | -53.92% | -0.90% | $0 | FAIL |
| ETH | 90d | 0.20 | 503 | 12 | -37.94% | 6.91% | 31.52% | 44.5% | -54.74% | -3.16% | $0 | FAIL |
| ETH | 90d | 0.30 | 503 | 12 | -39.34% | 7.40% | 43.41% | 45.1% | -55.18% | -4.31% | $0 | FAIL |
| ETH | 90d | 0.40 | 503 | 12 | -39.66% | 7.58% | 50.26% | 43.9% | -56.93% | -4.10% | $0 | FAIL |
| ETH | 180d | 0.10 | 458 | 6 | -42.00% | 5.94% | 38.45% | 43.4% | -60.85% | -0.99% | $0 | FAIL |
| ETH | 180d | 0.20 | 458 | 6 | -48.16% | 4.99% | 53.92% | 43.2% | -61.92% | -8.01% | $0 | FAIL |
| ETH | 180d | 0.30 | 458 | 6 | -47.58% | -1.06% | 70.57% | 51.1% | -62.80% | -7.21% | $0 | FAIL |
| ETH | 180d | 0.40 | 458 | 6 | -49.68% | -0.66% | 76.49% | 51.3% | -63.34% | -6.97% | $0 | FAIL |

## Acceptance interpretation

A policy passes only when its median beats the period-equivalent USDC hurdle,
loss probability and worst drawdown remain within their fixed limits, median
hedged MM PnL is non-negative, and every requested regime has enough samples.
Capacity is a sensitivity model, not an observed liquidity limit.
