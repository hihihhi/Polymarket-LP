#!/usr/bin/env python3
"""Sweep LP strategy configs on snapshot or synthetic data."""
from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from polymarket_lp.lp_backtest import LPConfig, load_snapshots, make_synthetic_snapshots, simulate_lp


def parse_csv_floats(value: str) -> list[float]:
    return [float(x.strip()) for x in value.split(",") if x.strip()]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots")
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--out", default="lp_config_sweep.csv")
    p.add_argument("--initial-capital", type=float, default=2000)
    p.add_argument("--quote-sizes", default="10,25,40")
    p.add_argument("--quote-offsets", default="0.015,0.02,0.025,0.03")
    p.add_argument("--max-unpaired-per-market", default="20,40,60")
    p.add_argument("--exit-loss-cents", default="0.015,0.025")
    p.add_argument("--max-unpaired-minutes", default="10,30")
    p.add_argument("--min-rewards", default="0,100,200")
    p.add_argument("--max-competitiveness", default="1.0,0.5,0.3")
    p.add_argument("--excluded-categories", default="sports,crypto")
    p.add_argument("--allowed-categories", default="")
    p.add_argument("--synthetic-days", type=int, default=7)
    p.add_argument("--synthetic-markets", type=int, default=18)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--top", type=int, default=20)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.synthetic:
        snapshots = make_synthetic_snapshots(seed=args.seed, days=args.synthetic_days, n_markets=args.synthetic_markets)
    elif args.snapshots:
        snapshots = load_snapshots(args.snapshots)
    else:
        raise SystemExit("Provide --snapshots or --synthetic")

    rows = []
    for quote_size, quote_offset, max_unpaired, exit_loss, max_minutes, min_reward, max_comp in product(
        parse_csv_floats(args.quote_sizes),
        parse_csv_floats(args.quote_offsets),
        parse_csv_floats(args.max_unpaired_per_market),
        parse_csv_floats(args.exit_loss_cents),
        parse_csv_floats(args.max_unpaired_minutes),
        parse_csv_floats(args.min_rewards),
        parse_csv_floats(args.max_competitiveness),
    ):
        cfg = LPConfig(
            initial_capital=args.initial_capital,
            quote_size_shares=quote_size,
            quote_offset=quote_offset,
            max_unpaired_per_market=max_unpaired,
            max_total_unpaired=min(450, max_unpaired * 8),
            max_cluster_unpaired=min(250, max_unpaired * 4),
            exit_loss_cents=exit_loss,
            max_unpaired_minutes=max_minutes,
            min_reward_daily=min_reward,
            max_market_competitiveness=max_comp,
            allowed_categories=args.allowed_categories,
            excluded_categories=args.excluded_categories,
        )
        _, _, summary = simulate_lp(snapshots, cfg)
        if summary.empty:
            continue
        row = summary.iloc[0].to_dict()
        row["score"] = (
            row.get("total_pnl_usdc", 0)
            + 0.25 * row.get("total_reward_usdc", 0)
            - 3.0 * abs(row.get("max_drawdown_mtm_usdc", 0))
            + 10.0 * row.get("reward_to_trading_loss_ratio", 0)
        )
        rows.append(row)
    result = pd.DataFrame(rows)
    if result.empty:
        raise SystemExit("No configs produced trades/quotes")
    result = result.sort_values(["total_pnl_usdc", "reward_to_trading_loss_ratio", "max_drawdown_mtm_pct"], ascending=[False, False, False])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    cols = [
        "total_pnl_usdc", "total_reward_usdc", "total_inventory_exit_pnl_usdc", "max_drawdown_mtm_pct",
        "reward_to_trading_loss_ratio", "pair_completion_ratio_shares", "quote_size_shares", "quote_offset",
        "max_unpaired_per_market", "exit_loss_cents", "max_unpaired_minutes", "min_reward_daily", "max_market_competitiveness",
    ]
    print(result[cols].head(args.top).to_string(index=False))
    print(f"\nWrote {len(result)} rows to {out}")


if __name__ == "__main__":
    main()
