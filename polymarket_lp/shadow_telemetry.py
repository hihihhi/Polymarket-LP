from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from .paper import PaperAnalysisConfig, analyze_paper_quotes


@dataclass(slots=True)
class ShadowTelemetryConfig:
    """Build local order/fill/cancel/reward telemetry from paper quote intents.

    This is a shadow execution audit, not exchange execution proof. It verifies
    that order lifecycle telemetry can be generated and audited from public-paper
    quote intents while preserving a separate paid-reward gate for real proof.
    """

    assumed_cancel_latency_seconds: float = 1.0
    assumed_fee_rate: float = 0.0
    paid_reward_capture_rate: float = 0.0
    max_reward_gap_seconds: float = 300.0


def build_shadow_execution_telemetry(
    *,
    snapshots: pd.DataFrame,
    quotes: pd.DataFrame,
    cfg: ShadowTelemetryConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or ShadowTelemetryConfig()
    quote = _normalise_quotes(quotes)
    if quote.empty:
        return _empty_payload(cfg)
    per_quote, summary = analyze_paper_quotes(
        snapshots,
        quote,
        PaperAnalysisConfig(max_reward_gap_seconds=cfg.max_reward_gap_seconds),
    )
    quote = quote.copy()
    quote["client_order_id"] = quote.apply(_order_id, axis=1)
    orders = _orders_from_quotes(quote)
    fills = _fills_from_outcomes(quote, per_quote, cfg)
    cancels = _cancels_from_quotes(quote, set(fills["client_order_id"]) if not fills.empty else set(), cfg)
    rewards = _rewards_from_summary(summary, cfg)
    return {
        "orders": orders,
        "fills": fills,
        "cancels": cancels,
        "rewards": rewards,
        "summary": {
            "orders": int(len(orders)),
            "fills": int(len(fills)),
            "cancels": int(len(cancels)),
            "estimated_reward_usdc": float(rewards["estimated_reward_usdc"].sum()) if not rewards.empty else 0.0,
            "paid_reward_usdc": float(rewards["paid_reward_usdc"].sum()) if not rewards.empty else 0.0,
            "reward_capture_rate": cfg.paid_reward_capture_rate,
            "would_fill_proxy_rate": float(summary.get("fill_proxy_rate", 0.0)),
            "stale_fill_proxy_rate": float(summary.get("stale_fill_rate", 0.0)),
            "pending_quote_rate": float(summary.get("pending_quote_rate", 0.0)),
            "safety": "shadow telemetry from public-paper quote intents; not exchange execution or paid reward proof",
        },
        "config": asdict(cfg),
    }


def _normalise_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes.empty:
        return pd.DataFrame()
    out = quotes.copy()
    required = {"timestamp", "condition_id", "side", "bid_price", "size_shares"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out["condition_id"] = out["condition_id"].astype(str)
    out["side"] = out["side"].astype(str).str.upper()
    out["bid_price"] = pd.to_numeric(out["bid_price"], errors="coerce")
    out["size_shares"] = pd.to_numeric(out["size_shares"], errors="coerce")
    if "active_order_notional_pair" in out:
        out["active_order_notional_pair"] = pd.to_numeric(out["active_order_notional_pair"], errors="coerce")
    return out.dropna(subset=["timestamp", "condition_id", "side", "bid_price", "size_shares"]).sort_values(
        ["timestamp", "condition_id", "side"]
    )


def _orders_from_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    orders = quotes[
        [
            "client_order_id",
            "timestamp",
            "condition_id",
            "side",
            "bid_price",
            "size_shares",
        ]
    ].copy()
    orders = orders.rename(columns={"timestamp": "created_ts"})
    orders["submitted_ts"] = orders["created_ts"]
    orders["order_type"] = "shadow_limit_bid"
    if "market_id" in quotes:
        orders["market_id"] = quotes["market_id"].values
    if "cluster" in quotes:
        orders["cluster"] = quotes["cluster"].values
    if "active_order_notional_pair" in quotes:
        orders["active_order_notional_pair"] = quotes["active_order_notional_pair"].values
    return orders


def _fills_from_outcomes(
    quotes: pd.DataFrame,
    outcomes: pd.DataFrame,
    cfg: ShadowTelemetryConfig,
) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame(columns=["client_order_id"])
    outcomes = outcomes.copy()
    outcomes["timestamp"] = pd.to_datetime(outcomes["timestamp"], utc=True, errors="coerce")
    outcomes["condition_id"] = outcomes["condition_id"].astype(str)
    outcomes["side"] = outcomes["side"].astype(str).str.upper()
    outcomes["client_order_id"] = outcomes.apply(_order_id, axis=1)
    fills = outcomes[outcomes["would_fill"].fillna(False)].copy()
    if fills.empty:
        return pd.DataFrame(columns=["client_order_id"])
    fills = fills.merge(
        quotes[["client_order_id", "bid_price", "size_shares"]],
        on="client_order_id",
        how="left",
        suffixes=("", "_quote"),
    )
    fills["fill_ts"] = pd.to_datetime(fills["next_ts"], utc=True, errors="coerce")
    fills["fill_price"] = pd.to_numeric(fills["bid_price_quote"].fillna(fills["bid_price"]), errors="coerce")
    fills["fill_size"] = pd.to_numeric(fills["size_shares_quote"].fillna(fills["size_shares"]), errors="coerce")
    fills["fill_notional_usdc"] = fills["fill_price"] * fills["fill_size"]
    fills["fee_usdc"] = fills["fill_notional_usdc"] * cfg.assumed_fee_rate
    fills["fill_source"] = "shadow_midpoint_cross_proxy"
    return fills[
        [
            "client_order_id",
            "fill_ts",
            "condition_id",
            "side",
            "fill_price",
            "fill_size",
            "fill_notional_usdc",
            "fee_usdc",
            "fill_source",
        ]
    ]


def _cancels_from_quotes(
    quotes: pd.DataFrame,
    filled_order_ids: set[str],
    cfg: ShadowTelemetryConfig,
) -> pd.DataFrame:
    ordered = quotes.copy()
    ordered["next_quote_ts"] = ordered.groupby(["condition_id", "side"])["timestamp"].shift(-1)
    cancels = ordered[ordered["next_quote_ts"].notna() & ~ordered["client_order_id"].isin(filled_order_ids)].copy()
    if cancels.empty:
        return pd.DataFrame(columns=["client_order_id"])
    cancels["cancel_requested_ts"] = pd.to_datetime(cancels["next_quote_ts"], utc=True, errors="coerce")
    latency = pd.to_timedelta(max(0.0, cfg.assumed_cancel_latency_seconds), unit="s")
    cancels["cancel_confirmed_ts"] = cancels["cancel_requested_ts"] + latency
    cancels["cancel_reason"] = "shadow_replace_or_expire"
    return cancels[
        [
            "client_order_id",
            "cancel_requested_ts",
            "cancel_confirmed_ts",
            "condition_id",
            "side",
            "cancel_reason",
        ]
    ]


def _rewards_from_summary(summary: dict[str, Any], cfg: ShadowTelemetryConfig) -> pd.DataFrame:
    estimated = float(summary.get("estimated_reward_accrual_usdc", 0.0))
    paid = estimated * max(0.0, min(1.0, cfg.paid_reward_capture_rate)) if math.isfinite(estimated) else 0.0
    return pd.DataFrame(
        [
            {
                "reward_period_start": summary.get("start_ts"),
                "reward_period_end": summary.get("end_ts"),
                "estimated_reward_usdc": estimated,
                "paid_reward_usdc": paid,
                "reward_source": "shadow_public_paper_estimate",
            }
        ]
    )


def _order_id(row: pd.Series) -> str:
    raw = "|".join(
        [
            str(pd.Timestamp(row["timestamp"]).isoformat()),
            str(row["condition_id"]),
            str(row["side"]),
            f"{float(row['bid_price']):.10f}",
            f"{float(row['size_shares']):.10f}",
        ]
    )
    return "shadow_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _empty_payload(cfg: ShadowTelemetryConfig) -> dict[str, Any]:
    empty = pd.DataFrame(columns=["client_order_id"])
    return {
        "orders": empty.copy(),
        "fills": empty.copy(),
        "cancels": empty.copy(),
        "rewards": pd.DataFrame(columns=["estimated_reward_usdc", "paid_reward_usdc"]),
        "summary": {
            "orders": 0,
            "fills": 0,
            "cancels": 0,
            "estimated_reward_usdc": 0.0,
            "paid_reward_usdc": 0.0,
            "reward_capture_rate": cfg.paid_reward_capture_rate,
            "safety": "shadow telemetry from public-paper quote intents; not exchange execution or paid reward proof",
        },
        "config": asdict(cfg),
    }
