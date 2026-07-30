#!/usr/bin/env python3
"""Evaluate LP target persistence and rescue depth over rolling windows.

This is a read-only research auditor. It consumes public snapshot/quote CSVs,
recomputes target-income diagnostics, rescue-stress metrics, and the CLOB-depth
readiness gate per rolling window. It never signs, submits, cancels, or inspects
orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.depth_gate import (  # noqa: E402
    DepthReadinessConfig,
    evaluate_depth_readiness,
)
from polymarket_lp.lp_backtest import load_snapshots  # noqa: E402
from polymarket_lp.paper import PaperAnalysisConfig, analyze_paper_quotes  # noqa: E402
from polymarket_lp.rescue_stress import (  # noqa: E402
    RescueStressConfig,
    evaluate_rescue_stress,
)
from polymarket_lp.target import (  # noqa: E402
    TargetMonitorConfig,
    target_monitor_from_summary,
)
from scripts.update_target_status import (  # noqa: E402
    _bootstrap_target_from_quotes,
    _json_safe,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--quotes", required=True)
    p.add_argument("--out-dir", default="data/processed/rolling_depth_windows")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--window-hours", type=float, default=6.0)
    p.add_argument("--step-hours", type=float, default=1.0)
    p.add_argument("--min-window-hours", type=float, default=6.0)
    p.add_argument("--min-quote-rows", type=int, default=24)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--max-active-pair-notional", type=float, default=1200.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--required-capture-rate", type=float, default=0.5)
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--bootstrap-resamples", type=int, default=2000)
    p.add_argument("--bootstrap-seed", type=int, default=77)
    p.add_argument("--bootstrap-block-size", type=int, default=2)
    p.add_argument("--min-book-scenarios", type=int, default=24)
    p.add_argument("--min-taker-rescue-feasible-rate", type=float, default=0.80)
    p.add_argument("--min-taker-rescue-depth-fraction", type=float, default=1.0)
    p.add_argument("--taker-rescue-min-pair-edge-per-share", type=float, default=0.0)
    p.add_argument("--allow-partial-taker-rescue", action="store_true")
    p.add_argument(
        "--max-latest-taker-residual-loss-fraction", type=float, default=0.05
    )
    p.add_argument("--allow-non-clob-quality", action="store_true")
    p.add_argument("--max-stale-seconds", type=float, default=900.0)
    p.add_argument("--stale-mid-change", type=float, default=0.03)
    p.add_argument("--fill-mid-cross-buffer", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snapshots = _with_ts(load_snapshots(args.snapshots))
    quotes = _with_ts(pd.read_csv(args.quotes))
    rows: list[dict[str, Any]] = []
    if not snapshots.empty:
        for idx, (win_start, win_end) in enumerate(
            _time_windows(
                snapshots["timestamp"].min(),
                snapshots["timestamp"].max(),
                window_hours=args.window_hours,
                step_hours=args.step_hours,
            ),
            start=1,
        ):
            rows.append(
                _evaluate_window(idx, win_start, win_end, snapshots, quotes, args)
            )
    frame = pd.DataFrame(rows)
    csv_path = out / "rolling_depth_windows.csv"
    frame.to_csv(csv_path, index=False)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "snapshots": args.snapshots,
        "quotes": args.quotes,
        "config": vars(args),
        "summary": _summary(frame),
        "csv": str(csv_path),
        "safety": "rolling public-paper depth audit only; no private keys, signing, order submission, or cancellation",
    }
    (out / "rolling_depth_summary.json").write_text(
        json.dumps(_json_safe(payload), indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (out / "rolling_depth_summary.md").write_text(_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "rolling_depth_summary": str(out / "rolling_depth_summary.json"),
                "windows": len(frame),
            },
            indent=2,
        )
    )


def _evaluate_window(
    idx: int,
    win_start: pd.Timestamp,
    win_end: pd.Timestamp,
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, Any]:
    s_win = snapshots[
        (snapshots["timestamp"] >= win_start) & (snapshots["timestamp"] <= win_end)
    ].copy()
    q_win = quotes[
        (quotes["timestamp"] >= win_start) & (quotes["timestamp"] <= win_end)
    ].copy()
    target_status = _target_status_for_window(s_win, q_win, args)
    rescue = evaluate_rescue_stress(
        q_win,
        RescueStressConfig(
            initial_capital=args.initial_capital,
            require_taker_residual_loss=args.allow_partial_taker_rescue,
            max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
            require_taker_rescue_depth=not args.allow_partial_taker_rescue,
            min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
            min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
            taker_rescue_min_pair_edge_per_share=args.taker_rescue_min_pair_edge_per_share,
        ),
    )
    depth = evaluate_depth_readiness(
        target_status=target_status,
        rescue_stress=rescue,
        cfg=DepthReadinessConfig(
            target_monthly_usdc=args.target_monthly * args.min_target_margin,
            required_capture_rate=args.required_capture_rate,
            min_observation_hours=args.min_window_hours,
            min_quote_rows=args.min_quote_rows,
            min_unique_markets=args.min_unique_markets,
            min_book_scenarios=args.min_book_scenarios,
            min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
            min_taker_rescue_pair_edge_per_share=args.taker_rescue_min_pair_edge_per_share,
            min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
            require_clob_book_quality=not args.allow_non_clob_quality,
            allow_partial_taker_rescue=args.allow_partial_taker_rescue,
            max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
        ),
    )
    d_metrics = depth["metrics"]
    r_metrics = rescue["metrics"]
    return {
        "window_index": idx,
        "window_start_utc": win_start.isoformat(),
        "window_end_utc": win_end.isoformat(),
        "depth_ready": bool(depth["gates"]["depth_ready"]),
        "depth_status": depth["status"],
        "blockers": "; ".join(depth.get("blockers", [])),
        "duration_hours": d_metrics.get("duration_hours"),
        "quote_rows": d_metrics.get("quote_rows"),
        "unique_markets_quoted": d_metrics.get("unique_markets_quoted"),
        "income_p05_at_required_capture": d_metrics.get(
            "income_p05_at_required_capture"
        ),
        "clob_quality_rate": d_metrics.get("clob_quality_rate"),
        "taker_rescue_book_scenarios": d_metrics.get("taker_rescue_book_scenarios"),
        "taker_rescue_feasible_rate": d_metrics.get("taker_rescue_feasible_rate"),
        "taker_rescue_min_depth_fraction": d_metrics.get(
            "taker_rescue_min_depth_fraction"
        ),
        "taker_size_weighted_rescue_fraction": d_metrics.get(
            "taker_size_weighted_rescue_fraction"
        ),
        "latest_taker_residual_loss_to_zero": d_metrics.get(
            "latest_taker_residual_loss_to_zero"
        ),
        "latest_taker_residual_loss_fraction": d_metrics.get(
            "latest_taker_residual_loss_fraction"
        ),
        "rescue_status": rescue["status"],
        "price_feasible_rate": r_metrics.get("price_feasible_rate"),
        "loss_weighted_price_feasible_rate": r_metrics.get(
            "loss_weighted_price_feasible_rate"
        ),
    }


def _target_status_for_window(
    snapshots: pd.DataFrame, quotes: pd.DataFrame, args: argparse.Namespace
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "duration_hours": 0.0,
        "quote_rows": int(len(quotes)),
        "unique_markets_quoted": int(quotes["condition_id"].nunique())
        if "condition_id" in quotes
        else 0,
        "quote_data_quality_counts": {},
    }
    if not snapshots.empty and not quotes.empty:
        _, summary = analyze_paper_quotes(
            snapshots,
            quotes,
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
            max_pending_quote_rate=args.max_pending_quote_rate,
            min_unique_markets=args.min_unique_markets,
            max_active_pair_notional=args.max_active_pair_notional,
        ),
    )
    bootstrap = _bootstrap_target_from_quotes(
        quotes,
        cfg=TargetMonitorConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            reward_to_loss_haircut=args.reward_to_loss_haircut,
            days_per_month=args.days_per_month,
        ),
        max_reward_gap_seconds=args.max_reward_gap_seconds,
        resamples=args.bootstrap_resamples,
        seed=args.bootstrap_seed,
        block_size=args.bootstrap_block_size,
        capture_rate=args.required_capture_rate,
        min_target_margin=args.min_target_margin,
    )
    capture_stress_grid = []
    if math.isfinite(_num(bootstrap.get("captured_net_monthly_p05"))):
        capture_stress_grid.append(
            {
                "capture_rate": args.required_capture_rate,
                "captured_net_monthly_p05": bootstrap.get("captured_net_monthly_p05"),
            }
        )
    return {
        "paper_summary": summary,
        "target_monitor": monitor,
        "bootstrap_target": bootstrap,
        "capture_stress_grid": capture_stress_grid,
    }


def _summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "windows": 0,
            "depth_ready_pass_rate": 0.0,
            "all_windows_depth_ready": False,
        }
    pass_rate = float(frame["depth_ready"].mean())
    return {
        "windows": int(len(frame)),
        "depth_ready_pass_rate": pass_rate,
        "all_windows_depth_ready": bool(pass_rate == 1.0),
        "min_income_p05_at_required_capture": _finite_min(
            frame["income_p05_at_required_capture"]
        ),
        "min_taker_rescue_feasible_rate": _finite_min(
            frame["taker_rescue_feasible_rate"]
        ),
        "min_taker_size_weighted_rescue_fraction": _finite_min(
            frame["taker_size_weighted_rescue_fraction"]
        ),
        "max_latest_taker_residual_loss_fraction": _finite_max(
            frame["latest_taker_residual_loss_fraction"]
        ),
        "max_latest_taker_residual_loss_to_zero": _finite_max(
            frame["latest_taker_residual_loss_to_zero"]
        ),
        "min_clob_quality_rate": _finite_min(frame["clob_quality_rate"]),
    }


def _markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    return "\n".join(
        [
            "# LP rolling CLOB-depth / partial-rescue audit",
            "",
            f"Generated: {payload['generated_utc']}. Safety: public-paper audit only.",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Windows | {s.get('windows', 0)} |",
            f"| Depth-ready pass rate | {_pct(s.get('depth_ready_pass_rate'))} |",
            f"| All windows depth-ready | {s.get('all_windows_depth_ready', False)} |",
            f"| Min required-capture p05 income | {_money(s.get('min_income_p05_at_required_capture'))} |",
            f"| Min full taker-rescue feasible rate | {_pct(s.get('min_taker_rescue_feasible_rate'))} |",
            f"| Min size-weighted rescued fraction | {_pct(s.get('min_taker_size_weighted_rescue_fraction'))} |",
            f"| Max residual loss | {_money(s.get('max_latest_taker_residual_loss_to_zero'))} ({_pct(s.get('max_latest_taker_residual_loss_fraction'))}) |",
            f"| Min CLOB quality rate | {_pct(s.get('min_clob_quality_rate'))} |",
            "",
            f"CSV: `{payload['csv']}`",
            "",
        ]
    )


def _time_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    window_hours: float,
    step_hours: float,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    window = pd.Timedelta(hours=max(float(window_hours), 0.0))
    step = pd.Timedelta(hours=max(float(step_hours), 0.0))
    if (
        pd.isna(start)
        or pd.isna(end)
        or window <= pd.Timedelta(0)
        or step <= pd.Timedelta(0)
        or end < start + window
    ):
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
    return (
        out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    )


def _num(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _finite_min(values: pd.Series) -> float | None:
    clean = (
        pd.to_numeric(values, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    return float(clean.min()) if len(clean) else None


def _finite_max(values: pd.Series) -> float | None:
    clean = (
        pd.to_numeric(values, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    return float(clean.max()) if len(clean) else None


def _money(value: Any) -> str:
    number = _num(value)
    return f"${number:,.2f}" if np.isfinite(number) else "n/a"


def _pct(value: Any) -> str:
    number = _num(value)
    return f"{100 * number:.2f}%" if np.isfinite(number) else "n/a"


if __name__ == "__main__":
    main()
