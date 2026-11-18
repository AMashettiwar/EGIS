from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
ADAPTER_VERSION = "synthetic-1.0.0"
DERIVATION_VERSION = "raw-to-normalized-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def derive_normalized(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    """Derive the canonical snapshot payload from a raw capture."""
    return {
        "source": raw_bundle.get("source"),
        "coverage": raw_bundle.get("coverage", {}),
        "entities": raw_bundle.get("entities", []),
        "relationships": raw_bundle.get("relationships", []),
        "evidence": raw_bundle.get("evidence", []),
    }


def _init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('case','sample','aliquot','assay','file')),
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            attributes_json TEXT NOT NULL
        );
        CREATE TABLE relationships (
            subject_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY(subject_id, predicate, object_id, source_record_id),
            FOREIGN KEY(subject_id) REFERENCES entities(entity_id),
            FOREIGN KEY(object_id) REFERENCES entities(entity_id)
        );
        CREATE TABLE evidence (
            evidence_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            raw_value_json TEXT,
            canonical_value_json TEXT,
            source_record_id TEXT NOT NULL,
            FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
        );
        """
    )
    return connection


def write_snapshot(
    raw_bundle: dict[str, Any],
    output_dir: Path,
    *,
    source: str = "synthetic",
    request: dict[str, Any] | None = None,
    retrieved_at: str | None = None,
    complete: bool = True,
    adapter_version: str = ADAPTER_VERSION,
) -> dict[str, Any]:
    """Write a complete snapshot atomically and return its manifest."""
    request = request or {"kind": "fixture"}
    retrieved_at = retrieved_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    required = {"entities", "relationships", "evidence", "coverage"}
    if complete and not required.issubset(raw_bundle):
        raise ValueError(f"complete snapshot is missing required sections: {sorted(required - set(raw_bundle))}")
    coverage = raw_bundle.get("coverage", {})
    actual_case_counts: dict[str, int] = {}
    for entity in raw_bundle.get("entities", []):
        if entity.get("kind") == "case":
            actual_case_counts[entity.get("source", "")] = actual_case_counts.get(entity.get("source", ""), 0) + 1
    coverage_complete = bool(coverage) and all(
        isinstance(value, dict)
        and value.get("complete") is True
        and (value.get("expected_total") is None or value.get("expected_total") == actual_case_counts.get(source, 0))
        and (value.get("retrieved_count") is None or value.get("retrieved_count") == actual_case_counts.get(source, 0))
        for source, value in coverage.items()
    )
    effective_complete = complete and coverage_complete
    if not effective_complete:
        publication = "INCOMPLETE"
    else:
        publication = "READY"
    raw_payload = (json.dumps(raw_bundle, indent=2, sort_keys=True) + "\n").encode("utf-8")
    raw_hash = sha256_bytes(raw_payload)
    normalized = derive_normalized(raw_bundle)
    normalized_payload = (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode("utf-8")
    normalized_hash = sha256_bytes(normalized_payload)
    snapshot_id = f"{source}-{raw_hash[:16]}"
    manifest = {
        "snapshot_id": snapshot_id,
        "source": source,
        "fixture_id": raw_bundle.get("fixture_id"),
        "cohort_id": raw_bundle.get("cohort_id"),
        "request": request,
        "retrieved_at_utc": retrieved_at,
        "schema_version": SCHEMA_VERSION,
        "adapter_version": adapter_version,
        "raw_content_sha256": raw_hash,
        "normalized_content_sha256": normalized_hash,
        "derivation": {
            "version": DERIVATION_VERSION,
            "input": "raw.json",
            "input_sha256": raw_hash,
            "output": "normalized.json",
            "output_sha256": normalized_hash,
        },
        "coverage": raw_bundle.get("coverage", {}),
        "complete": effective_complete,
        "publication": publication,
    }

    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    connection: sqlite3.Connection | None = None
    try:
        (temp / "raw.json").write_bytes(raw_payload)
        (temp / "normalized.json").write_bytes(normalized_payload)
        connection = _init_db(temp / "data.sqlite")
        entity_keys: set[tuple[str, str, str]] = set()
        for entity in normalized["entities"]:
            expected_id = f"{entity['source']}:{entity['kind']}:{entity['source_id']}"
            if entity["entity_id"] != expected_id or (entity["source"], entity["kind"], entity["source_id"]) in entity_keys:
                raise ValueError("entity identity is not a unique source/kind/source_id namespace")
            entity_keys.add((entity["source"], entity["kind"], entity["source_id"]))
            connection.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?)",
                (entity["entity_id"], entity["kind"], entity["source"], entity["source_id"], canonical_json(entity.get("attributes", {}))),
            )
        entity_sources = {row["entity_id"]: row["source"] for row in normalized["entities"]}
        entity_kinds = {row["entity_id"]: row["kind"] for row in normalized["entities"]}
        allowed_predicates = {
            "HAS_SAMPLE": {("case", "sample")},
            "HAS_ALIQUOT": {("case", "aliquot"), ("sample", "aliquot")},
            "HAS_ASSAY": {("case", "assay"), ("sample", "assay"), ("aliquot", "assay")},
            "HAS_FILE": {("assay", "file")},
        }
        for rel in normalized["relationships"]:
            subject_source = entity_sources.get(rel["subject_id"])
            object_source = entity_sources.get(rel["object_id"])
            pair = (entity_kinds.get(rel["subject_id"]), entity_kinds.get(rel["object_id"]))
            if subject_source is None or object_source is None or subject_source != object_source or rel["source"] != subject_source or pair not in allowed_predicates.get(rel["predicate"], set()):
                raise ValueError("cross-source, unknown, or invalid hierarchy relationship rejected")
            connection.execute(
                "INSERT INTO relationships VALUES (?,?,?,?,?)",
                (rel["subject_id"], rel["predicate"], rel["object_id"], rel["source_record_id"], rel["source"]),
            )
        for evidence in normalized["evidence"]:
            connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
                (evidence["evidence_id"], evidence["entity_id"], evidence["field_name"], canonical_json(evidence.get("raw_value")), canonical_json(evidence.get("canonical_value")), evidence["source_record_id"]),
            )
        connection.commit()
        connection.close()
        manifest["sqlite_sha256"] = sha256_bytes((temp / "data.sqlite").read_bytes())
        manifest["manifest_sha256"] = sha256_json(manifest)
        (temp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temp / "READY").write_text(publication + "\n", encoding="utf-8")
        if output_dir.exists():
            raise FileExistsError(f"refusing to replace existing snapshot: {output_dir}")
        os.replace(temp, output_dir)
        return manifest
    except Exception:
        if connection is not None:
            connection.close()
        shutil.rmtree(temp, ignore_errors=True)
        raise


def read_snapshot(snapshot_dir: Path) -> tuple[dict[str, Any], sqlite3.Connection]:
    snapshot_dir = Path(snapshot_dir)
    manifest_path = snapshot_dir / "manifest.json"
    marker_path = snapshot_dir / "READY"
    if not manifest_path.exists() or not marker_path.exists() or not (snapshot_dir / "data.sqlite").exists():
        raise ValueError("snapshot is missing manifest, READY marker, or database")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    marker = marker_path.read_text(encoding="utf-8").strip()
    if manifest.get("publication") not in {"READY", "INCOMPLETE"} or marker != manifest.get("publication"):
        raise ValueError("snapshot publication marker mismatch")
    manifest_for_hash = dict(manifest)
    stored_manifest_hash = manifest_for_hash.pop("manifest_sha256", None)
    if stored_manifest_hash != sha256_json(manifest_for_hash):
        raise ValueError("snapshot manifest hash mismatch")
    raw_path = snapshot_dir / "raw.json"
    if not raw_path.exists() or manifest.get("raw_content_sha256") != sha256_bytes(raw_path.read_bytes()):
        raise ValueError("snapshot raw evidence hash mismatch")
    normalized_path = snapshot_dir / "normalized.json"
    if not normalized_path.exists():
        raise ValueError("snapshot normalized derivation missing")
    raw_bundle = json.loads(raw_path.read_text(encoding="utf-8"))
    expected_normalized = derive_normalized(raw_bundle)
    actual_normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    if actual_normalized != expected_normalized:
        raise ValueError("snapshot derivation mismatch")
    normalized_hash = sha256_bytes(normalized_path.read_bytes())
    if manifest.get("normalized_content_sha256") != normalized_hash or manifest.get("derivation", {}).get("output_sha256") != normalized_hash:
        raise ValueError("snapshot normalized hash mismatch")
    if manifest.get("sqlite_sha256") != sha256_bytes((snapshot_dir / "data.sqlite").read_bytes()):
        raise ValueError("snapshot database hash mismatch")
    return manifest, sqlite3.connect(snapshot_dir / "data.sqlite")

