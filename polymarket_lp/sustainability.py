from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SustainabilityStressConfig:
    """Income-survival stress for the $1k/month LP objective.

    The audit is deliberately conservative: by default it uses the lower of the
    selected-allocation and current packet p05 monthly estimates, then applies
    explicit capture, reward-regime, and one-time loss shocks.
    """

    initial_capital: float = 2_000.0
    target_monthly_usdc: float = 1_000.0
    reference_capture_rate: float = 0.50
    days_per_month: float = 30.0
    max_required_reward_multiplier: float = 0.75
    max_configured_cap_recovery_days: float = 10.0
    max_unhedged_recovery_days: float = 15.0
    min_cash_reserve_fraction: float = 0.40
    max_unhedged_loss_fraction: float = 0.50
    reward_multipliers: tuple[float, ...] = (1.0, 0.75, 0.50, 0.35, 0.25)
    capture_rates: tuple[float, ...] = (1.0, 0.75, 0.50, 0.40, 0.35, 0.25)
    base_policy: str = "min_selected_packet"
    monthly_loss_shocks: tuple[float, ...] = field(default_factory=tuple)


def evaluate_sustainability_stress(
    *,
    evidence_packet: dict[str, Any],
    allocation_selection: dict[str, Any],
    cfg: SustainabilityStressConfig | None = None,
) -> dict[str, Any]:
    """Evaluate whether income target survives explicit capture/reward/loss shocks."""

    cfg = cfg or SustainabilityStressConfig()
    selected_metrics = _dict(_dict(allocation_selection.get("selected")).get("metrics"))
    income = _dict(evidence_packet.get("income"))
    risk = _dict(evidence_packet.get("risk"))

    selected_capture_p05 = _num(selected_metrics.get("captured_net_monthly_p05"))
    selected_raw_p05 = (
        selected_capture_p05 / cfg.reference_capture_rate
        if cfg.reference_capture_rate > 0
        else math.nan
    )
    packet_raw_p05 = _num(income.get("p05_monthly_raw"))
    if not math.isfinite(packet_raw_p05):
        packet_raw_p05 = (
            _num(income.get("p05_monthly_50pct_capture")) / cfg.reference_capture_rate
        )
    base_raw_p05 = _choose_base(selected_raw_p05, packet_raw_p05, cfg.base_policy)

    selected_configured_loss = _num(selected_metrics.get("configured_cap_loss"), 0.0)
    packet_configured_loss = _num(
        risk.get("configured_inventory_cap_loss_to_zero"), 0.0
    )
    configured_cap_loss = max(selected_configured_loss, packet_configured_loss)
    selected_unhedged_loss = _num(selected_metrics.get("unhedged_loss_to_zero"), 0.0)
    packet_unhedged_loss = _num(
        risk.get("all_active_unhedged_one_side_loss_to_zero"), 0.0
    )
    unhedged_loss = max(selected_unhedged_loss, packet_unhedged_loss)
    loss_shocks = _loss_shocks(cfg, configured_cap_loss, unhedged_loss)

    stress_rows = [
        _stress_row(
            base_raw_p05=base_raw_p05,
            capture_rate=capture,
            reward_multiplier=reward,
            monthly_loss=loss["monthly_loss_usdc"],
            loss_label=loss["label"],
            cfg=cfg,
        )
        for capture in cfg.capture_rates
        for reward in cfg.reward_multipliers
        for loss in loss_shocks
    ]

    reference_income = base_raw_p05 * cfg.reference_capture_rate
    configured_cap_reference_income = reference_income - configured_cap_loss
    configured_cap_recovery = _recovery_days(configured_cap_loss, reference_income, cfg)
    unhedged_recovery = _recovery_days(unhedged_loss, reference_income, cfg)
    breakeven_reward_no_loss = _required_reward_multiplier(
        base_raw_p05, cfg.reference_capture_rate, 0.0, cfg
    )
    breakeven_reward_configured = _required_reward_multiplier(
        base_raw_p05, cfg.reference_capture_rate, configured_cap_loss, cfg
    )
    breakeven_reward_unhedged = _required_reward_multiplier(
        base_raw_p05, cfg.reference_capture_rate, unhedged_loss, cfg
    )

    gates = {
        "reference_income_passed": reference_income >= cfg.target_monthly_usdc,
        "configured_cap_shock_income_passed": configured_cap_reference_income
        >= cfg.target_monthly_usdc,
        "configured_cap_recovery_passed": configured_cap_recovery
        <= cfg.max_configured_cap_recovery_days,
        "cash_reserve_passed": _num(selected_metrics.get("cash_reserve_fraction"))
        >= cfg.min_cash_reserve_fraction,
        "unhedged_loss_fraction_passed": _num(
            selected_metrics.get("unhedged_loss_fraction")
        )
        <= cfg.max_unhedged_loss_fraction,
        "unhedged_recovery_warning_passed": unhedged_recovery
        <= cfg.max_unhedged_recovery_days,
        "breakeven_reward_margin_passed": breakeven_reward_configured
        <= cfg.max_required_reward_multiplier,
    }
    required = [
        "reference_income_passed",
        "configured_cap_shock_income_passed",
        "configured_cap_recovery_passed",
        "cash_reserve_passed",
        "unhedged_loss_fraction_passed",
        "breakeven_reward_margin_passed",
    ]
    blockers = _blockers(gates, cfg)
    status = (
        "sustainability_stress_passed"
        if all(gates[name] for name in required)
        else "sustainability_stress_failed"
    )

    return {
        "config": _config_dict(cfg),
        "status": status,
        "metrics": {
            "base_policy": cfg.base_policy,
            "selected_raw_p05_monthly": selected_raw_p05,
            "packet_raw_p05_monthly": packet_raw_p05,
            "base_raw_p05_monthly": base_raw_p05,
            "reference_capture_rate": cfg.reference_capture_rate,
            "reference_monthly_income": reference_income,
            "configured_cap_loss_usdc": configured_cap_loss,
            "configured_cap_reference_monthly_after_loss": configured_cap_reference_income,
            "configured_cap_recovery_days": configured_cap_recovery,
            "unhedged_loss_usdc": unhedged_loss,
            "unhedged_loss_fraction": unhedged_loss / cfg.initial_capital
            if cfg.initial_capital > 0
            else math.inf,
            "unhedged_recovery_days": unhedged_recovery,
            "breakeven_reward_multiplier_no_loss_at_reference_capture": breakeven_reward_no_loss,
            "breakeven_reward_multiplier_configured_cap_at_reference_capture": breakeven_reward_configured,
            "breakeven_reward_multiplier_unhedged_at_reference_capture": breakeven_reward_unhedged,
            "target_monthly_usdc": cfg.target_monthly_usdc,
        },
        "gates": gates,
        "blockers": blockers,
        "stress_rows": stress_rows,
        "safety": "sustainability stress audit only; no private keys, signing, order submission, or cancellation",
    }


def _stress_row(
    *,
    base_raw_p05: float,
    capture_rate: float,
    reward_multiplier: float,
    monthly_loss: float,
    loss_label: str,
    cfg: SustainabilityStressConfig,
) -> dict[str, Any]:
    gross = base_raw_p05 * capture_rate * reward_multiplier
    net = gross - monthly_loss
    return {
        "capture_rate": capture_rate,
        "reward_multiplier": reward_multiplier,
        "loss_label": loss_label,
        "monthly_loss_usdc": monthly_loss,
        "gross_monthly_p05_usdc": gross,
        "net_monthly_after_loss_usdc": net,
        "target_passed": net >= cfg.target_monthly_usdc,
        "recovery_days": _recovery_days(monthly_loss, gross, cfg),
    }


def _loss_shocks(
    cfg: SustainabilityStressConfig,
    configured_cap_loss: float,
    unhedged_loss: float,
) -> list[dict[str, Any]]:
    shocks = [
        {"label": "no_loss", "monthly_loss_usdc": 0.0},
        {"label": "configured_cap_loss", "monthly_loss_usdc": configured_cap_loss},
        {"label": "all_active_unhedged_loss", "monthly_loss_usdc": unhedged_loss},
    ]
    shocks.extend(
        {"label": f"custom_loss_{i}", "monthly_loss_usdc": max(0.0, float(loss))}
        for i, loss in enumerate(cfg.monthly_loss_shocks)
    )
    return shocks


def _choose_base(selected_raw: float, packet_raw: float, policy: str) -> float:
    policy = policy.strip().lower()
    values = [x for x in [selected_raw, packet_raw] if math.isfinite(x)]
    if not values:
        return math.nan
    if policy == "selected":
        return selected_raw
    if policy == "packet":
        return packet_raw
    if policy == "max_selected_packet":
        return max(values)
    if policy != "min_selected_packet":
        raise ValueError(
            "base_policy must be one of: min_selected_packet, max_selected_packet, selected, packet"
        )
    return min(values)


def _required_reward_multiplier(
    base_raw_p05: float,
    capture_rate: float,
    monthly_loss: float,
    cfg: SustainabilityStressConfig,
) -> float:
    denom = base_raw_p05 * capture_rate
    if denom <= 0 or not math.isfinite(denom):
        return math.inf
    return (cfg.target_monthly_usdc + monthly_loss) / denom


def _recovery_days(
    loss: float, monthly_income: float, cfg: SustainabilityStressConfig
) -> float:
    if loss <= 0:
        return 0.0
    if (
        monthly_income <= 0
        or cfg.days_per_month <= 0
        or not math.isfinite(monthly_income)
    ):
        return math.inf
    return loss / (monthly_income / cfg.days_per_month)


def _blockers(gates: dict[str, bool], cfg: SustainabilityStressConfig) -> list[str]:
    labels = {
        "reference_income_passed": f"reference {cfg.reference_capture_rate:.0%} capture p05 income below target",
        "configured_cap_shock_income_passed": "configured-cap monthly loss shock drops p05 income below target",
        "configured_cap_recovery_passed": f"configured-cap recovery exceeds {cfg.max_configured_cap_recovery_days:.1f} days",
        "cash_reserve_passed": f"cash reserve below {cfg.min_cash_reserve_fraction:.0%}",
        "unhedged_loss_fraction_passed": f"unhedged loss exceeds {cfg.max_unhedged_loss_fraction:.0%} of capital",
        "unhedged_recovery_warning_passed": f"unhedged recovery exceeds {cfg.max_unhedged_recovery_days:.1f} days",
        "breakeven_reward_margin_passed": (
            f"configured-cap breakeven reward multiplier exceeds {cfg.max_required_reward_multiplier:.0%}"
        ),
    }
    return [message for gate, message in labels.items() if not gates.get(gate, False)]


def _config_dict(cfg: SustainabilityStressConfig) -> dict[str, Any]:
    out = asdict(cfg)
    out["reward_multipliers"] = list(cfg.reward_multipliers)
    out["capture_rates"] = list(cfg.capture_rates)
    out["monthly_loss_shocks"] = list(cfg.monthly_loss_shocks)
    return out


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
