from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .model import Reason, Status
from .signing import DEFAULT_PUBLIC_KEY, verify
from .snapshot import canonical_json, derive_normalized, read_snapshot, sha256_bytes

RULE_VERSION = "rules-2.0.0"
PUBLISHED_PUBLIC_KEY = Path(__file__).resolve().parents[2] / "keys" / "public_key.pem"
ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}
OPERATIONS = {"count_entities", "assay_availability", "assay_overlap", "file_lineage", "compare_inventories"}


def _invalid(reason: str, **extra: Any) -> dict[str, Any]:
    result = {"valid": False, "reason_code": reason}
    result.update(extra)
    return result


def certificate_digest(certificate: dict[str, Any]) -> str:
    unsigned = dict(certificate)
    unsigned.pop("certificate_sha256", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _signature_bytes(certificate: dict[str, Any]) -> bytes:
    unsigned = dict(certificate)
    unsigned.pop("certificate_sha256", None)
    unsigned.pop("signature", None)
    return canonical_json(unsigned).encode("utf-8")


def _entities(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"entity_id": row[0], "kind": row[1], "source": row[2], "source_id": row[3], "attributes": json.loads(row[4])}
        for row in connection.execute("SELECT entity_id, kind, source, source_id, attributes_json FROM entities ORDER BY entity_id")
    ]


def _relationships(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return list(connection.execute("SELECT subject_id, predicate, object_id, source_record_id FROM relationships ORDER BY subject_id, predicate, object_id, source_record_id"))


def _evidence_refs(connection: sqlite3.Connection, entity_ids: set[str]) -> list[str]:
    refs = [row[0] for entity_id in sorted(entity_ids) for row in connection.execute("SELECT evidence_id FROM evidence WHERE entity_id = ? ORDER BY evidence_id", (entity_id,))]
    refs.extend(f"rel:{row[3]}" for row in _relationships(connection) if row[0] in entity_ids or row[2] in entity_ids)
    return sorted(set(refs))


def _answer(status: str, reason: str, result: dict[str, Any], witness: list[str], evidence_refs: list[str]) -> dict[str, Any]:
    return {"status": status, "reason_code": reason, "result": result, "witness": witness, "evidence_refs": evidence_refs}


def _coverage_ok(manifest: dict[str, Any], sources: set[str]) -> bool:
    return all(source in manifest.get("coverage", {}) and manifest["coverage"][source].get("complete") is True for source in sources)


def _selector_entities(connection: sqlite3.Connection, selector: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    rows = _entities(connection)
    selected = [row for row in rows if row["kind"] == kind]
    for field in ("source", "project_id", "study_id", "assay_type", "data_type"):
        if selector.get(field) is not None:
            selected = [row for row in selected if (row["source"] if field == "source" else row["attributes"].get(field)) == selector[field]]
    return selected


def _conflicts(connection: sqlite3.Connection, entity_ids: set[str]) -> set[str]:
    rows = connection.execute(
        "SELECT entity_id FROM evidence WHERE entity_id IN ({}) GROUP BY entity_id, field_name HAVING COUNT(DISTINCT canonical_value_json) > 1".format(
            ",".join("?" for _ in entity_ids) or "NULL"
        ),
        tuple(sorted(entity_ids)),
    ).fetchall() if entity_ids else []
    return {row[0] for row in rows}


def _has_file_assay_ids(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM relationships WHERE predicate = 'HAS_FILE'")}


def _reachable_assay_ids(connection: sqlite3.Connection, case_id: str, assay_ids: set[str]) -> bool:
    edges = _relationships(connection)
    frontier = deque([case_id])
    seen: set[str] = set()
    while frontier:
        node = frontier.popleft()
        if node in seen:
            continue
        seen.add(node)
        if node in assay_ids:
            return True
        frontier.extend(object_id for subject, _, object_id, _ in edges if subject == node)
    return False


def _case_set(connection: sqlite3.Connection, selector: dict[str, Any]) -> set[str]:
    case_selector = {key: value for key, value in selector.items() if key not in {"assay_type", "data_type", "entity_level"}}
    cases = _selector_entities(connection, case_selector, "case")
    assay_type = selector.get("assay_type")
    if not assay_type:
        return {row["entity_id"] for row in cases}
    assay_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "entity_level"}}
    candidates = {
        row["entity_id"]
        for row in _selector_entities(connection, assay_selector, "assay")
        if row["attributes"].get("eligible") is True and row["entity_id"] in _has_file_assay_ids(connection)
    }
    return {row["entity_id"] for row in cases if _reachable_assay_ids(connection, row["entity_id"], candidates)}


def _candidate_assays(connection: sqlite3.Connection, selector: dict[str, Any]) -> set[str]:
    if not selector.get("assay_type"):
        return set()
    assay_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "entity_level"}}
    case_selector = {key: value for key, value in selector.items() if key not in {"assay_type", "data_type", "entity_level"}}
    return {
        row["entity_id"]
        for row in _selector_entities(connection, assay_selector, "assay")
        if row["entity_id"] in _has_file_assay_ids(connection)
        and any(_reachable_assay_ids(connection, case["entity_id"], {row["entity_id"]}) for case in _selector_entities(connection, case_selector, "case"))
    }


def _cohort_cases(connection: sqlite3.Connection, cohort_filter: dict[str, Any], *, eligible: bool) -> set[str]:
    cases = _selector_entities(connection, cohort_filter, "case")
    if not eligible or not cohort_filter.get("eligibility_field"):
        return {row["entity_id"] for row in cases}
    field = cohort_filter["eligibility_field"]
    expected = cohort_filter.get("eligibility_value", True)
    return {row["entity_id"] for row in cases if row["attributes"].get(field) == expected}


def _reconstruct(connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Independent SQL-backed reconstruction; intentionally not engine.evaluate's control flow."""
    if not isinstance(query, dict):
        return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
    operation = query.get("operation")
    if operation not in OPERATIONS:
        return _answer(Status.OUT_OF_SCOPE.value, Reason.UNSUPPORTED_OPERATION.value, {}, [], [])
    if query.get("snapshot_id") is not None and query.get("snapshot_id") != manifest.get("snapshot_id"):
        return _answer(Status.UNKNOWN.value, Reason.SNAPSHOT_MISMATCH.value, {}, [], [])
    if query.get("fixture_id") is not None and query.get("fixture_id") != manifest.get("fixture_id"):
        return _answer(Status.UNKNOWN.value, Reason.COHORT_OR_FIXTURE_MISMATCH.value, {}, [], [])
    if query.get("cohort_id") is not None and query.get("cohort_id") != manifest.get("cohort_id"):
        return _answer(Status.UNKNOWN.value, Reason.COHORT_OR_FIXTURE_MISMATCH.value, {}, [], [])
    if not manifest.get("complete"):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
    if operation == "count_entities":
        selector = query.get("selector", {})
        if not isinstance(selector, dict):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        selector = dict(selector)
        if query.get("entity_level") and not selector.get("entity_level"):
            selector["entity_level"] = query["entity_level"]
        kind = selector.get("entity_level", "case")
        if kind not in ENTITY_KINDS or set(selector) - SELECTOR_FIELDS:
            return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
        if selector.get("source") and not _coverage_ok(manifest, {selector["source"]}):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        values = {row["entity_id"] for row in _selector_entities(connection, selector, kind)}
        base_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
        base_values = {row["entity_id"] for row in _selector_entities(connection, base_selector, kind)}
        if _conflicts(connection, base_values) or _conflicts(connection, values):
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, sorted(values or base_values), _evidence_refs(connection, values or base_values))
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, {"count": len(values), "entity_level": kind}, sorted(values), _evidence_refs(connection, values))
    if operation == "assay_availability":
        selector = query.get("selector", {})
        if not isinstance(selector, dict):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        if not selector.get("source"):
            return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])
        if set(selector) - SELECTOR_FIELDS:
            return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
        if not _coverage_ok(manifest, {selector["source"]}):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        cases = _case_set(connection, {**selector, "entity_level": "case"})
        assays = _candidate_assays(connection, selector)
        if _conflicts(connection, cases):
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, sorted(cases), _evidence_refs(connection, cases))
        if _conflicts(connection, assays):
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, [], _evidence_refs(connection, assays))
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, {"count": len(cases), "entity_level": "case"}, sorted(cases), _evidence_refs(connection, cases | assays))
    if operation == "assay_overlap":
        left = query.get("left", {})
        right = query.get("right", {})
        if not isinstance(left, dict) or not isinstance(right, dict):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        if not left.get("source") or not right.get("source"):
            return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])
        if left.get("source") != right.get("source"):
            return _answer(Status.OUT_OF_SCOPE.value, Reason.CROSS_SOURCE_JOIN_FORBIDDEN.value, {}, [], [])
        if set(left) - SELECTOR_FIELDS or set(right) - SELECTOR_FIELDS:
            return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
        if not _coverage_ok(manifest, {left["source"], right["source"]}):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        left_cases = _case_set(connection, left)
        right_cases = _case_set(connection, right)
        assays = _candidate_assays(connection, left) | _candidate_assays(connection, right)
        if _conflicts(connection, left_cases | right_cases | assays):
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, [], _evidence_refs(connection, assays | left_cases | right_cases))
        mode = query.get("denominator_mode", query.get("denominator", "left"))
        if mode == "union":
            mode = "universe"
        if mode not in {"left", "eligible", "universe"}:
            return _answer(Status.AMBIGUOUS.value, Reason.INVALID_QUERY.value, {}, [], [])
        cohort_filter = query.get("cohort_filter", {})
        if not isinstance(cohort_filter, dict):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        population = set()
        if mode in {"eligible", "universe"}:
            if not cohort_filter.get("source"):
                return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])
            population = _cohort_cases(connection, cohort_filter, eligible=mode == "eligible")
        overlap = left_cases & right_cases if mode == "left" else (left_cases & right_cases & population)
        denominator = len(left_cases) if mode == "left" else len(population)
        if denominator == 0:
            return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {"numerator": 0, "denominator": 0}, [], [])
        result = {"numerator": len(overlap), "denominator": denominator, "percentage": len(overlap) / denominator * 100, "entity_level": "case", "denominator_mode": mode, "denominator_population": "left_assay_cases" if mode == "left" else ("snapshot_eligible_cases" if mode == "eligible" else "snapshot_cohort_cases"), "eligible_set_size": len(population) if mode != "left" else None}
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, sorted(overlap), _evidence_refs(connection, set(overlap) | assays))
    if operation == "file_lineage":
        entity_id = query.get("entity_id")
        all_entities = {row["entity_id"]: row for row in _entities(connection)}
        if entity_id not in all_entities:
            return _answer(Status.UNKNOWN.value, Reason.MISSING_LINKAGE.value, {}, [], [])
        if all_entities[entity_id]["kind"] != "file" or not _coverage_ok(manifest, {all_entities[entity_id]["source"]}):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        edges = _relationships(connection)
        seen: set[str] = set()
        queue = deque([entity_id])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(subject for subject, _, object_id, _ in edges if object_id == node)
            if node == entity_id:
                queue.extend(object_id for subject, _, object_id, _ in edges if subject == node)
        if len(seen) == 1:
            return _answer(Status.UNKNOWN.value, Reason.MISSING_LINKAGE.value, {}, [entity_id], _evidence_refs(connection, {entity_id}))
        lineage = sorted(seen)
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, {"entity_id": entity_id, "lineage": lineage}, lineage, _evidence_refs(connection, set(lineage)))
    sources = query.get("sources", [])
    if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources):
        return _answer(Status.AMBIGUOUS.value, Reason.INVALID_QUERY.value, {}, [], [])
    if not _coverage_ok(manifest, set(sources)):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
    inventories = {source: {"count": len(_selector_entities(connection, {"source": source}, "case"))} for source in sources}
    return _answer(Status.SUPPORTED.value, Reason.NONE.value, {"inventories": inventories, "pooled_count": None}, [], [])


def replay_certificate(snapshot_dir: Path, certificate_path: Path) -> dict[str, Any]:
    try:
        certificate = json.loads(Path(certificate_path).read_text(encoding="utf-8"))
        if not isinstance(certificate, dict):
            return _invalid(Reason.MALFORMED_CERTIFICATE.value)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return _invalid(Reason.MALFORMED_CERTIFICATE.value)
    try:
        if certificate.get("rule_version") != RULE_VERSION:
            return _invalid("RULE_VERSION_MISMATCH")
        if certificate.get("engine") != "cancer-evidence-engine" or certificate.get("certificate_version") != "2.0.0" or certificate.get("signature_algorithm") != "Ed25519":
            return _invalid(Reason.MALFORMED_CERTIFICATE.value)
        if certificate.get("verification_public_key") != str(DEFAULT_PUBLIC_KEY).replace("\\", "/"):
            return _invalid(Reason.SIGNATURE_INVALID.value)
        if not verify(_signature_bytes(certificate), certificate.get("signature", ""), PUBLISHED_PUBLIC_KEY):
            return _invalid(Reason.SIGNATURE_INVALID.value)
        if certificate.get("certificate_sha256") != certificate_digest(certificate):
            return _invalid(Reason.CERTIFICATE_TAMPERED.value)
        query = certificate.get("query")
        manifest, connection = read_snapshot(snapshot_dir)
        try:
            if certificate.get("snapshot_id") != manifest.get("snapshot_id") or certificate.get("snapshot_manifest_sha256") != manifest.get("manifest_sha256") or certificate.get("snapshot_sha256") != manifest.get("raw_content_sha256"):
                return _invalid(Reason.SNAPSHOT_MISMATCH.value)
            if certificate.get("derivation") != manifest.get("derivation"):
                return _invalid(Reason.DERIVATION_MISMATCH.value)
            if certificate.get("query_sha256") != hashlib.sha256(canonical_json(query).encode("utf-8")).hexdigest():
                return _invalid(Reason.CERTIFICATE_TAMPERED.value)
            expected = _reconstruct(connection, query, manifest)
        finally:
            connection.close()
        if expected != certificate.get("result"):
            return _invalid("RESULT_MISMATCH", expected=expected)
        return {"valid": True, "status": expected["status"], "reason_code": Reason.NONE.value}
    except ValueError as exc:
        if "derivation mismatch" in str(exc):
            return _invalid(Reason.DERIVATION_MISMATCH.value)
        return _invalid(Reason.SNAPSHOT_MISMATCH.value, error=str(exc))
    except (OSError, KeyError, TypeError, json.JSONDecodeError, UnicodeError, sqlite3.Error):
        return _invalid(Reason.MALFORMED_CERTIFICATE.value)
