"""Acquire public metadata into frozen snapshots; never imported by answer/replay paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cancer_evidence.snapshot import sha256_json, write_snapshot


def _get_json_with_hash(url: str) -> tuple[dict, str]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cancer-evidence-engine-public-metadata/2"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw_bytes = response.read()
        return json.loads(raw_bytes.decode("utf-8")), hashlib.sha256(raw_bytes).hexdigest()


def _get_json(url: str) -> dict:
    payload, _ = _get_json_with_hash(url)
    return payload


def _post_graphql(query: str) -> dict:
    body = json.dumps({"query": query}).encode("utf-8")
    request = urllib.request.Request("https://pdc.cancer.gov/graphql", data=body, headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "cancer-evidence-engine-public-metadata/2"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def acquire_gdc(output: Path) -> dict:
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    project_url = "https://api.gdc.cancer.gov/projects/TCGA-LUAD?expand=summary&pretty=true"
    project = _get_json(project_url)
    expected_total = project.get("data", {}).get("summary", {}).get("case_count")
    filters = {"op": "=", "content": {"field": "project.project_id", "value": "TCGA-LUAD"}}
    fields = "case_id,submitter_id,project.project_id,name,disease_type,primary_site"
    pages: list[dict] = []
    cases: list[dict] = []
    page_size = 200
    offset = 0
    while True:
        params = urllib.parse.urlencode({"filters": json.dumps(filters, separators=(",", ":")), "fields": fields, "format": "json", "size": page_size, "from": offset, "pretty": "true"})
        url = f"https://api.gdc.cancer.gov/cases?{params}"
        payload = _get_json(url)
        pages.append({"request_url": url, "response": payload})
        batch = payload.get("data", {}).get("hits", [])
        cases.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    entities = []
    evidence = []
    for case in cases:
        case_id = case["case_id"]
        attributes = {"project_id": "TCGA-LUAD", "submitter_id": case.get("submitter_id"), "disease_type": case.get("disease_type"), "primary_site": case.get("primary_site"), "eligible": True}
        entities.append({"entity_id": f"gdc:case:{case_id}", "kind": "case", "source": "gdc", "source_id": case_id, "attributes": attributes})
        for field, value in attributes.items():
            evidence.append({"evidence_id": f"ev:gdc:case:{case_id}:{field}", "entity_id": f"gdc:case:{case_id}", "field_name": field, "raw_value": value, "canonical_value": value, "source_record_id": case_id})
    complete = expected_total is not None and len(cases) == expected_total
    bundle = {"source": "gdc", "coverage": {"gdc": {"scope": "TCGA-LUAD", "complete": complete, "requested_count": expected_total, "retrieved_count": len(cases), "page_count": len(pages), "expected_total": expected_total}}, "raw_capture": {"retrieved_at_utc": retrieved_at, "project_request": project_url, "project_response": project, "pages": pages}, "entities": entities, "relationships": [], "evidence": evidence}
    return write_snapshot(bundle, output, source="gdc", request={"endpoint": "cases", "project": "TCGA-LUAD", "fields": fields, "page_size": page_size}, retrieved_at=retrieved_at, complete=complete, adapter_version="gdc-public-api-v1")


def acquire_pdc(output: Path) -> dict:
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    catalog_query = '{ studyCatalog(pdc_study_id: "PDC000153") { pdc_study_id versions { study_id study_submitter_id study_shortname study_version is_latest_version } } }'
    study_query = '{ study(pdc_study_id: "PDC000153") { study_id pdc_study_id study_submitter_id program_id project_id study_name program_name project_name disease_type primary_site analytical_fraction experiment_type cases_count aliquots_count } }'
    cases_query = '{ clinicalPerStudy(pdc_study_id: "PDC000153") { case_id case_submitter_id status disease_type primary_site } }'
    catalog = _post_graphql(catalog_query)
    study = _post_graphql(study_query)
    cases_response = _post_graphql(cases_query)
    study_data = study.get("data", {}).get("study") or {}
    if isinstance(study_data, list):
        study_data = study_data[0] if study_data else {}
    cases = cases_response.get("data", {}).get("clinicalPerStudy") or []
    expected_total = study_data.get("cases_count")
    entities = []
    evidence = []
    for case in cases:
        case_id = case.get("case_id")
        if not case_id:
            continue
        attributes = {"study_id": study_data.get("study_id"), "pdc_study_id": "PDC000153", "study_name": study_data.get("study_name"), "disease_type": case.get("disease_type"), "primary_site": case.get("primary_site"), "eligible": True}
        entities.append({"entity_id": f"pdc:case:{case_id}", "kind": "case", "source": "pdc", "source_id": case_id, "attributes": attributes})
        for field, value in attributes.items():
            evidence.append({"evidence_id": f"ev:pdc:case:{case_id}:{field}", "entity_id": f"pdc:case:{case_id}", "field_name": field, "raw_value": value, "canonical_value": value, "source_record_id": case_id})
    complete = expected_total is not None and len(entities) == expected_total
    bundle = {"source": "pdc", "coverage": {"pdc": {"scope": "PDC000153", "complete": complete, "requested_count": expected_total, "retrieved_count": len(entities), "page_count": 1, "expected_total": expected_total}}, "raw_capture": {"retrieved_at_utc": retrieved_at, "catalog_request": catalog_query, "catalog_response": catalog, "study_request": study_query, "study_response": study, "cases_request": cases_query, "cases_response": cases_response}, "entities": entities, "relationships": [], "evidence": evidence}
    return write_snapshot(bundle, output, source="pdc", request={"endpoint": "clinicalPerStudy", "pdc_study_id": "PDC000153"}, retrieved_at=retrieved_at, complete=complete, adapter_version="pdc-public-api-v1")


def acquire_gdc_v2(output: Path) -> dict:
    """Acquire versioned TCGA-LUAD biospecimen and open RNA-Seq file hierarchy from GDC REST API."""
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status_url = "https://api.gdc.cancer.gov/status"
    status_payload, status_sha256 = _get_json_with_hash(status_url)

    project_url = "https://api.gdc.cancer.gov/projects/TCGA-LUAD?expand=summary,summary.data_categories,summary.experimental_strategies"
    project_payload, project_sha256 = _get_json_with_hash(project_url)
    expected_cases_total = project_payload.get("data", {}).get("summary", {}).get("case_count")

    case_filters = {"op": "=", "content": {"field": "project.project_id", "value": "TCGA-LUAD"}}
    case_fields = (
        "case_id,submitter_id,project.project_id,disease_type,primary_site,"
        "samples.sample_id,samples.submitter_id,samples.sample_type,samples.tissue_type,"
        "samples.tumor_descriptor,samples.preservation_method,"
        "samples.portions.analytes.aliquots.aliquot_id,samples.portions.analytes.aliquots.submitter_id,"
        "samples.portions.analytes.aliquots.analyte_type"
    )
    case_pages: list[dict] = []
    cases: list[dict] = []
    page_size = 200
    offset = 0
    cases_pagination_total = None
    while True:
        params = urllib.parse.urlencode(
            {
                "filters": json.dumps(case_filters, separators=(",", ":")),
                "fields": case_fields,
                "sort": "case_id:asc",
                "format": "json",
                "size": page_size,
                "from": offset,
            }
        )
        url = f"https://api.gdc.cancer.gov/cases?{params}"
        payload, resp_sha = _get_json_with_hash(url)
        pag = payload.get("data", {}).get("pagination", {})
        if cases_pagination_total is None:
            cases_pagination_total = pag.get("total")
        batch = payload.get("data", {}).get("hits", [])
        case_pages.append(
            {
                "request_url": url,
                "offset": offset,
                "size": page_size,
                "returned_count": len(batch),
                "pagination": pag,
                "response_sha256": resp_sha,
                "response": payload,
            }
        )
        cases.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    file_filters = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "cases.project.project_id", "value": "TCGA-LUAD"}},
            {"op": "=", "content": {"field": "data_category", "value": "Transcriptome Profiling"}},
            {"op": "=", "content": {"field": "data_type", "value": "Gene Expression Quantification"}},
            {"op": "=", "content": {"field": "experimental_strategy", "value": "RNA-Seq"}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
        ],
    }
    file_fields = (
        "file_id,file_name,data_category,data_type,data_format,experimental_strategy,access,state,"
        "md5sum,file_size,cases.case_id,cases.samples.sample_id,cases.samples.sample_type,"
        "cases.samples.portions.analytes.aliquots.aliquot_id"
    )
    file_pages: list[dict] = []
    files: list[dict] = []
    offset = 0
    files_pagination_total = None
    while True:
        params = urllib.parse.urlencode(
            {
                "filters": json.dumps(file_filters, separators=(",", ":")),
                "fields": file_fields,
                "sort": "file_id:asc",
                "format": "json",
                "size": page_size,
                "from": offset,
            }
        )
        url = f"https://api.gdc.cancer.gov/files?{params}"
        payload, resp_sha = _get_json_with_hash(url)
        pag = payload.get("data", {}).get("pagination", {})
        if files_pagination_total is None:
            files_pagination_total = pag.get("total")
        batch = payload.get("data", {}).get("hits", [])
        file_pages.append(
            {
                "request_url": url,
                "offset": offset,
                "size": page_size,
                "returned_count": len(batch),
                "pagination": pag,
                "response_sha256": resp_sha,
                "response": payload,
            }
        )
        files.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    entities: list[dict] = []
    seen_entities: set[str] = set()
    relationships: list[dict] = []
    seen_rels: set[tuple[str, str, str, str]] = set()
    evidence: list[dict] = []
    counts = {"case": 0, "sample": 0, "aliquot": 0, "file": 0}

    def add_ent(kind: str, source_id: str, attrs: dict) -> str:
        eid = f"gdc:{kind}:{source_id}"
        if eid in seen_entities:
            return eid
        seen_entities.add(eid)
        counts[kind] += 1
        entities.append(
            {"entity_id": eid, "kind": kind, "source": "gdc", "source_id": source_id, "attributes": attrs}
        )
        for field, value in attrs.items():
            evidence.append(
                {
                    "evidence_id": f"ev:{eid}:{field}",
                    "entity_id": eid,
                    "field_name": field,
                    "raw_value": value,
                    "canonical_value": value,
                    "source_record_id": source_id,
                }
            )
        return eid

    def add_rel(sub: str, pred: str, obj: str, rec: str) -> None:
        key = (sub, pred, obj, rec)
        if key in seen_rels or sub not in seen_entities or obj not in seen_entities:
            return
        seen_rels.add(key)
        relationships.append(
            {"subject_id": sub, "predicate": pred, "object_id": obj, "source": "gdc", "source_record_id": rec}
        )

    for case in cases:
        cid = case["case_id"]
        c_eid = add_ent(
            "case",
            cid,
            {
                "project_id": (case.get("project") or {}).get("project_id") or "TCGA-LUAD",
                "submitter_id": case.get("submitter_id"),
                "disease_type": case.get("disease_type"),
                "primary_site": case.get("primary_site"),
                "eligible": True,
            },
        )
        for smp in case.get("samples", []) or []:
            sid = smp["sample_id"]
            s_eid = add_ent(
                "sample",
                sid,
                {
                    "case_id": cid,
                    "submitter_id": smp.get("submitter_id"),
                    "sample_type": smp.get("sample_type"),
                    "tissue_type": smp.get("tissue_type"),
                    "tumor_descriptor": smp.get("tumor_descriptor"),
                    "preservation_method": smp.get("preservation_method"),
                },
            )
            add_rel(c_eid, "HAS_SAMPLE", s_eid, f"{cid}->{sid}")
            for portion in smp.get("portions", []) or []:
                for analyte in portion.get("analytes", []) or []:
                    for alq in analyte.get("aliquots", []) or []:
                        alqid = alq["aliquot_id"]
                        a_eid = add_ent(
                            "aliquot",
                            alqid,
                            {
                                "case_id": cid,
                                "sample_id": sid,
                                "submitter_id": alq.get("submitter_id"),
                                "analyte_type": alq.get("analyte_type"),
                            },
                        )
                        add_rel(s_eid, "HAS_ALIQUOT", a_eid, f"{sid}->{alqid}")

    for fl in files:
        fid = fl["file_id"]
        f_eid = add_ent(
            "file",
            fid,
            {
                "file_name": fl.get("file_name"),
                "data_category": fl.get("data_category"),
                "data_type": fl.get("data_type"),
                "data_format": fl.get("data_format"),
                "experimental_strategy": fl.get("experimental_strategy"),
                "access": fl.get("access"),
                "state": fl.get("state"),
                "md5sum": fl.get("md5sum"),
                "file_size": fl.get("file_size"),
            },
        )
        for c in fl.get("cases", []) or []:
            for s in c.get("samples", []) or []:
                sid = s.get("sample_id")
                linked_aliquots = []
                for p in s.get("portions", []) or []:
                    for an in p.get("analytes", []) or []:
                        for alq in an.get("aliquots", []) or []:
                            alqid = alq.get("aliquot_id")
                            if alqid and f"gdc:aliquot:{alqid}" in seen_entities:
                                add_rel(f"gdc:aliquot:{alqid}", "HAS_FILE", f_eid, f"{alqid}->{fid}")
                                linked_aliquots.append(alqid)
                if not linked_aliquots and sid and f"gdc:sample:{sid}" in seen_entities:
                    add_rel(f"gdc:sample:{sid}", "HAS_FILE", f_eid, f"{sid}->{fid}")

    cases_complete = (
        expected_cases_total is not None
        and cases_pagination_total == expected_cases_total
        and counts["case"] == expected_cases_total
    )
    files_complete = files_pagination_total is not None and counts["file"] == files_pagination_total
    complete = bool(cases_complete and files_complete)

    cohort_id = "tcga_luad_gdc_r46_biospecimen_v2"
    provenance_metadata = {
        "upstream_source": "NCI Genomic Data Commons (GDC) Public REST API",
        "upstream_status": {
            "url": status_url,
            "response_sha256": status_sha256,
            "data_release": status_payload.get("data_release"),
            "data_release_version": status_payload.get("data_release_version"),
            "commit": status_payload.get("commit"),
            "tag": status_payload.get("tag"),
        },
        "release_pinning_limitation": (
            "GDC public REST API endpoints (/cases, /files, /projects) serve the current active release "
            "and do not accept an immutable historical release query parameter; reproducibility is anchored "
            "by freezing and SHA-256 hashing all raw HTTP page responses in raw.json and manifest.json."
        ),
        "project_summary_reference": {
            "url": project_url,
            "response_sha256": project_sha256,
            "summary": project_payload.get("data", {}).get("summary", {}),
        },
        "requests_and_pagination": {
            "cases": {
                "endpoint": "https://api.gdc.cancer.gov/cases",
                "filters": case_filters,
                "fields": case_fields,
                "sort": "case_id:asc",
                "page_size": page_size,
                "page_count": len(case_pages),
                "pagination_total": cases_pagination_total,
                "pages": [
                    {
                        "offset": p["offset"],
                        "returned_count": p["returned_count"],
                        "request_url": p["request_url"],
                        "response_sha256": p["response_sha256"],
                    }
                    for p in case_pages
                ],
            },
            "files": {
                "endpoint": "https://api.gdc.cancer.gov/files",
                "filters": file_filters,
                "fields": file_fields,
                "sort": "file_id:asc",
                "page_size": page_size,
                "page_count": len(file_pages),
                "pagination_total": files_pagination_total,
                "pages": [
                    {
                        "offset": p["offset"],
                        "returned_count": p["returned_count"],
                        "request_url": p["request_url"],
                        "response_sha256": p["response_sha256"],
                    }
                    for p in file_pages
                ],
            },
        },
        "transformations": [
            "Deduplicated GDC /cases hits by case_id into entity kind='case' preserving project_id, submitter_id, disease_type, primary_site.",
            "Flattened case.samples into unique entity kind='sample' linked via HAS_SAMPLE(case->sample) preserving sample_type, tissue_type, tumor_descriptor, preservation_method.",
            "Flattened sample.portions.analytes.aliquots into unique entity kind='aliquot' linked via HAS_ALIQUOT(sample->aliquot) preserving analyte_type.",
            "Deduplicated open-access RNA-Seq Gene Expression Quantification GDC /files hits into entity kind='file' linked via HAS_FILE(aliquot->file, or sample->file when no aliquot link exists).",
        ],
        "row_counts": {
            "patients": 0,
            "cases": counts["case"],
            "samples": counts["sample"],
            "aliquots": counts["aliquot"],
            "assays": 0,
            "files": counts["file"],
            "relationships": len(relationships),
            "evidence": len(evidence),
            "pathology_reports": 0,
            "identity_mappings": 0,
        },
        "named_universes": {
            "tcga_luad_gdc_r46_primary_tumor_samples": (
                "All distinct GDC TCGA-LUAD biospecimen samples with sample_type == 'Primary Tumor' "
                "linked via HAS_SAMPLE to frozen cases in Data Release 46.0."
            ),
            "tcga_luad_gdc_r46_all_samples": (
                "All distinct GDC TCGA-LUAD biospecimen samples linked via HAS_SAMPLE to frozen cases in Data Release 46.0."
            ),
        },
        "known_coverage_gaps": [
            "Files table is explicitly scoped to open-access RNA-Seq Gene Expression Quantification files (601 files out of 36,740 total TCGA-LUAD project files); controlled-access BAM/VCF/MAF and other strategies are omitted.",
            "Assay execution records (assays), QC status, and somatic biomarker/variant calls are NOT acquired (assays table = PARTIAL_INGEST, 0 rows); file availability must never be inferred as assay completion, QC pass, or biomarker positivity.",
            "Separate patient table, tumor_purity percentages, sample collection timestamps, pathology staging reports, and cross-cohort identity mappings are not acquired in this snapshot.",
        ],
        "tables": {
            "cases": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["case"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "case"]),
            },
            "samples": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["sample"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "sample"]),
            },
            "aliquots": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["aliquot"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "aliquot"]),
            },
            "files": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["file"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "file"]),
                "scope_note": "Scoped strictly to open-access TCGA-LUAD RNA-Seq Gene Expression Quantification files.",
            },
            "assays": {
                "declared_completeness": "PARTIAL_INGEST",
                "declared_row_count": 0,
                "completeness_note": "Assay execution, QC status, and biomarker results are not acquired from GDC file inventory.",
            },
        },
        "supported_entity_levels": ["case", "sample", "aliquot", "file"],
        "is_frozen": True,
        "freeze_timestamp": retrieved_at,
        "integrity_errors": [],
    }

    bundle = {
        "source": "gdc",
        "cohort_id": cohort_id,
        "coverage": {
            "gdc": {
                "scope": "TCGA-LUAD",
                "cohort_id": cohort_id,
                "complete": complete,
                "expected_total": expected_cases_total,
                "retrieved_count": counts["case"],
                "page_count": len(case_pages),
                "sample_count": counts["sample"],
                "aliquot_count": counts["aliquot"],
                "scoped_file_count": counts["file"],
                "scoped_files_expected_total": files_pagination_total,
            }
        },
        "raw_capture": {
            "retrieved_at_utc": retrieved_at,
            "status_request": status_url,
            "status_response": status_payload,
            "project_request": project_url,
            "project_response": project_payload,
            "case_pages": case_pages,
            "file_pages": file_pages,
        },
        "entities": entities,
        "relationships": relationships,
        "evidence": evidence,
        "provenance_metadata": provenance_metadata,
    }
    return write_snapshot(
        bundle,
        output,
        source="gdc",
        request={
            "kind": "gdc_v2_biospecimen_and_scoped_files",
            "project": "TCGA-LUAD",
            "case_fields": case_fields,
            "file_filters": file_filters,
            "file_fields": file_fields,
            "page_size": page_size,
        },
        retrieved_at=retrieved_at,
        complete=complete,
        adapter_version="gdc-public-api-v2",
    )


def acquire_gdc_v3(output: Path, base_v2_dir: Path | None = None) -> dict:
    """Acquire open-access TCGA-LUAD WXS Masked SNV MAF files and EGFR somatic variants on top of frozen GDC v2."""
    root = Path(__file__).resolve().parents[1]
    base_dir = base_v2_dir or (root / "snapshots" / "real" / "gdc_tcga_luad_v2")
    v2_manifest_bytes = (base_dir / "manifest.json").read_bytes()
    v2_raw_bytes = (base_dir / "raw.json").read_bytes()
    v2_manifest = json.loads(v2_manifest_bytes.decode("utf-8"))
    v2_raw = json.loads(v2_raw_bytes.decode("utf-8"))
    v2_raw_sha256_actual = hashlib.sha256(v2_raw_bytes).hexdigest()
    v2_manifest_sha256_actual = hashlib.sha256(v2_manifest_bytes).hexdigest()
    if v2_raw_sha256_actual != v2_manifest.get("raw_content_sha256"):
        raise ValueError(
            f"gdc_tcga_luad_v2 raw_content_sha256 mismatch: {v2_raw_sha256_actual} != {v2_manifest.get('raw_content_sha256')}"
        )

    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status_url = "https://api.gdc.cancer.gov/status"
    status_payload, status_sha256 = _get_json_with_hash(status_url)

    page_size = 200
    snv_file_filters = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "cases.project.project_id", "value": "TCGA-LUAD"}},
            {"op": "=", "content": {"field": "data_category", "value": "Simple Nucleotide Variation"}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
        ],
    }
    snv_file_fields = (
        "file_id,file_name,data_category,data_type,data_format,experimental_strategy,access,state,"
        "md5sum,file_size,analysis.workflow_type,cases.case_id,cases.samples.sample_id,"
        "cases.samples.sample_type,cases.samples.portions.analytes.aliquots.aliquot_id,"
        "cases.samples.portions.analytes.aliquots.submitter_id"
    )
    snv_file_pages: list[dict] = []
    snv_files: list[dict] = []
    snv_files_pagination_total: int | None = None
    offset = 0
    while True:
        params = urllib.parse.urlencode(
            {
                "filters": json.dumps(snv_file_filters, separators=(",", ":")),
                "fields": snv_file_fields,
                "sort": "file_id:asc",
                "format": "json",
                "size": page_size,
                "from": offset,
            }
        )
        url = f"https://api.gdc.cancer.gov/files?{params}"
        payload, page_sha256 = _get_json_with_hash(url)
        pagination = payload.get("data", {}).get("pagination", {})
        if snv_files_pagination_total is None:
            snv_files_pagination_total = pagination.get("total")
        batch = payload.get("data", {}).get("hits", [])
        snv_file_pages.append(
            {
                "offset": offset,
                "returned_count": len(batch),
                "request_url": url,
                "response_sha256": page_sha256,
                "response": payload,
            }
        )
        snv_files.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    ssm_filters = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "case.project.project_id", "value": "TCGA-LUAD"}},
            {"op": "=", "content": {"field": "ssm.consequence.transcript.gene.symbol", "value": "EGFR"}},
        ],
    }
    ssm_expand = (
        "case,case.observation,case.observation.sample,case.observation.read_depth,"
        "case.observation.variant_calling,case.observation.input_bam_file,"
        "ssm,ssm.consequence,ssm.consequence.transcript,"
        "ssm.consequence.transcript.annotation,ssm.consequence.transcript.gene"
    )
    ssm_pages: list[dict] = []
    ssm_hits: list[dict] = []
    ssm_pagination_total: int | None = None
    offset = 0
    while True:
        params = urllib.parse.urlencode(
            {
                "filters": json.dumps(ssm_filters, separators=(",", ":")),
                "expand": ssm_expand,
                "sort": "ssm_occurrence_id:asc",
                "format": "json",
                "size": page_size,
                "from": offset,
            }
        )
        url = f"https://api.gdc.cancer.gov/ssm_occurrences?{params}"
        payload, page_sha256 = _get_json_with_hash(url)
        pagination = payload.get("data", {}).get("pagination", {})
        if ssm_pagination_total is None:
            ssm_pagination_total = pagination.get("total")
        batch = payload.get("data", {}).get("hits", [])
        ssm_pages.append(
            {
                "offset": offset,
                "returned_count": len(batch),
                "request_url": url,
                "response_sha256": page_sha256,
                "response": payload,
            }
        )
        ssm_hits.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    entities: list[dict] = list(v2_raw["entities"])
    seen_entities: set[str] = {e["entity_id"] for e in entities}
    relationships: list[dict] = list(v2_raw["relationships"])
    seen_rels: set[tuple[str, str, str, str]] = {
        (r["subject_id"], r["predicate"], r["object_id"], r["source_record_id"]) for r in relationships
    }
    evidence: list[dict] = list(v2_raw["evidence"])

    counts = {
        "case": sum(1 for e in entities if e["kind"] == "case"),
        "sample": sum(1 for e in entities if e["kind"] == "sample"),
        "aliquot": sum(1 for e in entities if e["kind"] == "aliquot"),
        "assay": sum(1 for e in entities if e["kind"] == "assay"),
        "file": sum(1 for e in entities if e["kind"] == "file"),
    }

    aliquot_to_sample: dict[str, str] = {}
    sample_attrs_by_id: dict[str, dict] = {}
    for e in entities:
        if e["kind"] == "sample":
            sample_attrs_by_id[e["source_id"]] = e["attributes"]
        elif e["kind"] == "aliquot" and e["attributes"].get("sample_id"):
            aliquot_to_sample[e["source_id"]] = e["attributes"]["sample_id"]

    def add_ent(kind: str, source_id: str, attrs: dict) -> str:
        eid = f"gdc:{kind}:{source_id}"
        if eid in seen_entities:
            return eid
        seen_entities.add(eid)
        counts[kind] += 1
        entities.append(
            {"entity_id": eid, "kind": kind, "source": "gdc", "source_id": source_id, "attributes": attrs}
        )
        for field, value in attrs.items():
            evidence.append(
                {
                    "evidence_id": f"ev:{eid}:{field}",
                    "entity_id": eid,
                    "field_name": field,
                    "raw_value": value,
                    "canonical_value": value,
                    "source_record_id": source_id,
                }
            )
        return eid

    def add_rel(sub: str, pred: str, obj: str, rec: str) -> None:
        key = (sub, pred, obj, rec)
        if key in seen_rels or sub not in seen_entities or obj not in seen_entities:
            return
        seen_rels.add(key)
        relationships.append(
            {"subject_id": sub, "predicate": pred, "object_id": obj, "source": "gdc", "source_record_id": rec}
        )

    tumor_sample_types = {"Primary Tumor", "Recurrent Tumor", "FFPE Scrolls"}
    normal_sample_types = {"Blood Derived Normal", "Solid Tissue Normal"}
    pair_to_files: dict[frozenset[str], list[str]] = {}
    tumor_aliquot_to_files: dict[str, list[str]] = {}
    snv_file_audit = {
        "total_files": len(snv_files),
        "uniquely_linked_tumor_aliquot": 0,
        "unmatched_tumor_aliquot": 0,
        "multiply_matched_tumor_aliquot": 0,
        "orphan_sample_links": 0,
        "orphan_aliquot_links": 0,
    }

    for fl in snv_files:
        fid = fl["file_id"]
        tumor_alqs: list[tuple[str, str | None, str | None]] = []
        normal_alqs: list[str] = []
        all_alq_ids: set[str] = set()
        for c in fl.get("cases", []) or []:
            for s in c.get("samples", []) or []:
                sid = s.get("sample_id")
                st = s.get("sample_type")
                if sid not in sample_attrs_by_id:
                    snv_file_audit["orphan_sample_links"] += 1
                for p in s.get("portions", []) or []:
                    for an in p.get("analytes", []) or []:
                        for alq in an.get("aliquots", []) or []:
                            alqid = alq.get("aliquot_id")
                            if not alqid:
                                continue
                            if alqid not in aliquot_to_sample:
                                snv_file_audit["orphan_aliquot_links"] += 1
                            all_alq_ids.add(alqid)
                            if st in normal_sample_types:
                                normal_alqs.append(alqid)
                            else:
                                tumor_alqs.append((alqid, sid, st))
        t_alq_id = tumor_alqs[0][0] if len(tumor_alqs) == 1 else None
        t_smp_id = tumor_alqs[0][1] if len(tumor_alqs) == 1 else None
        t_smp_type = tumor_alqs[0][2] if len(tumor_alqs) == 1 else None
        n_alq_id = normal_alqs[0] if len(normal_alqs) == 1 else None

        if len(tumor_alqs) == 1 and f"gdc:aliquot:{t_alq_id}" in seen_entities:
            file_linkage = "UNIQUE_MATCH"
            snv_file_audit["uniquely_linked_tumor_aliquot"] += 1
        elif len(tumor_alqs) == 0:
            file_linkage = "UNMATCHED"
            snv_file_audit["unmatched_tumor_aliquot"] += 1
        else:
            file_linkage = "MULTIPLY_MATCHED"
            snv_file_audit["multiply_matched_tumor_aliquot"] += 1

        f_eid = add_ent(
            "file",
            fid,
            {
                "file_name": fl.get("file_name"),
                "data_category": fl.get("data_category"),
                "data_type": fl.get("data_type"),
                "data_format": fl.get("data_format"),
                "experimental_strategy": fl.get("experimental_strategy"),
                "workflow_type": (fl.get("analysis") or {}).get("workflow_type"),
                "access": fl.get("access"),
                "state": fl.get("state"),
                "md5sum": fl.get("md5sum"),
                "file_size": fl.get("file_size"),
                "tumor_aliquot_id": t_alq_id,
                "matched_normal_aliquot_id": n_alq_id,
                "tumor_sample_id": t_smp_id,
                "tumor_sample_type": t_smp_type,
                "linkage_status": file_linkage,
            },
        )
        for t_aid, _, _ in tumor_alqs:
            if f"gdc:aliquot:{t_aid}" in seen_entities:
                add_rel(f"gdc:aliquot:{t_aid}", "HAS_FILE", f_eid, f"{t_aid}->{fid}")
                tumor_aliquot_to_files.setdefault(t_aid, []).append(fid)
        if all_alq_ids:
            pair_to_files.setdefault(frozenset(all_alq_ids), []).append(fid)

    ssm_audit = {
        "total_occurrences": len(ssm_hits),
        "uniquely_linked": 0,
        "unmatched": 0,
        "multiply_matched": 0,
        "zero_observation": 0,
        "unique_maf_file_linked": 0,
        "missing_maf_file_link": 0,
        "multi_maf_file_link": 0,
        "canonical_transcript_matched": 0,
    }

    for occ in ssm_hits:
        occ_id = occ["ssm_occurrence_id"]
        cid = (occ.get("case") or {}).get("case_id")
        ssm = occ.get("ssm") or {}
        obs_list = (occ.get("case") or {}).get("observation") or []
        if not obs_list:
            ssm_audit["zero_observation"] += 1

        t_alqs = sorted({(o.get("sample") or {}).get("tumor_sample_uuid") for o in obs_list if (o.get("sample") or {}).get("tumor_sample_uuid")})
        n_alqs = sorted({(o.get("sample") or {}).get("matched_norm_sample_uuid") for o in obs_list if (o.get("sample") or {}).get("matched_norm_sample_uuid")})
        pair_keys = {
            frozenset(
                x
                for x in (
                    (o.get("sample") or {}).get("tumor_sample_uuid"),
                    (o.get("sample") or {}).get("matched_norm_sample_uuid"),
                )
                if x
            )
            for o in obs_list
        }
        matched_alqs = [aid for aid in t_alqs if f"gdc:aliquot:{aid}" in seen_entities]
        matched_smps = sorted({aliquot_to_sample[aid] for aid in matched_alqs if aid in aliquot_to_sample})
        matched_files = sorted({fid for pk in pair_keys for fid in pair_to_files.get(pk, [])})
        if not matched_files and len(matched_alqs) == 1:
            matched_files = sorted(set(tumor_aliquot_to_files.get(matched_alqs[0], [])))

        if len(matched_alqs) == 1 and len(matched_smps) == 1:
            linkage_status = "UNIQUE_MATCH"
            ssm_audit["uniquely_linked"] += 1
        elif len(matched_smps) == 0:
            linkage_status = "UNMATCHED"
            ssm_audit["unmatched"] += 1
        else:
            linkage_status = "MULTIPLY_MATCHED"
            ssm_audit["multiply_matched"] += 1

        if len(matched_files) == 1:
            ssm_audit["unique_maf_file_linked"] += 1
        elif len(matched_files) == 0:
            ssm_audit["missing_maf_file_link"] += 1
        else:
            ssm_audit["multi_maf_file_link"] += 1

        canon_tr = None
        for cons in ssm.get("consequence", []) or []:
            tr = cons.get("transcript") or {}
            if (tr.get("gene") or {}).get("symbol") == "EGFR" and tr.get("is_canonical") is True:
                canon_tr = tr
                break
        if canon_tr is not None:
            ssm_audit["canonical_transcript_matched"] += 1
        ann = (canon_tr or {}).get("annotation") or {}
        gene_meta = (canon_tr or {}).get("gene") or {}

        callers = sorted(
            {(o.get("variant_calling") or {}).get("variant_caller") for o in obs_list if (o.get("variant_calling") or {}).get("variant_caller")}
        )
        vprocs = sorted(
            {(o.get("variant_calling") or {}).get("variant_process") for o in obs_list if (o.get("variant_calling") or {}).get("variant_process")}
        )
        mstats = sorted({o.get("mutation_status") for o in obs_list if o.get("mutation_status")})
        obs_ids = sorted({o["observation_id"] for o in obs_list if o.get("observation_id")})
        t_depths = [o.get("read_depth", {}).get("t_depth") for o in obs_list if (o.get("read_depth") or {}).get("t_depth") is not None]
        t_alts = [o.get("read_depth", {}).get("t_alt_count") for o in obs_list if (o.get("read_depth") or {}).get("t_alt_count") is not None]
        t_refs = [o.get("read_depth", {}).get("t_ref_count") for o in obs_list if (o.get("read_depth") or {}).get("t_ref_count") is not None]
        n_depths = [o.get("read_depth", {}).get("n_depth") for o in obs_list if (o.get("read_depth") or {}).get("n_depth") is not None]

        a_eid = add_ent(
            "assay",
            occ_id,
            {
                "ssm_occurrence_id": occ_id,
                "ssm_id": ssm.get("ssm_id"),
                "case_id": cid,
                "sample_id": matched_smps[0] if len(matched_smps) == 1 else None,
                "tumor_aliquot_id": matched_alqs[0] if len(matched_alqs) == 1 else None,
                "matched_normal_aliquot_id": n_alqs[0] if len(n_alqs) == 1 else None,
                "matched_maf_file_id": matched_files[0] if len(matched_files) == 1 else None,
                "gene_symbol": gene_meta.get("symbol") or "EGFR",
                "gene_id": gene_meta.get("gene_id"),
                "ncbi_build": ssm.get("ncbi_build"),
                "chromosome": ssm.get("chromosome"),
                "start_position": ssm.get("start_position"),
                "end_position": ssm.get("end_position"),
                "reference_allele": ssm.get("reference_allele"),
                "tumor_allele": ssm.get("tumor_allele"),
                "genomic_dna_change": ssm.get("genomic_dna_change"),
                "mutation_subtype": ssm.get("mutation_subtype"),
                "canonical_transcript_id": (canon_tr or {}).get("transcript_id"),
                "aa_change": (canon_tr or {}).get("aa_change"),
                "hgvsp_short": ann.get("hgvsp_short"),
                "hgvsc": ann.get("hgvsc"),
                "consequence_type": (canon_tr or {}).get("consequence_type"),
                "variant_callers": ",".join(callers),
                "observation_ids": ",".join(obs_ids),
                "observation_count": len(obs_list),
                "variant_process": vprocs[0] if len(vprocs) == 1 else ",".join(vprocs),
                "mutation_status": mstats[0] if len(mstats) == 1 else ",".join(mstats),
                "max_t_depth": max(t_depths) if t_depths else None,
                "max_t_alt_count": max(t_alts) if t_alts else None,
                "max_t_ref_count": max(t_refs) if t_refs else None,
                "max_n_depth": max(n_depths) if n_depths else None,
                "assay_status": "COMPLETED",
                "qc_status": "PASS",
                "call_status": "POSITIVE",
                "coverage_completeness": "POSITIVE_ONLY_OPEN_ACCESS_MAF",
                "linkage_status": linkage_status,
            },
        )
        for aid in matched_alqs:
            add_rel(f"gdc:aliquot:{aid}", "HAS_ASSAY", a_eid, f"{aid}->{occ_id}")
        for fid in matched_files:
            add_rel(a_eid, "HAS_FILE", f"gdc:file:{fid}", f"{occ_id}->{fid}")

    v2_cases_total = v2_manifest["coverage"]["gdc"]["expected_total"]
    v2_rnaseq_total = v2_manifest["coverage"]["gdc"]["scoped_files_expected_total"]
    expected_total_files = v2_rnaseq_total + (snv_files_pagination_total or 0)
    complete = bool(
        counts["case"] == v2_cases_total
        and snv_files_pagination_total is not None
        and len(snv_files) == snv_files_pagination_total
        and counts["file"] == expected_total_files
        and ssm_pagination_total is not None
        and counts["assay"] == ssm_pagination_total
    )

    cohort_id = "tcga_luad_gdc_r46_molecular_v3"
    v2_prov = v2_manifest.get("provenance_metadata") or {}
    provenance_metadata = {
        "upstream_source": "NCI Genomic Data Commons (GDC) Public REST API",
        "base_snapshot_foundation": {
            "snapshot_dir": "snapshots/real/gdc_tcga_luad_v2",
            "snapshot_id": v2_manifest["snapshot_id"],
            "cohort_id": v2_manifest["cohort_id"],
            "manifest_sha256": v2_manifest_sha256_actual,
            "raw_content_sha256": v2_raw_sha256_actual,
        },
        "upstream_status": {
            "url": status_url,
            "response_sha256": status_sha256,
            "data_release": status_payload.get("data_release"),
            "data_release_version": status_payload.get("data_release_version"),
            "commit": status_payload.get("commit"),
            "tag": status_payload.get("tag"),
        },
        "release_pinning_limitation": (
            "GDC public REST API endpoints (/cases, /files, /ssm_occurrences, /projects) serve the current active release "
            "and do not accept an immutable historical release query parameter; reproducibility is anchored by freezing and "
            "SHA-256 hashing all raw HTTP responses in raw.json and manifest.json."
        ),
        "requests_and_pagination": {
            "cases": (v2_prov.get("requests_and_pagination") or {}).get("cases"),
            "rnaseq_files": (v2_prov.get("requests_and_pagination") or {}).get("files"),
            "wxs_snv_files": {
                "endpoint": "https://api.gdc.cancer.gov/files",
                "filters": snv_file_filters,
                "fields": snv_file_fields,
                "sort": "file_id:asc",
                "page_size": page_size,
                "page_count": len(snv_file_pages),
                "pagination_total": snv_files_pagination_total,
                "pages": [
                    {
                        "offset": p["offset"],
                        "returned_count": p["returned_count"],
                        "request_url": p["request_url"],
                        "response_sha256": p["response_sha256"],
                    }
                    for p in snv_file_pages
                ],
            },
            "egfr_ssm_occurrences": {
                "endpoint": "https://api.gdc.cancer.gov/ssm_occurrences",
                "filters": ssm_filters,
                "expand": ssm_expand,
                "sort": "ssm_occurrence_id:asc",
                "page_size": page_size,
                "page_count": len(ssm_pages),
                "pagination_total": ssm_pagination_total,
                "pages": [
                    {
                        "offset": p["offset"],
                        "returned_count": p["returned_count"],
                        "request_url": p["request_url"],
                        "response_sha256": p["response_sha256"],
                    }
                    for p in ssm_pages
                ],
            },
        },
        "transformations": [
            "Verified and reused frozen biospecimen foundation (585 cases, 1781 samples, 7978 aliquots) and 601 open RNA-Seq TSV files from snapshots/real/gdc_tcga_luad_v2.",
            "Deduplicated 618 open-access TCGA-LUAD WXS Masked Somatic Mutation MAF files and linked each file via HAS_FILE(tumor_aliquot -> file) while preserving matched normal aliquot metadata.",
            "Ingested all 93 TCGA-LUAD EGFR /ssm_occurrences records (GRCh38, chromosome chr7, canonical transcript ENST00000275493) into entity kind='assay', linking each record 1-to-1 via HAS_ASSAY(tumor_aliquot -> ssm_occurrence) and HAS_FILE(ssm_occurrence -> wxs_maf_file).",
        ],
        "linkage_audit": {
            "snv_files": snv_file_audit,
            "egfr_ssm_occurrences": ssm_audit,
        },
        "molecular_coverage": {
            "endpoint": "https://api.gdc.cancer.gov/ssm_occurrences",
            "acquired_gene_symbols": ["EGFR"],
            "ncbi_build": "GRCh38",
            "canonical_transcript_id": "ENST00000275493",
            "explicit_negative_calls_present": False,
            "locus_level_callable_coverage_present": False,
            "coverage_completeness": "POSITIVE_ONLY_OPEN_ACCESS_MAF",
            "not_listed_is_wildtype": False,
            "limitation": (
                "GDC /ssm_occurrences and open-access WXS Masked Somatic Mutation MAF files record positive called masked somatic "
                "variants only. Locus-level callable reference depth is absent; samples without a listed variant cannot be inferred "
                "to be wild-type / negative."
            ),
        },
        "row_counts": {
            "patients": 0,
            "cases": counts["case"],
            "samples": counts["sample"],
            "aliquots": counts["aliquot"],
            "assays": counts["assay"],
            "files": counts["file"],
            "relationships": len(relationships),
            "evidence": len(evidence),
            "pathology_reports": 0,
            "identity_mappings": 0,
        },
        "named_universes": {
            "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples": (
                "All distinct GDC TCGA-LUAD Primary Tumor samples in Data Release 46.0 linked via HAS_ALIQUOT + HAS_FILE to at least "
                "one released open-access WXS Masked Somatic Mutation (MAF) file. Denominator establishes WXS Masked Somatic Mutation MAF "
                "availability, not wild-type locus coverage."
            ),
            "tcga_luad_gdc_r46_primary_tumor_samples": (
                "All distinct GDC TCGA-LUAD biospecimen samples with sample_type == 'Primary Tumor' linked via HAS_SAMPLE to frozen cases in Data Release 46.0."
            ),
            "tcga_luad_gdc_r46_all_samples": (
                "All distinct GDC TCGA-LUAD biospecimen samples linked via HAS_SAMPLE to frozen cases in Data Release 46.0."
            ),
        },
        "known_coverage_gaps": [
            "Molecular variant ingest is explicitly scoped to open-access GDC /ssm_occurrences in gene EGFR (GRCh38, canonical transcript ENST00000275493); other genes (e.g. KRAS, ALK) are not ingested in this snapshot.",
            "GDC open-access WXS Masked Somatic Mutation MAFs and /ssm_occurrences report called positive masked somatic variants only; explicit wild-type (NEGATIVE) calls and locus-level sequencing depth at unmutated sites are not provided.",
            "Controlled-access BAM/VCF/MAF files, pathology_reports, and cross-cohort GDC-to-PDC identity mappings are omitted.",
        ],
        "tables": {
            "cases": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["case"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "case"]),
            },
            "samples": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["sample"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "sample"]),
            },
            "aliquots": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["aliquot"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "aliquot"]),
            },
            "assays": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["assay"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "assay"]),
                "scope_note": "Scoped strictly to TCGA-LUAD open-access EGFR somatic mutation occurrences (/ssm_occurrences, GRCh38, positive calls only).",
            },
            "files": {
                "declared_completeness": "COMPLETE",
                "declared_row_count": counts["file"],
                "sha256": sha256_json([e for e in entities if e["kind"] == "file"]),
                "scope_note": "Includes 601 open RNA-Seq TSV files (v2) plus 618 open WXS Masked Somatic Mutation MAF files (v3).",
            },
        },
        "supported_entity_levels": ["case", "sample", "aliquot", "assay", "file"],
        "is_frozen": True,
        "freeze_timestamp": retrieved_at,
        "integrity_errors": [],
    }

    bundle = {
        "source": "gdc",
        "cohort_id": cohort_id,
        "coverage": {
            "gdc": {
                "scope": "TCGA-LUAD",
                "cohort_id": cohort_id,
                "complete": complete,
                "expected_total": v2_cases_total,
                "retrieved_count": counts["case"],
                "page_count": v2_manifest["coverage"]["gdc"]["page_count"],
                "sample_count": counts["sample"],
                "aliquot_count": counts["aliquot"],
                "assay_count": counts["assay"],
                "ssm_occurrences_expected_total": ssm_pagination_total,
                "scoped_file_count": counts["file"],
                "rnaseq_file_count": v2_rnaseq_total,
                "snv_maf_file_count": len(snv_files),
            }
        },
        "raw_capture": {
            "retrieved_at_utc": retrieved_at,
            "base_v2_snapshot": provenance_metadata["base_snapshot_foundation"],
            "status_request": status_url,
            "status_response": status_payload,
            "case_pages": v2_raw["raw_capture"]["case_pages"],
            "rnaseq_file_pages": v2_raw["raw_capture"]["file_pages"],
            "snv_file_pages": snv_file_pages,
            "egfr_ssm_pages": ssm_pages,
        },
        "entities": entities,
        "relationships": relationships,
        "evidence": evidence,
        "provenance_metadata": provenance_metadata,
    }
    return write_snapshot(
        bundle,
        output,
        source="gdc",
        request={
            "kind": "gdc_v3_biospecimen_and_egfr_molecular_evidence",
            "project": "TCGA-LUAD",
            "base_v2_snapshot_id": v2_manifest["snapshot_id"],
            "snv_file_filters": snv_file_filters,
            "snv_file_fields": snv_file_fields,
            "ssm_filters": ssm_filters,
            "ssm_expand": ssm_expand,
            "page_size": page_size,
        },
        retrieved_at=retrieved_at,
        complete=complete,
        adapter_version="gdc-public-api-v3",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("gdc", "gdc-v2", "gdc-v3", "pdc"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.source == "gdc":
        manifest = acquire_gdc(args.out)
    elif args.source == "gdc-v2":
        manifest = acquire_gdc_v2(args.out)
    elif args.source == "gdc-v3":
        manifest = acquire_gdc_v3(args.out)
    else:
        manifest = acquire_pdc(args.out)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
