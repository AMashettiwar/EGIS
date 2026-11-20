from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from typing import Any
import urllib.parse
import urllib.request

from .adapters import AdapterPayload
from .models import MappingStatus, QCStatus, ReportStatus

# 16-gene Lung Cancer + ADC Target & Resistance Biomarker Panel
LUNG_ADC_GENE_PANEL: tuple[str, ...] = (
    # Major Lung & Solid-Tumor ADC Surface Targets
    "TACSTD2",  # TROP2 (Dato-DXd, sacituzumab govitecan)
    "ERBB2",    # HER2 (T-DXd)
    "MET",      # c-MET (telisotuzumab vedotin)
    "ERBB3",    # HER3 (patritumab deruxtecan)
    "EGFR",     # EGFR (TKIs + EGFRxHER3 / EGFRxMET ADCs)
    "CD276",    # B7-H3 (ifinatamab deruxtecan)
    "CEACAM5",  # CEACAM5 (tusamitamab ravtansine)
    "NECTIN4",  # Nectin-4 (enfortumab vedotin)
    "FOLR1",    # FR-alpha
    "MUC1",     # MUC1
    # Lung Drivers, Proliferation & Payload Context
    "KRAS",
    "TP53",
    "STK11",
    "KEAP1",
    "MKI67",    # Ki-67 proliferation
    "TOP1",     # Topoisomerase-I (DXd / SN-38 ADC payload target)
)

PANEL_SET = frozenset(LUNG_ADC_GENE_PANEL)
GDC_BASE = "https://api.gdc.cancer.gov"
PDC_GRAPHQL = "https://pdc.cancer.gov/graphql"
USER_AGENT = "EGIS-multimodal/1.0"

CONTROL_CASE_SUBSTRINGS = ("internal reference", " ir", "pooled sample", "control")


def _http_get_json(url: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pdc_graphql(query: str, timeout: int = 90) -> dict[str, Any]:
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        PDC_GRAPHQL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_control_case(submitter_id: str) -> bool:
    low = submitter_id.strip().lower()
    return any(tok in low for tok in CONTROL_CASE_SUBSTRINGS)


class GdcClinicalAdapter:
    """Ingests CPTAC-3 Lung clinical & biospecimen hierarchy from NCI GDC."""

    modality_name = "clinical"
    source_name = "gdc"

    def ingest(self, *, project_id: str = "CPTAC-3", primary_site: str = "Bronchus and lung") -> AdapterPayload:
        filters = json.dumps({
            "op": "and",
            "content": [
                {"op": "=", "content": {"field": "project.project_id", "value": project_id}},
                {"op": "=", "content": {"field": "primary_site", "value": primary_site}},
            ],
        })
        fields = ",".join([
            "case_id",
            "submitter_id",
            "disease_type",
            "primary_site",
            "demographic.gender",
            "demographic.vital_status",
            "diagnoses.primary_diagnosis",
            "diagnoses.ajcc_pathologic_stage",
            "diagnoses.age_at_diagnosis",
            "diagnoses.days_to_diagnosis",
            "samples.sample_id",
            "samples.submitter_id",
            "samples.sample_type",
            "samples.days_to_collection",
            "samples.tissue_type",
        ])

        hits: list[dict[str, Any]] = []
        offset = 0
        page_size = 200
        while True:
            url = f"{GDC_BASE}/cases?" + urllib.parse.urlencode({
                "filters": filters,
                "fields": fields,
                "size": page_size,
                "from": offset,
                "sort": "case_id:asc",
            })
            data = _http_get_json(url)
            batch = data["data"]["hits"]
            hits.extend(batch)
            total = int(data["data"]["pagination"]["total"])
            offset += len(batch)
            if not batch or offset >= total:
                break

        patients: list[dict[str, Any]] = []
        crosswalk: list[dict[str, Any]] = []
        specimens: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []

        for case in hits:
            pid = str(case["submitter_id"])
            gdc_case_id = str(case["case_id"])
            demo = case.get("demographic") or {}
            diagnoses = case.get("diagnoses") or []
            diag = diagnoses[0] if diagnoses else {}

            stage = diag.get("ajcc_pathologic_stage")
            age_days = diag.get("age_at_diagnosis")
            age_days_int = int(round(float(age_days))) if age_days is not None else None

            patients.append({
                "canonical_patient_id": pid,
                "source": "gdc",
                "source_patient_id": gdc_case_id,
                "is_control_or_pool": _is_control_case(pid),
                "disease_type": case.get("disease_type"),
                "primary_site": case.get("primary_site"),
                "ajcc_stage": stage,
                "gender": demo.get("gender"),
                "vital_status": demo.get("vital_status"),
                "age_at_diagnosis_days": age_days_int,
            })
            crosswalk.append({
                "canonical_patient_id": pid,
                "source": "gdc",
                "source_patient_id": gdc_case_id,
                "mapping_status": MappingStatus.APPROVED.value,
                "authority": "NCI-GDC",
                "evidence_note": f"GDC submitter_id={pid} -> case_id={gdc_case_id}",
            })

            for sample in case.get("samples") or []:
                sample_sub = str(sample.get("submitter_id") or "")
                sample_id = str(sample.get("sample_id") or "")
                sample_type = str(sample.get("sample_type") or "")
                day_col = sample.get("days_to_collection")
                day_int = int(day_col) if day_col is not None else None

                specimens.append({
                    "specimen_id": f"gdc:{sample_id}",
                    "canonical_patient_id": pid,
                    "episode_id": None,
                    "source": "gdc",
                    "source_specimen_id": sample_sub,
                    "sample_type": sample_type,
                    "collection_day_offset": day_int,
                    "report_status": ReportStatus.ACTIVE_FINAL.value,
                })
                runs.append({
                    "run_id": f"gdc:clinical:{sample_id}",
                    "modality": "clinical",
                    "canonical_patient_id": pid,
                    "specimen_id": f"gdc:{sample_id}",
                    "source_specimen_id": sample_sub,
                    "episode_id": None,
                    "source": "gdc",
                    "source_run_id": gdc_case_id,
                    "qc_status": QCStatus.PASS.value,
                    "report_status": ReportStatus.ACTIVE_FINAL.value,
                    "sample_type": sample_type,
                    "collection_day_offset": day_int,
                    "pipeline_version": "gdc-clinical-v1",
                    "artifact_uri": f"https://portal.gdc.cancer.gov/cases/{gdc_case_id}",
                    "artifact_sha256": None,
                    "features_json": json.dumps(
                        {
                            "disease_type": case.get("disease_type"),
                            "primary_diagnosis": diag.get("primary_diagnosis"),
                            "ajcc_stage": stage,
                            "gender": demo.get("gender"),
                            "vital_status": demo.get("vital_status"),
                            "age_at_diagnosis_days": age_days_int,
                        },
                        sort_keys=True,
                    ),
                })

        return AdapterPayload(
            modality_name="clinical",
            source_name="gdc",
            patients=patients,
            identity_crosswalk=crosswalk,
            specimens=specimens,
            modality_runs=runs,
            source_provenance={
                "endpoint": f"{GDC_BASE}/cases",
                "project_id": project_id,
                "primary_site": primary_site,
                "retrieved_cases": len(hits),
            },
        )


def _fetch_and_parse_rna_file(file_meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, float], dict[str, Any]]:
    """Download one open GDC STAR-Counts TSV and extract panel TPM + real QC metrics."""
    import time

    file_id = str(file_meta["file_id"])
    url = f"{GDC_BASE}/data/{file_id}"
    raw_bytes: bytes | None = None
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw_bytes = resp.read()
            break
        except Exception as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    if raw_bytes is None:
        raise RuntimeError(f"Failed downloading GDC RNA file {file_id}: {last_err}") from last_err

    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    actual_md5 = hashlib.md5(raw_bytes).hexdigest()
    expected_md5 = file_meta.get("md5sum")
    md5_ok = (expected_md5 is None) or (actual_md5 == expected_md5)

    panel_tpm: dict[str, float] = {}
    qc_counts: dict[str, int] = {}
    mapped_gene_reads = 0
    header_idx: dict[str, int] | None = None

    for line in raw_bytes.decode("utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if header_idx is None:
            header_idx = {col: idx for idx, col in enumerate(parts)}
            continue
        gene_id = parts[header_idx["gene_id"]]
        if gene_id in ("N_unmapped", "N_multimapping", "N_noFeature", "N_ambiguous"):
            qc_counts[gene_id] = int(parts[header_idx["unstranded"]])
            continue
        mapped_gene_reads += int(parts[header_idx["unstranded"]])
        gene_name = parts[header_idx["gene_name"]]
        if gene_name in PANEL_SET:
            # Prefer highest TPM if duplicate PAR/patch gene entries exist
            tpm_val = float(parts[header_idx["tpm_unstranded"]])
            if gene_name not in panel_tpm or tpm_val > panel_tpm[gene_name]:
                panel_tpm[gene_name] = round(tpm_val, 4)

    total_reads = mapped_gene_reads + sum(qc_counts.values())
    unmapped_frac = (qc_counts.get("N_unmapped", 0) / total_reads) if total_reads > 0 else 1.0
    qc_pass = md5_ok and total_reads > 1_000_000 and unmapped_frac < 0.50 and len(panel_tpm) >= 12

    qc_meta = {
        "sha256": actual_sha256,
        "md5_ok": md5_ok,
        "total_reads": total_reads,
        "mapped_gene_reads": mapped_gene_reads,
        "unmapped_fraction": round(unmapped_frac, 5),
        "panel_genes_found": len(panel_tpm),
        "qc_status": QCStatus.PASS.value if qc_pass else QCStatus.FAIL.value,
    }
    return file_meta, panel_tpm, qc_meta


class GdcRnaAndSsmAdapter:
    """Ingests open GDC CPTAC-3 Lung RNA-Seq (STAR-Counts TPM) and WXS Somatic Mutations."""

    modality_name = "rna"
    source_name = "gdc"

    def ingest(
        self,
        *,
        target_patient_ids: list[str] | None = None,
        project_id: str = "CPTAC-3",
        primary_site: str = "Bronchus and lung",
        max_workers: int = 8,
    ) -> tuple[AdapterPayload, AdapterPayload]:
        # 1. Fetch open RNA-Seq file metadata for CPTAC-3 Lung
        file_clauses: list[dict[str, Any]] = [
            {"op": "=", "content": {"field": "cases.project.project_id", "value": project_id}},
            {"op": "=", "content": {"field": "cases.primary_site", "value": primary_site}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
            {"op": "=", "content": {"field": "data_type", "value": "Gene Expression Quantification"}},
        ]
        if target_patient_ids:
            file_clauses.append(
                {"op": "in", "content": {"field": "cases.submitter_id", "value": sorted(set(target_patient_ids))}}
            )

        fields = ",".join([
            "file_id",
            "file_name",
            "file_size",
            "md5sum",
            "cases.case_id",
            "cases.submitter_id",
            "cases.samples.sample_id",
            "cases.samples.submitter_id",
            "cases.samples.sample_type",
            "cases.samples.days_to_collection",
        ])
        rna_files: list[dict[str, Any]] = []
        offset = 0
        while True:
            url = f"{GDC_BASE}/files?" + urllib.parse.urlencode({
                "filters": json.dumps({"op": "and", "content": file_clauses}),
                "fields": fields,
                "size": 200,
                "from": offset,
                "sort": "file_id:asc",
            })
            data = _http_get_json(url)
            batch = data["data"]["hits"]
            rna_files.extend(batch)
            total = int(data["data"]["pagination"]["total"])
            offset += len(batch)
            if not batch or offset >= total:
                break

        # Keep Primary Tumor files (plus Solid Tissue Normal files when present for tumor-vs-normal auditability)
        # Deduplicate cleanly if multiple identical sample files exist by picking first sorted file_id per sample
        selected_files: list[dict[str, Any]] = []
        seen_sample_ids: set[str] = set()
        for f in rna_files:
            cases = f.get("cases") or []
            if len(cases) != 1:
                continue
            samples = cases[0].get("samples") or []
            if len(samples) != 1:
                continue
            sample = samples[0]
            if sample.get("sample_type") != "Primary Tumor":
                continue
            sid = str(sample["sample_id"])
            if sid in seen_sample_ids:
                continue
            seen_sample_ids.add(sid)
            selected_files.append(f)

        rna_runs: list[dict[str, Any]] = []
        rna_feature_rows: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_and_parse_rna_file, f) for f in selected_files]
            for fut in as_completed(futures):
                fmeta, panel_tpm, qc = fut.result()
                case = fmeta["cases"][0]
                sample = case["samples"][0]
                pid = str(case["submitter_id"])
                sample_id = str(sample["sample_id"])
                sample_sub = str(sample["submitter_id"])
                sample_type = str(sample["sample_type"])
                day_col = sample.get("days_to_collection")
                day_int = int(day_col) if day_col is not None else None
                file_id = str(fmeta["file_id"])

                rna_runs.append({
                    "run_id": f"gdc:rna:{file_id}",
                    "modality": "rna",
                    "canonical_patient_id": pid,
                    "specimen_id": f"gdc:{sample_id}",
                    "source_specimen_id": sample_sub,
                    "episode_id": None,
                    "source": "gdc",
                    "source_run_id": file_id,
                    "qc_status": qc["qc_status"],
                    "report_status": ReportStatus.ACTIVE_FINAL.value,
                    "sample_type": sample_type,
                    "collection_day_offset": day_int,
                    "pipeline_version": "gdc-star-counts-gencode-v36",
                    "artifact_uri": f"{GDC_BASE}/data/{file_id}",
                    "artifact_sha256": qc["sha256"],
                    "features_json": json.dumps(
                        {
                            "tpm": panel_tpm,
                            "total_reads": qc["total_reads"],
                            "unmapped_fraction": qc["unmapped_fraction"],
                        },
                        sort_keys=True,
                    ),
                })
                for gene, tpm_val in sorted(panel_tpm.items()):
                    rna_feature_rows.append({
                        "canonical_patient_id": pid,
                        "source_specimen_id": sample_sub,
                        "sample_type": sample_type,
                        "file_id": file_id,
                        "gene_symbol": gene,
                        "tpm_unstranded": tpm_val,
                    })

        rna_payload = AdapterPayload(
            modality_name="rna",
            source_name="gdc",
            modality_runs=rna_runs,
            feature_tables={"rna_expression": rna_feature_rows},
            source_provenance={
                "endpoint": f"{GDC_BASE}/files + /data",
                "files_parsed": len(selected_files),
                "gene_panel": list(LUNG_ADC_GENE_PANEL),
            },
        )

        # 2. Fetch open WXS Somatic Mutations across the Lung/ADC panel for CPTAC-3 Lung
        ssm_filters = json.dumps({
            "op": "and",
            "content": [
                {"op": "=", "content": {"field": "case.project.project_id", "value": project_id}},
                {"op": "=", "content": {"field": "case.primary_site", "value": primary_site}},
                {"op": "in", "content": {"field": "ssm.consequence.transcript.gene.symbol", "value": list(LUNG_ADC_GENE_PANEL)}},
            ],
        })
        ssm_fields = ",".join([
            "ssm_occurrence_id",
            "case.case_id",
            "case.submitter_id",
            "ssm.genomic_dna_change",
            "ssm.consequence.transcript.gene.symbol",
            "ssm.consequence.transcript.aa_change",
            "ssm.consequence.transcript.is_canonical",
            "ssm.consequence.transcript.consequence_type",
        ])
        ssm_hits: list[dict[str, Any]] = []
        offset = 0
        while True:
            s_url = f"{GDC_BASE}/ssm_occurrences?" + urllib.parse.urlencode({
                "filters": ssm_filters,
                "fields": ssm_fields,
                "size": 200,
                "from": offset,
                "sort": "ssm_occurrence_id:asc",
            })
            sdata = _http_get_json(s_url)
            batch = sdata["data"]["hits"]
            ssm_hits.extend(batch)
            total = int(sdata["data"]["pagination"]["total"])
            offset += len(batch)
            if not batch or offset >= total:
                break

        # Also fetch WXS MAF file availability so we know which Primary Tumor specimens actually have WXS MAF coverage
        maf_filters = json.dumps({
            "op": "and",
            "content": [
                {"op": "=", "content": {"field": "cases.project.project_id", "value": project_id}},
                {"op": "=", "content": {"field": "cases.primary_site", "value": primary_site}},
                {"op": "=", "content": {"field": "access", "value": "open"}},
                {"op": "=", "content": {"field": "data_type", "value": "Masked Somatic Mutation"}},
            ],
        })
        maf_hits: list[dict[str, Any]] = []
        offset = 0
        while True:
            m_url = f"{GDC_BASE}/files?" + urllib.parse.urlencode({
                "filters": maf_filters,
                "fields": fields,
                "size": 200,
                "from": offset,
                "sort": "file_id:asc",
            })
            mdata = _http_get_json(m_url)
            batch = mdata["data"]["hits"]
            maf_hits.extend(batch)
            total = int(mdata["data"]["pagination"]["total"])
            offset += len(batch)
            if not batch or offset >= total:
                break

        mutations_by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
        mut_rows: list[dict[str, Any]] = []
        for hit in ssm_hits:
            case = hit.get("case") or {}
            pid = str(case.get("submitter_id") or "")
            if not pid:
                continue
            ssm = hit.get("ssm") or {}
            consequences = ssm.get("consequence") or []
            chosen = None
            for cons in consequences:
                tr = cons.get("transcript") or {}
                gene = ((tr.get("gene") or {}).get("symbol")) or ""
                if gene in PANEL_SET:
                    if tr.get("is_canonical") or chosen is None:
                        chosen = (gene, tr.get("aa_change"), tr.get("consequence_type"))
                        if tr.get("is_canonical"):
                            break
            if chosen is None:
                continue
            gene, aa_change, cons_type = chosen
            rec = {
                "ssm_occurrence_id": str(hit["ssm_occurrence_id"]),
                "canonical_patient_id": pid,
                "gene_symbol": gene,
                "aa_change": aa_change,
                "consequence_type": cons_type,
                "genomic_dna_change": ssm.get("genomic_dna_change"),
            }
            mutations_by_patient[pid].append(rec)
            mut_rows.append(rec)

        mut_runs: list[dict[str, Any]] = []
        seen_maf_samples: set[str] = set()
        for mf in maf_hits:
            cases = mf.get("cases") or []
            if len(cases) != 1:
                continue
            case = cases[0]
            pid = str(case["submitter_id"])
            pt_samples = [s for s in (case.get("samples") or []) if s.get("sample_type") == "Primary Tumor"]
            if not pt_samples:
                continue
            sample = pt_samples[0]
            sample_id = str(sample["sample_id"])
            if sample_id in seen_maf_samples:
                continue
            seen_maf_samples.add(sample_id)
            sample_sub = str(sample["submitter_id"])
            day_col = sample.get("days_to_collection")
            day_int = int(day_col) if day_col is not None else None
            file_id = str(mf["file_id"])
            pt_muts = mutations_by_patient.get(pid, [])
            mut_summary = sorted({
                f"{m['gene_symbol']}:{m['aa_change'] or m['consequence_type']}"
                for m in pt_muts
            })
            mut_runs.append({
                "run_id": f"gdc:wxs:{file_id}",
                "modality": "somatic_mutations",
                "canonical_patient_id": pid,
                "specimen_id": f"gdc:{sample_id}",
                "source_specimen_id": sample_sub,
                "episode_id": None,
                "source": "gdc",
                "source_run_id": file_id,
                "qc_status": QCStatus.PASS.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": "Primary Tumor",
                "collection_day_offset": day_int,
                "pipeline_version": "gdc-wxs-open-maf-v1",
                "artifact_uri": f"{GDC_BASE}/files/{file_id}",
                "artifact_sha256": None,
                "features_json": json.dumps(
                    {
                        "panel_mutation_count": len(pt_muts),
                        "mutated_genes": sorted({m["gene_symbol"] for m in pt_muts}),
                        "variants": mut_summary,
                        "note": "Open WXS MAF positive calls only; absence does not imply wild-type coverage.",
                    },
                    sort_keys=True,
                ),
            })

        ssm_payload = AdapterPayload(
            modality_name="somatic_mutations",
            source_name="gdc",
            modality_runs=mut_runs,
            feature_tables={"somatic_mutations": mut_rows},
            source_provenance={
                "endpoint": f"{GDC_BASE}/ssm_occurrences + /files (Masked Somatic Mutation)",
                "maf_samples": len(mut_runs),
                "panel_ssm_occurrences": len(mut_rows),
            },
        )
        return rna_payload, ssm_payload


class PdcProteomicsAdapter:
    """Ingests real CPTAC LUAD quantitative mass-spec proteomics (PDC000153) from NCI PDC."""

    modality_name = "proteomics"
    source_name = "pdc"

    def ingest(self, *, pdc_study_id: str = "PDC000153") -> AdapterPayload:
        q_cases = (
            f'{{ paginatedCasesSamplesAliquots(pdc_study_id: "{pdc_study_id}", offset: 0, limit: 250) '
            '{ total casesSamplesAliquots { case_id case_submitter_id disease_type primary_site '
            'samples { sample_id sample_submitter_id sample_type days_to_collection '
            'aliquots { aliquot_id aliquot_submitter_id } } } } }'
        )
        meta_resp = _pdc_graphql(q_cases)
        cases = meta_resp["data"]["paginatedCasesSamplesAliquots"]["casesSamplesAliquots"]

        # Fetch full quantitative TMT10 log2_ratio protein matrix directly from PDC GraphQL
        q_matrix = f'{{ quantDataMatrix(pdc_study_id: "{pdc_study_id}", data_type: "log2_ratio", acceptDUA: true) }}'
        mat_resp = _pdc_graphql(q_matrix, timeout=120)
        matrix: list[list[str]] = mat_resp["data"]["quantDataMatrix"]

        header = matrix[0]  # ["Gene/Aliquot", "<aliquot_id>:<aliquot_submitter_id>", ...]
        col_aliquots: list[tuple[int, str, str]] = []
        for idx, col in enumerate(header[1:], start=1):
            if ":" in col:
                aliquot_id, aliquot_sub = col.split(":", 1)
            else:
                aliquot_id, aliquot_sub = col, col
            col_aliquots.append((idx, aliquot_id, aliquot_sub))

        # Extract log2 protein abundance for all 16 panel genes across every aliquot
        aliquot_proteins: dict[str, dict[str, float]] = defaultdict(dict)
        found_genes: list[str] = []
        for row in matrix[1:]:
            if not row:
                continue
            gene = str(row[0]).strip()
            if gene not in PANEL_SET:
                continue
            found_genes.append(gene)
            for idx, aliquot_id, aliquot_sub in col_aliquots:
                if idx >= len(row):
                    continue
                val_str = row[idx]
                if val_str is None or val_str == "" or str(val_str).upper() == "NA":
                    continue
                try:
                    val = round(float(val_str), 4)
                except ValueError:
                    continue
                aliquot_proteins[aliquot_id][gene] = val
                aliquot_proteins[aliquot_sub][gene] = val

        patients: list[dict[str, Any]] = []
        crosswalk: list[dict[str, Any]] = []
        specimens: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        prot_feature_rows: list[dict[str, Any]] = []

        for case in cases:
            pid = str(case.get("case_submitter_id") or "")
            pdc_case_id = str(case.get("case_id") or "")
            is_ctrl = _is_control_case(pid)

            patients.append({
                "canonical_patient_id": pid,
                "source": "pdc",
                "source_patient_id": pdc_case_id,
                "is_control_or_pool": is_ctrl,
                "disease_type": case.get("disease_type"),
                "primary_site": case.get("primary_site"),
                "ajcc_stage": None,
                "gender": None,
                "vital_status": None,
                "age_at_diagnosis_days": None,
            })

            if not is_ctrl:
                crosswalk.append({
                    "canonical_patient_id": pid,
                    "source": "pdc",
                    "source_patient_id": pdc_case_id,
                    "mapping_status": MappingStatus.APPROVED.value,
                    "authority": "NCI-PDC",
                    "evidence_note": f"PDC {pdc_study_id} case_submitter_id={pid} -> case_id={pdc_case_id}",
                })

            for sample in case.get("samples") or []:
                sample_id = str(sample.get("sample_id") or "")
                sample_sub = str(sample.get("sample_submitter_id") or "")
                sample_type = str(sample.get("sample_type") or "")
                day_col = sample.get("days_to_collection")
                day_int = int(day_col) if day_col is not None else None

                specimens.append({
                    "specimen_id": f"pdc:{sample_id}",
                    "canonical_patient_id": pid,
                    "episode_id": None,
                    "source": "pdc",
                    "source_specimen_id": sample_sub,
                    "sample_type": sample_type,
                    "collection_day_offset": day_int,
                    "report_status": ReportStatus.ACTIVE_FINAL.value,
                })

                # Merge aliquot measurements for this sample
                for aliquot in sample.get("aliquots") or []:
                    aliquot_id = str(aliquot.get("aliquot_id") or "")
                    aliquot_sub = str(aliquot.get("aliquot_submitter_id") or "")
                    prots = aliquot_proteins.get(aliquot_id) or aliquot_proteins.get(aliquot_sub) or {}
                    if not prots:
                        continue
                    qc_status = QCStatus.PASS.value if len(prots) >= 5 else QCStatus.FAIL.value
                    runs.append({
                        "run_id": f"pdc:proteomics:{aliquot_id}",
                        "modality": "proteomics",
                        "canonical_patient_id": pid,
                        "specimen_id": f"pdc:{sample_id}",
                        "source_specimen_id": sample_sub,
                        "episode_id": None,
                        "source": "pdc",
                        "source_run_id": aliquot_sub or aliquot_id,
                        "qc_status": qc_status,
                        "report_status": ReportStatus.ACTIVE_FINAL.value,
                        "sample_type": sample_type,
                        "collection_day_offset": day_int,
                        "pipeline_version": f"pdc-{pdc_study_id}-tmt10-log2ratio",
                        "artifact_uri": f"https://pdc.cancer.gov/pdc/study/{pdc_study_id}",
                        "artifact_sha256": None,
                        "features_json": json.dumps(
                            {
                                "protein_log2_ratio": dict(sorted(prots.items())),
                                "panel_proteins_quantified": len(prots),
                                "aliquot_submitter_id": aliquot_sub,
                            },
                            sort_keys=True,
                        ),
                    })
                    if not is_ctrl:
                        for gene, val in sorted(prots.items()):
                            prot_feature_rows.append({
                                "canonical_patient_id": pid,
                                "source_specimen_id": sample_sub,
                                "sample_type": sample_type,
                                "aliquot_submitter_id": aliquot_sub,
                                "gene_symbol": gene,
                                "protein_log2_ratio": val,
                            })

        return AdapterPayload(
            modality_name="proteomics",
            source_name="pdc",
            patients=patients,
            identity_crosswalk=crosswalk,
            specimens=specimens,
            modality_runs=runs,
            feature_tables={"proteomics": prot_feature_rows},
            source_provenance={
                "endpoint": PDC_GRAPHQL,
                "pdc_study_id": pdc_study_id,
                "total_pdc_case_entries": len(cases),
                "control_entries_detected": sum(1 for p in patients if p["is_control_or_pool"]),
                "matrix_total_genes": len(matrix) - 1,
                "panel_genes_quantified_in_matrix": sorted(set(found_genes)),
            },
        )
