#!/usr/bin/env python3
"""Select a risk-managed LP allocation from a quote-size frontier CSV."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.allocation import AllocationSelectionConfig, select_allocation


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--frontier-csv", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--downloads-json-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.40)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.50)
    p.add_argument("--max-configured-cap-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--max-abs-mid-change-to-next", type=float, default=0.011)
    p.add_argument(
        "--objective", choices=["balanced", "sustainable", "income"], default="balanced"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rows = _read_csv(args.frontier_csv)
    result = select_allocation(
        rows,
        AllocationSelectionConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            min_target_margin=args.min_target_margin,
            min_unique_markets=args.min_unique_markets,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
            max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
            max_configured_cap_loss_fraction=args.max_configured_cap_loss_fraction,
            max_configured_cap_recovery_days=args.max_configured_cap_recovery_days,
            max_abs_mid_change_to_next=args.max_abs_mid_change_to_next,
            objective=args.objective,
        ),
    )
    payload = {
        **result,
        "source_frontier_csv": args.frontier_csv,
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
            {"status": result["status"], "selected": _qsize(result.get("selected"))},
            indent=2,
        )
    )


def _read_csv(path: str) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _markdown(payload: dict[str, Any]) -> str:
    selected = payload.get("selected") or {}
    income_max = payload.get("income_max") or {}
    lines = [
        "# LP allocation selector",
        "",
        "Safety: selector/report only; no private keys, signing, order submission, or cancellation.",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Config",
        "",
    ]
    cfg = payload["config"]
    lines.extend(
        [
            f"- Objective: {cfg['objective']}; target: {_money(cfg['target_monthly_usdc'])}/month on {_money(cfg['initial_capital'])}.",
            f"- Gates: min markets {cfg['min_unique_markets']}, min cash reserve {_pct(cfg['min_cash_reserve_fraction'])}, max unhedged loss {_pct(cfg['max_unhedged_loss_fraction'])}, max cap recovery {cfg['max_configured_cap_recovery_days']:.2f} days.",
        ]
    )
    lines += ["", "## Selected allocation", ""]
    lines.extend(_allocation_lines(selected, "No allocation passed."))
    lines += ["", "## Income-max passing comparison", ""]
    lines.extend(_allocation_lines(income_max, "No income-max candidate available."))
    if payload.get("blockers"):
        lines += ["", "## Blockers", ""]
        lines.extend(f"- {x}" for x in payload["blockers"])
    lines += ["", f"Source frontier: `{payload['source_frontier_csv']}`", ""]
    return "\n".join(lines)


def _allocation_lines(row: dict[str, Any], empty: str) -> list[str]:
    if not row:
        return [f"- {empty}"]
    m = row["metrics"]
    return [
        f"- qsize {m['qsize']}, quote_offset {m['quote_offset']:.3f}, density/day {_pct(m['min_reward_density_per_day'])}, markets {m['unique_markets_quoted']}.",
        f"- 50% capture p05 {_money(m['captured_net_monthly_p05'])}; net monthly after haircut {_money(m['net_monthly_after_loss_haircut'])}; capture needed {_pct(m['capture_needed_for_target'])}.",
        f"- Cash reserve {_pct(m['cash_reserve_fraction'])}; unhedged one-side loss {_money(m['unhedged_loss_to_zero'])} ({_pct(m['unhedged_loss_fraction'])}); recovery {m['unhedged_recovery_days']:.2f} days.",
        f"- Configured-cap loss {_money(m['configured_cap_loss'])} ({_pct(m['configured_cap_loss_fraction'])}); cap recovery {m['configured_cap_recovery_days']:.2f} days; max next-mid {m['max_abs_mid_change_to_next']:.4f}.",
    ]


def _qsize(row: Any) -> Any:
    return row.get("metrics", {}).get("qsize") if isinstance(row, dict) else None


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


if __name__ == "__main__":
    main()
