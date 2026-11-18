"""Acquire public metadata into frozen snapshots; never imported by answer/replay paths."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cancer_evidence.snapshot import write_snapshot


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cancer-evidence-engine-public-metadata/2"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("gdc", "pdc"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = acquire_gdc(args.out) if args.source == "gdc" else acquire_pdc(args.out)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
