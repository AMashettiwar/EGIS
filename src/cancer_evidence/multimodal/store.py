from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

CORE_SCHEMAS: dict[str, pa.Schema] = {
    "patients": pa.schema([
        ("canonical_patient_id", pa.string()),
        ("source", pa.string()),
        ("source_patient_id", pa.string()),
        ("is_control_or_pool", pa.bool_()),
        ("disease_type", pa.string()),
        ("primary_site", pa.string()),
        ("ajcc_stage", pa.string()),
        ("gender", pa.string()),
        ("vital_status", pa.string()),
        ("age_at_diagnosis_days", pa.int64()),
    ]),
    "identity_crosswalk": pa.schema([
        ("canonical_patient_id", pa.string()),
        ("source", pa.string()),
        ("source_patient_id", pa.string()),
        ("mapping_status", pa.string()),
        ("authority", pa.string()),
        ("evidence_note", pa.string()),
    ]),
    "specimens": pa.schema([
        ("specimen_id", pa.string()),
        ("canonical_patient_id", pa.string()),
        ("episode_id", pa.string()),
        ("source", pa.string()),
        ("source_specimen_id", pa.string()),
        ("sample_type", pa.string()),
        ("collection_day_offset", pa.int64()),
        ("report_status", pa.string()),
    ]),
    "modality_runs": pa.schema([
        ("run_id", pa.string()),
        ("modality", pa.string()),
        ("canonical_patient_id", pa.string()),
        ("specimen_id", pa.string()),
        ("source_specimen_id", pa.string()),
        ("episode_id", pa.string()),
        ("source", pa.string()),
        ("source_run_id", pa.string()),
        ("qc_status", pa.string()),
        ("report_status", pa.string()),
        ("sample_type", pa.string()),
        ("collection_day_offset", pa.int64()),
        ("pipeline_version", pa.string()),
        ("artifact_uri", pa.string()),
        ("artifact_sha256", pa.string()),
        ("features_json", pa.string()),
    ]),
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_sha256(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _rows_to_table(rows: list[dict[str, Any]], schema: pa.Schema | None = None) -> pa.Table:
    if schema is not None:
        normalized = []
        for row in rows:
            normalized.append({field.name: row.get(field.name) for field in schema})
        if not normalized:
            return pa.Table.from_pydict({f.name: [] for f in schema}, schema=schema)
        return pa.Table.from_pylist(normalized, schema=schema)
    if not rows:
        return pa.table({"_empty": pa.array([], type=pa.string())})
    return pa.Table.from_pylist(rows)


def write_multimodal_snapshot(
    snapshot_dir: str | Path,
    *,
    snapshot_id: str,
    patients: list[dict[str, Any]],
    identity_crosswalk: list[dict[str, Any]],
    specimens: list[dict[str, Any]],
    modality_runs: list[dict[str, Any]],
    feature_tables: dict[str, list[dict[str, Any]]] | None = None,
    source_provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
    is_frozen: bool = True,
) -> dict[str, Any]:
    """Write a frozen, content-addressed Parquet + manifest multimodal snapshot."""
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)

    core_data = {
        "patients": patients,
        "identity_crosswalk": identity_crosswalk,
        "specimens": specimens,
        "modality_runs": modality_runs,
    }
    table_entries: dict[str, dict[str, Any]] = {}

    for name, rows in core_data.items():
        table = _rows_to_table(rows, CORE_SCHEMAS[name])
        out_path = root / f"{name}.parquet"
        pq.write_table(table, out_path, compression="zstd")
        table_entries[name] = {
            "file": out_path.name,
            "row_count": table.num_rows,
            "sha256": sha256_file(out_path),
            "kind": "core",
        }

    for name, rows in sorted((feature_tables or {}).items()):
        if not rows:
            continue
        table = _rows_to_table(rows)
        out_path = root / f"feature_{name}.parquet"
        pq.write_table(table, out_path, compression="zstd")
        table_entries[f"feature_{name}"] = {
            "file": out_path.name,
            "row_count": table.num_rows,
            "sha256": sha256_file(out_path),
            "kind": "feature",
        }

    modalities = sorted({str(r["modality"]) for r in modality_runs if r.get("modality")})
    sources = sorted({str(r["source"]) for r in modality_runs if r.get("source")})

    manifest_body = {
        "schema_version": "multimodal-1.0.0",
        "snapshot_id": snapshot_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "is_frozen": is_frozen,
        "modalities": modalities,
        "sources": sources,
        "tables": table_entries,
        "source_provenance": source_provenance or {},
    }
    manifest = dict(manifest_body)
    manifest["manifest_sha256"] = canonical_json_sha256(manifest_body)

    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


class SnapshotIntegrityError(RuntimeError):
    """Raised when a multimodal snapshot fails freeze, hash, or row-count checks."""


class MultimodalSnapshot:
    """Verified read-only DuckDB handle over a frozen Parquet multimodal snapshot."""

    def __init__(self, snapshot_dir: str | Path, *, verify: bool = True) -> None:
        self.root = Path(snapshot_dir)
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise SnapshotIntegrityError(f"Missing manifest.json in {self.root}")
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        if verify:
            self.verify()
        self.conn = duckdb.connect(database=":memory:")
        self._register_views()

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def manifest_sha256(self) -> str:
        return str(self.manifest["manifest_sha256"])

    def verify(self) -> None:
        if not self.manifest.get("is_frozen"):
            raise SnapshotIntegrityError("Snapshot is not frozen (is_frozen != true)")

        body = {k: v for k, v in self.manifest.items() if k != "manifest_sha256"}
        expected_hash = canonical_json_sha256(body)
        if self.manifest.get("manifest_sha256") != expected_hash:
            raise SnapshotIntegrityError("Manifest SHA-256 mismatch (manifest tampered)")

        tables = self.manifest.get("tables") or {}
        for required in CORE_SCHEMAS:
            if required not in tables:
                raise SnapshotIntegrityError(f"Missing required core table: {required}")

        for name, meta in tables.items():
            path = self.root / meta["file"]
            if not path.exists():
                raise SnapshotIntegrityError(f"Missing Parquet file for table '{name}': {path.name}")
            actual_hash = sha256_file(path)
            if actual_hash != meta["sha256"]:
                raise SnapshotIntegrityError(f"SHA-256 mismatch for table '{name}'")
            actual_rows = pq.read_metadata(path).num_rows
            if actual_rows != meta["row_count"]:
                raise SnapshotIntegrityError(
                    f"Row-count mismatch for table '{name}': declared={meta['row_count']} actual={actual_rows}"
                )

    def _register_views(self) -> None:
        for name, meta in self.manifest["tables"].items():
            path = (self.root / meta["file"]).resolve().as_posix().replace("'", "''")
            self.conn.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM read_parquet('{path}')")

    def available_tables(self) -> list[str]:
        return sorted(self.manifest["tables"].keys())

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> MultimodalSnapshot:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
