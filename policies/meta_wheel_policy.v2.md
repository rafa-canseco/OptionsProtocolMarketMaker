# B1N-438 Meta Wheel policy v2

## Decision

**Go for bounded Base Sepolia functional validation only.** Runtime remains
disabled by default, exact JSON bytes remain externally SHA-256 pinned, and the
coordinator policy hash must match before any action. Mainnet is not authorized.

## CSP change

Dedicated ETH/USDC CSP lanes retain the 48-hour cadence, 80% utilization, 20%
parent liquid reserve, four-lane limits, and $25 strike tick. CSP quote selection
now targets absolute put delta `0.09` (`900` bps) with maximum deviation `0.015`
(`150` bps). There is no fixed-moneyness fallback. Missing, stale, or invalid
spot/IV, delta, runtime fee, quote, tick, or premium data leaves the tranche
pending and logs the rejection reason.

CSP net premium must be at least 20 bps of actual collateral after the runtime
shared protocol premium fee and before the parent performance fee. Runtime fee
values are read from `BatchSettler`, checked against market metadata and the
versioned unchanged 10% policy value, and applied with contract integer rounding.
Historical/backtest premiums referenced by this policy are modeled.

## Preserved behavior

Covered Calls remain unchanged: 5-delta target within 150 bps when compatible
with the immutable assignment floor, a 10 bps net-premium floor, $5 call tick,
and no below-floor fallback. Parent/child fees, deposits, redemptions, tranches,
settlement, assignment, and epoch behavior remain unchanged.
