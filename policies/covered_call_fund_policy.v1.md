# Covered Call Fund policy v1

This is a Base Sepolia functional-validation profile only. The authoritative
machine-readable policy is `covered_call_fund_policy.v1.base-sepolia.json`.

## Strategy

- WETH is the only accounting, deposit, and redemption asset.
- Sell one ETH call targeting delta `0.05` (maximum deviation `0.015`) with a
  target expiry of 48 hours.
- Allocate 25% of current idle WETH, capped at `0.0025 WETH`.
- Require at least 10 bps net premium and a fresh coherent NAV before opening.
- At expiry, settle once. OTM returns WETH; ITM enters physical delivery.
- Premium and called-away strike proceeds remain transient USDC.
- After every terminal settlement, normalize all USDC to WETH within the
  oracle/slippage cap, return all residual WETH to the vault, then reopen if
  WETH remains and no redemption is pending.
- Stop after 10 opened positions or the first physical call-away for review.

## Runtime

Both workers are disabled by default. The allocator and redemption processor
must use different keys.

Required allocator variables:

```text
COVERED_CALL_ALLOCATOR_ENABLED
COVERED_CALL_ALLOCATOR_PRIVATE_KEY
COVERED_CALL_ALLOCATOR_POLICY_PATH
COVERED_CALL_VAULT_ADDRESS
COVERED_CALL_FLOW_MANAGER_ADDRESS
COVERED_CALL_STRATEGY_MANAGER_ADDRESS
COVERED_CALL_ADAPTER_ADDRESS
COVERED_CALL_VALUATOR_ADDRESS
COVERED_CALL_WETH_ADDRESS
```

Required redemption-processor variables:

```text
COVERED_CALL_OPERATIONS_KEEPER_ENABLED
COVERED_CALL_PROCESSOR_PRIVATE_KEY
COVERED_CALL_VAULT_ADDRESS
COVERED_CALL_FLOW_MANAGER_ADDRESS
```

The workers refuse production, non-84532 chains, stale or mismatched NAV,
policy/config drift, unresolved inventory before opening, and cap breaches.
`AwaitingPhysicalDelivery` is the sole no-NAV transition: the approved valuator
cannot publish during that transient state, so the allocator completes the
adapter-ledger transition from confirmed on-chain state and then requires a new
NAV before normalization or reopening.

For the approved staging handoff, reuse the existing distinct worker keys through
Railway references rather than copied secret values:

```text
COVERED_CALL_ALLOCATOR_PRIVATE_KEY=${{FUND_ALLOCATOR_PRIVATE_KEY}}
COVERED_CALL_PROCESSOR_PRIVATE_KEY=${{FUND_PROCESSOR_PRIVATE_KEY}}
```

The derived public addresses are:

```text
allocator  0xc217F9F7607A479cFc531319f40463EF0666dcee
processor  0xb041D93Cd93F230B8FCb00904F293e62a6a104Bf
```
