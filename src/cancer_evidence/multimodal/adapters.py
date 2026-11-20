from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AdapterPayload:
    """Normalized rows emitted by one department/modality adapter."""

    modality_name: str
    source_name: str
    patients: list[dict[str, Any]] = field(default_factory=list)
    identity_crosswalk: list[dict[str, Any]] = field(default_factory=list)
    specimens: list[dict[str, Any]] = field(default_factory=list)
    modality_runs: list[dict[str, Any]] = field(default_factory=list)
    feature_tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    source_provenance: dict[str, Any] = field(default_factory=dict)


class ModalityAdapter(Protocol):
    """Pluggable contract for onboarding a new department or assay modality."""

    modality_name: str
    source_name: str

    def ingest(self, **kwargs: Any) -> AdapterPayload: ...


def merge_adapter_payloads(payloads: list[AdapterPayload]) -> dict[str, Any]:
    """Deterministically combine multiple ModalityAdapter outputs into unified tables."""
    patients_by_id: dict[str, dict[str, Any]] = {}
    crosswalk: list[dict[str, Any]] = []
    specimens_by_id: dict[str, dict[str, Any]] = {}
    runs: list[dict[str, Any]] = []
    feature_tables: dict[str, list[dict[str, Any]]] = {}
    provenance: dict[str, Any] = {}

    for payload in payloads:
        provenance[f"{payload.source_name}:{payload.modality_name}"] = payload.source_provenance

        for row in payload.patients:
            pid = str(row["canonical_patient_id"])
            existing = patients_by_id.get(pid)
            if existing is None:
                patients_by_id[pid] = dict(row)
            else:
                merged = dict(existing)
                for key, value in row.items():
                    if merged.get(key) is None and value is not None:
                        merged[key] = value
                if row.get("is_control_or_pool"):
                    merged["is_control_or_pool"] = True
                patients_by_id[pid] = merged

        crosswalk.extend(payload.identity_crosswalk)

        for spec in payload.specimens:
            sid = str(spec["specimen_id"])
            if sid not in specimens_by_id:
                specimens_by_id[sid] = dict(spec)

        runs.extend(payload.modality_runs)

        for table_name, table_rows in payload.feature_tables.items():
            feature_tables.setdefault(table_name, []).extend(table_rows)

    return {
        "patients": sorted(patients_by_id.values(), key=lambda r: str(r["canonical_patient_id"])),
        "identity_crosswalk": sorted(
            crosswalk,
            key=lambda r: (
                str(r["canonical_patient_id"]),
                str(r["source"]),
                str(r["source_patient_id"]),
                str(r["mapping_status"]),
            ),
        ),
        "specimens": sorted(
            specimens_by_id.values(),
            key=lambda r: (str(r["canonical_patient_id"]), str(r["specimen_id"])),
        ),
        "modality_runs": sorted(
            runs,
            key=lambda r: (
                str(r["canonical_patient_id"]),
                str(r["modality"]),
                str(r["run_id"]),
            ),
        ),
        "feature_tables": feature_tables,
        "source_provenance": provenance,
    }
