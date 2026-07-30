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
    capital_risk: dict[str, Any] | None = None


def build_candidate_leaderboard(
    candidates: Iterable[CandidateEvidence],
) -> dict[str, Any]:
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
    gate_config = _dict(gate.get("config"))
    drawdown_metrics = _dict(drawdown.get("metrics"))
    drawdown_gates = _dict(drawdown.get("gates"))
    capital = _dict(candidate.capital_risk)
    capital_config = _dict(capital.get("config"))
    capital_metrics = _dict(capital.get("metrics"))
    lp_config = _dict(drawdown.get("lp_config"))
    blockers_value = gate.get("blockers")
    blockers = blockers_value if isinstance(blockers_value, list) else []
    drawdown_blockers_value = drawdown.get("blockers")
    drawdown_blockers = (
        drawdown_blockers_value if isinstance(drawdown_blockers_value, list) else []
    )
    capital_blockers_value = capital.get("blockers")
    capital_blockers = (
        list(capital_blockers_value) if isinstance(capital_blockers_value, list) else []
    )
    capital_warnings_value = capital.get("warnings")
    capital_warnings = (
        list(capital_warnings_value) if isinstance(capital_warnings_value, list) else []
    )
    metadata = _dict(candidate.metadata)
    freshness = _dict(metadata.get("_input_freshness"))
    has_drawdown_guard = bool(drawdown)
    has_capital_risk = bool(capital)
    income_p05 = _float(metrics.get("income_p05_at_required_capture"), math.nan)
    target_monthly = _float(gate_config.get("target_monthly_usdc"), math.nan)
    required_capture = _float(metrics.get("required_capture_rate"), math.nan)
    capital_configured_cap_loss = _float(
        capital_metrics.get("configured_inventory_cap_loss_to_zero"), math.nan
    )
    capital_after_configured_cap_loss = income_p05 - capital_configured_cap_loss
    capture_needed_for_target = _capture_needed(
        target_monthly,
        income_p05,
        required_capture,
    )
    capture_needed_after_cap_loss = _capture_needed(
        target_monthly + capital_configured_cap_loss,
        income_p05,
        required_capture,
    )
    target_income_buffer = _income_buffer(income_p05, target_monthly)
    after_cap_loss_income_buffer = _income_buffer(
        capital_after_configured_cap_loss, target_monthly
    )
    after_cap_target_passed = (
        True
        if not has_capital_risk
        else math.isfinite(capital_after_configured_cap_loss)
        and math.isfinite(target_monthly)
        and capital_after_configured_cap_loss >= target_monthly
    )
    after_cap_target_required = bool(
        capital_config.get("require_after_cap_target", True)
    )
    drawdown_core_passed = (
        bool(drawdown.get("risk_core_passed", True)) if has_drawdown_guard else True
    )
    capital_core_passed = (
        bool(capital.get("status") == "capital_risk_stress_passed")
        and (after_cap_target_passed or not after_cap_target_required)
        if has_capital_risk
        else True
    )
    if has_capital_risk and not after_cap_target_passed and after_cap_target_required:
        capital_blockers.append(
            "configured-cap loss leaves p05 monthly income below target"
        )
    elif has_capital_risk and not after_cap_target_passed:
        capital_warnings.append(
            "configured-cap loss leaves p05 monthly income below target"
        )
    drawdown_sample_passed = (
        bool(drawdown_gates.get("sample_hours_gate_passed", False))
        if has_drawdown_guard
        else True
    )
    drawdown_guard_passed = (
        bool(drawdown_gates.get("drawdown_guard_passed", False))
        if has_drawdown_guard
        else True
    )
    risk_gates = [
        "clob_quality_gate_passed",
        "taker_rescue_rate_gate_passed",
        "taker_pair_edge_gate_passed",
        "taker_depth_gate_passed",
        "taker_residual_loss_gate_passed",
    ]
    sample_gates = [
        "sample_hours_gate_passed",
        "quote_rows_gate_passed",
        "book_scenario_gate_passed",
    ]
    sample_maturity_fraction, provisional_sample_hours_gate = _sample_maturity(
        metrics, gate_config
    )
    row = {
        "name": candidate.name,
        "status": str(gate.get("status", "unknown")),
        "public_paper_depth_ready": bool(gates.get("depth_ready", False)),
        "income_gate_passed": bool(gates.get("income_p05_gate_passed", False)),
        "risk_gates_passed": all(bool(gates.get(k, False)) for k in risk_gates),
        "sample_gates_passed": all(bool(gates.get(k, False)) for k in sample_gates),
        "sample_hours_gate_passed": bool(gates.get("sample_hours_gate_passed", False)),
        "quote_rows_gate_passed": bool(gates.get("quote_rows_gate_passed", False)),
        "book_scenario_gate_passed": bool(
            gates.get("book_scenario_gate_passed", False)
        ),
        "diversification_gate_passed": bool(
            gates.get("diversification_gate_passed", False)
        ),
        "sample_maturity_fraction": sample_maturity_fraction,
        "provisional_sample_hours_gate_passed": provisional_sample_hours_gate,
        "has_drawdown_guard": has_drawdown_guard,
        "drawdown_guard_status": str(drawdown.get("status", "not_evaluated")),
        "drawdown_core_passed": drawdown_core_passed,
        "drawdown_sample_passed": drawdown_sample_passed,
        "drawdown_guard_passed": drawdown_guard_passed,
        "has_capital_risk": has_capital_risk,
        "capital_risk_status": str(capital.get("status", "not_evaluated")),
        "capital_core_passed": capital_core_passed,
        "duration_hours": _float(metrics.get("duration_hours"), 0.0),
        "quote_rows": int(_float(metrics.get("quote_rows"), 0.0)),
        "unique_markets_quoted": int(_float(metrics.get("unique_markets_quoted"), 0.0)),
        "income_p05_at_required_capture": income_p05,
        "target_monthly_usdc": target_monthly,
        "required_capture_rate": required_capture,
        "capture_needed_for_target": capture_needed_for_target,
        "capture_needed_after_cap_loss": capture_needed_after_cap_loss,
        "target_income_buffer_at_required_capture": target_income_buffer,
        "after_cap_loss_income_buffer_at_required_capture": after_cap_loss_income_buffer,
        "taker_rescue_feasible_rate": _float(
            metrics.get("taker_rescue_feasible_rate"), math.nan
        ),
        "taker_rescue_min_depth_fraction": _float(
            metrics.get("taker_rescue_min_depth_fraction"), math.nan
        ),
        "taker_size_weighted_rescue_fraction": _float(
            metrics.get("taker_size_weighted_rescue_fraction"), math.nan
        ),
        "latest_taker_residual_loss_to_zero": _float(
            metrics.get("latest_taker_residual_loss_to_zero"), math.nan
        ),
        "latest_taker_residual_loss_fraction": _float(
            metrics.get("latest_taker_residual_loss_fraction"), math.nan
        ),
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
        "configured_max_unpaired_per_market": _first_float(
            lp_config.get("max_unpaired_per_market")
        ),
        "configured_max_total_unpaired": _first_float(
            lp_config.get("max_total_unpaired")
        ),
        "configured_max_cluster_unpaired": _first_float(
            lp_config.get("max_cluster_unpaired")
        ),
        "drawdown_reward_to_trading_loss_ratio": _float(
            drawdown_metrics.get("reward_to_trading_loss_ratio"), math.nan
        ),
        "drawdown_mtm_fraction": _float(
            drawdown_metrics.get("max_drawdown_mtm_fraction"), math.nan
        ),
        "drawdown_realized_fraction": _float(
            drawdown_metrics.get("max_drawdown_realized_fraction"), math.nan
        ),
        "drawdown_max_open_inventory_notional": _float(
            drawdown_metrics.get("max_open_inventory_notional"), math.nan
        ),
        "drawdown_max_open_inventory_fraction": _float(
            drawdown_metrics.get("max_open_inventory_fraction"), math.nan
        ),
        "drawdown_max_active_order_notional": _float(
            drawdown_metrics.get("max_active_order_notional"), math.nan
        ),
        "drawdown_max_active_order_fraction": _float(
            drawdown_metrics.get("max_active_order_fraction"), math.nan
        ),
        "capital_cash_reserve_fraction": _float(
            capital_metrics.get("cash_reserve_fraction"), math.nan
        ),
        "capital_active_pair_notional": _float(
            capital_metrics.get("active_pair_notional"), math.nan
        ),
        "capital_latest_markets": int(
            _float(capital_metrics.get("latest_markets"), 0.0)
        ),
        "capital_single_market_active_fraction": _float(
            capital_metrics.get("max_single_market_active_fraction"), math.nan
        ),
        "capital_single_cluster_active_fraction": _float(
            capital_metrics.get("max_single_cluster_active_fraction"), math.nan
        ),
        "capital_single_market_loss_fraction": _float(
            capital_metrics.get("single_market_worst_one_side_loss_fraction"), math.nan
        ),
        "capital_single_cluster_loss_fraction": _float(
            capital_metrics.get("single_cluster_worst_one_side_loss_fraction"), math.nan
        ),
        "capital_unhedged_loss_fraction": _float(
            capital_metrics.get("unhedged_loss_fraction_of_capital"), math.nan
        ),
        "capital_unhedged_loss_usdc": _float(
            capital_metrics.get("all_active_unhedged_one_side_loss_to_zero"), math.nan
        ),
        "capital_configured_cap_loss_usdc": capital_configured_cap_loss,
        "capital_configured_cap_loss_fraction": _float(
            capital_metrics.get("configured_inventory_cap_loss_fraction"), math.nan
        ),
        "capital_configured_cap_recovery_days": _float(
            capital_metrics.get("capped_recovery_days_at_p05_income"), math.nan
        ),
        "capital_after_configured_cap_loss_monthly": capital_after_configured_cap_loss,
        "capital_after_cap_loss_target_passed": after_cap_target_passed,
        "capital_after_cap_loss_target_required": after_cap_target_required,
        "capital_pair_cost_per_share": _float(
            capital_metrics.get("max_pair_cost_per_share"), math.nan
        ),
        "input_snapshot_age_seconds": _float(
            _dict(freshness.get("snapshot")).get("age_seconds"), math.nan
        ),
        "input_quotes_age_seconds": _float(
            _dict(freshness.get("quotes")).get("age_seconds"), math.nan
        ),
        "input_max_age_seconds": _float(freshness.get("max_age_seconds"), math.nan),
        "input_freshness_gate_seconds": _float(
            metadata.get("_max_input_staleness_seconds"), math.nan
        ),
        "blockers": [str(x) for x in blockers],
        "drawdown_blockers": [str(x) for x in drawdown_blockers],
        "capital_blockers": [str(x) for x in capital_blockers],
        "capital_warnings": [str(x) for x in capital_warnings],
        "metadata": metadata,
    }
    row["promotion_core_passed"] = bool(
        row["income_gate_passed"]
        and row["risk_gates_passed"]
        and row["drawdown_core_passed"]
        and row["capital_core_passed"]
    )
    row["promotion_public_paper_passed"] = bool(
        row["has_drawdown_guard"]
        and row["promotion_core_passed"]
        and row["sample_gates_passed"]
        and row["drawdown_guard_passed"]
    )
    recovery = _autonomous_recovery(row, capital_config)
    row.update(recovery)
    row["ranking_note"] = _ranking_note(row)
    return row


def _rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    rescue_rate = _finite(row.get("taker_rescue_feasible_rate"), -math.inf)
    rescue_fraction = _finite(row.get("taker_size_weighted_rescue_fraction"), -math.inf)
    residual_fraction = _risk_bucket(
        row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf
    )
    residual_loss = _risk_bucket(
        row.get("latest_taker_residual_loss_to_zero"), decimals=2, default=math.inf
    )
    drawdown_mtm = _risk_bucket(
        row.get("drawdown_mtm_fraction"), decimals=6, default=math.inf
    )
    open_inventory = _risk_bucket(
        row.get("drawdown_max_open_inventory_fraction"), decimals=6, default=math.inf
    )
    active_orders = _risk_bucket(
        row.get("drawdown_max_active_order_fraction"), decimals=6, default=math.inf
    )
    cap_loss = _risk_bucket(
        row.get("capital_configured_cap_loss_fraction"), decimals=6, default=math.inf
    )
    cap_recovery = _risk_bucket(
        row.get("capital_configured_cap_recovery_days"), decimals=4, default=math.inf
    )
    single_market_active = _risk_bucket(
        row.get("capital_single_market_active_fraction"), decimals=6, default=math.inf
    )
    single_cluster_active = _risk_bucket(
        row.get("capital_single_cluster_active_fraction"), decimals=6, default=math.inf
    )
    single_market_loss = _risk_bucket(
        row.get("capital_single_market_loss_fraction"), decimals=6, default=math.inf
    )
    single_cluster_loss = _risk_bucket(
        row.get("capital_single_cluster_loss_fraction"), decimals=6, default=math.inf
    )
    unhedged_loss = _risk_bucket(
        row.get("capital_unhedged_loss_fraction"), decimals=6, default=math.inf
    )
    cash_reserve = _finite(row.get("capital_cash_reserve_fraction"), -math.inf)
    reward_loss = _finite(row.get("drawdown_reward_to_trading_loss_ratio"), -math.inf)
    after_cap_capture_needed = _risk_bucket(
        row.get("capture_needed_after_cap_loss"), decimals=6, default=math.inf
    )
    after_cap_income_buffer = _finite(
        row.get("after_cap_loss_income_buffer_at_required_capture"), -math.inf
    )
    target_capture_needed = _risk_bucket(
        row.get("capture_needed_for_target"), decimals=6, default=math.inf
    )
    target_income_buffer = _finite(
        row.get("target_income_buffer_at_required_capture"), -math.inf
    )
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("income_gate_passed"))),
        float(bool(row.get("risk_gates_passed"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("capital_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("provisional_sample_hours_gate_passed"))),
        float(bool(row.get("quote_rows_gate_passed"))),
        float(bool(row.get("book_scenario_gate_passed"))),
        float(bool(row.get("diversification_gate_passed"))),
        float(bool(row.get("drawdown_guard_passed", True))),
        -residual_fraction,
        -residual_loss,
        -drawdown_mtm,
        -open_inventory,
        -active_orders,
        -cap_loss,
        -cap_recovery,
        -single_market_active,
        -single_cluster_active,
        -single_market_loss,
        -single_cluster_loss,
        -unhedged_loss,
        cash_reserve,
        reward_loss,
        -after_cap_capture_needed,
        after_cap_income_buffer,
        -target_capture_needed,
        target_income_buffer,
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
        if row.get("income_gate_passed")
        and row.get("risk_gates_passed")
        and row.get("drawdown_core_passed")
        and row.get("capital_core_passed")
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
    residual_fraction = _risk_bucket(
        row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf
    )
    residual_loss = _risk_bucket(
        row.get("latest_taker_residual_loss_to_zero"), decimals=2, default=math.inf
    )
    reward_loss = _finite(row.get("drawdown_reward_to_trading_loss_ratio"), -math.inf)
    cap_loss = _risk_bucket(
        row.get("capital_configured_cap_loss_fraction"), decimals=6, default=math.inf
    )
    cap_recovery = _risk_bucket(
        row.get("capital_configured_cap_recovery_days"), decimals=4, default=math.inf
    )
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("capital_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("drawdown_guard_passed", True))),
        income,
        -residual_fraction,
        -residual_loss,
        -cap_loss,
        -cap_recovery,
        reward_loss,
        float(row.get("unique_markets_quoted", 0)),
        float(row.get("quote_rows", 0)),
        float(row.get("duration_hours", 0.0)),
    )


def _sample_rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    residual_fraction = _risk_bucket(
        row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf
    )
    cap_loss = _risk_bucket(
        row.get("capital_configured_cap_loss_fraction"), decimals=6, default=math.inf
    )
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("drawdown_core_passed", True))),
        float(bool(row.get("capital_core_passed", True))),
        float(bool(row.get("sample_gates_passed"))),
        float(bool(row.get("drawdown_sample_passed", True))),
        float(row.get("duration_hours", 0.0)),
        float(row.get("quote_rows", 0)),
        float(row.get("unique_markets_quoted", 0)),
        -residual_fraction,
        -cap_loss,
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
        "sample_hours_gate_passed": row.get("sample_hours_gate_passed"),
        "quote_rows_gate_passed": row.get("quote_rows_gate_passed"),
        "book_scenario_gate_passed": row.get("book_scenario_gate_passed"),
        "diversification_gate_passed": row.get("diversification_gate_passed"),
        "sample_maturity_fraction": row.get("sample_maturity_fraction"),
        "provisional_sample_hours_gate_passed": row.get(
            "provisional_sample_hours_gate_passed"
        ),
        "latest_taker_residual_loss_to_zero": row.get(
            "latest_taker_residual_loss_to_zero"
        ),
        "latest_taker_residual_loss_fraction": row.get(
            "latest_taker_residual_loss_fraction"
        ),
        "configured_quote_size_shares": row.get("configured_quote_size_shares"),
        "configured_active_capital_limit": row.get("configured_active_capital_limit"),
        "configured_residual_loss_cap_usdc": row.get(
            "configured_residual_loss_cap_usdc"
        ),
        "configured_max_total_unpaired": row.get("configured_max_total_unpaired"),
        "drawdown_guard_status": row.get("drawdown_guard_status"),
        "drawdown_reward_to_trading_loss_ratio": row.get(
            "drawdown_reward_to_trading_loss_ratio"
        ),
        "drawdown_mtm_fraction": row.get("drawdown_mtm_fraction"),
        "drawdown_max_open_inventory_fraction": row.get(
            "drawdown_max_open_inventory_fraction"
        ),
        "drawdown_max_active_order_fraction": row.get(
            "drawdown_max_active_order_fraction"
        ),
        "capital_risk_status": row.get("capital_risk_status"),
        "capital_cash_reserve_fraction": row.get("capital_cash_reserve_fraction"),
        "capital_latest_markets": row.get("capital_latest_markets"),
        "capital_single_market_active_fraction": row.get(
            "capital_single_market_active_fraction"
        ),
        "capital_single_cluster_active_fraction": row.get(
            "capital_single_cluster_active_fraction"
        ),
        "capital_single_market_loss_fraction": row.get(
            "capital_single_market_loss_fraction"
        ),
        "capital_single_cluster_loss_fraction": row.get(
            "capital_single_cluster_loss_fraction"
        ),
        "capital_unhedged_loss_fraction": row.get("capital_unhedged_loss_fraction"),
        "capital_configured_cap_loss_usdc": row.get("capital_configured_cap_loss_usdc"),
        "capital_configured_cap_loss_fraction": row.get(
            "capital_configured_cap_loss_fraction"
        ),
        "capital_configured_cap_recovery_days": row.get(
            "capital_configured_cap_recovery_days"
        ),
        "capital_after_configured_cap_loss_monthly": row.get(
            "capital_after_configured_cap_loss_monthly"
        ),
        "capital_after_cap_loss_target_passed": row.get(
            "capital_after_cap_loss_target_passed"
        ),
        "capture_needed_for_target": row.get("capture_needed_for_target"),
        "capture_needed_after_cap_loss": row.get("capture_needed_after_cap_loss"),
        "target_income_buffer_at_required_capture": row.get(
            "target_income_buffer_at_required_capture"
        ),
        "after_cap_loss_income_buffer_at_required_capture": row.get(
            "after_cap_loss_income_buffer_at_required_capture"
        ),
        "input_max_age_seconds": row.get("input_max_age_seconds"),
        "input_freshness_gate_seconds": row.get("input_freshness_gate_seconds"),
        "autonomous_action": row.get("autonomous_action"),
        "recommended_quote_scale": row.get("recommended_quote_scale"),
        "recommended_quote_size_shares": row.get("recommended_quote_size_shares"),
        "autonomous_action_reason": row.get("autonomous_action_reason"),
        "ranking_note": row.get("ranking_note"),
    }


def _ranking_note(row: dict[str, Any]) -> str:
    if row.get("has_capital_risk") and not row.get("capital_core_passed"):
        return "income/rescue may pass, but capital-loss or recovery guard blocks promotion"
    if row.get("has_drawdown_guard") and not row.get("drawdown_core_passed"):
        return (
            "income/rescue may pass, but drawdown or reward-loss guard blocks promotion"
        )
    if row.get("promotion_public_paper_passed"):
        return "public-paper income/rescue/drawdown/capital gates passed; still needs signed paper and paid-reward proof"
    if row.get("public_paper_depth_ready"):
        return "public-paper depth/income gates passed; drawdown/capital or signed reward proof still pending"
    if row.get("promotion_core_passed"):
        return "income/rescue/drawdown/capital core passes; sample gate still pending"
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
    if (
        leader.get("has_capital_risk")
        and leader.get("income_gate_passed")
        and leader.get("risk_gates_passed")
        and not leader.get("capital_core_passed")
    ):
        return "public_paper_leader_capital_core_failed"
    if leader.get("income_gate_passed") and leader.get("risk_gates_passed"):
        return "public_paper_leader_sample_pending"
    return "no_public_paper_candidate_ready"


def _autonomous_recovery(
    row: dict[str, Any], capital_config: dict[str, Any]
) -> dict[str, Any]:
    """Return mechanical action/scale for public-paper monitoring.

    This is deliberately conservative: it never authorizes live capital. It only
    tells an autonomous runner whether the current evidence supports continuing
    public paper, reducing size, or entering rescue-only/pause mode.
    """

    configured_qsize = _finite(row.get("configured_quote_size_shares"), math.nan)
    scale, scale_reason = _recommended_scale(row, capital_config)
    recommended_qsize = (
        math.floor(configured_qsize * scale)
        if math.isfinite(configured_qsize)
        else math.nan
    )
    if not math.isfinite(scale):
        scale = 0.0
    if row.get("quote_rows", 0) <= 0:
        action = "collect_more_public_paper"
        reason = "no quote evidence yet"
    elif not row.get("risk_gates_passed"):
        action = "pause_new_quotes_rescue_only"
        reason = "rescue/depth/residual-loss risk gate failed"
    elif not row.get("income_gate_passed"):
        action = "collect_only_no_scale_up"
        reason = "income target not proven at required capture"
    elif scale < 0.25:
        action = "pause_new_quotes_rescue_only"
        reason = f"risk sizing scale too small: {scale_reason}"
    elif scale < 0.999:
        action = "reduce_size_continue_public_paper"
        reason = scale_reason
    elif row.get("promotion_public_paper_passed"):
        action = "signed_paper_telemetry_next"
        reason = "public-paper gates passed; live deployment still requires signed paper and paid reward proof"
    elif row.get("promotion_core_passed"):
        action = "continue_public_paper_current_size"
        reason = "core income/risk/recovery gates pass; sample or telemetry gate still pending"
    else:
        action = "collect_more_public_paper"
        reason = "sample is immature or non-core gates still pending"
    return {
        "autonomous_action": action,
        "recommended_quote_scale": scale,
        "recommended_quote_size_shares": recommended_qsize,
        "autonomous_action_reason": reason,
    }


def _recommended_scale(
    row: dict[str, Any], capital_config: dict[str, Any]
) -> tuple[float, str]:
    initial_capital = _finite(capital_config.get("initial_capital"), 2_000.0)
    limits: list[tuple[str, float]] = []
    if initial_capital > 0:
        active_notional = _finite(row.get("capital_active_pair_notional"), math.nan)
        min_cash = _finite(capital_config.get("min_cash_reserve_fraction"), math.nan)
        if (
            math.isfinite(active_notional)
            and math.isfinite(min_cash)
            and active_notional > 0
        ):
            limits.append(
                ("cash reserve", initial_capital * (1.0 - min_cash) / active_notional)
            )
        _append_fraction_limit(
            limits,
            "unhedged loss",
            row.get("capital_unhedged_loss_fraction"),
            capital_config.get("max_unhedged_loss_fraction"),
        )
        _append_fraction_limit(
            limits,
            "configured cap loss",
            row.get("capital_configured_cap_loss_fraction"),
            capital_config.get("max_capped_loss_fraction"),
        )
        _append_fraction_limit(
            limits,
            "single-market active",
            row.get("capital_single_market_active_fraction"),
            capital_config.get("max_single_market_active_fraction"),
        )
        _append_fraction_limit(
            limits,
            "single-cluster active",
            row.get("capital_single_cluster_active_fraction"),
            capital_config.get("max_single_cluster_active_fraction"),
        )
        _append_fraction_limit(
            limits,
            "single-market loss",
            row.get("capital_single_market_loss_fraction"),
            capital_config.get("max_single_market_unhedged_loss_fraction"),
        )
        _append_fraction_limit(
            limits,
            "single-cluster loss",
            row.get("capital_single_cluster_loss_fraction"),
            capital_config.get("max_single_cluster_unhedged_loss_fraction"),
        )
    recovery_days = _finite(row.get("capital_configured_cap_recovery_days"), math.nan)
    max_recovery_days = _finite(
        capital_config.get("max_capped_recovery_days"), math.nan
    )
    if (
        math.isfinite(recovery_days)
        and math.isfinite(max_recovery_days)
        and recovery_days > 0
    ):
        limits.append(("recovery days", max_recovery_days / recovery_days))
    finite_limits = [
        (name, value) for name, value in limits if math.isfinite(value) and value >= 0
    ]
    if not finite_limits:
        return 1.0, "no finite scaling constraint"
    name, value = min(finite_limits, key=lambda item: item[1])
    scale = max(0.0, min(1.0, value))
    return scale, f"binding constraint: {name}"


def _append_fraction_limit(
    limits: list[tuple[str, float]],
    name: str,
    current: Any,
    limit: Any,
) -> None:
    current_value = _finite(current, math.nan)
    limit_value = _finite(limit, math.nan)
    if (
        math.isfinite(current_value)
        and math.isfinite(limit_value)
        and current_value > 0
    ):
        limits.append((name, limit_value / current_value))


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


def _sample_maturity(
    metrics: dict[str, Any], gate_config: dict[str, Any]
) -> tuple[float, bool]:
    duration = _float(metrics.get("duration_hours"), 0.0)
    required = _float(gate_config.get("min_observation_hours"), math.nan)
    if not math.isfinite(required) or required <= 0:
        return math.inf, True
    maturity = max(0.0, min(1.0, duration / required))
    return maturity, maturity >= 0.25


def _capture_needed(
    target_income: float, income_at_required_capture: float, required_capture: float
) -> float:
    if not (
        math.isfinite(target_income)
        and math.isfinite(income_at_required_capture)
        and math.isfinite(required_capture)
    ):
        return math.nan
    if income_at_required_capture <= 0 or required_capture <= 0:
        return math.inf
    return required_capture * target_income / income_at_required_capture


def _income_buffer(income: float, target_income: float) -> float:
    if (
        not (math.isfinite(income) and math.isfinite(target_income))
        or target_income <= 0
    ):
        return math.nan
    return income / target_income - 1.0
