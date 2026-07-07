#!/usr/bin/env python3
"""Build shadow order/fill/cancel telemetry from public-paper LP quote intents."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.shadow_telemetry import ShadowTelemetryConfig, build_shadow_execution_telemetry
from polymarket_lp.telemetry import ExecutionTelemetryConfig, audit_execution_telemetry


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--quotes", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--assumed-cancel-latency-seconds", type=float, default=1.0)
    p.add_argument("--assumed-fee-rate", type=float, default=0.0)
    p.add_argument("--paid-reward-capture-rate", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    snapshots = pd.read_csv(args.snapshots)
    quotes = pd.read_csv(args.quotes)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    telemetry = build_shadow_execution_telemetry(
        snapshots=snapshots,
        quotes=quotes,
        cfg=ShadowTelemetryConfig(
            assumed_cancel_latency_seconds=args.assumed_cancel_latency_seconds,
            assumed_fee_rate=args.assumed_fee_rate,
            paid_reward_capture_rate=args.paid_reward_capture_rate,
            max_reward_gap_seconds=args.max_reward_gap_seconds,
        ),
    )
    for name in ["orders", "fills", "cancels", "rewards"]:
        telemetry[name].to_csv(out / f"{name}.csv", index=False)
    lifecycle_audit = audit_execution_telemetry(
        orders=telemetry["orders"],
        fills=telemetry["fills"],
        cancels=telemetry["cancels"],
        rewards=telemetry["rewards"],
        cfg=ExecutionTelemetryConfig(require_paid_rewards=False),
    )
    strict_paid_reward_audit = audit_execution_telemetry(
        orders=telemetry["orders"],
        fills=telemetry["fills"],
        cancels=telemetry["cancels"],
        rewards=telemetry["rewards"],
    )
    status = (
        "shadow_lifecycle_passed_real_reward_missing"
        if lifecycle_audit["gates"]["deployment_telemetry_passed"]
        and not strict_paid_reward_audit["gates"]["deployment_telemetry_passed"]
        else strict_paid_reward_audit["status"]
    )
    payload = {
        "status": status,
        "summary": telemetry["summary"],
        "config": telemetry["config"],
        "shadow_lifecycle_audit": lifecycle_audit,
        "strict_paid_reward_audit": strict_paid_reward_audit,
        "shadow_telemetry_audit": lifecycle_audit,
        "outputs": {name: str(out / f"{name}.csv") for name in ["orders", "fills", "cancels", "rewards"]},
        "safety": "shadow telemetry only; not exchange execution or paid reward proof",
    }
    (out / "shadow_telemetry_summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    markdown = _markdown(payload)
    (out / "shadow_telemetry_summary.md").write_text(markdown, encoding="utf-8")
    if args.downloads_copy:
        shutil.copyfile(out / "shadow_telemetry_summary.md", args.downloads_copy)
    if args.downloads_json_copy:
        shutil.copyfile(out / "shadow_telemetry_summary.json", args.downloads_json_copy)
    print(json.dumps({"status": status, "summary": telemetry["summary"]}, indent=2, default=str))


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    lifecycle_audit = payload["shadow_lifecycle_audit"]
    strict_audit = payload["strict_paid_reward_audit"]
    assert isinstance(summary, dict)
    assert isinstance(lifecycle_audit, dict)
    assert isinstance(strict_audit, dict)
    metrics = lifecycle_audit["metrics"]
    gates = lifecycle_audit["gates"]
    assert isinstance(metrics, dict)
    assert isinstance(gates, dict)
    strict_gates = strict_audit["gates"]
    assert isinstance(strict_gates, dict)
    lines = [
        "# Shadow execution telemetry",
        "",
        "Safety: generated from public-paper quote intents only; not exchange execution or paid reward proof.",
        "",
        f"Status: `{payload.get('status')}`",
        "",
        f"- Orders: {summary.get('orders')}; fills: {summary.get('fills')}; cancels: {summary.get('cancels')}.",
        f"- Estimated reward: {_money(summary.get('estimated_reward_usdc'))}; paid reward in shadow file: {_money(summary.get('paid_reward_usdc'))}.",
        f"- Fill/stale/pending proxies: {_pct(summary.get('would_fill_proxy_rate'))} / {_pct(summary.get('stale_fill_proxy_rate'))} / {_pct(summary.get('pending_quote_rate'))}.",
        f"- Lifecycle-only audit status: `{lifecycle_audit.get('status')}`; max cancel latency: {metrics.get('max_cancel_latency_seconds')}.",
        f"- Strict paid-reward audit status: `{strict_audit.get('status')}`.",
        "",
        "## Lifecycle-only gates",
        "",
        "| Gate | Passed |",
        "|---|:---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in gates.items())
    lines += ["", "## Strict paid-reward gates", "", "| Gate | Passed |", "|---|:---:|"]
    lines.extend(f"| {k} | {v} |" for k, v in strict_gates.items())
    lines += ["", "## Outputs", ""]
    outputs = payload["outputs"]
    assert isinstance(outputs, dict)
    lines.extend(f"- {k}: `{v}`" for k, v in outputs.items())
    lines.append("")
    return "\n".join(lines)


def _money(value: object) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        return f"{100 * float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
