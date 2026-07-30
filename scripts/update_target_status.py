#!/usr/bin/env python3
"""Refresh LP paper analysis plus monthly target monitor outputs.

This is a local paper-research helper. It reads public snapshot/quote CSVs,
recomputes paper diagnostics, evaluates the target gate, and writes a compact
Markdown/JSON status. It never signs, submits, cancels, or inspects orders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from polymarket_lp.lp_backtest import load_snapshots
from polymarket_lp.paper import PaperAnalysisConfig, run_paper_analysis_to_files
from polymarket_lp.target import TargetMonitorConfig, target_monitor_from_summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--quotes", required=True)
    p.add_argument("--out-dir", default="data/processed/target_status")
    p.add_argument(
        "--history-csv",
        default="",
        help="Optional CSV to append one compact status row per refresh",
    )
    p.add_argument(
        "--report",
        default="",
        help="Optional Markdown report to append latest status to",
    )
    p.add_argument(
        "--downloads-copy",
        default="",
        help="Optional path to copy the report/status Markdown to",
    )
    p.add_argument("--initial-capital", type=float, default=2000.0)
    p.add_argument("--target-monthly", type=float, default=1000.0)
    p.add_argument("--reward-to-loss-haircut", type=float, default=8.0)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--min-observation-hours", type=float, default=24.0)
    p.add_argument("--paid-reward-verified", action="store_true")
    p.add_argument("--max-stale-seconds", type=float, default=900.0)
    p.add_argument("--stale-mid-change", type=float, default=0.03)
    p.add_argument("--fill-mid-cross-buffer", type=float, default=0.0)
    p.add_argument("--max-reward-gap-seconds", type=float, default=300.0)
    p.add_argument("--min-unique-markets", type=int, default=1)
    p.add_argument("--max-active-pair-notional", type=float, default=float("inf"))
    p.add_argument("--max-pending-quote-rate", type=float, default=0.05)
    p.add_argument("--bootstrap-resamples", type=int, default=0)
    p.add_argument("--bootstrap-seed", type=int, default=7)
    p.add_argument("--bootstrap-block-size", type=int, default=1)
    p.add_argument("--bootstrap-capture-rate", type=float, default=1.0)
    p.add_argument(
        "--bootstrap-capture-rates",
        default="",
        help="Optional comma-separated capture stress rates, e.g. 0.35,0.5,0.75",
    )
    p.add_argument("--bootstrap-min-target-margin", type=float, default=1.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paper_summary = run_paper_analysis_to_files(
        snapshots=load_snapshots(args.snapshots),
        quotes=pd.read_csv(args.quotes),
        out_dir=out / "paper_analysis",
        cfg=PaperAnalysisConfig(
            max_stale_seconds=args.max_stale_seconds,
            stale_mid_change=args.stale_mid_change,
            fill_mid_cross_buffer=args.fill_mid_cross_buffer,
            max_reward_gap_seconds=args.max_reward_gap_seconds,
        ),
    )
    monitor = target_monitor_from_summary(
        paper_summary,
        TargetMonitorConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            reward_to_loss_haircut=args.reward_to_loss_haircut,
            days_per_month=args.days_per_month,
            min_observation_hours=args.min_observation_hours,
            min_unique_markets=args.min_unique_markets,
            max_active_pair_notional=args.max_active_pair_notional,
            max_pending_quote_rate=args.max_pending_quote_rate,
            paid_reward_verified=args.paid_reward_verified,
        ),
    )
    bootstrap = _bootstrap_target_from_quotes(
        pd.read_csv(args.quotes),
        cfg=TargetMonitorConfig(
            initial_capital=args.initial_capital,
            target_monthly_usdc=args.target_monthly,
            reward_to_loss_haircut=args.reward_to_loss_haircut,
            days_per_month=args.days_per_month,
            min_observation_hours=args.min_observation_hours,
            min_unique_markets=args.min_unique_markets,
            max_active_pair_notional=args.max_active_pair_notional,
            max_pending_quote_rate=args.max_pending_quote_rate,
            paid_reward_verified=args.paid_reward_verified,
        ),
        max_reward_gap_seconds=args.max_reward_gap_seconds,
        resamples=args.bootstrap_resamples,
        seed=args.bootstrap_seed,
        block_size=args.bootstrap_block_size,
        capture_rate=args.bootstrap_capture_rate,
        min_target_margin=args.bootstrap_min_target_margin,
    )
    capture_stress = _capture_stress_grid(
        bootstrap,
        capture_rates=_float_list(args.bootstrap_capture_rates),
        target_monthly_usdc=args.target_monthly,
        min_target_margin=args.bootstrap_min_target_margin,
    )
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "snapshots": args.snapshots,
        "quotes": args.quotes,
        "paper_summary": paper_summary,
        "target_monitor": monitor,
        "bootstrap_target": bootstrap,
        "capture_stress_grid": capture_stress,
        "safety": "paper analytics only; no private keys, order signing, order submission, or cancellation",
    }
    (out / "target_status.json").write_text(
        json.dumps(_json_safe(payload), indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if args.history_csv:
        _append_history(Path(args.history_csv), payload)
    markdown = _markdown(payload)
    status_md = out / "target_status.md"
    status_md.write_text(markdown, encoding="utf-8")
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as fh:
            fh.write("\n" + markdown)
        if args.downloads_copy:
            shutil.copyfile(report, args.downloads_copy)
    elif args.downloads_copy:
        shutil.copyfile(status_md, args.downloads_copy)
    print(
        json.dumps(
            {
                "status_json": str(out / "target_status.json"),
                "status_md": str(status_md),
                "status": monitor["status"],
            },
            indent=2,
        )
    )


def _append_history(path: Path, payload: dict[str, object]) -> None:
    monitor = payload["target_monitor"]  # type: ignore[index]
    paper = payload["paper_summary"]  # type: ignore[index]
    assert isinstance(monitor, dict)
    assert isinstance(paper, dict)
    target = monitor["target_math"]
    gates = monitor["gates"]
    assert isinstance(target, dict)
    assert isinstance(gates, dict)
    row = {
        "generated_utc": payload["generated_utc"],
        "duration_hours": monitor["input"]["duration_hours"],  # type: ignore[index]
        "quote_rows": monitor["input"]["quote_rows"],  # type: ignore[index]
        "unique_markets_quoted": monitor["input"]["unique_markets_quoted"],  # type: ignore[index]
        "estimated_reward_accrual_usdc": monitor["input"][
            "estimated_reward_accrual_usdc"
        ],  # type: ignore[index]
        "gross_reward_monthly": target["gross_reward_monthly"],
        "net_monthly_after_loss_haircut": target["net_monthly_after_loss_haircut"],
        "capture_needed_for_target": target["capture_needed_for_target"],
        "observed_reward_density_per_day": target["observed_reward_density_per_day"],
        "required_reward_density_per_day": target["required_reward_density_per_day"],
        "fill_proxy_rate": paper.get("fill_proxy_rate"),
        "stale_fill_rate": paper.get("stale_fill_rate"),
        "pending_quote_rate": paper.get("pending_quote_rate"),
        "raw_pending_quote_rate": paper.get("raw_pending_quote_rate"),
        "right_censored_pending_quote_rate": paper.get(
            "right_censored_pending_quote_rate"
        ),
        "evaluable_quote_rows": paper.get("evaluable_quote_rows"),
        "max_active_pair_notional": monitor["input"]["max_active_pair_notional"],  # type: ignore[index]
        "density_gate_passed": gates["density_gate_passed"],
        "capture_gate_passed": gates["capture_gate_passed"],
        "risk_proxy_gate_passed": gates["risk_proxy_gate_passed"],
        "diversification_gate_passed": gates["diversification_gate_passed"],
        "active_notional_gate_passed": gates["active_notional_gate_passed"],
        "sample_gate_passed": gates["sample_gate_passed"],
        "deployment_proof_passed": gates["deployment_proof_passed"],
        "status": monitor["status"],
    }
    bootstrap = payload.get("bootstrap_target")
    if isinstance(bootstrap, dict) and bootstrap.get("enabled"):
        row.update(
            {
                "bootstrap_intervals": bootstrap.get("intervals"),
                "bootstrap_net_monthly_p05": bootstrap.get("net_monthly_p05"),
                "bootstrap_target_hit_probability": bootstrap.get(
                    "target_hit_probability"
                ),
                "bootstrap_p05_target_gate_passed": bootstrap.get(
                    "p05_target_gate_passed"
                ),
                "bootstrap_capture_rate": bootstrap.get("capture_rate"),
                "bootstrap_captured_net_monthly_p05": bootstrap.get(
                    "captured_net_monthly_p05"
                ),
                "bootstrap_captured_p05_target_gate_passed": bootstrap.get(
                    "captured_p05_target_gate_passed"
                ),
            }
        )
    stress = payload.get("capture_stress_grid")
    if isinstance(stress, list) and stress:
        for item in stress:
            if isinstance(item, dict):
                label = str(item.get("capture_rate", "")).replace(".", "p")
                row[f"capture_{label}_p05_net_monthly"] = item.get(
                    "captured_net_monthly_p05"
                )
                row[f"capture_{label}_target_gate_passed"] = item.get(
                    "target_gate_passed"
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([row])
    if not path.exists() or path.stat().st_size == 0:
        frame.to_csv(path, index=False)
        return
    existing = pd.read_csv(path)
    existing_cols = list(existing.columns)
    new_cols = list(frame.columns)
    if existing_cols == new_cols:
        frame.to_csv(path, mode="a", header=False, index=False)
        return
    all_cols = list(dict.fromkeys(existing_cols + new_cols))
    pd.concat(
        [existing.reindex(columns=all_cols), frame.reindex(columns=all_cols)],
        ignore_index=True,
    ).to_csv(path, index=False)


def _markdown(payload: dict[str, object]) -> str:
    monitor = payload["target_monitor"]  # type: ignore[index]
    paper = payload["paper_summary"]  # type: ignore[index]
    assert isinstance(monitor, dict)
    assert isinstance(paper, dict)
    target = monitor["target_math"]
    gates = monitor["gates"]
    assert isinstance(target, dict)
    assert isinstance(gates, dict)
    rows = [
        ("Observation hours", _num(monitor["input"]["duration_hours"])),  # type: ignore[index]
        ("Quote rows", str(monitor["input"]["quote_rows"])),  # type: ignore[index]
        ("Unique markets quoted", str(monitor["input"]["unique_markets_quoted"])),  # type: ignore[index]
        ("Gross monthly pace", _money(target["gross_reward_monthly"])),
        (
            "Net monthly after loss haircut",
            _money(target["net_monthly_after_loss_haircut"]),
        ),
        ("Capture needed for target", _pct(target["capture_needed_for_target"])),
        (
            "Observed reward density/day",
            _pct(target["observed_reward_density_per_day"]),
        ),
        (
            "Required reward density/day",
            _pct(target["required_reward_density_per_day"]),
        ),
        (
            "Max active pair notional",
            _money(monitor["input"]["max_active_pair_notional"]),
        ),  # type: ignore[index]
        (
            "Fill/stale/evaluable-pending proxy",
            f"{_pct(paper.get('fill_proxy_rate'))} / {_pct(paper.get('stale_fill_rate'))} / {_pct(paper.get('pending_quote_rate'))}",
        ),
        (
            "Raw/right-censored pending",
            f"{_pct(paper.get('raw_pending_quote_rate'))} / {_pct(paper.get('right_censored_pending_quote_rate'))}",
        ),
        (
            "Density/capture/risk/diversification/notional gates",
            f"{gates['density_gate_passed']} / {gates['capture_gate_passed']} / {gates['risk_proxy_gate_passed']} / {gates['diversification_gate_passed']} / {gates['active_notional_gate_passed']}",
        ),
        ("Deployment proof", str(gates["deployment_proof_passed"])),
        ("Monitor status", str(monitor["status"])),
    ]
    bootstrap = payload.get("bootstrap_target")
    if isinstance(bootstrap, dict) and bootstrap.get("enabled"):
        rows.extend(
            [
                ("Bootstrap intervals", str(bootstrap.get("intervals"))),
                ("Bootstrap net monthly p05", _money(bootstrap.get("net_monthly_p05"))),
                (
                    "Bootstrap target-hit probability",
                    _pct(bootstrap.get("target_hit_probability")),
                ),
                (
                    "Bootstrap p05 target gate",
                    str(bootstrap.get("p05_target_gate_passed")),
                ),
                ("Bootstrap capture stress", _pct(bootstrap.get("capture_rate"))),
                (
                    "Bootstrap captured p05 net monthly",
                    _money(bootstrap.get("captured_net_monthly_p05")),
                ),
                (
                    "Bootstrap captured p05 target gate",
                    str(bootstrap.get("captured_p05_target_gate_passed")),
                ),
            ]
        )
    stress = payload.get("capture_stress_grid")
    if isinstance(stress, list) and stress:
        for item in stress:
            if isinstance(item, dict):
                rows.append(
                    (
                        f"Capture stress p05 @ {_pct(item.get('capture_rate'))}",
                        f"{_money(item.get('captured_net_monthly_p05'))} / gate {item.get('target_gate_passed')}",
                    )
                )
    lines = [
        "## Automated target-status refresh",
        "",
        f"Generated: {payload['generated_utc']}. Safety: paper analytics only.",
        "",
        "| Gate item | Value |",
        "|---|---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in rows)
    lines.append("")
    return "\n".join(lines)


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        return f"{100 * float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _float_list(text: str) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _capture_stress_grid(
    bootstrap: dict[str, object],
    *,
    capture_rates: list[float],
    target_monthly_usdc: float,
    min_target_margin: float,
) -> list[dict[str, object]]:
    if (
        not capture_rates
        or not bootstrap.get("enabled")
        or "net_monthly_p05" not in bootstrap
    ):
        return []
    p05 = float(bootstrap["net_monthly_p05"])
    threshold = float(target_monthly_usdc) * max(0.0, float(min_target_margin))
    rows: list[dict[str, object]] = []
    for rate in capture_rates:
        clipped = max(0.0, min(1.0, float(rate)))
        captured = p05 * clipped
        rows.append(
            {
                "capture_rate": clipped,
                "captured_net_monthly_p05": captured,
                "target_threshold_with_margin": threshold,
                "target_gate_passed": bool(captured >= threshold),
            }
        )
    return rows


def _bootstrap_target_from_quotes(
    quotes: pd.DataFrame,
    *,
    cfg: TargetMonitorConfig,
    max_reward_gap_seconds: float,
    resamples: int,
    seed: int,
    block_size: int,
    capture_rate: float,
    min_target_margin: float,
) -> dict[str, object]:
    """Bootstrap interval reward-density persistence from public paper quotes.

    This is deliberately a diagnostics layer, not deployment proof. It samples
    observed quote intervals to estimate whether the reward-density regime
    remains above target after the configured reward/loss haircut.
    """

    if resamples <= 0:
        return {"enabled": False}
    capture_rate = max(0.0, min(1.0, float(capture_rate)))
    min_target_margin = max(0.0, float(min_target_margin))
    required = {
        "timestamp",
        "condition_id",
        "reward_density_per_day",
        "active_order_notional_pair",
    }
    if quotes.empty or not required.issubset(quotes.columns):
        return {"enabled": True, "error": "missing_quote_columns", "intervals": 0}

    pair = quotes.copy()
    pair["timestamp"] = pd.to_datetime(pair["timestamp"], utc=True, errors="coerce")
    pair["condition_id"] = pair["condition_id"].astype(str)
    pair["reward_density_per_day"] = pd.to_numeric(
        pair["reward_density_per_day"], errors="coerce"
    )
    pair["active_order_notional_pair"] = pd.to_numeric(
        pair["active_order_notional_pair"], errors="coerce"
    )
    pair = pair.dropna(
        subset=[
            "timestamp",
            "condition_id",
            "reward_density_per_day",
            "active_order_notional_pair",
        ]
    )
    pair = pair.drop_duplicates(["timestamp", "condition_id"]).sort_values(
        ["condition_id", "timestamp"]
    )
    if pair.empty:
        return {"enabled": True, "error": "empty_quote_pairs", "intervals": 0}

    pair["next_ts"] = pair.groupby("condition_id", sort=False)["timestamp"].shift(-1)
    gap = (pair["next_ts"] - pair["timestamp"]).dt.total_seconds()
    pair["gap_seconds"] = (
        pd.to_numeric(gap, errors="coerce")
        .clip(lower=0, upper=max_reward_gap_seconds)
        .fillna(0)
    )
    pair["reward_usdc"] = (
        pair["reward_density_per_day"].clip(lower=0)
        * pair["active_order_notional_pair"].clip(lower=0)
        * pair["gap_seconds"]
        / 86_400.0
    )
    interval = (
        pair.groupby("timestamp", sort=True)
        .agg(reward_usdc=("reward_usdc", "sum"), gap_seconds=("gap_seconds", "max"))
        .reset_index()
    )
    interval = interval[interval["gap_seconds"] > 0].reset_index(drop=True)
    n = int(len(interval))
    if n == 0:
        return {"enabled": True, "error": "no_positive_gap_intervals", "intervals": 0}

    block_size = max(1, min(int(block_size), n))
    rng = np.random.default_rng(seed)
    reward = interval["reward_usdc"].to_numpy(dtype=float)
    gap_seconds = interval["gap_seconds"].to_numpy(dtype=float)
    net_fraction = (
        max(0.0, 1.0 - 1.0 / cfg.reward_to_loss_haircut)
        if cfg.reward_to_loss_haircut > 0
        else 0.0
    )
    monthly_scale = 86_400.0 * cfg.days_per_month
    samples = []
    hit_count = 0
    starts = np.arange(n)
    for _ in range(int(resamples)):
        picked: list[int] = []
        while len(picked) < n:
            start = int(rng.choice(starts))
            picked.extend((start + j) % n for j in range(block_size))
        idx = np.array(picked[:n], dtype=int)
        sample_seconds = float(gap_seconds[idx].sum())
        if sample_seconds <= 0:
            continue
        net_monthly = float(
            reward[idx].sum() / sample_seconds * monthly_scale * net_fraction
        )
        samples.append(net_monthly)
        if net_monthly >= cfg.target_monthly_usdc:
            hit_count += 1
    if not samples:
        return {"enabled": True, "error": "empty_bootstrap_samples", "intervals": n}
    values = np.array(samples, dtype=float)
    observed_net = float(
        reward.sum() / gap_seconds.sum() * monthly_scale * net_fraction
    )
    p05 = float(np.quantile(values, 0.05))
    captured_p05 = p05 * capture_rate
    target_threshold = cfg.target_monthly_usdc * min_target_margin
    return {
        "enabled": True,
        "resamples": int(resamples),
        "seed": int(seed),
        "block_size": int(block_size),
        "intervals": n,
        "observed_net_monthly": observed_net,
        "net_monthly_p05": p05,
        "net_monthly_median": float(np.quantile(values, 0.50)),
        "net_monthly_p95": float(np.quantile(values, 0.95)),
        "target_hit_probability": float(hit_count / len(samples)),
        "p05_target_gate_passed": bool(p05 >= cfg.target_monthly_usdc),
        "capture_rate": capture_rate,
        "min_target_margin": min_target_margin,
        "target_threshold_with_margin": target_threshold,
        "captured_net_monthly_p05": captured_p05,
        "captured_p05_target_gate_passed": bool(captured_p05 >= target_threshold),
        "safety": "bootstrap samples public paper reward intervals only; not paid reward/order-fill proof",
    }


if __name__ == "__main__":
    main()
