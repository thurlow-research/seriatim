#!/usr/bin/env python3
"""Contract lint (SPEC REQ-63; TD-slice-1 K-OAS1).

Until slice 1, there is no contract/ directory, and the check passes with a visible notice.
Once anything exists under contract/, contract/openapi.yaml must exist, declare
`openapi: 3.1.x`, pass openapi-spec-validator's OpenAPI 3.1 validator (never one the document
picks for itself, so a 3.0 contract fails), and every $ref in it must resolve. That is checked
separately, because the validator accepts an unresolved one. Only local refs (#/...) are
allowed. Slice 1 adds the allowlist-vs-route-list test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CONTRACT_DIR = Path("contract")
CONTRACT = CONTRACT_DIR / "openapi.yaml"
OPENAPI_31_RE = re.compile(r"^3\.1\.[0-9]+$")


def unresolved_refs(doc: object) -> list[str]:
    """Return every $ref in doc that is not a resolvable local JSON pointer."""
    bad: list[str] = []

    def resolve(ref: str) -> bool:
        if not ref.startswith("#/"):
            return False
        node: object = doc
        for raw in ref[2:].split("/"):
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return False
        return True

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not resolve(ref):
                bad.append(ref)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return bad


def main() -> int:
    if not CONTRACT_DIR.exists():
        print("::notice::no contract/ directory yet (slice 1 adds contract/openapi.yaml)")
        return 0
    if not CONTRACT.is_file():
        print(f"::error::contract/ exists but {CONTRACT} does not")
        return 1
    import yaml
    from openapi_spec_validator import OpenAPIV31SpecValidator, validate

    try:
        doc = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        version = doc.get("openapi") if isinstance(doc, dict) else None
        if not isinstance(version, str) or not OPENAPI_31_RE.fullmatch(version):
            print(f"::error file={CONTRACT}::openapi must be 3.1.x (TD-slice-1), got {version!r}")
            return 1
        validate(doc, cls=OpenAPIV31SpecValidator)
    except Exception as e:  # any parse or validation failure fails the check
        first = (str(e).splitlines() or [""])[0][:300]
        print(f"::error file={CONTRACT}::{type(e).__name__}: {first}")
        return 1
    bad = unresolved_refs(doc)
    for ref in bad:
        print(f"::error file={CONTRACT}::unresolved $ref {ref}")
    if bad:
        return 1
    print(f"{CONTRACT}: valid OpenAPI, every $ref resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
