# B1N-413 Meta Wheel policy evidence

This package applies the current fee semantics to the published B1N-345 ETH
Wheel rows without claiming new historical fills:

- 10% of gross option premium to the shared protocol fee;
- 2% annual parent management fee;
- 10% parent performance fee above the opening high-water mark;
- no child management or performance fee.

The terminal overlay is conservative: management is charged on the greater of
opening and terminal pre-parent-fee NAV, then performance fee is charged only
on the remaining gain. It does not model subscriptions, redemptions, or share
dilution. Those paths are state-machine and contract-test requirements.

Reproduce:

```bash
uv run python scripts/run_b1n_413_policy.py
```

The published B1N-345 premiums/liquidity remain modeled and no fixed policy is
mainnet-ready. B1N-413 authorizes only bounded Base Sepolia functional testing.
No stress assumption permits a below-floor WETH sale. A redemption that cannot
be funded from safe USDC remains pending while the allocator pauses new risk.
