from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PacketStatus(str, Enum):
    READY = "READY"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"


class GateReason(str, Enum):
    NONE = "NONE"
    CONTROL_OR_NON_PATIENT = "CONTROL_OR_NON_PATIENT"
    DISEASE_FILTER_EXCLUDED = "DISEASE_FILTER_EXCLUDED"
    UNLINKED_PATIENT = "UNLINKED_PATIENT"
    UNAPPROVED_MAPPING = "UNAPPROVED_MAPPING"
    CONFLICTING_MAPPING = "CONFLICTING_MAPPING"
    MISSING_MODALITY = "MISSING_MODALITY"
    SUPERSEDED_ONLY = "SUPERSEDED_ONLY"
    QC_FAILED = "QC_FAILED"
    CONFLICTING_ACTIVE_REPORTS = "CONFLICTING_ACTIVE_REPORTS"
    SAMPLE_TYPE_MISMATCH = "SAMPLE_TYPE_MISMATCH"
    TEMPORAL_MISALIGNMENT = "TEMPORAL_MISALIGNMENT"
    INCOMPLETE_SNAPSHOT = "INCOMPLETE_SNAPSHOT"


class MappingStatus(str, Enum):
    APPROVED = "APPROVED"
    DRAFT = "DRAFT"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    REVOKED = "REVOKED"


class ReportStatus(str, Enum):
    ACTIVE_FINAL = "ACTIVE_FINAL"
    SUPERSEDED = "SUPERSEDED"
    DRAFT = "DRAFT"


class QCStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AlignmentPolicy:
    """Explicit rules for aligning modalities to the same clinical episode/specimen."""

    required_sample_type: str = "Primary Tumor"
    max_day_delta: int | None = 30
    require_same_episode: bool = True
    allow_missing_timestamps: bool = False
    allow_shared_specimen_fallback: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CohortRequest:
    """Locked, deterministic request for cohort gating and patient packet assembly."""

    cohort_id: str
    required_modalities: tuple[str, ...]
    alignment_policy: AlignmentPolicy = field(default_factory=AlignmentPolicy)
    disease_types: tuple[str, ...] | None = None
    require_approved_mappings: bool = True
    include_feature_tables: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "required_modalities": list(self.required_modalities),
            "alignment_policy": self.alignment_policy.to_dict(),
            "disease_types": list(self.disease_types) if self.disease_types is not None else None,
            "require_approved_mappings": self.require_approved_mappings,
            "include_feature_tables": list(self.include_feature_tables),
        }


@dataclass(frozen=True)
class ModalityObservation:
    modality: str
    source: str
    run_id: str
    source_run_id: str
    specimen_id: str
    source_specimen_id: str
    episode_id: str | None
    sample_type: str
    collection_day_offset: int | None
    qc_status: str
    report_status: str
    pipeline_version: str
    artifact_uri: str | None
    artifact_sha256: str | None
    features: dict[str, Any]


@dataclass(frozen=True)
class PatientPacket:
    """Verified, deterministic per-patient multimodal feature packet (or explicit block)."""

    canonical_patient_id: str
    status: PacketStatus
    reason_codes: tuple[GateReason, ...]
    disease_type: str | None
    primary_site: str | None
    ajcc_stage: str | None
    episode_id: str | None
    alignment_mode: str
    max_observed_day_delta: int | None
    modalities: dict[str, ModalityObservation]
    witness_ids: tuple[str, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_patient_id": self.canonical_patient_id,
            "status": self.status.value,
            "reason_codes": [r.value for r in self.reason_codes],
            "disease_type": self.disease_type,
            "primary_site": self.primary_site,
            "ajcc_stage": self.ajcc_stage,
            "episode_id": self.episode_id,
            "alignment_mode": self.alignment_mode,
            "max_observed_day_delta": self.max_observed_day_delta,
            "modalities": {k: asdict(v) for k, v in sorted(self.modalities.items())},
            "witness_ids": list(self.witness_ids),
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class FunnelStage:
    stage: str
    remaining: int
    dropped: int
    reason: str


@dataclass(frozen=True)
class CohortResult:
    """Locked-denominator cohort summary + full per-patient gated packets."""

    snapshot_id: str
    snapshot_manifest_sha256: str
    request: CohortRequest
    denominator_definition: str
    total_raw_records: int
    excluded_controls: int
    eligible_human_patients: int
    ready_count: int
    unknown_count: int
    conflict_count: int
    funnel: tuple[FunnelStage, ...]
    reason_breakdown: dict[str, int]
    packets: tuple[PatientPacket, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "request": self.request.to_dict(),
            "denominator_definition": self.denominator_definition,
            "total_raw_records": self.total_raw_records,
            "excluded_controls": self.excluded_controls,
            "eligible_human_patients": self.eligible_human_patients,
            "ready_count": self.ready_count,
            "unknown_count": self.unknown_count,
            "conflict_count": self.conflict_count,
            "ready_fraction": (
                round(self.ready_count / self.eligible_human_patients, 6)
                if self.eligible_human_patients > 0
                else None
            ),
            "funnel": [asdict(s) for s in self.funnel],
            "reason_breakdown": dict(sorted(self.reason_breakdown.items())),
            "packets": [p.to_dict() for p in self.packets],
        }
