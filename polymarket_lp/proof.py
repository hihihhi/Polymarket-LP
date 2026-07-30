from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ObjectiveProofConfig:
    """Completion audit for the $1k/month sustainable LP objective."""

    target_monthly_usdc: float = 1_000.0
    require_allocation_selected: bool = True
    require_24h_sample: bool = True
    require_rolling_all_pass: bool = True
    require_execution_telemetry: bool = True
    require_deployment_ready: bool = True
    max_unhedged_loss_fraction: float = 0.50
    max_configured_cap_recovery_days: float = 10.0
    min_cash_reserve_fraction: float = 0.40


def evaluate_objective_proof(
    *,
    evidence_packet: dict[str, Any],
    allocation_selection: dict[str, Any],
    cfg: ObjectiveProofConfig | None = None,
) -> dict[str, Any]:
    """Audit whether current evidence proves the requested objective."""

    cfg = cfg or ObjectiveProofConfig()
    packet_gates = _dict(evidence_packet.get("gates"))
    income = _dict(evidence_packet.get("income"))
    risk = _dict(evidence_packet.get("risk"))
    selected = _dict(allocation_selection.get("selected"))
    selected_metrics = _dict(selected.get("metrics"))
    selected_gates = _dict(selected.get("gates"))

    metrics = {
        "target_monthly_usdc": cfg.target_monthly_usdc,
        "allocation_status": allocation_selection.get("status"),
        "selected_qsize": selected_metrics.get("qsize"),
        "selected_50pct_capture_p05": _num(
            selected_metrics.get("captured_net_monthly_p05")
        ),
        "selected_net_monthly_after_loss_haircut": _num(
            selected_metrics.get("net_monthly_after_loss_haircut")
        ),
        "selected_cash_reserve_fraction": _num(
            selected_metrics.get("cash_reserve_fraction")
        ),
        "selected_unhedged_loss_fraction": _num(
            selected_metrics.get("unhedged_loss_fraction")
        ),
        "selected_configured_cap_recovery_days": _num(
            selected_metrics.get("configured_cap_recovery_days")
        ),
        "packet_status": evidence_packet.get("status"),
        "packet_observation_hours": _num(income.get("observation_hours")),
        "packet_50pct_capture_p05": _num(income.get("p05_monthly_50pct_capture")),
        "packet_net_monthly_after_loss_haircut": _num(
            income.get("net_monthly_after_loss_haircut")
        ),
        "packet_cash_reserve_fraction": _num(risk.get("cash_reserve_fraction")),
        "packet_unhedged_loss_fraction": _num(
            risk.get("unhedged_loss_fraction_of_capital")
        ),
    }
    gates = {
        "allocation_selected": allocation_selection.get("status")
        == "allocation_selected",
        "allocation_income_above_target": metrics["selected_50pct_capture_p05"]
        >= cfg.target_monthly_usdc,
        "packet_income_above_target": metrics["packet_50pct_capture_p05"]
        >= cfg.target_monthly_usdc,
        "cash_reserve_passed": (
            bool(packet_gates.get("cash_reserve_passed"))
            and metrics["selected_cash_reserve_fraction"]
            >= cfg.min_cash_reserve_fraction
        ),
        "capital_stress_passed": bool(packet_gates.get("capital_stress_passed")),
        "selected_unhedged_loss_passed": (
            bool(selected_gates.get("unhedged_loss_gate_passed"))
            and metrics["selected_unhedged_loss_fraction"]
            <= cfg.max_unhedged_loss_fraction
        ),
        "selected_cap_recovery_passed": (
            bool(selected_gates.get("configured_cap_recovery_gate_passed"))
            and metrics["selected_configured_cap_recovery_days"]
            <= cfg.max_configured_cap_recovery_days
        ),
        "sample_24h_passed": bool(packet_gates.get("sample_24h_passed")),
        "rolling_all_windows_passed": bool(
            packet_gates.get("rolling_all_windows_passed")
        ),
        "telemetry_passed": bool(packet_gates.get("telemetry_passed")),
        "deployment_ready": bool(packet_gates.get("deployment_ready")),
    }
    required = {
        "allocation_selected": cfg.require_allocation_selected,
        "allocation_income_above_target": True,
        "packet_income_above_target": True,
        "cash_reserve_passed": True,
        "capital_stress_passed": True,
        "selected_unhedged_loss_passed": True,
        "selected_cap_recovery_passed": True,
        "sample_24h_passed": cfg.require_24h_sample,
        "rolling_all_windows_passed": cfg.require_rolling_all_pass,
        "telemetry_passed": cfg.require_execution_telemetry,
        "deployment_ready": cfg.require_deployment_ready,
    }
    blockers = _blockers(gates, required, cfg)
    objective_proven = not blockers
    return {
        "config": asdict(cfg),
        "status": "objective_proven" if objective_proven else "objective_not_proven",
        "objective_proven": objective_proven,
        "metrics": metrics,
        "gates": gates,
        "required_gates": required,
        "blockers": blockers,
        "safety": "objective proof audit only; no private keys, signing, order submission, or cancellation",
    }


def _blockers(
    gates: dict[str, bool],
    required: dict[str, bool],
    cfg: ObjectiveProofConfig,
) -> list[str]:
    labels = {
        "allocation_selected": "no selected allocation",
        "allocation_income_above_target": f"selected allocation 50% p05 income below ${cfg.target_monthly_usdc:,.0f}",
        "packet_income_above_target": f"live/paper evidence 50% p05 income below ${cfg.target_monthly_usdc:,.0f}",
        "cash_reserve_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.0%}",
        "capital_stress_passed": "capital stress gate failed",
        "selected_unhedged_loss_passed": f"selected unhedged loss exceeds {cfg.max_unhedged_loss_fraction:.0%}",
        "selected_cap_recovery_passed": f"selected cap recovery exceeds {cfg.max_configured_cap_recovery_days:.1f} days",
        "sample_24h_passed": "needs >=24h public-paper observation",
        "rolling_all_windows_passed": "rolling-window persistence is not all-pass",
        "telemetry_passed": "order/fill/cancel/paid-reward telemetry missing",
        "deployment_ready": "deployment readiness gate is false",
    }
    return [
        labels[name]
        for name, needed in required.items()
        if needed and not gates.get(name, False)
    ]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
