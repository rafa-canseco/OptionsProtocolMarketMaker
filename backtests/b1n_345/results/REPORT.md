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
| 30d | 16.84% | -4.53% | $1,466 | $0 | $15,375 | 0.10 | 100% | 0 bps | $100 | 1 | 0 | 30.0% |
| 90d | 1.56% | -0.20% | $1,562 | $0 | $0 | 0.10 | 100% | 25 bps | $0 | 0 | 0 | 0.0% |
| 180d | 7.23% | -15.91% | $4,957 | $2,272 | $0 | 0.10 | 100% | 25 bps | $0 | 1 | 1 | 85.0% |

### Benchmarks

| Window | Hold USDC | Hold ETH | 50/50 no rebalance |
|---:|---:|---:|---:|
| 30d | 0.00% | 8.98% | 4.49% |
| 90d | 0.00% | -20.53% | -10.26% |
| 180d | 0.00% | -43.29% | -21.64% |

## Material tradeoffs

- The leading 30-day base result is dominated by unrealized assigned-ETH PnL;
  it completed no full wheel cycle and should not be read as stable premium yield.
- The leading 90-day base result had no assignment and is a premium-only CSP path.
- The leading 180-day base result completed one cycle but assigned ETH was idle
  for most of its exposure because the protected call floor excluded lower strikes.
- Low-cost rankings are optimistic sensitivities. Base and stressed execution
  assumptions must remain visible when B1N-346 evaluates a policy.
- All positive-premium wheel rows have a modeled-premium fraction of 100%; there
  are no historical Binary fills in these windows.
- `lot_floor_breach_opportunities` quantifies occasions where an average basis
  would have permitted a call below an individual lot. The engine still enforced
  the lot's gross floor, so realized buy-low/sell-high PnL is never negative.

Full scenario rows, Pareto frontiers, sensitivities, and CSP-only comparisons are
available in `results.jsonl`, `results.csv`, and `summary.json`.
