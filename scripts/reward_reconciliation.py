#!/usr/bin/env python3
"""Reconcile estimated LP rewards to paid reward/account ledger CSVs.

Local files only. This script does not log in, fetch account data, place
orders, sign orders, or handle private keys.
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

from polymarket_lp.reward_reconciliation import (  # noqa: E402
    RewardReconciliationConfig,
    load_csv,
    reconcile_paid_rewards,
    reward_reconciliation_schema,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--estimates", default="", help="Estimated reward CSV")
    p.add_argument("--paid", default="", help="Paid reward/account ledger CSV")
    p.add_argument("--out", default="", help="Optional JSON output path")
    p.add_argument("--joined-out", default="", help="Optional joined reconciliation CSV")
    p.add_argument("--schema-out", default="", help="Optional schema CSV output path")
    p.add_argument("--min-paid-reward-usdc", type=float, default=0.01)
    p.add_argument("--min-reward-capture-rate", type=float, default=0.5)
    p.add_argument("--max-unmatched-estimate-rate", type=float, default=0.05)
    p.add_argument("--max-unmatched-paid-rate", type=float, default=0.05)
    p.add_argument("--require-client-order-match", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.schema_out:
        out = Path(args.schema_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(reward_reconciliation_schema()).to_csv(out, index=False)
    result = reconcile_paid_rewards(
        load_csv(args.estimates),
        load_csv(args.paid),
        RewardReconciliationConfig(
            min_paid_reward_usdc=args.min_paid_reward_usdc,
            min_reward_capture_rate=args.min_reward_capture_rate,
            max_unmatched_estimate_rate=args.max_unmatched_estimate_rate,
            max_unmatched_paid_rate=args.max_unmatched_paid_rate,
            require_client_order_match=args.require_client_order_match,
        ),
    )
    if args.joined_out:
        out = Path(args.joined_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result["joined"]).to_csv(out, index=False)
    payload = {k: v for k, v in result.items() if k != "joined"}
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
