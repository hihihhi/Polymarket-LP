# Resume notes

Last updated: 2026-07-07.

## Repo

```text
hihihhi/Polymarket-LP
```

This repo is a research toolkit for Polymarket LP/reward-farming. It does **not** place live orders and should not contain private keys.

## Current strategy direction

Use **Strategy V2: Reward-Density Rescue LP**.

Core idea:

```text
reward-density market selection
+ wide eligible maker quotes
+ volatility / jump filters
+ inventory rescue quoting
+ strict drawdown mode switching
```

This is not an HFT strategy and not a directional betting strategy.

## Current target

Starting capital: `$2,000`.

Target after losses:

```text
minimum: $200/month
good:    $400-$600/month
stretch: $1,000/month only in strong reward windows
```

Stable `$2,000/month` from `$2,000` is not modeled as realistic LP-only income.

## Current best synthetic profile

This is the current best **L0 synthetic** profile. It is not live proof.

```text
quote_size = 800
quote_offset = 0.035
active_capital_limit = 1900
max_unpaired_per_market = 800
max_total_unpaired = 1200
max_cluster_unpaired = 600
exit_loss_cents = 0.05
max_unpaired_minutes = 90
rank_by_reward_density = True
max_recent_vol = 0.006
max_recent_jump = 0.025
vol_quote_multiplier = 0.5
excluded_categories = ""
```

Latest 14-day synthetic result:

```text
net PnL = +$134.22
30-day equivalent = ~$287.61
return on $2k = +6.71%
max MTM drawdown = -3.10%
reward/loss = 4.21
max open inventory = $330.97
```

## Run current synthetic benchmark

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

Expected output files:

```text
data/processed/income_density_wide/lp_summary.csv
data/processed/income_density_wide/lp_equity_curve.csv
data/processed/income_density_wide/lp_events.csv
```

## Paper replay command

After real snapshots are collected:

```bash
python scripts/paper_replay.py \
  --snapshots data/raw/orderbook_snapshots.csv \
  --out data/processed/paper_quotes.csv
```

This only writes intended quotes. It does not sign, submit, cancel, or manage real orders.

## Required snapshot input columns

Minimum:

```text
timestamp, condition_id, yes_mid, reward_daily, max_incentive_spread, min_incentive_size
```

Recommended:

```text
no_mid, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask,
market_id, event_id, category, cluster,
market_competitiveness, competitor_score
```

## Key docs

```text
docs/STRATEGY_V2_REWARD_DENSITY_RESCUE_LP.md  # main strategy spec
docs/PAPER_RUN_AND_BACKTEST.md                # commands and workflow
docs/RESULTS.md                               # current synthetic result summary
docs/METRICS_SPEC.md                          # metric definitions
docs/REFINED_STRATEGY.md                      # earlier refined mandate
```

## Key scripts

```text
scripts/lp_backtest.py        # synthetic/snapshot backtest
scripts/paper_replay.py       # snapshot-to-paper-quote replay, no real orders
scripts/sweep_lp_configs.py   # synthetic/snapshot parameter sweeps
```

## Current implementation status

Implemented:

```text
basic LP backtest engine
synthetic market generator
reward score approximation
reward-density ranking
volatility gate
recent-jump gate
wide quote simulation
unpaired inventory caps
basic fixed exit slippage
mark-to-market equity curve
paper quote replay from snapshots
```

Not fully implemented:

```text
rescue quoting engine
real WebSocket collector
actual reward payout reconciliation
maker rebate EV model
order failure / cancel failure simulation
partial fills / queue position model
stale-fill simulation
cluster loss concentration report
walk-forward / out-of-sample validation
live paper daemon
```

## Next build order

1. Implement rescue quoting and pair-completion accounting.
2. Add cluster-level loss/reward/inventory reports.
3. Add real market snapshot collector using WebSocket + reward metadata.
4. Add reward payout reconciliation: expected vs actual rewards.
5. Add partial-fill, no-fill, stale-fill, cancel-latency, and queue-position models.
6. Run multi-seed + train/validation/out-of-sample sweeps.
7. Run 24-72h paper replay from real snapshots.
8. Tiny live only with `$200-$500` if paper/live metrics pass.

## Pass/fail before live

Do not go live unless real paper/live replay shows:

```text
monthly-equivalent net PnL >= $200
max MTM drawdown <= 35%
reward/loss ratio >= 2.0
profit factor >= 1.3
stale-fill rate < 2%
reward capture error < 30%
no single cluster causes >50% of losses
```

Preferred:

```text
monthly-equivalent net PnL >= $400
max MTM drawdown <= 25%
reward/loss ratio >= 3.0
profit factor >= 2.0
pair completion ratio > 40%
```

## Important caveat

The current profitable result is synthetic only. Real profitability depends on:

```text
actual reward share
competition
orderbook depth
queue position
fill toxicity
cancel latency
partial/no fills
actual reward payouts
```

The repo is ready to resume from strategy refinement into implementation and real data collection.
