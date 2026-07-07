#!/usr/bin/env python3
"""Sweep LP paper parameters and select a target-margin configuration.

The selector is for research/paper monitoring only. It reads public snapshots,
generates paper quote intents, applies target/capture/bootstrap/risk gates, and
writes CSV/JSON artifacts. It never signs, submits, or cancels orders.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lp_backtest import LPConfig, load_snapshots
from polymarket_lp.paper import PaperAnalysisConfig, analyze_paper_quotes, build_paper_quotes
from polymarket_lp.target import TargetMonitorConfig, target_monitor_from_summary
from scripts.update_target_status import _bootstrap_target_from_quotes


@dataclass(slots=True)
class SelectionConfig:
    target_monthly_usdc: float = 1_000.0
    reward_to_loss_haircut: float = 8.0
    bootstrap_resamples: int = 2_000
    bootstrap_seed: int = 77
    bootstrap_block_size: int = 2
    capture_rate: float = 0.5
    min_target_margin: float = 1.0
    min_pair_intervals: int = 12
    min_unique_markets: int = 1
    max_fill_proxy_rate: float = 0.0
    max_stale_fill_rate: float = 0.0
    max_pending_quote_rate: float = 1.0
    max_abs_mid_change_to_next: float = float("inf")
    max_avg_active_notional: float = 2_000.0
    min_observation_hours: float = 0.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--out-dir", default="data/processed/target_config_grid")
    p.add_argument("--selected-quotes", default="", help="Optional CSV for the selected quote intents")
    p.add_argument("--offset-grid", default="0.02,0.025,0.03,0.035")
    p.add_argument("--density-grid", default="0.0256,0.04,0.052,0.06,0.08,0.10")
    p.add_argument("--quote-size", type=float, default=800.0)
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--active-capital-limit", type=float, default=1900.0)
    p.add_argument("--excluded-categories", default="sports,crypto")
    p.add_argument("--max-recent-vol", type=float, default=0.006)
    p.add_argument("--max-recent-jump", type=float, default=0.025)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.5)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--bootstrap-resamples", type=int, default=2000)
    p.add_argument("--bootstrap-seed", type=int, default=77)
    p.add_argument("--bootstrap-block-size", type=int, default=2)
    p.add_argument("--capture-rate", type=float, default=0.5)
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--min-pair-intervals", type=int, default=12)
    p.add_argument("--min-unique-markets", type=int, default=1)
    p.add_argument("--max-fill-proxy-rate", type=float, default=0.0)
    p.add_argument("--max-stale-fill-rate", type=float, default=0.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=1.0)
    p.add_argument("--max-abs-mid-change-to-next", type=float, default=float("inf"))
    p.add_argument("--max-avg-active-notional", type=float, default=2000.0)
    p.add_argument("--min-observation-hours", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    snapshots = load_snapshots(args.snapshots)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    selector = SelectionConfig(
        target_monthly_usdc=args.target_monthly,
        reward_to_loss_haircut=args.reward_to_loss_haircut,
        bootstrap_resamples=args.bootstrap_resamples,
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_block_size=args.bootstrap_block_size,
        capture_rate=args.capture_rate,
        min_target_margin=args.min_target_margin,
        min_pair_intervals=args.min_pair_intervals,
        min_unique_markets=args.min_unique_markets,
        max_fill_proxy_rate=args.max_fill_proxy_rate,
        max_stale_fill_rate=args.max_stale_fill_rate,
        max_pending_quote_rate=args.max_pending_quote_rate,
        max_abs_mid_change_to_next=args.max_abs_mid_change_to_next,
        max_avg_active_notional=args.max_avg_active_notional,
        min_observation_hours=args.min_observation_hours,
    )
    candidates: list[dict[str, Any]] = []
    quote_by_key: dict[tuple[float, float], pd.DataFrame] = {}
    for offset, density in product(_float_grid(args.offset_grid), _float_grid(args.density_grid)):
        cfg = LPConfig(
            quote_size_shares=args.quote_size,
            quote_offset=offset,
            safety_margin=args.safety_margin,
            active_capital_limit=args.active_capital_limit,
            excluded_categories=args.excluded_categories,
            min_reward_density_per_day=density,
            max_recent_vol=args.max_recent_vol,
            max_recent_jump=args.max_recent_jump,
            vol_quote_multiplier=args.vol_quote_multiplier,
        )
        quotes = build_paper_quotes(snapshots, cfg)
        _, summary = analyze_paper_quotes(
            snapshots,
            quotes,
            PaperAnalysisConfig(max_reward_gap_seconds=args.max_reward_gap_seconds),
        )
        monitor = target_monitor_from_summary(
            summary,
            TargetMonitorConfig(
                target_monthly_usdc=args.target_monthly,
                reward_to_loss_haircut=args.reward_to_loss_haircut,
                min_observation_hours=args.min_observation_hours,
                max_fill_proxy_rate=args.max_fill_proxy_rate,
                max_stale_fill_rate=args.max_stale_fill_rate,
            ),
        )
        bootstrap = _bootstrap_target_from_quotes(
            quotes,
            cfg=TargetMonitorConfig(target_monthly_usdc=args.target_monthly, reward_to_loss_haircut=args.reward_to_loss_haircut),
            max_reward_gap_seconds=args.max_reward_gap_seconds,
            resamples=args.bootstrap_resamples,
            seed=args.bootstrap_seed,
            block_size=args.bootstrap_block_size,
            capture_rate=args.capture_rate,
            min_target_margin=args.min_target_margin,
        )
        row = _candidate_row(offset, density, summary, monitor, bootstrap, selector)
        candidates.append(row)
        quote_by_key[(offset, density)] = quotes

    frame = pd.DataFrame(candidates).sort_values(
        ["selected_gate_passed", "captured_net_monthly_p05", "avg_active_pair_notional"],
        ascending=[False, False, True],
    )
    frame.to_csv(out / "target_config_grid.csv", index=False)
    selected = frame.iloc[0].to_dict() if len(frame) else {}
    selection_status = (
        "selected_config_passed"
        if selected and bool(selected.get("selected_gate_passed"))
        else "no_config_passed"
        if selected
        else "no_candidates"
    )
    payload = {
        "selection_status": selection_status,
        "selector_config": asdict(selector),
        "lp_grid": {
            "offset_grid": _float_grid(args.offset_grid),
            "density_grid": _float_grid(args.density_grid),
            "quote_size": args.quote_size,
            "safety_margin": args.safety_margin,
            "active_capital_limit": args.active_capital_limit,
            "excluded_categories": args.excluded_categories,
            "max_recent_vol": args.max_recent_vol,
            "max_recent_jump": args.max_recent_jump,
            "vol_quote_multiplier": args.vol_quote_multiplier,
        },
        "selected": selected,
        "csv": str(out / "target_config_grid.csv"),
        "safety": "paper research only; no private keys, order signing, order submission, or cancellation",
    }
    (out / "target_config_selection.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    if selected and bool(selected.get("selected_gate_passed")):
        selected_quotes = Path(args.selected_quotes) if args.selected_quotes else out / "selected_quotes.csv"
        selected_quotes.parent.mkdir(parents=True, exist_ok=True)
        quote_by_key[(float(selected["quote_offset"]), float(selected["min_reward_density_per_day"]))].to_csv(selected_quotes, index=False)
        payload["selected_quotes"] = str(selected_quotes)
        (out / "target_config_selection.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"selection_json": str(out / "target_config_selection.json"), "selected": selected}, indent=2, default=str))


def _candidate_row(
    offset: float,
    density: float,
    summary: dict[str, Any],
    monitor: dict[str, Any],
    bootstrap: dict[str, Any],
    selector: SelectionConfig,
) -> dict[str, Any]:
    captured_p05 = _to_float(bootstrap.get("captured_net_monthly_p05"))
    selected_gate = bool(
        captured_p05 >= selector.target_monthly_usdc * selector.min_target_margin
        and _to_float(summary.get("fill_proxy_rate")) <= selector.max_fill_proxy_rate
        and _to_float(summary.get("stale_fill_rate")) <= selector.max_stale_fill_rate
        and _to_float(summary.get("pending_quote_rate")) <= selector.max_pending_quote_rate
        and _to_float(summary.get("max_abs_mid_change_to_next")) <= selector.max_abs_mid_change_to_next
        and int(summary.get("quote_pair_intervals", 0)) >= selector.min_pair_intervals
        and int(summary.get("unique_markets_quoted", 0)) >= selector.min_unique_markets
        and _to_float(summary.get("avg_active_pair_notional")) <= selector.max_avg_active_notional
        and _to_float(summary.get("duration_hours")) >= selector.min_observation_hours
    )
    return {
        "quote_offset": offset,
        "min_reward_density_per_day": density,
        "quote_rows": int(summary.get("quote_rows", 0)),
        "quote_pair_intervals": int(summary.get("quote_pair_intervals", 0)),
        "unique_markets_quoted": int(summary.get("unique_markets_quoted", 0)),
        "duration_hours": _to_float(summary.get("duration_hours")),
        "avg_active_pair_notional": _to_float(summary.get("avg_active_pair_notional")),
        "avg_reward_density_per_day": _to_float(summary.get("avg_reward_density_per_day")),
        "estimated_reward_accrual_usdc": _to_float(summary.get("estimated_reward_accrual_usdc")),
        "fill_proxy_rate": _to_float(summary.get("fill_proxy_rate")),
        "stale_fill_rate": _to_float(summary.get("stale_fill_rate")),
        "pending_quote_rate": _to_float(summary.get("pending_quote_rate")),
        "max_abs_mid_change_to_next": _to_float(summary.get("max_abs_mid_change_to_next")),
        "net_monthly_after_loss_haircut": _to_float(monitor.get("target_math", {}).get("net_monthly_after_loss_haircut")),
        "capture_needed_for_target": _to_float(monitor.get("target_math", {}).get("capture_needed_for_target")),
        "bootstrap_intervals": int(bootstrap.get("intervals", 0) or 0),
        "net_monthly_p05": _to_float(bootstrap.get("net_monthly_p05")),
        "captured_net_monthly_p05": captured_p05,
        "captured_p05_target_gate_passed": bool(bootstrap.get("captured_p05_target_gate_passed", False)),
        "selected_gate_passed": selected_gate,
    }


def _float_grid(text: str) -> list[float]:
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


if __name__ == "__main__":
    main()
