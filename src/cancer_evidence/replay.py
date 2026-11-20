from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import Reason, Status
from .signing import DEFAULT_PUBLIC_KEY, verify
from .snapshot import canonical_json, derive_normalized, read_snapshot, sha256_bytes

RULE_VERSION = "rules-2.0.0"
PUBLISHED_PUBLIC_KEY = Path(__file__).resolve().parents[2] / "keys" / "public_key.pem"
ENTITY_KINDS = {"case", "sample", "aliquot", "assay", "file"}
ALL_SUPPORTED_LEVELS = ENTITY_KINDS | {"patient"}
SELECTOR_FIELDS = {"entity_level", "source", "project_id", "study_id", "assay_type", "data_type"}
OPERATIONS = {
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
ALLOWED_JSON_ATTRS = {
    "project_id",
    "study_id",
    "disease_type",
    "primary_site",
    "histology",
    "stage",
    "biomarker",
    "panel_name",
    "assay_status",
    "qc_status",
    "result",
    "er_status",
    "pr_status",
    "her2_status",
    "sample_type",
    "tissue_type",
    "tumor_descriptor",
    "preservation_method",
    "file_type",
    "data_category",
    "data_type",
    "experimental_strategy",
    "data_format",
    "access",
    "state",
    "gene_symbol",
    "gene_id",
    "ncbi_build",
    "canonical_transcript_id",
    "aa_change",
    "hgvsp_short",
    "hgvsc",
    "consequence_type",
    "genomic_dna_change",
    "variant_process",
    "mutation_status",
    "call_status",
}


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
    if not entity_ids:
        return set()
    rows = connection.execute(
        "SELECT entity_id FROM evidence GROUP BY entity_id, field_name HAVING COUNT(DISTINCT canonical_value_json) > 1"
    ).fetchall()
    return {row[0] for row in rows} & entity_ids


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


def _parse_iso_dt(value: str) -> datetime | None:
    try:
        norm = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(norm)
    except ValueError:
        return None


def _has_sql_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _check_provenance_and_ontology_sql(manifest: dict[str, Any], query: dict[str, Any]) -> dict[str, Any] | None:
    prov = manifest.get("provenance_metadata")
    if not isinstance(prov, dict):
        return None
    if prov.get("integrity_errors"):
        reason = prov["integrity_errors"][0].get("reason", Reason.PROVENANCE_ERROR.value)
        valid_reasons = {r.value for r in Reason}
        return _answer(Status.ERROR.value, reason if reason in valid_reasons else Reason.PROVENANCE_ERROR.value, {}, [], [])
    entity_level = query.get("entity_level")
    if entity_level is not None:
        supported_levels = prov.get("supported_entity_levels")
        if not isinstance(entity_level, str):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        if (isinstance(supported_levels, list) and entity_level not in supported_levels) or (
            entity_level not in ALL_SUPPORTED_LEVELS
        ):
            return _answer(Status.ERROR.value, Reason.UNSUPPORTED_ENTITY_LEVEL.value, {}, [], [])
    return None


def _partial_tables_present_sql(manifest: dict[str, Any], table_names: tuple[str, ...]) -> bool:
    prov = manifest.get("provenance_metadata")
    if not isinstance(prov, dict):
        return False
    tables_meta = prov.get("tables") or {}
    return any(
        isinstance(tables_meta.get(name), dict)
        and tables_meta[name].get("declared_completeness") == "PARTIAL_INGEST"
        for name in table_names
    )


def _sql_criteria_clauses(table_alias: str, criteria: dict[str, Any]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for key in sorted(criteria.keys()):
        value = criteria[key]
        if key == "source":
            clauses.append(f"{table_alias}.source = ?")
            params.append(value)
        elif key == "positive_results_in":
            placeholders = ",".join("?" for _ in value)
            clauses.append(f"json_extract({table_alias}.attributes_json, '$.result') IN ({placeholders})")
            params.extend(value)
        elif key == "min_tumor_purity":
            clauses.append(f"CAST(json_extract({table_alias}.attributes_json, '$.tumor_purity') AS REAL) >= ?")
            params.append(float(value))
        elif key == "max_collection_timestamp":
            # Applied after ISO normalization or SQLite julianday when exact ISO format matches
            clauses.append(f"json_extract({table_alias}.attributes_json, '$.collection_timestamp') IS NOT NULL")
        elif key.endswith("_in"):
            attr_name = key[:-3]
            if attr_name not in ALLOWED_JSON_ATTRS:
                raise ValueError(f"disallowed SQL JSON field: {attr_name}")
            placeholders = ",".join("?" for _ in value)
            clauses.append(f"json_extract({table_alias}.attributes_json, '$.{attr_name}') IN ({placeholders})")
            params.extend(value)
        else:
            if key not in ALLOWED_JSON_ATTRS:
                raise ValueError(f"disallowed SQL JSON field: {key}")
            if value is None:
                clauses.append(f"json_extract({table_alias}.attributes_json, '$.{key}') IS NULL")
            else:
                clauses.append(f"json_extract({table_alias}.attributes_json, '$.{key}') = ?")
                params.append(value)
    return clauses, params


def _reconstruct_cohort_count_sql(
    connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    entity_level = query.get("entity_level")
    if entity_level is None:
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])
    if not isinstance(entity_level, str) or entity_level not in ALL_SUPPORTED_LEVELS:
        return _answer(Status.ERROR.value, Reason.UNSUPPORTED_ENTITY_LEVEL.value, {}, [], [])

    denominator_basis = query.get("denominator_basis")
    if not isinstance(denominator_basis, str) or not denominator_basis.strip():
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])

    predicate = query.get("predicate")
    if not isinstance(predicate, dict):
        return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
    if not predicate or (set(predicate) - ALL_COHORT_FIELDS):
        return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
    if not (
        set(predicate)
        & (
            COHORT_POSITIVE_FIELDS
            | COHORT_SAMPLE_FIELDS
            | COHORT_FILE_FIELDS
            | COHORT_ASSAY_ELIGIBILITY_FIELDS
            | COHORT_MOLECULAR_FIELDS
        )
    ):
        return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
    for key, value in predicate.items():
        if key == "require_explicit_negative_coverage":
            if not isinstance(value, bool):
                return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        elif key.endswith("_in"):
            if not isinstance(value, list) or not value:
                return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        elif isinstance(value, (list, dict)):
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])

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

    if _partial_tables_present_sql(manifest, tuple(required_tables)):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    if molecular_criteria:
        if assay_elig_criteria or assay_pos_criteria:
            return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])
        return _reconstruct_molecular_cohort_count_sql(
            connection,
            manifest,
            query,
            clinical_criteria,
            sample_criteria,
            file_criteria,
            molecular_criteria,
        )

    source_filter = predicate.get("source")
    base_sql = (
        "SELECT DISTINCT p.entity_id, p.source_id, p.source, c.entity_id, "
        "COALESCE(json_extract(c.attributes_json, '$.is_duplicate_case_entry'), 0), c.attributes_json "
        "FROM entities p "
        "LEFT JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
        "LEFT JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
        "WHERE p.kind = 'patient'"
    )
    base_params: list[Any] = []
    if source_filter is not None:
        base_sql += " AND p.source = ?"
        base_params.append(source_filter)
    base_rows = connection.execute(base_sql, base_params).fetchall()

    patient_ids = {row[0] for row in base_rows}
    has_dup_markers = any(
        row[5] is not None and "is_duplicate_case_entry" in json.loads(row[5])
        for row in base_rows
    )
    # Canonical non-duplicate cases per patient
    raw_cases_by_pat: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in base_rows:
        if row[3] is not None:
            raw_cases_by_pat[row[0]].append((row[3], int(bool(row[4]))))
    canonical_case_ids: set[str] = set()
    for pat_id, c_list in raw_cases_by_pat.items():
        primaries = [cid for cid, is_dup in c_list if not is_dup]
        canonical_case_ids.update(primaries if primaries else [cid for cid, _ in c_list])

    case_sources_set: set[str] = set()
    if not patient_ids and not canonical_case_ids and entity_level != "patient":
        case_sql = (
            "SELECT entity_id, source, COALESCE(json_extract(attributes_json, '$.is_duplicate_case_entry'), 0) "
            "FROM entities WHERE kind = 'case'"
        )
        case_params: list[Any] = []
        if source_filter is not None:
            case_sql += " AND source = ?"
            case_params.append(source_filter)
        for cid, csrc, is_dup in connection.execute(case_sql, case_params).fetchall():
            if not is_dup:
                canonical_case_ids.add(cid)
                case_sources_set.add(csrc)

    target_sources = (
        {source_filter}
        if source_filter is not None
        else ({row[2] for row in base_rows} or case_sources_set or set(manifest.get("coverage", {}).keys()))
    )
    if target_sources and not _coverage_ok(manifest, target_sources):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    receptor_keys = {"er_status", "pr_status", "her2_status"}
    is_receptor_panel = bool(receptor_keys & set(assay_pos_criteria))
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

    clin_clauses, clin_params = _sql_criteria_clauses("c", clinical_criteria)
    sample_clauses, sample_params = _sql_criteria_clauses("s", sample_criteria)
    scope_clauses, scope_params = _sql_criteria_clauses("a", target_assay_scope)
    elig_clauses, elig_params = _sql_criteria_clauses("a", effective_assay_elig)
    pos_clauses, pos_params = _sql_criteria_clauses("a", assay_pos_criteria)
    file_clauses, file_params = _sql_criteria_clauses("f", file_criteria)

    for k in receptor_keys & set(assay_pos_criteria):
        elig_clauses.append(f"json_extract(a.attributes_json, '$.{k}') IS NOT NULL")

    cte_prefix = """
    WITH RECURSIVE case_descendants(root_case_id, entity_id, depth) AS (
        SELECT subject_id, object_id, 1
        FROM relationships
        WHERE predicate IN ('HAS_SAMPLE', 'HAS_ALIQUOT', 'HAS_ASSAY', 'HAS_FILE')
        UNION ALL
        SELECT cd.root_case_id, r.object_id, cd.depth + 1
        FROM case_descendants cd
        JOIN relationships r ON r.subject_id = cd.entity_id
        WHERE r.predicate IN ('HAS_SAMPLE', 'HAS_ALIQUOT', 'HAS_ASSAY', 'HAS_FILE')
          AND cd.depth < 5
    )
    """

    canon_placeholders = ",".join("?" for _ in canonical_case_ids) or "NULL"
    canon_params = sorted(canonical_case_ids)

    scope_rows: list[Any] = []
    if patient_ids:
        scope_where = ["p.kind = 'patient'", f"c.entity_id IN ({canon_placeholders})"]
        if source_filter is not None:
            scope_where.append("p.source = ?")
        scope_where.extend(clin_clauses)
        scope_where.extend(scope_clauses)
        scope_sql = (
            cte_prefix
            + "SELECT DISTINCT p.entity_id, p.source_id, a.entity_id, a.source_id, "
            + "json_extract(a.attributes_json, '$.assay_status'), "
            + "json_extract(a.attributes_json, '$.result') "
            + "FROM entities p "
            + "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
            + "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
            + "JOIN case_descendants cd ON cd.root_case_id = c.entity_id "
            + "JOIN entities a ON a.entity_id = cd.entity_id AND a.kind = 'assay' "
            + "WHERE "
            + " AND ".join(scope_where)
        )
        scope_all_params = canon_params + ([source_filter] if source_filter is not None else []) + clin_params + scope_params
        scope_rows = connection.execute(scope_sql, scope_all_params).fetchall()
    scope_assay_ids = {row[2] for row in scope_rows}

    conflicted = _conflicts(connection, patient_ids | canonical_case_ids | scope_assay_ids)
    if conflicted:
        placeholders = ",".join("?" for _ in conflicted)
        source_ids = [
            r[0]
            for r in connection.execute(
                f"SELECT source_id FROM entities WHERE entity_id IN ({placeholders}) ORDER BY source_id",
                tuple(sorted(conflicted)),
            ).fetchall()
        ]
        return _answer(
            Status.CONFLICT.value,
            Reason.CONFLICTING_EVIDENCE.value,
            {},
            source_ids,
            _evidence_refs(connection, conflicted),
        )

    if entity_level == "sample":
        has_assay_criteria = bool(assay_elig_criteria or assay_pos_criteria)
        if patient_ids:
            case_from = (
                "FROM entities p "
                "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
                "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
            )
            base_where = ["p.kind = 'patient'", f"c.entity_id IN ({canon_placeholders})"]
            base_where_params = canon_params + ([source_filter] if source_filter is not None else [])
            if source_filter is not None:
                base_where.append("p.source = ?")
        else:
            case_from = "FROM entities c "
            base_where = ["c.kind = 'case'", "COALESCE(json_extract(c.attributes_json, '$.is_duplicate_case_entry'), 0) = 0"]
            base_where_params = []
            if source_filter is not None:
                base_where.append("c.source = ?")
                base_where_params.append(source_filter)

        if not has_assay_criteria and file_criteria:
            sample_elig_criteria = {
                k: v for k, v in sample_criteria.items() if k not in {"min_tumor_purity", "max_collection_timestamp"}
            }
            sample_cutoff_criteria = {
                k: v for k, v in sample_criteria.items() if k in {"min_tumor_purity", "max_collection_timestamp"}
            }
            smp_elig_clauses, smp_elig_params = _sql_criteria_clauses("s", sample_elig_criteria)
            smp_cut_clauses, smp_cut_params = _sql_criteria_clauses("s", sample_cutoff_criteria)

            elig_where = list(base_where) + clin_clauses + smp_elig_clauses
            elig_sql = (
                "SELECT DISTINCT s.entity_id, s.source_id, json_extract(s.attributes_json, '$.tumor_purity') "
                + case_from
                + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
                + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
                + "WHERE " + " AND ".join(elig_where)
            )
            elig_rows = connection.execute(
                elig_sql,
                base_where_params + clin_params + smp_elig_params,
            ).fetchall()
            if not elig_rows:
                return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])
            if "min_tumor_purity" in sample_cutoff_criteria and any(r[2] is None for r in elig_rows):
                return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

            smp_file_cte = """
            WITH RECURSIVE smp_files(sample_id, entity_id, depth) AS (
                SELECT subject_id, object_id, 1
                FROM relationships
                WHERE predicate IN ('HAS_ALIQUOT', 'HAS_FILE')
                UNION ALL
                SELECT sf.sample_id, r.object_id, sf.depth + 1
                FROM smp_files sf
                JOIN relationships r ON r.subject_id = sf.entity_id
                WHERE r.predicate IN ('HAS_ALIQUOT', 'HAS_FILE') AND sf.depth < 3
            )
            """
            pos_where = list(elig_where) + smp_cut_clauses + file_clauses
            pos_sql = (
                smp_file_cte
                + "SELECT DISTINCT s.entity_id, s.source_id, json_extract(s.attributes_json, '$.collection_timestamp') "
                + case_from
                + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
                + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
                + "JOIN smp_files sf ON sf.sample_id = s.entity_id "
                + "JOIN entities f ON f.entity_id = sf.entity_id AND f.kind = 'file' "
                + "WHERE " + " AND ".join(pos_where)
            )
            pos_candidates = connection.execute(
                pos_sql,
                base_where_params + clin_params + smp_elig_params + smp_cut_params + file_params,
            ).fetchall()
            max_ts = sample_cutoff_criteria.get("max_collection_timestamp")
            max_dt = _parse_iso_dt(str(max_ts)) if max_ts is not None else None
            pos_sample_ids = []
            for _, sid_src, ts_val in pos_candidates:
                if max_ts is not None:
                    ts_dt = _parse_iso_dt(str(ts_val)) if ts_val is not None else None
                    if ts_dt and max_dt:
                        if ts_dt > max_dt:
                            continue
                    elif str(ts_val) > str(max_ts):
                        continue
                pos_sample_ids.append(sid_src)
            pos_sample_ids = sorted(set(pos_sample_ids))
            result = {
                "numerator": len(pos_sample_ids),
                "denominator": len(elig_rows),
                "numerator_count": len(pos_sample_ids),
                "denominator_count": len(elig_rows),
                "percentage": len(pos_sample_ids) / len(elig_rows) * 100.0,
                "entity_level": "sample",
                "denominator_basis": denominator_basis,
                "supporting_evidence_ids": pos_sample_ids,
            }
            return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, pos_sample_ids, pos_sample_ids)

        if not has_assay_criteria:
            elig_where = list(base_where) + clin_clauses
            elig_sql = (
                "SELECT DISTINCT s.entity_id, s.source_id, s.attributes_json "
                + case_from
                + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
                + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
                + "WHERE " + " AND ".join(elig_where)
            )
            elig_rows = connection.execute(elig_sql, base_where_params + clin_params).fetchall()
            if not elig_rows:
                return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])

            pos_where = list(elig_where) + sample_clauses
            pos_sql = (
                "SELECT DISTINCT s.entity_id, s.source_id, json_extract(s.attributes_json, '$.collection_timestamp') "
                + case_from
                + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
                + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
                + "WHERE " + " AND ".join(pos_where)
            )
            pos_candidates = connection.execute(
                pos_sql,
                base_where_params + clin_params + sample_params,
            ).fetchall()
            max_ts = sample_criteria.get("max_collection_timestamp")
            max_dt = _parse_iso_dt(str(max_ts)) if max_ts is not None else None
            pos_sample_ids = []
            for _, sid_src, ts_val in pos_candidates:
                if max_ts is not None:
                    ts_dt = _parse_iso_dt(str(ts_val)) if ts_val is not None else None
                    if ts_dt and max_dt:
                        if ts_dt > max_dt:
                            continue
                    elif str(ts_val) > str(max_ts):
                        continue
                pos_sample_ids.append(sid_src)
            pos_sample_ids = sorted(set(pos_sample_ids))
            result = {
                "numerator": len(pos_sample_ids),
                "denominator": len(elig_rows),
                "numerator_count": len(pos_sample_ids),
                "denominator_count": len(elig_rows),
                "percentage": len(pos_sample_ids) / len(elig_rows) * 100.0,
                "entity_level": "sample",
                "denominator_basis": denominator_basis,
                "supporting_evidence_ids": pos_sample_ids,
            }
            return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, pos_sample_ids, pos_sample_ids)

        smp_cte = """
        WITH RECURSIVE smp_assays(sample_id, entity_id, depth) AS (
            SELECT subject_id, object_id, 1
            FROM relationships
            WHERE predicate IN ('HAS_ALIQUOT', 'HAS_ASSAY')
            UNION ALL
            SELECT sa.sample_id, r.object_id, sa.depth + 1
            FROM smp_assays sa
            JOIN relationships r ON r.subject_id = sa.entity_id
            WHERE r.predicate = 'HAS_ASSAY' AND sa.depth < 3
        )
        """
        elig_where = list(base_where) + clin_clauses + elig_clauses
        elig_sql = (
            smp_cte
            + "SELECT DISTINCT s.entity_id, s.source_id "
            + case_from
            + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
            + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
            + "JOIN smp_assays sa ON sa.sample_id = s.entity_id "
            + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
            + "WHERE " + " AND ".join(elig_where)
        )
        elig_rows = connection.execute(
            elig_sql,
            base_where_params + clin_params + elig_params,
        ).fetchall()
        if not elig_rows:
            return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])

        pos_where = list(elig_where) + pos_clauses
        pos_sql = (
            smp_cte
            + "SELECT DISTINCT s.entity_id, s.source_id "
            + case_from
            + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
            + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
            + "JOIN smp_assays sa ON sa.sample_id = s.entity_id "
            + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
            + "WHERE " + " AND ".join(pos_where)
        )
        pos_rows = connection.execute(
            pos_sql,
            base_where_params + clin_params + elig_params + pos_params,
        ).fetchall()
        pos_ids = sorted({row[1] for row in pos_rows})
        result = {
            "numerator": len(pos_ids),
            "denominator": len(elig_rows),
            "numerator_count": len(pos_ids),
            "denominator_count": len(elig_rows),
            "percentage": len(pos_ids) / len(elig_rows) * 100.0,
            "entity_level": "sample",
            "denominator_basis": denominator_basis,
            "supporting_evidence_ids": pos_ids,
        }
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, pos_ids, pos_ids)

    if entity_level == "file":
        elig_where = ["p.kind = 'patient'", f"c.entity_id IN ({canon_placeholders})"]
        if source_filter is not None:
            elig_where.append("p.source = ?")
        elig_where.extend(clin_clauses)
        elig_where.extend(elig_clauses)
        elig_where.extend(file_clauses)
        elig_sql = (
            cte_prefix
            + "SELECT DISTINCT f.entity_id, f.source_id "
            + "FROM entities p "
            + "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
            + "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
            + "JOIN case_descendants cd ON cd.root_case_id = c.entity_id "
            + "JOIN entities a ON a.entity_id = cd.entity_id AND a.kind = 'assay' "
            + "JOIN relationships hf ON hf.subject_id = a.entity_id AND hf.predicate = 'HAS_FILE' "
            + "JOIN entities f ON f.entity_id = hf.object_id AND f.kind = 'file' "
            + "WHERE " + " AND ".join(elig_where)
        )
        elig_rows = connection.execute(
            elig_sql,
            canon_params + ([source_filter] if source_filter is not None else []) + clin_params + elig_params + file_params,
        ).fetchall()
        if not elig_rows:
            return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])

        pos_where = list(elig_where) + pos_clauses
        pos_sql = (
            cte_prefix
            + "SELECT DISTINCT f.entity_id, f.source_id "
            + "FROM entities p "
            + "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
            + "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
            + "JOIN case_descendants cd ON cd.root_case_id = c.entity_id "
            + "JOIN entities a ON a.entity_id = cd.entity_id AND a.kind = 'assay' "
            + "JOIN relationships hf ON hf.subject_id = a.entity_id AND hf.predicate = 'HAS_FILE' "
            + "JOIN entities f ON f.entity_id = hf.object_id AND f.kind = 'file' "
            + "WHERE " + " AND ".join(pos_where)
        )
        pos_rows = connection.execute(
            pos_sql,
            canon_params + ([source_filter] if source_filter is not None else []) + clin_params + elig_params + file_params + pos_params,
        ).fetchall()
        pos_ids = sorted({row[1] for row in pos_rows})
        result = {
            "numerator": len(pos_ids),
            "denominator": len(elig_rows),
            "numerator_count": len(pos_ids),
            "denominator_count": len(elig_rows),
            "percentage": len(pos_ids) / len(elig_rows) * 100.0,
            "entity_level": "file",
            "denominator_basis": denominator_basis,
            "supporting_evidence_ids": pos_ids,
        }
        return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, pos_ids, pos_ids)

    # patient-level SQL reconstruction
    elig_where = ["p.kind = 'patient'", f"c.entity_id IN ({canon_placeholders})"]
    if source_filter is not None:
        elig_where.append("p.source = ?")
    elig_where.extend(clin_clauses)
    elig_where.extend(elig_clauses)
    elig_sql = (
        cte_prefix
        + "SELECT DISTINCT p.entity_id, p.source_id, a.entity_id, a.source_id "
        + "FROM entities p "
        + "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
        + "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
        + "JOIN case_descendants cd ON cd.root_case_id = c.entity_id "
        + "JOIN entities a ON a.entity_id = cd.entity_id AND a.kind = 'assay' "
        + "WHERE " + " AND ".join(elig_where)
    )
    elig_all_params = canon_params + ([source_filter] if source_filter is not None else []) + clin_params + elig_params
    elig_rows = connection.execute(elig_sql, elig_all_params).fetchall()

    elig_patient_source_ids = {row[1] for row in elig_rows}
    elig_patient_entity_ids = {row[0] for row in elig_rows}
    elig_assay_source_ids = sorted({row[3] for row in elig_rows})
    for row in scope_rows:
        if row[0] not in elig_patient_entity_ids:
            if row[4] in INCOMPLETE_ASSAY_STATUSES or row[5] is None:
                return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    if not elig_patient_source_ids:
        return _answer(
            Status.UNKNOWN.value,
            Reason.ZERO_DENOMINATOR.value,
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

    pos_where = list(elig_where) + pos_clauses
    pos_sql = (
        cte_prefix
        + "SELECT DISTINCT p.entity_id, p.source_id, a.entity_id, a.source_id "
        + "FROM entities p "
        + "JOIN relationships hc ON hc.subject_id = p.entity_id AND hc.predicate = 'HAS_CASE' "
        + "JOIN entities c ON c.entity_id = hc.object_id AND c.kind = 'case' "
        + "JOIN case_descendants cd ON cd.root_case_id = c.entity_id "
        + "JOIN entities a ON a.entity_id = cd.entity_id AND a.kind = 'assay' "
        + "WHERE " + " AND ".join(pos_where)
    )
    pos_all_params = elig_all_params + pos_params
    pos_rows = connection.execute(pos_sql, pos_all_params).fetchall()

    eligible_patient_ids = sorted(elig_patient_source_ids)
    positive_patient_ids = sorted({row[1] for row in pos_rows})
    positive_assay_ids = sorted({row[3] for row in pos_rows})

    if not positive_patient_ids:
        supporting_evidence = elig_assay_source_ids
    elif has_dup_markers or denominator_basis == "distinct_canonical_patients_in_snapshot":
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
    return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, positive_patient_ids, supporting_evidence)


def _reconstruct_molecular_cohort_count_sql(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    query: dict[str, Any],
    clinical_criteria: dict[str, Any],
    sample_criteria: dict[str, Any],
    file_criteria: dict[str, Any],
    molecular_criteria: dict[str, Any],
) -> dict[str, Any]:
    if query.get("entity_level") != "sample":
        return _answer(Status.ERROR.value, Reason.UNSUPPORTED_ENTITY_LEVEL.value, {}, [], [])

    denominator_basis = query["denominator_basis"]
    prov = manifest.get("provenance_metadata") or {}
    named_universes = prov.get("named_universes")
    if isinstance(named_universes, dict) and named_universes and denominator_basis not in named_universes:
        return _answer(Status.UNKNOWN.value, Reason.COHORT_OR_FIXTURE_MISMATCH.value, {}, [], [])

    mol_cov = prov.get("molecular_coverage") or {}
    if isinstance(mol_cov.get("acquired_gene_symbols"), list):
        if "gene_symbol" in molecular_criteria:
            req_genes = [molecular_criteria["gene_symbol"]]
        elif "gene_symbol_in" in molecular_criteria:
            req_genes = list(molecular_criteria["gene_symbol_in"])
        else:
            req_genes = []
        if not req_genes or any(g not in mol_cov["acquired_gene_symbols"] for g in req_genes):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    source_filter = clinical_criteria.get("source")
    if source_filter is not None:
        target_sources = {source_filter}
    else:
        src_rows = connection.execute("SELECT DISTINCT source FROM entities WHERE kind = 'case'").fetchall()
        target_sources = {r[0] for r in src_rows} or set(manifest.get("coverage", {}).keys())
    if target_sources and not _coverage_ok(manifest, target_sources):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

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

    gs_clauses, gs_params = _sql_criteria_clauses("a", gene_scope_criteria)
    gs_where = ["a.kind = 'assay'"]
    gs_all_params: list[Any] = []
    if source_filter is not None:
        gs_where.append("a.source = ?")
        gs_all_params.append(source_filter)
    if not gene_scope_criteria:
        gs_where.append(
            "(json_extract(a.attributes_json, '$.gene_symbol') IS NOT NULL "
            "OR json_extract(a.attributes_json, '$.linkage_status') IS NOT NULL)"
        )
    gs_where.extend(gs_clauses)
    gs_all_params.extend(gs_params)

    link_sql = (
        "SELECT a.entity_id, a.source_id, "
        "json_extract(a.attributes_json, '$.linkage_status'), "
        "alq.entity_id, alq.source_id, "
        "COALESCE(s_direct.entity_id, s_via_alq.entity_id), "
        "COALESCE(s_direct.source_id, s_via_alq.source_id) "
        "FROM entities a "
        "LEFT JOIN relationships ha_alq ON ha_alq.object_id = a.entity_id AND ha_alq.predicate = 'HAS_ASSAY' "
        "LEFT JOIN entities alq ON alq.entity_id = ha_alq.subject_id AND alq.kind = 'aliquot' "
        "LEFT JOIN relationships hsa ON hsa.object_id = alq.entity_id AND hsa.predicate = 'HAS_ALIQUOT' "
        "LEFT JOIN entities s_via_alq ON s_via_alq.entity_id = hsa.subject_id AND s_via_alq.kind = 'sample' "
        "LEFT JOIN entities s_direct ON s_direct.entity_id = ha_alq.subject_id AND s_direct.kind = 'sample' "
        "WHERE " + " AND ".join(gs_where)
    )
    link_rows = connection.execute(link_sql, gs_all_params).fetchall()
    by_assay: dict[str, dict[str, Any]] = {}
    for aid, asrc, lstat, alqid, alqsrc, smpid, smpsrc in link_rows:
        entry = by_assay.setdefault(
            aid, {"source_id": asrc, "linkage_status": lstat, "aliquots": {}, "samples": {}}
        )
        if alqid is not None:
            entry["aliquots"][alqid] = alqsrc
        if smpid is not None:
            entry["samples"][smpid] = smpsrc

    gene_scope_assay_ids = set(by_assay.keys())
    bad_refs: set[str] = set()
    for aid, info in by_assay.items():
        if info["linkage_status"] in {"UNMATCHED", "MULTIPLY_MATCHED"} or len(info["samples"]) != 1 or len(info["aliquots"]) > 1:
            bad_refs.add(info["source_id"])
            bad_refs.update(info["samples"].values())
            bad_refs.update(info["aliquots"].values())
    if bad_refs:
        sorted_bad = sorted(bad_refs)
        return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, sorted_bad, sorted_bad)

    clin_clauses, clin_params = _sql_criteria_clauses("c", clinical_criteria)
    sample_elig_criteria = {
        k: v for k, v in sample_criteria.items() if k not in {"min_tumor_purity", "max_collection_timestamp"}
    }
    sample_cutoff_criteria = {
        k: v for k, v in sample_criteria.items() if k in {"min_tumor_purity", "max_collection_timestamp"}
    }
    smp_elig_clauses, smp_elig_params = _sql_criteria_clauses("s", sample_elig_criteria)
    smp_cut_clauses, smp_cut_params = _sql_criteria_clauses("s", sample_cutoff_criteria)
    file_clauses, file_params = _sql_criteria_clauses("f", file_criteria)

    base_where = [
        "c.kind = 'case'",
        "COALESCE(json_extract(c.attributes_json, '$.is_duplicate_case_entry'), 0) = 0",
    ]
    base_params: list[Any] = []
    if source_filter is not None:
        base_where.append("c.source = ?")
        base_params.append(source_filter)
    base_where.extend(clin_clauses)
    base_where.extend(smp_elig_clauses)
    base_all_params = base_params + clin_params + smp_elig_params

    cand_sql = (
        "SELECT DISTINCT s.entity_id, s.source_id, c.entity_id, "
        "json_extract(s.attributes_json, '$.tumor_purity'), "
        "json_extract(s.attributes_json, '$.collection_timestamp') "
        "FROM entities c "
        "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
        "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
        "WHERE " + " AND ".join(base_where)
    )
    cand_rows = connection.execute(cand_sql, base_all_params).fetchall()
    if "min_tumor_purity" in sample_cutoff_criteria and any(r[3] is None for r in cand_rows):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    max_ts = sample_cutoff_criteria.get("max_collection_timestamp")
    max_dt = _parse_iso_dt(str(max_ts)) if max_ts is not None else None
    min_purity = sample_cutoff_criteria.get("min_tumor_purity")
    candidate_samples: dict[str, str] = {}
    candidate_cases: set[str] = set()
    for sid, s_src, cid, purity_val, ts_val in cand_rows:
        if min_purity is not None and (purity_val is None or float(purity_val) < float(min_purity)):
            continue
        if max_ts is not None:
            if ts_val is None:
                continue
            ts_dt = _parse_iso_dt(str(ts_val))
            if ts_dt and max_dt:
                if ts_dt > max_dt:
                    continue
            elif str(ts_val) > str(max_ts):
                continue
        candidate_samples[sid] = s_src
        candidate_cases.add(cid)

    if file_criteria:
        smp_file_cte = """
        WITH RECURSIVE smp_files(sample_id, entity_id, depth) AS (
            SELECT subject_id, object_id, 1
            FROM relationships
            WHERE predicate IN ('HAS_ALIQUOT', 'HAS_FILE')
            UNION ALL
            SELECT sf.sample_id, r.object_id, sf.depth + 1
            FROM smp_files sf
            JOIN relationships r ON r.subject_id = sf.entity_id
            WHERE r.predicate IN ('HAS_ALIQUOT', 'HAS_FILE') AND sf.depth < 3
        )
        """
        elig_file_where = list(base_where) + smp_cut_clauses + file_clauses
        elig_file_sql = (
            smp_file_cte
            + "SELECT DISTINCT s.entity_id "
            + "FROM entities c "
            + "JOIN relationships hs ON hs.subject_id = c.entity_id AND hs.predicate = 'HAS_SAMPLE' "
            + "JOIN entities s ON s.entity_id = hs.object_id AND s.kind = 'sample' "
            + "JOIN smp_files sf ON sf.sample_id = s.entity_id "
            + "JOIN entities f ON f.entity_id = sf.entity_id AND f.kind = 'file' "
            + "WHERE "
            + " AND ".join(elig_file_where)
        )
        file_matched_sids = {
            r[0]
            for r in connection.execute(
                elig_file_sql, base_all_params + smp_cut_params + file_params
            ).fetchall()
        }
        eligible_samples = {sid: src for sid, src in candidate_samples.items() if sid in file_matched_sids}
    else:
        smp_assay_cte = """
        WITH RECURSIVE smp_assays(sample_id, entity_id, depth) AS (
            SELECT subject_id, object_id, 1
            FROM relationships
            WHERE predicate IN ('HAS_ALIQUOT', 'HAS_ASSAY')
            UNION ALL
            SELECT sa.sample_id, r.object_id, sa.depth + 1
            FROM smp_assays sa
            JOIN relationships r ON r.subject_id = sa.entity_id
            WHERE r.predicate = 'HAS_ASSAY' AND sa.depth < 3
        )
        """
        cand_assay_sql = (
            smp_assay_cte
            + "SELECT DISTINCT sa.sample_id, a.entity_id, "
            + "json_extract(a.attributes_json, '$.call_status'), "
            + "json_extract(a.attributes_json, '$.coverage_completeness'), "
            + "json_extract(a.attributes_json, '$.assay_status'), "
            + "json_extract(a.attributes_json, '$.qc_status') "
            + "FROM smp_assays sa "
            + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
            + "WHERE "
            + " AND ".join(gs_where)
        )
        eligible_sids_no_file: set[str] = set()
        for sid, aid, cstat, covc, astat, qstat in connection.execute(cand_assay_sql, gs_all_params).fetchall():
            if sid not in candidate_samples:
                continue
            if (
                cstat in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
                or covc in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
                or astat in INCOMPLETE_ASSAY_STATUSES
                or qstat in {"FAIL", "QC_FAILED"}
            ):
                return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
            if cstat in {"POSITIVE", "NEGATIVE"}:
                eligible_sids_no_file.add(sid)
        eligible_samples = {sid: src for sid, src in candidate_samples.items() if sid in eligible_sids_no_file}

    if not eligible_samples:
        return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])

    conflicted = _conflicts(connection, candidate_cases | set(eligible_samples.keys()) | gene_scope_assay_ids)
    if conflicted:
        source_ids = sorted(
            r[1]
            for r in connection.execute("SELECT entity_id, source_id FROM entities").fetchall()
            if r[0] in conflicted
        )
        return _answer(
            Status.CONFLICT.value,
            Reason.CONFLICTING_EVIDENCE.value,
            {},
            source_ids,
            _evidence_refs(connection, conflicted),
        )

    smp_assay_cte = """
    WITH RECURSIVE smp_assays(sample_id, entity_id, depth) AS (
        SELECT subject_id, object_id, 1
        FROM relationships
        WHERE predicate IN ('HAS_ALIQUOT', 'HAS_ASSAY')
        UNION ALL
        SELECT sa.sample_id, r.object_id, sa.depth + 1
        FROM smp_assays sa
        JOIN relationships r ON r.subject_id = sa.entity_id
        WHERE r.predicate = 'HAS_ASSAY' AND sa.depth < 3
    )
    """
    elig_gene_sql = (
        smp_assay_cte
        + "SELECT DISTINCT sa.sample_id, a.entity_id, a.source_id, "
        + "json_extract(a.attributes_json, '$.call_status'), "
        + "json_extract(a.attributes_json, '$.coverage_completeness'), "
        + "json_extract(a.attributes_json, '$.assay_status'), "
        + "json_extract(a.attributes_json, '$.qc_status') "
        + "FROM smp_assays sa "
        + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
        + "WHERE "
        + " AND ".join(gs_where)
    )
    for sid, _, _, cstat, covc, astat, qstat in connection.execute(elig_gene_sql, gs_all_params).fetchall():
        if sid not in eligible_samples:
            continue
        if (
            cstat in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
            or covc in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
            or astat in INCOMPLETE_ASSAY_STATUSES
            or qstat in {"FAIL", "QC_FAILED"}
        ):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    loc_clauses, loc_params = _sql_criteria_clauses("a", variant_locator_criteria)
    loc_where = list(gs_where) + loc_clauses
    loc_sql = (
        smp_assay_cte
        + "SELECT DISTINCT sa.sample_id, a.entity_id, a.source_id, "
        + "json_extract(a.attributes_json, '$.call_status'), "
        + "json_extract(a.attributes_json, '$.coverage_completeness'), "
        + "json_extract(a.attributes_json, '$.adjudication_status') "
        + "FROM smp_assays sa "
        + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
        + "WHERE "
        + " AND ".join(loc_where)
    )
    loc_rows = connection.execute(loc_sql, gs_all_params + loc_params).fetchall()
    loc_by_sample: dict[str, list[tuple[str, str, Any, Any, Any]]] = defaultdict(list)
    for sid, aid, asrc, cstat, covc, adjst in loc_rows:
        if sid in eligible_samples:
            loc_by_sample[sid].append((aid, asrc, cstat, covc, adjst))

    conflict_sample_srcs: set[str] = set()
    conflict_assay_srcs: set[str] = set()
    for sid in sorted(eligible_samples):
        rows_s = loc_by_sample.get(sid, [])
        statuses = {r[2] for r in rows_s}
        if "CONFLICT" in statuses or any(r[4] == "UNRESOLVED" for r in rows_s) or ("POSITIVE" in statuses and "NEGATIVE" in statuses):
            conflict_sample_srcs.add(eligible_samples[sid])
            conflict_assay_srcs.update(r[1] for r in rows_s)
    if conflict_sample_srcs:
        return _answer(
            Status.CONFLICT.value,
            Reason.CONFLICTING_EVIDENCE.value,
            {},
            sorted(conflict_sample_srcs),
            sorted(conflict_sample_srcs | conflict_assay_srcs),
        )

    if wants_negative_calls or require_neg_cov:
        if (
            mol_cov.get("explicit_negative_calls_present") is False
            or mol_cov.get("locus_level_callable_coverage_present") is False
        ):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        for sid in sorted(eligible_samples):
            rows_s = loc_by_sample.get(sid, [])
            if not any(r[2] == "POSITIVE" or (r[2] == "NEGATIVE" and r[3] == "COMPLETE_LOCUS_COVERAGE") for r in rows_s):
                return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    match_clauses, match_params = _sql_criteria_clauses("a", variant_match_criteria)
    match_where = list(gs_where) + match_clauses
    match_file_join = (
        "LEFT JOIN relationships hf ON hf.subject_id = a.entity_id AND hf.predicate = 'HAS_FILE' "
        "LEFT JOIN entities f ON f.entity_id = hf.object_id AND f.kind = 'file' "
    )
    match_sql = (
        smp_assay_cte
        + "SELECT DISTINCT sa.sample_id, a.entity_id, a.source_id, f.entity_id, f.source_id "
        + "FROM smp_assays sa "
        + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
        + match_file_join
        + "WHERE "
        + " AND ".join(match_where)
    )
    match_rows = connection.execute(match_sql, gs_all_params + match_params).fetchall()

    matching_samples: set[str] = set()
    matching_assays: set[str] = set()
    matching_aliquot_srcs: set[str] = set()
    matching_file_ids: set[str] = set()

    # Track file criteria filter when linking matching_files if file_criteria was given
    allowed_file_eids: set[str] | None = None
    if file_criteria:
        fc_clauses, fc_params = _sql_criteria_clauses("f", file_criteria)
        allowed_file_eids = {
            r[0]
            for r in connection.execute(
                "SELECT entity_id FROM entities f WHERE f.kind = 'file' AND " + " AND ".join(fc_clauses),
                fc_params,
            ).fetchall()
        }

    for sid, aid, asrc, fid, fsrc in match_rows:
        if sid not in eligible_samples:
            continue
        matching_samples.add(eligible_samples[sid])
        matching_assays.add(asrc)
        matching_aliquot_srcs.update(by_assay[aid]["aliquots"].values())
        if fid is not None and fsrc is not None and (allowed_file_eids is None or fid in allowed_file_eids):
            matching_file_ids.add(fsrc)

    pos_sample_ids = sorted(matching_samples)
    pos_assay_ids = sorted(matching_assays)
    pos_aliquot_ids = sorted(matching_aliquot_srcs)
    pos_file_ids = sorted(matching_file_ids)
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
    return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, pos_sample_ids, all_lineage_refs)


def _reconstruct_claim_verification_sql(
    connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    predicate = query.get("predicate") or {}
    if not isinstance(predicate, dict) or not predicate:
        return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
    subject_ref = query.get("subject_ref") or {}

    if subject_ref.get("sample_id") and "gene_symbol" in predicate:
        if _partial_tables_present_sql(manifest, ("cases", "samples", "assays")):
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        prov = manifest.get("provenance_metadata") or {}
        mol_cov = prov.get("molecular_coverage") or {}
        if isinstance(mol_cov.get("acquired_gene_symbols"), list) and predicate["gene_symbol"] not in mol_cov["acquired_gene_symbols"]:
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

        sample_id = subject_ref["sample_id"]
        source_filter = predicate.get("source")
        smp_sql = "SELECT entity_id, source_id FROM entities WHERE kind = 'sample' AND source_id = ?"
        smp_params: list[Any] = [sample_id]
        if source_filter is not None:
            smp_sql += " AND source = ?"
            smp_params.append(source_filter)
        smp_matches = connection.execute(smp_sql, smp_params).fetchall()
        if not smp_matches:
            return _answer(Status.UNKNOWN.value, Reason.INSUFFICIENT_EVIDENCE.value, {}, [], [])
        if len(smp_matches) > 1:
            return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SUBJECT.value, {}, [], [])
        sid, sid_src = smp_matches[0]

        expected_call = predicate.get("expected_call", "POSITIVE")
        if expected_call not in {"POSITIVE", "NEGATIVE"}:
            return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
        allowed_claim_keys = (COHORT_MOLECULAR_FIELDS - {"call_status", "call_status_in", "require_explicit_negative_coverage"}) | {"expected_call", "source"}
        if set(predicate) - allowed_claim_keys:
            return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])

        gene_scope_keys = {"gene_symbol", "gene_symbol_in", "gene_id", "ncbi_build", "canonical_transcript_id"}
        gene_scope_criteria = {k: v for k, v in predicate.items() if k in gene_scope_keys}
        locator_criteria = {
            k: v
            for k, v in predicate.items()
            if k in (COHORT_MOLECULAR_FIELDS - {"call_status", "call_status_in", "require_explicit_negative_coverage"})
        }

        gs_clauses, gs_params = _sql_criteria_clauses("a", gene_scope_criteria)
        gs_where = ["a.kind = 'assay'"]
        gs_all_params: list[Any] = []
        if source_filter is not None:
            gs_where.append("a.source = ?")
            gs_all_params.append(source_filter)
        gs_where.extend(gs_clauses)
        gs_all_params.extend(gs_params)

        link_sql = (
            "SELECT a.entity_id, a.source_id, json_extract(a.attributes_json, '$.linkage_status'), "
            "alq.entity_id, alq.source_id, "
            "COALESCE(s_direct.entity_id, s_via_alq.entity_id), "
            "COALESCE(s_direct.source_id, s_via_alq.source_id) "
            "FROM entities a "
            "LEFT JOIN relationships ha_alq ON ha_alq.object_id = a.entity_id AND ha_alq.predicate = 'HAS_ASSAY' "
            "LEFT JOIN entities alq ON alq.entity_id = ha_alq.subject_id AND alq.kind = 'aliquot' "
            "LEFT JOIN relationships hsa ON hsa.object_id = alq.entity_id AND hsa.predicate = 'HAS_ALIQUOT' "
            "LEFT JOIN entities s_via_alq ON s_via_alq.entity_id = hsa.subject_id AND s_via_alq.kind = 'sample' "
            "LEFT JOIN entities s_direct ON s_direct.entity_id = ha_alq.subject_id AND s_direct.kind = 'sample' "
            "WHERE " + " AND ".join(gs_where)
        )
        by_assay: dict[str, dict[str, Any]] = {}
        for aid, asrc, lstat, alqid, alqsrc, smpid, smpsrc in connection.execute(link_sql, gs_all_params).fetchall():
            entry = by_assay.setdefault(
                aid, {"source_id": asrc, "linkage_status": lstat, "aliquots": {}, "samples": {}}
            )
            if alqid is not None:
                entry["aliquots"][alqid] = alqsrc
            if smpid is not None:
                entry["samples"][smpid] = smpsrc
        bad_refs: set[str] = set()
        for aid, info in by_assay.items():
            bad_cond = info["linkage_status"] in {"UNMATCHED", "MULTIPLY_MATCHED"} or len(info["samples"]) != 1 or len(info["aliquots"]) > 1
            touch_cond = (sid in info["samples"]) or (not info["samples"]) or (info["linkage_status"] in {"UNMATCHED", "MULTIPLY_MATCHED"})
            if bad_cond and touch_cond:
                bad_refs.add(info["source_id"])
                bad_refs.update(info["samples"].values())
                bad_refs.update(info["aliquots"].values())
        if bad_refs:
            sorted_bad = sorted(bad_refs)
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, sorted_bad, sorted_bad)

        smp_assay_cte = """
        WITH RECURSIVE smp_assays(sample_id, entity_id, depth) AS (
            SELECT subject_id, object_id, 1
            FROM relationships
            WHERE predicate IN ('HAS_ALIQUOT', 'HAS_ASSAY')
            UNION ALL
            SELECT sa.sample_id, r.object_id, sa.depth + 1
            FROM smp_assays sa
            JOIN relationships r ON r.subject_id = sa.entity_id
            WHERE r.predicate = 'HAS_ASSAY' AND sa.depth < 3
        )
        """
        gene_sql = (
            smp_assay_cte
            + "SELECT DISTINCT a.entity_id, a.source_id, "
            + "json_extract(a.attributes_json, '$.call_status'), "
            + "json_extract(a.attributes_json, '$.coverage_completeness'), "
            + "json_extract(a.attributes_json, '$.assay_status'), "
            + "json_extract(a.attributes_json, '$.qc_status') "
            + "FROM smp_assays sa "
            + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
            + "WHERE sa.sample_id = ? AND "
            + " AND ".join(gs_where)
        )
        for _, _, cstat, covc, astat, qstat in connection.execute(gene_sql, [sid] + gs_all_params).fetchall():
            if (
                cstat in {"UNKNOWN", "MISSING_COVERAGE", "INCOMPLETE"}
                or covc in {"MISSING_COVERAGE", "UNSEQUENCED", "LOW_COVERAGE"}
                or astat in INCOMPLETE_ASSAY_STATUSES
                or qstat in {"FAIL", "QC_FAILED"}
            ):
                return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

        loc_clauses, loc_params = _sql_criteria_clauses("a", locator_criteria)
        loc_where = ["sa.sample_id = ?"] + gs_where + loc_clauses
        loc_sql = (
            smp_assay_cte
            + "SELECT DISTINCT a.entity_id, a.source_id, "
            + "json_extract(a.attributes_json, '$.call_status'), "
            + "json_extract(a.attributes_json, '$.coverage_completeness'), "
            + "json_extract(a.attributes_json, '$.adjudication_status') "
            + "FROM smp_assays sa "
            + "JOIN entities a ON a.entity_id = sa.entity_id AND a.kind = 'assay' "
            + "WHERE "
            + " AND ".join(loc_where)
        )
        loc_rows = connection.execute(loc_sql, [sid] + gs_all_params + loc_params).fetchall()
        loc_assay_eids = {r[0] for r in loc_rows}
        conflicted = _conflicts(connection, {sid} | loc_assay_eids)
        if conflicted:
            source_ids = sorted(
                r[1]
                for r in connection.execute("SELECT entity_id, source_id FROM entities").fetchall()
                if r[0] in conflicted
            )
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, source_ids, _evidence_refs(connection, conflicted))

        statuses = {r[2] for r in loc_rows}
        if "CONFLICT" in statuses or any(r[4] == "UNRESOLVED" for r in loc_rows) or ("POSITIVE" in statuses and "NEGATIVE" in statuses):
            refs = sorted({sid_src} | {r[1] for r in loc_rows})
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, [sid_src], refs)

        pos_rows = [r for r in loc_rows if r[2] == "POSITIVE"]
        neg_complete_rows = [r for r in loc_rows if r[2] == "NEGATIVE" and r[3] == "COMPLETE_LOCUS_COVERAGE"]
        if not pos_rows and not neg_complete_rows:
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

        active_rows = pos_rows if pos_rows else neg_complete_rows
        active_eids = [r[0] for r in active_rows]
        placeholders = ",".join("?" for _ in active_eids)
        file_rows = connection.execute(
            f"SELECT f.source_id FROM relationships r "
            f"JOIN entities f ON f.entity_id = r.object_id AND f.kind = 'file' "
            f"WHERE r.predicate = 'HAS_FILE' AND r.subject_id IN ({placeholders})",
            active_eids,
        ).fetchall()
        aliquot_srcs = {src for aid in active_eids for src in by_assay.get(aid, {}).get("aliquots", {}).values()}
        ev_ids = sorted({sid_src} | {r[1] for r in active_rows} | aliquot_srcs | {fr[0] for fr in file_rows})
        observed_call = "POSITIVE" if pos_rows else "NEGATIVE"
        if observed_call == expected_call:
            return _answer(
                Status.SUPPORTED.value,
                Reason.NONE.value,
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
            Status.CONTRADICTED.value,
            Reason.CLAIM_CONTRADICTED.value,
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

    if _partial_tables_present_sql(manifest, ("assays", "pathology_reports")):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])

    if "claimed_prevalence" in predicate:
        clauses = [
            "kind = 'assay'",
            "json_extract(attributes_json, '$.assay_status') = 'COMPLETED'",
            "json_extract(attributes_json, '$.qc_status') = 'PASS'",
        ]
        params: list[Any] = []
        for qk, jk in (("subcohort_er", "er_status"), ("subcohort_pr", "pr_status"), ("subcohort_her2", "her2_status")):
            if predicate.get(qk) is not None:
                clauses.append(f"json_extract(attributes_json, '$.{jk}') = ?")
                params.append(predicate[qk])
        cnt = connection.execute(
            "SELECT COUNT(*) FROM entities WHERE " + " AND ".join(clauses),
            params,
        ).fetchone()[0]
        if cnt == 0:
            return _answer(Status.UNKNOWN.value, Reason.ZERO_DENOMINATOR.value, {}, [], [])

    if subject_ref.get("patient_id") and (predicate.get("field") == "stage" or "required_stage" in predicate):
        if not _has_sql_table(connection, "pathology_reports"):
            return _answer(Status.UNKNOWN.value, Reason.INSUFFICIENT_EVIDENCE.value, {}, [], [])
        rows = connection.execute(
            "SELECT report_id, record_status, superseded_by, json_extract(attributes_json, '$.stage') "
            "FROM pathology_reports WHERE patient_id = ? ORDER BY report_id",
            (subject_ref["patient_id"],),
        ).fetchall()
        if not rows:
            return _answer(Status.UNKNOWN.value, Reason.INSUFFICIENT_EVIDENCE.value, {}, [], [])
        active = [r for r in rows if r[1] == "ACTIVE_FINAL" and not r[2]]
        superseded = [r for r in rows if r[1] == "SUPERSEDED"]
        if not active:
            return _answer(Status.UNKNOWN.value, Reason.INSUFFICIENT_EVIDENCE.value, {}, [], [])
        if len({r[3] for r in active}) > 1:
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, [], [])
        expected_stage = predicate.get("expected_value") if "expected_value" in predicate else predicate.get("required_stage")
        if active[0][3] == expected_stage:
            supp = sorted([active[0][0]])
            return _answer(
                Status.SUPPORTED.value,
                Reason.NONE.value,
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
        contra = sorted([active[0][0]] + [r[0] for r in superseded])
        return _answer(
            Status.CONTRADICTED.value,
            Reason.CLAIM_CONTRADICTED.value,
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

    if subject_ref.get("patient_id") and "biomarker" in predicate:
        rows = connection.execute(
            "SELECT entity_id, source_id, json_extract(attributes_json, '$.result'), "
            "json_extract(attributes_json, '$.adjudication_status') "
            "FROM entities WHERE kind = 'assay' "
            "AND json_extract(attributes_json, '$.patient_id') = ? "
            "AND json_extract(attributes_json, '$.biomarker') = ? "
            "AND json_extract(attributes_json, '$.assay_status') = 'COMPLETED' "
            "AND json_extract(attributes_json, '$.qc_status') = 'PASS' "
            "AND json_extract(attributes_json, '$.result') IS NOT NULL "
            "ORDER BY source_id",
            (subject_ref["patient_id"], predicate["biomarker"]),
        ).fetchall()
        if not rows:
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        if any(r[3] == "UNRESOLVED" for r in rows) or len({r[2] for r in rows}) > 1:
            return _answer(Status.CONFLICT.value, Reason.CONFLICTING_EVIDENCE.value, {}, [], [])
        assay_eids = [r[0] for r in rows]
        placeholders = ",".join("?" for _ in assay_eids)
        file_rows = connection.execute(
            f"SELECT f.source_id FROM relationships r "
            f"JOIN entities f ON f.entity_id = r.object_id AND f.kind = 'file' "
            f"WHERE r.predicate = 'HAS_FILE' AND r.subject_id IN ({placeholders})",
            assay_eids,
        ).fetchall()
        ev_ids = sorted({r[1] for r in rows} | {fr[0] for fr in file_rows})
        if rows[0][2] == predicate.get("expected_result"):
            return _answer(
                Status.SUPPORTED.value,
                Reason.NONE.value,
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
            Status.CONTRADICTED.value,
            Reason.CLAIM_CONTRADICTED.value,
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

    if "claim_numerator_count" in predicate or "claimed_negative_count" in predicate:
        biomarker = predicate.get("biomarker")
        pos_rows = connection.execute(
            "SELECT source_id, json_extract(attributes_json, '$.patient_id') "
            "FROM entities WHERE kind = 'assay' "
            "AND json_extract(attributes_json, '$.biomarker') = ? "
            "AND json_extract(attributes_json, '$.assay_status') = 'COMPLETED' "
            "AND json_extract(attributes_json, '$.qc_status') = 'PASS' "
            "AND json_extract(attributes_json, '$.result') IS NOT NULL "
            "AND json_extract(attributes_json, '$.result') NOT IN ('WT_NEGATIVE', 'NEGATIVE')",
            (biomarker,),
        ).fetchall()
        if pos_rows:
            contra_ids = sorted({r[0] for r in pos_rows} | {r[1] for r in pos_rows if r[1]})
            return _answer(
                Status.CONTRADICTED.value,
                Reason.CLAIM_CONTRADICTED.value,
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
        all_pids = {
            r[0] for r in connection.execute("SELECT source_id FROM entities WHERE kind = 'patient'").fetchall()
        }
        tested_rows = connection.execute(
            "SELECT source_id, json_extract(attributes_json, '$.patient_id') "
            "FROM entities WHERE kind = 'assay' "
            "AND json_extract(attributes_json, '$.biomarker') = ? "
            "AND json_extract(attributes_json, '$.assay_status') = 'COMPLETED' "
            "AND json_extract(attributes_json, '$.qc_status') = 'PASS' "
            "AND json_extract(attributes_json, '$.result') IS NOT NULL",
            (biomarker,),
        ).fetchall()
        tested_pids = {r[1] for r in tested_rows if r[1]}
        if not all_pids or tested_pids != all_pids:
            return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
        supp_ids = sorted({r[0] for r in tested_rows})
        return _answer(
            Status.SUPPORTED.value,
            Reason.NONE.value,
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

    return _answer(Status.OUT_OF_SCOPE.value, Reason.INVALID_QUERY.value, {}, [], [])


def _reconstruct_cross_source_overlap_sql(
    connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    requested_sources = query.get("requested_sources")
    entity_level = query.get("entity_level")
    denominator_basis = query.get("denominator_basis")
    if not isinstance(requested_sources, list) or len(requested_sources) < 2:
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])
    if entity_level is None or not isinstance(denominator_basis, str) or not denominator_basis.strip():
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SELECTOR.value, {}, [], [])

    if not _has_sql_table(connection, "identity_mappings"):
        return _answer(Status.UNKNOWN.value, Reason.NO_APPROVED_MAPPING.value, {}, [], [])

    src_set = set(requested_sources)
    rows = connection.execute(
        "SELECT mapping_id, source_a, source_b, source_a_id, source_b_id, approval_status, cardinality, resolution "
        "FROM identity_mappings ORDER BY mapping_id"
    ).fetchall()
    mappings = [r for r in rows if {r[1], r[2]} == src_set]
    if not mappings:
        return _answer(Status.UNKNOWN.value, Reason.NO_APPROVED_MAPPING.value, {}, [], [])
    if any(r[7] == "AMBIGUOUS_ONE_TO_MANY" or r[6] == "ONE_TO_MANY" or r[5] == "APPROVED_CANDIDATE_SET" for r in mappings):
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_MAPPING.value, {}, [], [])

    approved = [r for r in mappings if r[5] == "APPROVED"]
    if not approved:
        return _answer(Status.UNKNOWN.value, Reason.NO_APPROVED_MAPPING.value, {}, [], [])

    a_to_b: dict[str, set[str]] = defaultdict(set)
    b_to_a: dict[str, set[str]] = defaultdict(set)
    for r in approved:
        if r[3] and r[4]:
            a_to_b[r[3]].add(r[4])
            b_to_a[r[4]].add(r[3])
    if any(len(v) > 1 for v in a_to_b.values()) or any(len(v) > 1 for v in b_to_a.values()):
        return _answer(Status.CONFLICT.value, Reason.CONFLICTING_MAPPING.value, {}, [], [])

    src_placeholders = ",".join("?" for _ in src_set)
    pat_rows = connection.execute(
        f"SELECT source_id FROM entities WHERE kind = 'patient' AND source IN ({src_placeholders}) ORDER BY source_id",
        tuple(sorted(src_set)),
    ).fetchall()
    parent = {r[0]: r[0] for r in pat_rows}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for r in approved:
        sa, sb = r[3], r[4]
        if sa in parent and sb in parent:
            ra, rb = find(sa), find(sb)
            if ra != rb:
                parent[rb] = ra

    union_count = len({find(pid) for pid in parent})
    overlap_pairs = len(approved)
    supp_ids = sorted([r[0] for r in approved] + [r[0] for r in pat_rows])
    result = {
        "numerator": overlap_pairs,
        "denominator": union_count,
        "numerator_count": overlap_pairs,
        "denominator_count": union_count,
        "entity_level": entity_level,
        "denominator_basis": denominator_basis,
        "supporting_evidence_ids": supp_ids,
    }
    return _answer(Status.SUPPORTED.value, Reason.NONE.value, result, supp_ids, supp_ids)


def _reconstruct_entity_attribute_sql(
    connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    subject_ref = query.get("subject_ref") or {}
    if not isinstance(subject_ref, dict) or not subject_ref:
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SUBJECT.value, {}, [], [])
    if "legacy_subject_alias" in subject_ref:
        rows = connection.execute(
            "SELECT source_id FROM entities WHERE kind = 'patient' "
            "AND json_extract(attributes_json, '$.legacy_subject_alias') = ? ORDER BY source_id",
            (subject_ref["legacy_subject_alias"],),
        ).fetchall()
    elif "patient_id" in subject_ref:
        rows = connection.execute(
            "SELECT source_id FROM entities WHERE kind = 'patient' AND source_id = ? ORDER BY source_id",
            (subject_ref["patient_id"],),
        ).fetchall()
    else:
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SUBJECT.value, {}, [], [])
    if not rows:
        return _answer(Status.UNKNOWN.value, Reason.INSUFFICIENT_EVIDENCE.value, {}, [], [])
    if len(rows) > 1:
        return _answer(Status.AMBIGUOUS.value, Reason.AMBIGUOUS_SUBJECT.value, {}, [], [])
    pid = rows[0][0]
    supp = [pid]
    return _answer(
        Status.SUPPORTED.value,
        Reason.NONE.value,
        {"entity_level": "patient", "patient_id": pid, "supporting_evidence_ids": supp},
        supp,
        supp,
    )


def _reconstruct(connection: sqlite3.Connection, query: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Independent SQL-backed reconstruction; intentionally not engine.evaluate's control flow."""
    if not isinstance(query, dict):
        return _answer(Status.ERROR.value, Reason.INVALID_QUERY.value, {}, [], [])
    operation = query.get("operation") or query.get("query_type")
    if operation not in OPERATIONS:
        return _answer(Status.OUT_OF_SCOPE.value, Reason.UNSUPPORTED_OPERATION.value, {}, [], [])
    if query.get("snapshot_id") is not None and query.get("snapshot_id") != manifest.get("snapshot_id"):
        return _answer(Status.UNKNOWN.value, Reason.SNAPSHOT_MISMATCH.value, {}, [], [])
    if query.get("fixture_id") is not None and query.get("fixture_id") != manifest.get("fixture_id"):
        return _answer(Status.UNKNOWN.value, Reason.COHORT_OR_FIXTURE_MISMATCH.value, {}, [], [])
    if query.get("cohort_id") is not None and query.get("cohort_id") != manifest.get("cohort_id"):
        return _answer(Status.UNKNOWN.value, Reason.COHORT_OR_FIXTURE_MISMATCH.value, {}, [], [])
    prov_err = _check_provenance_and_ontology_sql(manifest, query)
    if prov_err is not None:
        return prov_err
    if not manifest.get("complete"):
        return _answer(Status.UNKNOWN.value, Reason.INCOMPLETE_SNAPSHOT.value, {}, [], [])
    if operation == "out_of_scope_request":
        return _answer(Status.OUT_OF_SCOPE.value, Reason.OUT_OF_SCOPE_QUERY.value, {}, [], [])
    if operation == "cohort_count":
        return _reconstruct_cohort_count_sql(connection, query, manifest)
    if operation == "claim_verification":
        return _reconstruct_claim_verification_sql(connection, query, manifest)
    if operation == "cross_source_overlap_count":
        return _reconstruct_cross_source_overlap_sql(connection, query, manifest)
    if operation == "entity_attribute_query":
        return _reconstruct_entity_attribute_sql(connection, query, manifest)
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


def _compute_binary_merkle_root(leaf_hashes: list[str]) -> str:
    if not leaf_hashes:
        return hashlib.sha256(b"").hexdigest()
    level = list(leaf_hashes)
    while len(level) > 1:
        next_level = []
        for idx in range(0, len(level), 2):
            left = bytes.fromhex(level[idx])
            right = bytes.fromhex(level[idx + 1] if idx + 1 < len(level) else level[idx])
            next_level.append(hashlib.sha256(left + right).hexdigest())
        level = next_level
    return level[0]


def _sql_record_map(connection: sqlite3.Connection, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    prov = manifest.get("provenance_metadata") or {}
    if isinstance(prov.get("raw_tables_by_id"), dict):
        return prov["raw_tables_by_id"]
    records: dict[str, dict[str, Any]] = {}
    id_field_map = {
        "patient": "patient_id",
        "case": "case_id",
        "sample": "sample_id",
        "aliquot": "aliquot_id",
        "assay": "assay_id",
        "file": "file_id",
    }
    for row in _entities(connection):
        rec = dict(row["attributes"])
        id_key = id_field_map.get(row["kind"], "id")
        rec[id_key] = row["source_id"]
        if row["kind"] in {"patient", "case"}:
            rec.setdefault("source_id", row["source"])
        records[row["source_id"]] = rec
    if _has_sql_table(connection, "pathology_reports"):
        for rep_id, attrs_json in connection.execute("SELECT report_id, attributes_json FROM pathology_reports").fetchall():
            records[rep_id] = json.loads(attrs_json)
    if _has_sql_table(connection, "identity_mappings"):
        for map_id, attrs_json in connection.execute("SELECT mapping_id, attributes_json FROM identity_mappings").fetchall():
            records[map_id] = json.loads(attrs_json)
    return records


def _replay_synthetic_exam_envelope(snapshot_dir: Path, certificate: dict[str, Any]) -> dict[str, Any]:
    """Verify structured witness-Merkle test envelopes and replay embedded SQL query independently."""
    required_keys = {
        "certificate_id",
        "snapshot_id",
        "snapshot_sha256",
        "issued_at",
        "signer_key_id",
        "query",
        "result",
        "evidence_references",
        "digests",
        "signature",
    }
    if not required_keys.issubset(certificate.keys()):
        return _invalid(Reason.MALFORMED_CERTIFICATE.value, status=Status.ERROR.value, replay_verified=False)

    manifest, connection = read_snapshot(snapshot_dir)
    try:
        prov = manifest.get("provenance_metadata") or {}
        if prov.get("integrity_errors"):
            return _invalid(Reason.PROVENANCE_ERROR.value, status=Status.ERROR.value, replay_verified=False)

        declared_snap_id = prov.get("declared_snapshot_id") or manifest.get("fixture_id") or manifest.get("snapshot_id")
        declared_snap_sha = prov.get("declared_snapshot_sha256") or manifest.get("raw_content_sha256")
        if certificate.get("snapshot_id") != declared_snap_id or certificate.get("snapshot_sha256") != declared_snap_sha:
            return _invalid(Reason.SNAPSHOT_MISMATCH.value, status=Status.ERROR.value, replay_verified=False)

        trusted_keys = {
            k["key_id"]: k
            for k in (prov.get("trusted_signing_keys") or [])
            if isinstance(k, dict) and k.get("status") == "ACTIVE_TRUSTED"
        }
        signer_key_id = certificate.get("signer_key_id")
        sig = certificate.get("signature") or {}
        sig_hex = sig.get("hex", "")
        if (
            signer_key_id not in trusted_keys
            or sig.get("key_id") != signer_key_id
            or sig.get("algorithm") != trusted_keys[signer_key_id].get("algorithm")
            or not isinstance(sig_hex, str)
            or len(sig_hex) != 128
            or any(c not in "0123456789abcdef" for c in sig_hex.lower())
        ):
            return _invalid(Reason.SIGNATURE_INVALID.value, status=Status.ERROR.value, replay_verified=False)

        query = certificate["query"]
        cert_result = certificate["result"]
        ev_refs = certificate["evidence_references"]
        digests = certificate["digests"]
        if not isinstance(query, dict) or not isinstance(cert_result, dict) or not isinstance(ev_refs, dict) or not isinstance(digests, dict):
            return _invalid(Reason.MALFORMED_CERTIFICATE.value, status=Status.ERROR.value, replay_verified=False)

        query_sha = hashlib.sha256(canonical_json(query).encode("utf-8")).hexdigest()
        result_sha = hashlib.sha256(canonical_json(cert_result).encode("utf-8")).hexdigest()
        if digests.get("query_sha256") != query_sha or digests.get("result_sha256") != result_sha:
            return _invalid(Reason.CERTIFICATE_TAMPERED.value, status=Status.ERROR.value, replay_verified=False)

        supp_ids = list(ev_refs.get("supporting_evidence_ids") or [])
        contra_ids = list(ev_refs.get("contradicting_evidence_ids") or [])
        all_ev_ids = sorted(set(supp_ids + contra_ids))
        record_map = _sql_record_map(connection, manifest)
        leaves = [
            hashlib.sha256(canonical_json({"evidence_id": eid, "record": record_map.get(eid)}).encode("utf-8")).hexdigest()
            for eid in all_ev_ids
        ]
        merkle_root = _compute_binary_merkle_root(leaves)
        if ev_refs.get("witness_leaves_sha256") != leaves or digests.get("evidence_merkle_root") != merkle_root:
            return _invalid(Reason.CERTIFICATE_TAMPERED.value, status=Status.ERROR.value, replay_verified=False)

        body = {
            "certificate_id": certificate["certificate_id"],
            "snapshot_id": certificate["snapshot_id"],
            "snapshot_sha256": certificate["snapshot_sha256"],
            "issued_at": certificate["issued_at"],
            "signer_key_id": certificate["signer_key_id"],
            "query": query,
            "result": cert_result,
            "evidence_references": ev_refs,
            "digests_without_body_hash": {
                "query_sha256": query_sha,
                "result_sha256": result_sha,
                "evidence_merkle_root": merkle_root,
            },
        }
        body_sha = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        if digests.get("certificate_body_sha256") != body_sha:
            return _invalid(Reason.CERTIFICATE_TAMPERED.value, status=Status.ERROR.value, replay_verified=False)

        expected = _reconstruct(connection, query, manifest)
    finally:
        connection.close()

    expected_res = expected.get("result") or {}
    if (
        expected.get("status") != cert_result.get("status")
        or expected_res.get("numerator_count") != cert_result.get("numerator_count")
        or expected_res.get("denominator_count") != cert_result.get("denominator_count")
        or expected_res.get("entity_level") != cert_result.get("entity_level")
        or expected_res.get("denominator_basis") != cert_result.get("denominator_basis")
        or expected_res.get("claim_holds") != cert_result.get("claim_holds")
    ):
        return _invalid("RESULT_MISMATCH", status=Status.ERROR.value, replay_verified=False, expected=expected)

    expected_supp = sorted(expected_res.get("supporting_evidence_ids", expected.get("evidence_refs", [])))
    expected_contra = sorted(expected_res.get("contradicting_evidence_ids", []))
    if sorted(supp_ids) != expected_supp or sorted(contra_ids) != expected_contra:
        return _invalid("RESULT_MISMATCH", status=Status.ERROR.value, replay_verified=False, expected=expected)

    return {
        "valid": True,
        "status": expected["status"],
        "reason_code": Reason.NONE.value,
        "replay_verified": True,
        "result": {
            "claim_holds": cert_result.get("claim_holds"),
            "entity_level": cert_result.get("entity_level"),
            "denominator_basis": cert_result.get("denominator_basis"),
            "numerator_count": cert_result.get("numerator_count"),
            "denominator_count": cert_result.get("denominator_count"),
            "replay_verified": True,
            "supporting_evidence_ids": sorted(supp_ids),
            "contradicting_evidence_ids": sorted(contra_ids),
        },
        "witness": sorted(supp_ids),
        "evidence_refs": sorted(supp_ids),
    }


def replay_certificate(snapshot_dir: Path, certificate_path: Path) -> dict[str, Any]:
    try:
        certificate = json.loads(Path(certificate_path).read_text(encoding="utf-8"))
        if not isinstance(certificate, dict):
            return _invalid(Reason.MALFORMED_CERTIFICATE.value)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return _invalid(Reason.MALFORMED_CERTIFICATE.value)
    try:
        sig = certificate.get("signature")
        if isinstance(sig, dict) and sig.get("signature_kind") == "SYNTHETIC_EXAM_TEST_ENVELOPE":
            return _replay_synthetic_exam_envelope(snapshot_dir, certificate)
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
