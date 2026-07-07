#!/usr/bin/env python3
"""Audit whether LP losses can be hedged or capped.

This script is read-only. It reads capital-risk and optional evidence-packet
JSON, then writes JSON/Markdown hedge feasibility reports. It never signs,
submits, cancels, or inspects private keys.
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

from polymarket_lp.hedge import HedgeFeasibilityConfig, evaluate_hedge_feasibility  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capital-risk", required=True)
    p.add_argument("--evidence-packet", default="")
    p.add_argument("--out", default="")
    p.add_argument("--markdown-out", default="")
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--min-loss-reduction-fraction", type=float, default=0.50)
    p.add_argument("--max-configured-cap-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--min-pair-edge-usdc", type=float, default=0.0)
    p.add_argument("--max-pair-cost-per-share", type=float, default=1.0)
    p.add_argument("--exit-slippage", type=float, default=0.005)
    p.add_argument("--min-slippage-cushion-multiplier", type=float, default=1.0)
    p.add_argument("--external-hedge-available", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = HedgeFeasibilityConfig(
        initial_capital=args.initial_capital,
        min_loss_reduction_fraction=args.min_loss_reduction_fraction,
        max_configured_cap_loss_fraction=args.max_configured_cap_loss_fraction,
        max_configured_cap_recovery_days=args.max_configured_cap_recovery_days,
        min_pair_edge_usdc=args.min_pair_edge_usdc,
        max_pair_cost_per_share=args.max_pair_cost_per_share,
        exit_slippage=args.exit_slippage,
        min_slippage_cushion_multiplier=args.min_slippage_cushion_multiplier,
        external_hedge_available=args.external_hedge_available,
    )
    result = evaluate_hedge_feasibility(
        capital_risk=_load_json(args.capital_risk),
        evidence_packet=_load_json(args.evidence_packet) if args.evidence_packet else None,
        cfg=cfg,
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_json_safe(result), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    markdown = _markdown(result)
    if args.markdown_out:
        md = Path(args.markdown_out)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(markdown, encoding="utf-8")
        if args.downloads_copy:
            shutil.copyfile(md, args.downloads_copy)
    print(json.dumps({"status": result["status"], "blockers": result["blockers"]}, indent=2))


def _load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _markdown(result: dict[str, Any]) -> str:
    m = result["metrics"]
    gates = result["gates"]
    lines = [
        "# LP hedge feasibility audit",
        "",
        "Safety: local/read-only hedge audit; no private keys, order signing, order submission, or cancellation.",
        "",
        "| Item | Value |",
        "|---|---:|",
        f"| Status | `{result['status']}` |",
        f"| Monthly dollar amounts are profit/reward, not capital | {result['interpretation']['monthly_amounts_are_profit_not_capital']} |",
        f"| Unhedged one-sided loss | {_money(m.get('unhedged_one_side_loss_usdc'))} |",
        f"| Configured-cap loss | {_money(m.get('configured_cap_loss_usdc'))} ({_pct(m.get('configured_cap_loss_fraction'))}) |",
        f"| Tail-loss reduction from caps | {_pct(m.get('configured_cap_loss_reduction_fraction'))} |",
        f"| Configured-cap recovery | {_num(m.get('configured_cap_recovery_days'))} days |",
        f"| Max pair cost / min pair edge | {_num(m.get('max_pair_cost_per_share'), 4)} / {_money(m.get('min_pair_lock_edge_usdc'))} |",
        f"| Total latest pair-lock edge | {_money(m.get('pair_lock_edge_total_usdc'))} |",
        f"| Pair slippage cushion / exit slippage assumption | {_num(m.get('pair_slippage_cushion_per_share'), 4)} / {_num(m.get('exit_slippage_assumption'), 4)} |",
        "",
        "| Gate | Passed |",
        "|---|:---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in gates.items())
    blockers = result.get("blockers", [])
    if isinstance(blockers, list):
        lines.extend(["", "## Blockers / caveats", ""])
        lines.extend(f"- {x}" for x in blockers)
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- The feasible hedge is partial/internal: pair YES and NO fills below $1 total cost, enforce inventory caps, and exit only inside the pair-edge cushion.",
            "- A perfect external hedge is not assumed for non-sports/non-crypto event markets.",
            "- Full deployment proof still requires real order/fill/cancel and paid-reward telemetry.",
            "",
        ]
    )
    return "\n".join(lines)


def _money(value: object) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"${x:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"{100 * x:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: object, digits: int = 2) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"{x:.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


if __name__ == "__main__":
    main()
