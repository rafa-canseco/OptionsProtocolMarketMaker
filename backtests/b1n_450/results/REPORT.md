# B1N-450 — ETH CSP partial-utilization and weekly-ladder comparison

> Research only. This package does not deploy, activate, or change live ETH policy/config.

Gate-derived recommendation: **KEEP** the current B1N-438 live policy.
Content-integrity attestation (not signer authentication): `59358acae0e4c4562a79a5b79b44754deddbb5a5a1d07cc8ac4be90d66177ccf`.

Observed Deribit ETH perpetual closes and DVOL are causal inputs. Every option premium is a modeled Binary bid; observed executable premium evidence is zero for every arm.

## Frozen arms

- `current_48h`: B1N-438 current policy: one 48h 0.09-delta put, 20 bps post-protocol-fee premium floor, 80% utilization/20% reserve and $25 ticks.
- `current_48h_staggered_4`: Current delta/floor/utilization divided into four bounded 20%-of-current-idle-USDC 48h lanes staggered every 12h.
- `weekly_4_low_30pct`: Four seven-day put lanes staggered every 42h at 30% aggregate utilization (inside the predeclared 25-35% band).
- `weekly_4_high_60pct`: Four seven-day put lanes staggered every 42h at a higher bounded 60% aggregate risk budget/40% reserve.
- `atm_48h_control`: One 48h approximately 50-delta ATM CSP at current utilization, used only as a high-assignment stress/control.
- `hybrid_current_csp_weekly_cc`: Current conservative CSP remains unchanged; assigned ETH is divided across four staggered seven-day 0.05-delta covered-call lanes with a strict literal assignment-strike floor.

All arms share the pinned market snapshot, $100,000 opening NAV, 5% risk-free rate, current fee semantics, causal observation-age gates, and base/stressed cost definitions. Development and validation periods are disjoint; windows are non-overlapping within each split.

Assignment frequency is per settled put; assignments/30d is the time-normalized cross-tenor intensity. Drawdown and daily NAV CVaR marks include accrued management fees and hypothetical HWM performance fees at each mark. With four 90-day validation windows, CVaR10 of period return equals the single worst window.

## Validation — base costs

| Policy | Window | Median return | Premium yield | Assign/settled put | Assignments/30d | Worst DD after fees | Return CVaR10 | ETH time | Idle capital | Redemption liquidity p5 | Turnover | Complete Wheel cycles | Ops/30d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| atm_48h_control | 30d | 0.41% | 2.17% | 44.6% | 6.25 | -45.19% | -32.97% | 87.4% | 3.3% | 0.0% | $188,126 | 0 | 28.0 |
| atm_48h_control | 60d | -14.54% | 2.86% | 44.8% | 6.50 | -45.51% | -36.52% | 92.3% | 1.9% | 0.0% | $218,287 | 0 | 29.0 |
| atm_48h_control | 90d | -22.26% | 2.78% | 45.5% | 6.67 | -45.51% | -30.66% | 95.0% | 1.3% | 0.0% | $222,282 | 0 | 29.3 |
| current_48h | 30d | 0.86% | 0.53% | 10.8% | 0.67 | -19.02% | -4.99% | 34.0% | 51.7% | 0.8% | $312,732 | 0 | 12.3 |
| current_48h | 60d | 6.34% | 1.22% | 10.1% | 0.67 | -19.02% | -7.98% | 46.7% | 41.0% | 0.7% | $583,037 | 0 | 13.2 |
| current_48h | 90d | 8.00% | 1.03% | 10.4% | 0.67 | -30.03% | -13.63% | 66.2% | 29.1% | 0.2% | $530,033 | 0 | 12.8 |
| current_48h_staggered_4 | 30d | 1.84% | 0.91% | 7.5% | 1.92 | -9.33% | -2.82% | 51.8% | 56.0% | 3.4% | $393,490 | 0 | 51.2 |
| current_48h_staggered_4 | 60d | 2.80% | 1.70% | 8.0% | 2.17 | -13.64% | -2.70% | 69.1% | 43.8% | 5.0% | $696,270 | 0 | 53.8 |
| current_48h_staggered_4 | 90d | 2.37% | 1.88% | 8.4% | 2.25 | -19.96% | -6.71% | 72.0% | 38.9% | 2.2% | $806,411 | 0 | 53.8 |
| hybrid_current_csp_weekly_cc | 30d | 0.86% | 0.73% | 10.8% | 0.67 | -18.64% | -4.78% | 34.0% | 52.1% | 0.8% | $412,663 | 0 | 20.5 |
| hybrid_current_csp_weekly_cc | 60d | 6.89% | 1.94% | 10.1% | 0.67 | -18.64% | -7.57% | 46.7% | 42.5% | 0.9% | $951,773 | 0 | 28.8 |
| hybrid_current_csp_weekly_cc | 90d | 7.67% | 2.02% | 10.4% | 0.67 | -29.44% | -12.66% | 66.2% | 31.6% | 0.2% | $1,260,208 | 0 | 36.2 |
| weekly_4_high_60pct | 30d | 0.65% | 0.86% | 12.5% | 1.75 | -26.97% | -14.53% | 27.2% | 46.3% | 14.0% | $200,873 | 0 | 28.0 |
| weekly_4_high_60pct | 60d | -1.00% | 1.53% | 10.2% | 1.58 | -27.39% | -18.81% | 55.0% | 34.1% | 2.3% | $359,145 | 0 | 31.0 |
| weekly_4_high_60pct | 90d | -1.75% | 1.95% | 12.0% | 1.92 | -27.39% | -12.88% | 60.7% | 28.6% | 5.7% | $493,465 | 0 | 32.0 |
| weekly_4_low_30pct | 30d | 0.25% | 0.43% | 12.5% | 1.75 | -15.33% | -8.02% | 27.2% | 72.6% | 52.8% | $102,698 | 0 | 28.0 |
| weekly_4_low_30pct | 60d | -0.59% | 0.87% | 10.2% | 1.58 | -15.68% | -10.39% | 55.0% | 63.8% | 41.3% | $204,059 | 0 | 31.0 |
| weekly_4_low_30pct | 90d | -0.46% | 1.19% | 12.0% | 1.92 | -15.68% | -6.84% | 60.7% | 59.5% | 40.0% | $297,980 | 0 | 32.0 |

## Validation — stressed modeled costs

| Policy | Window | Median return | Premium yield | Assign/settled put | Assignments/30d | Worst DD after fees | Return CVaR10 | Redemption liquidity p5 | Ops/30d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| atm_48h_control | 30d | 0.39% | 2.14% | 44.6% | 6.25 | -45.20% | -32.99% | 0.0% | 28.0 |
| atm_48h_control | 60d | -14.56% | 2.83% | 44.8% | 6.50 | -45.52% | -36.54% | 0.0% | 29.0 |
| atm_48h_control | 90d | -22.28% | 2.75% | 45.5% | 6.67 | -45.52% | -30.68% | 0.0% | 29.3 |
| current_48h | 30d | 0.78% | 0.47% | 10.4% | 0.58 | -19.03% | -5.02% | 0.7% | 11.2 |
| current_48h | 60d | 5.34% | 1.17% | 9.7% | 0.58 | -19.03% | -8.06% | 0.8% | 12.0 |
| current_48h | 90d | 0.93% | 0.87% | 10.0% | 0.58 | -30.07% | -13.72% | 0.2% | 11.7 |
| current_48h_staggered_4 | 30d | 1.22% | 0.83% | 7.6% | 1.83 | -9.34% | -2.91% | 3.4% | 48.2 |
| current_48h_staggered_4 | 60d | 2.76% | 1.55% | 8.3% | 2.08 | -13.64% | -2.96% | 5.0% | 50.5 |
| current_48h_staggered_4 | 90d | 0.68% | 1.76% | 8.6% | 2.17 | -20.00% | -6.86% | 2.2% | 50.7 |
| hybrid_current_csp_weekly_cc | 30d | 0.78% | 0.63% | 10.4% | 0.58 | -18.65% | -4.82% | 0.8% | 18.8 |
| hybrid_current_csp_weekly_cc | 60d | 5.67% | 1.86% | 9.7% | 0.58 | -18.65% | -7.65% | 0.9% | 24.8 |
| hybrid_current_csp_weekly_cc | 90d | 1.54% | 1.89% | 10.0% | 0.58 | -29.49% | -12.76% | 0.2% | 29.3 |
| weekly_4_high_60pct | 30d | 0.64% | 0.85% | 12.5% | 1.75 | -26.97% | -14.54% | 14.0% | 28.0 |
| weekly_4_high_60pct | 60d | -1.01% | 1.51% | 10.2% | 1.58 | -27.39% | -18.82% | 2.3% | 31.0 |
| weekly_4_high_60pct | 90d | -1.77% | 1.93% | 12.0% | 1.92 | -27.39% | -12.89% | 5.7% | 32.0 |
| weekly_4_low_30pct | 30d | 0.25% | 0.42% | 12.5% | 1.75 | -15.33% | -8.03% | 52.8% | 28.0 |
| weekly_4_low_30pct | 60d | -0.60% | 0.86% | 10.2% | 1.58 | -15.69% | -10.40% | 41.3% | 31.0 |
| weekly_4_low_30pct | 90d | -0.47% | 1.17% | 12.0% | 1.92 | -15.69% | -6.85% | 40.0% | 32.0 |

## ATM hypothesis

The ATM control targets absolute put delta 0.50 (approximately 50-delta under the model). It is a deliberately high-assignment control, not a candidate. No CSP rule in this package is attributed to BlackRock.
 Median opened absolute put delta across raw windows: `0.4532`.

Materially riskier on the predeclared assignment-frequency and drawdown checks: `True`.

## Causal and regime evidence

Rows use only the latest spot/DVOL observations available at each decision. Market regimes are classified independently as bull, bear, sideways and volatility-crash overlays. Raw normalized rows retain regime labels, causal-coverage failures, 30/60/90-day metrics, base/stressed costs, and operational counts.

## Recommendation and limitations

- No observed executable Binary premium or fill evidence exists for any arm.
- The 30% weekly ladder improved 90-day worst drawdown and redemption liquidity but had -0.46% median return versus 8.00% for current.
- The staggered 48h arm improved 90-day worst drawdown but reduced median return to 2.37% and raised modeled operational load to 53.8 actions/30d versus 12.8.
- The hybrid completed 0 full assignment-lot Wheel cycles and did not dominate current 90-day return (7.67% versus 8.00%); it also increased turnover/operations.
- ATM is a high-assignment stress/control, not a candidate and not a rule attributed to BlackRock.

- Limitation: Premiums and capacity are modeled rather than executable observations.
- Limitation: The redemption metric is an unencumbered-USDC proxy without subscriptions or queued redemptions.
- Limitation: Non-overlapping samples are limited: four 90-day validation windows make CVaR10 equal the single worst observation.
- Limitation: Historical Deribit perpetual/DVOL inputs may not represent Base execution or future regimes.

A future ladder or hybrid change must remain disabled, require a new implementation ticket, and pass independent pricing review plus executable-premium evidence gates. This ticket authorizes no runtime action.

## Policy descriptions

- `current_48h` — B1N-438 current policy: one 48h 0.09-delta put, 20 bps post-protocol-fee premium floor, 80% utilization/20% reserve and $25 ticks.
- `current_48h_staggered_4` — Current delta/floor/utilization divided into four bounded 20%-of-current-idle-USDC 48h lanes staggered every 12h.
- `weekly_4_low_30pct` — Four seven-day put lanes staggered every 42h at 30% aggregate utilization (inside the predeclared 25-35% band).
- `weekly_4_high_60pct` — Four seven-day put lanes staggered every 42h at a higher bounded 60% aggregate risk budget/40% reserve.
- `atm_48h_control` — One 48h approximately 50-delta ATM CSP at current utilization, used only as a high-assignment stress/control.
- `hybrid_current_csp_weekly_cc` — Current conservative CSP remains unchanged; assigned ETH is divided across four staggered seven-day 0.05-delta covered-call lanes with a strict literal assignment-strike floor.
