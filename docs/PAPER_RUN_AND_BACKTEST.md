# Paper run and backtest workflow

## What is possible here

The repo can run:

1. synthetic backtests;
2. snapshot backtests from collected orderbook/reward data;
3. paper replay that outputs intended quote rows from snapshots;
4. public live reward snapshots plus paper quote logging;
5. paper quote outcome analytics using the next observed snapshot.

This repo intentionally does not place live orders. The live paper loop only reads public market/reward data and writes snapshots, quote intents, and a safety manifest.

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

## Public live snapshot + paper loop

Run a 24-hour paper collection at 5-minute cadence:

```bash
python scripts/paper_replay.py \
  --live \
  --iterations 288 \
  --interval-seconds 300 \
  --snapshot-out data/raw/live_lp_snapshots.csv \
  --out data/processed/live_lp_quotes.csv \
  --manifest-out data/raw/live_lp_paper_manifest.json
```

The manifest records the exact LP and snapshot configs plus this safety invariant:

```text
paper only; no private keys, order signing, order submission, or cancellation
```

## Paper outcome analytics

After at least two snapshots, estimate paper quote outcomes:

```bash
python scripts/paper_analyze.py \
  --snapshots data/raw/live_lp_snapshots.csv \
  --quotes data/processed/live_lp_quotes.csv \
  --out-dir data/processed/live_lp_paper_analysis
```

Outputs:

```text
paper_summary.json
paper_quote_outcomes.csv
```

Fill diagnostics are midpoint-cross proxies, not executed-order proof. A would-fill is counted only when the next observed midpoint crosses through the paper bid. Pending latest quotes are separated from stale quotes.

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
pending_quote_rate
fill_proxy_rate
estimated_mark_to_next_pnl_if_all_fills_usdc
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
