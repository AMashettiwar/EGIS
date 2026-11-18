from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

RULE_VERSION = "case-overlap-v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fixture_hash(fixture: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(fixture)).hexdigest()


def certificate_digest(certificate: dict[str, Any]) -> str:
    unsigned = dict(certificate)
    unsigned.pop("certificate_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _reconstruct(fixture: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    metadata = fixture["fixture_metadata"]
    if metadata.get("synthetic_only") is not True or "NOT TCGA" not in metadata.get("label", "") or "NOT CPTAC" not in metadata.get("label", ""):
        return {"status": "UNKNOWN", "reason_code": "NON_SYNTHETIC_FIXTURE", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    if metadata.get("complete") is False:
        return {"status": "UNKNOWN", "reason_code": "INCOMPLETE_FIXTURE", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    source_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_ids = set()
    for record in fixture["source_records"]:
        source_records[record["entity_id"]].append(record)
        source_ids.add(record["source_record_id"])
    cases = {record["case_id"]: record for record in fixture["cases"]}
    samples = {record["sample_id"]: record for record in fixture["samples"]}
    assays = {record["assay_record_id"]: record for record in fixture["assay_records"]}
    files = fixture.get("files", [])
    if query.get("entity_level") != "case":
        return {"status": "AMBIGUOUS", "reason_code": "ENTITY_LEVEL_OR_SET_MISSING", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    if "left" not in query or "right" not in query:
        return {"status": "ERROR", "reason_code": "INVALID_QUERY", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    if not query.get("left") or not query.get("right"):
        return {"status": "ERROR", "reason_code": "EMPTY_ASSAY_RECORD_SET", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    evidence: set[str] = set()
    for assay_id in list(dict.fromkeys(query["left"] + query["right"])):
        if assay_id not in assays:
            return {"status": "OUT_OF_SCOPE", "reason_code": "UNKNOWN_ASSAY_RECORD", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
        assay = assays[assay_id]
        for entity_id in (assay_id, assay["sample_id"], assay["case_id"]):
            records = source_records.get(entity_id, [])
            if not records:
                return {"status": "UNKNOWN", "reason_code": "MISSING_EVIDENCE", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
            by_field: dict[str, set[str]] = defaultdict(set)
            for record in records:
                by_field[record["field"]].add(repr(record["value"]))
                evidence.add(record["source_record_id"])
            if any(len(values) > 1 for values in by_field.values()):
                return {"status": "CONFLICT", "reason_code": "CONFLICTING_EVIDENCE", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
        evidence.update(
            record["source_record_id"]
            for record in files
            if record.get("assay_record_id") == assay_id
            and record.get("source_record_id") in source_ids
        )
        sample = samples.get(assay["sample_id"])
        if sample is None or sample["case_id"] != assay["case_id"] or assay["case_id"] not in cases:
            return {"status": "CONFLICT", "reason_code": "CASE_SAMPLE_LINK_CONFLICT", "numerator": None, "denominator": None, "filters": {}, "evidence_references": []}
    left = {assays[assay_id]["case_id"] for assay_id in query["left"]}
    right = {assays[assay_id]["case_id"] for assay_id in query["right"]}
    return {"status": "SUPPORTED", "reason_code": "NONE", "numerator": len(left & right), "denominator": len(left), "filters": {"entity_level": "case", "fixture_id": metadata["fixture_id"], "cohort_id": metadata["cohort_id"], "left_assay_record_ids": list(dict.fromkeys(query["left"])), "right_assay_record_ids": list(dict.fromkeys(query["right"]))}, "overlap_case_ids": sorted(left & right), "evidence_references": sorted(evidence)}


def replay_certificate(fixture: dict[str, Any], certificate: dict[str, Any]) -> dict[str, Any]:
    if certificate.get("rule_version") != RULE_VERSION:
        return {"valid": False, "reason_code": "RULE_VERSION_MISMATCH"}
    if certificate.get("fixture_sha256") != fixture_hash(fixture):
        return {"valid": False, "reason_code": "FIXTURE_HASH_MISMATCH"}
    if certificate.get("certificate_sha256") != certificate_digest(certificate):
        return {"valid": False, "reason_code": "CERTIFICATE_DIGEST_MISMATCH"}
    expected = _reconstruct(fixture, certificate["query"])
    if expected != certificate.get("result"):
        return {"valid": False, "reason_code": "RESULT_MISMATCH", "expected": expected}
    return {"valid": True, "reason_code": "NONE", "status": expected["status"]}

