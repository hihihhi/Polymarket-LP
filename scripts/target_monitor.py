#!/usr/bin/env python3
"""Evaluate whether LP paper evidence clears a monthly income target gate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.target import TargetMonitorConfig, target_monitor_from_summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paper-summary", required=True, help="paper_summary.json from scripts/paper_analyze.py")
    p.add_argument("--out", default="", help="Optional JSON output path")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--min-observation-hours", type=float, default=24.0)
    p.add_argument("--max-fill-proxy-rate", type=float, default=0.0)
    p.add_argument("--max-stale-fill-rate", type=float, default=0.0)
    p.add_argument("--min-capture-margin", type=float, default=1.0)
    p.add_argument("--paid-reward-verified", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    summary = json.loads(Path(args.paper_summary).read_text(encoding="utf-8"))
    result = target_monitor_from_summary(
        summary,
        TargetMonitorConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            reward_to_loss_haircut=args.reward_to_loss_haircut,
            days_per_month=args.days_per_month,
            min_observation_hours=args.min_observation_hours,
            max_fill_proxy_rate=args.max_fill_proxy_rate,
            max_stale_fill_rate=args.max_stale_fill_rate,
            min_capture_margin=args.min_capture_margin,
            paid_reward_verified=args.paid_reward_verified,
        ),
    )
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
