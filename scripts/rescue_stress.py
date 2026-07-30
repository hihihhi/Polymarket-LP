#!/usr/bin/env python3
"""Audit LP one-sided fill rescue economics from paper quote intents.

Read-only: this script consumes quote CSVs and writes JSON/Markdown stress
artifacts. It never signs, submits, cancels, or inspects live orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.rescue_stress import (  # noqa: E402
    RescueStressConfig,
    evaluate_rescue_stress,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--quotes", required=True)
    p.add_argument("--out", default="")
    p.add_argument("--markdown-out", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--rescue-min-pair-edge-per-share", type=float, default=0.01)
    p.add_argument("--rescue-quote-offset", type=float, default=0.005)
    p.add_argument("--min-rescue-bid", type=float, default=0.001)
    p.add_argument("--exit-slippage", type=float, default=0.005)
    p.add_argument("--min-price-feasible-rate", type=float, default=0.95)
    p.add_argument("--max-latest-blocked-loss-fraction", type=float, default=0.05)
    p.add_argument("--max-immediate-exit-loss-fraction", type=float, default=0.02)
    p.add_argument("--require-taker-rescue-depth", action="store_true")
    p.add_argument("--taker-rescue-min-pair-edge-per-share", type=float, default=0.0)
    p.add_argument("--min-taker-rescue-depth-fraction", type=float, default=1.0)
    p.add_argument("--min-taker-rescue-feasible-rate", type=float, default=0.80)
    p.add_argument("--require-taker-residual-loss", action="store_true")
    p.add_argument(
        "--max-latest-taker-residual-loss-fraction", type=float, default=0.05
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    quotes = pd.read_csv(args.quotes)
    cfg = RescueStressConfig(
        initial_capital=args.initial_capital,
        rescue_min_pair_edge_per_share=args.rescue_min_pair_edge_per_share,
        rescue_quote_offset=args.rescue_quote_offset,
        min_rescue_bid=args.min_rescue_bid,
        exit_slippage=args.exit_slippage,
        min_price_feasible_rate=args.min_price_feasible_rate,
        max_latest_blocked_loss_fraction=args.max_latest_blocked_loss_fraction,
        max_immediate_exit_loss_fraction=args.max_immediate_exit_loss_fraction,
        require_taker_rescue_depth=args.require_taker_rescue_depth,
        taker_rescue_min_pair_edge_per_share=args.taker_rescue_min_pair_edge_per_share,
        min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
        min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
        require_taker_residual_loss=args.require_taker_residual_loss,
        max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
    )
    result = evaluate_rescue_stress(quotes, cfg)
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
    print(
        json.dumps(
            {"status": result["status"], "blockers": result["blockers"]}, indent=2
        )
    )


def _markdown(result: dict[str, Any]) -> str:
    m = result["metrics"]
    gates = result["gates"]
    lines = [
        "# LP rescue stress audit",
        "",
        "Safety: local/read-only quote-intent audit; no private keys, signing, order submission, or cancellation.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Status | `{result['status']}` |",
        f"| Scenarios | {_num(m.get('scenario_count'), 0)} |",
        f"| Price-feasible rescue rate | {_pct(m.get('price_feasible_rate'))} |",
        f"| Loss-weighted feasible rate | {_pct(m.get('loss_weighted_price_feasible_rate'))} |",
        f"| p05 rescued pair edge/share | {_num(m.get('p05_pair_edge_per_share_if_rescued'), 4)} |",
        f"| Latest worst one-sided loss | {_money(m.get('latest_worst_one_sided_loss_to_zero'))} |",
        f"| Latest rescue-blocked loss | {_money(m.get('latest_blocked_loss_to_zero'))} ({_pct(m.get('latest_blocked_loss_fraction'))}) |",
        f"| Latest immediate exit slippage if rescue fails | {_money(m.get('latest_immediate_exit_loss_if_rescue_fails'))} ({_pct(m.get('latest_immediate_exit_loss_fraction'))}) |",
        f"| Taker rescue book scenarios | {_num(m.get('taker_rescue_book_scenarios'), 0)} |",
        f"| Taker rescue feasible rate | {_pct(m.get('taker_rescue_feasible_rate'))} |",
        f"| Taker rescue min pair edge/share | {_num(m.get('taker_rescue_min_pair_edge_per_share'), 4)} |",
        f"| Taker rescue min depth fraction | {_num(m.get('taker_rescue_min_depth_fraction'), 2)} |",
        f"| Partial taker-rescue feasible rate | {_pct(m.get('taker_partial_rescue_feasible_rate'))} |",
        f"| Size-weighted rescued fraction | {_pct(m.get('taker_size_weighted_rescue_fraction'))} |",
        f"| Loss-weighted rescued fraction | {_pct(m.get('taker_loss_weighted_rescue_fraction'))} |",
        f"| p05 rescued size fraction | {_pct(m.get('taker_rescued_size_fraction_p05'))} |",
        f"| Latest partial-rescue residual loss | {_money(m.get('latest_taker_residual_loss_to_zero'))} ({_pct(m.get('latest_taker_residual_loss_fraction'))}) |",
        f"| Latest residual immediate-exit loss | {_money(m.get('latest_taker_residual_immediate_exit_loss'))} ({_pct(m.get('latest_taker_residual_immediate_exit_loss_fraction'))}) |",
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
            "- This proves rescue prices are economically feasible under the configured pair-edge rule, not that fills are executable.",
            "- Deployment still requires real order/fill/cancel telemetry, queue/fill probability, and paid reward reconciliation.",
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
