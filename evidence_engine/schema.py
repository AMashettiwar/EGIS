from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"

SCHEMAS: dict[str, dict[str, Any]] = {
    "query": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["operation"],
        "properties": {"operation": {"type": "string"}},
        "additionalProperties": True,
    },
    "certificate": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["certificate_version", "snapshot_id", "query", "result", "certificate_sha256"],
        "properties": {
            "certificate_version": {"type": "string"},
            "snapshot_id": {"type": "string"},
            "query": {"type": "object"},
            "result": {"type": "object"},
            "certificate_sha256": {"type": "string"},
        },
        "additionalProperties": True,
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

