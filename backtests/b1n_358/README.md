# B1N-358

Reproducible 30/90-day ETH covered-call policy research. The source dataset is
the immutable ETH market snapshot already checked into `backtests/b1n_345`.

Run:

```bash
uv run python scripts/run_b1n_358_policy.py
```

The runner selects parameters on the development split, evaluates that fixed
selection on the final validation split under base and stressed costs, and writes
the versioned policy plus checksummed evidence. Policy v2 also binds the
covered-call fair-NAV model, IV source, spot-feed freshness, signer quorum and
observation divergence/window; these are integration inputs, not additional
backtest degrees of freedom. The runner also writes a checksum companion next
to the machine-readable policy for deployment pinning.
