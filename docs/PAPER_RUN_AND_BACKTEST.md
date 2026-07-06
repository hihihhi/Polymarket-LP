# Paper run and backtest workflow

## What is possible here

The repo can run:

1. synthetic backtests;
2. snapshot backtests from collected orderbook/reward data;
3. paper replay that outputs intended quote rows from snapshots.

A live paper bot must run on your machine/server because it has to keep a WebSocket connection open and log data over time. This repo intentionally does not place live orders.

## Backtest command

Current income-mode synthetic benchmark:

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
  --max-unpaired-minutes 90 \
  --max-recent-vol 0.006 \
  --max-recent-jump 0.025 \
  --vol-quote-multiplier 0.5 \
  --out-dir data/processed/income_density_wide
```

## Snapshot paper replay

After collecting point-in-time snapshots, run:

```bash
python scripts/paper_replay.py \
  --snapshots data/raw/orderbook_snapshots.csv \
  --out data/processed/paper_quotes.csv
```

This writes intended quotes only. It does not sign orders, send orders, or touch private keys.

## Required snapshot columns

```text
timestamp, condition_id, yes_mid, reward_daily, max_incentive_spread, min_incentive_size
```

Optional but strongly recommended:

```text
no_mid, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask,
market_id, event_id, category, cluster,
market_competitiveness, competitor_score
```

## Paper-run metrics to compare

The paper quotes must be joined later against trades/orderbook changes and actual reward payouts.

Minimum metrics:

```text
paper_quotes_count
eligible_quote_intervals
active_order_notional
expected_reward_per_day
reward_density_per_day
would_fill_yes
would_fill_no
would_pair_complete
would_open_inventory
would_exit_inventory
stale_fill_rate
reward_capture_error
cluster_loss_concentration
```

## Pass/fail

Do not go live unless paper/live replay shows:

```text
monthly-equivalent net PnL >= $200
max MTM drawdown <= 35%
reward/loss ratio >= 2.0
stale-fill rate < 2%
reward capture error < 30%
no single cluster > 50% of losses
```
