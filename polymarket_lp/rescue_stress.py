from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd


@dataclass(slots=True)
class RescueStressConfig:
    """Stress one-sided LP fills against price-feasible rescue quotes.

    This is a local auditor. It does not claim the rescue fill will execute; it
    only checks whether the opposite-side price needed to complete the pair is
    still economically valid under configurable pair-edge and slippage
    assumptions.
    """

    initial_capital: float = 2_000.0
    rescue_min_pair_edge_per_share: float = 0.01
    rescue_quote_offset: float = 0.005
    min_rescue_bid: float = 0.001
    exit_slippage: float = 0.005
    min_price_feasible_rate: float = 0.95
    max_latest_blocked_loss_fraction: float = 0.05
    max_immediate_exit_loss_fraction: float = 0.02


def evaluate_rescue_stress(quotes: pd.DataFrame, cfg: RescueStressConfig | None = None) -> dict[str, Any]:
    cfg = cfg or RescueStressConfig()
    q = _normalise_quotes(quotes)
    if q.empty:
        return {
            "config": asdict(cfg),
            "metrics": _empty_metrics(),
            "gates": {"rescue_stress_passed": False},
            "blockers": ["no quote intents available"],
            "status": "rescue_stress_failed",
            "safety": _SAFETY,
        }

    scenarios = _scenario_frame(q, cfg)
    if scenarios.empty:
        return {
            "config": asdict(cfg),
            "metrics": _empty_metrics(),
            "gates": {"rescue_stress_passed": False},
            "blockers": ["no paired YES/NO quote intents available for rescue stress"],
            "status": "rescue_stress_failed",
            "safety": _SAFETY,
        }

    latest_ts = scenarios["timestamp"].max()
    latest = scenarios[scenarios["timestamp"].eq(latest_ts)].copy()
    latest_market = pd.DataFrame(
        [_latest_market_row(group) for _, group in latest.groupby("condition_id", dropna=False)]
    )

    scenario_count = int(len(scenarios))
    feasible = scenarios["rescue_price_feasible"].astype(bool)
    loss = scenarios["one_sided_loss_to_zero"]
    latest_blocked_loss = float(latest_market["blocked_loss_to_zero"].sum()) if not latest_market.empty else 0.0
    latest_worst_loss = float(latest_market["worst_one_sided_loss_to_zero"].sum()) if not latest_market.empty else 0.0
    latest_exit_loss = float(latest_market["immediate_exit_loss_if_rescue_fails"].sum()) if not latest_market.empty else 0.0
    p05_edge = _quantile(scenarios.loc[feasible, "pair_edge_per_share"], 0.05)
    min_edge = _finite_min(scenarios.loc[feasible, "pair_edge_per_share"])
    loss_weighted_feasible = (
        float(loss[feasible].sum() / loss.sum()) if float(loss.sum()) > 0 else math.nan
    )
    metrics = {
        "scenario_count": scenario_count,
        "latest_timestamp": str(latest_ts),
        "latest_markets": int(latest["condition_id"].nunique()),
        "price_feasible_scenarios": int(feasible.sum()),
        "price_feasible_rate": float(feasible.mean()) if scenario_count else math.nan,
        "loss_weighted_price_feasible_rate": loss_weighted_feasible,
        "p05_pair_edge_per_share_if_rescued": p05_edge,
        "min_pair_edge_per_share_if_rescued": min_edge,
        "latest_worst_one_sided_loss_to_zero": latest_worst_loss,
        "latest_blocked_loss_to_zero": latest_blocked_loss,
        "latest_blocked_loss_fraction": latest_blocked_loss / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "latest_immediate_exit_loss_if_rescue_fails": latest_exit_loss,
        "latest_immediate_exit_loss_fraction": latest_exit_loss / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "latest_rescueable_loss_to_zero": max(0.0, latest_worst_loss - latest_blocked_loss),
    }
    gates = {
        "price_feasible_rate_passed": metrics["price_feasible_rate"] >= cfg.min_price_feasible_rate,
        "latest_blocked_loss_gate_passed": metrics["latest_blocked_loss_fraction"]
        <= cfg.max_latest_blocked_loss_fraction,
        "immediate_exit_loss_gate_passed": metrics["latest_immediate_exit_loss_fraction"]
        <= cfg.max_immediate_exit_loss_fraction,
        "pair_edge_gate_passed": min_edge >= cfg.rescue_min_pair_edge_per_share - 1e-12,
    }
    gates["rescue_stress_passed"] = bool(all(gates.values()))
    return {
        "config": asdict(cfg),
        "metrics": metrics,
        "gates": gates,
        "blockers": _blockers(gates, cfg),
        "scenario_sample": _json_records(scenarios.tail(20)),
        "latest_market_stress": _json_records(latest_market),
        "status": "rescue_stress_passed" if gates["rescue_stress_passed"] else "rescue_stress_failed",
        "interpretation": {
            "proof_type": "price-feasible rescue stress, not executable fill proof",
            "requires_for_deployment": [
                "real order/fill/cancel telemetry",
                "queue position or fill-probability model",
                "paid reward reconciliation",
            ],
        },
        "safety": _SAFETY,
    }


def _normalise_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes.empty:
        return pd.DataFrame()
    required = {"timestamp", "condition_id", "side", "bid_price", "size_shares"}
    missing = required - set(quotes.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")
    q = quotes.copy()
    q["timestamp"] = pd.to_datetime(q["timestamp"], utc=True, errors="coerce")
    q["condition_id"] = q["condition_id"].astype(str)
    q["side"] = q["side"].astype(str).str.upper()
    q["bid_price"] = pd.to_numeric(q["bid_price"], errors="coerce")
    q["size_shares"] = pd.to_numeric(q["size_shares"], errors="coerce")
    if "quote_offset" in q:
        q["quote_offset"] = pd.to_numeric(q["quote_offset"], errors="coerce").fillna(0.0)
    else:
        q["quote_offset"] = 0.0
    if "cluster" not in q:
        q["cluster"] = "unknown"
    return q.dropna(subset=["timestamp", "condition_id", "side", "bid_price", "size_shares"]).sort_values(
        ["timestamp", "condition_id", "side"]
    )


def _scenario_frame(q: pd.DataFrame, cfg: RescueStressConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (ts, condition_id), group in q.groupby(["timestamp", "condition_id"], sort=True):
        yes = group[group["side"].eq("YES")]
        no = group[group["side"].eq("NO")]
        if yes.empty or no.empty:
            continue
        by_side = {"YES": yes.iloc[0], "NO": no.iloc[0]}
        for side, row in by_side.items():
            opp_side = "NO" if side == "YES" else "YES"
            opp = by_side[opp_side]
            entry = float(row["bid_price"])
            size = float(row["size_shares"])
            opp_bid = float(opp["bid_price"])
            opp_mid_est = opp_bid + float(opp.get("quote_offset", 0.0))
            max_rescue_bid = 1.0 - entry - cfg.rescue_min_pair_edge_per_share
            post_only_rescue_bid = opp_mid_est - cfg.rescue_quote_offset
            rescue_bid = min(max_rescue_bid, post_only_rescue_bid)
            feasible = (
                math.isfinite(rescue_bid)
                and rescue_bid >= cfg.min_rescue_bid
                and max_rescue_bid >= cfg.min_rescue_bid
                and post_only_rescue_bid >= cfg.min_rescue_bid
            )
            pair_cost = entry + rescue_bid if feasible else math.nan
            edge = 1.0 - pair_cost if feasible else math.nan
            rows.append(
                {
                    "timestamp": ts,
                    "condition_id": str(condition_id),
                    "cluster": str(row.get("cluster", "unknown")),
                    "filled_side": side,
                    "entry_price": entry,
                    "size_shares": size,
                    "one_sided_loss_to_zero": entry * size,
                    "rescue_side": opp_side,
                    "max_rescue_bid": max_rescue_bid,
                    "post_only_rescue_bid": post_only_rescue_bid,
                    "rescue_bid": rescue_bid if feasible else math.nan,
                    "pair_cost": pair_cost,
                    "pair_edge_per_share": edge,
                    "rescue_price_feasible": bool(feasible and edge >= cfg.rescue_min_pair_edge_per_share - 1e-12),
                    "immediate_exit_loss": size * cfg.exit_slippage,
                }
            )
    return pd.DataFrame(rows)


def _latest_market_row(group: pd.DataFrame) -> dict[str, Any]:
    worst = group.loc[group["one_sided_loss_to_zero"].idxmax()]
    blocked = group[~group["rescue_price_feasible"].astype(bool)]
    blocked_loss = float(blocked["one_sided_loss_to_zero"].max()) if not blocked.empty else 0.0
    return {
        "condition_id": str(worst.name) if "condition_id" not in worst else str(worst["condition_id"]),
        "cluster": str(worst.get("cluster", "unknown")),
        "worst_one_sided_loss_to_zero": float(group["one_sided_loss_to_zero"].max()),
        "blocked_loss_to_zero": blocked_loss,
        "immediate_exit_loss_if_rescue_fails": float(group["immediate_exit_loss"].max()),
        "all_sides_rescue_price_feasible": bool(group["rescue_price_feasible"].all()),
        "min_pair_edge_per_share": _finite_min(group["pair_edge_per_share"]),
    }


def _blockers(gates: dict[str, bool], cfg: RescueStressConfig) -> list[str]:
    labels = {
        "price_feasible_rate_passed": f"price-feasible rescue rate below {cfg.min_price_feasible_rate:.0%}",
        "latest_blocked_loss_gate_passed": (
            f"latest rescue-blocked one-sided loss exceeds {cfg.max_latest_blocked_loss_fraction:.0%} of capital"
        ),
        "immediate_exit_loss_gate_passed": (
            f"immediate exit slippage exceeds {cfg.max_immediate_exit_loss_fraction:.0%} of capital"
        ),
        "pair_edge_gate_passed": "rescued pair edge below configured minimum",
    }
    return [msg for gate, msg in labels.items() if not gates.get(gate, False)]


def _empty_metrics() -> dict[str, Any]:
    return {"scenario_count": 0, "price_feasible_rate": 0.0}


def _finite_min(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.min()) if not clean.empty else math.nan


def _quantile(values: pd.Series, q: float) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.quantile(q)) if not clean.empty else math.nan


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, pd.Timestamp):
                clean[key] = value.isoformat()
            elif isinstance(value, float) and not math.isfinite(value):
                clean[key] = None
            else:
                clean[key] = value
        out.append(clean)
    return out


_SAFETY = "rescue stress audit only; no private keys, order signing, order submission, or cancellation"
