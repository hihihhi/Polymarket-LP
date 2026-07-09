from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .lifecycle import CANONICAL_LIFECYCLE_STATES


SCHEMA_VERSION = "lp_lifecycle_ledger_v1"
REQUIRED_EVENT_FIELDS = ("timestamp", "client_order_id", "lifecycle_state")
FORBIDDEN_KEY_FRAGMENTS = (
    "private_key",
    "secret",
    "mnemonic",
    "seed_phrase",
    "password",
    "passphrase",
    "api_key",
)


@dataclass(slots=True)
class LifecycleLedgerConfig:
    """Tamper-evident local lifecycle ledger config.

    This module is intentionally local-file only. It never signs orders, submits
    orders, cancels orders, fetches account data, or reads credentials. The
    ledger is a collection target for a separate permitted signed-paper/live
    runner.
    """

    require_hash_chain: bool = True
    reject_secret_like_keys: bool = True
    max_timestamp_lag_seconds: float | None = None


def lifecycle_event_schema() -> list[dict[str, str]]:
    rows = [
        ("timestamp", "yes", "all", "UTC timestamp for the lifecycle event."),
        ("client_order_id", "yes", "all", "Client order id or redacted order id reference."),
        ("lifecycle_state", "yes", "all", "Canonical state from CANONICAL_LIFECYCLE_STATES."),
        ("market_id", "yes", "book_snapshot..final", "Market identifier."),
        ("condition_id", "yes", "book_snapshot..final", "Condition identifier."),
        ("side", "yes", "quote/fill/rescue", "YES/NO or buy/sell direction."),
        ("book_state_before", "yes", "book_snapshot", "Redacted serialized book/depth snapshot."),
        ("rank_score", "yes", "ranking_decision", "Candidate/ranking score."),
        ("quote_price", "yes", "quote_intent", "Limit quote price."),
        ("quote_size", "yes", "quote_intent", "Quote size/shares."),
        ("post_only", "yes", "quote_intent", "Post-only/maker intent."),
        ("risk_gate_status", "yes", "risk_gate", "Capital, concentration, stale-data and kill-switch state."),
        ("signed_order_hash", "yes", "sign", "Safe hash/reference to signed order, not private key material."),
        ("submit_ts", "yes", "submit", "Order submission timestamp."),
        ("ack_reject_ts", "yes", "ack/reject", "Exchange ack/reject timestamp."),
        ("reject_reason", "conditional", "reject", "Reject reason if rejected."),
        ("queue_depth_ahead", "yes", "queue_estimate", "Estimated displayed queue/depth ahead."),
        ("resting_seconds", "yes", "resting", "Resting eligible seconds."),
        ("fill_status", "yes", "no_fill/partial_fill/full_fill", "Fill state."),
        ("fill_price", "conditional", "partial_fill/full_fill", "Fill price."),
        ("fill_size", "conditional", "partial_fill/full_fill", "Fill size."),
        ("cancel_request_ts", "yes", "cancel_request", "Cancel request timestamp."),
        ("cancel_confirm_ts", "yes", "cancel_confirm", "Cancel confirmation timestamp."),
        ("inventory_state", "conditional", "inventory_update", "Inventory after fills/rescue."),
        ("rescue_action", "conditional", "maker_rescue/taker_rescue/forced_cut", "Autonomous rescue/cut action."),
        ("slippage_usdc", "yes", "fee_slippage", "Slippage cost."),
        ("fees_usdc", "yes", "fee_slippage", "Fees paid."),
        ("estimated_reward_usdc", "yes", "reward_estimate", "Estimated reward linked to eligible resting."),
        ("paid_reward_usdc", "yes", "reward_paid", "Actual paid reward from reward ledger."),
        ("final_pnl_usdc", "yes", "final_pnl_attribution", "Final realized/MTM PnL attribution."),
        ("evidence_source", "yes", "all", "signed-paper runner, exchange export, or reward ledger."),
    ]
    return [
        {"column": c, "required": req, "state_scope": scope, "description": desc}
        for c, req, scope, desc in rows
    ]


def append_lifecycle_event(
    path: str | Path,
    event: dict[str, Any],
    cfg: LifecycleLedgerConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or LifecycleLedgerConfig()
    record = _normalise_event(event, cfg)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    previous = _last_record(p)
    record["sequence"] = int(previous.get("sequence", -1)) + 1 if previous else 0
    record["previous_event_hash"] = previous.get("event_hash", "") if previous else ""
    record["recorded_utc"] = datetime.now(timezone.utc).isoformat()
    record["schema_version"] = SCHEMA_VERSION
    record["event_hash"] = _record_hash(record)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=_json_default) + "\n")
    return record


def load_lifecycle_jsonl(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return pd.DataFrame(rows)


def export_lifecycle_csv(jsonl_path: str | Path, csv_path: str | Path) -> Path:
    df = load_lifecycle_jsonl(jsonl_path)
    out = Path(csv_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return out


def verify_lifecycle_ledger(
    path: str | Path,
    cfg: LifecycleLedgerConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or LifecycleLedgerConfig()
    p = Path(path)
    rows: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    if p.exists() and p.stat().st_size > 0:
        with p.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"line {lineno}: {exc}")
    df = pd.DataFrame(rows)
    schema_errors = _schema_errors(rows, cfg)
    hash_errors = _hash_chain_errors(rows) if cfg.require_hash_chain else []
    timestamp_errors = _timestamp_errors(df, cfg)
    gates = {
        "events_present": len(rows) > 0,
        "json_parse_passed": not parse_errors,
        "schema_passed": not schema_errors,
        "hash_chain_passed": not hash_errors,
        "timestamp_gate_passed": not timestamp_errors,
    }
    gates["ledger_integrity_passed"] = bool(all(gates.values()))
    return {
        "config": asdict(cfg),
        "metrics": {
            "event_rows": int(len(rows)),
            "orders": int(df["client_order_id"].nunique()) if "client_order_id" in df else 0,
            "states": int(df["lifecycle_state"].nunique()) if "lifecycle_state" in df else 0,
        },
        "gates": gates,
        "errors": {
            "parse_errors": parse_errors[:20],
            "schema_errors": schema_errors[:20],
            "hash_errors": hash_errors[:20],
            "timestamp_errors": timestamp_errors[:20],
        },
        "status": "lifecycle_ledger_integrity_passed" if gates["ledger_integrity_passed"] else "lifecycle_ledger_incomplete",
        "safety": "local append-only proof ledger; no private keys, signing, order submission, or cancellation",
    }


def _normalise_event(event: dict[str, Any], cfg: LifecycleLedgerConfig) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise TypeError("event must be a dict")
    if cfg.reject_secret_like_keys:
        _reject_secret_like_keys(event)
    missing = [field for field in REQUIRED_EVENT_FIELDS if field not in event or event[field] in (None, "")]
    if missing:
        raise ValueError(f"missing required lifecycle event fields: {missing}")
    state = str(event["lifecycle_state"]).strip().lower()
    if state not in CANONICAL_LIFECYCLE_STATES:
        raise ValueError(f"unknown lifecycle_state: {state}")
    timestamp = pd.Timestamp(event["timestamp"])
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    out = dict(event)
    out["timestamp"] = timestamp.isoformat()
    out["client_order_id"] = str(event["client_order_id"])
    out["lifecycle_state"] = state
    return _json_safe(out)


def _reject_secret_like_keys(value: Any, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lower = str(key).lower()
            if any(fragment in lower for fragment in FORBIDDEN_KEY_FRAGMENTS):
                raise ValueError(f"secret-like key is not allowed in lifecycle ledger: {prefix}{key}")
            _reject_secret_like_keys(child, f"{prefix}{key}.")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _reject_secret_like_keys(child, f"{prefix}{i}.")


def _last_record(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    last = ""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last) if last else {}


def _record_hash(record: dict[str, Any]) -> str:
    payload = {k: v for k, v in record.items() if k != "event_hash"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_chain_errors(rows: Iterable[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous_hash = ""
    previous_sequence = -1
    for idx, row in enumerate(rows):
        expected = _record_hash(row)
        if row.get("event_hash") != expected:
            errors.append(f"row {idx}: event_hash mismatch")
        if row.get("previous_event_hash", "") != previous_hash:
            errors.append(f"row {idx}: previous_event_hash mismatch")
        sequence = int(row.get("sequence", -999999))
        if sequence != previous_sequence + 1:
            errors.append(f"row {idx}: sequence mismatch")
        previous_hash = str(row.get("event_hash", ""))
        previous_sequence = sequence
    return errors


def _schema_errors(rows: Iterable[dict[str, Any]], cfg: LifecycleLedgerConfig) -> list[str]:
    errors: list[str] = []
    for idx, row in enumerate(rows):
        for field in REQUIRED_EVENT_FIELDS:
            if field not in row or row[field] in (None, ""):
                errors.append(f"row {idx}: missing {field}")
        state = str(row.get("lifecycle_state", "")).strip().lower()
        if state and state not in CANONICAL_LIFECYCLE_STATES:
            errors.append(f"row {idx}: unknown lifecycle_state {state}")
        if cfg.reject_secret_like_keys:
            try:
                _reject_secret_like_keys(row)
            except ValueError as exc:
                errors.append(f"row {idx}: {exc}")
    return errors


def _timestamp_errors(df: pd.DataFrame, cfg: LifecycleLedgerConfig) -> list[str]:
    if df.empty or "timestamp" not in df:
        return []
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    errors = [f"row {i}: invalid timestamp" for i, ok in enumerate(ts.notna()) if not ok]
    if cfg.max_timestamp_lag_seconds is not None:
        now = pd.Timestamp.now(tz="UTC")
        lag = (now - ts).dt.total_seconds()
        bad = lag > cfg.max_timestamp_lag_seconds
        errors.extend(f"row {i}: timestamp lag {lag.iloc[i]:.1f}s exceeds {cfg.max_timestamp_lag_seconds}s" for i in list(bad[bad].index)[:20])
    return errors


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return str(value)
