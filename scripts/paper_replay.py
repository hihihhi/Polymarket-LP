#!/usr/bin/env python3
"""Generate paper LP quote intents from snapshot data.

This does not place orders. It reads point-in-time market snapshots and writes the
quotes the strategy would have posted after reward-density, volatility, jump, and
capital-budget filters.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from polymarket_lp.lp_backtest import LPConfig, filter_snapshots_for_strategy, load_snapshots, quote_for_row


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--out", default="paper_quotes.csv")
    p.add_argument("--initial-capital", type=float, default=2000)
    p.add_argument("--quote-size", type=float, default=800)
    p.add_argument("--quote-offset", type=float, default=0.035)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--active-capital-limit", type=float, default=1900)
    p.add_argument("--min-reward-daily", type=float, default=0.0)
    p.add_argument("--max-market-competitiveness", type=float, default=1.0)
    p.add_argument("--allowed-categories", default="")
    p.add_argument("--excluded-categories", default="")
    p.add_argument("--min-reward-density-per-day", type=float, default=0.0)
    p.add_argument("--recent-vol-window", type=int, default=6)
    p.add_argument("--max-recent-vol", type=float, default=0.006)
    p.add_argument("--max-recent-jump", type=float, default=0.025)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.5)
    return p.parse_args()


def add_risk_features(df: pd.DataFrame, cfg: LPConfig) -> pd.DataFrame:
    rows = df.copy().sort_values(["condition_id", "timestamp"]).reset_index(drop=True)
    rows["mid_change"] = rows.groupby("condition_id")["yes_mid"].diff().abs().fillna(0.0)
    rows["recent_vol"] = rows.groupby("condition_id")["mid_change"].transform(
        lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).mean()
    )
    rows["recent_jump"] = rows.groupby("condition_id")["mid_change"].transform(
        lambda s: s.rolling(cfg.recent_vol_window, min_periods=1).max()
    )
    return rows


def main() -> None:
    args = parse_args()
    cfg = LPConfig(
        initial_capital=args.initial_capital,
        quote_size_shares=args.quote_size,
        quote_offset=args.quote_offset,
        safety_margin=args.safety_margin,
        active_capital_limit=args.active_capital_limit,
        min_reward_daily=args.min_reward_daily,
        max_market_competitiveness=args.max_market_competitiveness,
        allowed_categories=args.allowed_categories,
        excluded_categories=args.excluded_categories,
        min_reward_density_per_day=args.min_reward_density_per_day,
        recent_vol_window=args.recent_vol_window,
        max_recent_vol=args.max_recent_vol,
        max_recent_jump=args.max_recent_jump,
        vol_quote_multiplier=args.vol_quote_multiplier,
    )
    snapshots = add_risk_features(filter_snapshots_for_strategy(load_snapshots(args.snapshots), cfg), cfg)
    out_rows = []
    for ts, group in snapshots.groupby("timestamp", sort=True):
        candidates = []
        for _, row in group.iterrows():
            if float(row.get("recent_vol", 0.0)) > cfg.max_recent_vol:
                continue
            if float(row.get("recent_jump", 0.0)) > cfg.max_recent_jump:
                continue
            adaptive_offset = cfg.quote_offset + cfg.vol_quote_multiplier * float(row.get("recent_vol", 0.0))
            q = quote_for_row(row, cfg, quote_offset=adaptive_offset)
            if not q["eligible"]:
                continue
            if float(q["reward_density_per_day"]) < cfg.min_reward_density_per_day:
                continue
            candidates.append((float(q["reward_density_per_day"]), row, q))
        candidates.sort(key=lambda x: x[0], reverse=True)
        active = 0.0
        for _, row, q in candidates:
            notional = float(q["active_order_notional"])
            if active + notional > cfg.active_capital_limit:
                continue
            active += notional
            for side, bid_col in [("YES", "yes_bid"), ("NO", "no_bid")]:
                out_rows.append(
                    {
                        "timestamp": ts,
                        "condition_id": row["condition_id"],
                        "market_id": row.get("market_id", ""),
                        "category": row.get("category", ""),
                        "cluster": row.get("cluster", "unknown"),
                        "side": side,
                        "bid_price": q[bid_col],
                        "size_shares": q["quote_size"],
                        "pair_cost": q["pair_cost"],
                        "quote_offset": q["quote_offset"],
                        "reward_share_est": q["reward_share"],
                        "reward_density_per_day": q["reward_density_per_day"],
                        "active_order_notional_pair": notional,
                        "recent_vol": row.get("recent_vol", 0.0),
                        "recent_jump": row.get("recent_jump", 0.0),
                    }
                )
    out = pd.DataFrame(out_rows)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    print(f"Wrote {len(out):,} paper quote rows to {path}")


if __name__ == "__main__":
    main()
