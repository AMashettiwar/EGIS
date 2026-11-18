from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Reason, Status
from .snapshot import read_snapshot, sha256_json

RULE_VERSION = "rules-2.0.0"
ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}


def _entities(connection: Any) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT entity_id, kind, source, source_id, attributes_json FROM entities").fetchall()
    return {row[0]: {"entity_id": row[0], "kind": row[1], "source": row[2], "source_id": row[3], "attributes": json.loads(row[4])} for row in rows}


def _edges(connection: Any) -> list[tuple[str, str, str, str]]:
    return connection.execute("SELECT subject_id, predicate, object_id, source_record_id FROM relationships").fetchall()


def _entity_set(connection: Any, selector: dict[str, Any]) -> set[str]:
    entities = _entities(connection)
    kind = selector.get("entity_level", "case")
    if kind not in ENTITY_KINDS:
        return set()
    values = {key for key, value in entities.items() if value["kind"] == kind}
    for field in ("source", "project_id", "study_id", "assay_type", "data_type"):
        if selector.get(field) is not None:
            values = {key for key in values if (entities[key]["source"] if field == "source" else entities[key]["attributes"].get(field)) == selector[field]}
    return values


def _case_sets(connection: Any, selector: dict[str, Any]) -> set[str]:
    entities = _entities(connection)
    source = selector.get("source")
    if not source:
        return set()
    case_selector = dict(selector)
    case_selector.pop("assay_type", None)
    case_selector["entity_level"] = "case"
    case_ids = _entity_set(connection, case_selector)
    assay_type = selector.get("assay_type")
    if assay_type:
        eligible_assays = {key for key in _candidate_assays(connection, selector) if entities[key]["attributes"].get("eligible") is True}
        case_ids = {case_id for case_id in case_ids if _case_reaches_assay(connection, case_id, eligible_assays)}
    return case_ids


def _candidate_assays(connection: Any, selector: dict[str, Any]) -> set[str]:
    entities = _entities(connection)
    base = dict(selector)
    base.pop("assay_type", None)
    base["entity_level"] = "case"
    cases = _entity_set(connection, base)
    file_assays = {subject for subject, predicate, _, _ in _edges(connection) if predicate == "HAS_FILE"}
    candidates = {key for key, value in entities.items() if value["kind"] == "assay" and value["source"] == selector.get("source") and value["attributes"].get("assay_type") == selector.get("assay_type") and key in file_assays}
    return {assay_id for assay_id in candidates if any(_case_reaches_assay(connection, case_id, {assay_id}) for case_id in cases)}


def _cohort_cases(connection: Any, cohort_filter: dict[str, Any], *, eligible: bool) -> set[str]:
    selector = {key: cohort_filter[key] for key in ("source", "project_id", "study_id") if cohort_filter.get(key) is not None}
    selector["entity_level"] = "case"
    values = _entity_set(connection, selector)
    if not eligible:
        return values
    field = cohort_filter.get("eligibility_field")
    if not field:
        return values
    expected = cohort_filter.get("eligibility_value", True)
    entities = _entities(connection)
    return {entity_id for entity_id in values if entities[entity_id]["attributes"].get(field) == expected}


def _coverage_ok(manifest: dict[str, Any], sources: set[str]) -> bool:
    return all(source in manifest.get("coverage", {}) and manifest["coverage"][source].get("complete") is True for source in sources)


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


def _evidence_for(connection: Any, entity_ids: set[str]) -> list[str]:
    refs: list[str] = []
    for entity_id in sorted(entity_ids):
        refs.extend(row[0] for row in connection.execute("SELECT evidence_id FROM evidence WHERE entity_id = ? ORDER BY evidence_id", (entity_id,)).fetchall())
    for subject, _, object_id, source_record_id in _edges(connection):
        if subject in entity_ids or object_id in entity_ids:
            refs.append(f"rel:{source_record_id}")
    return sorted(set(refs))


def _lineage(connection: Any, entity_id: str) -> list[str]:
    seen: set[str] = set()
    frontier = [entity_id]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(row[0] for row in _edges(connection) if row[2] == current)
        if current == entity_id:
            frontier.extend(row[2] for row in _edges(connection) if row[0] == current)
    return sorted(seen)


def _conflict_entities(connection: Any, entity_ids: set[str]) -> set[str]:
    conflicts: set[str] = set()
    for entity_id in entity_ids:
        rows = connection.execute("SELECT field_name, canonical_value_json FROM evidence WHERE entity_id = ?", (entity_id,)).fetchall()
        fields: dict[str, set[str]] = {}
        for field, value in rows:
            fields.setdefault(field, set()).add(value)
        if any(len(values) > 1 for values in fields.values()):
            conflicts.add(entity_id)
    return conflicts


def _answer(status: Status, reason: Reason, result: dict[str, Any], witness: list[str], evidence_refs: list[str]) -> dict[str, Any]:
    return {"status": status.value, "reason_code": reason.value, "result": result, "witness": witness, "evidence_refs": evidence_refs}


def evaluate(snapshot_dir: Path, query: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(query, dict):
        return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
    try:
        manifest, connection = read_snapshot(snapshot_dir)
    except (OSError, ValueError, TypeError) as exc:
        return _answer(Status.UNKNOWN, Reason.SNAPSHOT_MISMATCH, {"error": str(exc)}, [], [])
    try:
        operation = query.get("operation")
        if operation not in {"count_entities", "assay_availability", "assay_overlap", "file_lineage", "compare_inventories"}:
            return _answer(Status.OUT_OF_SCOPE, Reason.UNSUPPORTED_OPERATION, {}, [], [])
        if query.get("snapshot_id") is not None and query.get("snapshot_id") != manifest.get("snapshot_id"):
            return _answer(Status.UNKNOWN, Reason.SNAPSHOT_MISMATCH, {}, [], [])
        if query.get("fixture_id") is not None and query.get("fixture_id") != manifest.get("fixture_id"):
            return _answer(Status.UNKNOWN, Reason.COHORT_OR_FIXTURE_MISMATCH, {}, [], [])
        if query.get("cohort_id") is not None and query.get("cohort_id") != manifest.get("cohort_id"):
            return _answer(Status.UNKNOWN, Reason.COHORT_OR_FIXTURE_MISMATCH, {}, [], [])
        if not manifest.get("complete"):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        if operation == "count_entities":
            if not isinstance(query.get("selector", {}), dict):
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
            selector = dict(query.get("selector", {}))
            if query.get("entity_level") and not selector.get("entity_level"):
                selector["entity_level"] = query["entity_level"]
            kind = selector.get("entity_level", "case")
            if kind not in ENTITY_KINDS:
                return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])
            if set(selector) - SELECTOR_FIELDS:
                return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])
            if selector.get("source") and not _coverage_ok(manifest, {selector["source"]}):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
            base_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
            base_values = _entity_set(connection, base_selector)
            if _conflict_entities(connection, base_values):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, sorted(base_values), _evidence_for(connection, base_values))
            values = _entity_set(connection, selector)
            if _conflict_entities(connection, values):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, sorted(values), _evidence_for(connection, values))
            return _answer(Status.SUPPORTED, Reason.NONE, {"count": len(values), "entity_level": kind}, sorted(values), _evidence_for(connection, values))
        if operation == "assay_availability":
            if not isinstance(query.get("selector", {}), dict):
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
            selector = dict(query.get("selector", {}))
            if not selector.get("source"):
                return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])
            if set(selector) - SELECTOR_FIELDS:
                return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])
            if not _coverage_ok(manifest, {selector["source"]}):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
            selector["entity_level"] = "case"
            base_selector = {key: value for key, value in selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
            base_cases = _entity_set(connection, base_selector)
            if _conflict_entities(connection, base_cases):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, sorted(base_cases), _evidence_for(connection, base_cases))
            candidate_assays = _candidate_assays(connection, selector)
            if _conflict_entities(connection, candidate_assays):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, [], _evidence_for(connection, candidate_assays))
            values = _case_sets(connection, selector)
            assay_ids = candidate_assays
            if _conflict_entities(connection, values):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, sorted(values), _evidence_for(connection, values | assay_ids))
            return _answer(Status.SUPPORTED, Reason.NONE, {"count": len(values), "entity_level": "case"}, sorted(values), _evidence_for(connection, values | assay_ids))
        if operation == "assay_overlap":
            left_selector = query.get("left", {})
            right_selector = query.get("right", {})
            if not isinstance(left_selector, dict) or not isinstance(right_selector, dict):
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
            if not left_selector.get("source") or not right_selector.get("source"):
                return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])
            if left_selector.get("source") != right_selector.get("source"):
                return _answer(Status.OUT_OF_SCOPE, Reason.CROSS_SOURCE_JOIN_FORBIDDEN, {}, [], [])
            if set(left_selector) - SELECTOR_FIELDS or set(right_selector) - SELECTOR_FIELDS:
                return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])
            if not _coverage_ok(manifest, {left_selector["source"], right_selector["source"]}):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
            left_base = {key: value for key, value in left_selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
            right_base = {key: value for key, value in right_selector.items() if key not in {"project_id", "study_id", "assay_type", "data_type"}}
            left_assays = _candidate_assays(connection, left_selector)
            right_assays = _candidate_assays(connection, right_selector)
            if _conflict_entities(connection, _entity_set(connection, left_base) | _entity_set(connection, right_base) | left_assays | right_assays):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, [], [])
            denominator_mode = query.get("denominator_mode", query.get("denominator", "left"))
            if denominator_mode == "union":
                denominator_mode = "universe"
            if denominator_mode not in {"left", "eligible", "universe"}:
                return _answer(Status.AMBIGUOUS, Reason.INVALID_QUERY, {}, [], [])
            left = _case_sets(connection, left_selector)
            right = _case_sets(connection, right_selector)
            cohort_filter = query.get("cohort_filter", {})
            if not isinstance(cohort_filter, dict):
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
            if denominator_mode in {"eligible", "universe"} and not cohort_filter.get("source"):
                return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])
            population = _cohort_cases(connection, cohort_filter, eligible=denominator_mode == "eligible") if denominator_mode in {"eligible", "universe"} else set()
            numerator_cases = (left & right) & population if denominator_mode in {"eligible", "universe"} else (left & right)
            denominator = len(left) if denominator_mode == "left" else len(population)
            if denominator == 0:
                return _answer(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {"numerator": 0, "denominator": 0}, [], [])
            overlap = sorted(numerator_cases)
            assay_entities = left_assays | right_assays
            evidence_ids = set(overlap) | assay_entities
            if _conflict_entities(connection, (left | right) | assay_entities):
                return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, overlap, _evidence_for(connection, evidence_ids))
            result = {"numerator": len(overlap), "denominator": denominator, "percentage": len(overlap) / denominator * 100, "entity_level": "case", "denominator_mode": denominator_mode, "denominator_population": "left_assay_cases" if denominator_mode == "left" else ("snapshot_eligible_cases" if denominator_mode == "eligible" else "snapshot_cohort_cases"), "eligible_set_size": len(population) if denominator_mode in {"eligible", "universe"} else None}
            return _answer(Status.SUPPORTED, Reason.NONE, result, overlap, _evidence_for(connection, evidence_ids))
        if operation == "file_lineage":
            entity_id = query.get("entity_id")
            entities = _entities(connection)
            if entity_id not in entities:
                return _answer(Status.UNKNOWN, Reason.MISSING_LINKAGE, {}, [], [])
            if entities[entity_id]["kind"] != "file" or not _coverage_ok(manifest, {entities[entity_id]["source"]}):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
            lineage = _lineage(connection, entity_id)
            if len(lineage) == 1:
                return _answer(Status.UNKNOWN, Reason.MISSING_LINKAGE, {}, [], _evidence_for(connection, {entity_id}))
            return _answer(Status.SUPPORTED, Reason.NONE, {"entity_id": entity_id, "lineage": lineage}, lineage, _evidence_for(connection, set(lineage)))
        sources = query.get("sources", [])
        if not sources or len(set(sources)) != len(sources):
            return _answer(Status.AMBIGUOUS, Reason.INVALID_QUERY, {}, [], [])
        if not _coverage_ok(manifest, set(sources)):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        inventories = {source: {"count": len(_entity_set(connection, {"source": source, "entity_level": "case"}))} for source in sources}
        return _answer(Status.SUPPORTED, Reason.NONE, {"inventories": inventories, "pooled_count": None}, [], [])
    finally:
        connection.close()


def query_hash(query: dict[str, Any]) -> str:
    return sha256_json(query)

