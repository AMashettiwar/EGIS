from __future__ import annotations

import hashlib
import json
from typing import Any

from .engine import evaluate
from .replay import RULE_VERSION, certificate_digest, fixture_hash


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_certificate(fixture: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    certificate = {"certificate_version": "1", "rule_version": RULE_VERSION, "fixture_sha256": fixture_hash(fixture), "query": query, "query_sha256": hashlib.sha256(_canonical(query)).hexdigest(), "result": evaluate(fixture, query)}
    certificate["certificate_sha256"] = certificate_digest(certificate)
    return certificate

