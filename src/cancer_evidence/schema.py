from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"

SCHEMAS: dict[str, dict[str, Any]] = {
    "query": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Structured evidence query",
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"enum": ["count_entities", "assay_availability", "assay_overlap", "file_lineage", "compare_inventories"]},
            "entity_level": {"enum": ["case", "sample", "aliquot", "assay", "file"]},
            "denominator_mode": {"enum": ["left", "eligible", "universe"]},
            "cohort_filter": {"type": "object", "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "study_id": {"type": "string"}, "eligibility_field": {"type": "string"}, "eligibility_value": {}}, "additionalProperties": False},
        },
        "additionalProperties": True,
    },
    "certificate": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Proof-carrying answer certificate",
        "type": "object",
        "required": ["certificate_version", "engine", "rule_version", "query", "query_sha256", "snapshot_id", "snapshot_manifest_sha256", "snapshot_sha256", "derivation", "evidence_scope", "result", "certificate_sha256", "signature", "signature_algorithm", "verification_public_key"],
        "properties": {
            "evidence_scope": {"type": "object"},
            "certificate_version": {"type": "string"},
            "engine": {"const": "cancer-evidence-engine"},
            "rule_version": {"type": "string"},
            "snapshot_id": {"type": "string"},
            "snapshot_manifest_sha256": {"type": "string"},
            "snapshot_sha256": {"type": "string"},
            "derivation": {"type": "object"},
            "query": {"type": "object"},
            "query_sha256": {"type": "string"},
            "result": {"type": "object"},
            "certificate_sha256": {"type": "string"},
            "signature": {"type": "string"},
            "signature_algorithm": {"const": "Ed25519"},
            "verification_public_key": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "normalized": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Normalized evidence bundle",
        "type": "object",
        "required": ["entities", "relationships", "evidence"],
        "properties": {"entities": {"type": "array"}, "relationships": {"type": "array"}, "evidence": {"type": "array"}},
    },
    "result": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Answer result",
        "type": "object",
        "required": ["status", "reason_code", "result", "witness", "evidence_refs"],
        "properties": {"status": {"type": "string"}, "reason_code": {"type": "string"}, "result": {"type": "object"}, "witness": {"type": "array"}, "evidence_refs": {"type": "array"}},
    },
    "snapshot": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Frozen snapshot manifest",
        "type": "object",
        "required": ["snapshot_id", "source", "schema_version", "adapter_version", "raw_content_sha256", "normalized_content_sha256", "derivation", "sqlite_sha256", "complete", "publication"],
        "properties": {"snapshot_id": {"type": "string"}, "source": {"type": "string"}, "schema_version": {"type": "string"}, "adapter_version": {"type": "string"}, "raw_content_sha256": {"type": "string"}, "normalized_content_sha256": {"type": "string"}, "derivation": {"type": "object"}, "sqlite_sha256": {"type": "string"}, "complete": {"type": "boolean"}, "publication": {"enum": ["READY", "INCOMPLETE"]}},
    },
}


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, schema in SCHEMAS.items():
        (directory / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def validate_query(query: dict[str, Any]) -> None:
    if not isinstance(query, dict) or not isinstance(query.get("operation"), str):
        raise ValueError("query must be an object with a string operation")

