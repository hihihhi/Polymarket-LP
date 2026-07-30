from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class DeploymentReadinessConfig:
    """Final gate for the LP monthly-income objective.

    This is deliberately an auditor, not an execution component. It combines
    public-paper target diagnostics with local execution/reward telemetry and
    returns explicit blockers. It never signs, submits, or cancels orders.
    """

    initial_capital: float = 2_000.0
    target_monthly_usdc: float = 1_000.0
    required_capture_rate: float = 0.5
    min_target_margin: float = 1.0
    min_observation_hours: float = 24.0
    min_unique_markets: int = 2
    max_active_pair_notional: float = 1_600.0
    max_pending_quote_rate: float = 0.05
    max_abs_mid_change_to_next: float = math.inf
    min_cash_reserve_fraction: float = 0.20
    require_telemetry: bool = True


def evaluate_deployment_readiness(
    *,
    target_status: dict[str, Any],
    telemetry_audit: dict[str, Any] | None = None,
    cfg: DeploymentReadinessConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or DeploymentReadinessConfig()
    monitor = _dict(target_status.get("target_monitor"))
    paper = _dict(target_status.get("paper_summary"))
    bootstrap = _dict(target_status.get("bootstrap_target"))
    target_gates = _dict(monitor.get("gates"))
    target_math = _dict(monitor.get("target_math"))
    monitor_input = _dict(monitor.get("input"))

    observed_hours = _float(
        monitor_input.get("duration_hours", paper.get("duration_hours")), 0.0
    )
    unique_markets = int(
        _float(
            monitor_input.get(
                "unique_markets_quoted", paper.get("unique_markets_quoted")
            ),
            0.0,
        )
    )
    max_active = _float(
        monitor_input.get(
            "max_active_pair_notional", paper.get("max_active_pair_notional")
        ),
        math.inf,
    )
    cash_reserve = (
        cfg.initial_capital - max_active if math.isfinite(max_active) else -math.inf
    )
    cash_reserve_fraction = (
        cash_reserve / cfg.initial_capital if cfg.initial_capital > 0 else -math.inf
    )
    net_monthly = _float(target_math.get("net_monthly_after_loss_haircut"), math.nan)
    capture_needed = _float(target_math.get("capture_needed_for_target"), math.inf)
    fill_proxy = _float(
        monitor_input.get("fill_proxy_rate", paper.get("fill_proxy_rate")), math.inf
    )
    stale_fill = _float(
        monitor_input.get("stale_fill_rate", paper.get("stale_fill_rate")), math.inf
    )
    pending_quote = _float(
        monitor_input.get("pending_quote_rate", paper.get("pending_quote_rate")), 0.0
    )
    max_abs_mid_change = _float(paper.get("max_abs_mid_change_to_next"), 0.0)

    captured_p05 = _capture_p05(target_status, cfg.required_capture_rate)
    target_threshold = cfg.target_monthly_usdc * cfg.min_target_margin
    telemetry = _dict(telemetry_audit)
    telemetry_gates = _dict(telemetry.get("gates"))
    telemetry_passed = bool(telemetry_gates.get("deployment_telemetry_passed", False))

    gates = {
        "density_gate_passed": bool(target_gates.get("density_gate_passed", False)),
        "capture_gate_passed": bool(target_gates.get("capture_gate_passed", False)),
        "risk_proxy_gate_passed": bool(
            target_gates.get("risk_proxy_gate_passed", False)
        ),
        "pending_quote_gate_passed": bool(pending_quote <= cfg.max_pending_quote_rate),
        "mid_move_gate_passed": bool(
            max_abs_mid_change <= cfg.max_abs_mid_change_to_next
        ),
        "diversification_gate_passed": bool(
            target_gates.get(
                "diversification_gate_passed", unique_markets >= cfg.min_unique_markets
            )
        ),
        "active_notional_gate_passed": bool(
            target_gates.get(
                "active_notional_gate_passed",
                max_active <= cfg.max_active_pair_notional,
            )
        ),
        "cash_reserve_gate_passed": bool(
            cash_reserve_fraction >= cfg.min_cash_reserve_fraction
        ),
        "sample_gate_passed": bool(
            target_gates.get(
                "sample_gate_passed", observed_hours >= cfg.min_observation_hours
            )
        ),
        "capture_stress_gate_passed": bool(
            math.isfinite(captured_p05) and captured_p05 >= target_threshold
        ),
        "telemetry_gate_passed": bool((not cfg.require_telemetry) or telemetry_passed),
    }
    gates["deployment_ready"] = bool(all(gates.values()))
    blockers = _blockers(gates, cfg)
    return {
        "config": asdict(cfg),
        "metrics": {
            "duration_hours": observed_hours,
            "unique_markets_quoted": unique_markets,
            "max_active_pair_notional": max_active,
            "cash_reserve_usdc": cash_reserve,
            "cash_reserve_fraction": cash_reserve_fraction,
            "net_monthly_after_loss_haircut": net_monthly,
            "capture_needed_for_target": capture_needed,
            "required_capture_rate": cfg.required_capture_rate,
            "captured_p05_net_monthly_at_required_capture": captured_p05,
            "target_threshold_with_margin": target_threshold,
            "fill_proxy_rate": fill_proxy,
            "stale_fill_rate": stale_fill,
            "pending_quote_rate": pending_quote,
            "max_abs_mid_change_to_next": max_abs_mid_change,
            "bootstrap_intervals": int(_float(bootstrap.get("intervals"), 0.0)),
        },
        "gates": gates,
        "blockers": blockers,
        "status": "deployment_ready"
        if gates["deployment_ready"]
        else "deployment_not_ready",
        "safety": "readiness audit only; no private keys, order signing, order submission, or cancellation",
    }


def _capture_p05(target_status: dict[str, Any], required_capture_rate: float) -> float:
    stress = target_status.get("capture_stress_grid")
    if isinstance(stress, list):
        for row in stress:
            if (
                isinstance(row, dict)
                and abs(_float(row.get("capture_rate"), -1.0) - required_capture_rate)
                < 1e-12
            ):
                return _float(row.get("captured_net_monthly_p05"), math.nan)
    bootstrap = _dict(target_status.get("bootstrap_target"))
    if abs(_float(bootstrap.get("capture_rate"), -1.0) - required_capture_rate) < 1e-12:
        return _float(bootstrap.get("captured_net_monthly_p05"), math.nan)
    raw = _float(bootstrap.get("net_monthly_p05"), math.nan)
    return (
        raw * max(0.0, min(1.0, required_capture_rate))
        if math.isfinite(raw)
        else math.nan
    )


def _blockers(gates: dict[str, bool], cfg: DeploymentReadinessConfig) -> list[str]:
    names = {
        "density_gate_passed": "target reward density below requirement",
        "capture_gate_passed": "point-estimate capture requirement too high",
        "risk_proxy_gate_passed": "fill/stale/pending quote risk proxy failed",
        "pending_quote_gate_passed": f"pending quote rate exceeds {cfg.max_pending_quote_rate:.2%}",
        "mid_move_gate_passed": f"max next-mid move exceeds {cfg.max_abs_mid_change_to_next:.4f}",
        "diversification_gate_passed": f"needs at least {cfg.min_unique_markets} quoted markets",
        "active_notional_gate_passed": f"active pair notional exceeds {cfg.max_active_pair_notional:,.2f}",
        "cash_reserve_gate_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.2%} of capital",
        "sample_gate_passed": f"needs at least {cfg.min_observation_hours:.2f} observation hours",
        "capture_stress_gate_passed": f"{cfg.required_capture_rate:.0%} capture-stress p05 below target",
        "telemetry_gate_passed": "order/fill/cancel/paid-reward telemetry audit not passed",
    }
    return [message for gate, message in names.items() if not gates.get(gate, False)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
