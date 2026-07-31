# B1N-413 Meta Wheel policy v1

## Decision

**Go for bounded Base Sepolia functional validation only.** The policy does not
authorize mainnet and the runtime is disabled by default. The exact JSON bytes
must match the separately configured SHA-256 and the coordinator's on-chain
policy hash before any action.

## Fixed bounds

- ETH/USDC, 48-hour cadence; expiry between 36 and 61 hours.
- Four dedicated CSP lanes and four dedicated Covered Call lanes, with one
  active option per lane.
- Parent testnet AUM capped at 20,000 USDC; each CSP lane at 5,000 USDC and each
  CC lane at 2 WETH.
- CSP targets 80% of eligible liquid USDC. The greater of pending USDC claims
  and a 20% liquid reserve is protected before any new CSP allocation.
- Each Covered Call tranche consumes exactly one immutable assignment lot in
  v1. Up to four lanes may progress concurrently; additional or incompatible
  lots remain in the on-chain transition queue and are retried without changing
  their literal floor.
- The call floor is the maximum literal CSP assignment strike of every consumed
  lot plus $10 per ETH, rounded up to the next $5 strike. Premiums never reduce
  the floor. If no fresh executable quote meets it, WETH remains idle.
- Quotes may be at most 60 seconds old and must retain 30 seconds of TTL.
  Settlement delay is bounded at six hours; seven days idle is an alert/review
  boundary, not permission to lower the protected floor.
- There is no allocator, admin, or emergency below-floor sale path. During a
  redemption or stress regime without safe USDC, new allocation pauses and the
  request waits for normal settlement/call-away liquidity.
- CSP uses the current 15% OTM / $25-tick functional rule. Covered Calls target
  5 delta within 150 bps when that target also satisfies the protected floor;
  the floor always wins.

## Fees

- Shared BatchSettler: 10% of gross premium once.
- Parent: 2% annual AUM and 10% performance fee above one HWM.
- Dedicated children: zero management and performance fees.
- The keeper requires at least 10 bps net premium after the shared premium fee.

## Evidence boundary

B1N-345 provides observed ETH price/IV inputs but modeled premiums and
liquidity. It reported zero production-ready fixed policies. The B1N-413 fee
overlay subtracts the current premium and parent fees and remains modeled. The
numeric bounds above therefore control Base Sepolia functional risk and keeper
load; they are not a claim of economic or mainnet readiness.

Standalone CSP and Covered Call policy files, workers, state transitions, and
addresses are not inputs to this policy. Wheel operations target only dedicated
child lanes registered by the Meta Wheel coordinator.
