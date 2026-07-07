#!/usr/bin/env python3
"""Terminal completion audit for the managed $1k/month LP objective."""
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

from polymarket_lp.completion import CompletionAuditConfig, evaluate_completion_audit


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--objective-audit", required=True)
    p.add_argument("--sustainability-stress", required=True)
    p.add_argument("--risk-governor", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--allow-objective-not-proven", action="store_true")
    p.add_argument("--allow-sustainability-fail", action="store_true")
    p.add_argument("--allow-risk-governor-fail", action="store_true")
    p.add_argument("--allow-deployment-not-allowed", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate_completion_audit(
        objective_audit=_load(args.objective_audit),
        sustainability_stress=_load(args.sustainability_stress),
        risk_governor=_load(args.risk_governor),
        cfg=CompletionAuditConfig(
            require_objective_proven=not args.allow_objective_not_proven,
            require_sustainability_stress=not args.allow_sustainability_fail,
            require_risk_governor=not args.allow_risk_governor_fail,
            require_deployment_allowed=not args.allow_deployment_not_allowed,
        ),
    )
    payload = {
        **result,
        "source_paths": {
            "objective_audit": args.objective_audit,
            "sustainability_stress": args.sustainability_stress,
            "risk_governor": args.risk_governor,
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
    print(json.dumps({"status": payload["status"], "blockers": payload["blockers"]}, indent=2))


def _markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    lines = [
        "# LP objective completion audit",
        "",
        "Safety: audit/report only; no private keys, signing, order submission, or cancellation.",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Evidence metrics",
        "",
        f"- Selected qsize: {metrics.get('selected_qsize')}; governed qsize: {metrics.get('governed_qsize')}.",
        f"- Objective packet 50% p05: {_money(metrics.get('objective_packet_50pct_p05'))}; governor 50% p05: {_money(metrics.get('governor_50pct_p05'))}.",
        f"- Sustainability reference income: {_money(metrics.get('sustainability_reference_income'))}; after cap loss: {_money(metrics.get('sustainability_after_cap_loss'))}.",
        f"- Cash reserve: {_pct(metrics.get('cash_reserve_fraction'))}; unhedged loss: {_money(metrics.get('unhedged_loss_usdc'))}; cap loss: {_money(metrics.get('configured_cap_loss_usdc'))}; cap recovery: {_num(metrics.get('configured_cap_recovery_days'))} days.",
        "",
        "## Terminal gates",
        "",
        "| Gate | Required | Passed |",
        "|---|:---:|:---:|",
    ]
    required = payload["required_gates"]
    gates = payload["gates"]
    lines.extend(f"| {name} | {required[name]} | {gates.get(name)} |" for name in required)
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
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: Any) -> str:
    try:
        return f"{100 * float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
