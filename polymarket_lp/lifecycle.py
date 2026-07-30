from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

CANONICAL_LIFECYCLE_STATES = (
    "book_snapshot",
    "ranking_decision",
    "quote_intent",
    "risk_gate",
    "sign",
    "submit",
    "ack",
    "reject",
    "queue_estimate",
    "resting",
    "crossing_status",
    "no_fill",
    "partial_fill",
    "full_fill",
    "cancel_request",
    "cancel_confirm",
    "inventory_update",
    "maker_rescue",
    "taker_rescue",
    "forced_cut",
    "fee_slippage",
    "reward_estimate",
    "reward_paid",
    "final_pnl_attribution",
)

STATE_ORDER = {state: i for i, state in enumerate(CANONICAL_LIFECYCLE_STATES)}
CORE_STATES = (
    "book_snapshot",
    "ranking_decision",
    "quote_intent",
    "risk_gate",
    "sign",
    "submit",
)
ACCEPTANCE_STATES = ("ack", "reject")
FILL_STATES = ("partial_fill", "full_fill")
RESCUE_STATES = ("maker_rescue", "taker_rescue", "forced_cut", "no_fill")


@dataclass(slots=True)
class LifecycleAuditConfig:
    """Strict signed-paper/live lifecycle proof gate.

    The audit is read-only and local-file based. It never signs, submits,
    cancels, or places orders. Missing proof is returned as a blocker instead of
    being inferred from public-paper or shadow telemetry.
    """

    min_completed_orders: int = 1
    max_sign_to_submit_seconds: float = 10.0
    max_submit_to_ack_seconds: float = 10.0
    max_cancel_latency_seconds: float = 30.0
    max_fill_to_rescue_seconds: float = 120.0
    min_paid_reward_usdc: float = 0.01
    min_reward_capture_rate: float = 0.5
    require_queue_estimate: bool = True
    require_cancel_confirmation: bool = True
    require_paid_reward: bool = True
    require_final_pnl: bool = True
    require_rescue_decision_for_fills: bool = True


def audit_order_lifecycle(
    events: pd.DataFrame, cfg: LifecycleAuditConfig | None = None
) -> dict[str, Any]:
    """Audit timestamped order lifecycle rows for deployment-quality proof.

    Required input columns are `timestamp`, `client_order_id`, and
    `lifecycle_state`. State-specific fields are checked when present in the
    relevant state rows. The function deliberately treats proxy/shadow evidence
    as insufficient for deployment unless paid rewards and final PnL are present.
    """

    cfg = cfg or LifecycleAuditConfig()
    raw_rows = int(len(events)) if events is not None else 0
    normalised, missing_required = _normalise_events(events)
    if missing_required:
        return _empty_result(cfg, raw_rows, missing_required)
    if normalised.empty:
        return _empty_result(cfg, raw_rows, [])

    per_order: list[dict[str, Any]] = []
    for client_order_id, group in normalised.groupby("client_order_id", sort=True):
        per_order.append(
            _audit_one_order(str(client_order_id), group.sort_values("timestamp"), cfg)
        )

    completed_orders = sum(1 for row in per_order if row["passed"])
    estimated_reward = _state_sum(
        normalised, "reward_estimate", "estimated_reward_usdc"
    )
    paid_reward = _state_sum(normalised, "reward_paid", "paid_reward_usdc")
    reward_capture = (
        paid_reward / estimated_reward
        if estimated_reward > 0
        else math.inf
        if paid_reward > 0
        else 0.0
    )

    gate_checks = {
        "events_present": bool(raw_rows > 0),
        "schema_columns_present": True,
        "min_completed_orders_passed": completed_orders >= cfg.min_completed_orders,
        "core_sequence_passed": all(
            row["gates"]["core_states_present"] for row in per_order
        ),
        "acceptance_passed": all(
            row["gates"]["ack_or_reject_present"] for row in per_order
        ),
        "queue_depth_passed": all(
            row["gates"]["queue_estimate_present"] for row in per_order
        ),
        "time_order_passed": all(
            row["gates"]["time_order_passed"] for row in per_order
        ),
        "submit_latency_passed": all(
            row["gates"]["submit_latency_passed"] for row in per_order
        ),
        "ack_latency_passed": all(
            row["gates"]["ack_latency_passed"] for row in per_order
        ),
        "cancel_latency_passed": all(
            row["gates"]["cancel_latency_passed"] for row in per_order
        ),
        "fill_attribution_passed": all(
            row["gates"]["fill_attribution_passed"] for row in per_order
        ),
        "rescue_decision_passed": all(
            row["gates"]["rescue_decision_passed"] for row in per_order
        ),
        "final_pnl_passed": all(row["gates"]["final_pnl_present"] for row in per_order),
        "paid_reward_passed": (not cfg.require_paid_reward)
        or (
            paid_reward >= cfg.min_paid_reward_usdc
            and reward_capture >= cfg.min_reward_capture_rate
        ),
    }
    gate_checks["deployment_lifecycle_passed"] = bool(all(gate_checks.values()))

    return {
        "config": asdict(cfg),
        "metrics": {
            "event_rows": raw_rows,
            "orders": int(len(per_order)),
            "completed_orders": int(completed_orders),
            "estimated_reward_usdc": float(estimated_reward),
            "paid_reward_usdc": float(paid_reward),
            "reward_capture_rate": float(reward_capture),
            "orders_with_fills": int(
                sum(1 for row in per_order if row["metrics"]["has_fill"])
            ),
            "orders_with_rejects": int(
                sum(1 for row in per_order if row["metrics"]["has_reject"])
            ),
            "max_cancel_latency_seconds": _max_metric(
                per_order, "cancel_latency_seconds"
            ),
            "max_submit_latency_seconds": _max_metric(
                per_order, "sign_to_submit_seconds"
            ),
            "max_ack_latency_seconds": _max_metric(per_order, "submit_to_ack_seconds"),
        },
        "gates": gate_checks,
        "blockers": _blockers(gate_checks, cfg),
        "per_order": per_order,
        "status": "deployment_lifecycle_passed"
        if gate_checks["deployment_lifecycle_passed"]
        else "deployment_lifecycle_incomplete",
        "safety": "local lifecycle audit only; no private keys, signing, order submission, or cancellation",
    }


def load_lifecycle_csv(path: str | Path | None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p)


def _audit_one_order(
    client_order_id: str, group: pd.DataFrame, cfg: LifecycleAuditConfig
) -> dict[str, Any]:
    states = set(group["lifecycle_state"])
    has_fill = bool(states.intersection(FILL_STATES))
    has_reject = "reject" in states
    sequence_order = [
        STATE_ORDER.get(state, 10_000) for state in group["lifecycle_state"]
    ]
    time_order_passed = all(a <= b for a, b in zip(sequence_order, sequence_order[1:]))

    sign_to_submit = _state_delta_seconds(group, "sign", "submit")
    submit_to_ack = (
        _state_delta_seconds(group, "submit", "ack")
        if "ack" in states
        else _state_delta_seconds(group, "submit", "reject")
    )
    cancel_latency = _state_delta_seconds(group, "cancel_request", "cancel_confirm")
    fill_to_rescue = _fill_to_rescue_seconds(group)

    gates = {
        "core_states_present": all(state in states for state in CORE_STATES),
        "ack_or_reject_present": bool(states.intersection(ACCEPTANCE_STATES)),
        "queue_estimate_present": (not cfg.require_queue_estimate)
        or has_reject
        or "queue_estimate" in states,
        "time_order_passed": time_order_passed,
        "submit_latency_passed": sign_to_submit is not None
        and sign_to_submit <= cfg.max_sign_to_submit_seconds,
        "ack_latency_passed": submit_to_ack is not None
        and submit_to_ack <= cfg.max_submit_to_ack_seconds,
        "cancel_latency_passed": (not cfg.require_cancel_confirmation)
        or has_reject
        or (
            cancel_latency is not None
            and cancel_latency <= cfg.max_cancel_latency_seconds
        ),
        "fill_attribution_passed": (not has_fill)
        or ("inventory_update" in states and "fee_slippage" in states),
        "rescue_decision_passed": (not cfg.require_rescue_decision_for_fills)
        or (not has_fill)
        or bool(
            states.intersection(RESCUE_STATES)
            and (
                fill_to_rescue is None
                or fill_to_rescue <= cfg.max_fill_to_rescue_seconds
            )
        ),
        "final_pnl_present": (not cfg.require_final_pnl)
        or _state_has_numeric(group, "final_pnl_attribution", "final_pnl_usdc"),
    }

    return {
        "client_order_id": client_order_id,
        "passed": bool(all(gates.values())),
        "states": sorted(states, key=lambda s: STATE_ORDER.get(s, 10_000)),
        "gates": gates,
        "metrics": {
            "event_rows": int(len(group)),
            "has_fill": has_fill,
            "has_reject": has_reject,
            "sign_to_submit_seconds": sign_to_submit,
            "submit_to_ack_seconds": submit_to_ack,
            "cancel_latency_seconds": cancel_latency,
            "fill_to_rescue_seconds": fill_to_rescue,
            "estimated_reward_usdc": _state_sum(
                group, "reward_estimate", "estimated_reward_usdc"
            ),
            "paid_reward_usdc": _state_sum(group, "reward_paid", "paid_reward_usdc"),
            "final_pnl_usdc": _state_sum(
                group, "final_pnl_attribution", "final_pnl_usdc"
            ),
        },
    }


def _normalise_events(events: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    if events is None or events.empty:
        return pd.DataFrame(
            columns=["timestamp", "client_order_id", "lifecycle_state"]
        ), []
    out = events.copy()
    if "client_order_id" not in out and "order_id" in out:
        out["client_order_id"] = out["order_id"]
    required = {"timestamp", "client_order_id", "lifecycle_state"}
    missing = sorted(required - set(out.columns))
    if missing:
        return pd.DataFrame(), missing
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out["client_order_id"] = out["client_order_id"].astype(str)
    out["lifecycle_state"] = out["lifecycle_state"].astype(str).str.strip().str.lower()
    return out.dropna(subset=["timestamp", "client_order_id", "lifecycle_state"]), []


def _state_delta_seconds(
    group: pd.DataFrame, start_state: str, end_state: str
) -> float | None:
    start = _first_state_ts(group, start_state)
    end = _first_state_ts(group, end_state)
    if start is None or end is None:
        return None
    return float((end - start).total_seconds())


def _fill_to_rescue_seconds(group: pd.DataFrame) -> float | None:
    fill_ts = min(
        (
            _first_state_ts(group, state)
            for state in FILL_STATES
            if _first_state_ts(group, state) is not None
        ),
        default=None,
    )
    rescue_ts = min(
        (
            _first_state_ts(group, state)
            for state in RESCUE_STATES
            if _first_state_ts(group, state) is not None
        ),
        default=None,
    )
    if fill_ts is None or rescue_ts is None:
        return None
    return float((rescue_ts - fill_ts).total_seconds())


def _first_state_ts(group: pd.DataFrame, state: str) -> pd.Timestamp | None:
    rows = group[group["lifecycle_state"].eq(state)]
    if rows.empty:
        return None
    return rows["timestamp"].min()


def _state_has_numeric(group: pd.DataFrame, state: str, column: str) -> bool:
    rows = group[group["lifecycle_state"].eq(state)]
    return bool(
        not rows.empty
        and column in rows
        and pd.to_numeric(rows[column], errors="coerce").notna().any()
    )


def _state_sum(group: pd.DataFrame, state: str, column: str) -> float:
    if column not in group:
        return 0.0
    rows = group[group["lifecycle_state"].eq(state)]
    if rows.empty:
        return 0.0
    return float(pd.to_numeric(rows[column], errors="coerce").fillna(0.0).sum())


def _max_metric(per_order: list[dict[str, Any]], metric: str) -> float | None:
    values = [row["metrics"].get(metric) for row in per_order]
    numeric = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return max(numeric) if numeric else None


def _empty_result(
    cfg: LifecycleAuditConfig, raw_rows: int, missing_required: list[str]
) -> dict[str, Any]:
    gates = {
        "events_present": raw_rows > 0,
        "schema_columns_present": not missing_required,
        "min_completed_orders_passed": False,
        "core_sequence_passed": False,
        "acceptance_passed": False,
        "queue_depth_passed": False,
        "time_order_passed": False,
        "submit_latency_passed": False,
        "ack_latency_passed": False,
        "cancel_latency_passed": False,
        "fill_attribution_passed": False,
        "rescue_decision_passed": False,
        "final_pnl_passed": False,
        "paid_reward_passed": not cfg.require_paid_reward,
        "deployment_lifecycle_passed": False,
    }
    return {
        "config": asdict(cfg),
        "metrics": {"event_rows": raw_rows, "orders": 0, "completed_orders": 0},
        "gates": gates,
        "blockers": _blockers(gates, cfg)
        + (
            [f"missing required columns: {missing_required}"]
            if missing_required
            else []
        ),
        "per_order": [],
        "status": "deployment_lifecycle_incomplete",
        "safety": "local lifecycle audit only; no private keys, signing, order submission, or cancellation",
    }


def _blockers(gates: dict[str, bool], cfg: LifecycleAuditConfig) -> list[str]:
    names = {
        "events_present": "no lifecycle events supplied",
        "schema_columns_present": "lifecycle CSV missing required schema columns",
        "min_completed_orders_passed": f"fewer than {cfg.min_completed_orders} complete lifecycle orders",
        "core_sequence_passed": "book/rank/quote/risk/sign/submit core lifecycle missing",
        "acceptance_passed": "ack/reject proof missing",
        "queue_depth_passed": "queue/depth-ahead estimate missing",
        "time_order_passed": "lifecycle states are out of canonical timestamp order",
        "submit_latency_passed": f"sign-to-submit latency exceeds {cfg.max_sign_to_submit_seconds}s or is missing",
        "ack_latency_passed": f"submit-to-ack/reject latency exceeds {cfg.max_submit_to_ack_seconds}s or is missing",
        "cancel_latency_passed": f"cancel confirmation missing or exceeds {cfg.max_cancel_latency_seconds}s",
        "fill_attribution_passed": "filled orders lack inventory and fee/slippage attribution",
        "rescue_decision_passed": "filled orders lack timely maker/taker rescue, forced cut, or no-fill decision",
        "final_pnl_passed": "final PnL attribution missing",
        "paid_reward_passed": f"paid reward missing or capture below {cfg.min_reward_capture_rate:.0%}",
    }
    return [message for gate, message in names.items() if not gates.get(gate, False)]
