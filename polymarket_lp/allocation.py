from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class AllocationSelectionConfig:
    """Select the LP quote-size profile that best satisfies income and survival gates.

    The selector is intentionally data-driven: reports/scripts provide the frontier
    rows and all thresholds. It does not assume q300/q800 or any fixed market.
    """

    initial_capital: float = 2_000.0
    target_monthly_usdc: float = 1_000.0
    min_target_margin: float = 1.0
    min_unique_markets: int = 2
    min_cash_reserve_fraction: float = 0.40
    max_unhedged_loss_fraction: float = 0.50
    max_configured_cap_loss_fraction: float = 0.25
    max_configured_cap_recovery_days: float = 10.0
    max_abs_mid_change_to_next: float = 0.011
    objective: str = "balanced"


def select_allocation(
    frontier_rows: list[dict[str, Any]],
    cfg: AllocationSelectionConfig | None = None,
) -> dict[str, Any]:
    """Return a reproducible allocation choice from quote-size frontier rows."""

    cfg = cfg or AllocationSelectionConfig()
    evaluated = [evaluate_allocation_row(row, cfg) for row in frontier_rows]
    passed = [row for row in evaluated if row["gates"]["allocation_gate_passed"]]
    income_passed = [
        row
        for row in evaluated
        if row["gates"]["income_gate_passed"]
        and row["gates"]["capital_stress_gate_passed"]
    ]
    selected = _select_passed(passed, cfg)
    income_max = max(
        income_passed,
        key=lambda row: _num(row["metrics"].get("captured_net_monthly_p05")),
        default=None,
    )
    return {
        "config": asdict(cfg),
        "status": "allocation_selected" if selected else "no_allocation_passed",
        "selected": selected,
        "income_max": income_max,
        "rows": evaluated,
        "blockers": [] if selected else _aggregate_blockers(evaluated),
        "safety": "allocation selector only; no private keys, signing, order submission, or cancellation",
    }


def evaluate_allocation_row(
    row: dict[str, Any], cfg: AllocationSelectionConfig
) -> dict[str, Any]:
    qsize = _num(row.get("qsize"))
    captured_p05 = _num(row.get("captured_net_monthly_p05"))
    configured_cap_loss = _num(row.get("configured_cap_loss"))
    configured_cap_fraction = (
        configured_cap_loss / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf
    )
    metrics = {
        "qsize": int(qsize) if math.isfinite(qsize) else row.get("qsize"),
        "quote_offset": _num(row.get("quote_offset")),
        "min_reward_density_per_day": _num(row.get("min_reward_density_per_day")),
        "quote_rows": int(_num(row.get("quote_rows"), 0.0)),
        "unique_markets_quoted": int(_num(row.get("unique_markets_quoted"), 0.0)),
        "duration_hours": _num(row.get("duration_hours")),
        "avg_active_pair_notional": _num(row.get("avg_active_pair_notional")),
        "net_monthly_after_loss_haircut": _num(
            row.get("net_monthly_after_loss_haircut")
        ),
        "captured_net_monthly_p05": captured_p05,
        "capture_needed_for_target": _num(row.get("capture_needed_for_target")),
        "cash_reserve_fraction": _num(row.get("cash_reserve_fraction")),
        "unhedged_loss_to_zero": _num(row.get("unhedged_loss_to_zero")),
        "unhedged_loss_fraction": _num(row.get("unhedged_loss_fraction")),
        "unhedged_recovery_days": _num(row.get("unhedged_recovery_days")),
        "configured_cap_loss": configured_cap_loss,
        "configured_cap_loss_fraction": configured_cap_fraction,
        "configured_cap_recovery_days": _num(row.get("configured_cap_recovery_days")),
        "max_abs_mid_change_to_next": _num(row.get("max_abs_mid_change_to_next")),
        "fill_proxy_rate": _num(row.get("fill_proxy_rate")),
        "stale_fill_rate": _num(row.get("stale_fill_rate")),
        "pending_quote_rate": _num(row.get("pending_quote_rate")),
    }
    gates = {
        "frontier_selected_gate_passed": _bool(row.get("selected_gate_passed")),
        "capital_stress_gate_passed": _bool(row.get("capital_risk_stress_passed")),
        "income_gate_passed": captured_p05
        >= cfg.target_monthly_usdc * cfg.min_target_margin,
        "diversification_gate_passed": metrics["unique_markets_quoted"]
        >= cfg.min_unique_markets,
        "cash_reserve_gate_passed": metrics["cash_reserve_fraction"]
        >= cfg.min_cash_reserve_fraction,
        "unhedged_loss_gate_passed": metrics["unhedged_loss_fraction"]
        <= cfg.max_unhedged_loss_fraction,
        "configured_cap_loss_gate_passed": configured_cap_fraction
        <= cfg.max_configured_cap_loss_fraction,
        "configured_cap_recovery_gate_passed": metrics["configured_cap_recovery_days"]
        <= cfg.max_configured_cap_recovery_days,
        "mid_move_gate_passed": metrics["max_abs_mid_change_to_next"]
        <= cfg.max_abs_mid_change_to_next,
    }
    gates["allocation_gate_passed"] = bool(all(gates.values()))
    return {
        "metrics": metrics,
        "gates": gates,
        "blockers": _row_blockers(gates, cfg),
        "raw": row,
    }


def _select_passed(
    passed: list[dict[str, Any]],
    cfg: AllocationSelectionConfig,
) -> dict[str, Any] | None:
    if not passed:
        return None
    objective = cfg.objective.strip().lower()
    if objective == "income":
        return max(
            passed, key=lambda row: _num(row["metrics"].get("captured_net_monthly_p05"))
        )
    if objective == "sustainable":
        return min(
            passed,
            key=lambda row: (
                _num(row["metrics"].get("unhedged_loss_fraction")),
                _num(row["metrics"].get("configured_cap_recovery_days")),
                -_num(row["metrics"].get("captured_net_monthly_p05")),
            ),
        )
    if objective != "balanced":
        raise ValueError("objective must be one of: balanced, sustainable, income")
    return max(
        passed,
        key=lambda row: (
            _risk_adjusted_income(row),
            -_num(row["metrics"].get("unhedged_loss_fraction")),
            _num(row["metrics"].get("captured_net_monthly_p05")),
        ),
    )


def _risk_adjusted_income(row: dict[str, Any]) -> float:
    metrics = row["metrics"]
    income = _num(metrics.get("captured_net_monthly_p05"))
    risk = max(_num(metrics.get("unhedged_loss_fraction")), 1e-9)
    return income / risk


def _row_blockers(gates: dict[str, bool], cfg: AllocationSelectionConfig) -> list[str]:
    names = {
        "frontier_selected_gate_passed": "frontier economics/quality selector failed",
        "capital_stress_gate_passed": "capital stress failed",
        "income_gate_passed": f"50% p05 income below ${cfg.target_monthly_usdc:,.0f} target",
        "diversification_gate_passed": f"fewer than {cfg.min_unique_markets} markets",
        "cash_reserve_gate_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.0%}",
        "unhedged_loss_gate_passed": f"unhedged one-side loss above {cfg.max_unhedged_loss_fraction:.0%}",
        "configured_cap_loss_gate_passed": f"configured-cap loss above {cfg.max_configured_cap_loss_fraction:.0%}",
        "configured_cap_recovery_gate_passed": f"configured-cap recovery above {cfg.max_configured_cap_recovery_days:.1f} days",
        "mid_move_gate_passed": f"next-mid move above {cfg.max_abs_mid_change_to_next:.4f}",
    }
    return [message for gate, message in names.items() if not gates.get(gate, False)]


def _aggregate_blockers(rows: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        for blocker in row["blockers"]:
            counts[blocker] = counts.get(blocker, 0) + 1
    return [
        f"{blocker} ({count} candidates)" for blocker, count in sorted(counts.items())
    ]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
