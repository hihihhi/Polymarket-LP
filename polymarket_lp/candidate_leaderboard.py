from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class CandidateEvidence:
    name: str
    gate: dict[str, Any]
    metadata: dict[str, Any] | None = None


def build_candidate_leaderboard(candidates: Iterable[CandidateEvidence]) -> dict[str, Any]:
    """Rank public-paper LP candidates by proof gates, income, and rescue risk.

    This is a read-only evidence combiner. It does not infer fills, paid rewards,
    account balances, private keys, or deployment permission.
    """

    rows = [_candidate_row(candidate) for candidate in candidates]
    rows.sort(key=_rank_key, reverse=True)
    leader = rows[0] if rows else {}
    return {
        "status": _status(leader),
        "leader": leader,
        "candidates": rows,
        "safety": (
            "public-paper evidence comparison only; no private keys, signing, "
            "order submission, cancellation, or paid-reward verification"
        ),
    }


def _candidate_row(candidate: CandidateEvidence) -> dict[str, Any]:
    gate = _dict(candidate.gate)
    metrics = _dict(gate.get("metrics"))
    gates = _dict(gate.get("gates"))
    blockers = gate.get("blockers") if isinstance(gate.get("blockers"), list) else []
    metadata = _dict(candidate.metadata)
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
        "blockers": [str(x) for x in blockers],
        "metadata": metadata,
    }
    row["ranking_note"] = _ranking_note(row)
    return row


def _rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    income = _finite(row.get("income_p05_at_required_capture"), -math.inf)
    rescue_rate = _finite(row.get("taker_rescue_feasible_rate"), -math.inf)
    rescue_fraction = _finite(row.get("taker_size_weighted_rescue_fraction"), -math.inf)
    residual_fraction = _risk_bucket(row.get("latest_taker_residual_loss_fraction"), decimals=6, default=math.inf)
    residual_loss = _risk_bucket(row.get("latest_taker_residual_loss_to_zero"), decimals=2, default=math.inf)
    return (
        float(bool(row.get("public_paper_depth_ready"))),
        float(bool(row.get("income_gate_passed"))),
        float(bool(row.get("risk_gates_passed"))),
        float(bool(row.get("sample_gates_passed"))),
        -residual_fraction,
        -residual_loss,
        income,
        rescue_rate,
        rescue_fraction,
        float(row.get("unique_markets_quoted", 0)),
        float(row.get("quote_rows", 0)),
        float(row.get("duration_hours", 0.0)),
    )


def _ranking_note(row: dict[str, Any]) -> str:
    if row.get("public_paper_depth_ready"):
        return "public-paper depth/income gates passed; still needs signed paper and paid-reward proof"
    if row.get("income_gate_passed") and row.get("risk_gates_passed"):
        return "income/risk scout passes; sample gate still pending"
    if row.get("risk_gates_passed"):
        return "risk scout passes; income/sample gate still pending"
    return "risk or income gate not yet passed"


def _status(leader: dict[str, Any]) -> str:
    if not leader:
        return "no_candidates"
    if leader.get("public_paper_depth_ready"):
        return "public_paper_leader_depth_ready"
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


def _finite(value: Any, default: float) -> float:
    value = _float(value, default)
    return value if math.isfinite(value) else default


def _risk_bucket(value: Any, *, decimals: int, default: float) -> float:
    """Round risk metrics before ranking so floating noise cannot promote a worse income candidate."""

    value = _finite(value, default)
    return round(value, decimals) if math.isfinite(value) else default
