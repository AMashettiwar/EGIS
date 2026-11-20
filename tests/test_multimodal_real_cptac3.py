from __future__ import annotations

from pathlib import Path

import pytest

from cancer_evidence.multimodal import (
    AlignmentPolicy,
    CohortRequest,
    MultimodalSnapshot,
    evaluate_cohort,
)
from cancer_evidence.multimodal.demo_ui import compute_rna_protein_summary

SNAP_DIR = Path(__file__).resolve().parents[1] / "snapshots" / "multimodal" / "cptac3_lung_v1"


@pytest.mark.skipif(
    not (SNAP_DIR / "patients.parquet").exists(),
    reason="Local Parquet snapshot not present (run scripts/acquire_cptac3_lung_multimodal.py to build)",
)
def test_real_cptac3_lung_cross_silo_snapshot_reconciles_offline() -> None:
    with MultimodalSnapshot(SNAP_DIR) as snap:
        req = CohortRequest(
            cohort_id="cptac3-luad-4mod-check",
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
        res = evaluate_cohort(snap, req)

    assert res.excluded_controls == 4
    assert res.eligible_human_patients == 230
    assert res.ready_count == 80
    assert res.unknown_count == 150
    assert res.conflict_count == 0
    assert res.reason_breakdown.get("SAMPLE_TYPE_MISMATCH") == 2

    summary = {r["gene"]: r for r in compute_rna_protein_summary(res)}
    assert summary["TACSTD2"]["paired_patients"] == 80
    assert summary["ERBB2"]["paired_patients"] == 80
    assert summary["CD276"]["paired_patients"] == 80
