#!/usr/bin/env python3
"""Run Polymarket LP portfolio backtests."""
from __future__ import annotations

import argparse
from pathlib import Path

from polymarket_lp.lp_backtest import LPConfig, load_snapshots, make_synthetic_snapshots, run_backtest_to_files


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", help="CSV of point-in-time market snapshots/orderbook mids")
    p.add_argument("--synthetic", action="store_true", help="Run L0 synthetic sanity-check data")
    p.add_argument("--out-dir", default="lp_backtest_out")
    p.add_argument("--initial-capital", type=float, default=2000)
    p.add_argument("--quote-size", type=float, default=25)
    p.add_argument("--quote-offset", type=float, default=0.020)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--max-unpaired-per-market", type=float, default=60)
    p.add_argument("--max-total-unpaired", type=float, default=450)
    p.add_argument("--max-cluster-unpaired", type=float, default=250)
    p.add_argument("--exit-loss-cents", type=float, default=0.025)
    p.add_argument("--max-unpaired-minutes", type=float, default=30)
    p.add_argument("--assumed-competitor-score", type=float, default=2000)
    p.add_argument("--active-capital-limit", type=float, default=1700)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--synthetic-days", type=int, default=14)
    p.add_argument("--synthetic-markets", type=int, default=30)
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
        exit_loss_cents=args.exit_loss_cents,
        max_unpaired_minutes=args.max_unpaired_minutes,
        assumed_competitor_score=args.assumed_competitor_score,
        active_capital_limit=args.active_capital_limit,
    )
    if args.synthetic:
        snapshots = make_synthetic_snapshots(seed=args.seed, days=args.synthetic_days, n_markets=args.synthetic_markets)
    elif args.snapshots:
        snapshots = load_snapshots(args.snapshots)
    else:
        raise SystemExit("Provide --snapshots path or use --synthetic")

    summary = run_backtest_to_files(snapshots, cfg, Path(args.out_dir))
    print(summary.T.to_string(header=False))
    print(f"\nWrote: {Path(args.out_dir) / 'lp_summary.csv'}, {Path(args.out_dir) / 'lp_equity_curve.csv'}, {Path(args.out_dir) / 'lp_events.csv'}")


if __name__ == "__main__":
    main()
