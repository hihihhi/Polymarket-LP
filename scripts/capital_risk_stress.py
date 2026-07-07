#!/usr/bin/env python3
"""Audit LP quote intents for capital survival and recovery time.

This script is local/read-only. It reads paper quote CSV plus optional target
status/manifest JSON, then writes JSON/Markdown stress reports. It never signs,
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

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.capital_risk import (  # noqa: E402
    CapitalRiskStressConfig,
    config_from_lp_manifest,
    evaluate_capital_risk_stress,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--quotes", required=True)
    p.add_argument("--target-status", default="")
    p.add_argument("--manifest", default="")
    p.add_argument("--out", default="")
    p.add_argument("--markdown-out", default="")
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.20)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.80)
    p.add_argument("--max-capped-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-capped-recovery-days", type=float, default=10.0)
    p.add_argument("--max-unpaired-per-market", type=float, default=60.0)
    p.add_argument("--max-total-unpaired", type=float, default=450.0)
    p.add_argument("--max-cluster-unpaired", type=float, default=250.0)
    p.add_argument("--exit-slippage", type=float, default=0.005)
    p.add_argument("--days-per-month", type=float, default=30.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = CapitalRiskStressConfig(
        initial_capital=args.initial_capital,
        min_cash_reserve_fraction=args.min_cash_reserve_fraction,
        max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
        max_capped_loss_fraction=args.max_capped_loss_fraction,
        max_capped_recovery_days=args.max_capped_recovery_days,
        max_unpaired_per_market=args.max_unpaired_per_market,
        max_total_unpaired=args.max_total_unpaired,
        max_cluster_unpaired=args.max_cluster_unpaired,
        exit_slippage=args.exit_slippage,
        days_per_month=args.days_per_month,
    )
    manifest = _load_json(args.manifest) if args.manifest else None
    cfg = config_from_lp_manifest(manifest, cfg)
    target = _load_json(args.target_status) if args.target_status else None
    quotes = pd.read_csv(args.quotes)
    result = evaluate_capital_risk_stress(quotes, target_status=target, cfg=cfg)
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
        "# LP capital risk stress",
        "",
        "Safety: local/read-only capital stress audit; no private keys, order signing, order submission, or cancellation.",
        "",
        "| Item | Value |",
        "|---|---:|",
        f"| Status | `{result['status']}` |",
        f"| Latest timestamp | {m.get('latest_timestamp', 'n/a')} |",
        f"| Latest quote rows / markets | {m.get('latest_quote_rows')} / {m.get('latest_markets')} |",
        f"| Active pair notional | {_money(m.get('active_pair_notional'))} |",
        f"| Cash reserve | {_money(m.get('cash_reserve_usdc'))} ({_pct(m.get('cash_reserve_fraction'))}) |",
        f"| Single-market one-side loss to zero | {_money(m.get('single_market_worst_one_side_loss_to_zero'))} |",
        f"| All-active unhedged one-side loss to zero | {_money(m.get('all_active_unhedged_one_side_loss_to_zero'))} ({_pct(m.get('unhedged_loss_fraction_of_capital'))}) |",
        f"| Configured inventory-cap loss to zero | {_money(m.get('configured_inventory_cap_loss_to_zero'))} ({_pct(m.get('configured_inventory_cap_loss_fraction'))}) |",
        f"| Immediate-exit slippage loss if caps reject | {_money(m.get('immediate_exit_slippage_loss_if_caps_reject'))} |",
        f"| 50% capture p05 monthly income | {_money(m.get('captured_p05_monthly_income_usdc'))} |",
        f"| Recovery days: unhedged / configured-cap | {_num(m.get('unhedged_recovery_days_at_p05_income'))} / {_num(m.get('capped_recovery_days_at_p05_income'))} |",
        f"| Max pair cost / min locked pair edge | {_num(m.get('max_pair_cost_per_share'), 4)} / {_money(m.get('min_locked_pair_edge_usdc'))} |",
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
