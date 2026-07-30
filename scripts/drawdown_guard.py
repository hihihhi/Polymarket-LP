#!/usr/bin/env python3
"""Evaluate LP public-paper candidates for MDD and inventory survivability.

This reads public snapshot CSVs from candidate background manifests and runs the
local LP simulator with each candidate's parameterized quote rules. It never
signs, submits, cancels, or inspects orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.drawdown_guard import (  # noqa: E402
    DrawdownGuardConfig,
    evaluate_drawdown_guard,
    lp_config_from_manifest,
)
from polymarket_lp.lp_backtest import load_snapshots  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="NAME=background_manifest.json",
    )
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--min-observation-hours", type=float, default=6.0)
    p.add_argument("--max-mtm-drawdown-fraction", type=float, default=0.10)
    p.add_argument("--max-realized-drawdown-fraction", type=float, default=0.05)
    p.add_argument("--max-open-inventory-fraction", type=float, default=0.50)
    p.add_argument("--max-active-order-fraction", type=float, default=0.70)
    p.add_argument("--min-reward-to-trading-loss-ratio", type=float, default=3.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = DrawdownGuardConfig(
        initial_capital=args.initial_capital,
        min_observation_hours=args.min_observation_hours,
        max_mtm_drawdown_fraction=args.max_mtm_drawdown_fraction,
        max_realized_drawdown_fraction=args.max_realized_drawdown_fraction,
        max_open_inventory_fraction=args.max_open_inventory_fraction,
        max_active_order_fraction=args.max_active_order_fraction,
        min_reward_to_trading_loss_ratio=args.min_reward_to_trading_loss_ratio,
    )
    rows = []
    for item in args.candidate:
        name, path = _split_named_path(item)
        manifest = _load_json(path)
        snapshot_path = manifest.get("snapshot")
        if not snapshot_path:
            rows.append(_pending_result(name, "manifest missing snapshot"))
            continue
        if not Path(str(snapshot_path)).exists():
            rows.append(
                _pending_result(name, f"snapshot path does not exist: {snapshot_path}")
            )
            continue
        manifest["_candidate_name"] = name
        lp_cfg = lp_config_from_manifest(manifest)
        result = evaluate_drawdown_guard(load_snapshots(snapshot_path), lp_cfg, cfg)
        result["name"] = name
        result["manifest"] = path
        result["snapshot"] = snapshot_path
        rows.append(result)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": _status(rows),
        "config": vars(args),
        "candidates": rows,
        "safety": "public snapshot drawdown guard only; no private keys, signing, order submission, or cancellation",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(_json_safe(payload), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    md = Path(args.markdown_out)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "candidates": len(rows)}, indent=2))


def _status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no_candidates"
    if all(row.get("status") == "drawdown_guard_passed" for row in rows):
        return "all_drawdown_guards_passed"
    if all(row.get("risk_core_passed") for row in rows):
        return "drawdown_guards_sample_pending"
    return "drawdown_guard_failed"


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# LP candidate drawdown guard",
        "",
        f"Generated: {payload['generated_utc']}. Safety: public-paper simulator only.",
        "",
        f"Status: `{payload['status']}`",
        "",
        "| Candidate | Status | Quote | Active cap | Rescue cap | Hours | PnL | MTM MDD | Realized MDD | Max inventory | Max active | Reward/loss | Blockers |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload.get("candidates", []):
        m = row.get("metrics", {})
        lp_cfg = row.get("lp_config", {})
        lines.append(
            "| {name} | {status} | {quote} | {active_cap} | {rescue_cap} | {hours} | {pnl} | {mdd} | {real_dd} | {inv} | {active} | {rl} | {blockers} |".format(
                name=row.get("name", ""),
                status=row.get("status", ""),
                quote=_shares(lp_cfg.get("quote_size_shares")),
                active_cap=_money(lp_cfg.get("active_capital_limit")),
                rescue_cap=_money(lp_cfg.get("partial_rescue_max_residual_loss_usdc")),
                hours=_num(m.get("duration_hours")),
                pnl=_money(m.get("total_pnl_usdc")),
                mdd=_pct(m.get("max_drawdown_mtm_fraction")),
                real_dd=_pct(m.get("max_drawdown_realized_fraction")),
                inv=_pct(m.get("max_open_inventory_fraction")),
                active=_pct(m.get("max_active_order_fraction")),
                rl=_num(m.get("reward_to_trading_loss_ratio")),
                blockers="; ".join(row.get("blockers", [])),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _split_named_path(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise SystemExit("--candidate must be NAME=path")
    name, path = value.split("=", 1)
    if not name.strip() or not path.strip():
        raise SystemExit("--candidate must be NAME=path")
    return name.strip(), path.strip()


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pending_result(name: str, message: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "drawdown_guard_data_pending",
        "risk_core_passed": False,
        "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
        "metrics": {
            "duration_hours": 0.0,
            "total_pnl_usdc": 0.0,
            "max_drawdown_mtm_fraction": math.nan,
            "max_drawdown_realized_fraction": math.nan,
            "max_open_inventory_fraction": math.nan,
            "max_active_order_fraction": math.nan,
            "reward_to_trading_loss_ratio": math.nan,
        },
        "blockers": [message],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
    return value


def _num(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{x:,.2f}" if math.isfinite(x) else "inf"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"${x:,.2f}" if math.isfinite(x) else "n/a"


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{100 * x:.2f}%" if math.isfinite(x) else "n/a"


def _shares(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{x:,.0f}" if math.isfinite(x) else "n/a"


if __name__ == "__main__":
    main()
