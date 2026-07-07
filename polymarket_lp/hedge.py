from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class HedgeFeasibilityConfig:
    """Audit whether LP inventory loss is hedgeable/capped.

    This is a read-only feasibility auditor. It distinguishes perfect external
    hedges from the internal hedge actually available to Polymarket LP: paired
    YES/NO fills, inventory caps, and emergency exits within pair-edge cushion.
    """

    initial_capital: float = 2_000.0
    min_loss_reduction_fraction: float = 0.50
    max_configured_cap_loss_fraction: float = 0.25
    max_configured_cap_recovery_days: float = 10.0
    min_pair_edge_usdc: float = 0.0
    max_pair_cost_per_share: float = 1.0
    exit_slippage: float = 0.005
    min_slippage_cushion_multiplier: float = 1.0
    external_hedge_available: bool = False


def evaluate_hedge_feasibility(
    *,
    capital_risk: dict[str, Any],
    evidence_packet: dict[str, Any] | None = None,
    cfg: HedgeFeasibilityConfig | None = None,
) -> dict[str, Any]:
    """Return hedge feasibility, separating partial internal hedge from full hedge."""

    cfg = cfg or HedgeFeasibilityConfig()
    cap_metrics = _dict(capital_risk.get("metrics"))
    cap_config = _dict(capital_risk.get("config"))
    cap_gates = _dict(capital_risk.get("gates"))
    market_stress = capital_risk.get("market_stress")
    markets = market_stress if isinstance(market_stress, list) else []

    initial_capital = _num(cap_config.get("initial_capital"), cfg.initial_capital)
    exit_slippage = _num(cap_config.get("exit_slippage"), cfg.exit_slippage)
    unhedged_loss = _num(cap_metrics.get("all_active_unhedged_one_side_loss_to_zero"))
    configured_cap_loss = _num(cap_metrics.get("configured_inventory_cap_loss_to_zero"))
    configured_cap_recovery_days = _num(cap_metrics.get("capped_recovery_days_at_p05_income"))
    max_pair_cost = _num(cap_metrics.get("max_pair_cost_per_share"))
    min_pair_edge = _num(cap_metrics.get("min_locked_pair_edge_usdc"))
    if not math.isfinite(min_pair_edge) and markets:
        min_pair_edge = min(_num(row.get("locked_pair_edge_usdc")) for row in markets)
    pair_lock_edge_total = sum(
        x for x in (_num(row.get("locked_pair_edge_usdc")) for row in markets) if math.isfinite(x)
    )
    pair_slippage_cushion = max(0.0, 1.0 - max_pair_cost) if math.isfinite(max_pair_cost) else math.nan
    loss_reduction_fraction = (
        1.0 - configured_cap_loss / unhedged_loss
        if unhedged_loss > 0 and math.isfinite(unhedged_loss) and math.isfinite(configured_cap_loss)
        else math.nan
    )

    gates = {
        "pair_lock_when_both_sides_fill_passed": min_pair_edge >= cfg.min_pair_edge_usdc
        and max_pair_cost <= cfg.max_pair_cost_per_share,
        "emergency_exit_slippage_cushion_passed": pair_slippage_cushion
        >= exit_slippage * cfg.min_slippage_cushion_multiplier,
        "configured_cap_tail_reduction_passed": loss_reduction_fraction >= cfg.min_loss_reduction_fraction,
        "configured_cap_loss_passed": (configured_cap_loss / initial_capital) <= cfg.max_configured_cap_loss_fraction
        if initial_capital > 0 and math.isfinite(configured_cap_loss)
        else False,
        "configured_cap_recovery_passed": configured_cap_recovery_days <= cfg.max_configured_cap_recovery_days,
        "no_total_ruin_unhedged_gate_passed": bool(
            cap_gates.get("no_total_ruin_unhedged_gate_passed", unhedged_loss < initial_capital)
        ),
        "perfect_external_hedge_available": bool(cfg.external_hedge_available),
    }
    core_gate_names = [
        "pair_lock_when_both_sides_fill_passed",
        "emergency_exit_slippage_cushion_passed",
        "configured_cap_tail_reduction_passed",
        "configured_cap_loss_passed",
        "configured_cap_recovery_passed",
        "no_total_ruin_unhedged_gate_passed",
    ]
    gates["partial_internal_hedge_feasible"] = all(gates[name] for name in core_gate_names)
    gates["perfect_hedge_feasible"] = gates["partial_internal_hedge_feasible"] and gates[
        "perfect_external_hedge_available"
    ]

    if gates["perfect_hedge_feasible"]:
        status = "perfect_hedge_feasible"
    elif gates["partial_internal_hedge_feasible"]:
        status = "partial_internal_hedge_feasible"
    else:
        status = "hedge_not_feasible"

    packet_risk = _dict(_dict(evidence_packet).get("risk"))
    return {
        "config": asdict(cfg),
        "status": status,
        "metrics": {
            "initial_capital": initial_capital,
            "unhedged_one_side_loss_usdc": unhedged_loss,
            "configured_cap_loss_usdc": configured_cap_loss,
            "configured_cap_loss_fraction": configured_cap_loss / initial_capital if initial_capital > 0 else math.inf,
            "configured_cap_recovery_days": configured_cap_recovery_days,
            "configured_cap_loss_reduction_fraction": loss_reduction_fraction,
            "max_pair_cost_per_share": max_pair_cost,
            "min_pair_lock_edge_usdc": min_pair_edge,
            "pair_lock_edge_total_usdc": pair_lock_edge_total,
            "pair_slippage_cushion_per_share": pair_slippage_cushion,
            "exit_slippage_assumption": exit_slippage,
            "packet_cash_reserve_fraction": _num(packet_risk.get("cash_reserve_fraction")),
        },
        "gates": gates,
        "blockers": _blockers(gates, cfg),
        "interpretation": {
            "monthly_amounts_are_profit_not_capital": True,
            "hedge_type": "internal_pair_lock_plus_inventory_caps",
            "external_hedge_note": "not assumed available for non-sports/non-crypto event markets",
            "full_hedge_requires": [
                "both YES/NO sides filled below $1 total cost",
                "real order/fill/cancel telemetry",
                "paid reward telemetry",
                "caps preventing one-sided inventory from exceeding configured loss",
            ],
        },
        "safety": "hedge feasibility audit only; no private keys, signing, order submission, or cancellation",
    }


def _blockers(gates: dict[str, bool], cfg: HedgeFeasibilityConfig) -> list[str]:
    labels = {
        "pair_lock_when_both_sides_fill_passed": "paired YES/NO fill is not locked non-negative",
        "emergency_exit_slippage_cushion_passed": "emergency exit slippage exceeds pair-edge cushion",
        "configured_cap_tail_reduction_passed": f"configured caps reduce unhedged loss by less than {cfg.min_loss_reduction_fraction:.0%}",
        "configured_cap_loss_passed": f"configured-cap loss exceeds {cfg.max_configured_cap_loss_fraction:.0%} of capital",
        "configured_cap_recovery_passed": f"configured-cap recovery exceeds {cfg.max_configured_cap_recovery_days:.1f} days",
        "no_total_ruin_unhedged_gate_passed": "unhedged one-sided fill stress can lose all capital",
    }
    blockers = [message for gate, message in labels.items() if not gates.get(gate, False)]
    if gates.get("partial_internal_hedge_feasible") and not gates.get("perfect_external_hedge_available"):
        blockers.append("no perfect external hedge assumed; hedge is partial/internal")
    return blockers


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
