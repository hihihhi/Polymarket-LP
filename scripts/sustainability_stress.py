#!/usr/bin/env python3
"""Run LP income-survival stress for the $1k/month objective."""

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

from polymarket_lp.sustainability import (
    SustainabilityStressConfig,
    evaluate_sustainability_stress,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-packet", required=True)
    p.add_argument("--allocation-selection", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reference-capture-rate", type=float, default=0.5)
    p.add_argument("--base-policy", default="min_selected_packet")
    p.add_argument("--reward-multipliers", default="1,0.75,0.5,0.35,0.25")
    p.add_argument("--capture-rates", default="1,0.75,0.5,0.4,0.35,0.25")
    p.add_argument("--monthly-loss-shocks", default="")
    p.add_argument("--max-required-reward-multiplier", type=float, default=0.75)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--max-unhedged-recovery-days", type=float, default=15.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.40)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.50)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    packet = _load(args.evidence_packet)
    allocation = _load(args.allocation_selection)
    result = evaluate_sustainability_stress(
        evidence_packet=packet,
        allocation_selection=allocation,
        cfg=SustainabilityStressConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            reference_capture_rate=args.reference_capture_rate,
            base_policy=args.base_policy,
            reward_multipliers=tuple(_float_list(args.reward_multipliers)),
            capture_rates=tuple(_float_list(args.capture_rates)),
            monthly_loss_shocks=tuple(_float_list(args.monthly_loss_shocks)),
            max_required_reward_multiplier=args.max_required_reward_multiplier,
            max_configured_cap_recovery_days=args.max_configured_cap_recovery_days,
            max_unhedged_recovery_days=args.max_unhedged_recovery_days,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
            max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
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
            {"status": payload["status"], "blockers": payload["blockers"]}, indent=2
        )
    )


def _markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    gates = payload["gates"]
    rows = payload["stress_rows"]
    configured_rows = [r for r in rows if r["loss_label"] == "configured_cap_loss"]
    lines = [
        "# LP sustainability stress audit",
        "",
        "Safety: audit/report only; no private keys, signing, order submission, or cancellation.",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Conservative base",
        "",
        f"- Base p05 raw monthly ({metrics['base_policy']}): {_money(metrics['base_raw_p05_monthly'])}.",
        f"- Reference capture: {_pct(metrics['reference_capture_rate'])}; reference p05 income: {_money(metrics['reference_monthly_income'])}.",
        f"- Configured-cap loss shock: {_money(metrics['configured_cap_loss_usdc'])}; after-shock monthly: {_money(metrics['configured_cap_reference_monthly_after_loss'])}; recovery: {metrics['configured_cap_recovery_days']:.2f} days.",
        f"- All-active unhedged loss shock: {_money(metrics['unhedged_loss_usdc'])}; recovery at reference income: {metrics['unhedged_recovery_days']:.2f} days.",
        f"- Breakeven reward multiplier at 50% capture: no loss {_pct(metrics['breakeven_reward_multiplier_no_loss_at_reference_capture'])}; configured cap {_pct(metrics['breakeven_reward_multiplier_configured_cap_at_reference_capture'])}; unhedged {_pct(metrics['breakeven_reward_multiplier_unhedged_at_reference_capture'])}.",
        "",
        "## Gate summary",
        "",
        "| Gate | Passed |",
        "|---|:---:|",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in gates.items())
    lines += [
        "",
        "## Configured-cap stress grid",
        "",
        "| Capture | Reward regime | Net monthly after cap loss | Target pass | Recovery days |",
        "|---:|---:|---:|:---:|---:|",
    ]
    for row in configured_rows:
        lines.append(
            f"| {_pct(row['capture_rate'])} | {_pct(row['reward_multiplier'])} | "
            f"{_money(row['net_monthly_after_loss_usdc'])} | {row['target_passed']} | {row['recovery_days']:.2f} |"
        )
    if payload["blockers"]:
        lines += ["", "## Blockers / warnings", ""]
        lines.extend(f"- {x}" for x in payload["blockers"])
    lines += ["", "## Source artifacts", ""]
    for key, path in payload["source_paths"].items():
        lines.append(f"- {key}: `{path}`")
    lines.append("")
    return "\n".join(lines)


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _float_list(text: str) -> list[float]:
    if not text.strip():
        return []
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


if __name__ == "__main__":
    main()
