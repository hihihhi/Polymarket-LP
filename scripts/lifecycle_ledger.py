#!/usr/bin/env python3
"""Append, verify, or export a local LP lifecycle JSONL ledger.

Read/write local files only. This script never signs, submits, cancels, or
places orders and must not be given secrets.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.lifecycle_ledger import (  # noqa: E402
    LifecycleLedgerConfig,
    append_lifecycle_event,
    export_lifecycle_csv,
    lifecycle_event_schema,
    verify_lifecycle_ledger,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("--out", default="")
    append = sub.add_parser("append")
    append.add_argument("--ledger", required=True)
    append.add_argument("--event-json", required=True, help="JSON string or path to JSON object")
    verify = sub.add_parser("verify")
    verify.add_argument("--ledger", required=True)
    verify.add_argument("--out", default="")
    export = sub.add_parser("export-csv")
    export.add_argument("--ledger", required=True)
    export.add_argument("--csv", required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.cmd == "schema":
        payload = lifecycle_event_schema()
        text = json.dumps(payload, indent=2)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
        print(text)
        return
    if args.cmd == "append":
        src = Path(args.event_json)
        event = json.loads(src.read_text(encoding="utf-8-sig") if src.exists() else args.event_json)
        print(json.dumps(append_lifecycle_event(args.ledger, event), indent=2, default=str))
        return
    if args.cmd == "verify":
        payload = verify_lifecycle_ledger(args.ledger, LifecycleLedgerConfig())
        text = json.dumps(payload, indent=2, default=str)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
        print(text)
        return
    if args.cmd == "export-csv":
        print(export_lifecycle_csv(args.ledger, args.csv))


if __name__ == "__main__":
    main()
