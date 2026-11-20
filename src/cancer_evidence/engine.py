from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import Reason, Status
from .snapshot import read_snapshot, sha256_json

RULE_VERSION = "rules-2.0.0"
ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
ALL_SUPPORTED_LEVELS = ENTITY_KINDS | {"patient"}
COHORT_CLINICAL_FIELDS = {
    "source",
    "project_id",
    "project_id_in",
    "study_id",
    "study_id_in",
    "disease_type",
    "disease_type_in",
    "primary_site",
    "primary_site_in",
    "histology",
    "histology_in",
    "stage",
    "stage_in",
}
COHORT_SAMPLE_FIELDS = {
    "sample_type",
    "sample_type_in",
    "tissue_type",
    "tissue_type_in",
    "tumor_descriptor",
    "tumor_descriptor_in",
    "preservation_method",
    "preservation_method_in",
    "min_tumor_purity",
    "max_collection_timestamp",
}
COHORT_ASSAY_ELIGIBILITY_FIELDS = {
    "biomarker",
    "biomarker_in",
    "panel_name",
    "panel_name_in",
    "assay_status",
    "assay_status_in",
    "qc_status",
    "qc_status_in",
}
COHORT_POSITIVE_FIELDS = {"result", "positive_results_in", "er_status", "pr_status", "her2_status"}
COHORT_MOLECULAR_FIELDS = {
    "gene_symbol",
    "gene_symbol_in",
    "gene_id",
    "ncbi_build",
    "canonical_transcript_id",
    "aa_change",
    "aa_change_in",
    "hgvsp_short",
    "hgvsp_short_in",
    "hgvsc",
    "hgvsc_in",
    "consequence_type",
    "consequence_type_in",
    "genomic_dna_change",
    "genomic_dna_change_in",
    "variant_process",
    "mutation_status",
    "call_status",
    "call_status_in",
    "require_explicit_negative_coverage",
}
COHORT_FILE_FIELDS = {
    "file_type",
    "file_type_in",
    "data_category",
    "data_category_in",
    "data_type",
    "data_type_in",
    "experimental_strategy",
    "experimental_strategy_in",
    "data_format",
    "data_format_in",
    "access",
    "state",
}
ALL_COHORT_FIELDS = (
    COHORT_CLINICAL_FIELDS
    | COHORT_SAMPLE_FIELDS
    | COHORT_ASSAY_ELIGIBILITY_FIELDS
    | COHORT_POSITIVE_FIELDS
    | COHORT_MOLECULAR_FIELDS
    | COHORT_FILE_FIELDS
)
INCOMPLETE_ASSAY_STATUSES = {"NOT_PERFORMED", "PENDING", "FAILED", "QC_FAILED"}
SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}
ALL_OPERATIONS = {
    "count_entities",
    "assay_availability",
    "assay_overlap",
    "file_lineage",
    "compare_inventories",
    "cohort_count",
    "claim_verification",
    "cross_source_overlap_count",
    "entity_attribute_query",
    "out_of_scope_request",
}
REQUIRED_COUNT_TABLES = ("patients", "cases", "samples", "assays", "files")


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
    if not entity_ids:
        return set()
    all_conflicted = {
        row[0]
        for row in connection.execute(
            "SELECT entity_id FROM evidence GROUP BY entity_id, field_name HAVING COUNT(DISTINCT canonical_value_json) > 1"
        ).fetchall()
    }
    return all_conflicted & entity_ids


def _has_table(connection: Any, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _pathology_reports(connection: Any) -> list[dict[str, Any]]:
    if not _has_table(connection, "pathology_reports"):
        return []
    rows = connection.execute("SELECT attributes_json FROM pathology_reports ORDER BY report_id").fetchall()
    return [json.loads(r[0]) for r in rows]


def _identity_mappings(connection: Any) -> list[dict[str, Any]]:
    if not _has_table(connection, "identity_mappings"):
        return []
    rows = connection.execute("SELECT attributes_json FROM identity_mappings ORDER BY mapping_id").fetchall()
    return [json.loads(r[0]) for r in rows]


def _matches_criteria(entity: dict[str, Any], criteria: dict[str, Any]) -> bool:
    attrs = entity.get("attributes", {})
    for key, expected in criteria.items():
        if key == "source":
            if entity.get("source") != expected:
                return False
        elif key == "positive_results_in":
            if attrs.get("result") not in expected:
                return False
        elif key == "min_tumor_purity":
            purity = attrs.get("tumor_purity")
            if purity is None or float(purity) < float(expected):
                return False
        elif key == "max_collection_timestamp":
            ts = attrs.get("collection_timestamp")
            if not isinstance(ts, str):
                return False
            try:
                ts_dt = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
                exp_str = str(expected)
                exp_dt = datetime.fromisoformat(exp_str[:-1] + "+00:00" if exp_str.endswith("Z") else exp_str)
                if ts_dt > exp_dt:
                    return False
            except ValueError:
                if ts > str(expected):
                    return False
        elif key.endswith("_in"):
            attr_name = key[:-3]
            if attrs.get(attr_name) not in expected:
                return False
        elif attrs.get(key) != expected:
            return False
    return True


def _edges_adj(edges: list[tuple[str, str, str, str]]) -> dict[str, list[tuple[str, str]]]:
    adj: dict[str, list[tuple[str, str]]] = {}
    for subject, predicate, object_id, _ in edges:
        adj.setdefault(subject, []).append((predicate, object_id))
    return adj


def _linked_descendants(
    edges_or_adj: list[tuple[str, str, str, str]] | dict[str, list[tuple[str, str]]],
    entities: dict[str, dict[str, Any]],
    root_id: str,
    target_kind: str,
    allowed_preds: set[str],
) -> set[str]:
    adj = edges_or_adj if isinstance(edges_or_adj, dict) else _edges_adj(edges_or_adj)
    frontier = [root_id]
    seen: set[str] = set()
    matched: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        if current != root_id and entities.get(current, {}).get("kind") == target_kind:
            matched.add(current)
        for predicate, object_id in adj.get(current, ()):
            if predicate in allowed_preds:
                frontier.append(object_id)
    return matched


def _case_linked_assays(
    edges_or_adj: list[tuple[str, str, str, str]] | dict[str, list[tuple[str, str]]],
    entities: dict[str, dict[str, Any]],
    case_id: str,
) -> set[str]:
    return _linked_descendants(edges_or_adj, entities, case_id, "assay", {"HAS_SAMPLE", "HAS_ALIQUOT", "HAS_ASSAY"})


def _check_provenance_and_ontology(manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any] | None:
    prov = manifest.get("provenance_metadata")
    if not isinstance(prov, dict):
        return None
    if prov.get("integrity_errors"):
        first_reason = prov["integrity_errors"][0].get("reason", Reason.PROVENANCE_ERROR.value)
        reason = Reason(first_reason) if first_reason in {r.value for r in Reason} else Reason.PROVENANCE_ERROR
        return _answer(Status.ERROR, reason, {}, [], [])
    entity_level = query.get("entity_level")
    if entity_level is not None:
        supported_levels = prov.get("supported_entity_levels")
        if not isinstance(entity_level, str):
            return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
        if (isinstance(supported_levels, list) and entity_level not in supported_levels) or (
            entity_level not in ALL_SUPPORTED_LEVELS
        ):
            return _answer(Status.ERROR, Reason.UNSUPPORTED_ENTITY_LEVEL, {}, [], [])
    return None


def _partial_tables_present(manifest: dict[str, Any], table_names: tuple[str, ...]) -> bool:
    prov = manifest.get("provenance_metadata")
    if not isinstance(prov, dict):
        return False
    tables_meta = prov.get("tables") or {}
    return any(
        isinstance(tables_meta.get(name), dict)
        and tables_meta[name].get("declared_completeness") == "PARTIAL_INGEST"
        for name in table_names
    )


def _canonical_patient_cases(
    entities: dict[str, dict[str, Any]],
    edges: list[tuple[str, str, str, str]],
    patient_ids: set[str],
) -> tuple[dict[str, list[str]], bool]:
    """Group non-duplicate cases per patient and report whether duplicate markers exist."""
    raw_by_patient: dict[str, set[str]] = {}
    for subject, pred_name, object_id, _ in edges:
        if pred_name == "HAS_CASE" and subject in patient_ids and entities.get(object_id, {}).get("kind") == "case":
            raw_by_patient.setdefault(subject, set()).add(object_id)

    has_dup_markers = any(
        "is_duplicate_case_entry" in entities[cid]["attributes"]
        for cids in raw_by_patient.values()
        for cid in cids
    )
    canonical_by_patient: dict[str, list[str]] = {}
    for pid, cids in raw_by_patient.items():
        primary = [
            cid for cid in sorted(cids)
            if entities[cid]["attributes"].get("is_duplicate_case_entry") is not True
        ]
        canonical_by_patient[pid] = primary if primary else sorted(cids)
    return canonical_by_patient, has_dup_markers


def _evaluate_cohort_count(connection: Any, manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    entity_level = query.get("entity_level")
    if entity_level is None:
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])
    if not isinstance(entity_level, str) or entity_level not in ALL_SUPPORTED_LEVELS:
        return _answer(Status.ERROR, Reason.UNSUPPORTED_ENTITY_LEVEL, {}, [], [])

    denominator_basis = query.get("denominator_basis")
    if not isinstance(denominator_basis, str) or not denominator_basis.strip():
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])

    predicate = query.get("predicate")
    if not isinstance(predicate, dict):
        return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
    if not predicate or (set(predicate) - ALL_COHORT_FIELDS):
        return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])

    has_assay_or_sample_filter = bool(
        set(predicate)
        & (
            COHORT_POSITIVE_FIELDS
            | COHORT_SAMPLE_FIELDS
            | COHORT_FILE_FIELDS
            | COHORT_ASSAY_ELIGIBILITY_FIELDS
            | COHORT_MOLECULAR_FIELDS
        )
    )
    has_clin_or_assay_elig = bool(
        set(predicate)
        & (
            COHORT_CLINICAL_FIELDS
            | COHORT_ASSAY_ELIGIBILITY_FIELDS
            | COHORT_SAMPLE_FIELDS
            | COHORT_MOLECULAR_FIELDS
        )
    )
    if not has_assay_or_sample_filter or not (has_clin_or_assay_elig or (set(predicate) & COHORT_POSITIVE_FIELDS)):
        return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])

    for key, value in predicate.items():
        if key == "require_explicit_negative_coverage":
            if not isinstance(value, bool):
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
        elif key.endswith("_in"):
            if not isinstance(value, list) or not value:
                return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])
        elif isinstance(value, (list, dict)):
            return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])

    clinical_criteria = {k: v for k, v in predicate.items() if k in COHORT_CLINICAL_FIELDS}
    sample_criteria = {k: v for k, v in predicate.items() if k in COHORT_SAMPLE_FIELDS}
    assay_elig_criteria = {k: v for k, v in predicate.items() if k in COHORT_ASSAY_ELIGIBILITY_FIELDS}
    assay_pos_criteria = {k: v for k, v in predicate.items() if k in COHORT_POSITIVE_FIELDS}
    file_criteria = {k: v for k, v in predicate.items() if k in COHORT_FILE_FIELDS}
    molecular_criteria = {k: v for k, v in predicate.items() if k in COHORT_MOLECULAR_FIELDS}

    required_tables: list[str] = ["cases"]
    if entity_level == "patient":
        required_tables.append("patients")
    if entity_level in {"sample", "aliquot", "file"} or sample_criteria:
        required_tables.append("samples")
    if entity_level == "aliquot" or molecular_criteria:
        required_tables.append("aliquots")
    if entity_level == "assay" or assay_elig_criteria or assay_pos_criteria or molecular_criteria:
        required_tables.append("assays")
    if entity_level == "file" or file_criteria:
        required_tables.append("files")

    if _partial_tables_present(manifest, tuple(required_tables)):
        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    if molecular_criteria:
        if assay_elig_criteria or assay_pos_criteria:
            return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])
        return _evaluate_molecular_cohort_count(
            connection,
            manifest,
            query,
            clinical_criteria,
            sample_criteria,
            file_criteria,
            molecular_criteria,
        )

    entities = _entities(connection)
    edges = _edges(connection)
    adj = _edges_adj(edges)
    source_filter = predicate.get("source")
    patient_ids = {
        eid
        for eid, row in entities.items()
        if row["kind"] == "patient" and (source_filter is None or row["source"] == source_filter)
    }
    target_sources = (
        {source_filter}
        if source_filter is not None
        else (
            {entities[pid]["source"] for pid in patient_ids}
            or {row["source"] for row in entities.values() if row["kind"] == "case"}
            or set(manifest.get("coverage", {}).keys())
        )
    )
    if target_sources and not _coverage_ok(manifest, target_sources):
        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    canonical_cases_by_patient, has_dup_markers = _canonical_patient_cases(entities, edges, patient_ids)

    receptor_keys = {"er_status", "pr_status", "her2_status"}
    is_receptor_panel = bool(receptor_keys & set(assay_pos_criteria))
    # Default completed+PASS requirement when assay criteria omit explicit status/qc filters.
    effective_assay_elig = dict(assay_elig_criteria)
    if (assay_elig_criteria or assay_pos_criteria) and "assay_status" not in effective_assay_elig and "assay_status_in" not in effective_assay_elig:
        effective_assay_elig["assay_status"] = "COMPLETED"
    if (assay_elig_criteria or assay_pos_criteria) and "qc_status" not in effective_assay_elig and "qc_status_in" not in effective_assay_elig:
        effective_assay_elig["qc_status"] = "PASS"

    target_assay_scope = {
        k: v
        for k, v in assay_elig_criteria.items()
        if k in {"biomarker", "biomarker_in", "panel_name", "panel_name_in"}
    }

    all_canonical_cases = {cid for cids in canonical_cases_by_patient.values() for cid in cids}
    if not patient_ids and not all_canonical_cases and entity_level != "patient":
        all_canonical_cases = {
            eid
            for eid, row in entities.items()
            if row["kind"] == "case"
            and (source_filter is None or row["source"] == source_filter)
            and row["attributes"].get("is_duplicate_case_entry") is not True
        }

    clinically_eligible_cases = {
        cid for cid in all_canonical_cases if _matches_criteria(entities[cid], clinical_criteria)
    }
    scope_assays = {
        aid
        for cid in clinically_eligible_cases
        for aid in _case_linked_assays(adj, entities, cid)
        if _matches_criteria(entities[aid], target_assay_scope)
    }

    conflicted = _conflict_entities(connection, patient_ids | all_canonical_cases | scope_assays)
    if conflicted:
        return _answer(
            Status.CONFLICT,
            Reason.CONFLICTING_EVIDENCE,
            {},
            sorted(entities[e]["source_id"] for e in conflicted if e in entities),
            _evidence_for(connection, conflicted),
        )

    def _assay_passes_elig(aid: str) -> bool:
        row = entities[aid]
        if not _matches_criteria(row, effective_assay_elig):
            return False
        if is_receptor_panel:
            attrs = row["attributes"]
            return all(attrs.get(k) is not None for k in assay_pos_criteria if k in receptor_keys)
        return True

    if entity_level == "sample":
        eligible_samples: set[str] = set()
        positive_samples: set[str] = set()
        has_incomplete_assay = False
        has_assay_criteria = bool(assay_elig_criteria or assay_pos_criteria)

        if has_assay_criteria:
            for cid in sorted(clinically_eligible_cases):
                samples_for_case = _linked_descendants(adj, entities, cid, "sample", {"HAS_SAMPLE"})
                for sid in sorted(samples_for_case):
                    smp_assays = _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"})
                    smp_scope = {aid for aid in smp_assays if _matches_criteria(entities[aid], target_assay_scope)}
                    smp_elig = {aid for aid in smp_scope if _assay_passes_elig(aid)}
                    if not smp_elig:
                        if any(
                            entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
                            or entities[aid]["attributes"].get("result") is None
                            for aid in smp_scope
                        ):
                            has_incomplete_assay = True
                        continue
                    eligible_samples.add(sid)
                    if any(_matches_criteria(entities[aid], assay_pos_criteria) for aid in smp_elig):
                        positive_samples.add(sid)
        elif file_criteria:
            sample_elig_criteria = {
                k: v for k, v in sample_criteria.items() if k not in {"min_tumor_purity", "max_collection_timestamp"}
            }
            sample_cutoff_criteria = {
                k: v for k, v in sample_criteria.items() if k in {"min_tumor_purity", "max_collection_timestamp"}
            }
            for cid in sorted(clinically_eligible_cases):
                for sid in sorted(_linked_descendants(adj, entities, cid, "sample", {"HAS_SAMPLE"})):
                    if not _matches_criteria(entities[sid], sample_elig_criteria):
                        continue
                    if "min_tumor_purity" in sample_cutoff_criteria and entities[sid]["attributes"].get("tumor_purity") is None:
                        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
                    eligible_samples.add(sid)
                    if _matches_criteria(entities[sid], sample_cutoff_criteria):
                        linked_files = _linked_descendants(adj, entities, sid, "file", {"HAS_ALIQUOT", "HAS_FILE"})
                        if any(_matches_criteria(entities[fid], file_criteria) for fid in linked_files):
                            positive_samples.add(sid)
        else:
            for cid in sorted(clinically_eligible_cases):
                for sid in sorted(_linked_descendants(adj, entities, cid, "sample", {"HAS_SAMPLE"})):
                    eligible_samples.add(sid)
                    if _matches_criteria(entities[sid], sample_criteria):
                        positive_samples.add(sid)

        if has_incomplete_assay:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        if not eligible_samples:
            return _answer(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {}, [], [])

        pos_ids = sorted(entities[s]["source_id"] for s in positive_samples)
        result = {
            "numerator": len(pos_ids),
            "denominator": len(eligible_samples),
            "numerator_count": len(pos_ids),
            "denominator_count": len(eligible_samples),
            "percentage": len(pos_ids) / len(eligible_samples) * 100.0,
            "entity_level": "sample",
            "denominator_basis": denominator_basis,
            "supporting_evidence_ids": pos_ids,
        }
        return _answer(Status.SUPPORTED, Reason.NONE, result, pos_ids, pos_ids)

    if entity_level == "file":
        eligible_files: set[str] = set()
        positive_files: set[str] = set()
        has_incomplete_assay = False

        for cid in sorted(clinically_eligible_cases):
            for aid in sorted(_case_linked_assays(adj, entities, cid)):
                if not _matches_criteria(entities[aid], target_assay_scope):
                    continue
                if not _assay_passes_elig(aid):
                    if (
                        entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
                        or entities[aid]["attributes"].get("result") is None
                    ):
                        has_incomplete_assay = True
                    continue
                linked_files = _linked_descendants(adj, entities, aid, "file", {"HAS_FILE"})
                elig_assay_files = {fid for fid in linked_files if _matches_criteria(entities[fid], file_criteria)}
                eligible_files.update(elig_assay_files)
                if _matches_criteria(entities[aid], assay_pos_criteria):
                    positive_files.update(elig_assay_files)

        if has_incomplete_assay:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        if not eligible_files:
            return _answer(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {}, [], [])

        pos_ids = sorted(entities[f]["source_id"] for f in positive_files)
        result = {
            "numerator": len(pos_ids),
            "denominator": len(eligible_files),
            "numerator_count": len(pos_ids),
            "denominator_count": len(eligible_files),
            "percentage": len(pos_ids) / len(eligible_files) * 100.0,
            "entity_level": "file",
            "denominator_basis": denominator_basis,
            "supporting_evidence_ids": pos_ids,
        }
        return _answer(Status.SUPPORTED, Reason.NONE, result, pos_ids, pos_ids)

    # entity_level == "patient" (or case/aliquot/assay aggregated on patient semantics if requested)
    eligible_patients: set[str] = set()
    positive_patients: set[str] = set()
    positive_assays: set[str] = set()
    eligible_assays: set[str] = set()
    has_incomplete_assay = False

    for pid in sorted(patient_ids):
        elig_cases = [cid for cid in canonical_cases_by_patient.get(pid, []) if cid in clinically_eligible_cases]
        if not elig_cases:
            continue
        patient_target_assays: set[str] = set()
        patient_elig_assays: set[str] = set()
        for cid in elig_cases:
            for aid in _case_linked_assays(adj, entities, cid):
                if _matches_criteria(entities[aid], target_assay_scope):
                    patient_target_assays.add(aid)
                    if _assay_passes_elig(aid):
                        patient_elig_assays.add(aid)
        if not patient_elig_assays:
            if any(
                entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
                or entities[aid]["attributes"].get("result") is None
                for aid in patient_target_assays
            ):
                has_incomplete_assay = True
            continue
        eligible_patients.add(pid)
        eligible_assays.update(patient_elig_assays)
        patient_pos_assays = {
            aid for aid in patient_elig_assays if _matches_criteria(entities[aid], assay_pos_criteria)
        }
        if patient_pos_assays:
            positive_patients.add(pid)
            positive_assays.update(patient_pos_assays)

    if has_incomplete_assay:
        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
    if not eligible_patients:
        return _answer(
            Status.UNKNOWN,
            Reason.ZERO_DENOMINATOR,
            {
                "numerator": 0,
                "denominator": 0,
                "numerator_count": 0,
                "denominator_count": 0,
                "entity_level": "patient",
                "denominator_basis": denominator_basis,
            },
            [],
            [],
        )

    eligible_patient_ids = sorted(entities[p]["source_id"] for p in eligible_patients)
    positive_patient_ids = sorted(entities[p]["source_id"] for p in positive_patients)
    positive_assay_ids = sorted(entities[a]["source_id"] for a in positive_assays)
    eligible_assay_ids = sorted(entities[a]["source_id"] for a in eligible_assays)

    if not positive_patient_ids:
        # True 0 / N empty subcohort proved by complete negative denominator assays.
        supporting_evidence = eligible_assay_ids
    elif has_dup_markers or denominator_basis == "distinct_canonical_patients_in_snapshot":
        # Patient deduplication queries cite distinct canonical patient witnesses.
        supporting_evidence = positive_patient_ids
    else:
        supporting_evidence = sorted(set(positive_patient_ids) | set(positive_assay_ids))

    result = {
        "numerator": len(positive_patient_ids),
        "denominator": len(eligible_patient_ids),
        "numerator_count": len(positive_patient_ids),
        "denominator_count": len(eligible_patient_ids),
        "percentage": len(positive_patient_ids) / len(eligible_patient_ids) * 100.0,
        "entity_level": "patient",
        "denominator_basis": denominator_basis,
        "eligible_patient_ids": eligible_patient_ids,
        "supporting_evidence_ids": supporting_evidence,
    }
    return _answer(Status.SUPPORTED, Reason.NONE, result, positive_patient_ids, supporting_evidence)


def _molecular_assay_maps(
    edges: list[tuple[str, str, str, str]],
    entities: dict[str, dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    assay_to_aliquots: dict[str, set[str]] = {}
    aliquot_to_samples: dict[str, set[str]] = {}
    assay_to_direct_samples: dict[str, set[str]] = {}
    for subject, predicate, object_id, _ in edges:
        sub_kind = entities.get(subject, {}).get("kind")
        obj_kind = entities.get(object_id, {}).get("kind")
        if predicate == "HAS_ASSAY" and obj_kind == "assay":
            if sub_kind == "aliquot":
                assay_to_aliquots.setdefault(object_id, set()).add(subject)
            elif sub_kind == "sample":
                assay_to_direct_samples.setdefault(object_id, set()).add(subject)
        elif predicate == "HAS_ALIQUOT" and sub_kind == "sample" and obj_kind == "aliquot":
            aliquot_to_samples.setdefault(object_id, set()).add(subject)
    assay_to_samples: dict[str, set[str]] = {}
    for eid, row in entities.items():
        if row["kind"] != "assay":
            continue
        samples_set = set(assay_to_direct_samples.get(eid, set()))
        for alqid in assay_to_aliquots.get(eid, set()):
            samples_set.update(aliquot_to_samples.get(alqid, set()))
        assay_to_samples[eid] = samples_set
    return assay_to_aliquots, aliquot_to_samples, assay_to_samples


def _evaluate_molecular_cohort_count(
    connection: Any,
    manifest: dict[str, Any],
    query: dict[str, Any],
    clinical_criteria: dict[str, Any],
    sample_criteria: dict[str, Any],
    file_criteria: dict[str, Any],
    molecular_criteria: dict[str, Any],
) -> dict[str, Any]:
    entity_level = query.get("entity_level")
    if entity_level != "sample":
        return _answer(Status.ERROR, Reason.UNSUPPORTED_ENTITY_LEVEL, {}, [], [])

    denominator_basis = query["denominator_basis"]
    prov = manifest.get("provenance_metadata") or {}
    named_universes = prov.get("named_universes")
    if isinstance(named_universes, dict) and named_universes and denominator_basis not in named_universes:
        return _answer(Status.UNKNOWN, Reason.COHORT_OR_FIXTURE_MISMATCH, {}, [], [])

    mol_cov = prov.get("molecular_coverage") or {}
    if isinstance(mol_cov.get("acquired_gene_symbols"), list):
        if "gene_symbol" in molecular_criteria:
            req_genes = [molecular_criteria["gene_symbol"]]
        elif "gene_symbol_in" in molecular_criteria:
            req_genes = list(molecular_criteria["gene_symbol_in"])
        else:
            req_genes = []
        if not req_genes or any(g not in mol_cov["acquired_gene_symbols"] for g in req_genes):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    entities = _entities(connection)
    edges = _edges(connection)
    adj = _edges_adj(edges)
    source_filter = clinical_criteria.get("source")
    target_sources = (
        {source_filter}
        if source_filter is not None
        else ({row["source"] for row in entities.values() if row["kind"] == "case"} or set(manifest.get("coverage", {}).keys()))
    )
    if target_sources and not _coverage_ok(manifest, target_sources):
        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    gene_scope_keys = {"gene_symbol", "gene_symbol_in", "gene_id", "ncbi_build", "canonical_transcript_id"}
    gene_scope_criteria = {k: v for k, v in molecular_criteria.items() if k in gene_scope_keys}
    variant_locator_criteria = {
        k: v
        for k, v in molecular_criteria.items()
        if k not in {"call_status", "call_status_in", "require_explicit_negative_coverage"}
    }
    variant_match_criteria = dict(variant_locator_criteria)
    if "call_status" in molecular_criteria:
        variant_match_criteria["call_status"] = molecular_criteria["call_status"]
    elif "call_status_in" in molecular_criteria:
        variant_match_criteria["call_status_in"] = molecular_criteria["call_status_in"]
    else:
        variant_match_criteria["call_status"] = "POSITIVE"

    require_neg_cov = bool(molecular_criteria.get("require_explicit_negative_coverage"))
    wants_negative_calls = (
        molecular_criteria.get("call_status") == "NEGATIVE"
        or "NEGATIVE" in (molecular_criteria.get("call_status_in") or [])
    )

    assay_to_aliquots, _, assay_to_samples = _molecular_assay_maps(edges, entities)
    gene_scope_assays = {
        eid
        for eid, row in entities.items()
        if row["kind"] == "assay"
        and (source_filter is None or row["source"] == source_filter)
        and (gene_scope_criteria or "gene_symbol" in row["attributes"] or "linkage_status" in row["attributes"])
        and _matches_criteria(row, gene_scope_criteria)
    }

    bad_linkage_eids: set[str] = set()
    for aid in gene_scope_assays:
        lstat = entities[aid]["attributes"].get("linkage_status")
        linked_smps = assay_to_samples.get(aid, set())
        linked_alqs = assay_to_aliquots.get(aid, set())
        if lstat in {"UNMATCHED", "MULTIPLY_MATCHED"} or len(linked_smps) != 1 or len(linked_alqs) > 1:
            bad_linkage_eids.add(aid)
            bad_linkage_eids.update(linked_smps)
            bad_linkage_eids.update(linked_alqs)
    if bad_linkage_eids:
        bad_refs = sorted(entities[e]["source_id"] for e in bad_linkage_eids if e in entities)
        return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, bad_refs, bad_refs)

    all_canonical_cases = {
        eid
        for eid, row in entities.items()
        if row["kind"] == "case"
        and (source_filter is None or row["source"] == source_filter)
        and row["attributes"].get("is_duplicate_case_entry") is not True
    }
    clinically_eligible_cases = {
        cid for cid in all_canonical_cases if _matches_criteria(entities[cid], clinical_criteria)
    }

    sample_elig_criteria = {
        k: v for k, v in sample_criteria.items() if k not in {"min_tumor_purity", "max_collection_timestamp"}
    }
    sample_cutoff_criteria = {
        k: v for k, v in sample_criteria.items() if k in {"min_tumor_purity", "max_collection_timestamp"}
    }

    candidate_samples: set[str] = set()
    for cid in clinically_eligible_cases:
        for sid in _linked_descendants(adj, entities, cid, "sample", {"HAS_SAMPLE"}):
            if _matches_criteria(entities[sid], sample_elig_criteria):
                if "min_tumor_purity" in sample_cutoff_criteria and entities[sid]["attributes"].get("tumor_purity") is None:
                    return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
                if _matches_criteria(entities[sid], sample_cutoff_criteria):
                    candidate_samples.add(sid)

    eligible_samples: set[str] = set()
    if file_criteria:
        for sid in candidate_samples:
            linked_files = _linked_descendants(adj, entities, sid, "file", {"HAS_ALIQUOT", "HAS_FILE"})
            if any(_matches_criteria(entities[fid], file_criteria) for fid in linked_files):
                eligible_samples.add(sid)
    else:
        for sid in candidate_samples:
            smp_gene_assays = {
                aid
                for aid in _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"})
                if aid in gene_scope_assays
            }
            if any(
                entities[aid]["attributes"].get("call_status") in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
                or entities[aid]["attributes"].get("coverage_completeness") in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
                or entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
                or entities[aid]["attributes"].get("qc_status") in {"FAIL", "QC_FAILED"}
                for aid in smp_gene_assays
            ):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
            if any(
                entities[aid]["attributes"].get("call_status") in {"POSITIVE", "NEGATIVE"}
                for aid in smp_gene_assays
            ):
                eligible_samples.add(sid)

    if not eligible_samples:
        return _answer(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {}, [], [])

    conflicted = _conflict_entities(connection, clinically_eligible_cases | eligible_samples | gene_scope_assays)
    if conflicted:
        return _answer(
            Status.CONFLICT,
            Reason.CONFLICTING_EVIDENCE,
            {},
            sorted(entities[e]["source_id"] for e in conflicted if e in entities),
            _evidence_for(connection, conflicted),
        )

    call_conflict_samples: set[str] = set()
    call_conflict_assays: set[str] = set()
    for sid in sorted(eligible_samples):
        smp_gene_assays = {
            aid
            for aid in _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"})
            if aid in gene_scope_assays
        }
        if any(
            entities[aid]["attributes"].get("call_status") in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
            or entities[aid]["attributes"].get("coverage_completeness") in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
            or entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
            or entities[aid]["attributes"].get("qc_status") in {"FAIL", "QC_FAILED"}
            for aid in smp_gene_assays
        ):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        smp_loc_assays = {aid for aid in smp_gene_assays if _matches_criteria(entities[aid], variant_locator_criteria)}
        loc_statuses = {entities[aid]["attributes"].get("call_status") for aid in smp_loc_assays}
        if (
            "CONFLICT" in loc_statuses
            or any(entities[aid]["attributes"].get("adjudication_status") == "UNRESOLVED" for aid in smp_loc_assays)
            or ("POSITIVE" in loc_statuses and "NEGATIVE" in loc_statuses)
        ):
            call_conflict_samples.add(sid)
            call_conflict_assays.update(smp_loc_assays)

    if call_conflict_samples:
        conflict_smps = sorted(entities[s]["source_id"] for s in call_conflict_samples)
        conflict_refs = sorted(
            {entities[s]["source_id"] for s in call_conflict_samples}
            | {entities[a]["source_id"] for a in call_conflict_assays}
        )
        return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, conflict_smps, conflict_refs)

    if wants_negative_calls or require_neg_cov:
        if (
            mol_cov.get("explicit_negative_calls_present") is False
            or mol_cov.get("locus_level_callable_coverage_present") is False
        ):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        for sid in sorted(eligible_samples):
            smp_loc_assays = {
                aid
                for aid in _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"})
                if aid in gene_scope_assays and _matches_criteria(entities[aid], variant_locator_criteria)
            }
            if not any(
                entities[aid]["attributes"].get("call_status") == "POSITIVE"
                or (
                    entities[aid]["attributes"].get("call_status") == "NEGATIVE"
                    and entities[aid]["attributes"].get("coverage_completeness") == "COMPLETE_LOCUS_COVERAGE"
                )
                for aid in smp_loc_assays
            ):
                return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    matching_samples: set[str] = set()
    matching_assays: set[str] = set()
    matching_aliquots: set[str] = set()
    matching_files: set[str] = set()
    for sid in sorted(eligible_samples):
        smp_match_assays = {
            aid
            for aid in _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"})
            if aid in gene_scope_assays and _matches_criteria(entities[aid], variant_match_criteria)
        }
        if smp_match_assays:
            matching_samples.add(sid)
            matching_assays.update(smp_match_assays)
            for aid in smp_match_assays:
                matching_aliquots.update(assay_to_aliquots.get(aid, set()))
                for fid in _linked_descendants(adj, entities, aid, "file", {"HAS_FILE"}):
                    if not file_criteria or _matches_criteria(entities[fid], file_criteria):
                        matching_files.add(fid)

    pos_sample_ids = sorted(entities[s]["source_id"] for s in matching_samples)
    pos_assay_ids = sorted(entities[a]["source_id"] for a in matching_assays)
    pos_aliquot_ids = sorted(entities[alq]["source_id"] for alq in matching_aliquots)
    pos_file_ids = sorted(entities[f]["source_id"] for f in matching_files)
    all_lineage_refs = sorted(
        set(pos_sample_ids) | set(pos_assay_ids) | set(pos_aliquot_ids) | set(pos_file_ids)
    )
    result = {
        "numerator": len(pos_sample_ids),
        "denominator": len(eligible_samples),
        "numerator_count": len(pos_sample_ids),
        "denominator_count": len(eligible_samples),
        "percentage": len(pos_sample_ids) / len(eligible_samples) * 100.0,
        "entity_level": "sample",
        "denominator_basis": denominator_basis,
        "supporting_evidence_ids": pos_sample_ids,
        "supporting_variant_record_ids": pos_assay_ids,
        "supporting_aliquot_ids": pos_aliquot_ids,
        "supporting_file_ids": pos_file_ids,
    }
    return _answer(Status.SUPPORTED, Reason.NONE, result, pos_sample_ids, all_lineage_refs)


def _evaluate_claim_verification(connection: Any, manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    predicate = query.get("predicate") or {}
    if not isinstance(predicate, dict) or not predicate:
        return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])

    entities = _entities(connection)
    edges = _edges(connection)
    subject_ref = query.get("subject_ref") or {}

    if subject_ref.get("sample_id") and "gene_symbol" in predicate:
        if _partial_tables_present(manifest, ("cases", "samples", "assays")):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        prov = manifest.get("provenance_metadata") or {}
        mol_cov = prov.get("molecular_coverage") or {}
        if isinstance(mol_cov.get("acquired_gene_symbols"), list) and predicate["gene_symbol"] not in mol_cov["acquired_gene_symbols"]:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

        sample_id = subject_ref["sample_id"]
        source_filter = predicate.get("source")
        matches = [
            eid
            for eid, row in entities.items()
            if row["kind"] == "sample"
            and row["source_id"] == sample_id
            and (source_filter is None or row["source"] == source_filter)
        ]
        if not matches:
            return _answer(Status.UNKNOWN, Reason.INSUFFICIENT_EVIDENCE, {}, [], [])
        if len(matches) > 1:
            return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SUBJECT, {}, [], [])
        sid = matches[0]
        expected_call = predicate.get("expected_call", "POSITIVE")
        if expected_call not in {"POSITIVE", "NEGATIVE"}:
            return _answer(Status.ERROR, Reason.INVALID_QUERY, {}, [], [])

        allowed_claim_keys = (COHORT_MOLECULAR_FIELDS - {"call_status", "call_status_in", "require_explicit_negative_coverage"}) | {"expected_call", "source"}
        if set(predicate) - allowed_claim_keys:
            return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])

        gene_scope_keys = {"gene_symbol", "gene_symbol_in", "gene_id", "ncbi_build", "canonical_transcript_id"}
        gene_scope_criteria = {k: v for k, v in predicate.items() if k in gene_scope_keys}
        locator_criteria = {
            k: v
            for k, v in predicate.items()
            if k in (COHORT_MOLECULAR_FIELDS - {"call_status", "call_status_in", "require_explicit_negative_coverage"})
        }

        adj = _edges_adj(edges)
        assay_to_aliquots, _, assay_to_samples = _molecular_assay_maps(edges, entities)
        gene_scope_assays = {
            eid
            for eid, row in entities.items()
            if row["kind"] == "assay"
            and (source_filter is None or row["source"] == source_filter)
            and _matches_criteria(row, gene_scope_criteria)
        }
        bad_eids: set[str] = set()
        for aid in gene_scope_assays:
            lstat = entities[aid]["attributes"].get("linkage_status")
            linked_smps = assay_to_samples.get(aid, set())
            linked_alqs = assay_to_aliquots.get(aid, set())
            if (lstat in {"UNMATCHED", "MULTIPLY_MATCHED"} or len(linked_smps) != 1 or len(linked_alqs) > 1) and (
                sid in linked_smps or not linked_smps or lstat in {"UNMATCHED", "MULTIPLY_MATCHED"}
            ):
                bad_eids.add(aid)
                bad_eids.update(linked_smps)
                bad_eids.update(linked_alqs)
        if bad_eids:
            bad_refs = sorted(entities[e]["source_id"] for e in bad_eids if e in entities)
            return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, bad_refs, bad_refs)

        smp_gene_assays = {
            aid for aid in _linked_descendants(adj, entities, sid, "assay", {"HAS_ALIQUOT", "HAS_ASSAY"}) if aid in gene_scope_assays
        }
        if any(
            entities[aid]["attributes"].get("call_status") in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
            or entities[aid]["attributes"].get("coverage_completeness") in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
            or entities[aid]["attributes"].get("assay_status") in INCOMPLETE_ASSAY_STATUSES
            or entities[aid]["attributes"].get("qc_status") in {"FAIL", "QC_FAILED"}
            for aid in smp_gene_assays
        ):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

        smp_loc_assays = {aid for aid in smp_gene_assays if _matches_criteria(entities[aid], locator_criteria)}
        conflicted = _conflict_entities(connection, {sid} | smp_loc_assays)
        if conflicted:
            return _answer(
                Status.CONFLICT,
                Reason.CONFLICTING_EVIDENCE,
                {},
                sorted(entities[e]["source_id"] for e in conflicted if e in entities),
                _evidence_for(connection, conflicted),
            )
        loc_statuses = {entities[aid]["attributes"].get("call_status") for aid in smp_loc_assays}
        if (
            "CONFLICT" in loc_statuses
            or any(entities[aid]["attributes"].get("adjudication_status") == "UNRESOLVED" for aid in smp_loc_assays)
            or ("POSITIVE" in loc_statuses and "NEGATIVE" in loc_statuses)
        ):
            refs = sorted({entities[sid]["source_id"]} | {entities[a]["source_id"] for a in smp_loc_assays})
            return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, [entities[sid]["source_id"]], refs)

        pos_assays = {aid for aid in smp_loc_assays if entities[aid]["attributes"].get("call_status") == "POSITIVE"}
        neg_complete_assays = {
            aid
            for aid in smp_loc_assays
            if entities[aid]["attributes"].get("call_status") == "NEGATIVE"
            and entities[aid]["attributes"].get("coverage_completeness") == "COMPLETE_LOCUS_COVERAGE"
        }
        if not pos_assays and not neg_complete_assays:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

        active_assays = pos_assays if pos_assays else neg_complete_assays
        linked_alqs = {alq for a in active_assays for alq in assay_to_aliquots.get(a, set())}
        linked_files = {fid for a in active_assays for fid in _linked_descendants(adj, entities, a, "file", {"HAS_FILE"})}
        ev_ids = sorted(
            {entities[sid]["source_id"]}
            | {entities[a]["source_id"] for a in active_assays}
            | {entities[alq]["source_id"] for alq in linked_alqs}
            | {entities[f]["source_id"] for f in linked_files}
        )
        observed_call = "POSITIVE" if pos_assays else "NEGATIVE"
        if observed_call == expected_call:
            return _answer(
                Status.SUPPORTED,
                Reason.NONE,
                {
                    "claim_holds": True,
                    "entity_level": query.get("entity_level"),
                    "denominator_basis": query.get("denominator_basis"),
                    "numerator_count": None,
                    "denominator_count": None,
                    "supporting_evidence_ids": ev_ids,
                    "contradicting_evidence_ids": [],
                },
                ev_ids,
                ev_ids,
            )
        return _answer(
            Status.CONTRADICTED,
            Reason.CLAIM_CONTRADICTED,
            {
                "claim_holds": False,
                "entity_level": query.get("entity_level"),
                "denominator_basis": query.get("denominator_basis"),
                "numerator_count": None,
                "denominator_count": None,
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": ev_ids,
            },
            [],
            ev_ids,
        )

    if _partial_tables_present(manifest, ("assays", "pathology_reports")):
        return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])

    # 1. Subcohort prevalence over receptor/subgroup predicate (0/0 must return UNKNOWN)
    if "claimed_prevalence" in predicate:
        er_req = predicate.get("subcohort_er")
        pr_req = predicate.get("subcohort_pr")
        her2_req = predicate.get("subcohort_her2")
        subcohort_patients = [
            row
            for row in entities.values()
            if row["kind"] == "assay"
            and row["attributes"].get("assay_status") == "COMPLETED"
            and row["attributes"].get("qc_status") == "PASS"
            and (er_req is None or row["attributes"].get("er_status") == er_req)
            and (pr_req is None or row["attributes"].get("pr_status") == pr_req)
            and (her2_req is None or row["attributes"].get("her2_status") == her2_req)
        ]
        if not subcohort_patients:
            return _answer(Status.UNKNOWN, Reason.ZERO_DENOMINATOR, {}, [], [])

    # 2. Single-subject pathology stage claim (with supersession chain & conflict check)
    if subject_ref.get("patient_id") and (predicate.get("field") == "stage" or "required_stage" in predicate):
        pid = subject_ref["patient_id"]
        reports = [r for r in _pathology_reports(connection) if r.get("patient_id") == pid]
        if not reports:
            return _answer(Status.UNKNOWN, Reason.INSUFFICIENT_EVIDENCE, {}, [], [])
        active_reports = [
            r for r in reports
            if r.get("record_status") == "ACTIVE_FINAL" and not r.get("superseded_by")
        ]
        superseded_reports = [r for r in reports if r.get("record_status") == "SUPERSEDED"]
        if not active_reports:
            return _answer(Status.UNKNOWN, Reason.INSUFFICIENT_EVIDENCE, {}, [], [])
        distinct_stages = {r.get("stage") for r in active_reports}
        if len(distinct_stages) > 1:
            return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, [], [])

        active = active_reports[0]
        expected_stage = predicate.get("expected_value") if "expected_value" in predicate else predicate.get("required_stage")
        if active.get("stage") == expected_stage:
            supp = sorted([active["report_id"]])
            return _answer(
                Status.SUPPORTED,
                Reason.NONE,
                {
                    "claim_holds": True,
                    "entity_level": query.get("entity_level"),
                    "denominator_basis": query.get("denominator_basis"),
                    "numerator_count": None,
                    "denominator_count": None,
                    "supporting_evidence_ids": supp,
                    "contradicting_evidence_ids": [],
                },
                supp,
                supp,
            )
        contra = sorted([active["report_id"]] + [r["report_id"] for r in superseded_reports])
        return _answer(
            Status.CONTRADICTED,
            Reason.CLAIM_CONTRADICTED,
            {
                "claim_holds": False,
                "entity_level": query.get("entity_level"),
                "denominator_basis": query.get("denominator_basis"),
                "numerator_count": None,
                "denominator_count": None,
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": contra,
            },
            [],
            contra,
        )

    # 3. Single-subject biomarker claim
    if subject_ref.get("patient_id") and "biomarker" in predicate:
        pid = subject_ref["patient_id"]
        biomarker = predicate["biomarker"]
        expected_result = predicate.get("expected_result")
        patient_assays = [
            row
            for row in entities.values()
            if row["kind"] == "assay"
            and row["attributes"].get("patient_id") == pid
            and row["attributes"].get("biomarker") == biomarker
        ]
        valid_assays = [
            row
            for row in patient_assays
            if row["attributes"].get("assay_status") == "COMPLETED"
            and row["attributes"].get("qc_status") == "PASS"
            and row["attributes"].get("result") is not None
        ]
        if not valid_assays:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        if any(row["attributes"].get("adjudication_status") == "UNRESOLVED" for row in valid_assays) or (
            len({row["attributes"].get("result") for row in valid_assays}) > 1
        ):
            return _answer(Status.CONFLICT, Reason.CONFLICTING_EVIDENCE, {}, [], [])

        linked_file_ids: set[str] = set()
        for row in valid_assays:
            linked_file_ids.update(_linked_descendants(edges, entities, row["entity_id"], "file", {"HAS_FILE"}))
        ev_ids = sorted({row["source_id"] for row in valid_assays} | {entities[f]["source_id"] for f in linked_file_ids})
        observed = valid_assays[0]["attributes"].get("result")
        if observed == expected_result:
            return _answer(
                Status.SUPPORTED,
                Reason.NONE,
                {
                    "claim_holds": True,
                    "entity_level": query.get("entity_level"),
                    "denominator_basis": query.get("denominator_basis"),
                    "numerator_count": None,
                    "denominator_count": None,
                    "supporting_evidence_ids": ev_ids,
                    "contradicting_evidence_ids": [],
                },
                ev_ids,
                ev_ids,
            )
        return _answer(
            Status.CONTRADICTED,
            Reason.CLAIM_CONTRADICTED,
            {
                "claim_holds": False,
                "entity_level": query.get("entity_level"),
                "denominator_basis": query.get("denominator_basis"),
                "numerator_count": None,
                "denominator_count": None,
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": ev_ids,
            },
            [],
            ev_ids,
        )

    # 4. Cohort-wide universal zero / negative count claim
    if "claim_numerator_count" in predicate or "claimed_negative_count" in predicate:
        biomarker = predicate.get("biomarker")
        patient_entities = [row for row in entities.values() if row["kind"] == "patient"]
        biomarker_assays = [
            row
            for row in entities.values()
            if row["kind"] == "assay" and row["attributes"].get("biomarker") == biomarker
        ]
        positive_assays = [
            row
            for row in biomarker_assays
            if row["attributes"].get("assay_status") == "COMPLETED"
            and row["attributes"].get("qc_status") == "PASS"
            and row["attributes"].get("result") not in (None, "WT_NEGATIVE", "NEGATIVE")
        ]
        if positive_assays:
            contra_ids = sorted(
                {row["attributes"].get("patient_id") for row in positive_assays if row["attributes"].get("patient_id")}
                | {row["source_id"] for row in positive_assays}
            )
            return _answer(
                Status.CONTRADICTED,
                Reason.CLAIM_CONTRADICTED,
                {
                    "claim_holds": False,
                    "entity_level": None,
                    "denominator_basis": None,
                    "numerator_count": None,
                    "denominator_count": None,
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": contra_ids,
                },
                [],
                contra_ids,
            )
        tested_pids = {
            row["attributes"].get("patient_id")
            for row in biomarker_assays
            if row["attributes"].get("assay_status") == "COMPLETED"
            and row["attributes"].get("qc_status") == "PASS"
            and row["attributes"].get("result") is not None
        }
        all_pids = {row["source_id"] for row in patient_entities}
        if not all_pids or tested_pids != all_pids:
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        supp_ids = sorted(row["source_id"] for row in biomarker_assays)
        return _answer(
            Status.SUPPORTED,
            Reason.NONE,
            {
                "claim_holds": True,
                "entity_level": query.get("entity_level"),
                "denominator_basis": query.get("denominator_basis"),
                "numerator_count": 0,
                "denominator_count": len(all_pids),
                "supporting_evidence_ids": supp_ids,
                "contradicting_evidence_ids": [],
            },
            supp_ids,
            supp_ids,
        )

    return _answer(Status.OUT_OF_SCOPE, Reason.INVALID_QUERY, {}, [], [])


def _evaluate_cross_source_overlap(connection: Any, manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    requested_sources = query.get("requested_sources")
    entity_level = query.get("entity_level")
    denominator_basis = query.get("denominator_basis")
    if not isinstance(requested_sources, list) or len(requested_sources) < 2:
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])
    if entity_level is None or not isinstance(denominator_basis, str) or not denominator_basis.strip():
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SELECTOR, {}, [], [])

    src_set = set(requested_sources)
    mappings = [
        m for m in _identity_mappings(connection)
        if {m.get("source_a"), m.get("source_b")} == src_set
    ]
    if not mappings:
        return _answer(Status.UNKNOWN, Reason.NO_APPROVED_MAPPING, {}, [], [])
    if any(
        m.get("resolution") == "AMBIGUOUS_ONE_TO_MANY"
        or m.get("cardinality") == "ONE_TO_MANY"
        or m.get("approval_status") == "APPROVED_CANDIDATE_SET"
        for m in mappings
    ):
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_MAPPING, {}, [], [])

    approved = [m for m in mappings if m.get("approval_status") == "APPROVED"]
    if not approved:
        return _answer(Status.UNKNOWN, Reason.NO_APPROVED_MAPPING, {}, [], [])

    a_to_b: dict[str, set[str]] = {}
    b_to_a: dict[str, set[str]] = {}
    for m in approved:
        sa, sb = m.get("source_a_id"), m.get("source_b_id")
        if sa and sb:
            a_to_b.setdefault(sa, set()).add(sb)
            b_to_a.setdefault(sb, set()).add(sa)
    if any(len(v) > 1 for v in a_to_b.values()) or any(len(v) > 1 for v in b_to_a.values()):
        return _answer(Status.CONFLICT, Reason.CONFLICTING_MAPPING, {}, [], [])

    entities = _entities(connection)
    patients_in_sources = [
        row for row in entities.values() if row["kind"] == "patient" and row["source"] in src_set
    ]
    # Union-Find over cross-source patients linked by approved 1:1 mappings
    parent = {row["source_id"]: row["source_id"] for row in patients_in_sources}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for m in approved:
        sa, sb = m.get("source_a_id"), m.get("source_b_id")
        if sa in parent and sb in parent:
            ra, rb = find(sa), find(sb)
            if ra != rb:
                parent[rb] = ra

    union_count = len({find(pid) for pid in parent})
    overlap_pairs = len(approved)
    supp_ids = sorted([m["mapping_id"] for m in approved] + [row["source_id"] for row in patients_in_sources])
    result = {
        "numerator": overlap_pairs,
        "denominator": union_count,
        "numerator_count": overlap_pairs,
        "denominator_count": union_count,
        "entity_level": entity_level,
        "denominator_basis": denominator_basis,
        "supporting_evidence_ids": supp_ids,
    }
    return _answer(Status.SUPPORTED, Reason.NONE, result, supp_ids, supp_ids)


def _evaluate_entity_attribute_query(connection: Any, manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    subject_ref = query.get("subject_ref") or {}
    if not isinstance(subject_ref, dict) or not subject_ref:
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SUBJECT, {}, [], [])

    entities = _entities(connection)
    patients = [row for row in entities.values() if row["kind"] == "patient"]
    if "legacy_subject_alias" in subject_ref:
        alias = subject_ref["legacy_subject_alias"]
        matches = [row for row in patients if row["attributes"].get("legacy_subject_alias") == alias]
    elif "patient_id" in subject_ref:
        pid = subject_ref["patient_id"]
        matches = [row for row in patients if row["source_id"] == pid]
    else:
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SUBJECT, {}, [], [])

    if not matches:
        return _answer(Status.UNKNOWN, Reason.INSUFFICIENT_EVIDENCE, {}, [], [])
    if len(matches) > 1:
        return _answer(Status.AMBIGUOUS, Reason.AMBIGUOUS_SUBJECT, {}, [], [])

    patient = matches[0]
    supp = [patient["source_id"]]
    return _answer(
        Status.SUPPORTED,
        Reason.NONE,
        {"entity_level": "patient", "patient_id": patient["source_id"], "supporting_evidence_ids": supp},
        supp,
        supp,
    )


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
        operation = query.get("operation") or query.get("query_type")
        if operation not in ALL_OPERATIONS:
            return _answer(Status.OUT_OF_SCOPE, Reason.UNSUPPORTED_OPERATION, {}, [], [])
        if query.get("snapshot_id") is not None and query.get("snapshot_id") != manifest.get("snapshot_id"):
            return _answer(Status.UNKNOWN, Reason.SNAPSHOT_MISMATCH, {}, [], [])
        if query.get("fixture_id") is not None and query.get("fixture_id") != manifest.get("fixture_id"):
            return _answer(Status.UNKNOWN, Reason.COHORT_OR_FIXTURE_MISMATCH, {}, [], [])
        if query.get("cohort_id") is not None and query.get("cohort_id") != manifest.get("cohort_id"):
            return _answer(Status.UNKNOWN, Reason.COHORT_OR_FIXTURE_MISMATCH, {}, [], [])
        prov_err = _check_provenance_and_ontology(manifest, query)
        if prov_err is not None:
            return prov_err
        if not manifest.get("complete"):
            return _answer(Status.UNKNOWN, Reason.INCOMPLETE_SNAPSHOT, {}, [], [])
        if operation == "out_of_scope_request":
            return _answer(Status.OUT_OF_SCOPE, Reason.OUT_OF_SCOPE_QUERY, {}, [], [])
        if operation == "cohort_count":
            return _evaluate_cohort_count(connection, manifest, query)
        if operation == "claim_verification":
            return _evaluate_claim_verification(connection, manifest, query)
        if operation == "cross_source_overlap_count":
            return _evaluate_cross_source_overlap(connection, manifest, query)
        if operation == "entity_attribute_query":
            return _evaluate_entity_attribute_query(connection, manifest, query)
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

