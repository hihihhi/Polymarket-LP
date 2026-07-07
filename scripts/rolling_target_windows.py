#!/usr/bin/env python3
"""Evaluate LP monthly target persistence over rolling public-paper windows.

This is a research auditor only. It reads public snapshot/quote CSVs, slices
them into rolling point-in-time windows, recomputes paper diagnostics, and
reports whether each window clears configurable income/risk gates. It never
signs, submits, cancels, or inspects orders.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lp_backtest import load_snapshots
from polymarket_lp.paper import PaperAnalysisConfig, analyze_paper_quotes
from polymarket_lp.target import TargetMonitorConfig, target_monitor_from_summary
from scripts.update_target_status import _bootstrap_target_from_quotes, _json_safe


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--quotes", required=True)
    p.add_argument("--out-dir", default="data/processed/rolling_target_windows")
    p.add_argument("--downloads-copy", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--window-hours", type=float, default=6.0)
    p.add_argument("--step-hours", type=float, default=1.0)
    p.add_argument("--min-window-hours", type=float, default=6.0)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--max-active-pair-notional", type=float, default=1600.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.20)
    p.add_argument("--max-fill-proxy-rate", type=float, default=0.0)
    p.add_argument("--max-stale-fill-rate", type=float, default=0.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--max-abs-mid-change-to-next", type=float, default=float("inf"))
    p.add_argument("--required-capture-rate", type=float, default=0.5)
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--max-stale-seconds", type=float, default=900.0)
    p.add_argument("--stale-mid-change", type=float, default=0.03)
    p.add_argument("--fill-mid-cross-buffer", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    p.add_argument("--bootstrap-resamples", type=int, default=2000)
    p.add_argument("--bootstrap-seed", type=int, default=77)
    p.add_argument("--bootstrap-block-size", type=int, default=2)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snapshots = _with_ts(load_snapshots(args.snapshots))
    quotes = _with_ts(pd.read_csv(args.quotes))
    rows: list[dict[str, Any]] = []
    if not snapshots.empty:
        start = snapshots["timestamp"].min()
        end = snapshots["timestamp"].max()
        for idx, (win_start, win_end) in enumerate(
            _time_windows(start, end, window_hours=args.window_hours, step_hours=args.step_hours), start=1
        ):
            rows.append(_evaluate_window(idx, win_start, win_end, snapshots, quotes, args))

    frame = pd.DataFrame(rows)
    csv_path = out / "rolling_windows.csv"
    frame.to_csv(csv_path, index=False)
    summary = _summary(frame, args)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "snapshots": args.snapshots,
        "quotes": args.quotes,
        "config": vars(args),
        "summary": summary,
        "csv": str(csv_path),
        "safety": "rolling public-paper audit only; no private keys, signing, order submission, or cancellation",
    }
    (out / "rolling_summary.json").write_text(
        json.dumps(_json_safe(payload), indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    markdown = _markdown(payload)
    md_path = out / "rolling_summary.md"
    md_path.write_text(markdown, encoding="utf-8")
    if args.downloads_copy:
        shutil.copyfile(md_path, args.downloads_copy)
    print(json.dumps({"rolling_summary": str(out / "rolling_summary.json"), "windows": len(frame)}, indent=2))


def _evaluate_window(
    idx: int,
    win_start: pd.Timestamp,
    win_end: pd.Timestamp,
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    s_win = snapshots[(snapshots["timestamp"] >= win_start) & (snapshots["timestamp"] <= win_end)].copy()
    q_win = quotes[(quotes["timestamp"] >= win_start) & (quotes["timestamp"] <= win_end)].copy()
    summary = {}
    monitor: dict[str, Any] = {"target_math": {}, "gates": {}, "status": "empty_window"}
    bootstrap: dict[str, Any] = {"enabled": True, "intervals": 0}
    if not s_win.empty and not q_win.empty:
        _, summary = analyze_paper_quotes(
            s_win,
            q_win,
            PaperAnalysisConfig(
                max_stale_seconds=args.max_stale_seconds,
                stale_mid_change=args.stale_mid_change,
                fill_mid_cross_buffer=args.fill_mid_cross_buffer,
                max_reward_gap_seconds=args.max_reward_gap_seconds,
            ),
        )
        monitor = target_monitor_from_summary(
            summary,
            TargetMonitorConfig(
                initial_capital=args.initial_capital,
                target_monthly_usdc=args.target_monthly,
                reward_to_loss_haircut=args.reward_to_loss_haircut,
                days_per_month=args.days_per_month,
                min_observation_hours=args.min_window_hours,
                max_fill_proxy_rate=args.max_fill_proxy_rate,
                max_stale_fill_rate=args.max_stale_fill_rate,
                max_pending_quote_rate=args.max_pending_quote_rate,
                min_unique_markets=args.min_unique_markets,
                max_active_pair_notional=args.max_active_pair_notional,
            ),
        )
        bootstrap = _bootstrap_target_from_quotes(
            q_win,
            cfg=TargetMonitorConfig(
                initial_capital=args.initial_capital,
                target_monthly_usdc=args.target_monthly,
                reward_to_loss_haircut=args.reward_to_loss_haircut,
                days_per_month=args.days_per_month,
            ),
            max_reward_gap_seconds=args.max_reward_gap_seconds,
            resamples=args.bootstrap_resamples,
            seed=args.bootstrap_seed + idx,
            block_size=args.bootstrap_block_size,
            capture_rate=args.required_capture_rate,
            min_target_margin=args.min_target_margin,
        )
    target_math = monitor.get("target_math", {})
    gates = monitor.get("gates", {})
    duration = _num(summary.get("duration_hours"))
    max_active = _num(summary.get("max_active_pair_notional"))
    cash_reserve_fraction = (args.initial_capital - max_active) / args.initial_capital if args.initial_capital else float("nan")
    captured_p05 = _num(bootstrap.get("captured_net_monthly_p05"))
    target_threshold = args.target_monthly * args.min_target_margin
    window_gate = bool(
        gates.get("density_gate_passed", False)
        and gates.get("capture_gate_passed", False)
        and gates.get("risk_proxy_gate_passed", False)
        and gates.get("diversification_gate_passed", False)
        and gates.get("active_notional_gate_passed", False)
        and duration >= args.min_window_hours
        and cash_reserve_fraction >= args.min_cash_reserve_fraction
        and captured_p05 >= target_threshold
        and _num(summary.get("max_abs_mid_change_to_next")) <= args.max_abs_mid_change_to_next
    )
    return {
        "window_index": idx,
        "window_start_utc": win_start.isoformat(),
        "window_end_utc": win_end.isoformat(),
        "duration_hours": duration,
        "quote_rows": int(summary.get("quote_rows", 0) or 0),
        "unique_markets_quoted": int(summary.get("unique_markets_quoted", 0) or 0),
        "max_active_pair_notional": max_active,
        "cash_reserve_fraction": cash_reserve_fraction,
        "net_monthly_after_loss_haircut": _num(target_math.get("net_monthly_after_loss_haircut")),
        "capture_needed_for_target": _num(target_math.get("capture_needed_for_target")),
        "bootstrap_intervals": int(bootstrap.get("intervals", 0) or 0),
        "captured_p05_net_monthly": captured_p05,
        "fill_proxy_rate": _num(summary.get("fill_proxy_rate"), 0.0),
        "stale_fill_rate": _num(summary.get("stale_fill_rate"), 0.0),
        "pending_quote_rate": _num(summary.get("pending_quote_rate"), 0.0),
        "raw_pending_quote_rate": _num(summary.get("raw_pending_quote_rate"), 0.0),
        "right_censored_pending_quote_rate": _num(summary.get("right_censored_pending_quote_rate"), 0.0),
        "max_abs_mid_change_to_next": _num(summary.get("max_abs_mid_change_to_next"), 0.0),
        "density_gate_passed": bool(gates.get("density_gate_passed", False)),
        "capture_gate_passed": bool(gates.get("capture_gate_passed", False)),
        "risk_proxy_gate_passed": bool(gates.get("risk_proxy_gate_passed", False)),
        "diversification_gate_passed": bool(gates.get("diversification_gate_passed", False)),
        "active_notional_gate_passed": bool(gates.get("active_notional_gate_passed", False)),
        "sample_gate_passed": bool(duration >= args.min_window_hours),
        "cash_reserve_gate_passed": bool(cash_reserve_fraction >= args.min_cash_reserve_fraction),
        "capture_stress_gate_passed": bool(captured_p05 >= target_threshold),
        "mid_move_gate_passed": bool(_num(summary.get("max_abs_mid_change_to_next")) <= args.max_abs_mid_change_to_next),
        "window_gate_passed": window_gate,
        "monitor_status": monitor.get("status", "empty_window"),
    }


def _summary(frame: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if frame.empty:
        return {
            "windows": 0,
            "window_gate_pass_rate": 0.0,
            "all_windows_passed": False,
            "safety_note": "no complete rolling windows available yet",
        }
    pass_rate = float(frame["window_gate_passed"].mean())
    return {
        "windows": int(len(frame)),
        "window_gate_pass_rate": pass_rate,
        "all_windows_passed": bool(pass_rate == 1.0),
        "min_captured_p05_net_monthly": _finite_min(frame["captured_p05_net_monthly"]),
        "median_captured_p05_net_monthly": _finite_quantile(frame["captured_p05_net_monthly"], 0.5),
        "min_net_monthly_after_loss_haircut": _finite_min(frame["net_monthly_after_loss_haircut"]),
        "max_capture_needed_for_target": _finite_max(frame["capture_needed_for_target"]),
        "max_abs_mid_change_to_next": _finite_max(frame["max_abs_mid_change_to_next"]),
        "max_pending_quote_rate": _finite_max(frame["pending_quote_rate"]),
        "target_monthly_usdc": args.target_monthly,
        "required_capture_rate": args.required_capture_rate,
        "min_window_hours": args.min_window_hours,
    }


def _markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lines = [
        "# LP rolling target-window audit",
        "",
        f"Generated: {payload['generated_utc']}. Safety: public-paper audit only.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Windows | {s.get('windows', 0)} |",
        f"| Window gate pass rate | {_pct(s.get('window_gate_pass_rate'))} |",
        f"| All windows passed | {s.get('all_windows_passed', False)} |",
        f"| Min captured p05 net monthly | {_money(s.get('min_captured_p05_net_monthly'))} |",
        f"| Median captured p05 net monthly | {_money(s.get('median_captured_p05_net_monthly'))} |",
        f"| Min net monthly after haircut | {_money(s.get('min_net_monthly_after_loss_haircut'))} |",
        f"| Max capture needed | {_pct(s.get('max_capture_needed_for_target'))} |",
        f"| Max pending quote rate | {_pct(s.get('max_pending_quote_rate'))} |",
        f"| Max next-mid move | {_num_str(s.get('max_abs_mid_change_to_next'))} |",
        "",
        f"CSV: `{payload['csv']}`",
        "",
    ]
    return "\n".join(lines)


def _time_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    window_hours: float,
    step_hours: float,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    window = pd.Timedelta(hours=max(float(window_hours), 0.0))
    step = pd.Timedelta(hours=max(float(step_hours), 0.0))
    if pd.isna(start) or pd.isna(end) or window <= pd.Timedelta(0) or step <= pd.Timedelta(0) or end < start + window:
        return []
    out: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    current = start
    while current + window <= end:
        out.append((current, current + window))
        current += step
    return out


def _with_ts(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "timestamp" not in frame:
        return pd.DataFrame()
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def _num(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _finite_min(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return float(clean.min()) if len(clean) else None


def _finite_max(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return float(clean.max()) if len(clean) else None


def _finite_quantile(values: pd.Series, q: float) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return float(clean.quantile(q)) if len(clean) else None


def _money(value: Any) -> str:
    number = _num(value)
    return f"${number:,.2f}" if np.isfinite(number) else "n/a"


def _pct(value: Any) -> str:
    number = _num(value)
    return f"{100 * number:.2f}%" if np.isfinite(number) else "n/a"


def _num_str(value: Any) -> str:
    number = _num(value)
    return f"{number:.4f}" if np.isfinite(number) else "n/a"


if __name__ == "__main__":
    main()
