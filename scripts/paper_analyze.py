#!/usr/bin/env python3
"""Analyze LP paper quote outcomes against later public snapshots.

This does not place orders. It estimates would-fill/stale-fill diagnostics by
checking whether the next observed midpoint crossed each paper bid.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lp_backtest import load_snapshots
from polymarket_lp.paper import PaperAnalysisConfig, run_paper_analysis_to_files


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True, help="CSV written by scripts/paper_replay.py --live")
    p.add_argument("--quotes", required=True, help="Paper quote intents CSV")
    p.add_argument("--out-dir", default="data/processed/paper_analysis")
    p.add_argument("--max-stale-seconds", type=float, default=900.0)
    p.add_argument("--stale-mid-change", type=float, default=0.03)
    p.add_argument("--fill-mid-cross-buffer", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_paper_analysis_to_files(
        snapshots=load_snapshots(args.snapshots),
        quotes=pd.read_csv(args.quotes),
        out_dir=args.out_dir,
        cfg=PaperAnalysisConfig(
            max_stale_seconds=args.max_stale_seconds,
            stale_mid_change=args.stale_mid_change,
            fill_mid_cross_buffer=args.fill_mid_cross_buffer,
            max_reward_gap_seconds=args.max_reward_gap_seconds,
        ),
    )
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
