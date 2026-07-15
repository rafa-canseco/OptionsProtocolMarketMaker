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

| Asset | Window | Delta | N | P5 | Median | P95 | Loss prob. | Worst DD | MM median | Capacity | Ready |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| BTC | 30d | 0.10 | 533 | -10.85% | 2.96% | 5.75% | 20.8% | -26.45% | -1.35% | $0 | FAIL |
| BTC | 30d | 0.20 | 533 | -14.84% | 5.69% | 9.66% | 25.7% | -27.59% | -2.44% | $0 | FAIL |
| BTC | 30d | 0.30 | 533 | -15.76% | 6.01% | 13.14% | 29.6% | -30.03% | -3.17% | $0 | FAIL |
| BTC | 30d | 0.40 | 533 | -16.47% | 5.82% | 17.10% | 30.8% | -31.38% | -2.96% | $0 | FAIL |
| BTC | 90d | 0.10 | 503 | -20.27% | 9.28% | 17.97% | 24.1% | -34.22% | -3.05% | $0 | FAIL |
| BTC | 90d | 0.20 | 503 | -20.29% | 13.82% | 27.05% | 25.6% | -35.52% | -6.80% | $0 | FAIL |
| BTC | 90d | 0.30 | 503 | -22.04% | 13.61% | 36.71% | 32.2% | -36.84% | -7.89% | $0 | FAIL |
| BTC | 90d | 0.40 | 503 | -22.81% | 13.92% | 49.66% | 34.0% | -37.15% | -7.29% | $0 | FAIL |
| BTC | 180d | 0.10 | 458 | -36.81% | 20.54% | 33.64% | 26.2% | -44.43% | -5.96% | $0 | FAIL |
| BTC | 180d | 0.20 | 458 | -36.18% | 33.23% | 55.15% | 25.5% | -45.80% | -15.56% | $0 | FAIL |
| BTC | 180d | 0.30 | 458 | -37.40% | 29.45% | 75.23% | 26.4% | -47.47% | -16.26% | $0 | FAIL |
| BTC | 180d | 0.40 | 458 | -36.72% | 27.69% | 99.52% | 27.3% | -47.76% | -12.81% | $0 | FAIL |
| ETH | 30d | 0.10 | 533 | -21.39% | 3.53% | 6.87% | 32.5% | -37.30% | -0.54% | $0 | FAIL |
| ETH | 30d | 0.20 | 533 | -22.52% | 4.93% | 11.10% | 35.1% | -39.03% | -1.18% | $0 | FAIL |
| ETH | 30d | 0.30 | 533 | -24.66% | 3.67% | 16.26% | 38.6% | -40.94% | -1.32% | $0 | FAIL |
| ETH | 30d | 0.40 | 533 | -25.41% | 2.60% | 22.59% | 42.2% | -42.39% | -1.60% | $0 | FAIL |
| ETH | 90d | 0.10 | 503 | -36.48% | 3.68% | 22.14% | 41.9% | -51.67% | -1.69% | $0 | FAIL |
| ETH | 90d | 0.20 | 503 | -36.54% | 7.07% | 32.59% | 43.7% | -52.55% | -2.96% | $0 | FAIL |
| ETH | 90d | 0.30 | 503 | -38.81% | 7.33% | 42.42% | 44.7% | -53.01% | -3.53% | $0 | FAIL |
| ETH | 90d | 0.40 | 503 | -40.04% | 6.77% | 51.89% | 44.3% | -54.18% | -3.41% | $0 | FAIL |
| ETH | 180d | 0.10 | 458 | -49.30% | 1.60% | 37.73% | 48.7% | -59.10% | -3.17% | $0 | FAIL |
| ETH | 180d | 0.20 | 458 | -48.36% | -2.85% | 51.54% | 51.7% | -60.59% | -6.82% | $0 | FAIL |
| ETH | 180d | 0.30 | 458 | -48.45% | -2.19% | 65.31% | 51.7% | -61.31% | -6.04% | $0 | FAIL |
| ETH | 180d | 0.40 | 458 | -49.78% | -1.74% | 79.14% | 51.5% | -61.79% | -5.36% | $0 | FAIL |

## Acceptance interpretation

A policy passes only when its median beats the period-equivalent USDC hurdle,
loss probability and worst drawdown remain within their fixed limits, median
hedged MM PnL is non-negative, and every requested regime has enough samples.
Capacity is a sensitivity model, not an observed liquidity limit.
