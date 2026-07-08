#!/usr/bin/env python3
"""Refresh LP candidate gates and build a current public-paper leaderboard.

This orchestrator reads candidate background manifests, recomputes target-income,
CLOB rescue stress, and depth gates from public snapshot/quote CSVs, then ranks
candidates with the reusable leaderboard module. It never signs, submits,
cancels, or inspects orders.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
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
from polymarket_lp.candidate_leaderboard import CandidateEvidence, build_candidate_leaderboard  # noqa: E402
from polymarket_lp.drawdown_guard import (  # noqa: E402
    DrawdownGuardConfig,
    evaluate_drawdown_guard,
    lp_config_from_manifest,
)
from polymarket_lp.lp_backtest import load_snapshots  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", action="append", required=True, help="NAME=background_manifest.json")
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    p.add_argument("--work-dir", default="")
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.03937017359762)
    p.add_argument("--required-capture-rate", type=float, default=0.5)
    p.add_argument("--capture-rates", default="0.25,0.35,0.4,0.5,0.75,1.0")
    p.add_argument("--min-observation-hours", type=float, default=6.0)
    p.add_argument("--min-quote-rows", type=int, default=24)
    p.add_argument("--min-unique-markets", type=int, default=2)
    p.add_argument("--min-book-scenarios", type=int, default=24)
    p.add_argument("--max-active-pair-notional", type=float, default=1200.0)
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--bootstrap-resamples", type=int, default=1000)
    p.add_argument("--bootstrap-seed", type=int, default=177)
    p.add_argument("--bootstrap-block-size", type=int, default=2)
    p.add_argument("--min-taker-rescue-feasible-rate", type=float, default=0.80)
    p.add_argument("--min-taker-rescue-depth-fraction", type=float, default=1.0)
    p.add_argument("--min-taker-rescue-pair-edge-per-share", type=float, default=0.0)
    p.add_argument("--max-latest-taker-residual-loss-fraction", type=float, default=0.05)
    p.add_argument("--max-mtm-drawdown-fraction", type=float, default=0.10)
    p.add_argument("--max-realized-drawdown-fraction", type=float, default=0.05)
    p.add_argument("--max-open-inventory-fraction", type=float, default=0.50)
    p.add_argument("--max-active-order-fraction", type=float, default=0.70)
    p.add_argument("--min-reward-to-trading-loss-ratio", type=float, default=3.0)
    p.add_argument("--min-cash-reserve-fraction", type=float, default=0.40)
    p.add_argument("--max-unhedged-loss-fraction", type=float, default=0.50)
    p.add_argument("--max-configured-cap-loss-fraction", type=float, default=0.25)
    p.add_argument("--max-configured-cap-recovery-days", type=float, default=10.0)
    p.add_argument("--max-unpaired-per-market", type=float, default=60.0)
    p.add_argument("--max-total-unpaired", type=float, default=450.0)
    p.add_argument("--max-cluster-unpaired", type=float, default=250.0)
    p.add_argument("--min-latest-markets", type=int, default=2)
    p.add_argument("--max-single-market-active-fraction", type=float, default=0.35)
    p.add_argument("--max-single-cluster-active-fraction", type=float, default=0.70)
    p.add_argument("--max-single-market-unhedged-loss-fraction", type=float, default=0.20)
    p.add_argument("--max-single-cluster-unhedged-loss-fraction", type=float, default=0.50)
    p.add_argument("--exit-slippage", type=float, default=0.005)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    work_dir = Path(args.work_dir) if args.work_dir else Path(args.out).resolve().parent / "candidate_refresh"
    work_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[CandidateEvidence] = []
    refreshed: list[dict[str, Any]] = []
    for index, item in enumerate(args.candidate):
        name, background_path = _split_named_path(item, "--candidate")
        background = json.loads(Path(background_path).read_text(encoding="utf-8-sig"))
        candidate_dir = work_dir / _safe_name(name)
        try:
            refreshed_paths = refresh_candidate(name, background, candidate_dir, args, seed=args.bootstrap_seed + index)
            gate = json.loads(refreshed_paths["gate"].read_text(encoding="utf-8-sig"))
            drawdown_guard = evaluate_candidate_drawdown(name, background, args)
            capital_risk = evaluate_candidate_capital(name, background, refreshed_paths["target_status"], args)
            gate_path_text = str(refreshed_paths["gate"])
        except SystemExit as exc:
            message = str(exc)
            gate = _pending_gate(message)
            drawdown_guard = _pending_drawdown(message)
            capital_risk = _pending_capital(message)
            gate_path_text = ""
        candidates.append(
            CandidateEvidence(
                name=name,
                gate=gate,
                metadata=background,
                drawdown_guard=drawdown_guard,
                capital_risk=capital_risk,
            )
        )
        refreshed.append(
            {
                "name": name,
                "background": str(background_path),
                "gate": gate_path_text,
                "candidate_dir": str(candidate_dir),
            }
        )
    result = build_candidate_leaderboard(candidates)
    result["refreshed_candidates"] = refreshed
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_json_safe(result), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md = Path(args.markdown_out)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_markdown(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "leader": result.get("leader", {}).get("name")}, indent=2))


def refresh_candidate(
    name: str,
    background: dict[str, Any],
    out_dir: Path,
    args: argparse.Namespace,
    *,
    seed: int,
) -> dict[str, Path]:
    snapshot = _required_path(background, "snapshot", name)
    quotes = _required_path(background, "quotes", name)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_dir = out_dir / "target_status"
    history = out_dir / "target_status_history.csv"
    rescue_json = out_dir / "rescue_stress.json"
    rescue_md = out_dir / "rescue_stress.md"
    gate_json = out_dir / "depth_gate.json"
    gate_md = out_dir / "depth_gate.md"
    run(
        [
            sys.executable,
            "scripts/update_target_status.py",
            "--snapshots",
            str(snapshot),
            "--quotes",
            str(quotes),
            "--out-dir",
            str(target_dir),
            "--history-csv",
            str(history),
            "--initial-capital",
            str(args.initial_capital),
            "--target-monthly",
            str(args.target_monthly),
            "--reward-to-loss-haircut",
            str(args.reward_to_loss_haircut),
            "--min-observation-hours",
            str(args.min_observation_hours),
            "--min-unique-markets",
            str(args.min_unique_markets),
            "--max-active-pair-notional",
            str(args.max_active_pair_notional),
            "--max-pending-quote-rate",
            str(args.max_pending_quote_rate),
            "--bootstrap-resamples",
            str(args.bootstrap_resamples),
            "--bootstrap-seed",
            str(seed),
            "--bootstrap-block-size",
            str(args.bootstrap_block_size),
            "--bootstrap-capture-rate",
            str(args.required_capture_rate),
            "--bootstrap-capture-rates",
            str(args.capture_rates),
            "--bootstrap-min-target-margin",
            "1.0",
        ],
        out_dir,
    )
    run(
        [
            sys.executable,
            "scripts/rescue_stress.py",
            "--quotes",
            str(quotes),
            "--out",
            str(rescue_json),
            "--markdown-out",
            str(rescue_md),
            "--initial-capital",
            str(args.initial_capital),
            "--require-taker-residual-loss",
            "--max-latest-taker-residual-loss-fraction",
            str(args.max_latest_taker_residual_loss_fraction),
            "--min-taker-rescue-depth-fraction",
            str(args.min_taker_rescue_depth_fraction),
            "--taker-rescue-min-pair-edge-per-share",
            str(args.min_taker_rescue_pair_edge_per_share),
        ],
        out_dir,
    )
    run(
        [
            sys.executable,
            "scripts/depth_gate.py",
            "--target-status",
            str(target_dir / "target_status.json"),
            "--rescue-stress",
            str(rescue_json),
            "--out",
            str(gate_json),
            "--markdown-out",
            str(gate_md),
            "--target-monthly",
            str(args.target_monthly),
            "--required-capture-rate",
            str(args.required_capture_rate),
            "--min-observation-hours",
            str(args.min_observation_hours),
            "--min-quote-rows",
            str(args.min_quote_rows),
            "--min-unique-markets",
            str(args.min_unique_markets),
            "--min-book-scenarios",
            str(args.min_book_scenarios),
            "--min-taker-rescue-feasible-rate",
            str(args.min_taker_rescue_feasible_rate),
            "--min-taker-rescue-depth-fraction",
            str(args.min_taker_rescue_depth_fraction),
            "--min-taker-rescue-pair-edge-per-share",
            str(args.min_taker_rescue_pair_edge_per_share),
            "--allow-partial-taker-rescue",
            "--max-latest-taker-residual-loss-fraction",
            str(args.max_latest_taker_residual_loss_fraction),
        ],
        out_dir,
    )
    return {"gate": gate_json, "target_status": target_dir / "target_status.json"}


def evaluate_candidate_drawdown(name: str, background: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    snapshot = _required_path(background, "snapshot", name)
    manifest = dict(background)
    manifest["_candidate_name"] = name
    cfg = DrawdownGuardConfig(
        initial_capital=args.initial_capital,
        min_observation_hours=args.min_observation_hours,
        max_mtm_drawdown_fraction=args.max_mtm_drawdown_fraction,
        max_realized_drawdown_fraction=args.max_realized_drawdown_fraction,
        max_open_inventory_fraction=args.max_open_inventory_fraction,
        max_active_order_fraction=args.max_active_order_fraction,
        min_reward_to_trading_loss_ratio=args.min_reward_to_trading_loss_ratio,
    )
    return evaluate_drawdown_guard(load_snapshots(snapshot), lp_config_from_manifest(manifest), cfg)


def evaluate_candidate_capital(
    name: str,
    background: dict[str, Any],
    target_status: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    quotes = _required_path(background, "quotes", name)
    if not target_status.exists():
        raise SystemExit(f"candidate {name!r} target status path does not exist: {target_status}")
    cfg = config_from_lp_manifest(
        background,
        CapitalRiskStressConfig(
            initial_capital=args.initial_capital,
            min_cash_reserve_fraction=args.min_cash_reserve_fraction,
            max_unhedged_loss_fraction=args.max_unhedged_loss_fraction,
            max_capped_loss_fraction=args.max_configured_cap_loss_fraction,
            max_capped_recovery_days=args.max_configured_cap_recovery_days,
            max_unpaired_per_market=args.max_unpaired_per_market,
            max_total_unpaired=args.max_total_unpaired,
            max_cluster_unpaired=args.max_cluster_unpaired,
            min_latest_markets=args.min_latest_markets,
            max_single_market_active_fraction=args.max_single_market_active_fraction,
            max_single_cluster_active_fraction=args.max_single_cluster_active_fraction,
            max_single_market_unhedged_loss_fraction=args.max_single_market_unhedged_loss_fraction,
            max_single_cluster_unhedged_loss_fraction=args.max_single_cluster_unhedged_loss_fraction,
            exit_slippage=args.exit_slippage,
        ),
    )
    target = json.loads(target_status.read_text(encoding="utf-8-sig"))
    return evaluate_capital_risk_stress(pd.read_csv(quotes), target_status=target, cfg=cfg)


def _pending_gate(message: str) -> dict[str, Any]:
    return {
        "status": "candidate_data_pending",
        "metrics": {
            "duration_hours": 0.0,
            "quote_rows": 0,
            "unique_markets_quoted": 0,
            "income_p05_at_required_capture": math.nan,
            "taker_rescue_feasible_rate": math.nan,
            "taker_size_weighted_rescue_fraction": math.nan,
            "latest_taker_residual_loss_to_zero": math.nan,
            "latest_taker_residual_loss_fraction": math.nan,
        },
        "gates": {
            "depth_ready": False,
            "income_p05_gate_passed": False,
            "clob_quality_gate_passed": False,
            "taker_rescue_rate_gate_passed": False,
            "taker_pair_edge_gate_passed": False,
            "taker_depth_gate_passed": False,
            "taker_residual_loss_gate_passed": False,
            "sample_hours_gate_passed": False,
            "quote_rows_gate_passed": False,
            "book_scenario_gate_passed": False,
        },
        "blockers": [message],
    }


def _pending_drawdown(message: str) -> dict[str, Any]:
    return {
        "status": "drawdown_guard_data_pending",
        "risk_core_passed": False,
        "gates": {"sample_hours_gate_passed": False, "drawdown_guard_passed": False},
        "metrics": {
            "duration_hours": 0.0,
            "max_drawdown_mtm_fraction": math.nan,
            "max_drawdown_realized_fraction": math.nan,
            "reward_to_trading_loss_ratio": math.nan,
            "max_active_order_fraction": math.nan,
        },
        "blockers": [message],
    }


def _pending_capital(message: str) -> dict[str, Any]:
    return {
        "status": "capital_risk_data_pending",
        "metrics": {
            "latest_markets": 0,
            "cash_reserve_fraction": math.nan,
            "max_single_market_active_fraction": math.nan,
            "max_single_cluster_active_fraction": math.nan,
            "single_market_worst_one_side_loss_fraction": math.nan,
            "single_cluster_worst_one_side_loss_fraction": math.nan,
            "unhedged_loss_fraction_of_capital": math.nan,
            "configured_inventory_cap_loss_to_zero": math.nan,
            "configured_inventory_cap_loss_fraction": math.nan,
            "capped_recovery_days_at_p05_income": math.nan,
        },
        "gates": {"capital_risk_stress_passed": False},
        "blockers": [message],
    }


def run(command: list[str], out_dir: Path) -> None:
    log = out_dir / "refresh_candidate_leaderboard.log"
    with log.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n")
        subprocess.run(command, cwd=ROOT, check=True, stdout=handle, stderr=subprocess.STDOUT)


def _required_path(background: dict[str, Any], key: str, name: str) -> Path:
    value = background.get(key)
    if not value:
        raise SystemExit(f"candidate {name!r} background missing {key!r}")
    path = Path(str(value))
    if not path.exists():
        raise SystemExit(f"candidate {name!r} {key!r} path does not exist: {path}")
    return path


def _split_named_path(item: str, flag: str) -> tuple[str, str]:
    if "=" not in item:
        raise SystemExit(f"{flag} must be NAME=PATH")
    name, path = item.split("=", 1)
    if not name or not path:
        raise SystemExit(f"{flag} must be NAME=PATH")
    return name, path


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name).strip("._") or "candidate"


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# LP candidate leaderboard",
        "",
        "Safety: read-only public-paper comparison; no keys, signing, orders, cancels, or paid-reward verification.",
        "",
        f"Status: `{result['status']}`",
        f"Leader policy: `{result.get('leader_policy', 'n/a')}`",
        "",
        "| Rank | Candidate | Status | p05/mo @ capture | After cap loss | Req capture | Req capture after cap | After-cap buffer | Quote | Active cap | Rescue cap | Cap loss | Cap recovery | Cash reserve | Mkt active | Cluster active | Hours | Rows | Markets | Rescue feasible | Residual loss | Max active | DD guard | Cap guard | Note |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for idx, row in enumerate(result.get("candidates", []), start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    str(row.get("name", "")),
                    str(row.get("status", "")),
                    _money(row.get("income_p05_at_required_capture")),
                    _money(row.get("capital_after_configured_cap_loss_monthly")),
                    _pct(row.get("capture_needed_for_target")),
                    _pct(row.get("capture_needed_after_cap_loss")),
                    _pct(row.get("after_cap_loss_income_buffer_at_required_capture")),
                    _shares(row.get("configured_quote_size_shares")),
                    _money(row.get("configured_active_capital_limit")),
                    _money(row.get("configured_residual_loss_cap_usdc")),
                    _money(row.get("capital_configured_cap_loss_usdc")),
                    _num(row.get("capital_configured_cap_recovery_days"), 2),
                    _pct(row.get("capital_cash_reserve_fraction")),
                    _pct(row.get("capital_single_market_active_fraction")),
                    _pct(row.get("capital_single_cluster_active_fraction")),
                    _num(row.get("duration_hours"), 2),
                    str(row.get("quote_rows", 0)),
                    str(row.get("unique_markets_quoted", 0)),
                    _pct(row.get("taker_rescue_feasible_rate")),
                    _money(row.get("latest_taker_residual_loss_to_zero")),
                    _pct(row.get("drawdown_max_active_order_fraction")),
                    str(row.get("drawdown_guard_status", "not_evaluated")),
                    str(row.get("capital_risk_status", "not_evaluated")),
                    str(row.get("ranking_note", "")),
                ]
            )
            + " |"
        )
    leader = result.get("leader") or {}
    blockers = leader.get("blockers") or []
    if blockers:
        lines.extend(["", f"## Leader blockers: {leader.get('name', '')}", ""])
        lines.extend(f"- {x}" for x in blockers)
    policy = result.get("policy_leaders") or {}
    if isinstance(policy, dict) and policy:
        lines.extend(["", "## Policy leaders", ""])
        for label in ["risk_first_leader", "income_first_leader", "sample_first_leader"]:
            item = policy.get(label) or {}
            if isinstance(item, dict) and item:
                lines.append(
                    f"- {label}: `{item.get('name')}`; p05/mo {_money(item.get('income_p05_at_required_capture'))}; "
                    f"quote {_shares(item.get('configured_quote_size_shares'))}; active cap {_money(item.get('configured_active_capital_limit'))}; "
                    f"rescue cap {_money(item.get('configured_residual_loss_cap_usdc'))}; hours {_num(item.get('duration_hours'), 2)}; "
                    f"cap loss {_money(item.get('capital_configured_cap_loss_usdc'))}; cap recovery {_num(item.get('capital_configured_cap_recovery_days'), 2)}d; "
                    f"after-cap p05 {_money(item.get('capital_after_configured_cap_loss_monthly'))}; "
                    f"capture needed after cap {_pct(item.get('capture_needed_after_cap_loss'))}; "
                    f"after-cap buffer {_pct(item.get('after_cap_loss_income_buffer_at_required_capture'))}; "
                    f"single-market active {_pct(item.get('capital_single_market_active_fraction'))}; "
                    f"single-cluster active {_pct(item.get('capital_single_cluster_active_fraction'))}; "
                    f"drawdown guard {item.get('drawdown_guard_status')}; capital guard {item.get('capital_risk_status')}."
                )
        if policy.get("note"):
            lines.append(f"- note: {policy['note']}")
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
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return "n/a" if not math.isfinite(x) else f"{x:.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _shares(value: object) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"{x:,.0f}"
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
