from __future__ import annotations

import json
from pathlib import Path
import time

from cancer_evidence.multimodal import (
    AlignmentPolicy,
    CohortRequest,
    MultimodalSnapshot,
    evaluate_cohort,
    export_cohort_result,
    merge_adapter_payloads,
    write_multimodal_snapshot,
)
from cancer_evidence.multimodal.cptac_lung import (
    GdcClinicalAdapter,
    GdcRnaAndSsmAdapter,
    PdcProteomicsAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "snapshots" / "multimodal" / "cptac3_lung_v1"
EXPORT_DIR = REPO_ROOT / "artifacts" / "multimodal-demo" / "cptac3_lung_v1"


def main() -> None:
    t0 = time.time()
    print("[1/4] Fetching GDC CPTAC-3 Lung clinical & biospecimen hierarchy...")
    clin_payload = GdcClinicalAdapter().ingest()
    print(f"      GDC cases: {len(clin_payload.patients)}, specimens: {len(clin_payload.specimens)}")

    print("[2/4] Fetching NCI PDC PDC000153 LUAD quantitative proteomics (GraphQL quantDataMatrix)...")
    pdc_payload = PdcProteomicsAdapter().ingest()
    pdc_human_ids = [p["canonical_patient_id"] for p in pdc_payload.patients if not p["is_control_or_pool"]]
    pdc_controls = [p["canonical_patient_id"] for p in pdc_payload.patients if p["is_control_or_pool"]]
    print(
        f"      PDC entries: {len(pdc_payload.patients)} "
        f"(human={len(pdc_human_ids)}, controls={len(pdc_controls)}: {pdc_controls}), "
        f"protein runs: {len(pdc_payload.modality_runs)}"
    )

    print("[3/4] Fetching GDC open RNA-Seq (STAR-Counts TPM) for PDC-overlapped cases + WXS somatic mutations...")
    rna_payload, ssm_payload = GdcRnaAndSsmAdapter().ingest(target_patient_ids=pdc_human_ids, max_workers=4)
    print(
        f"      RNA Primary Tumor runs parsed: {len(rna_payload.modality_runs)}, "
        f"WXS MAF runs: {len(ssm_payload.modality_runs)}, "
        f"panel SSMs: {len(ssm_payload.feature_tables.get('somatic_mutations', []))}"
    )

    print("[4/4] Merging adapters & freezing DuckDB/Parquet multimodal snapshot...")
    merged = merge_adapter_payloads([clin_payload, rna_payload, ssm_payload, pdc_payload])
    manifest = write_multimodal_snapshot(
        SNAPSHOT_DIR,
        snapshot_id="cptac3-lung-multimodal-v1",
        **merged,
    )
    print(f"      Frozen snapshot: {SNAPSHOT_DIR}")
    print(f"      manifest_sha256: {manifest['manifest_sha256']}")
    print(f"      Tables: { {k: v['row_count'] for k, v in manifest['tables'].items()} }")

    # End-to-end deterministic evaluation on frozen snapshot
    with MultimodalSnapshot(SNAPSHOT_DIR) as snap:
        req_4mod = CohortRequest(
            cohort_id="cptac3-luad-4mod-primary-tumor",
            required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=True,
            ),
            disease_types=("Adenomas and Adenocarcinomas",),
        )
        res_4mod = evaluate_cohort(snap, req_4mod)
        export_manifest = export_cohort_result(res_4mod, EXPORT_DIR)

        # Also run strict timestamp-only check (no shared-specimen fallback) to prove honest abstention
        req_strict_ts = CohortRequest(
            cohort_id="cptac3-luad-4mod-strict-timestamp-only",
            required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=False,
            ),
            disease_types=("Adenomas and Adenocarcinomas",),
        )
        res_strict = evaluate_cohort(snap, req_strict_ts)

    print("\n=== REAL E2E CROSS-SILO FUNNEL (CPTAC-3 LUAD: Clinical + RNA + WXS + PDC Proteomics) ===")
    for st in res_4mod.funnel:
        print(f"  {st.stage:45s} remaining={st.remaining:4d}  dropped={st.dropped:4d}  ({st.reason})")
    print(f"  Status counts: READY={res_4mod.ready_count}  UNKNOWN={res_4mod.unknown_count}  CONFLICT={res_4mod.conflict_count}")
    print(f"  Reason breakdown: {json.dumps(res_4mod.reason_breakdown, sort_keys=True)}")
    print(
        f"\n=== STRICT TIMESTAMP-ONLY CHECK (allow_shared_specimen_fallback=False) ===\n"
        f"  READY={res_strict.ready_count}  UNKNOWN={res_strict.unknown_count}  "
        f"reasons={json.dumps(res_strict.reason_breakdown, sort_keys=True)}"
    )
    print(f"\nExported verified packets -> {EXPORT_DIR} (export_sha256={export_manifest['export_sha256'][:16]}...)")
    print(f"Total elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
