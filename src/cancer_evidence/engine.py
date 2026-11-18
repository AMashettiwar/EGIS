from __future__ import annotations

from collections import defaultdict
from typing import Any

SUPPORTED = "SUPPORTED"
UNKNOWN = "UNKNOWN"
CONFLICT = "CONFLICT"
AMBIGUOUS = "AMBIGUOUS"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
ERROR = "ERROR"


def _result(status: str, reason: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, "reason_code": reason, "numerator": None, "denominator": None, "filters": {}, "evidence_references": [], **fields}


def _index_source_records(fixture: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ids: set[str] = set()
    for record in fixture.get("source_records", []):
        source_id = record.get("source_record_id")
        if not source_id:
            raise ValueError("source record is missing source_record_id")
        ids.add(source_id)
        by_entity[record.get("entity_id", "")].append(record)
    return by_entity, ids


def _validate_fixture(fixture: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    metadata = fixture.get("fixture_metadata", {})
    if metadata.get("synthetic_only") is not True or "NOT TCGA" not in metadata.get("label", "") or "NOT CPTAC" not in metadata.get("label", ""):
        raise ValueError("fixture is not explicitly synthetic-only")
    if metadata.get("complete") is False:
        raise ValueError("fixture is incomplete")
    return _index_source_records(fixture)


def _conflicting_fields(records: list[dict[str, Any]]) -> bool:
    values: dict[str, set[str]] = defaultdict(set)
    for record in records:
        values[record.get("field", "")].add(repr(record.get("value")))
    return any(len(items) > 1 for items in values.values())


def evaluate(fixture: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    try:
        if query.get("operation") != "case_overlap":
            return _result(OUT_OF_SCOPE, "UNSUPPORTED_OPERATION")
        if "left" not in query or "right" not in query:
            if "left" not in query and "right" not in query:
                return _result(AMBIGUOUS, "ENTITY_LEVEL_OR_SET_MISSING")
            return _result(ERROR, "INVALID_QUERY")
        if "entity_level" not in query:
            return _result(AMBIGUOUS, "ENTITY_LEVEL_OR_SET_MISSING")
        if not isinstance(query.get("left"), list) or not isinstance(query.get("right"), list):
            return _result(ERROR, "INVALID_QUERY")
        if query.get("entity_level") != "case":
            return _result(AMBIGUOUS, "ENTITY_LEVEL_OR_SET_MISSING")
        if not query.get("left") or not query.get("right"):
            return _result(ERROR, "EMPTY_ASSAY_RECORD_SET")
        metadata = fixture.get("fixture_metadata", {})
        if query.get("fixture_id") != metadata.get("fixture_id") or query.get("cohort_id") != metadata.get("cohort_id"):
            return _result(UNKNOWN, "COHORT_OR_FIXTURE_MISMATCH")
        if metadata.get("complete") is False:
            return _result(UNKNOWN, "INCOMPLETE_FIXTURE")
        source_records, source_record_ids = _validate_fixture(fixture)
        cases = {record["case_id"]: record for record in fixture.get("cases", [])}
        samples = {record["sample_id"]: record for record in fixture.get("samples", [])}
        assays = {record["assay_record_id"]: record for record in fixture.get("assay_records", [])}
        files = fixture.get("files", [])
        requested_ids = list(dict.fromkeys(query["left"] + query["right"]))
        if any(assay_id not in assays for assay_id in requested_ids):
            return _result(OUT_OF_SCOPE, "UNKNOWN_ASSAY_RECORD")
        evidence_refs: set[str] = set()
        for assay_id in requested_ids:
            assay = assays[assay_id]
            if assay_id not in source_records or not source_records[assay_id]:
                return _result(UNKNOWN, "MISSING_EVIDENCE")
            if _conflicting_fields(source_records[assay_id]):
                return _result(CONFLICT, "CONFLICTING_EVIDENCE")
            sample = samples.get(assay.get("sample_id"))
            case = cases.get(assay.get("case_id"))
            if sample is None or case is None or sample.get("case_id") != case.get("case_id"):
                return _result(CONFLICT, "CASE_SAMPLE_LINK_CONFLICT")
            for entity_id in (assay_id, assay.get("sample_id"), assay.get("case_id")):
                records = source_records.get(entity_id, [])
                if not records:
                    return _result(UNKNOWN, "MISSING_EVIDENCE")
                evidence_refs.update(record["source_record_id"] for record in records)
            evidence_refs.update(file["source_record_id"] for file in files if file.get("assay_record_id") == assay_id and file.get("source_record_id") in source_record_ids)
        def cases_for(ids: list[str]) -> set[str]:
            return {assays[assay_id]["case_id"] for assay_id in ids}
        left_cases = cases_for(query["left"])
        right_cases = cases_for(query["right"])
        overlap = sorted(left_cases & right_cases)
        return {
            "status": SUPPORTED,
            "reason_code": "NONE",
            "numerator": len(overlap),
            "denominator": len(left_cases),
            "filters": {"entity_level": "case", "fixture_id": metadata["fixture_id"], "cohort_id": metadata["cohort_id"], "left_assay_record_ids": list(dict.fromkeys(query["left"])), "right_assay_record_ids": list(dict.fromkeys(query["right"]))},
            "overlap_case_ids": overlap,
            "evidence_references": sorted(evidence_refs),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return _result(ERROR, "MALFORMED_FIXTURE", error=str(exc))

