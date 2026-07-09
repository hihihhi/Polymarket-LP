#!/usr/bin/env python3
"""Evaluate the Polymarket new-order compliance/API availability gate.

This script is read-only. It can fetch the public geoblock endpoint or read a
saved endpoint JSON. It never logs in, signs, submits, cancels, or places
orders, and it redacts direct IP evidence from outputs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_lp.compliance import (  # noqa: E402
    ComplianceAvailabilityConfig,
    DEFAULT_DOCS_URL,
    DEFAULT_GEOBLOCK_URL,
    DEFAULT_NEW_ORDER_RESTRICTED_COUNTRY_CODES,
    evaluate_compliance_availability,
    fetch_geoblock_status,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--geoblock-json", default="", help="Optional saved geoblock endpoint JSON")
    p.add_argument("--fetch-geoblock", action="store_true", help="Fetch the public geoblock endpoint")
    p.add_argument("--endpoint-url", default=DEFAULT_GEOBLOCK_URL)
    p.add_argument("--docs-url", default=DEFAULT_DOCS_URL)
    p.add_argument("--session-country-code", default="")
    p.add_argument("--session-region-code", default="")
    p.add_argument(
        "--restricted-country-codes",
        default=",".join(DEFAULT_NEW_ORDER_RESTRICTED_COUNTRY_CODES),
        help="Comma-separated ISO country codes that cannot open new positions",
    )
    p.add_argument("--legal-review-cleared", action="store_true")
    p.add_argument("--entity-account-cleared", action="store_true")
    p.add_argument("--api-terms-cleared", action="store_true")
    p.add_argument("--allow-endpoint-error", action="store_true")
    p.add_argument("--timeout-seconds", type=float, default=10.0)
    p.add_argument("--out", default="", help="Optional JSON output path")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    payload: dict | None = None
    endpoint_error = ""
    if args.geoblock_json:
        payload = json.loads(Path(args.geoblock_json).read_text(encoding="utf-8-sig"))
    elif args.fetch_geoblock:
        try:
            payload = fetch_geoblock_status(args.endpoint_url, timeout_seconds=args.timeout_seconds)
        except Exception as exc:  # pragma: no cover - network failures are environment-dependent
            endpoint_error = f"{type(exc).__name__}: {exc}"
            payload = {}
    restricted = tuple(code.strip().upper() for code in args.restricted_country_codes.split(",") if code.strip())
    result = evaluate_compliance_availability(
        payload,
        ComplianceAvailabilityConfig(
            session_country_code=args.session_country_code,
            session_region_code=args.session_region_code,
            endpoint_url=args.endpoint_url,
            docs_url=args.docs_url,
            new_order_restricted_country_codes=restricted,
            legal_review_cleared=args.legal_review_cleared,
            entity_account_cleared=args.entity_account_cleared,
            api_terms_cleared=args.api_terms_cleared,
            block_on_endpoint_error=not args.allow_endpoint_error,
        ),
        endpoint_error=endpoint_error,
    )
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
