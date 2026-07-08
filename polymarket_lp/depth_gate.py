from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class DepthReadinessConfig:
    """Deployment-side depth gate for LP public-paper evidence.

    This auditor combines target-income diagnostics with CLOB top-book rescue
    depth stress. It is read-only: no private keys, order signing, submission,
    cancellation, or account inspection.
    """

    target_monthly_usdc: float = 1_000.0
    required_capture_rate: float = 0.50
    min_observation_hours: float = 6.0
    min_quote_rows: int = 12
    min_unique_markets: int = 2
    min_book_scenarios: int = 12
    min_taker_rescue_feasible_rate: float = 0.80
    min_taker_rescue_pair_edge_per_share: float = 0.0
    min_taker_rescue_depth_fraction: float = 1.0
    require_clob_book_quality: bool = True
    allow_partial_taker_rescue: bool = False
    max_latest_taker_residual_loss_fraction: float = 0.05


def evaluate_depth_readiness(
    *,
    target_status: dict[str, Any],
    rescue_stress: dict[str, Any],
    cfg: DepthReadinessConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or DepthReadinessConfig()
    paper = _dict(target_status.get("paper_summary"))
    monitor = _dict(target_status.get("target_monitor"))
    monitor_input = _dict(monitor.get("input"))
    target_math = _dict(monitor.get("target_math"))
    bootstrap = _dict(target_status.get("bootstrap_target"))
    rescue_metrics = _dict(rescue_stress.get("metrics"))

    duration_hours = _float(monitor_input.get("duration_hours", paper.get("duration_hours")), 0.0)
    quote_rows = int(_float(monitor_input.get("quote_rows", paper.get("quote_rows")), 0.0))
    unique_markets = int(_float(monitor_input.get("unique_markets_quoted", paper.get("unique_markets_quoted")), 0.0))
    book_counts = _dict(paper.get("quote_data_quality_counts"))
    clob_rows = sum(int(_float(v, 0.0)) for key, v in book_counts.items() if str(key).startswith("clob_book"))
    total_quality_rows = sum(int(_float(v, 0.0)) for v in book_counts.values())
    clob_quality_rate = clob_rows / max(total_quality_rows, 1)

    income_p05 = _capture_p05(target_status, cfg.required_capture_rate)
    if not math.isfinite(income_p05):
        income_p05 = _float(target_math.get("net_monthly_after_loss_haircut"), math.nan) * max(
            0.0, min(1.0, cfg.required_capture_rate)
        )

    book_scenarios = int(_float(rescue_metrics.get("taker_rescue_book_scenarios"), 0.0))
    taker_rate = _float(rescue_metrics.get("taker_rescue_feasible_rate"), math.nan)
    min_edge = _float(rescue_metrics.get("taker_rescue_min_pair_edge_per_share"), math.nan)
    min_depth = _float(rescue_metrics.get("taker_rescue_min_depth_fraction"), math.nan)
    residual_loss = _float(rescue_metrics.get("latest_taker_residual_loss_to_zero"), math.nan)
    residual_loss_fraction = _float(rescue_metrics.get("latest_taker_residual_loss_fraction"), math.nan)
    size_weighted_rescue_fraction = _float(rescue_metrics.get("taker_size_weighted_rescue_fraction"), math.nan)
    depth_gate_passed = math.isfinite(min_depth) and min_depth >= cfg.min_taker_rescue_depth_fraction - 1e-12
    residual_gate_passed = (
        math.isfinite(residual_loss_fraction)
        and residual_loss_fraction <= cfg.max_latest_taker_residual_loss_fraction
    )

    gates = {
        "income_p05_gate_passed": math.isfinite(income_p05) and income_p05 >= cfg.target_monthly_usdc,
        "sample_hours_gate_passed": duration_hours >= cfg.min_observation_hours,
        "quote_rows_gate_passed": quote_rows >= cfg.min_quote_rows,
        "diversification_gate_passed": unique_markets >= cfg.min_unique_markets,
        "clob_quality_gate_passed": (not cfg.require_clob_book_quality) or clob_quality_rate >= 0.99,
        "book_scenario_gate_passed": book_scenarios >= cfg.min_book_scenarios,
        "taker_rescue_rate_gate_passed": math.isfinite(taker_rate)
        and taker_rate >= cfg.min_taker_rescue_feasible_rate,
        "taker_pair_edge_gate_passed": math.isfinite(min_edge)
        and min_edge >= cfg.min_taker_rescue_pair_edge_per_share - 1e-12,
        "taker_depth_gate_passed": depth_gate_passed or (cfg.allow_partial_taker_rescue and residual_gate_passed),
        "taker_residual_loss_gate_passed": (not cfg.allow_partial_taker_rescue) or residual_gate_passed,
    }
    gates["depth_ready"] = bool(all(gates.values()))
    return {
        "config": asdict(cfg),
        "metrics": {
            "duration_hours": duration_hours,
            "quote_rows": quote_rows,
            "unique_markets_quoted": unique_markets,
            "clob_quality_rate": clob_quality_rate,
            "income_p05_at_required_capture": income_p05,
            "required_capture_rate": cfg.required_capture_rate,
            "taker_rescue_book_scenarios": book_scenarios,
            "taker_rescue_feasible_rate": taker_rate,
            "taker_rescue_min_pair_edge_per_share": min_edge,
            "taker_rescue_min_depth_fraction": min_depth,
            "partial_taker_rescue_allowed": cfg.allow_partial_taker_rescue,
            "taker_size_weighted_rescue_fraction": size_weighted_rescue_fraction,
            "latest_taker_residual_loss_to_zero": residual_loss,
            "latest_taker_residual_loss_fraction": residual_loss_fraction,
        },
        "gates": gates,
        "blockers": _blockers(gates, cfg),
        "status": "depth_ready" if gates["depth_ready"] else "depth_not_ready",
        "safety": "depth readiness audit only; no private keys, order signing, order submission, or cancellation",
    }


def _capture_p05(target_status: dict[str, Any], required_capture_rate: float) -> float:
    stress = target_status.get("capture_stress_grid")
    if isinstance(stress, list):
        for row in stress:
            if isinstance(row, dict) and abs(_float(row.get("capture_rate"), -1.0) - required_capture_rate) < 1e-12:
                return _float(row.get("captured_net_monthly_p05"), math.nan)
    bootstrap = _dict(target_status.get("bootstrap_target"))
    if abs(_float(bootstrap.get("capture_rate"), -1.0) - required_capture_rate) < 1e-12:
        return _float(bootstrap.get("captured_net_monthly_p05"), math.nan)
    raw = _float(bootstrap.get("net_monthly_p05"), math.nan)
    return raw * max(0.0, min(1.0, required_capture_rate)) if math.isfinite(raw) else math.nan


def _blockers(gates: dict[str, bool], cfg: DepthReadinessConfig) -> list[str]:
    labels = {
        "income_p05_gate_passed": f"{cfg.required_capture_rate:.0%} capture p05 income below ${cfg.target_monthly_usdc:,.0f}/month",
        "sample_hours_gate_passed": f"needs at least {cfg.min_observation_hours:.2f} CLOB-depth observation hours",
        "quote_rows_gate_passed": f"needs at least {cfg.min_quote_rows} CLOB-depth quote rows",
        "diversification_gate_passed": f"needs at least {cfg.min_unique_markets} unique markets",
        "clob_quality_gate_passed": "quote evidence is not CLOB-depth quality",
        "book_scenario_gate_passed": f"needs at least {cfg.min_book_scenarios} taker-rescue book scenarios",
        "taker_rescue_rate_gate_passed": f"taker rescue feasible rate below {cfg.min_taker_rescue_feasible_rate:.0%}",
        "taker_pair_edge_gate_passed": "taker rescue pair edge below requirement",
        "taker_depth_gate_passed": "taker rescue displayed depth below required quote size and partial-residual cap did not pass",
        "taker_residual_loss_gate_passed": (
            f"latest partial taker-rescue residual loss exceeds "
            f"{cfg.max_latest_taker_residual_loss_fraction:.0%} of capital"
        ),
    }
    return [message for gate, message in labels.items() if not gates.get(gate, False)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
