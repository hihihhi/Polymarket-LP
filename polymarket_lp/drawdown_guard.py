from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from polymarket_lp.lp_backtest import LPConfig, simulate_lp


@dataclass(slots=True)
class DrawdownGuardConfig:
    """MDD / inventory survival gate for public-paper LP candidates.

    This is an auditor only. It uses public snapshots and the existing LP
    simulation model to check whether candidate sizing would keep drawdown and
    inventory pressure recoverable under the same parameterized quote rules.
    """

    initial_capital: float = 2_000.0
    min_observation_hours: float = 6.0
    max_mtm_drawdown_fraction: float = 0.10
    max_realized_drawdown_fraction: float = 0.05
    max_open_inventory_fraction: float = 0.50
    max_active_order_fraction: float = 0.70
    min_reward_to_trading_loss_ratio: float = 3.0


def lp_config_from_manifest(manifest: dict[str, Any], base: LPConfig | None = None) -> LPConfig:
    """Build an LPConfig from a parameterized public-paper manifest."""

    cfg = base or LPConfig()
    lp_cfg = manifest.get("lp_config") if isinstance(manifest.get("lp_config"), dict) else {}
    merged = {**lp_cfg, **manifest}
    text = " ".join(
        str(merged.get(key, ""))
        for key in ["_candidate_name", "strategy", "config", "replaced"]
    ).lower()
    quote_size = _num(merged.get("quote_size", merged.get("quote_size_shares")), math.nan)
    residual_cap = _num(merged.get("partial_rescue_max_residual_loss_usdc"), math.nan)
    density = _num(merged.get("min_reward_density_per_day"), math.nan)
    quote_offset = _num(merged.get("quote_offset"), math.nan)
    active_capital_limit = _num(merged.get("active_capital_limit"), math.nan)
    values = {
        "initial_capital": _num(merged.get("initial_capital"), cfg.initial_capital),
        "quote_size_shares": quote_size if math.isfinite(quote_size) else _infer_text_number(text, r"(?:^|[^a-z0-9])q(\d+(?:\.\d+)?)", cfg.quote_size_shares),
        "quote_offset": quote_offset if math.isfinite(quote_offset) else _infer_text_number(text, r"(?:^|[^a-z0-9])offset(\d+(?:\.\d+)?)", cfg.quote_offset),
        "safety_margin": _num(merged.get("safety_margin"), cfg.safety_margin),
        "active_capital_limit": active_capital_limit if math.isfinite(active_capital_limit) else _infer_text_number(text, r"(?:^|[^a-z0-9])active[_-]?cap(\d+(?:\.\d+)?)", cfg.active_capital_limit),
        "excluded_categories": str(merged.get("excluded_categories", cfg.excluded_categories)),
        "min_reward_density_per_day": density if math.isfinite(density) else _infer_density(text, cfg.min_reward_density_per_day),
        "max_recent_vol": _num(merged.get("max_recent_vol"), cfg.max_recent_vol),
        "max_recent_jump": _num(merged.get("max_recent_jump"), cfg.max_recent_jump),
        "vol_quote_multiplier": _num(merged.get("vol_quote_multiplier"), cfg.vol_quote_multiplier),
        "partial_rescue_max_residual_loss_usdc": residual_cap if math.isfinite(residual_cap) else _infer_text_number(text, r"(?:^|[^a-z0-9])cap(\d+(?:\.\d+)?)", cfg.partial_rescue_max_residual_loss_usdc),
    }
    return LPConfig(**{**asdict(cfg), **values})


def evaluate_drawdown_guard(
    snapshots: pd.DataFrame,
    lp_cfg: LPConfig,
    cfg: DrawdownGuardConfig | None = None,
) -> dict[str, Any]:
    """Evaluate whether a candidate passes drawdown/inventory survival gates."""

    cfg = cfg or DrawdownGuardConfig(initial_capital=lp_cfg.initial_capital)
    if snapshots.empty:
        return {
            "config": asdict(cfg),
            "lp_config": asdict(lp_cfg),
            "status": "drawdown_guard_failed",
            "risk_core_passed": False,
            "gates": {"drawdown_guard_passed": False},
            "metrics": _empty_metrics(),
            "blockers": ["no snapshots available"],
            "safety": _SAFETY,
        }

    _, equity, summary_frame = simulate_lp(snapshots, lp_cfg)
    if equity.empty or summary_frame.empty:
        return {
            "config": asdict(cfg),
            "lp_config": asdict(lp_cfg),
            "status": "drawdown_guard_failed",
            "risk_core_passed": False,
            "gates": {"drawdown_guard_passed": False},
            "metrics": _empty_metrics(),
            "blockers": ["simulation produced no equity curve"],
            "safety": _SAFETY,
        }

    summary = summary_frame.iloc[0].to_dict()
    duration_hours = _duration_hours(equity)
    mtm_dd_fraction = abs(_num(summary.get("max_drawdown_mtm_pct"), 0.0))
    realized_dd_fraction = abs(_num(summary.get("max_drawdown_realized_pct"), 0.0))
    max_open_inventory_fraction = _num(summary.get("max_open_inventory_notional"), 0.0) / cfg.initial_capital
    max_active_order_fraction = _num(summary.get("max_active_order_notional"), 0.0) / cfg.initial_capital
    reward_to_loss = _num(summary.get("reward_to_trading_loss_ratio"), math.inf)
    metrics = {
        "duration_hours": duration_hours,
        "total_pnl_usdc": _num(summary.get("total_pnl_usdc")),
        "return_on_initial_capital": _num(summary.get("return_on_initial_capital")),
        "max_drawdown_mtm_usdc": _num(summary.get("max_drawdown_mtm_usdc")),
        "max_drawdown_mtm_fraction": mtm_dd_fraction,
        "max_drawdown_realized_usdc": _num(summary.get("max_drawdown_realized_usdc")),
        "max_drawdown_realized_fraction": realized_dd_fraction,
        "max_open_inventory_notional": _num(summary.get("max_open_inventory_notional")),
        "max_open_inventory_fraction": max_open_inventory_fraction,
        "max_active_order_notional": _num(summary.get("max_active_order_notional")),
        "max_active_order_fraction": max_active_order_fraction,
        "reward_to_trading_loss_ratio": reward_to_loss,
        "profit_factor_trading_only": _num(summary.get("profit_factor_trading_only"), math.inf),
        "pair_completion_ratio_shares": _num(summary.get("pair_completion_ratio_shares")),
        "recovery_factor": _num(summary.get("recovery_factor"), math.inf),
    }
    gates = {
        "sample_hours_gate_passed": duration_hours >= cfg.min_observation_hours,
        "mtm_drawdown_gate_passed": mtm_dd_fraction <= cfg.max_mtm_drawdown_fraction,
        "realized_drawdown_gate_passed": realized_dd_fraction <= cfg.max_realized_drawdown_fraction,
        "open_inventory_gate_passed": max_open_inventory_fraction <= cfg.max_open_inventory_fraction,
        "active_order_gate_passed": max_active_order_fraction <= cfg.max_active_order_fraction,
        "reward_loss_gate_passed": reward_to_loss >= cfg.min_reward_to_trading_loss_ratio,
    }
    risk_core = all(v for k, v in gates.items() if k != "sample_hours_gate_passed")
    gates["drawdown_guard_passed"] = risk_core and gates["sample_hours_gate_passed"]
    status = (
        "drawdown_guard_passed"
        if gates["drawdown_guard_passed"]
        else "drawdown_guard_sample_pending"
        if risk_core and not gates["sample_hours_gate_passed"]
        else "drawdown_guard_failed"
    )
    return {
        "config": asdict(cfg),
        "lp_config": asdict(lp_cfg),
        "status": status,
        "risk_core_passed": risk_core,
        "gates": gates,
        "metrics": metrics,
        "blockers": _blockers(gates, cfg),
        "safety": _SAFETY,
    }


def _blockers(gates: dict[str, bool], cfg: DrawdownGuardConfig) -> list[str]:
    labels = {
        "sample_hours_gate_passed": f"needs at least {cfg.min_observation_hours:.2f} observation hours",
        "mtm_drawdown_gate_passed": f"MTM drawdown exceeds {cfg.max_mtm_drawdown_fraction:.2%}",
        "realized_drawdown_gate_passed": f"realized drawdown exceeds {cfg.max_realized_drawdown_fraction:.2%}",
        "open_inventory_gate_passed": f"open inventory exceeds {cfg.max_open_inventory_fraction:.2%} of capital",
        "active_order_gate_passed": f"active orders exceed {cfg.max_active_order_fraction:.2%} of capital",
        "reward_loss_gate_passed": f"reward/trading-loss ratio below {cfg.min_reward_to_trading_loss_ratio:.2f}",
    }
    return [message for gate, message in labels.items() if not gates.get(gate, False)]


def _duration_hours(equity: pd.DataFrame) -> float:
    ts = pd.to_datetime(equity["timestamp"], utc=True, errors="coerce").dropna()
    if ts.empty:
        return 0.0
    return float((ts.max() - ts.min()).total_seconds() / 3600.0)


def _empty_metrics() -> dict[str, Any]:
    return {
        "duration_hours": 0.0,
        "total_pnl_usdc": 0.0,
        "max_drawdown_mtm_fraction": math.nan,
        "max_drawdown_realized_fraction": math.nan,
        "max_open_inventory_fraction": math.nan,
        "max_active_order_fraction": math.nan,
    }


def _num(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _infer_text_number(text: str, pattern: str, default: float) -> float:
    match = re.search(pattern, text)
    if not match:
        return default
    return _num(match.group(1), default)


def _infer_density(text: str, default: float) -> float:
    explicit = _infer_text_number(text, r"(?:^|[^a-z0-9])density(\d+(?:\.\d+)?)", math.nan)
    if math.isfinite(explicit):
        return explicit
    short = re.search(r"(?:^|[^a-z0-9])d(\d{3})(?:$|[^a-z0-9])", text)
    if short:
        return _num(short.group(1), 0.0) / 100.0
    return default


_SAFETY = "drawdown guard audit only; no private keys, signing, order submission, or cancellation"
