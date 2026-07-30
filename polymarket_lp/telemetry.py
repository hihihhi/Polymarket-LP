from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(slots=True)
class ExecutionTelemetryConfig:
    """Configurable deployment-proof checks for live/paper LP execution logs.

    This module never signs, submits, or cancels orders. It only audits local
    telemetry exported by a separate paper/live runner.
    """

    min_orders: int = 1
    min_cancels: int = 1
    min_paid_reward_usdc: float = 0.01
    min_reward_capture_rate: float = 0.5
    max_cancel_latency_seconds: float = 30.0
    max_unmatched_fill_rate: float = 0.0
    require_paid_rewards: bool = True
    require_cancel_telemetry: bool = True


def audit_execution_telemetry(
    *,
    orders: pd.DataFrame,
    fills: pd.DataFrame | None = None,
    cancels: pd.DataFrame | None = None,
    rewards: pd.DataFrame | None = None,
    cfg: ExecutionTelemetryConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or ExecutionTelemetryConfig()
    orders = _normalise_orders(orders)
    fills = _normalise_fills(fills)
    cancels = _normalise_cancels(cancels)
    rewards = _normalise_rewards(rewards)

    order_ids = set(orders["client_order_id"]) if "client_order_id" in orders else set()
    fill_count = int(len(fills))
    unmatched_fills = (
        int((~fills["client_order_id"].isin(order_ids)).sum())
        if fill_count and order_ids
        else fill_count
    )
    unmatched_fill_rate = unmatched_fills / max(fill_count, 1)

    cancel_latency = pd.Series(dtype=float)
    if not cancels.empty and {"cancel_requested_ts", "cancel_confirmed_ts"}.issubset(
        cancels.columns
    ):
        cancel_latency = (
            (cancels["cancel_confirmed_ts"] - cancels["cancel_requested_ts"])
            .dt.total_seconds()
            .dropna()
        )

    estimated_reward = _sum_col(rewards, "estimated_reward_usdc")
    paid_reward = _sum_col(rewards, "paid_reward_usdc")
    reward_capture = (
        paid_reward / estimated_reward
        if estimated_reward > 0
        else math.inf
        if paid_reward > 0
        else 0.0
    )

    gates = {
        "orders_present": int(len(orders)) >= cfg.min_orders,
        "cancel_telemetry_present": (not cfg.require_cancel_telemetry)
        or int(len(cancels)) >= cfg.min_cancels,
        "cancel_latency_passed": (not cfg.require_cancel_telemetry)
        or (
            not cancel_latency.empty
            and float(cancel_latency.max()) <= cfg.max_cancel_latency_seconds
        ),
        "reward_payment_present": (not cfg.require_paid_rewards)
        or paid_reward >= cfg.min_paid_reward_usdc,
        "reward_capture_passed": (not cfg.require_paid_rewards)
        or (
            math.isfinite(reward_capture)
            and reward_capture >= cfg.min_reward_capture_rate
        ),
        "fill_matching_passed": unmatched_fill_rate <= cfg.max_unmatched_fill_rate,
    }
    gates["deployment_telemetry_passed"] = bool(all(gates.values()))

    return {
        "config": asdict(cfg),
        "metrics": {
            "orders": int(len(orders)),
            "fills": fill_count,
            "cancels": int(len(cancels)),
            "unmatched_fills": unmatched_fills,
            "unmatched_fill_rate": unmatched_fill_rate,
            "max_cancel_latency_seconds": float(cancel_latency.max())
            if not cancel_latency.empty
            else None,
            "median_cancel_latency_seconds": float(cancel_latency.median())
            if not cancel_latency.empty
            else None,
            "estimated_reward_usdc": estimated_reward,
            "paid_reward_usdc": paid_reward,
            "reward_capture_rate": reward_capture,
            "fees_usdc": _sum_col(fills, "fee_usdc"),
            "filled_notional_usdc": _sum_col(fills, "fill_notional_usdc"),
        },
        "gates": gates,
        "status": "deployment_telemetry_passed"
        if gates["deployment_telemetry_passed"]
        else "deployment_telemetry_incomplete",
        "safety": "local telemetry audit only; no private keys, signing, order submission, or cancellation",
    }


def load_csv_if(path: str | Path | None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p)


def _normalise_orders(frame: pd.DataFrame | None) -> pd.DataFrame:
    out = frame.copy() if frame is not None else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["client_order_id"])
    _ensure_id(out)
    for col in ["created_ts", "submitted_ts"]:
        if col in out:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    return out.dropna(subset=["client_order_id"])


def _normalise_fills(frame: pd.DataFrame | None) -> pd.DataFrame:
    out = frame.copy() if frame is not None else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["client_order_id"])
    _ensure_id(out)
    if "fill_ts" in out:
        out["fill_ts"] = pd.to_datetime(out["fill_ts"], utc=True, errors="coerce")
    if "fill_notional_usdc" not in out and {"fill_price", "fill_size"}.issubset(
        out.columns
    ):
        out["fill_notional_usdc"] = pd.to_numeric(
            out["fill_price"], errors="coerce"
        ).fillna(0) * pd.to_numeric(out["fill_size"], errors="coerce").fillna(0)
    for col in ["fee_usdc", "fill_notional_usdc"]:
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["client_order_id"])


def _normalise_cancels(frame: pd.DataFrame | None) -> pd.DataFrame:
    out = frame.copy() if frame is not None else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["client_order_id"])
    _ensure_id(out)
    for col in ["cancel_requested_ts", "cancel_confirmed_ts"]:
        if col in out:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    return out.dropna(subset=["client_order_id"])


def _normalise_rewards(frame: pd.DataFrame | None) -> pd.DataFrame:
    out = frame.copy() if frame is not None else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["estimated_reward_usdc", "paid_reward_usdc"])
    for col in ["estimated_reward_usdc", "paid_reward_usdc"]:
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _ensure_id(frame: pd.DataFrame) -> None:
    if "client_order_id" not in frame and "order_id" in frame:
        frame["client_order_id"] = frame["order_id"]
    if "client_order_id" in frame:
        frame["client_order_id"] = frame["client_order_id"].astype(str)


def _sum_col(frame: pd.DataFrame, col: str) -> float:
    if frame.empty or col not in frame:
        return 0.0
    return float(pd.to_numeric(frame[col], errors="coerce").fillna(0).sum())
