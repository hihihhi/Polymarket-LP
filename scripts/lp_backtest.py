#!/usr/bin/env python3
"""Run Polymarket LP portfolio backtests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lp_backtest import (
    LPConfig,
    load_snapshots,
    make_synthetic_snapshots,
    run_backtest_to_files,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--snapshots", help="CSV of point-in-time market snapshots/orderbook mids"
    )
    p.add_argument(
        "--synthetic", action="store_true", help="Run L0 synthetic sanity-check data"
    )
    p.add_argument("--out-dir", default="lp_backtest_out")
    p.add_argument("--initial-capital", type=float, default=2000)
    p.add_argument("--quote-size", type=float, default=25)
    p.add_argument("--quote-offset", type=float, default=0.020)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--max-unpaired-per-market", type=float, default=60)
    p.add_argument("--max-total-unpaired", type=float, default=450)
    p.add_argument("--max-cluster-unpaired", type=float, default=250)
    p.add_argument("--disable-rescue-quotes", action="store_true")
    p.add_argument("--rescue-min-pair-edge-per-share", type=float, default=0.0)
    p.add_argument("--rescue-quote-offset", type=float, default=0.005)
    p.add_argument("--exit-loss-cents", type=float, default=0.025)
    p.add_argument("--max-unpaired-minutes", type=float, default=30)
    p.add_argument("--assumed-competitor-score", type=float, default=2000)
    p.add_argument("--active-capital-limit", type=float, default=1700)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--synthetic-days", type=int, default=14)
    p.add_argument("--synthetic-markets", type=int, default=30)
    p.add_argument("--min-reward-daily", type=float, default=0.0)
    p.add_argument("--max-market-competitiveness", type=float, default=1.0)
    p.add_argument("--allowed-categories", default="")
    p.add_argument("--excluded-categories", default="sports,crypto")
    p.add_argument("--min-reward-density-per-day", type=float, default=0.0)
    p.add_argument("--recent-vol-window", type=int, default=6)
    p.add_argument("--max-recent-vol", type=float, default=1.0)
    p.add_argument("--max-recent-jump", type=float, default=1.0)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.0)
    p.add_argument("--no-rank-by-reward-density", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = LPConfig(
        initial_capital=args.initial_capital,
        quote_size_shares=args.quote_size,
        quote_offset=args.quote_offset,
        safety_margin=args.safety_margin,
        max_unpaired_per_market=args.max_unpaired_per_market,
        max_total_unpaired=args.max_total_unpaired,
        max_cluster_unpaired=args.max_cluster_unpaired,
        enable_rescue_quotes=not args.disable_rescue_quotes,
        rescue_min_pair_edge_per_share=args.rescue_min_pair_edge_per_share,
        rescue_quote_offset=args.rescue_quote_offset,
        exit_loss_cents=args.exit_loss_cents,
        max_unpaired_minutes=args.max_unpaired_minutes,
        assumed_competitor_score=args.assumed_competitor_score,
        active_capital_limit=args.active_capital_limit,
        min_reward_daily=args.min_reward_daily,
        max_market_competitiveness=args.max_market_competitiveness,
        allowed_categories=args.allowed_categories,
        excluded_categories=args.excluded_categories,
        rank_by_reward_density=not args.no_rank_by_reward_density,
        min_reward_density_per_day=args.min_reward_density_per_day,
        recent_vol_window=args.recent_vol_window,
        max_recent_vol=args.max_recent_vol,
        max_recent_jump=args.max_recent_jump,
        vol_quote_multiplier=args.vol_quote_multiplier,
    )
    if args.synthetic:
        snapshots = make_synthetic_snapshots(
            seed=args.seed, days=args.synthetic_days, n_markets=args.synthetic_markets
        )
    elif args.snapshots:
        snapshots = load_snapshots(args.snapshots)
    else:
        raise SystemExit("Provide --snapshots path or use --synthetic")

    summary = run_backtest_to_files(snapshots, cfg, Path(args.out_dir))
    print(summary.T.to_string(header=False))
    print(
        f"\nWrote: {Path(args.out_dir) / 'lp_summary.csv'}, {Path(args.out_dir) / 'lp_equity_curve.csv'}, {Path(args.out_dir) / 'lp_events.csv'}"
    )


if __name__ == "__main__":
    main()
