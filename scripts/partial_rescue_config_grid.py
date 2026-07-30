#!/usr/bin/env python3
"""Sweep LP quote size/residual caps under target-income + rescue-depth gates.

This is a public-paper research selector. It consumes point-in-time public
snapshots, generates quote intents for configurable LP parameters, then applies
the same monthly target, bootstrap capture, CLOB depth, and partial-rescue
residual-loss gates used by the live-paper watcher. It never signs, submits,
cancels, or inspects orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.capital_risk import (  # noqa: E402
    CapitalRiskStressConfig,
    evaluate_capital_risk_stress,
)
from polymarket_lp.depth_gate import (  # noqa: E402
    DepthReadinessConfig,
    evaluate_depth_readiness,
)
from polymarket_lp.drawdown_guard import (  # noqa: E402
    DrawdownGuardConfig,
    evaluate_drawdown_guard,
)
from polymarket_lp.lp_backtest import LPConfig, load_snapshots  # noqa: E402
from polymarket_lp.paper import (  # noqa: E402
    PaperAnalysisConfig,
    analyze_paper_quotes,
    build_paper_quotes,
)
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
    _capture_stress_grid,
)


@dataclass(slots=True)
class GridSelectorConfig:
    target_monthly_usdc: float = 1_000.0
    reward_to_loss_haircut: float = 8.0
    capture_rate: float = 0.5
    min_target_margin: float = 1.0
    min_observation_hours: float = 0.0
    min_quote_rows: int = 12
    min_unique_markets: int = 2
    min_book_scenarios: int = 12
    max_active_pair_notional: float = 1_200.0
    max_pending_quote_rate: float = 0.05
    min_taker_rescue_feasible_rate: float = 0.80
    min_taker_rescue_depth_fraction: float = 1.0
    max_latest_taker_residual_loss_fraction: float = 0.05
    max_capture_needed_after_cap_loss: float = 0.40


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--out-dir", default="data/processed/partial_rescue_config_grid")
    p.add_argument("--selected-quotes", default="")
    p.add_argument("--quote-size-grid", default="250,300,325,350,375,400")
    p.add_argument("--residual-loss-grid", default="25,50,75")
    p.add_argument("--offset-grid", default="0.02")
    p.add_argument("--density-grid", default="0.08,0.10,0.12")
    p.add_argument("--safety-margin", type=float, default=0.015)
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--active-capital-limit", type=float, default=1200.0)
    p.add_argument(
        "--max-unpaired-per-market-grid",
        default=str(LPConfig().max_unpaired_per_market),
    )
    p.add_argument(
        "--max-total-unpaired-grid", default=str(LPConfig().max_total_unpaired)
    )
    p.add_argument(
        "--max-cluster-unpaired-grid", default=str(LPConfig().max_cluster_unpaired)
    )
    p.add_argument(
        "--max-unpaired-minutes-grid", default=str(LPConfig().max_unpaired_minutes)
    )
    p.add_argument(
        "--depth-cap-quote-size-grid",
        default="0",
        help="Comma grid of 0/1; 1 caps quotes by displayed opposite-side ask depth before reward scoring",
    )
    p.add_argument(
        "--depth-quote-size-fraction-grid",
        default="1.0",
        help="Comma grid for displayed-depth fraction when depth cap is enabled",
    )
    p.add_argument("--min-depth-capped-quote-size", type=float, default=1.0)
    p.add_argument("--excluded-categories", default="sports,crypto")
    p.add_argument("--max-recent-vol", type=float, default=0.006)
    p.add_argument("--max-recent-jump", type=float, default=0.025)
    p.add_argument("--vol-quote-multiplier", type=float, default=0.5)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--capture-rate", type=float, default=0.5)
    p.add_argument("--capture-rates", default="0.25,0.35,0.4,0.5,0.75,1.0")
    p.add_argument("--min-target-margin", type=float, default=1.0)
    p.add_argument("--min-observation-hours", type=float, default=0.0)
    p.add_argument("--min-quote-rows", type=int, default=12)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--min-book-scenarios", type=int, default=12)
    p.add_argument("--max-active-pair-notional", type=float, default=1200.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--bootstrap-resamples", type=int, default=1000)
    p.add_argument("--bootstrap-seed", type=int, default=101)
    p.add_argument("--bootstrap-block-size", type=int, default=2)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    p.add_argument("--min-taker-rescue-feasible-rate", type=float, default=0.80)
    p.add_argument("--min-taker-rescue-depth-fraction", type=float, default=1.0)
    p.add_argument("--taker-rescue-min-pair-edge-per-share", type=float, default=0.0)
    p.add_argument(
        "--max-latest-taker-residual-loss-fraction", type=float, default=0.05
    )
    p.add_argument("--max-mtm-drawdown-fraction", type=float, default=0.10)
    p.add_argument("--max-realized-drawdown-fraction", type=float, default=0.05)
    p.add_argument("--max-open-inventory-fraction", type=float, default=0.50)
    p.add_argument("--max-active-order-fraction", type=float, default=0.70)
    p.add_argument("--min-reward-to-trading-loss-ratio", type=float, default=3.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.20)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.80)
    p.add_argument("--max-capped-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-capped-recovery-days", type=float, default=10.0)
    p.add_argument("--max-capture-needed-after-cap-loss", type=float, default=0.40)
    p.add_argument("--min-latest-markets", type=int, default=2)
    p.add_argument("--max-single-market-active-fraction", type=float, default=0.35)
    p.add_argument("--max-single-cluster-active-fraction", type=float, default=0.70)
    p.add_argument(
        "--max-single-market-unhedged-loss-fraction", type=float, default=0.20
    )
    p.add_argument(
        "--max-single-cluster-unhedged-loss-fraction", type=float, default=0.50
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=0,
        help="Write checkpoint artifacts every N configs; 0 disables.",
    )
    p.add_argument(
        "--progress-json",
        default="",
        help="Optional progress JSON path; defaults under --out-dir when progress is enabled.",
    )
    p.add_argument(
        "--progress-csv",
        default="",
        help="Optional progress CSV path; defaults under --out-dir when progress is enabled.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    snapshots = load_snapshots(args.snapshots)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    selector = GridSelectorConfig(
        target_monthly_usdc=args.target_monthly,
        reward_to_loss_haircut=args.reward_to_loss_haircut,
        capture_rate=args.capture_rate,
        min_target_margin=args.min_target_margin,
        min_observation_hours=args.min_observation_hours,
        min_quote_rows=args.min_quote_rows,
        min_unique_markets=args.min_unique_markets,
        min_book_scenarios=args.min_book_scenarios,
        max_active_pair_notional=args.max_active_pair_notional,
        max_pending_quote_rate=args.max_pending_quote_rate,
        min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
        min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
        max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
        max_capture_needed_after_cap_loss=args.max_capture_needed_after_cap_loss,
    )

    grid_values = list(
        product(
            _float_grid(args.quote_size_grid),
            _float_grid(args.residual_loss_grid),
            _float_grid(args.offset_grid),
            _float_grid(args.density_grid),
            _float_grid(args.max_unpaired_per_market_grid),
            _float_grid(args.max_total_unpaired_grid),
            _float_grid(args.max_cluster_unpaired_grid),
            _float_grid(args.max_unpaired_minutes_grid),
            _bool_grid(args.depth_cap_quote_size_grid),
            _float_grid(args.depth_quote_size_fraction_grid),
        )
    )
    started = time.monotonic()
    candidates: list[dict[str, Any]] = []
    total_configs = len(grid_values)
    for idx, (
        quote_size,
        residual_cap,
        offset,
        density,
        max_unpaired_market,
        max_total_unpaired,
        max_cluster_unpaired,
        max_unpaired_minutes,
        depth_cap_quote_size,
        depth_quote_size_fraction,
    ) in enumerate(grid_values, start=1):
        cfg = LPConfig(
            quote_size_shares=quote_size,
            quote_offset=offset,
            safety_margin=args.safety_margin,
            active_capital_limit=args.active_capital_limit,
            max_unpaired_per_market=max_unpaired_market,
            max_total_unpaired=max_total_unpaired,
            max_cluster_unpaired=max_cluster_unpaired,
            max_unpaired_minutes=max_unpaired_minutes,
            excluded_categories=args.excluded_categories,
            min_reward_density_per_day=density,
            max_recent_vol=args.max_recent_vol,
            max_recent_jump=args.max_recent_jump,
            vol_quote_multiplier=args.vol_quote_multiplier,
            depth_cap_quote_size=depth_cap_quote_size,
            depth_quote_size_fraction=depth_quote_size_fraction,
            min_depth_capped_quote_size_shares=args.min_depth_capped_quote_size,
            partial_rescue_max_residual_loss_usdc=residual_cap,
        )
        quotes = build_paper_quotes(snapshots, cfg)
        target_status = _target_status_from_quotes(
            snapshots=snapshots,
            quotes=quotes,
            args=args,
            selector=selector,
        )
        rescue = evaluate_rescue_stress(
            quotes,
            RescueStressConfig(
                initial_capital=args.initial_capital,
                require_taker_residual_loss=True,
                taker_rescue_min_pair_edge_per_share=args.taker_rescue_min_pair_edge_per_share,
                min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
                min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
                max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
            ),
        )
        drawdown = evaluate_drawdown_guard(
            snapshots,
            cfg,
            DrawdownGuardConfig(
                initial_capital=args.initial_capital,
                min_observation_hours=args.min_observation_hours,
                max_mtm_drawdown_fraction=args.max_mtm_drawdown_fraction,
                max_realized_drawdown_fraction=args.max_realized_drawdown_fraction,
                max_open_inventory_fraction=args.max_open_inventory_fraction,
                max_active_order_fraction=args.max_active_order_fraction,
                min_reward_to_trading_loss_ratio=args.min_reward_to_trading_loss_ratio,
            ),
        )
        capital = evaluate_capital_risk_stress(
            quotes,
            target_status=target_status,
            cfg=CapitalRiskStressConfig(
                initial_capital=args.initial_capital,
                min_cash_reserve_fraction=args.min_cash_reserve_fraction,
                max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
                max_capped_loss_fraction=args.max_capped_loss_fraction,
                max_capped_recovery_days=args.max_capped_recovery_days,
                max_unpaired_per_market=max_unpaired_market,
                max_total_unpaired=max_total_unpaired,
                max_cluster_unpaired=max_cluster_unpaired,
                min_latest_markets=args.min_latest_markets,
                max_single_market_active_fraction=args.max_single_market_active_fraction,
                max_single_cluster_active_fraction=args.max_single_cluster_active_fraction,
                max_single_market_unhedged_loss_fraction=args.max_single_market_unhedged_loss_fraction,
                max_single_cluster_unhedged_loss_fraction=args.max_single_cluster_unhedged_loss_fraction,
                target_monthly_usdc=args.target_monthly,
                reference_capture_rate=args.capture_rate,
                max_capture_needed_after_cap_loss=args.max_capture_needed_after_cap_loss,
                days_per_month=args.days_per_month,
            ),
        )
        depth = evaluate_depth_readiness(
            target_status=target_status,
            rescue_stress=rescue,
            cfg=DepthReadinessConfig(
                target_monthly_usdc=args.target_monthly,
                required_capture_rate=args.capture_rate,
                min_observation_hours=args.min_observation_hours,
                min_quote_rows=args.min_quote_rows,
                min_unique_markets=args.min_unique_markets,
                min_book_scenarios=args.min_book_scenarios,
                min_taker_rescue_feasible_rate=args.min_taker_rescue_feasible_rate,
                min_taker_rescue_pair_edge_per_share=args.taker_rescue_min_pair_edge_per_share,
                min_taker_rescue_depth_fraction=args.min_taker_rescue_depth_fraction,
                allow_partial_taker_rescue=True,
                max_latest_taker_residual_loss_fraction=args.max_latest_taker_residual_loss_fraction,
            ),
        )
        row = _candidate_row(
            quote_size=quote_size,
            residual_cap=residual_cap,
            offset=offset,
            density=density,
            max_unpaired_market=max_unpaired_market,
            max_total_unpaired=max_total_unpaired,
            max_cluster_unpaired=max_cluster_unpaired,
            max_unpaired_minutes=max_unpaired_minutes,
            depth_cap_quote_size=depth_cap_quote_size,
            depth_quote_size_fraction=depth_quote_size_fraction,
            target_status=target_status,
            rescue=rescue,
            depth=depth,
            drawdown=drawdown,
            capital=capital,
        )
        candidates.append(row)
        if args.progress_every > 0 and (
            idx == 1 or idx % args.progress_every == 0 or idx == total_configs
        ):
            _write_progress(
                candidates=candidates,
                completed=len(candidates),
                total=total_configs,
                started=started,
                out=out,
                args=args,
                final=False,
            )

    frame = _sorted_frame(candidates)
    frame.to_csv(out / "partial_rescue_config_grid.csv", index=False)
    if args.progress_every > 0:
        _write_progress(
            candidates=candidates,
            completed=len(candidates),
            total=total_configs,
            started=started,
            out=out,
            args=args,
            final=True,
        )
    selected = frame.iloc[0].to_dict() if len(frame) else {}
    payload = {
        "selection_status": _selection_status(selected),
        "selector_config": asdict(selector),
        "lp_grid": {
            "quote_size_grid": _float_grid(args.quote_size_grid),
            "residual_loss_grid": _float_grid(args.residual_loss_grid),
            "offset_grid": _float_grid(args.offset_grid),
            "density_grid": _float_grid(args.density_grid),
            "max_unpaired_per_market_grid": _float_grid(
                args.max_unpaired_per_market_grid
            ),
            "max_total_unpaired_grid": _float_grid(args.max_total_unpaired_grid),
            "max_cluster_unpaired_grid": _float_grid(args.max_cluster_unpaired_grid),
            "max_unpaired_minutes_grid": _float_grid(args.max_unpaired_minutes_grid),
            "depth_cap_quote_size_grid": _bool_grid(args.depth_cap_quote_size_grid),
            "depth_quote_size_fraction_grid": _float_grid(
                args.depth_quote_size_fraction_grid
            ),
            "min_depth_capped_quote_size": args.min_depth_capped_quote_size,
            "active_capital_limit": args.active_capital_limit,
            "excluded_categories": args.excluded_categories,
            "max_recent_vol": args.max_recent_vol,
            "max_recent_jump": args.max_recent_jump,
            "vol_quote_multiplier": args.vol_quote_multiplier,
        },
        "selected": _json_safe(selected),
        "csv": str(out / "partial_rescue_config_grid.csv"),
        "safety": "paper research only; no private keys, order signing, order submission, or cancellation",
    }
    if selected:
        selected_quotes = (
            Path(args.selected_quotes)
            if args.selected_quotes
            else out / "selected_quotes.csv"
        )
        selected_quotes.parent.mkdir(parents=True, exist_ok=True)
        build_paper_quotes(snapshots, _lp_config_from_selected(selected, args)).to_csv(
            selected_quotes, index=False
        )
        payload["selected_quotes"] = str(selected_quotes)
    (out / "partial_rescue_config_selection.json").write_text(
        json.dumps(_json_safe(payload), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selection_json": str(out / "partial_rescue_config_selection.json"),
                "selected": _json_safe(selected),
            },
            indent=2,
        )
    )


def _target_status_from_quotes(
    *,
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    args: argparse.Namespace,
    selector: GridSelectorConfig,
) -> dict[str, Any]:
    _, summary = analyze_paper_quotes(
        snapshots,
        quotes,
        PaperAnalysisConfig(max_reward_gap_seconds=args.max_reward_gap_seconds),
    )
    target_cfg = TargetMonitorConfig(
        target_monthly_usdc=args.target_monthly,
        reward_to_loss_haircut=args.reward_to_loss_haircut,
        days_per_month=args.days_per_month,
        min_observation_hours=args.min_observation_hours,
        min_unique_markets=args.min_unique_markets,
        max_active_pair_notional=args.max_active_pair_notional,
        max_pending_quote_rate=args.max_pending_quote_rate,
    )
    monitor = target_monitor_from_summary(summary, target_cfg)
    bootstrap = _bootstrap_target_from_quotes(
        quotes,
        cfg=target_cfg,
        max_reward_gap_seconds=args.max_reward_gap_seconds,
        resamples=args.bootstrap_resamples,
        seed=args.bootstrap_seed,
        block_size=args.bootstrap_block_size,
        capture_rate=args.capture_rate,
        min_target_margin=args.min_target_margin,
    )
    return {
        "paper_summary": summary,
        "target_monitor": monitor,
        "bootstrap_target": bootstrap,
        "capture_stress_grid": _capture_stress_grid(
            bootstrap,
            capture_rates=_float_grid(args.capture_rates),
            target_monthly_usdc=selector.target_monthly_usdc,
            min_target_margin=selector.min_target_margin,
        ),
    }


def _candidate_row(
    *,
    quote_size: float,
    residual_cap: float,
    offset: float,
    density: float,
    max_unpaired_market: float,
    max_total_unpaired: float,
    max_cluster_unpaired: float,
    max_unpaired_minutes: float,
    depth_cap_quote_size: bool,
    depth_quote_size_fraction: float,
    target_status: dict[str, Any],
    rescue: dict[str, Any],
    depth: dict[str, Any],
    drawdown: dict[str, Any] | None = None,
    capital: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paper = _dict(target_status.get("paper_summary"))
    bootstrap = _dict(target_status.get("bootstrap_target"))
    depth_metrics = _dict(depth.get("metrics"))
    depth_gates = _dict(depth.get("gates"))
    rescue_metrics = _dict(rescue.get("metrics"))
    drawdown = _dict(drawdown)
    drawdown_metrics = _dict(drawdown.get("metrics"))
    capital = _dict(capital)
    capital_metrics = _dict(capital.get("metrics"))
    capital_gates = _dict(capital.get("gates"))
    risk_income_gate = bool(
        depth_gates.get("income_p05_gate_passed")
        and depth_gates.get("diversification_gate_passed")
        and depth_gates.get("clob_quality_gate_passed")
        and depth_gates.get("taker_rescue_rate_gate_passed")
        and depth_gates.get("taker_depth_gate_passed")
        and depth_gates.get("taker_residual_loss_gate_passed")
    )
    capital_passed = bool(capital_gates.get("capital_risk_stress_passed", True))
    drawdown_core_passed = bool(drawdown.get("risk_core_passed", True))
    research_core_passed = bool(
        risk_income_gate and drawdown_core_passed and capital_passed
    )
    promotion_ready = bool(
        depth_gates.get("depth_ready") and drawdown_core_passed and capital_passed
    )
    strict_topbook_rescue = bool(
        _float(depth_metrics.get("taker_rescue_feasible_rate"), 0.0) >= 1.0
        and _float(depth_metrics.get("taker_rescue_min_depth_fraction"), 0.0) >= 1.0
        and _float(depth_metrics.get("latest_taker_residual_loss_fraction"), math.inf)
        <= 1e-12
    )
    return {
        "quote_size": quote_size,
        "partial_rescue_max_residual_loss_usdc": residual_cap,
        "quote_offset": offset,
        "min_reward_density_per_day": density,
        "max_unpaired_per_market": max_unpaired_market,
        "max_total_unpaired": max_total_unpaired,
        "max_cluster_unpaired": max_cluster_unpaired,
        "max_unpaired_minutes": max_unpaired_minutes,
        "depth_cap_quote_size": bool(depth_cap_quote_size),
        "depth_quote_size_fraction": depth_quote_size_fraction,
        "quote_rows": int(_float(paper.get("quote_rows"), 0.0)),
        "quote_pair_intervals": int(_float(paper.get("quote_pair_intervals"), 0.0)),
        "unique_markets_quoted": int(_float(paper.get("unique_markets_quoted"), 0.0)),
        "duration_hours": _float(paper.get("duration_hours")),
        "avg_active_pair_notional": _float(paper.get("avg_active_pair_notional")),
        "max_active_pair_notional": _float(paper.get("max_active_pair_notional")),
        "pending_quote_rate": _float(paper.get("pending_quote_rate")),
        "stale_fill_rate": _float(paper.get("stale_fill_rate")),
        "fill_proxy_rate": _float(paper.get("fill_proxy_rate")),
        "bootstrap_intervals": int(_float(bootstrap.get("intervals"), 0.0)),
        "net_monthly_p05": _float(bootstrap.get("net_monthly_p05")),
        "income_p05_at_required_capture": _float(
            depth_metrics.get("income_p05_at_required_capture")
        ),
        "taker_rescue_feasible_rate": _float(
            depth_metrics.get("taker_rescue_feasible_rate")
        ),
        "taker_rescue_min_depth_fraction": _float(
            depth_metrics.get("taker_rescue_min_depth_fraction")
        ),
        "taker_size_weighted_rescue_fraction": _float(
            depth_metrics.get("taker_size_weighted_rescue_fraction")
        ),
        "latest_taker_residual_loss_to_zero": _float(
            depth_metrics.get("latest_taker_residual_loss_to_zero")
        ),
        "latest_taker_residual_loss_fraction": _float(
            depth_metrics.get("latest_taker_residual_loss_fraction")
        ),
        "taker_rescued_size_fraction_p05": _float(
            rescue_metrics.get("taker_rescued_size_fraction_p05")
        ),
        "strict_topbook_rescue_passed": strict_topbook_rescue,
        "drawdown_status": drawdown.get("status", "not_evaluated"),
        "drawdown_core_passed": bool(drawdown.get("risk_core_passed", True)),
        "drawdown_mtm_fraction": _float(
            drawdown_metrics.get("max_drawdown_mtm_fraction")
        ),
        "drawdown_realized_fraction": _float(
            drawdown_metrics.get("max_drawdown_realized_fraction")
        ),
        "drawdown_reward_to_trading_loss_ratio": _float(
            drawdown_metrics.get("reward_to_trading_loss_ratio")
        ),
        "drawdown_max_active_order_fraction": _float(
            drawdown_metrics.get("max_active_order_fraction")
        ),
        "capital_status": capital.get("status", "not_evaluated"),
        "capital_risk_stress_passed": capital_passed,
        "capital_cash_reserve_fraction": _float(
            capital_metrics.get("cash_reserve_fraction")
        ),
        "capital_unhedged_loss_fraction": _float(
            capital_metrics.get("unhedged_loss_fraction_of_capital")
        ),
        "capital_configured_cap_loss_usdc": _float(
            capital_metrics.get("configured_inventory_cap_loss_to_zero")
        ),
        "capital_configured_cap_loss_fraction": _float(
            capital_metrics.get("configured_inventory_cap_loss_fraction")
        ),
        "capital_configured_cap_recovery_days": _float(
            capital_metrics.get("capped_recovery_days_at_p05_income")
        ),
        "capital_capture_needed_after_cap_loss": _float(
            capital_metrics.get("capture_needed_after_cap_loss")
        ),
        "capital_after_cap_loss_monthly": _float(
            capital_metrics.get("reference_capture_p05_monthly_after_cap_loss")
        ),
        "risk_income_gate_passed": risk_income_gate,
        "research_core_passed": research_core_passed,
        "depth_ready": bool(depth_gates.get("depth_ready")),
        "promotion_ready": promotion_ready,
        "depth_status": depth.get("status"),
        "blockers": "; ".join(
            str(x)
            for x in [
                *(depth.get("blockers", []) or []),
                *(drawdown.get("blockers", []) or []),
                *(capital.get("blockers", []) or []),
            ]
        ),
    }


def _selection_status(selected: dict[str, Any]) -> str:
    if not selected:
        return "no_candidates"
    capital_passed = bool(selected.get("capital_risk_stress_passed", True))
    drawdown_passed = bool(selected.get("drawdown_core_passed", True))
    if bool(selected.get("depth_ready")) and drawdown_passed and capital_passed:
        return "selected_depth_ready"
    if (
        bool(selected.get("risk_income_gate_passed"))
        and drawdown_passed
        and capital_passed
    ):
        return "selected_risk_income_drawdown_capital_passed_sample_not_ready"
    if (
        bool(selected.get("risk_income_gate_passed"))
        and drawdown_passed
        and not capital_passed
    ):
        return "selected_risk_income_drawdown_passed_capital_failed"
    if bool(selected.get("risk_income_gate_passed")) and not drawdown_passed:
        return "selected_risk_income_passed_drawdown_failed"
    return "selected_best_failed"


def _sorted_frame(candidates: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(candidates)
    if frame.empty:
        return frame
    return frame.sort_values(
        [
            "depth_ready",
            "risk_income_gate_passed",
            "drawdown_core_passed",
            "capital_risk_stress_passed",
            "strict_topbook_rescue_passed",
            "latest_taker_residual_loss_fraction",
            "capital_configured_cap_loss_fraction",
            "capital_capture_needed_after_cap_loss",
            "partial_rescue_max_residual_loss_usdc",
            "income_p05_at_required_capture",
            "drawdown_reward_to_trading_loss_ratio",
            "avg_active_pair_notional",
        ],
        ascending=[
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
        ],
    )


def _write_progress(
    *,
    candidates: list[dict[str, Any]],
    completed: int,
    total: int,
    started: float,
    out: Path,
    args: argparse.Namespace,
    final: bool,
) -> None:
    progress_csv = (
        Path(args.progress_csv)
        if args.progress_csv
        else out / "partial_rescue_config_progress.csv"
    )
    progress_json = (
        Path(args.progress_json)
        if args.progress_json
        else out / "partial_rescue_config_progress.json"
    )
    progress_csv.parent.mkdir(parents=True, exist_ok=True)
    progress_json.parent.mkdir(parents=True, exist_ok=True)
    frame = _sorted_frame(candidates)
    if not frame.empty:
        frame.to_csv(progress_csv, index=False)
        best = frame.iloc[0].to_dict()
    else:
        best = {}
    elapsed = max(time.monotonic() - started, 0.0)
    rate = completed / elapsed if elapsed > 0 else math.inf
    remaining = (total - completed) / rate if rate and math.isfinite(rate) else math.inf
    payload = {
        "completed_configs": completed,
        "total_configs": total,
        "completed_fraction": completed / total if total else 1.0,
        "elapsed_seconds": elapsed,
        "configs_per_second": rate,
        "estimated_seconds_remaining": remaining,
        "final": final,
        "best": _json_safe(best),
        "progress_csv": str(progress_csv),
        "safety": "progress checkpoint only; no private keys, order signing, order submission, or cancellation",
    }
    tmp = progress_json.with_suffix(progress_json.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_json_safe(payload), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(progress_json)


def _lp_config_from_selected(
    selected: dict[str, Any], args: argparse.Namespace
) -> LPConfig:
    """Rebuild the selected LP config without retaining every grid quote frame.

    Large grids can contain thousands of parameter combinations. Persisting every
    generated quote DataFrame only to write one selected CSV causes avoidable RAM
    growth; recomputing the selected quotes is deterministic and keeps broad
    research sweeps bounded by the current candidate, not all candidates.
    """

    return LPConfig(
        quote_size_shares=float(selected["quote_size"]),
        quote_offset=float(selected["quote_offset"]),
        safety_margin=args.safety_margin,
        active_capital_limit=args.active_capital_limit,
        max_unpaired_per_market=float(selected["max_unpaired_per_market"]),
        max_total_unpaired=float(selected["max_total_unpaired"]),
        max_cluster_unpaired=float(selected["max_cluster_unpaired"]),
        max_unpaired_minutes=float(selected["max_unpaired_minutes"]),
        excluded_categories=args.excluded_categories,
        min_reward_density_per_day=float(selected["min_reward_density_per_day"]),
        max_recent_vol=args.max_recent_vol,
        max_recent_jump=args.max_recent_jump,
        vol_quote_multiplier=args.vol_quote_multiplier,
        depth_cap_quote_size=bool(selected["depth_cap_quote_size"]),
        depth_quote_size_fraction=float(selected["depth_quote_size_fraction"]),
        min_depth_capped_quote_size_shares=args.min_depth_capped_quote_size,
        partial_rescue_max_residual_loss_usdc=float(
            selected["partial_rescue_max_residual_loss_usdc"]
        ),
    )


def _float_grid(text: str) -> list[float]:
    return [float(part.strip()) for part in str(text).split(",") if part.strip()]


def _bool_grid(text: str) -> list[bool]:
    values: list[bool] = []
    for part in str(text).split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token in {"1", "true", "t", "yes", "y", "on"}:
            values.append(True)
        elif token in {"0", "false", "f", "no", "n", "off"}:
            values.append(False)
        else:
            raise SystemExit(
                f"invalid boolean grid token for --depth-cap-quote-size-grid: {part!r}"
            )
    return values


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return value


if __name__ == "__main__":
    main()
