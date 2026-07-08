#!/usr/bin/env python3
"""Audit LP monthly target evidence against CLOB depth/rescue requirements."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.depth_gate import DepthReadinessConfig, evaluate_depth_readiness  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-status", required=True)
    p.add_argument("--rescue-stress", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--required-capture-rate", type=float, default=0.50)
    p.add_argument("--min-observation-hours", type=float, default=6.0)
    p.add_argument("--min-quote-rows", type=int, default=12)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--min-book-scenarios", type=int, default=12)
    p.add_argument("--min-taker-rescue-feasible-rate", type=float, default=0.80)
    p.add_argument("--min-taker-rescue-pair-edge-per-share", type=float, default=0.0)
    p.add_argument("--min-taker-rescue-depth-fraction", type=float, default=1.0)
    p.add_argument("--allow-partial-taker-rescue", action="store_true")
    p.add_argument("--max-latest-taker-residual-loss-fraction", type=float, default=0.05)
    p.add_argument("--allow-non-clob-quality", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    target_status = json.loads(Path(args.target_status).read_text(encoding="utf-8-sig"))
    rescue_stress = json.loads(Path(args.rescue_stress).read_text(encoding="utf-8-sig"))
    result = evaluate_depth_readiness(
        target_status=target_status,
        rescue_stress=rescue_stress,
        cfg=DepthReadinessConfig(
            target_monthly_usdc=args.target_monthly,
            required_capture_rate=args.required_capture_rate,
            min_observation_hours=args.min_observation_hours,
            min_quote_rows=args.min_quote_rows,
            min_unique_markets=args.min_unique_markets,
            min_book_scenarios=args.min_book_scenarios,
            min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
            min_taker_rescue_pair_edge_per_share=args.min_taker_rescue_pair_edge_per_share,
            min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
            require_clob_book_quality=not args.allow_non_clob_quality,
            allow_partial_taker_rescue=args.allow_partial_taker_rescue,
            max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
        ),
    )
    payload = {
        **result,
        "source_paths": {"target_status": args.target_status, "rescue_stress": args.rescue_stress},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_json_safe(payload), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md = Path(args.markdown_out)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"status": result["status"], "blockers": result["blockers"]}, indent=2))


def _markdown(payload: dict[str, Any]) -> str:
    m = payload["metrics"]
    gates = payload["gates"]
    lines = [
        "# LP CLOB-depth readiness gate",
        "",
        "Safety: local/read-only evidence audit; no private keys, signing, order submission, or cancellation.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Status | `{payload['status']}` |",
        f"| Observation hours | {_num(m.get('duration_hours'), 2)} |",
        f"| Quote rows | {_num(m.get('quote_rows'), 0)} |",
        f"| Unique markets | {_num(m.get('unique_markets_quoted'), 0)} |",
        f"| CLOB quality rate | {_pct(m.get('clob_quality_rate'))} |",
        f"| Required-capture p05 income | {_money(m.get('income_p05_at_required_capture'))} |",
        f"| Taker rescue scenarios | {_num(m.get('taker_rescue_book_scenarios'), 0)} |",
        f"| Taker rescue feasible rate | {_pct(m.get('taker_rescue_feasible_rate'))} |",
        f"| Min taker pair edge/share | {_num(m.get('taker_rescue_min_pair_edge_per_share'), 4)} |",
        f"| Min taker depth fraction | {_num(m.get('taker_rescue_min_depth_fraction'), 2)} |",
        f"| Partial taker rescue allowed | {m.get('partial_taker_rescue_allowed')} |",
        f"| Size-weighted rescued fraction | {_pct(m.get('taker_size_weighted_rescue_fraction'))} |",
        f"| Latest residual loss | {_money(m.get('latest_taker_residual_loss_to_zero'))} ({_pct(m.get('latest_taker_residual_loss_fraction'))}) |",
        "",
        "| Gate | Passed |",
        "|---|:---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in gates.items())
    blockers = payload.get("blockers", [])
    if blockers:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {x}" for x in blockers)
    lines.append("")
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


def _num(value: object, digits: int) -> str:
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
