# Polymarket-LP

Research toolkit for a managed-risk Polymarket LP/reward-farming portfolio.

This repo is for **liquidity-provider reward farming**, not directional prediction betting. The core question is:

```text
How much reward income do we earn per dollar of unpaired inventory risk?
```

## Current status

The repo includes an L0/L1-style backtest scaffold:

- synthetic stress backtest for sanity checks;
- snapshot replay backtest once point-in-time market/orderbook data is collected;
- reward-share approximation using market reward pool, max spread, min size, quote distance, and competitor score;
- paired YES/NO inventory accounting;
- mark-to-market equity and drawdown metrics;
- one-sided fill, pair completion, reward-to-loss, and inventory-risk metrics.

It does **not** place live orders.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Run a synthetic sanity-check backtest

The default run now excludes `sports,crypto` categories because the first stress test showed those clusters dominated one-sided inventory losses.

```bash
python scripts/lp_backtest.py --synthetic --out-dir data/processed/synthetic_default
```

To include everything and prove the dangerous baseline, override the filter:

```bash
python scripts/lp_backtest.py --synthetic --excluded-categories "" --out-dir data/processed/synthetic_all_markets
```

Outputs:

```text
data/processed/synthetic_default/lp_summary.csv
data/processed/synthetic_default/lp_equity_curve.csv
data/processed/synthetic_default/lp_events.csv
```

## Sweep configs

Use the sweep script to avoid trusting one arbitrary parameter set:

```bash
python scripts/sweep_lp_configs.py \
  --synthetic \
  --synthetic-days 7 \
  --synthetic-markets 18 \
  --out data/processed/lp_config_sweep.csv
```

The sweep ranks quote size, quote offset, inventory limits, exit loss, reward filters, and competition filters.

## Run with real snapshots

```bash
python scripts/lp_backtest.py \
  --snapshots data/raw/orderbook_snapshots.csv \
  --out-dir data/processed/real_replay
```

Required snapshot columns:

```text
timestamp, condition_id, reward_daily, max_incentive_spread, min_incentive_size
```

Plus either:

```text
yes_mid
```

or:

```text
yes_best_bid, yes_best_ask
```

Recommended optional columns:

```text
market_id, event_id, category, cluster,
no_mid, no_best_bid, no_best_ask,
market_competitiveness, competitor_score
```

`max_incentive_spread` should be in price units, e.g. `0.04` for ±4c.

## Backtest assumptions

The backtest is conservative and intentionally imperfect until full WebSocket quote history exists:

- filter out high-volatility categories/clusters by default, currently `sports,crypto`;
- quote both YES and NO bids at `mid - quote_offset`;
- enforce complete-set safety: `YES bid + NO bid <= 1 - safety_margin`;
- estimate reward share from an approximate quadratic order score;
- simulate toxic fills when the next midpoint moves through our bid;
- pair opposite YES/NO inventory into complete sets when possible;
- exit stale or adverse naked inventory according to risk limits;
- report both realized and mark-to-market drawdown.

## Important limitation

Historical `prices-history` is not enough for a reliable LP backtest. A serious test needs point-in-time orderbook snapshots, trades, reward config, and your own intended quotes/cancels. Treat synthetic results as stress tests only.

## Key metrics

The main acceptance metrics are:

```text
total_pnl_usdc
max_drawdown_mtm_pct
reward_to_trading_loss_ratio
pair_completion_ratio_shares
max_open_inventory_notional
avg_open_inventory_notional
reward_per_dollar_avg_inventory
pnl_per_dollar_avg_inventory
profit_factor_trading_only
daily_sortino_mtm
bad_day_p95_return
```

See `docs/METRICS_SPEC.md` for the full metric stack.
