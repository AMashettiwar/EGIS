from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import urllib.parse
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

CBIOPORTAL_API = "https://www.cbioportal.org/api"
USER_AGENT = "EGIS-discovery/1.0"

# 30 curated Lung Adenocarcinoma drivers, tumor suppressors, DDR & ADC-relevant genes
MUTATION_GENES: tuple[str, ...] = (
    "EGFR", "KRAS", "TP53", "STK11", "KEAP1", "RBM10", "NF1", "BRAF",
    "MET", "ERBB2", "PIK3CA", "SMARCA4", "ARID1A", "ATM", "SETD2", "RB1",
    "CDKN2A", "CTNNB1", "U2AF1", "RIT1", "NFE2L2", "LRP1B", "BRCA1", "BRCA2",
    "ERBB3", "TACSTD2", "TOP1", "ALK", "ROS1", "RET",
)

# 45 biologically curated genes across ADC targets, payload sensitivity, immune, lineage, EMT & cell cycle
EXPRESSION_GENES: tuple[str, ...] = (
    # ADC surface targets (12)
    "TACSTD2", "ERBB2", "MET", "ERBB3", "EGFR", "CD276",
    "CEACAM5", "NECTIN4", "FOLR1", "MUC1", "MSLN", "DLL3",
    # ADC payload sensitivity & efflux resistance (7)
    "SLFN11", "TOP1", "ABCG2", "ABCB1", "TUBB3", "BRCA1", "BRCA2",
    # Immune & checkpoint microenvironment (8)
    "CD274", "PDCD1", "CD8A", "IFNG", "CXCL9", "GZMB", "FOXP3", "CD68",
    # Lung adenocarcinoma lineage & differentiation (6)
    "NKX2-1", "NAPSA", "SFTA2", "KRT7", "KRT5", "SOX2",
    # Proliferation, EMT & signaling (12)
    "MKI67", "CDK4", "CCNE1", "E2F1", "VIM", "CDH1",
    "ZEB1", "SNAI1", "VEGFA", "HIF1A", "MYC", "BCL2",
)

COHORT_SPECS: tuple[dict[str, str | None], ...] = (
    {
        "cohort": "TCGA_LUAD",
        "study_id": "luad_tcga_pan_can_atlas_2018",
        "mutation_profile": "luad_tcga_pan_can_atlas_2018_mutations",
        "rna_profile": "luad_tcga_pan_can_atlas_2018_rna_seq_v2_mrna_median_all_sample_Zscores",
        "protein_profile": None,
        "region": "Western_US",
    },
    {
        "cohort": "ONCOSG_LUAD",
        "study_id": "luad_oncosg_2020",
        "mutation_profile": "luad_oncosg_2020_mutations",
        "rna_profile": "luad_oncosg_2020_rna_seq_v2_mrna_median_all_sample_Zscores",
        "protein_profile": None,
        "region": "East_Asian",
    },
    {
        "cohort": "CPTAC_LUAD",
        "study_id": "luad_cptac_2020",
        "mutation_profile": "luad_cptac_2020_mutations",
        "rna_profile": "luad_cptac_2020_rna_seq_mrna_median_all_sample_Zscores",
        "protein_profile": "luad_cptac_2020_protein_quantification_zscores",
        "region": "Global_CPTAC",
    },
)

SILENT_MUTATION_TYPES = frozenset({"Silent", "Intron", "3'UTR", "5'UTR", "3'Flank", "5'Flank", "IGR", "RNA"})


def _cbio_get(path: str, timeout: int = 45) -> Any:
    req = urllib.request.Request(
        f"{CBIOPORTAL_API}{path}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cbio_post(path: str, body: Any, timeout: int = 60) -> Any:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{CBIOPORTAL_API}{path}",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_entrez_ids(gene_symbols: list[str]) -> dict[str, int]:
    rows = _cbio_post("/genes/fetch?geneIdType=HUGO_GENE_SYMBOL", sorted(set(gene_symbols)))
    return {str(r["hugoGeneSymbol"]): int(r["entrezGeneId"]) for r in rows if r.get("entrezGeneId")}


def _parse_stage(raw: str | None) -> tuple[int | None, str | None]:
    if not raw:
        return None, None
    s = str(raw).strip().upper().replace("STAGE ", "").replace("STAGE", "").strip()
    if s.startswith("IV") or s.startswith("4"):
        return 4, "III_IV"
    if s.startswith("III") or s.startswith("3"):
        return 3, "III_IV"
    if s.startswith("II") or s.startswith("2"):
        return 2, "I_II"
    if s.startswith("I") or s.startswith("1"):
        return 1, "I_II"
    return None, None


def _parse_sex(raw: str | None) -> int | None:
    if not raw:
        return None
    s = str(raw).strip().upper()
    if s.startswith("M"):
        return 1
    if s.startswith("F"):
        return 0
    return None


def _parse_smoking(raw_status: str | None, pack_years: float | None) -> int | None:
    if pack_years is not None:
        return 1 if pack_years > 0 else 0
    if not raw_status:
        return None
    s = str(raw_status).strip().lower()
    if any(k in s for k in ("never", "non-smoker", "nonsmoker", "no")):
        return 0
    if any(k in s for k in ("smoker", "current", "former", "reformed", "yes")):
        return 1
    return None


def _parse_event(raw: str | None) -> int | None:
    if not raw:
        return None
    s = str(raw).strip().upper()
    if s.startswith("1:") or "DECEASED" in s or "PROGRESSION" in s or "RECURRED" in s:
        return 1
    if s.startswith("0:") or "LIVING" in s or "CENSORED" in s or "DISEASEFREE" in s:
        return 0
    return None


def _to_float(val: Any) -> float | None:
    if val is None or val == "" or str(val).upper() in ("NA", "NAN", "NULL"):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_luad_981_cohort(out_dir: str | Path) -> dict[str, Any]:
    """Acquire and freeze 981 real Lung Adenocarcinoma patients across TCGA, OncoSG, and CPTAC."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)

    all_genes = sorted(set(MUTATION_GENES) | set(EXPRESSION_GENES))
    symbol_to_entrez = _resolve_entrez_ids(all_genes)
    entrez_to_symbol = {v: k for k, v in symbol_to_entrez.items()}
    mut_entrez = [symbol_to_entrez[g] for g in MUTATION_GENES if g in symbol_to_entrez]
    expr_entrez = [symbol_to_entrez[g] for g in EXPRESSION_GENES if g in symbol_to_entrez]

    patient_rows: list[dict[str, Any]] = []
    mutation_records: list[dict[str, Any]] = []
    rna_records: list[dict[str, Any]] = []
    protein_records: list[dict[str, Any]] = []
    cohort_summary: dict[str, Any] = {}

    for spec in COHORT_SPECS:
        cohort = str(spec["cohort"])
        study_id = str(spec["study_id"])
        print(f"  -> Fetching {cohort} ({study_id})...")

        samples = _cbio_get(f"/studies/{study_id}/samples?pageSize=2000")
        # One canonical primary sample per patient
        sample_to_patient: dict[str, str] = {}
        patient_to_sample: dict[str, str] = {}
        for s in sorted(samples, key=lambda x: str(x["sampleId"])):
            pid = str(s["patientId"])
            sid = str(s["sampleId"])
            sample_to_patient[sid] = pid
            if pid not in patient_to_sample:
                patient_to_sample[pid] = sid

        sample_ids = sorted(patient_to_sample.values())

        # Fetch patient-level + sample-level clinical attributes
        pt_clin_raw = _cbio_get(
            f"/studies/{study_id}/clinical-data?clinicalDataType=PATIENT&pageSize=100000"
        )
        sm_clin_raw = _cbio_get(
            f"/studies/{study_id}/clinical-data?clinicalDataType=SAMPLE&pageSize=100000"
        )

        clin_by_pt: dict[str, dict[str, str]] = {pid: {} for pid in patient_to_sample}
        for row in pt_clin_raw:
            pid = str(row["patientId"])
            if pid in clin_by_pt:
                clin_by_pt[pid][str(row["clinicalAttributeId"])] = str(row["value"])
        for row in sm_clin_raw:
            sid = str(row["sampleId"])
            pid = sample_to_patient.get(sid)
            if pid and patient_to_sample.get(pid) == sid:
                clin_by_pt[pid][str(row["clinicalAttributeId"])] = str(row["value"])

        # Fetch mutations across the 30-gene panel
        mut_profile = str(spec["mutation_profile"])
        mut_hits = _cbio_post(
            f"/molecular-profiles/{mut_profile}/mutations/fetch",
            {"sampleIds": sample_ids, "entrezGeneIds": mut_entrez},
        )
        mutated_by_pt: dict[str, set[str]] = {pid: set() for pid in patient_to_sample}
        for m in mut_hits:
            if str(m.get("mutationType") or "") in SILENT_MUTATION_TYPES:
                continue
            sid = str(m["sampleId"])
            pid = sample_to_patient.get(sid)
            gene = entrez_to_symbol.get(int(m["entrezGeneId"]))
            if pid and gene:
                mutated_by_pt[pid].add(gene)

        # Determine which samples are actually sequenced in the mutation profile
        try:
            seq_list = _cbio_get(f"/sample-lists/{study_id}_sequenced")
            sequenced_samples = set(seq_list.get("sampleIds") or sample_ids)
        except Exception:
            sequenced_samples = set(sample_ids)

        # Fetch RNA-Seq Z-scores across the 45-gene panel
        rna_profile = str(spec["rna_profile"])
        rna_hits = _cbio_post(
            f"/molecular-profiles/{rna_profile}/molecular-data/fetch",
            {"sampleIds": sample_ids, "entrezGeneIds": expr_entrez},
        )
        rna_by_pt: dict[str, dict[str, float]] = {}
        for d in rna_hits:
            val = _to_float(d.get("value"))
            if val is None:
                continue
            pid = sample_to_patient.get(str(d["sampleId"]))
            gene = entrez_to_symbol.get(int(d["entrezGeneId"]))
            if pid and gene:
                rna_by_pt.setdefault(pid, {})[gene] = round(val, 4)

        # Fetch Mass-Spec Proteomics Z-scores (CPTAC)
        prot_by_pt: dict[str, dict[str, float]] = {}
        if spec.get("protein_profile"):
            prot_profile = str(spec["protein_profile"])
            prot_hits = _cbio_post(
                f"/molecular-profiles/{prot_profile}/molecular-data/fetch",
                {"sampleIds": sample_ids, "entrezGeneIds": expr_entrez},
            )
            for d in prot_hits:
                val = _to_float(d.get("value"))
                if val is None:
                    continue
                pid = sample_to_patient.get(str(d["sampleId"]))
                gene = entrez_to_symbol.get(int(d["entrezGeneId"]))
                if pid and gene:
                    prot_by_pt.setdefault(pid, {})[gene] = round(val, 4)

        # Build unified rows
        for pid, sid in sorted(patient_to_sample.items()):
            c = clin_by_pt[pid]
            uid = f"{cohort}:{pid}"
            stage_num, stage_grp = _parse_stage(
                c.get("AJCC_PATHOLOGIC_TUMOR_STAGE") or c.get("STAGE")
            )
            pack_yrs = _to_float(c.get("PACK_YEARS_SMOKED") or c.get("SMOKING_PACK_YEARS"))
            smoking_bin = _parse_smoking(
                c.get("SMOKING_STATUS") or c.get("SMOKING_HISTORY"),
                pack_yrs,
            )
            purity = _to_float(
                c.get("PURITY") or c.get("TUMOR_PURITY_BYESTIMATE_RNASEQ") or c.get("TSNET PURITY")
            )
            tmb = _to_float(c.get("TMB_NONSYNONYMOUS"))
            age = _to_float(c.get("AGE"))
            sex = _parse_sex(c.get("SEX"))
            os_months = _to_float(c.get("OS_MONTHS"))
            os_event = _parse_event(c.get("OS_STATUS"))
            pfs_months = _to_float(c.get("PFS_MONTHS"))
            pfs_event = _parse_event(c.get("PFS_STATUS"))
            has_wxs = sid in sequenced_samples

            patient_rows.append({
                "patient_uid": uid,
                "cohort": cohort,
                "region": str(spec["region"]),
                "patient_id": pid,
                "sample_id": sid,
                "stage_numeric": stage_num,
                "stage_group": stage_grp,
                "age": age,
                "sex_male": sex,
                "smoking_ever": smoking_bin,
                "pack_years": pack_yrs,
                "tumor_purity": purity,
                "tmb_nonsynonymous": tmb,
                "site_id": c.get("TISSUE_SOURCE_SITE") or cohort,
                "os_months": os_months,
                "os_event": os_event,
                "pfs_months": pfs_months,
                "pfs_event": pfs_event,
                "has_wxs": has_wxs,
                "has_rna": pid in rna_by_pt and len(rna_by_pt[pid]) >= 20,
                "has_proteomics": pid in prot_by_pt and len(prot_by_pt[pid]) >= 15,
            })

            if has_wxs:
                m_row: dict[str, Any] = {"patient_uid": uid, "cohort": cohort}
                pt_muts = mutated_by_pt[pid]
                for g in MUTATION_GENES:
                    m_row[g] = 1 if g in pt_muts else 0
                mutation_records.append(m_row)

            if pid in rna_by_pt and len(rna_by_pt[pid]) >= 20:
                r_row: dict[str, Any] = {"patient_uid": uid, "cohort": cohort}
                for g in EXPRESSION_GENES:
                    r_row[g] = rna_by_pt[pid].get(g)
                rna_records.append(r_row)

            if pid in prot_by_pt and len(prot_by_pt[pid]) >= 15:
                p_row: dict[str, Any] = {"patient_uid": uid, "cohort": cohort}
                for g in EXPRESSION_GENES:
                    p_row[g] = prot_by_pt[pid].get(g)
                protein_records.append(p_row)

        cohort_summary[cohort] = {
            "study_id": study_id,
            "patients": len(patient_to_sample),
            "with_wxs": sum(1 for sid in sample_ids if sid in sequenced_samples),
            "with_rna": sum(1 for pid in patient_to_sample if pid in rna_by_pt and len(rna_by_pt[pid]) >= 20),
            "with_proteomics": sum(1 for pid in patient_to_sample if pid in prot_by_pt and len(prot_by_pt[pid]) >= 15),
        }

    tables_to_write = {
        "patients": pd.DataFrame(patient_rows),
        "mutations": pd.DataFrame(mutation_records),
        "rna_zscores": pd.DataFrame(rna_records),
        "proteomics_zscores": pd.DataFrame(protein_records),
    }

    table_meta: dict[str, Any] = {}
    for name, df in tables_to_write.items():
        path = root / f"{name}.parquet"
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path, compression="zstd")
        table_meta[name] = {
            "file": path.name,
            "row_count": int(len(df)),
            "col_count": int(len(df.columns)),
            "sha256": _sha256_file(path),
        }

    manifest_body = {
        "snapshot_id": "luad-multi-cohort-discovery-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_patients": len(patient_rows),
        "cohorts": cohort_summary,
        "mutation_genes": list(MUTATION_GENES),
        "expression_genes": list(EXPRESSION_GENES),
        "tables": table_meta,
    }
    raw_canon = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = dict(manifest_body)
    manifest["manifest_sha256"] = hashlib.sha256(raw_canon).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    m = ingest_luad_981_cohort(Path(__file__).resolve().parents[3] / "snapshots" / "discovery" / "luad_981_v1")
    print(json.dumps(m["cohorts"], indent=2))
    print("TOTAL:", m["total_patients"], "SHA:", m["manifest_sha256"][:16])
