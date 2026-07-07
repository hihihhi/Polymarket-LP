from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class TargetMonitorConfig:
    """Configurable gate for a monthly LP income target.

    The monitor intentionally separates a public-paper reward-density gate from
    deployment proof. Public snapshots can show that a regime is rich enough,
    but they do not prove queue priority, paid rewards, partial fills, cancel
    latency, or real inventory losses.
    """

    initial_capital: float = 2_000.0
    target_monthly_usdc: float = 1_000.0
    reward_to_loss_haircut: float = 8.0
    days_per_month: float = 30.0
    min_observation_hours: float = 24.0
    max_fill_proxy_rate: float = 0.0
    max_stale_fill_rate: float = 0.0
    max_pending_quote_rate: float = 0.05
    min_capture_margin: float = 1.0
    min_unique_markets: int = 1
    max_active_pair_notional: float = math.inf
    paid_reward_verified: bool = False


def target_monitor_from_summary(summary: dict[str, Any], cfg: TargetMonitorConfig | None = None) -> dict[str, Any]:
    """Evaluate whether a paper LP slice is rich enough for a monthly target.

    ``summary`` is the JSON emitted by ``paper_analyze.py``. The output is a
    deterministic diagnostics dictionary suitable for reports and CI-style
    gates. No strategy thresholds are hidden: every gate comes from ``cfg``.
    """

    cfg = cfg or TargetMonitorConfig()
    duration_hours = _to_float(summary.get("duration_hours"))
    estimated_reward = _to_float(summary.get("estimated_reward_accrual_usdc"))
    avg_active = _to_float(summary.get("avg_active_pair_notional"))
    max_active = _to_float(summary.get("max_active_pair_notional"), avg_active)
    fill_proxy = _to_float(summary.get("fill_proxy_rate"))
    stale_fill = _to_float(summary.get("stale_fill_rate"))
    pending_quote = _to_float(summary.get("pending_quote_rate"), 0.0)
    quote_rows = int(_to_float(summary.get("quote_rows"), 0.0))
    unique_markets = int(_to_float(summary.get("unique_markets_quoted"), 1.0))

    gross_reward_per_day = estimated_reward / duration_hours * 24.0 if duration_hours > 0 else math.nan
    gross_reward_monthly = gross_reward_per_day * cfg.days_per_month if math.isfinite(gross_reward_per_day) else math.nan
    observed_density_per_day = gross_reward_per_day / avg_active if avg_active > 0 and math.isfinite(gross_reward_per_day) else math.nan

    net_fraction_after_loss = _net_fraction_after_loss(cfg.reward_to_loss_haircut)
    required_gross_reward_per_day = (cfg.target_monthly_usdc / cfg.days_per_month) / max(net_fraction_after_loss, 1e-12)
    required_density_per_day = required_gross_reward_per_day / avg_active if avg_active > 0 else math.nan
    net_monthly_after_loss = gross_reward_monthly * net_fraction_after_loss if math.isfinite(gross_reward_monthly) else math.nan
    capture_needed_for_target = cfg.target_monthly_usdc / net_monthly_after_loss if net_monthly_after_loss > 0 else math.inf

    density_gate_passed = bool(
        math.isfinite(observed_density_per_day)
        and math.isfinite(required_density_per_day)
        and observed_density_per_day >= required_density_per_day
    )
    capture_gate_passed = bool(math.isfinite(capture_needed_for_target) and capture_needed_for_target <= cfg.min_capture_margin)
    risk_proxy_gate_passed = bool(
        fill_proxy <= cfg.max_fill_proxy_rate
        and stale_fill <= cfg.max_stale_fill_rate
        and pending_quote <= cfg.max_pending_quote_rate
    )
    diversification_gate_passed = bool(unique_markets >= cfg.min_unique_markets)
    active_notional_gate_passed = bool(max_active <= cfg.max_active_pair_notional)
    sample_gate_passed = bool(duration_hours >= cfg.min_observation_hours)
    deployment_proof_passed = bool(
        density_gate_passed
        and capture_gate_passed
        and risk_proxy_gate_passed
        and diversification_gate_passed
        and active_notional_gate_passed
        and sample_gate_passed
        and cfg.paid_reward_verified
    )

    return {
        "config": asdict(cfg),
        "input": {
            "duration_hours": duration_hours,
            "quote_rows": quote_rows,
            "unique_markets_quoted": unique_markets,
            "estimated_reward_accrual_usdc": estimated_reward,
            "avg_active_pair_notional": avg_active,
            "max_active_pair_notional": max_active,
            "fill_proxy_rate": fill_proxy,
            "stale_fill_rate": stale_fill,
            "pending_quote_rate": pending_quote,
        },
        "target_math": {
            "net_fraction_after_loss_haircut": net_fraction_after_loss,
            "gross_reward_per_day": gross_reward_per_day,
            "gross_reward_monthly": gross_reward_monthly,
            "net_monthly_after_loss_haircut": net_monthly_after_loss,
            "observed_reward_density_per_day": observed_density_per_day,
            "required_gross_reward_per_day": required_gross_reward_per_day,
            "required_reward_density_per_day": required_density_per_day,
            "capture_needed_for_target": capture_needed_for_target,
            "target_return_on_capital_monthly": cfg.target_monthly_usdc / cfg.initial_capital if cfg.initial_capital else math.nan,
        },
        "gates": {
            "density_gate_passed": density_gate_passed,
            "capture_gate_passed": capture_gate_passed,
            "risk_proxy_gate_passed": risk_proxy_gate_passed,
            "diversification_gate_passed": diversification_gate_passed,
            "active_notional_gate_passed": active_notional_gate_passed,
            "sample_gate_passed": sample_gate_passed,
            "paid_reward_verified": bool(cfg.paid_reward_verified),
            "deployment_proof_passed": deployment_proof_passed,
        },
        "status": _status(
            density_gate_passed=density_gate_passed,
            capture_gate_passed=capture_gate_passed,
            risk_proxy_gate_passed=risk_proxy_gate_passed,
            diversification_gate_passed=diversification_gate_passed,
            active_notional_gate_passed=active_notional_gate_passed,
            sample_gate_passed=sample_gate_passed,
            paid_reward_verified=cfg.paid_reward_verified,
        ),
    }


def _net_fraction_after_loss(reward_to_loss: float) -> float:
    if reward_to_loss <= 0 or not math.isfinite(reward_to_loss):
        return 0.0
    return max(0.0, 1.0 - 1.0 / reward_to_loss)


def _to_float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _status(
    *,
    density_gate_passed: bool,
    capture_gate_passed: bool,
    risk_proxy_gate_passed: bool,
    diversification_gate_passed: bool,
    active_notional_gate_passed: bool,
    sample_gate_passed: bool,
    paid_reward_verified: bool,
) -> str:
    if not density_gate_passed:
        return "target_density_failed"
    if not capture_gate_passed:
        return "target_capture_failed"
    if not risk_proxy_gate_passed:
        return "risk_proxy_failed"
    if not diversification_gate_passed:
        return "diversification_failed"
    if not active_notional_gate_passed:
        return "active_notional_failed"
    if not sample_gate_passed:
        return "target_density_passed_sample_too_short"
    if not paid_reward_verified:
        return "target_density_passed_needs_paid_reward_telemetry"
    return "deployment_proof_passed"
