from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any

import pandas as pd


@dataclass(slots=True)
class CapitalRiskStressConfig:
    """Capital survival stress test for LP quote intents.

    This is an auditor only. It converts the latest paper quote intents into
    worst-case capital-at-risk numbers. It does not sign, submit, cancel, or
    inspect orders.
    """

    initial_capital: float = 2_000.0
    min_cash_reserve_fraction: float = 0.20
    max_unhedged_loss_fraction: float = 0.80
    max_capped_loss_fraction: float = 0.25
    max_capped_recovery_days: float = 10.0
    max_unpaired_per_market: float = 60.0
    max_total_unpaired: float = 450.0
    max_cluster_unpaired: float = 250.0
    min_latest_markets: int = 2
    max_single_market_active_fraction: float = 0.35
    max_single_cluster_active_fraction: float = 0.70
    max_single_market_unhedged_loss_fraction: float = 0.20
    max_single_cluster_unhedged_loss_fraction: float = 0.50
    exit_slippage: float = 0.005
    days_per_month: float = 30.0


def config_from_lp_manifest(
    manifest: dict[str, Any] | None,
    cfg: CapitalRiskStressConfig | None = None,
) -> CapitalRiskStressConfig:
    """Overlay paper/live LPConfig values from a run manifest onto stress config."""

    out = cfg or CapitalRiskStressConfig()
    lp_cfg = manifest.get("lp_config", {}) if isinstance(manifest, dict) else {}
    if not isinstance(lp_cfg, dict):
        return out
    updates: dict[str, float] = {}
    for key in [
        "initial_capital",
        "max_unpaired_per_market",
        "max_total_unpaired",
        "max_cluster_unpaired",
        "exit_slippage",
    ]:
        if key in lp_cfg:
            updates[key] = _float(lp_cfg.get(key), getattr(out, key))
    return replace(out, **updates)


def evaluate_capital_risk_stress(
    quotes: pd.DataFrame,
    *,
    target_status: dict[str, Any] | None = None,
    cfg: CapitalRiskStressConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or CapitalRiskStressConfig()
    q = _normalise_quotes(quotes)
    if q.empty:
        return {
            "config": asdict(cfg),
            "metrics": _empty_metrics(),
            "gates": {"capital_risk_stress_passed": False},
            "blockers": ["no quote intents available for capital risk stress"],
            "status": "capital_risk_stress_failed",
            "safety": _SAFETY,
        }

    latest_ts = q["timestamp"].max()
    latest = q[q["timestamp"].eq(latest_ts)].copy()
    grouped = latest.groupby("condition_id", dropna=False)
    market_rows: list[dict[str, Any]] = []
    for condition_id, group in grouped:
        side_cost = group.assign(cost=group["bid_price"] * group["size_shares"]).groupby("side")["cost"].sum()
        side_size = group.groupby("side")["size_shares"].sum()
        yes_cost = float(side_cost.get("YES", 0.0))
        no_cost = float(side_cost.get("NO", 0.0))
        yes_size = float(side_size.get("YES", 0.0))
        no_size = float(side_size.get("NO", 0.0))
        paired_size = min(yes_size, no_size)
        yes_bid = yes_cost / yes_size if yes_size > 0 else 0.0
        no_bid = no_cost / no_size if no_size > 0 else 0.0
        paired_cost = paired_size * (yes_bid + no_bid)
        paired_payoff = paired_size
        one_side_worst = max(yes_cost, no_cost)
        market_rows.append(
            {
                "condition_id": str(condition_id),
                "cluster": str(group["cluster"].iloc[0]) if "cluster" in group else "unknown",
                "quote_rows": int(len(group)),
                "yes_cost": yes_cost,
                "no_cost": no_cost,
                "one_side_worst_loss_to_zero": one_side_worst,
                "paired_size": paired_size,
                "pair_cost_per_share": yes_bid + no_bid if paired_size > 0 else math.nan,
                "locked_pair_edge_usdc": paired_payoff - paired_cost,
                "active_pair_notional": float(group["active_order_notional_pair"].max())
                if "active_order_notional_pair" in group
                else yes_cost + no_cost,
            }
        )

    market = pd.DataFrame(market_rows)
    active_notional = float(market["active_pair_notional"].sum())
    cash_reserve = cfg.initial_capital - active_notional
    cash_reserve_fraction = cash_reserve / cfg.initial_capital if cfg.initial_capital > 0 else -math.inf
    unhedged_loss = float(market["one_side_worst_loss_to_zero"].sum())
    single_market_loss = float(market["one_side_worst_loss_to_zero"].max())
    by_cluster = market.groupby("cluster")["one_side_worst_loss_to_zero"].sum()
    by_cluster_active = market.groupby("cluster")["active_pair_notional"].sum()
    latest_markets = int(market["condition_id"].nunique())
    max_single_market_active = float(market["active_pair_notional"].max())
    max_single_cluster_active = float(by_cluster_active.max())
    max_single_cluster_loss = float(by_cluster.max())
    capped_by_market = float(market["one_side_worst_loss_to_zero"].clip(upper=cfg.max_unpaired_per_market).sum())
    capped_by_cluster = float(by_cluster.clip(upper=cfg.max_cluster_unpaired).sum())
    capped_loss = min(unhedged_loss, cfg.max_total_unpaired, capped_by_market, capped_by_cluster)
    immediate_exit_loss = float(latest.drop_duplicates("condition_id")["size_shares"].sum() * cfg.exit_slippage)
    monthly_p05 = _captured_p05_monthly(target_status)
    daily_p05 = monthly_p05 / cfg.days_per_month if monthly_p05 > 0 and cfg.days_per_month > 0 else math.nan
    unhedged_recovery_days = unhedged_loss / daily_p05 if daily_p05 and math.isfinite(daily_p05) else math.inf
    capped_recovery_days = capped_loss / daily_p05 if daily_p05 and math.isfinite(daily_p05) else math.inf
    min_pair_edge = float(market["locked_pair_edge_usdc"].min()) if len(market) else math.nan
    max_pair_cost = float(market["pair_cost_per_share"].max()) if len(market) else math.nan

    metrics = {
        "latest_timestamp": str(latest_ts),
        "latest_quote_rows": int(len(latest)),
        "latest_markets": latest_markets,
        "active_pair_notional": active_notional,
        "cash_reserve_usdc": cash_reserve,
        "cash_reserve_fraction": cash_reserve_fraction,
        "max_single_market_active_notional": max_single_market_active,
        "max_single_market_active_fraction": max_single_market_active / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "max_single_cluster_active_notional": max_single_cluster_active,
        "max_single_cluster_active_fraction": max_single_cluster_active / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "single_market_worst_one_side_loss_to_zero": single_market_loss,
        "single_market_worst_one_side_loss_fraction": single_market_loss / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "single_cluster_worst_one_side_loss_to_zero": max_single_cluster_loss,
        "single_cluster_worst_one_side_loss_fraction": max_single_cluster_loss / cfg.initial_capital
        if cfg.initial_capital > 0
        else math.inf,
        "all_active_unhedged_one_side_loss_to_zero": unhedged_loss,
        "unhedged_loss_fraction_of_capital": unhedged_loss / cfg.initial_capital if cfg.initial_capital > 0 else math.inf,
        "configured_inventory_cap_loss_to_zero": capped_loss,
        "configured_inventory_cap_loss_fraction": capped_loss / cfg.initial_capital if cfg.initial_capital > 0 else math.inf,
        "immediate_exit_slippage_loss_if_caps_reject": immediate_exit_loss,
        "captured_p05_monthly_income_usdc": monthly_p05,
        "unhedged_recovery_days_at_p05_income": unhedged_recovery_days,
        "capped_recovery_days_at_p05_income": capped_recovery_days,
        "max_pair_cost_per_share": max_pair_cost,
        "min_locked_pair_edge_usdc": min_pair_edge,
    }
    gates = {
        "cash_reserve_gate_passed": cash_reserve_fraction >= cfg.min_cash_reserve_fraction,
        "latest_market_count_gate_passed": latest_markets >= cfg.min_latest_markets,
        "single_market_active_gate_passed": metrics["max_single_market_active_fraction"]
        <= cfg.max_single_market_active_fraction,
        "single_cluster_active_gate_passed": metrics["max_single_cluster_active_fraction"]
        <= cfg.max_single_cluster_active_fraction,
        "single_market_loss_gate_passed": metrics["single_market_worst_one_side_loss_fraction"]
        <= cfg.max_single_market_unhedged_loss_fraction,
        "single_cluster_loss_gate_passed": metrics["single_cluster_worst_one_side_loss_fraction"]
        <= cfg.max_single_cluster_unhedged_loss_fraction,
        "no_total_ruin_unhedged_gate_passed": unhedged_loss < cfg.initial_capital,
        "unhedged_loss_gate_passed": metrics["unhedged_loss_fraction_of_capital"] <= cfg.max_unhedged_loss_fraction,
        "configured_cap_loss_gate_passed": metrics["configured_inventory_cap_loss_fraction"] <= cfg.max_capped_loss_fraction,
        "configured_cap_recovery_gate_passed": capped_recovery_days <= cfg.max_capped_recovery_days,
        "pair_lock_edge_gate_passed": min_pair_edge >= -1e-9 and max_pair_cost <= 1.0 + 1e-9,
    }
    gates["capital_risk_stress_passed"] = bool(all(gates.values()))
    blockers = _blockers(gates, cfg)
    return {
        "config": asdict(cfg),
        "metrics": metrics,
        "market_stress": market_rows,
        "gates": gates,
        "blockers": blockers,
        "status": "capital_risk_stress_passed" if gates["capital_risk_stress_passed"] else "capital_risk_stress_failed",
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
    if "active_order_notional_pair" in q:
        q["active_order_notional_pair"] = pd.to_numeric(q["active_order_notional_pair"], errors="coerce")
    else:
        q["active_order_notional_pair"] = q["bid_price"] * q["size_shares"]
    if "cluster" not in q:
        q["cluster"] = "unknown"
    return q.dropna(subset=["timestamp", "condition_id", "bid_price", "size_shares"]).sort_values(
        ["timestamp", "condition_id", "side"]
    )


def _captured_p05_monthly(target_status: dict[str, Any] | None) -> float:
    if not isinstance(target_status, dict):
        return math.nan
    stress = target_status.get("capture_stress_grid")
    if isinstance(stress, list):
        best = None
        for row in stress:
            if isinstance(row, dict) and abs(_float(row.get("capture_rate"), -1.0) - 0.5) < 1e-12:
                best = _float(row.get("captured_net_monthly_p05"), math.nan)
                break
        if best is not None:
            return best
    bootstrap = target_status.get("bootstrap_target")
    if isinstance(bootstrap, dict):
        value = _float(bootstrap.get("captured_net_monthly_p05"), math.nan)
        if math.isfinite(value):
            return value
    return math.nan


def _blockers(gates: dict[str, bool], cfg: CapitalRiskStressConfig) -> list[str]:
    names = {
        "cash_reserve_gate_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.0%}",
        "latest_market_count_gate_passed": f"latest quoted markets below {cfg.min_latest_markets}",
        "single_market_active_gate_passed": f"single-market active notional exceeds {cfg.max_single_market_active_fraction:.0%} of capital",
        "single_cluster_active_gate_passed": f"single-cluster active notional exceeds {cfg.max_single_cluster_active_fraction:.0%} of capital",
        "single_market_loss_gate_passed": f"single-market one-side loss exceeds {cfg.max_single_market_unhedged_loss_fraction:.0%} of capital",
        "single_cluster_loss_gate_passed": f"single-cluster one-side loss exceeds {cfg.max_single_cluster_unhedged_loss_fraction:.0%} of capital",
        "no_total_ruin_unhedged_gate_passed": "unhedged one-side fill stress can lose all capital",
        "unhedged_loss_gate_passed": f"unhedged one-side fill loss exceeds {cfg.max_unhedged_loss_fraction:.0%} of capital",
        "configured_cap_loss_gate_passed": f"configured inventory-cap loss exceeds {cfg.max_capped_loss_fraction:.0%} of capital",
        "configured_cap_recovery_gate_passed": f"configured-cap recovery exceeds {cfg.max_capped_recovery_days:.1f} days",
        "pair_lock_edge_gate_passed": "two-sided pair fill is not locked non-negative",
    }
    return [message for gate, message in names.items() if not gates.get(gate, False)]


def _empty_metrics() -> dict[str, Any]:
    return {
        "latest_quote_rows": 0,
        "latest_markets": 0,
        "active_pair_notional": 0.0,
        "cash_reserve_usdc": 0.0,
        "cash_reserve_fraction": 0.0,
    }


def _float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_SAFETY = "capital stress audit only; no private keys, order signing, order submission, or cancellation"
