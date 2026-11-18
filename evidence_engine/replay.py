from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Reason, Status
from .snapshot import canonical_json, read_snapshot, sha256_bytes

RULE_VERSION = "rules-1.0.0"
ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}


def replay_certificate(snapshot_dir: Path, certificate_path: Path) -> dict[str, Any]:
    certificate = json.loads(Path(certificate_path).read_text(encoding="utf-8"))
    try:
        manifest, connection = read_snapshot(snapshot_dir)
    except ValueError as exc:
        return {"valid": False, "reason_code": Reason.SNAPSHOT_MISMATCH.value, "error": str(exc)}
    try:
        unsigned = dict(certificate)
        digest = unsigned.pop("certificate_sha256", None)
        if digest != sha256_bytes(canonical_json(unsigned).encode("utf-8")):
            return {"valid": False, "reason_code": Reason.CERTIFICATE_TAMPERED.value}
        if certificate.get("engine") != "cancer-evidence-engine" or certificate.get("rule_version") != RULE_VERSION:
            return {"valid": False, "reason_code": Reason.CERTIFICATE_TAMPERED.value}
        if certificate.get("snapshot_id") != manifest.get("snapshot_id") or certificate.get("snapshot_manifest_sha256") != manifest.get("manifest_sha256"):
            return {"valid": False, "reason_code": Reason.SNAPSHOT_MISMATCH.value}
        scope = certificate.get("evidence_scope", {})
        if scope.get("source") != manifest.get("source") or scope.get("coverage") != manifest.get("coverage"):
            return {"valid": False, "reason_code": Reason.CERTIFICATE_TAMPERED.value}
        query = certificate.get("query", {})
        if certificate.get("query_sha256") != _query_hash(query):
            return {"valid": False, "reason_code": Reason.CERTIFICATE_TAMPERED.value}
        expected = _recompute(connection, query, manifest)
        if expected != certificate.get("result"):
            return {"valid": False, "reason_code": Reason.CERTIFICATE_TAMPERED.value, "expected": expected}
        return {"valid": True, "status": certificate["result"].get("status"), "reason_code": Reason.NONE.value}
    finally:
        connection.close()


def _query_hash(query: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(canonical_json(query).encode("utf-8")).hexdigest()


def _entities(connection: Any) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT entity_id, kind, source, source_id, attributes_json FROM entities").fetchall()
    return {row[0]: {"entity_id": row[0], "kind": row[1], "source": row[2], "source_id": row[3], "attributes": json.loads(row[4])} for row in rows}


def _edges(connection: Any) -> list[tuple[str, str, str, str]]:
    return connection.execute("SELECT subject_id, predicate, object_id, source_record_id FROM relationships").fetchall()


def _entity_set(connection: Any, selector: dict[str, Any]) -> set[str]:
    entities = _entities(connection)
    kind = selector.get("entity_level", "case")
    values = {key for key, value in entities.items() if value["kind"] == kind}
    for field in ("source", "project_id", "study_id", "assay_type", "data_type"):
        if selector.get(field) is not None:
            values = {key for key in values if (entities[key]["source"] if field == "source" else entities[key]["attributes"].get(field)) == selector[field]}
    return values


def _case_sets(connection: Any, selector: dict[str, Any]) -> set[str]:
    source = selector.get("source")
    if not source:
        return set()
    entities = _entities(connection)
    case_selector = dict(selector)
    case_selector.pop("assay_type", None)
    case_selector["entity_level"] = "case"
    values = _entity_set(connection, case_selector)
    assay_type = selector.get("assay_type")
    if assay_type:
        assays = {key for key in _candidate_assays(connection, selector) if entities[key]["attributes"].get("eligible") is True}
        values = {case_id for case_id in values if _case_reaches_assay(connection, case_id, assays)}
    return values


def _candidate_assays(connection: Any, selector: dict[str, Any]) -> set[str]:
    entities = _entities(connection)
    base = dict(selector)
    base.pop("assay_type", None)
    base["entity_level"] = "case"
    cases = _entity_set(connection, base)
    candidates = {key for key, value in entities.items() if value["kind"] == "assay" and value["source"] == selector.get("source") and value["attributes"].get("assay_type") == selector.get("assay_type")}
    return {assay_id for assay_id in candidates if any(_case_reaches_assay(connection, case_id, {assay_id}) for case_id in cases)}


def _case_reaches_assay(connection: Any, case_id: str, assay_ids: set[str]) -> bool:
    edges = _edges(connection)
    frontier = [case_id]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in assay_ids:
            return True
        frontier.extend(object_id for subject, _, object_id, _ in edges if subject == current)
    return False


def _evidence_for(connection: Any, ids: set[str]) -> list[str]:
    refs = [row[0] for entity_id in sorted(ids) for row in connection.execute("SELECT evidence_id FROM evidence WHERE entity_id = ? ORDER BY evidence_id", (entity_id,)).fetchall()]
    refs.extend(f"rel:{row[3]}" for row in _edges(connection) if row[0] in ids or row[2] in ids)
    return sorted(set(refs))


def _lineage(connection: Any, entity_id: str) -> list[str]:
    edges = _edges(connection)
    seen: set[str] = set()
    frontier = [entity_id]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(row[0] for row in edges if row[2] == current)
        if current == entity_id:
            frontier.extend(row[2] for row in edges if row[0] == current)
    return sorted(seen)


def _conflict_entities(connection: Any, ids: set[str]) -> set[str]:
    conflicts: set[str] = set()
    for entity_id in ids:
        fields: dict[str, set[str]] = {}
        for field, value in connection.execute("SELECT field_name, canonical_value_json FROM evidence WHERE entity_id = ?", (entity_id,)).fetchall():
            fields.setdefault(field, set()).add(value)
        if any(len(values) > 1 for values in fields.values()):
            conflicts.add(entity_id)
    return conflicts


def _coverage_ok(manifest: dict[str, Any], sources: set[str]) -> bool:
    return all(source in manifest.get("coverage", {}) and manifest["coverage"][source].get("complete") is True for source in sources)


def _recompute(connection: Any, query: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    operation = query.get("operation")
    supported_operations = {"count_entities", "assay_availability", "assay_overlap", "file_lineage", "compare_inventories"}
    if operation not in supported_operations:
        return _result(Status.OUT_OF_SCOPE, Reason.UNSUPPORTED_OPERATION, {}, set(), connection)
    if not manifest.get("complete"):
        return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
    if operation == "count_entities":
        selector = dict(query.get("selector", {}))
        if query.get("entity_level") and not selector.get("entity_level"):
            selector["entity_level"] = query["entity_level"]
        if selector.get("entity_level", "case") not in ENTITY_KINDS:
            return _result(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, set(), connection)
        if set(selector) - SELECTOR_FIELDS:
            return _result(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, set(), connection)
        if selector.get("source") and not _coverage_ok(manifest, {selector["source"]}):
            return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
        kind = selector.get("entity_level", "case")
        base_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
        base_values = _entity_set(connection, base_selector)
        if _conflict_entities(connection, base_values):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, base_values, connection)
        values = _entity_set(connection, selector)
        if _conflict_entities(connection, values):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, values, connection)
        return _result(Status.SUPPORTED, Reason.NONE, {"count": len(values), "entity_level": kind}, values, connection)
    if operation == "assay_availability":
        selector = query.get("selector", {})
        if not selector.get("source"):
            return _result(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, set(), connection)
        if set(selector) - SELECTOR_FIELDS:
            return _result(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, set(), connection)
        if not _coverage_ok(manifest, {selector["source"]}):
            return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
        base_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
        base_cases = _entity_set(connection, base_selector)
        if _conflict_entities(connection, base_cases):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, base_cases, connection)
        candidate_assays = _candidate_assays(connection, selector)
        if _conflict_entities(connection, candidate_assays):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, set(candidate_assays), connection, witness=[])
        values = _case_sets(connection, selector)
        if _conflict_entities(connection, values | candidate_assays):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, values | candidate_assays, connection, witness=sorted(values))
        return _result(Status.SUPPORTED, Reason.NONE, {"count": len(values), "entity_level": "case"}, values | candidate_assays, connection, witness=sorted(values))
    if operation == "assay_overlap":
        left_selector = query.get("left", {})
        right_selector = query.get("right", {})
        if not left_selector.get("source") or not right_selector.get("source"):
            return _result(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, set(), connection)
        if left_selector.get("source") != right_selector.get("source"):
            return _result(Status.OUT_OF_SCOPE, Reason.CROSS_SOURCE_JOIN_FORBIDDEN, {}, set(), connection)
        if set(left_selector) - SELECTOR_FIELDS or set(right_selector) - SELECTOR_FIELDS:
            return _result(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, set(), connection)
        if not _coverage_ok(manifest, {left_selector["source"], right_selector["source"]}):
            return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
        left_base = {key: value for key, value in left_selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
        right_base = {key: value for key, value in right_selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
        left_assays = _candidate_assays(connection, left_selector)
        right_assays = _candidate_assays(connection, right_selector)
        if _conflict_entities(connection, _entity_set(connection, left_base) | _entity_set(connection, right_base) | left_assays | right_assays):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, set(), connection)
        if query.get("denominator", "left") not in {"left", "union"}:
            return _result(Status.AMBIGUOUS, Reason.INVALID_QUERY, {}, set(), connection)
        left = _case_sets(connection, left_selector)
        right = _case_sets(connection, right_selector)
        denominator = len(left) if query.get("denominator", "left") == "left" else len(left | right)
        if denominator == 0:
            return _result(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {"numerator": 0, "denominator": 0}, set(), connection)
        overlap = sorted(left & right)
        assays = left_assays | right_assays
        if _conflict_entities(connection, (left | right) | assays):
            return _result(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, set(overlap), connection, witness=overlap)
        return _result(Status.SUPPORTED, Reason.NONE, {"numerator": len(overlap), "denominator": denominator, "percentage": len(overlap) / denominator * 100, "entity_level": "case"}, set(overlap) | assays, connection, witness=overlap)
    if operation == "file_lineage":
        entity_id = query.get("entity_id")
        entities = _entities(connection)
        if entity_id not in entities:
            return _result(Status.UNKNOWN, Reason.MISSING_LINKAGE, {}, set(), connection)
        if entities[entity_id]["kind"] != "file" or not _coverage_ok(manifest, {entities[entity_id]["source"]}):
            return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
        lineage = _lineage(connection, entity_id)
        if len(lineage) == 1:
            return _result(Status.UNKNOWN, Reason.MISSING_LINKAGE, {}, {entity_id}, connection, witness=[])
        return _result(Status.SUPPORTED, Reason.NONE, {"entity_id": entity_id, "lineage": lineage}, set(lineage), connection, witness=lineage)
    if operation == "compare_inventories":
        sources = query.get("sources", [])
        if not sources or len(set(sources)) != len(sources):
            return _result(Status.AMBIGUOUS, Reason.INVALID_QUERY, {}, set(), connection)
        if not _coverage_ok(manifest, set(sources)):
            return _result(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, set(), connection)
        inventories = {source: {"count": len(_entity_set(connection, {"source": source, "entity_level": "case"}))} for source in sources}
        return {"status": Status.SUPPORTED.value, "reason_code": Reason.NONE.value, "result": {"inventories": inventories, "pooled_count": None}, "witness": [], "evidence_refs": []}
    return _result(Status.OUT_OF_SCOPE, Reason.UNSUPPORTED_OPERATION, {}, set(), connection)


def _result(status: Status, reason: Reason, result: dict[str, Any], ids: set[str], connection: Any, witness: list[str] | None = None) -> dict[str, Any]:
    return {"status": status.value, "reason_code": reason.value, "result": result, "witness": sorted(witness if witness is not None else ids), "evidence_refs": _evidence_for(connection, ids)}

