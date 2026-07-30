#!/usr/bin/env python3
"""Audit signed-paper/live LP order lifecycle logs for deployment proof.

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

from polymarket_lp.lifecycle import (
    LifecycleAuditConfig,
    audit_order_lifecycle,
    load_lifecycle_csv,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--events",
        required=True,
        help="CSV with timestamp, client_order_id/order_id, lifecycle_state",
    )
    p.add_argument("--out", default="", help="Optional JSON output path")
    p.add_argument("--min-completed-orders", type=int, default=1)
    p.add_argument("--max-sign-to-submit-seconds", type=float, default=10.0)
    p.add_argument("--max-submit-to-ack-seconds", type=float, default=10.0)
    p.add_argument("--max-cancel-latency-seconds", type=float, default=30.0)
    p.add_argument("--max-fill-to-rescue-seconds", type=float, default=120.0)
    p.add_argument("--min-paid-reward-usdc", type=float, default=0.01)
    p.add_argument("--min-reward-capture-rate", type=float, default=0.5)
    p.add_argument("--no-require-queue-estimate", action="store_true")
    p.add_argument("--no-require-cancel-confirmation", action="store_true")
    p.add_argument("--no-require-paid-reward", action="store_true")
    p.add_argument("--no-require-final-pnl", action="store_true")
    p.add_argument("--no-require-rescue-decision-for-fills", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result = audit_order_lifecycle(
        load_lifecycle_csv(args.events),
        LifecycleAuditConfig(
            min_completed_orders=args.min_completed_orders,
            max_sign_to_submit_seconds=args.max_sign_to_submit_seconds,
            max_submit_to_ack_seconds=args.max_submit_to_ack_seconds,
            max_cancel_latency_seconds=args.max_cancel_latency_seconds,
            max_fill_to_rescue_seconds=args.max_fill_to_rescue_seconds,
            min_paid_reward_usdc=args.min_paid_reward_usdc,
            min_reward_capture_rate=args.min_reward_capture_rate,
            require_queue_estimate=not args.no_require_queue_estimate,
            require_cancel_confirmation=not args.no_require_cancel_confirmation,
            require_paid_reward=not args.no_require_paid_reward,
            require_final_pnl=not args.no_require_final_pnl,
            require_rescue_decision_for_fills=not args.no_require_rescue_decision_for_fills,
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
