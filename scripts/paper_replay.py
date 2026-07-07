#!/usr/bin/env python3
"""Generate paper LP quote intents from snapshot or live public data.

This does not place orders. It reads point-in-time market snapshots and writes the
quotes the strategy would have posted after reward-density, volatility, jump, and
capital-budget filters.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lp_backtest import LPConfig, load_snapshots
from polymarket_lp.paper import LiveSnapshotConfig, build_paper_quotes, run_live_paper_loop
from polymarket_lp.governed_config import apply_risk_governor_to_lp_config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", help="CSV of point-in-time snapshots for offline replay")
    p.add_argument("--out", default="paper_quotes.csv")
    p.add_argument("--live", action="store_true", help="Collect public live snapshots before writing paper quotes")
    p.add_argument("--snapshot-out", default="data/raw/live_lp_snapshots.csv")
    p.add_argument("--manifest-out", default="data/raw/live_lp_paper_manifest.json")
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--interval-seconds", type=float, default=300.0)
    p.add_argument("--gamma-base-url", default="https://gamma-api.polymarket.com")
    p.add_argument("--clob-base-url", default="https://clob.polymarket.com")
    p.add_argument("--event-limit", type=int, default=500)
    p.add_argument("--max-events", type=int)
    p.add_argument("--include-clob-books", action="store_true")
    p.add_argument("--request-timeout-seconds", type=float, default=20.0)
    p.add_argument("--sleep-between-book-requests-seconds", type=float, default=0.0)
    p.add_argument("--initial-capital", type=float, default=2000)
    p.add_argument("--quote-size", type=float, default=800)
    p.add_argument("--quote-offset", type=float, default=0.035)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--active-capital-limit", type=float, default=1900)
    p.add_argument("--min-reward-daily", type=float, default=0.0)
    p.add_argument("--max-market-competitiveness", type=float, default=1.0)
    p.add_argument("--allowed-categories", default="")
    p.add_argument("--excluded-categories", default=LPConfig().excluded_categories)
    p.add_argument("--min-reward-density-per-day", type=float, default=0.0)
    p.add_argument("--recent-vol-window", type=int, default=6)
    p.add_argument("--max-recent-vol", type=float, default=0.006)
    p.add_argument("--max-recent-jump", type=float, default=0.025)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.5)
    p.add_argument("--depth-cap-quote-size", action="store_true", help="Cap quote size by displayed opposite-side CLOB ask depth for taker rescue")
    p.add_argument("--depth-quote-size-fraction", type=float, default=1.0, help="Maximum quote size as fraction of the smaller YES/NO displayed ask size")
    p.add_argument("--min-depth-capped-quote-size", type=float, default=1.0, help="Drop depth-capped quotes smaller than this share size")
    p.add_argument("--risk-governor-json", default="", help="Optional risk_governor.py JSON to govern qsize/capital")
    p.add_argument("--allow-risk-governor-not-core", action="store_true")
    return p.parse_args()


def make_lp_config(args: argparse.Namespace) -> LPConfig:
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
        depth_cap_quote_size=args.depth_cap_quote_size,
        depth_quote_size_fraction=args.depth_quote_size_fraction,
        min_depth_capped_quote_size_shares=args.min_depth_capped_quote_size,
    )
    if getattr(args, "risk_governor_json", ""):
        risk_governor = json.loads(Path(args.risk_governor_json).read_text(encoding="utf-8-sig"))
        cfg, _ = apply_risk_governor_to_lp_config(
            cfg,
            risk_governor,
            require_core_passed=not getattr(args, "allow_risk_governor_not_core", False),
        )
    return cfg


def make_snapshot_config(args: argparse.Namespace) -> LiveSnapshotConfig:
    return LiveSnapshotConfig(
        gamma_base_url=args.gamma_base_url,
        clob_base_url=args.clob_base_url,
        event_limit=args.event_limit,
        max_events=args.max_events,
        include_clob_books=args.include_clob_books,
        request_timeout_seconds=args.request_timeout_seconds,
        sleep_between_book_requests_seconds=args.sleep_between_book_requests_seconds,
    )


def replay_snapshots(args: argparse.Namespace, cfg: LPConfig) -> None:
    if not args.snapshots:
        raise SystemExit("Provide --snapshots for offline replay or use --live")
    out = build_paper_quotes(load_snapshots(args.snapshots), cfg)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    print(f"Wrote {len(out):,} paper quote rows to {path}")


def run_live(args: argparse.Namespace, cfg: LPConfig) -> None:
    manifest = run_live_paper_loop(
        snapshot_path=args.snapshot_out,
        quotes_path=args.out,
        manifest_path=args.manifest_out,
        lp_config=cfg,
        snapshot_config=make_snapshot_config(args),
        iterations=args.iterations,
        interval_seconds=args.interval_seconds,
    )
    print(json.dumps(manifest, indent=2, default=str))


def main() -> None:
    args = parse_args()
    cfg = make_lp_config(args)
    if args.live:
        run_live(args, cfg)
    else:
        replay_snapshots(args, cfg)


if __name__ == "__main__":
    main()
