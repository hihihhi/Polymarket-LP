from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class CompletionAuditConfig:
    """Terminal proof gate for the managed $1k/month objective."""

    require_objective_proven: bool = True
    require_sustainability_stress: bool = True
    require_risk_governor: bool = True
    require_deployment_allowed: bool = True


def evaluate_completion_audit(
    *,
    objective_audit: dict[str, Any],
    sustainability_stress: dict[str, Any],
    risk_governor: dict[str, Any],
    cfg: CompletionAuditConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or CompletionAuditConfig()
    objective = _dict(objective_audit)
    sustain = _dict(sustainability_stress)
    governor = _dict(risk_governor)
    objective_metrics = _dict(objective.get("metrics"))
    governor_metrics = _dict(governor.get("metrics"))
    sustain_metrics = _dict(sustain.get("metrics"))

    gates = {
        "objective_proven": bool(objective.get("objective_proven", False)),
        "sustainability_stress_passed": sustain.get("status") == "sustainability_stress_passed",
        "risk_governor_core_passed": bool(governor.get("risk_core_passed", False)),
        "deployment_allowed": bool(governor.get("deployment_allowed", False)),
    }
    required = {
        "objective_proven": cfg.require_objective_proven,
        "sustainability_stress_passed": cfg.require_sustainability_stress,
        "risk_governor_core_passed": cfg.require_risk_governor,
        "deployment_allowed": cfg.require_deployment_allowed,
    }
    blockers = _blockers(gates, required)
    completion_proven = not blockers
    return {
        "config": asdict(cfg),
        "status": "completion_proven" if completion_proven else "completion_not_proven",
        "completion_proven": completion_proven,
        "metrics": {
            "selected_qsize": governor_metrics.get("selected_qsize", objective_metrics.get("selected_qsize")),
            "governed_qsize": governor_metrics.get("recommended_qsize"),
            "objective_packet_50pct_p05": objective_metrics.get("packet_50pct_capture_p05"),
            "governor_50pct_p05": governor_metrics.get("governing_50pct_p05_monthly_income"),
            "sustainability_reference_income": sustain_metrics.get("reference_monthly_income"),
            "sustainability_after_cap_loss": sustain_metrics.get("configured_cap_reference_monthly_after_loss"),
            "cash_reserve_fraction": governor_metrics.get("cash_reserve_fraction"),
            "unhedged_loss_usdc": governor_metrics.get("unhedged_loss_usdc"),
            "configured_cap_loss_usdc": governor_metrics.get("configured_cap_loss_usdc"),
            "configured_cap_recovery_days": governor_metrics.get("configured_cap_recovery_days"),
        },
        "gates": gates,
        "required_gates": required,
        "blockers": blockers,
        "safety": "completion audit only; no private keys, signing, order submission, or cancellation",
    }


def _blockers(gates: dict[str, bool], required: dict[str, bool]) -> list[str]:
    labels = {
        "objective_proven": "objective proof audit is not proven",
        "sustainability_stress_passed": "sustainability stress is not passed",
        "risk_governor_core_passed": "risk governor core gates are not passed",
        "deployment_allowed": "deployment is not allowed by risk governor",
    }
    return [
        labels[name]
        for name, needed in required.items()
        if needed and not gates.get(name, False)
    ]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
