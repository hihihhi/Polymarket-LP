# Current synthetic results

These are L0 synthetic stress-test results only. They are useful for comparing strategy logic, not for claiming live profitability.

## Best current profile: reward-density wide LP

Command equivalent:

```bash
python scripts/lp_backtest.py \
  --synthetic \
  --quote-size 800 \
  --quote-offset 0.035 \
  --excluded-categories "" \
  --active-capital-limit 1900 \
  --max-unpaired-per-market 800 \
  --max-total-unpaired 1200 \
  --max-cluster-unpaired 600 \
  --exit-loss-cents 0.05 \
  --max-unpaired-minutes 90
```

With reward-density ranking and volatility gates configured in `LPConfig`:

```python
rank_by_reward_density=True
max_recent_vol=0.006
max_recent_jump=0.025
vol_quote_multiplier=0.5
```

14-day seed-7 synthetic output:

| Metric | Value |
|---|---:|
| Total PnL | `$134.22` |
| 30-day equivalent | `~$287.61` |
| Return on $2k | `6.71%` |
| Total rewards | `$171.80` |
| Inventory exit PnL | `-$37.58` |
| Max MTM drawdown | `-3.10%` |
| Reward / trading loss | `4.21` |
| Max open inventory | `$330.97` |
| Avg open inventory | `$1.73` |
| Active order notional | `~$1,488` |

## Interpretation

This finally clears the minimum `$200/month` synthetic target while keeping drawdown small. It does not clear the `$500-$1,000/month` target yet.

Important weakness: pair completion is still `0%`, so the current profitable profile is mostly reward capture with wide quotes, not complete-set spread capture. The next iteration should add explicit rescue quoting and pair-priority logic.

## Multi-seed 7-day probe

Same family of profile, 7-day/18-market synthetic probe:

| Seed | Monthly equivalent | Max MTM DD | Reward/loss |
|---:|---:|---:|---:|
| 1 | `$260.14` | `-2%` | `3.0` |
| 2 | `$268.73` | `0%` | `inf` |
| 3 | `$263.19` | `0%` | `inf` |

This is not enough to declare robustness, but it is better than the previous income/turbo attempts that blew up.
