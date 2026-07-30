from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class RiskGovernorConfig:
    """Capital-preservation governor for LP quote sizing and deployment state."""

    initial_capital: float = 2_000.0
    target_monthly_usdc: float = 1_000.0
    min_cash_reserve_fraction: float = 0.40
    max_unhedged_loss_fraction: float = 0.50
    max_configured_cap_loss_fraction: float = 0.25
    max_configured_cap_recovery_days: float = 10.0
    min_income_margin: float = 1.0
    min_sizing_scale: float = 0.25
    require_objective_proven_for_deployment: bool = True


def evaluate_risk_governor(
    *,
    evidence_packet: dict[str, Any],
    allocation_selection: dict[str, Any],
    objective_audit: dict[str, Any] | None = None,
    sustainability_stress: dict[str, Any] | None = None,
    cfg: RiskGovernorConfig | None = None,
) -> dict[str, Any]:
    """Return operating state and quote-size scaling under explicit risk limits."""

    cfg = cfg or RiskGovernorConfig()
    selected = _dict(_dict(allocation_selection.get("selected")).get("metrics"))
    packet_income = _dict(evidence_packet.get("income"))
    packet_risk = _dict(evidence_packet.get("risk"))
    packet_gates = _dict(evidence_packet.get("gates"))
    objective = _dict(objective_audit)
    sustain = _dict(sustainability_stress)

    selected_qsize = _num(selected.get("qsize"))
    selected_income = _num(selected.get("captured_net_monthly_p05"))
    packet_income_p05 = _num(packet_income.get("p05_monthly_50pct_capture"))
    governing_income = _finite_min(selected_income, packet_income_p05)
    active_notional = _num(
        packet_risk.get("max_active_pair_notional"),
        _num(selected.get("avg_active_pair_notional")),
    )
    cash_reserve_fraction = _num(
        packet_risk.get("cash_reserve_fraction"),
        _num(selected.get("cash_reserve_fraction")),
    )
    unhedged_loss = _num(
        packet_risk.get("all_active_unhedged_one_side_loss_to_zero"),
        _num(selected.get("unhedged_loss_to_zero")),
    )
    configured_cap_loss = _num(
        packet_risk.get("configured_inventory_cap_loss_to_zero"),
        _num(selected.get("configured_cap_loss")),
    )
    configured_cap_recovery_days = _num(
        packet_risk.get("configured_cap_recovery_days"),
        _num(selected.get("configured_cap_recovery_days")),
    )
    max_active_by_cash = cfg.initial_capital * (1.0 - cfg.min_cash_reserve_fraction)
    max_unhedged_loss = cfg.initial_capital * cfg.max_unhedged_loss_fraction
    max_configured_loss = cfg.initial_capital * cfg.max_configured_cap_loss_fraction
    max_configured_loss_by_recovery = _max_loss_by_recovery(governing_income, cfg)

    scale_limits = {
        "cash_reserve": _ratio(max_active_by_cash, active_notional),
        "unhedged_loss": _ratio(max_unhedged_loss, unhedged_loss),
        "configured_cap_loss": _ratio(max_configured_loss, configured_cap_loss),
        "configured_cap_recovery": _ratio(
            max_configured_loss_by_recovery, configured_cap_loss
        ),
    }
    raw_scale = min([1.0, *[v for v in scale_limits.values() if math.isfinite(v)]])
    recommended_scale = max(0.0, min(1.0, raw_scale))
    recommended_qsize = (
        math.floor(selected_qsize * recommended_scale)
        if math.isfinite(selected_qsize)
        else math.nan
    )
    scale_viable = recommended_scale >= cfg.min_sizing_scale

    gates = {
        "allocation_selected": allocation_selection.get("status")
        == "allocation_selected",
        "income_gate_passed": governing_income
        >= cfg.target_monthly_usdc * cfg.min_income_margin,
        "cash_reserve_gate_passed": cash_reserve_fraction
        >= cfg.min_cash_reserve_fraction,
        "unhedged_loss_gate_passed": (unhedged_loss / cfg.initial_capital)
        <= cfg.max_unhedged_loss_fraction
        if cfg.initial_capital > 0
        else False,
        "configured_cap_loss_gate_passed": (configured_cap_loss / cfg.initial_capital)
        <= cfg.max_configured_cap_loss_fraction
        if cfg.initial_capital > 0
        else False,
        "configured_cap_recovery_gate_passed": configured_cap_recovery_days
        <= cfg.max_configured_cap_recovery_days,
        "sustainability_stress_passed": sustain.get("status")
        in {"sustainability_stress_passed", None},
        "sizing_scale_viable": scale_viable,
        "objective_proven": bool(objective.get("objective_proven", False)),
        "sample_24h_passed": bool(packet_gates.get("sample_24h_passed", False)),
        "rolling_all_windows_passed": bool(
            packet_gates.get("rolling_all_windows_passed", False)
        ),
        "telemetry_passed": bool(packet_gates.get("telemetry_passed", False)),
        "deployment_ready": bool(packet_gates.get("deployment_ready", False)),
    }
    deployment_allowed = (
        gates["objective_proven"] and gates["deployment_ready"]
        if cfg.require_objective_proven_for_deployment
        else gates["deployment_ready"]
    )
    risk_core_passed = all(
        gates[name]
        for name in [
            "allocation_selected",
            "income_gate_passed",
            "cash_reserve_gate_passed",
            "unhedged_loss_gate_passed",
            "configured_cap_loss_gate_passed",
            "configured_cap_recovery_gate_passed",
            "sustainability_stress_passed",
            "sizing_scale_viable",
        ]
    )
    if deployment_allowed:
        action = "deploy_with_governor"
    elif risk_core_passed:
        action = "continue_public_paper_collect_signed_telemetry_next"
    elif scale_viable:
        action = "reduce_size_and_continue_paper"
    else:
        action = "pause_strategy"

    blockers = _blockers(gates, cfg, deployment_allowed)
    return {
        "config": asdict(cfg),
        "status": action,
        "deployment_allowed": deployment_allowed,
        "risk_core_passed": risk_core_passed,
        "metrics": {
            "selected_qsize": selected_qsize,
            "recommended_scale": recommended_scale,
            "recommended_qsize": recommended_qsize,
            "governing_50pct_p05_monthly_income": governing_income,
            "packet_50pct_p05_monthly_income": packet_income_p05,
            "selected_50pct_p05_monthly_income": selected_income,
            "active_pair_notional": active_notional,
            "max_active_pair_notional_by_cash": max_active_by_cash,
            "cash_reserve_fraction": cash_reserve_fraction,
            "unhedged_loss_usdc": unhedged_loss,
            "max_unhedged_loss_usdc": max_unhedged_loss,
            "configured_cap_loss_usdc": configured_cap_loss,
            "max_configured_cap_loss_usdc": max_configured_loss,
            "max_configured_cap_loss_by_recovery_usdc": max_configured_loss_by_recovery,
            "configured_cap_recovery_days": configured_cap_recovery_days,
        },
        "scale_limits": scale_limits,
        "gates": gates,
        "blockers": blockers,
        "safety": "risk governor audit only; no private keys, signing, order submission, or cancellation",
    }


def _max_loss_by_recovery(monthly_income: float, cfg: RiskGovernorConfig) -> float:
    if monthly_income <= 0 or not math.isfinite(monthly_income):
        return 0.0
    return monthly_income / 30.0 * cfg.max_configured_cap_recovery_days


def _ratio(limit: float, value: float) -> float:
    if value <= 0:
        return math.inf
    if not math.isfinite(value) or limit < 0:
        return 0.0
    return limit / value


def _finite_min(*values: float) -> float:
    finite = [x for x in values if math.isfinite(x)]
    return min(finite) if finite else math.nan


def _blockers(
    gates: dict[str, bool], cfg: RiskGovernorConfig, deployment_allowed: bool
) -> list[str]:
    labels = {
        "allocation_selected": "no selected allocation",
        "income_gate_passed": f"50% p05 income below ${cfg.target_monthly_usdc:,.0f}",
        "cash_reserve_gate_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.0%}",
        "unhedged_loss_gate_passed": f"unhedged loss exceeds {cfg.max_unhedged_loss_fraction:.0%} of capital",
        "configured_cap_loss_gate_passed": f"configured-cap loss exceeds {cfg.max_configured_cap_loss_fraction:.0%} of capital",
        "configured_cap_recovery_gate_passed": f"configured-cap recovery exceeds {cfg.max_configured_cap_recovery_days:.1f} days",
        "sustainability_stress_passed": "sustainability stress failed",
        "sizing_scale_viable": f"required scale below {cfg.min_sizing_scale:.0%}; pause instead of trading",
    }
    blockers = [
        message for gate, message in labels.items() if not gates.get(gate, False)
    ]
    if not deployment_allowed:
        for gate, message in {
            "sample_24h_passed": "needs >=24h public-paper sample",
            "rolling_all_windows_passed": "rolling-window persistence not proven",
            "telemetry_passed": "real order/fill/cancel/paid-reward telemetry missing",
            "deployment_ready": "deployment readiness gate false",
        }.items():
            if not gates.get(gate, False):
                blockers.append(message)
    return blockers


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
