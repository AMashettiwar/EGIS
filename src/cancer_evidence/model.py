from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Status(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"
    AMBIGUOUS = "AMBIGUOUS"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    ERROR = "ERROR"


class Reason(str, Enum):
    NONE = "NONE"
    INVALID_QUERY = "INVALID_QUERY"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    INCOMPLETE_SNAPSHOT = "INCOMPLETE_SNAPSHOT"
    MISSING_LINKAGE = "MISSING_LINKAGE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    AMBIGUOUS_SELECTOR = "AMBIGUOUS_SELECTOR"
    ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
    CROSS_SOURCE_JOIN_FORBIDDEN = "CROSS_SOURCE_JOIN_FORBIDDEN"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    COHORT_OR_FIXTURE_MISMATCH = "COHORT_OR_FIXTURE_MISMATCH"
    CERTIFICATE_TAMPERED = "CERTIFICATE_TAMPERED"
    REAL_SOURCE_NOT_RUN = "REAL_SOURCE_NOT_RUN"
    DERIVATION_MISMATCH = "DERIVATION_MISMATCH"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    MALFORMED_CERTIFICATE = "MALFORMED_CERTIFICATE"
    NO_APPROVED_MAPPING = "NO_APPROVED_MAPPING"


@dataclass(frozen=True)
class Entity:
    entity_id: str
    kind: str
    source: str
    source_id: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class Relationship:
    subject_id: str
    predicate: str
    object_id: str
    source_record_id: str
    source: str


@dataclass(frozen=True)
class Answer:
    status: Status
    reason_code: Reason
    result: dict[str, Any]
    witness: list[str]
    evidence_refs: list[str]

