# B1N-438 CSP Fund Policy v4

## Decision

**Go for bounded Base Sepolia ETH/USDC functional validation only.** This policy
is not a mainnet authorization.

## Active selection

- Sell 48-hour cash-secured puts using an absolute put delta target of `0.09`
  (`900` bps) and reject quotes more than `0.015` (`150` bps) from target.
- Eligible strikes must be exact $25 ticks. Strike distance is an observed result
  of delta selection, not a fixed-moneyness input or fallback.
- Quotes and their causal spot/IV snapshot may be at most 60 seconds old. Missing,
  stale, or invalid spot, IV, delta, fee, expiry, tick, or premium data fails closed.
- Target 80% of current idle USDC while preserving a 20% liquid reserve. Keep one
  active position and the existing 36-to-60-hour admission bounds around the
  48-hour target.

## Premium floor and fees

The quote must leave at least 20 bps of actual CSP collateral after the runtime
`BatchSettler.protocolFeeBps` fee and before any performance fee. The allocator
validates backend and on-chain fee equality and mirrors contract integer math:
`net = gross - floor(gross * protocolFeeBps / 10_000)`, followed by
`net * 10_000 >= collateral * 20`. This policy does not change the protocol fee.

Premiums in historical backtest artifacts are modeled unless their artifact says
otherwise; this functional policy does not relabel them as observed liquidity.
Covered Call, deposit, redemption, settlement, assignment, and epoch behavior are
outside this policy change and remain unchanged.
