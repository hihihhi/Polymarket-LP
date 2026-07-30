from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
DEFAULT_DOCS_URL = "https://docs.polymarket.com/api-reference/geoblock"
DEFAULT_NEW_ORDER_RESTRICTED_COUNTRY_CODES = ("US", "TW", "UM")


@dataclass(slots=True)
class ComplianceAvailabilityConfig:
    """Read-only Polymarket availability gate.

    This gate is deliberately stricter than the API geoblock boolean. A false
    geoblock response is not enough for deployment: legal/entity/API terms must
    also be explicitly cleared before the result can permit new order placement.
    """

    session_country_code: str = ""
    session_region_code: str = ""
    endpoint_url: str = DEFAULT_GEOBLOCK_URL
    docs_url: str = DEFAULT_DOCS_URL
    new_order_restricted_country_codes: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_NEW_ORDER_RESTRICTED_COUNTRY_CODES
    )
    legal_review_cleared: bool = False
    entity_account_cleared: bool = False
    api_terms_cleared: bool = False
    block_on_endpoint_error: bool = True


def fetch_geoblock_status(
    endpoint_url: str = DEFAULT_GEOBLOCK_URL, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    """Fetch the public geoblock endpoint and redact direct IP evidence."""

    req = urllib.request.Request(
        endpoint_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polymarket-lp-compliance-gate/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict) and "ip" in payload:
        payload = dict(payload)
        payload["ip"] = "REDACTED"
    return payload


def evaluate_compliance_availability(
    geoblock_payload: dict[str, Any] | None = None,
    cfg: ComplianceAvailabilityConfig | None = None,
    endpoint_error: str | None = None,
) -> dict[str, Any]:
    """Evaluate whether new Polymarket order placement is permitted.

    The function is local and read-only. It never signs, submits, cancels,
    queries private account state, or gives legal advice. It returns deployment
    blockers whenever proof is missing.
    """

    cfg = cfg or ComplianceAvailabilityConfig()
    payload = _redact_payload(geoblock_payload or {})
    restricted = {
        _norm_country(code)
        for code in cfg.new_order_restricted_country_codes
        if _norm_country(code)
    }
    endpoint_country = _norm_country(payload.get("country"))
    session_country = _norm_country(cfg.session_country_code)
    endpoint_blocked = _as_bool(payload.get("blocked"))

    endpoint_error_present = bool(endpoint_error)
    endpoint_available = endpoint_blocked is not None and not endpoint_error_present
    endpoint_clear = bool(endpoint_available and endpoint_blocked is False)
    if endpoint_error_present and not cfg.block_on_endpoint_error:
        endpoint_clear = True

    endpoint_country_restricted = bool(
        endpoint_country and endpoint_country in restricted
    )
    session_country_restricted = bool(session_country and session_country in restricted)

    gates = {
        "geoblock_endpoint_available": endpoint_available,
        "geoblock_endpoint_clear": endpoint_clear,
        "endpoint_country_not_restricted": not endpoint_country_restricted,
        "session_country_not_restricted": not session_country_restricted,
        "legal_review_cleared": bool(cfg.legal_review_cleared),
        "entity_account_cleared": bool(cfg.entity_account_cleared),
        "api_terms_cleared": bool(cfg.api_terms_cleared),
    }
    gates["new_order_placement_allowed"] = bool(all(gates.values()))

    hard_block = (
        endpoint_blocked is True
        or endpoint_country_restricted
        or session_country_restricted
    )
    status = (
        "new_order_placement_allowed"
        if gates["new_order_placement_allowed"]
        else "new_order_blocked"
        if hard_block
        else "new_order_not_cleared"
    )

    return {
        "config": {
            **asdict(cfg),
            "new_order_restricted_country_codes": sorted(restricted),
        },
        "source": {
            "endpoint_url": cfg.endpoint_url,
            "docs_url": cfg.docs_url,
            "endpoint_error": endpoint_error or "",
        },
        "observed": {
            "endpoint_blocked": endpoint_blocked,
            "endpoint_country_code": endpoint_country,
            "endpoint_region_code": str(payload.get("region", "") or ""),
            "session_country_code": session_country,
            "session_region_code": str(cfg.session_region_code or ""),
            "payload_redacted": payload,
        },
        "gates": gates,
        "blockers": _blockers(gates, endpoint_blocked, endpoint_error_present, cfg),
        "status": status,
        "decision": "block_new_order_placement"
        if not gates["new_order_placement_allowed"]
        else "allow_only_after_recorded_clearance",
        "safety": "read-only compliance availability gate; no login, no private keys, no signing, no order placement; not legal advice",
    }


def _blockers(
    gates: dict[str, bool],
    endpoint_blocked: bool | None,
    endpoint_error_present: bool,
    cfg: ComplianceAvailabilityConfig,
) -> list[str]:
    reasons: list[str] = []
    if endpoint_error_present:
        reasons.append("geoblock endpoint check errored")
    if endpoint_blocked is True:
        reasons.append("geoblock endpoint reports blocked")
    names = {
        "geoblock_endpoint_available": "geoblock endpoint unavailable",
        "geoblock_endpoint_clear": "geoblock endpoint not clear",
        "endpoint_country_not_restricted": "endpoint country is in new-order restricted set",
        "session_country_not_restricted": "session/operator country is in new-order restricted set",
        "legal_review_cleared": "legal review not cleared",
        "entity_account_cleared": "permitted entity/account not cleared",
        "api_terms_cleared": "API terms/access not cleared",
    }
    for gate, message in names.items():
        if not gates.get(gate, False) and message not in reasons:
            reasons.append(message)
    if not cfg.block_on_endpoint_error and endpoint_error_present:
        reasons.append(
            "endpoint error was configured non-blocking; requires manual review"
        )
    return reasons


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "ip" in out:
        out["ip"] = "REDACTED"
    return out


def _norm_country(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None
