from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from .lp_backtest import LPConfig


def apply_risk_governor_to_lp_config(
    cfg: LPConfig,
    risk_governor: dict[str, Any],
    *,
    require_core_passed: bool = True,
) -> tuple[LPConfig, dict[str, Any]]:
    """Apply risk-governor sizing limits to an LPConfig.

    This function is intentionally mechanical: it consumes a risk-governor JSON
    artifact and does not embed q300/q800 or strategy-specific thresholds.
    """

    metrics = _dict(risk_governor.get("metrics"))
    risk_core_passed = bool(risk_governor.get("risk_core_passed", False))
    if require_core_passed and not risk_core_passed:
        raise ValueError("risk governor core gates are not passed")

    recommended_qsize = _num(metrics.get("recommended_qsize"), cfg.quote_size_shares)
    max_active_by_cash = _num(
        metrics.get("max_active_pair_notional_by_cash"), cfg.active_capital_limit
    )
    recommended_scale = _num(metrics.get("recommended_scale"), 1.0)
    if not math.isfinite(recommended_qsize) or recommended_qsize <= 0:
        raise ValueError("risk governor recommended_qsize is not positive")
    if not math.isfinite(max_active_by_cash) or max_active_by_cash <= 0:
        raise ValueError(
            "risk governor max_active_pair_notional_by_cash is not positive"
        )

    governed = replace(
        cfg,
        quote_size_shares=float(recommended_qsize),
        active_capital_limit=float(min(cfg.active_capital_limit, max_active_by_cash)),
    )
    metadata = {
        "risk_governor_status": risk_governor.get("status"),
        "risk_core_passed": risk_core_passed,
        "deployment_allowed": bool(risk_governor.get("deployment_allowed", False)),
        "recommended_scale": recommended_scale,
        "quote_size_before": cfg.quote_size_shares,
        "quote_size_after": governed.quote_size_shares,
        "active_capital_limit_before": cfg.active_capital_limit,
        "active_capital_limit_after": governed.active_capital_limit,
        "safety": "risk-governed config application only; no private keys, signing, order submission, or cancellation",
    }
    return governed, metadata


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
