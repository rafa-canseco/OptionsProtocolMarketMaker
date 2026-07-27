# B1N-358

Reproducible 30/90-day ETH covered-call policy research. The source dataset is
the immutable ETH market snapshot already checked into `backtests/b1n_345`.

Run:

```bash
uv run python scripts/run_b1n_358_policy.py
```

The runner selects parameters on the development split, evaluates that fixed
selection on the final validation split under base and stressed costs, and writes
the policy plus checksummed evidence.
