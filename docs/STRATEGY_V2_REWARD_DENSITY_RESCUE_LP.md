# Strategy V2: Reward-Density Rescue LP

## Mission

Starting bankroll: `$2,000`.

Required outcome after losses:

- minimum: `$200/month`
- good: `$400-$600/month`
- stretch: `$1,000/month` only in unusually strong reward windows

This strategy is not a high-frequency speed race. It is a reward-density and inventory-control strategy that only quotes when the reward pool appears mispriced relative to unpaired inventory risk.

## Core thesis

Most naive LP fails because it does this:

```text
quote many markets
collect rewards
get one-sided filled
bleed more than rewards
```

V2 does this instead:

```text
rank markets by reward density
quote only top opportunities
use wide-enough quotes to reduce toxic fills
rescue one-sided inventory by completing opposite-side pairs
switch modes based on drawdown
```

## What we are trying to monetize

1. Liquidity rewards from resting eligible orders.
2. Complete-set spread when YES and NO are bought for less than `$1` combined.
3. Maker rebates where fills are not toxic.
4. Occasional under-farmed high-reward windows.

## What we are not trying to do

```text
beat HFT on live sports
beat HFT on 5-minute crypto
quote the tightest book everywhere
market-buy directionally
martingale inventory
```

## Market universe

### Preferred

```text
politics, culture, weather, economics, business, slow-moving general markets
```

### Conditional

```text
sports, crypto, live markets
```

Conditional markets are allowed only when all are true:

```text
not close to resolution
not in a live/high-volatility phase
recent midpoint volatility is low
recent jump filter passes
reward density is high enough
cluster exposure remains available
```

### Avoid

```text
ambiguous resolution rules
near-deadline markets
breaking-news markets
markets with one obvious informed side
markets where reward share is already crowded
```

## Strategy layers

### Layer 1: Reward-density scanner

For every reward market, calculate:

```text
expected_reward_per_day = reward_daily * expected_score_share
reward_density = expected_reward_per_day / active_order_notional
```

Only quote if:

```text
reward_density >= mode_threshold
```

Suggested thresholds:

```text
safe:   >= 0.5%-1.0% per day
income: >= 1.0%-2.5% per day
turbo:  >= 2.5%-5.0% per day
```

### Layer 2: Volatility and jump gate

Before quoting, compute recent midpoint movement.

Quote only if:

```text
recent_vol <= max_recent_vol
recent_jump <= max_recent_jump
```

Default synthetic-winning family:

```text
max_recent_vol = 0.006
max_recent_jump = 0.025
vol_quote_multiplier = 0.5
```

### Layer 3: Wide reward quote

Base quote:

```text
YES_bid = YES_mid - quote_offset
NO_bid  = NO_mid  - quote_offset
```

Complete-set safety:

```text
YES_bid + NO_bid <= 1 - safety_margin
```

Synthetic-winning family:

```text
quote_size = 800 shares
quote_offset = 0.035
active_capital_limit = $1,900
safety_margin = 0.015
```

This looks big, but because quotes are wide, the goal is mostly reward score, not getting filled constantly.

### Layer 4: Rescue quoting

This is the main V2 upgrade still to implement.

If one side fills, stop normal quoting on that side and prioritize completing the pair.

If long YES:

```text
stop normal YES bids
place/improve NO rescue bid
max_NO_rescue_bid = 1 - YES_entry_price - target_pair_profit
```

If long NO:

```text
stop normal NO bids
place/improve YES rescue bid
max_YES_rescue_bid = 1 - NO_entry_price - target_pair_profit
```

Example:

```text
YES filled at 0.47
target_pair_profit = 0.01
max NO rescue bid = 0.52
```

If NO fills at or below `0.52`, pair cost is at most `0.99`, so the completed set is neutral and profitable before rewards/rebates.

### Layer 5: Inventory exit

If rescue does not work, exit only by rules.

Exit one-sided inventory if any are true:

```text
mark-to-market loss >= exit_loss_cents
unpaired age >= max_unpaired_minutes
market enters high-volatility state
cluster cap is hit
account enters recovery mode
```

Do not panic-exit merely because there is drawdown.

## Modes

### Safe mode

Purpose: preserve capital and collect data.

```text
monthly target: $50-$150
max planned MDD: 10%-15%
quote_size: 50-200
quote_offset: 0.025-0.045
max_total_unpaired: $300-$500
min reward density: 0.5%-1.0%/day
```

### Income mode

Purpose: hit `$200-$500/month`.

```text
monthly target: $200-$500
max planned MDD: 25%-35%
quote_size: 400-900
quote_offset: 0.025-0.040
active_capital_limit: $1,700-$1,950
max_total_unpaired: $800-$1,200
max_cluster_unpaired: $400-$700
exit_loss_cents: 0.035-0.060
max_unpaired_minutes: 60-120
min reward density: 1.0%-2.5%/day
```

### Turbo mode

Purpose: attack `$500-$1,000/month` windows.

Only activate when live data shows:

```text
reward density is high
competition is low
recent fill toxicity is low
rescue pair completion is working
cluster exposure is clean
```

```text
monthly target: $500-$1,000
max planned MDD: 35%-45%
quote_size: 900-1500
quote_offset: 0.020-0.035
active_capital_limit: $1,900-$2,000
max_total_unpaired: $1,200-$1,500
hard kill: -40% account drawdown
```

Turbo disables immediately in Yellow drawdown.

### Recovery mode

Trigger:

```text
account drawdown >= 20%
```

Actions:

```text
no turbo
no new high-vol clusters
new quotes only in top reward-density safe markets
quote mainly to rescue/complete pairs
size down 50%-70%
```

## Risk states

| State | Drawdown | Action |
|---|---:|---|
| Green | 0% to -10% | income mode full size |
| Yellow | -10% to -20% | reduce size, no turbo |
| Orange | -20% to -30% | recovery mode |
| Red | -30% to -40% | pair/exit only, no new risk |
| Black | worse than -40% | shutdown; thesis failed |

Drawdown changes mode. It is not an automatic liquidation trigger.

## Portfolio caps for $2k

```text
single market loss cap: $100-$160
single cluster loss cap: $300-$450
max total unpaired: $800-$1,200 in income mode
hard account stop: -$800
```

Cluster examples:

```text
same game
same election/person
same geopolitical event
same crypto asset/time window
same weather event
```

## Live pass/fail criteria

A config is usable only if:

```text
monthly-equivalent net PnL >= $200
max MTM drawdown <= 35%
reward/loss ratio >= 2.0
profit factor >= 1.3
no cluster causes >50% of losses
stale-fill rate < 2%
reward capture error < 30%
```

Preferred:

```text
monthly-equivalent net PnL >= $400
max MTM drawdown <= 25%
reward/loss ratio >= 3.0
profit factor >= 2.0
pair completion ratio > 40%
```

## Current synthetic best profile

The current synthetic winner is the income-mode family:

```text
quote_size = 800
quote_offset = 0.035
active_capital_limit = 1900
max_total_unpaired = 1200
max_cluster_unpaired = 600
exit_loss_cents = 0.05
max_unpaired_minutes = 90
rank_by_reward_density = True
max_recent_vol = 0.006
max_recent_jump = 0.025
vol_quote_multiplier = 0.5
```

14-day synthetic seed-7 result:

```text
net PnL = +$134.22
30-day equivalent = ~$287.61
max MTM drawdown = -3.10%
reward/loss = 4.21
max open inventory = $330.97
```

This passes the minimum synthetic target but does not prove live profitability.

## Known gaps

1. Rescue quoting not implemented in the engine yet.
2. Pair completion is still weak in current synthetic runs.
3. Reward share model is approximate.
4. Maker rebate EV is not included robustly.
5. Real orderbook/reward/trade logging is required.
6. Capacity of 800-share quotes must be tested live/paper.
7. Cluster-loss reporting needs to be hardened.
8. Walk-forward and out-of-sample validation are still required.

## Build order

1. Implement rescue quoting and pair-completion accounting.
2. Add reward payout reconciliation.
3. Add maker rebate EV model.
4. Add WebSocket collector and paper quote logs.
5. Add cluster-loss reports.
6. Run train/validation/out-of-sample sweeps.
7. Paper trade for 1-2 weeks.
8. Tiny live with $200-$500.
9. Scale toward $2k only if live reward/loss and stale-fill metrics pass.

## Final positioning

This strategy is realistic only as:

```text
reward-density LP + wide quotes + rescue inventory + strict mode switching
```

It is not realistic as:

```text
retail HFT trying to out-cancel professional makers
```
