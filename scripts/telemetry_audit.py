#!/usr/bin/env python3
"""Audit local LP execution telemetry for deployment proof.

This script reads local CSV exports only. It never signs, submits, cancels, or
inspects private keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.telemetry import (
    ExecutionTelemetryConfig,
    audit_execution_telemetry,
    load_csv_if,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--orders",
        required=True,
        help="CSV with client_order_id/order_id and order timestamps",
    )
    p.add_argument("--fills", default="", help="Optional fill CSV")
    p.add_argument("--cancels", default="", help="Optional cancel CSV")
    p.add_argument("--rewards", default="", help="Optional paid reward CSV")
    p.add_argument("--out", default="", help="Optional JSON output path")
    p.add_argument("--min-orders", type=int, default=1)
    p.add_argument("--min-cancels", type=int, default=1)
    p.add_argument("--min-paid-reward-usdc", type=float, default=0.01)
    p.add_argument("--min-reward-capture-rate", type=float, default=0.5)
    p.add_argument("--max-cancel-latency-seconds", type=float, default=30.0)
    p.add_argument("--max-unmatched-fill-rate", type=float, default=0.0)
    p.add_argument("--no-require-paid-rewards", action="store_true")
    p.add_argument("--no-require-cancel-telemetry", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result = audit_execution_telemetry(
        orders=load_csv_if(args.orders),
        fills=load_csv_if(args.fills),
        cancels=load_csv_if(args.cancels),
        rewards=load_csv_if(args.rewards),
        cfg=ExecutionTelemetryConfig(
            min_orders=args.min_orders,
            min_cancels=args.min_cancels,
            min_paid_reward_usdc=args.min_paid_reward_usdc,
            min_reward_capture_rate=args.min_reward_capture_rate,
            max_cancel_latency_seconds=args.max_cancel_latency_seconds,
            max_unmatched_fill_rate=args.max_unmatched_fill_rate,
            require_paid_rewards=not args.no_require_paid_rewards,
            require_cancel_telemetry=not args.no_require_cancel_telemetry,
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
