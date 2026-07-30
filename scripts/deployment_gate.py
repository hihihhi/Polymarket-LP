#!/usr/bin/env python3
"""Combine LP target status and execution telemetry into a deployment gate.

This is a local auditor only. It reads JSON files and writes JSON/Markdown
artifacts. It never signs, submits, cancels, or inspects private keys.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.deployment_gate import (
    DeploymentReadinessConfig,
    evaluate_deployment_readiness,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-status", required=True)
    p.add_argument("--telemetry-audit", default="")
    p.add_argument("--out", default="")
    p.add_argument("--markdown-out", default="")
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--required-capture-rate", type=float, default=0.5)
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--min-observation-hours", type=float, default=24.0)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--max-active-pair-notional", type=float, default=1600.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--max-abs-mid-change-to-next", type=float, default=float("inf"))
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.20)
    p.add_argument("--no-require-telemetry", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    target = _load_json(args.target_status)
    telemetry = _load_json(args.telemetry_audit) if args.telemetry_audit else None
    result = evaluate_deployment_readiness(
        target_status=target,
        telemetry_audit=telemetry,
        cfg=DeploymentReadinessConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            required_capture_rate=args.required_capture_rate,
            min_target_margin=args.min_target_margin,
            min_observation_hours=args.min_observation_hours,
            min_unique_markets=args.min_unique_markets,
            max_active_pair_notional=args.max_active_pair_notional,
            max_pending_quote_rate=args.max_pending_quote_rate,
            max_abs_mid_change_to_next=args.max_abs_mid_change_to_next,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
            require_telemetry=not args.no_require_telemetry,
        ),
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(_json_safe(result), indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    markdown = _markdown(result)
    if args.markdown_out:
        md = Path(args.markdown_out)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(markdown, encoding="utf-8")
        if args.downloads_copy:
            shutil.copyfile(md, args.downloads_copy)
    print(
        json.dumps(
            {"status": result["status"], "blockers": result["blockers"]}, indent=2
        )
    )


def _load_json(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _markdown(result: dict[str, object]) -> str:
    metrics = result["metrics"]
    gates = result["gates"]
    assert isinstance(metrics, dict)
    assert isinstance(gates, dict)
    lines = [
        "# LP deployment readiness gate",
        "",
        "Safety: local audit only; no private keys, order signing, order submission, or cancellation.",
        "",
        "| Item | Value |",
        "|---|---:|",
        f"| Status | `{result['status']}` |",
        f"| Observation hours | {_num(metrics.get('duration_hours'))} |",
        f"| Unique markets quoted | {metrics.get('unique_markets_quoted')} |",
        f"| Max active pair notional | {_money(metrics.get('max_active_pair_notional'))} |",
        f"| Cash reserve | {_money(metrics.get('cash_reserve_usdc'))} ({_pct(metrics.get('cash_reserve_fraction'))}) |",
        f"| Net monthly after haircut | {_money(metrics.get('net_monthly_after_loss_haircut'))} |",
        f"| Capture needed | {_pct(metrics.get('capture_needed_for_target'))} |",
        f"| Required capture stress | {_pct(metrics.get('required_capture_rate'))} |",
        f"| Required-capture p05 net monthly | {_money(metrics.get('captured_p05_net_monthly_at_required_capture'))} |",
        f"| Fill/stale/pending proxy | {_pct(metrics.get('fill_proxy_rate'))} / {_pct(metrics.get('stale_fill_rate'))} / {_pct(metrics.get('pending_quote_rate'))} |",
        f"| Max next-mid move | {_num(metrics.get('max_abs_mid_change_to_next'), 4)} |",
        "",
        "| Gate | Passed |",
        "|---|:---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in gates.items())
    blockers = result.get("blockers", [])
    if isinstance(blockers, list):
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {x}" for x in blockers)
    lines.append("")
    return "\n".join(lines)


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        return f"{100 * float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
