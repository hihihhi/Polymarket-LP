#!/usr/bin/env python3
"""Build a read-only leaderboard across LP public-paper candidates."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.candidate_leaderboard import (  # noqa: E402
    CandidateEvidence,
    build_candidate_leaderboard,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--candidate", action="append", required=True, help="NAME=depth_gate.json"
    )
    p.add_argument("--metadata", action="append", default=[], help="NAME=metadata.json")
    p.add_argument("--out", required=True)
    p.add_argument("--markdown-out", required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    metadata = _load_named_paths(args.metadata)
    candidates = []
    for item in args.candidate:
        name, path = _split_named_path(item, "--candidate")
        gate = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        candidates.append(
            CandidateEvidence(name=name, gate=gate, metadata=metadata.get(name, {}))
        )
    result = build_candidate_leaderboard(candidates)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(_json_safe(result), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    md = Path(args.markdown_out)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "leader": result.get("leader", {}).get("name"),
            },
            indent=2,
        )
    )


def _load_named_paths(items: list[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        name, path = _split_named_path(item, "--metadata")
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        output[name] = value if isinstance(value, dict) else {"value": value}
    return output


def _split_named_path(item: str, flag: str) -> tuple[str, str]:
    if "=" not in item:
        raise SystemExit(f"{flag} must be NAME=PATH")
    name, path = item.split("=", 1)
    if not name or not path:
        raise SystemExit(f"{flag} must be NAME=PATH")
    return name, path


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# LP candidate leaderboard",
        "",
        "Safety: read-only public-paper comparison; no keys, signing, orders, cancels, or paid-reward verification.",
        "",
        f"Status: `{result['status']}`",
        "",
        "| Rank | Candidate | Status | p05/mo @ capture | Hours | Rows | Markets | Rescue feasible | Residual loss | Note |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for idx, row in enumerate(result.get("candidates", []), start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    str(row.get("name", "")),
                    str(row.get("status", "")),
                    _money(row.get("income_p05_at_required_capture")),
                    _num(row.get("duration_hours"), 2),
                    str(row.get("quote_rows", 0)),
                    str(row.get("unique_markets_quoted", 0)),
                    _pct(row.get("taker_rescue_feasible_rate")),
                    _money(row.get("latest_taker_residual_loss_to_zero")),
                    str(row.get("ranking_note", "")),
                ]
            )
            + " |"
        )
    leader = result.get("leader") or {}
    blockers = leader.get("blockers") or []
    if blockers:
        lines.extend(["", f"## Leader blockers: {leader.get('name', '')}", ""])
        lines.extend(f"- {x}" for x in blockers)
    lines.append("")
    return "\n".join(lines)


def _money(value: object) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"${x:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: object) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"{100 * x:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: object, digits: int) -> str:
    try:
        x = float(value)
        return "n/a" if not math.isfinite(x) else f"{x:.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


if __name__ == "__main__":
    main()
