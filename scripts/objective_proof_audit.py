#!/usr/bin/env python3
"""Audit whether the current evidence proves the $1k/month managed-risk objective."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.proof import ObjectiveProofConfig, evaluate_objective_proof


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-packet", required=True)
    p.add_argument("--allocation-selection", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.50)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.40)
    p.add_argument("--allow-short-sample", action="store_true")
    p.add_argument("--allow-missing-rolling", action="store_true")
    p.add_argument("--allow-missing-telemetry", action="store_true")
    p.add_argument("--allow-deployment-not-ready", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    packet = _load(args.evidence_packet)
    allocation = _load(args.allocation_selection)
    result = evaluate_objective_proof(
        evidence_packet=packet,
        allocation_selection=allocation,
        cfg=ObjectiveProofConfig(
            target_monthly_usdc=args.target_monthly,
            require_24h_sample=not args.allow_short_sample,
            require_rolling_all_pass=not args.allow_missing_rolling,
            require_execution_telemetry=not args.allow_missing_telemetry,
            require_deployment_ready=not args.allow_deployment_not_ready,
            max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
            max_configured_cap_recovery_days=args.max_configured_cap_recovery_days,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
        ),
    )
    payload = {
        **result,
        "source_paths": {
            "evidence_packet": args.evidence_packet,
            "allocation_selection": args.allocation_selection,
        },
    }
    out = Path(args.out)
    md = Path(args.markdown_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    md.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    md.write_text(_markdown(payload), encoding="utf-8")
    if args.downloads_copy:
        shutil.copyfile(md, args.downloads_copy)
    if args.downloads_json_copy:
        shutil.copyfile(out, args.downloads_json_copy)
    print(
        json.dumps(
            {"status": result["status"], "blockers": result["blockers"]}, indent=2
        )
    )


def _markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    lines = [
        "# Objective proof audit",
        "",
        "Safety: audit/report only; no private keys, signing, order submission, or cancellation.",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Current evidence",
        "",
        f"- Selected qsize: {metrics.get('selected_qsize')}; selected 50% p05 income: {_money(metrics['selected_50pct_capture_p05'])}; selected net monthly: {_money(metrics['selected_net_monthly_after_loss_haircut'])}.",
        f"- Packet observation: {metrics['packet_observation_hours']:.2f}h; packet 50% p05 income: {_money(metrics['packet_50pct_capture_p05'])}; packet net monthly: {_money(metrics['packet_net_monthly_after_loss_haircut'])}.",
        f"- Selected cash reserve: {_pct(metrics['selected_cash_reserve_fraction'])}; selected unhedged loss: {_pct(metrics['selected_unhedged_loss_fraction'])}; selected cap recovery: {metrics['selected_configured_cap_recovery_days']:.2f} days.",
        "",
        "## Gate summary",
        "",
        "| Gate | Required | Passed |",
        "|---|:---:|:---:|",
    ]
    required = payload["required_gates"]
    gates = payload["gates"]
    lines.extend(
        f"| {name} | {required[name]} | {gates.get(name)} |" for name in required
    )
    if payload["blockers"]:
        lines += ["", "## Blockers", ""]
        lines.extend(f"- {x}" for x in payload["blockers"])
    lines += ["", "## Source artifacts", ""]
    for key, path in payload["source_paths"].items():
        lines.append(f"- {key}: `{path}`")
    lines.append("")
    return "\n".join(lines)


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


if __name__ == "__main__":
    main()
