# Refined LP strategy: income-first, recoverable-risk

## Mandate

Starting capital: `$2,000`.

Target net income after losses:

- minimum: `$200/month`
- good: `$400-$600/month`
- stretch: `$1,000/month`

Risk is acceptable only if drawdown does not force the system to stop, does not require adding capital, and recovery remains plausible within 1-2 months.

This is not a no-loss strategy. It is a controlled-loss LP engine.

## Core principle

Optimize this:

```text
monthly_net_pnl_after_losses / max_drawdown
```

Not this:

```text
gross_rewards
```

The LP bot should quote only when:

```text
expected_rewards + expected_pair_spread + expected_maker_rebate
>
expected_one_sided_inventory_loss + operational_risk
```

## Risk states

| State | Drawdown on $2k | Bot action |
|---|---:|---|
| Green | 0% to -10% | income mode, full size |
| Yellow | -10% to -20% | reduce order size 25-40%, no turbo |
| Orange | -20% to -30% | recovery mode, no new aggressive inventory |
| Red | -30% to -40% | no new risk, pair/exit only |
| Black | worse than -40% | shutdown; thesis failed |

Drawdown is not an instant exit. It changes the mode.

## Strategy modes

### 1. Safe mode

Purpose: preserve bankroll, collect baseline rewards, collect data.

Expected monthly on $2k: `$50-$150`.

```text
active_capital_limit = 1500-1700
quote_size = 20-40
quote_offset = 0.020-0.035
max_total_unpaired = 300-500
max_cluster_unpaired = 150-250
exit_loss_cents = 0.015-0.025
max_unpaired_minutes = 10-30
min_reward_daily = 50-100
```

### 2. Income mode

Purpose: target the minimum mandate, `$200-$500/month`.

```text
active_capital_limit = 1800-1950
quote_size = 50-100
quote_offset = 0.010-0.018
max_unpaired_per_market = 80-140
max_total_unpaired = 650-900
max_cluster_unpaired = 300-450
exit_loss_cents = 0.025-0.045
max_unpaired_minutes = 30-60
min_reward_daily = 100-200
max_market_competitiveness = 0.60
```

### 3. Turbo mode

Purpose: attack `$500-$1,000/month` windows.

Use only when reward pools are fat, competition is low, and live/paper fill quality is healthy.

```text
active_capital_limit = 1900-2000
quote_size = 75-150
quote_offset = 0.006-0.012
max_unpaired_per_market = 120-200
max_total_unpaired = 900-1200
max_cluster_unpaired = 450-650
exit_loss_cents = 0.035-0.060
max_unpaired_minutes = 45-90
min_reward_daily = 200+
max_market_competitiveness = 0.45
```

Turbo mode must disable automatically in Yellow drawdown.

### 4. Recovery mode

Purpose: recover after drawdown without panic-selling.

```text
no new turbo quotes
no new high-vol clusters
quote only to pair existing inventory
safe reward farming only
max_new_unpaired = 10-20% of bankroll
```

## Market gates

Quote only if all hard gates pass:

```text
reward_daily >= min_reward_daily
quote_size >= min_incentive_size
quote_offset <= max_incentive_spread
YES_bid + NO_bid <= 1 - safety_margin
category/cluster not banned by current risk state
```

Then apply soft gates:

```text
recent_mid_volatility <= threshold
jump_risk <= threshold
reward_density >= threshold
competition <= threshold
cluster_exposure_remaining > planned_quote_notional
```

## Reward-density gate

Do not quote merely because a market has a reward pool. Quote because the pool is good relative to capital and risk.

```text
reward_density = expected_reward_per_day / active_order_notional
```

Income mode should prefer markets where:

```text
expected_reward_per_day / active_order_notional >= 1.0%-2.5% per day
```

Turbo mode should prefer:

```text
>= 2.5%-5.0% per day
```

## Adaptive quote width

Static quoting is too dumb. Quote offset should widen when midpoint is moving.

```text
quote_offset = base_offset + volatility_multiplier * recent_mid_volatility
```

Rules:

```text
if recent jump >= 2.5c: skip for 5-15 minutes
if repeated jumps: disable market
if near resolution/catalyst: reduce or disable
```

## Inventory rescue quoting

This is the main upgrade needed after the synthetic tests.

If long YES:

```text
stop normal YES bids
improve NO bid up to max safe pair cost
reduce other risk in the same market
```

If long NO:

```text
stop normal NO bids
improve YES bid up to max safe pair cost
reduce other risk in the same market
```

Safe pair price:

```text
max_opposite_bid = 1 - held_side_price - target_pair_profit
```

Example:

```text
held YES = 0.47
target_pair_profit = 0.01
max NO rescue bid = 0.52
```

If rescue quote does not fill within max time:

```text
exit if mark loss >= exit_loss_cents
else keep only while within inventory cap and market remains stable
```

## Category policy

Default safe mode excludes high-vol categories like `sports,crypto`.

Income mode allows them only if:

```text
not live
not near resolution
low recent midpoint volatility
reward_daily is high
reward_density passes
cluster exposure is available
```

Turbo mode can include them only with smaller cluster caps and faster kill switches.

## Portfolio caps

For $2k bankroll:

```text
single_market_loss_cap = $100-$160
single_cluster_loss_cap = $300-$450
max_total_unpaired = $700-$1,200 depending on mode
hard_account_stop = -$800
```

Cluster examples:

```text
same sports game
same election/person
same war/geopolitical event
same crypto asset/time bucket
same weather event
```

## Acceptance test

A config is usable only if it passes:

```text
monthly_equivalent_net_pnl >= $200
max_mtm_drawdown <= 35%
reward_to_trading_loss_ratio >= 2.0
profit_factor_trading_only >= 1.3
pair_completion_ratio improving over baseline
no cluster causes >50% of total losses
recovery_days_from_mdd <= 45
```

A config is preferred if:

```text
monthly_equivalent_net_pnl >= $400
max_mtm_drawdown <= 25%
reward_to_trading_loss_ratio >= 3.0
profit_factor_trading_only >= 2.0
```

## Backtest protocol

Never choose the highest-PnL config directly.

Use this order:

1. run safe, income, turbo sweeps;
2. discard configs with MDD beyond threshold;
3. discard configs with reward/loss ratio below threshold;
4. discard configs with cluster concentration failure;
5. rank remaining configs by monthly net PnL and recovery factor;
6. validate on a later unseen period;
7. paper trade live;
8. go tiny live with $200-$500 only.

## Current conclusion

The first synthetic tests proved that safe/adaptive LP is too conservative for the income mandate. The refined strategy must use income/turbo mode, but it must earn its right to take risk through reward-density, volatility, cluster, and inventory-rescue controls.
