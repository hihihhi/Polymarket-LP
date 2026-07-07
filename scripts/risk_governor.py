#!/usr/bin/env python3
"""Build a risk-governed operating decision from LP proof artifacts."""
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

from polymarket_lp.risk_governor import RiskGovernorConfig, evaluate_risk_governor


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-packet", required=True)
    p.add_argument("--allocation-selection", required=True)
    p.add_argument("--objective-audit", default="")
    p.add_argument("--sustainability-stress", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.40)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.50)
    p.add_argument("--max-configured-cap-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--min-income-margin", type=float, default=1.0)
    p.add_argument("--min-sizing-scale", type=float, default=0.25)
    p.add_argument("--allow-deployment-without-objective-proof", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate_risk_governor(
        evidence_packet=_load(args.evidence_packet),
        allocation_selection=_load(args.allocation_selection),
        objective_audit=_load(args.objective_audit) if args.objective_audit else None,
        sustainability_stress=_load(args.sustainability_stress) if args.sustainability_stress else None,
        cfg=RiskGovernorConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
            max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
            max_configured_cap_loss_fraction=args.max_configured_cap_loss_fraction,
            max_configured_cap_recovery_days=args.max_configured_cap_recovery_days,
            min_income_margin=args.min_income_margin,
            min_sizing_scale=args.min_sizing_scale,
            require_objective_proven_for_deployment=not args.allow_deployment_without_objective_proof,
        ),
    )
    payload = {
        **result,
        "source_paths": {
            "evidence_packet": args.evidence_packet,
            "allocation_selection": args.allocation_selection,
            "objective_audit": args.objective_audit,
            "sustainability_stress": args.sustainability_stress,
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
        "# LP risk governor",
        "",
        "Safety: audit/report only; no private keys, signing, order submission, or cancellation.",
        "",
        f"Status: `{payload['status']}`",
        f"Deployment allowed: `{payload['deployment_allowed']}`",
        "",
        "## Operating size",
        "",
        f"- Selected qsize: {metrics['selected_qsize']:.0f}; recommended scale: {_pct(metrics['recommended_scale'])}; governed qsize: {metrics['recommended_qsize']:.0f}.",
        f"- Governing 50% p05 monthly income: {_money(metrics['governing_50pct_p05_monthly_income'])}.",
        f"- Active notional: {_money(metrics['active_pair_notional'])}; max by cash reserve: {_money(metrics['max_active_pair_notional_by_cash'])}.",
        f"- Cash reserve: {_pct(metrics['cash_reserve_fraction'])}.",
        f"- Unhedged loss: {_money(metrics['unhedged_loss_usdc'])}; max allowed: {_money(metrics['max_unhedged_loss_usdc'])}.",
        f"- Configured-cap loss: {_money(metrics['configured_cap_loss_usdc'])}; max by recovery: {_money(metrics['max_configured_cap_loss_by_recovery_usdc'])}; recovery: {metrics['configured_cap_recovery_days']:.2f} days.",
        "",
        "## Scale limits",
        "",
        "| Constraint | Scale cap |",
        "|---|---:|",
    ]
    lines.extend(f"| {k} | {_pct(v)} |" for k, v in payload["scale_limits"].items())
    lines += ["", "## Gates", "", "| Gate | Passed |", "|---|:---:|"]
    lines.extend(f"| {k} | {v} |" for k, v in payload["gates"].items())
    if payload["blockers"]:
        lines += ["", "## Blockers / remaining proof gates", ""]
        lines.extend(f"- {x}" for x in payload["blockers"])
    lines += ["", "## Source artifacts", ""]
    for key, path in payload["source_paths"].items():
        if path:
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
