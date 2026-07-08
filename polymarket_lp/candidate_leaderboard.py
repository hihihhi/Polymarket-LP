from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class CandidateEvidence:
    name: str
    gate: dict[str, Any]
    metadata: dict[str, Any] | None = None
    drawdown_guard: dict[str, Any] | None = None


def build_candidate_leaderboard(candidates: Iterable[CandidateEvidence]) -> dict[str, Any]:
    """Rank public-paper LP candidates by proof gates, income, and rescue risk.

    This is a read-only evidence combiner. It does not infer fills, paid rewards,
    account balances, private keys, or deployment permission.
    """

    rows = [_candidate_row(candidate) for candidate in candidates]
    rows.sort(key=_rank_key, reverse=True)
    leader = rows[0] if rows else {}
    policy_leaders = _policy_leaders(rows)
    return {
        "status": _status(leader),
        "leader": leader,
        "leader_policy": "risk_first_after_income_and_rescue_gates",
        "policy_leaders": policy_leaders,
        "candidates": rows,
        "safety": (
            "public-paper evidence comparison only; no private keys, signing, "
            "order submission, cancellation, or paid-reward verification"
        ),
    }


def _candidate_row(candidate: CandidateEvidence) -> dict[str, Any]:
    gate = _dict(candidate.gate)
    drawdown = _dict(candidate.drawdown_guard)
    metrics = _dict(gate.get("metrics"))
    gates = _dict(gate.get("gates"))
    drawdown_metrics = _dict(drawdown.get("metrics"))
    drawdown_gates = _dict(drawdown.get("gates"))
    lp_config = _dict(drawdown.get("lp_config"))
    blockers = gate.get("blockers") if isinstance(gate.get("blockers"), list) else []
    drawdown_blockers = drawdown.get("blockers") if isinstance(drawdown.get("blockers"), list) else []
    metadata = _dict(candidate.metadata)
    has_drawdown_guard = bool(drawdown)
    drawdown_core_passed = bool(drawdown.get("risk_core_passed", True)) if has_drawdown_guard else True
    drawdown_sample_passed = (
        bool(drawdown_gates.get("sample_hours_gate_passed", False)) if has_drawdown_guard else True
    )
    drawdown_guard_passed = (
        bool(drawdown_gates.get("drawdown_guard_passed", False)) if has_drawdown_guard else True
    )
    risk_gates = [
        "clob_quality_gate_passed",
        "taker_rescue_rate_gate_passed",
        "taker_pair_edge_gate_passed",
        "taker_depth_gate_passed",
        "taker_residual_loss_gate_passed",
    ]
    sample_gates = ["sample_hours_gate_passed", "quote_rows_gate_passed", "book_scenario_gate_passed"]
    row = {
        "name": candidate.name,
        "status": str(gate.get("status", "unknown")),
        "public_paper_depth_ready": bool(gates.get("depth_ready", False)),
        "income_gate_passed": bool(gates.get("income_p05_gate_passed", False)),
        "risk_gates_passed": all(bool(gates.get(k, False)) for k in risk_gates),
        "sample_gates_passed": all(bool(gates.get(k, False)) for k in sample_gates),
        "has_drawdown_guard": has_drawdown_guard,
        "drawdown_guard_status": str(drawdown.get("status", "not_evaluated")),
        "drawdown_core_passed": drawdown_core_passed,
        "drawdown_sample_passed": drawdown_sample_passed,
        "drawdown_guard_passed": drawdown_guard_passed,
        "duration_hours": _float(metrics.get("duration_hours"), 0.0),
        "quote_rows": int(_float(metrics.get("quote_rows"), 0.0)),
        "unique_markets_quoted": int(_float(metrics.get("unique_markets_quoted"), 0.0)),
        "income_p05_at_required_capture": _float(metrics.get("income_p05_at_required_capture"), math.nan),
        "required_capture_rate": _float(metrics.get("required_capture_rate"), math.nan),
        "taker_rescue_feasible_rate": _float(metrics.get("taker_rescue_feasible_rate"), math.nan),
        "taker_rescue_min_depth_fraction": _float(metrics.get("taker_rescue_min_depth_fraction"), math.nan),
        "taker_size_weighted_rescue_fraction": _float(metrics.get("taker_size_weighted_rescue_fraction"), math.nan),
        "latest_taker_residual_loss_to_zero": _float(metrics.get("latest_taker_residual_loss_to_zero"), math.nan),
        "latest_taker_residual_loss_fraction": _float(metrics.get("latest_taker_residual_loss_fraction"), math.nan),
        "configured_quote_size_shares": _first_float(
            metadata.get("quote_size"),
            metadata.get("quote_size_shares"),
            lp_config.get("quote_size_shares"),
        ),
        "configured_active_capital_limit": _first_float(
            metadata.get("active_capital_limit"),
            lp_config.get("active_capital_limit"),
        ),
        "configured_residual_loss_cap_usdc": _first_float(
            metadata.get("partial_rescue_max_residual_loss_usdc"),
            lp_config.get("partial_rescue_max_residual_loss_usdc"),
        ),
        "configured_max_unpaired_per_market": _first_float(lp_config.get("max_unpaired_per_market")),
        "configured_max_total_unpaired": _first_float(lp_config.get("max_total_unpaired")),
        "configured_max_cluster_unpaired": _first_float(lp_config.get("max_cluster_unpaired")),
        "drawdown_reward_to_trading_loss_ratio": _float(drawdown_metrics.get("reward_to_trading_loss_ratio"), math.nan),
        "drawdown_mtm_fraction": _float(drawdown_metrics.get("max_drawdown_mtm_fraction"), math.nan),
        "drawdown_realized_fraction": _float(drawdown_metrics.get("max_drawdown_realized_fraction"), math.nan),
        "drawdown_max_open_inventory_notional": _float(drawdown_metrics.get("max_open_inventory_notional"), math.nan),
        "drawdown_max_open_inventory_fraction": _float(drawdown_metrics.get("max_open_inventory_fraction"), math.nan),
        "drawdown_max_active_order_notional": _float(drawdown_metrics.get("max_active_order_notional"), math.nan),
        "drawdown_max_active_order_fraction": _float(drawdown_metrics.get("max_active_order_fraction"), math.nan),
        "blockers": [str(x) for x in blockers],
        "drawdown_blockers": [str(x) for x in drawdown_blockers],
        "metadata": metadata,
    }
    row["promotion_core_passed"] = bool(
        row["income_gate_passed"] and row["risk_gates_passed"] and row["drawdown_core_passed"]
    )
    row["promotion_public_paper_passed"] = bool(
        row["has_drawdown_guard"]
        and row["promotion_core_passed"]
        and row["sample_gates_passed"]
        and row["drawdown_guard_passed"]
    )
    row["ranking_note"] = _ranking_note(row)
    return row


def _rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    rescue_rate = _finite(row.get("taker_rescue_feasible_rate"), -math.inf)
    rescue_fraction = _finite(row.get("taker_size_weighted_rescue_fraction"), -math.inf)
    residual_fraction = _risk_bucket(row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf)
    residual_loss = _risk_bucket(row.get("latest_taker_residual_loss_to_zero"), decimals=2, default=math.inf)
    drawdown_mtm = _risk_bucket(row.get("drawdown_mtm_fraction"), decimals=6, default=math.inf)
    open_inventory = _risk_bucket(row.get("drawdown_max_open_inventory_fraction"), decimals=6, default=math.inf)
    active_orders = _risk_bucket(row.get("drawdown_max_active_order_fraction"), decimals=6, default=math.inf)
    reward_loss = _finite(row.get("drawdown_reward_to_trading_loss_ratio"), -math.inf)
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("income_gate_passed"))),
        float(bool(row.get("risk_gates_passed"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("drawdown_guard_passed", True))),
        -residual_fraction,
        -residual_loss,
        -drawdown_mtm,
        -open_inventory,
        -active_orders,
        reward_loss,
        income,
        rescue_rate,
        rescue_fraction,
        float(row.get("unique_markets_quoted", 0)),
        float(row.get("quote_rows", 0)),
        float(row.get("duration_hours", 0.0)),
    )


def _policy_leaders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    eligible = [
        row
        for row in rows
        if row.get("income_gate_passed") and row.get("risk_gates_passed") and row.get("drawdown_core_passed")
    ]
    population = eligible or rows
    risk_first = max(population, key=_rank_key)
    income_first = max(population, key=_income_rank_key)
    sample_first = max(population, key=_sample_rank_key)
    return {
        "risk_first_leader": _leader_summary(risk_first),
        "income_first_leader": _leader_summary(income_first),
        "sample_first_leader": _leader_summary(sample_first),
        "note": (
            "risk-first is the conservative promotion lane; income-first shows the highest p05 monthly "
            "candidate after required risk gates; sample-first favors more mature evidence"
        ),
    }


def _income_rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    residual_fraction = _risk_bucket(row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf)
    residual_loss = _risk_bucket(row.get("latest_taker_residual_loss_to_zero"), decimals=2, default=math.inf)
    reward_loss = _finite(row.get("drawdown_reward_to_trading_loss_ratio"), -math.inf)
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("drawdown_guard_passed", True))),
        income,
        -residual_fraction,
        -residual_loss,
        reward_loss,
        float(row.get("unique_markets_quoted", 0)),
        float(row.get("quote_rows", 0)),
        float(row.get("duration_hours", 0.0)),
    )


def _sample_rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    residual_fraction = _risk_bucket(row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf)
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("drawdown_sample_passed", True))),
        float(row.get("duration_hours", 0.0)),
        float(row.get("quote_rows", 0)),
        float(row.get("unique_markets_quoted", 0)),
        -residual_fraction,
        income,
    )


def _leader_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "status": row.get("status"),
        "income_p05_at_required_capture": row.get("income_p05_at_required_capture"),
        "duration_hours": row.get("duration_hours"),
        "quote_rows": row.get("quote_rows"),
        "unique_markets_quoted": row.get("unique_markets_quoted"),
        "latest_taker_residual_loss_to_zero": row.get("latest_taker_residual_loss_to_zero"),
        "latest_taker_residual_loss_fraction": row.get("latest_taker_residual_loss_fraction"),
        "configured_quote_size_shares": row.get("configured_quote_size_shares"),
        "configured_active_capital_limit": row.get("configured_active_capital_limit"),
        "configured_residual_loss_cap_usdc": row.get("configured_residual_loss_cap_usdc"),
        "configured_max_total_unpaired": row.get("configured_max_total_unpaired"),
        "drawdown_guard_status": row.get("drawdown_guard_status"),
        "drawdown_reward_to_trading_loss_ratio": row.get("drawdown_reward_to_trading_loss_ratio"),
        "drawdown_mtm_fraction": row.get("drawdown_mtm_fraction"),
        "drawdown_max_open_inventory_fraction": row.get("drawdown_max_open_inventory_fraction"),
        "drawdown_max_active_order_fraction": row.get("drawdown_max_active_order_fraction"),
        "ranking_note": row.get("ranking_note"),
    }


def _ranking_note(row: dict[str, Any]) -> str:
    if row.get("has_drawdown_guard") and not row.get("drawdown_core_passed"):
        return "income/rescue may pass, but drawdown or reward-loss guard blocks promotion"
    if row.get("promotion_public_paper_passed"):
        return "public-paper income/rescue/drawdown gates passed; still needs signed paper and paid-reward proof"
    if row.get("public_paper_depth_ready"):
        return "public-paper depth/income gates passed; drawdown or signed reward proof still pending"
    if row.get("promotion_core_passed"):
        return "income/rescue/drawdown core passes; sample gate still pending"
    if row.get("income_gate_passed") and row.get("risk_gates_passed"):
        return "income/risk scout passes; sample gate still pending"
    if row.get("risk_gates_passed"):
        return "risk scout passes; income/sample gate still pending"
    return "risk or income gate not yet passed"


def _status(leader: dict[str, Any]) -> str:
    if not leader:
        return "no_candidates"
    if leader.get("promotion_public_paper_passed"):
        return "public_paper_leader_income_rescue_drawdown_ready"
    if leader.get("public_paper_depth_ready"):
        return "public_paper_leader_depth_ready"
    if (
        leader.get("has_drawdown_guard")
        and leader.get("income_gate_passed")
        and leader.get("risk_gates_passed")
        and not leader.get("drawdown_core_passed")
    ):
        return "public_paper_leader_drawdown_core_failed"
    if leader.get("income_gate_passed") and leader.get("risk_gates_passed"):
        return "public_paper_leader_sample_pending"
    return "no_public_paper_candidate_ready"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_float(*values: Any, default: float = math.nan) -> float:
    for value in values:
        parsed = _float(value, math.nan)
        if math.isfinite(parsed):
            return parsed
    return default


def _finite(value: Any, default: float) -> float:
    value = _float(value, default)
    return value if math.isfinite(value) else default


def _risk_bucket(value: Any, *, decimals: int, default: float) -> float:
    """Round risk metrics before ranking so floating noise cannot promote a worse income candidate."""

    value = _finite(value, default)
    return round(value, decimals) if math.isfinite(value) else default
