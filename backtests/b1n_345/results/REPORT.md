# B1N-345 results

> Research output only. This report does not recommend or authorize allocator activation.

## Data and interpretation

Deribit ETH/USD spot and ETH DVOL are observed historical inputs. Every option
premium is a counterfactual Binary quote produced by the production Market Maker
pricer; therefore premium-derived PnL is labeled modeled, never observed Binary PnL.

The canonical covered-call floor is strict per assignment lot:
`call strike > gross lot assignment basis + X`. Weighted-basis modes can only
tighten that floor.

## Coverage gate

| Window | Required | Observed causal | Modeled premium | Missing | Coverage |
|---:|---:|---:|---:|---:|---:|
| 30d | 15 | 15 | 15 | 0 | 100.0% |
| 90d | 45 | 45 | 45 | 0 | 100.0% |
| 180d | 90 | 90 | 90 | 0 | 100.0% |

## Results

The table uses the approved **base** execution scenario and canonical lot-level
protection. It shows the highest-return configuration in that constrained set,
not a recommendation.

| Window | Return | Max DD | Net premium | Buy-low/sell-high | Unrealized ETH | Delta | Util. | Min premium | X | Assign. | Cycles | ETH idle |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 30d | 15.94% | -5.02% | $3,302 | $12,639 | $0 | 0.10 | 100% | 0 bps | $200 | 1 | 1 | 0.0% |
| 90d | 11.24% | -5.66% | $10,706 | $531 | $0 | 0.20 | 100% | 50 bps | $0 | 2 | 2 | 21.3% |
| 180d | 20.59% | -4.03% | $9,863 | $10,730 | $0 | 0.10 | 100% | 25 bps | $200 | 1 | 1 | 60.0% |

### Benchmarks

| Window | Hold USDC | Hold ETH | 50/50 no rebalance |
|---:|---:|---:|---:|
| 30d | 0.00% | 8.98% | 4.49% |
| 90d | 0.00% | -20.53% | -10.26% |
| 180d | 0.00% | -43.29% | -21.64% |

## Material tradeoffs

- The leading 30-day base result completed 1 cycle(s), used X=$200, and had 0.0% ETH idle exposure.
- The leading 90-day base result completed 2 cycle(s), used X=$0, and had 21.3% ETH idle exposure.
- The leading 180-day base result completed 1 cycle(s), used X=$200, and had 60.0% ETH idle exposure.
- X=0 selects the first $5 strike strictly above each lot's gross basis; larger
  X values intentionally trade less call premium/frequency for a higher sale price.
- Low/base/stressed sensitivities vary Binary's embedded MM spread and
  operational delay only; sponsored gas and platform fees are not deducted
  a second time.
- All positive-premium wheel rows have a modeled-premium fraction of 100%; there
  are no historical Binary fills in these windows.
- `lot_floor_breach_opportunities` quantifies occasions where an average basis
  would have permitted a call below an individual lot. The engine still enforced
  the lot's gross floor, so realized buy-low/sell-high PnL is never negative.

Full scenario rows, Pareto frontiers, sensitivities, and CSP-only comparisons are
available in `results.jsonl`, `results.csv`, and `summary.json`.
