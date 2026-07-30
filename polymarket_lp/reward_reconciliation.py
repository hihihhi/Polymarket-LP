from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(slots=True)
class RewardReconciliationConfig:
    """Paid reward reconciliation gate for LP economics.

    The reconciler is intentionally read-only and local-file based. It compares
    estimated reward rows from the lifecycle/order ledger with paid reward or
    payout rows exported separately by a permitted account/API process.
    """

    min_paid_reward_usdc: float = 0.01
    min_reward_capture_rate: float = 0.50
    max_unmatched_estimate_rate: float = 0.05
    max_unmatched_paid_rate: float = 0.05
    require_client_order_match: bool = False


REWARD_ESTIMATE_SCHEMA = (
    "reward_period_start",
    "reward_period_end",
    "market_id",
    "condition_id",
    "client_order_id",
    "eligible_seconds",
    "estimated_reward_usdc",
    "source_hash",
)

PAID_REWARD_SCHEMA = (
    "reward_period_start",
    "reward_period_end",
    "market_id",
    "condition_id",
    "client_order_id",
    "paid_reward_usdc",
    "transaction_hash",
    "source_hash",
)


def reconcile_paid_rewards(
    estimates: pd.DataFrame | None,
    paid: pd.DataFrame | None,
    cfg: RewardReconciliationConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or RewardReconciliationConfig()
    est = _normalise(estimates, "estimate")
    pay = _normalise(paid, "paid")
    keys = ["market_id", "condition_id", "reward_period_start", "reward_period_end"]
    if cfg.require_client_order_match:
        keys.append("client_order_id")
    est_grouped = _group(est, keys, "estimated_reward_usdc")
    pay_grouped = _group(pay, keys, "paid_reward_usdc")
    joined = est_grouped.merge(pay_grouped, on=keys, how="outer", indicator=True)
    if "estimated_reward_usdc" not in joined:
        joined["estimated_reward_usdc"] = 0.0
    if "paid_reward_usdc" not in joined:
        joined["paid_reward_usdc"] = 0.0
    joined["estimated_reward_usdc"] = pd.to_numeric(
        joined["estimated_reward_usdc"], errors="coerce"
    ).fillna(0.0)
    joined["paid_reward_usdc"] = pd.to_numeric(
        joined["paid_reward_usdc"], errors="coerce"
    ).fillna(0.0)
    joined["reward_capture_rate"] = joined.apply(
        lambda row: (
            row["paid_reward_usdc"] / row["estimated_reward_usdc"]
            if row["estimated_reward_usdc"] > 0
            else math.inf
            if row["paid_reward_usdc"] > 0
            else 0.0
        ),
        axis=1,
    )
    estimated_total = float(joined["estimated_reward_usdc"].sum())
    paid_total = float(joined["paid_reward_usdc"].sum())
    unmatched_estimated = float(
        joined.loc[joined["_merge"].eq("left_only"), "estimated_reward_usdc"].sum()
    )
    unmatched_paid = float(
        joined.loc[joined["_merge"].eq("right_only"), "paid_reward_usdc"].sum()
    )
    capture_rate = (
        paid_total / estimated_total
        if estimated_total > 0
        else math.inf
        if paid_total > 0
        else 0.0
    )
    unmatched_estimate_rate = (
        unmatched_estimated / estimated_total if estimated_total > 0 else 0.0
    )
    unmatched_paid_rate = unmatched_paid / paid_total if paid_total > 0 else 0.0
    gates = {
        "estimated_rewards_present": estimated_total > 0,
        "paid_rewards_present": paid_total >= cfg.min_paid_reward_usdc,
        "reward_capture_passed": math.isfinite(capture_rate)
        and capture_rate >= cfg.min_reward_capture_rate,
        "unmatched_estimate_rate_passed": unmatched_estimate_rate
        <= cfg.max_unmatched_estimate_rate,
        "unmatched_paid_rate_passed": unmatched_paid_rate
        <= cfg.max_unmatched_paid_rate,
    }
    gates["paid_reward_reconciliation_passed"] = bool(all(gates.values()))
    blockers = _blockers(gates, cfg)
    return {
        "config": asdict(cfg),
        "metrics": {
            "estimate_rows": int(len(est)),
            "paid_rows": int(len(pay)),
            "matched_rows": int(joined["_merge"].eq("both").sum())
            if "_merge" in joined
            else 0,
            "estimated_reward_usdc": estimated_total,
            "paid_reward_usdc": paid_total,
            "reward_capture_rate": float(capture_rate),
            "unmatched_estimated_reward_usdc": unmatched_estimated,
            "unmatched_paid_reward_usdc": unmatched_paid,
            "unmatched_estimate_rate": float(unmatched_estimate_rate),
            "unmatched_paid_rate": float(unmatched_paid_rate),
        },
        "gates": gates,
        "blockers": blockers,
        "status": "paid_reward_reconciliation_passed"
        if gates["paid_reward_reconciliation_passed"]
        else "paid_reward_reconciliation_incomplete",
        "joined": joined.drop(columns=["_merge"]).to_dict(orient="records")
        if not joined.empty
        else [],
        "safety": "local paid reward reconciliation only; no account login, private keys, or order placement",
    }


def reward_reconciliation_schema(kind: str = "both") -> list[dict[str, str]]:
    rows: list[tuple[str, str, str]] = []
    if kind in {"both", "estimate"}:
        rows += [
            (field, "estimate", _description(field)) for field in REWARD_ESTIMATE_SCHEMA
        ]
    if kind in {"both", "paid"}:
        rows += [(field, "paid", _description(field)) for field in PAID_REWARD_SCHEMA]
    return [
        {"field": field, "table": table, "description": desc}
        for field, table, desc in rows
    ]


def load_csv(path: str | Path | None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p)


def _normalise(frame: pd.DataFrame | None, kind: str) -> pd.DataFrame:
    out = frame.copy() if frame is not None else pd.DataFrame()
    schema = REWARD_ESTIMATE_SCHEMA if kind == "estimate" else PAID_REWARD_SCHEMA
    if out.empty:
        return pd.DataFrame(columns=schema)
    for col in schema:
        if col not in out:
            out[col] = ""
    for col in ["reward_period_start", "reward_period_end"]:
        out[col] = (
            pd.to_datetime(out[col], utc=True, errors="coerce")
            .dt.floor("s")
            .astype(str)
        )
    for col in ["market_id", "condition_id", "client_order_id"]:
        out[col] = out[col].fillna("").astype(str)
    amount = "estimated_reward_usdc" if kind == "estimate" else "paid_reward_usdc"
    out[amount] = pd.to_numeric(out[amount], errors="coerce").fillna(0.0)
    return out[list(schema)]


def _group(frame: pd.DataFrame, keys: list[str], amount_col: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=[*keys, amount_col])
    return frame.groupby(keys, dropna=False, as_index=False)[amount_col].sum()


def _blockers(gates: dict[str, bool], cfg: RewardReconciliationConfig) -> list[str]:
    names = {
        "estimated_rewards_present": "estimated reward rows missing",
        "paid_rewards_present": f"paid reward below {cfg.min_paid_reward_usdc}",
        "reward_capture_passed": f"reward capture below {cfg.min_reward_capture_rate:.0%}",
        "unmatched_estimate_rate_passed": f"unmatched estimate rate above {cfg.max_unmatched_estimate_rate:.0%}",
        "unmatched_paid_rate_passed": f"unmatched paid rate above {cfg.max_unmatched_paid_rate:.0%}",
    }
    return [message for gate, message in names.items() if not gates.get(gate, False)]


def _description(field: str) -> str:
    return {
        "reward_period_start": "UTC reward period start.",
        "reward_period_end": "UTC reward period end.",
        "market_id": "Market identifier.",
        "condition_id": "Condition identifier.",
        "client_order_id": "Optional client/order id for order-level reconciliation.",
        "eligible_seconds": "Eligible resting seconds from lifecycle ledger.",
        "estimated_reward_usdc": "Estimated reward from formula/proxy.",
        "paid_reward_usdc": "Actually paid reward from account/payout ledger.",
        "transaction_hash": "Payment/on-chain transaction hash if available.",
        "source_hash": "Hash of raw source export used for provenance.",
    }.get(field, field)
