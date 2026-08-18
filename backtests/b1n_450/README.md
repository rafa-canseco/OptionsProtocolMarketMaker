# B1N-450 — ETH CSP weekly-ladder comparison

This research-only package compares six frozen ETH CSP/Wheel arms on the same
causal market snapshot, fee stack, costs, capital and non-overlapping windows.
It does not edit the live B1N-438 policy, deploy, activate or authorize a policy
change.

## Reproduce

```bash
uv run --frozen python scripts/run_b1n_450_ladder_comparison.py
uv run --frozen pytest tests/test_ladder_comparison.py
(cd backtests/b1n_450/results && sha256sum -c checksums.sha256)
```

`config.json` pins the normalized ETH market, source settings and a byte-for-byte
snapshot of the B1N-438 0.09-delta policy. The development period ends before the
validation period begins. Each split uses non-overlapping 30/60/90-day windows;
validation does not reselect or mutate an arm.

## Interpretation

Observed Deribit ETH perpetual closes and DVOL are causal inputs. Binary option
premiums, capacity and execution are modeled, not observed fills or executable
quotes. The raw JSONL/CSV rows and normalized summary report period return,
premium yield, assignment frequency, ETH inventory/time, drawdown, CVaR, idle
capital, turnover, complete Wheel cycles, a redemption-liquidity proxy and
operational load. Base and stressed modeled costs and bull/bear/sideways/crash
regime labels are retained in machine-readable output.

The ATM arm targets absolute put delta 0.50 only as a high-assignment stress
control. No CSP rule is attributed to BlackRock. The gate-derived recommendation
has a deterministic SHA-256 content-integrity attestation; it provides no signer
authentication. Assignment frequency is per settled put and is accompanied by a
time-normalized assignments-per-30-days metric for cross-tenor comparison. Risk
metrics are marked after accrued management and hypothetical HWM performance fees. Any future change needs an independent review and a new implementation
ticket, must remain disabled by default, and needs separately approved executable
premium evidence.
