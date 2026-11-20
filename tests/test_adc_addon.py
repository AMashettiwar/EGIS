from __future__ import annotations

import json
from pathlib import Path

import pytest

from cancer_evidence.multimodal import (
    AdapterPayload,
    AlignmentPolicy,
    CohortRequest,
    GateReason,
    MultimodalSnapshot,
    PacketStatus,
    QCStatus,
    ReportStatus,
    evaluate_cohort,
    merge_adapter_payloads,
    write_multimodal_snapshot,
)
from cancer_evidence.multimodal.adc import (
    DEFAULT_LUNG_ADC_RULES,
    SingleCellCtcAdcAdapter,
    SingleCellMeasurement,
    evaluate_adc_target_cohort,
    evaluate_dual_target_adc,
)


def test_single_cell_ctc_three_way_evaluability_and_same_cell_coexpression(tmp_path: Path) -> None:
    """Proves <5 CTCs is UNEVALUABLE (never NEGATIVE) and measures true same-cell dual co-expression."""
    base = AdapterPayload(
        modality_name="clinical",
        source_name="gdc",
        patients=[
            {"canonical_patient_id": pid, "source": "gdc", "source_patient_id": pid, "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage IV", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 22000}
            for pid in ("PT-POS", "PT-NEG", "PT-LOW-CTC")
        ],
        identity_crosswalk=[
            {"canonical_patient_id": pid, "source": "gdc", "source_patient_id": pid, "mapping_status": "APPROVED", "authority": "GDC", "evidence_note": "ok"}
            for pid in ("PT-POS", "PT-NEG", "PT-LOW-CTC")
        ],
        modality_runs=[
            {
                "run_id": f"clin:{pid}",
                "modality": "clinical",
                "canonical_patient_id": pid,
                "specimen_id": f"gdc:S-{pid}",
                "source_specimen_id": f"S-{pid}",
                "episode_id": "EP1",
                "source": "gdc",
                "source_run_id": f"clin:{pid}",
                "qc_status": QCStatus.PASS.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": "Primary Tumor",
                "collection_day_offset": 0,
                "pipeline_version": "v1",
                "artifact_uri": "local://clin",
                "artifact_sha256": None,
                "features_json": "{}",
            }
            for pid in ("PT-POS", "PT-NEG", "PT-LOW-CTC")
        ],
    )

    cells: list[SingleCellMeasurement] = []
    # PT-POS: 6 QC-passing CTCs; 4 express BOTH TROP2 >= 100 and HER2 >= 100 on the SAME cell!
    for idx in range(6):
        cells.append(
            SingleCellMeasurement(
                canonical_patient_id="PT-POS",
                source_specimen_id="S-PT-POS",
                cell_id=f"C-POS-{idx}",
                qc_pass=True,
                surface_protein_intensity={
                    "TACSTD2": 180.0 if idx < 4 else 40.0,
                    "ERBB2": 150.0 if idx < 4 else 30.0,
                },
                episode_id="EP1",
                collection_day_offset=2,
            )
        )
    # PT-NEG: 6 QC-passing CTCs; ALL below cutoff -> CONFIRMED_NEGATIVE (NOT unevaluable!)
    for idx in range(6):
        cells.append(
            SingleCellMeasurement(
                canonical_patient_id="PT-NEG",
                source_specimen_id="S-PT-NEG",
                cell_id=f"C-NEG-{idx}",
                qc_pass=True,
                surface_protein_intensity={"TACSTD2": 12.0, "ERBB2": 8.0},
                episode_id="EP1",
                collection_day_offset=1,
            )
        )
    # PT-LOW-CTC: Only 2 QC-passing CTCs (< min_evaluable_ctcs=5) -> MUST BE BLOCKED AS QC_FAILED / UNKNOWN!
    for idx in range(2):
        cells.append(
            SingleCellMeasurement(
                canonical_patient_id="PT-LOW-CTC",
                source_specimen_id="S-PT-LOW-CTC",
                cell_id=f"C-LOW-{idx}",
                qc_pass=True,
                surface_protein_intensity={"TACSTD2": 5.0, "ERBB2": 5.0},
                episode_id="EP1",
                collection_day_offset=0,
            )
        )

    ctc_payload = SingleCellCtcAdcAdapter().ingest(
        cells=cells,
        target_cutoffs={"TACSTD2": 100.0, "ERBB2": 100.0},
        dual_pairs=(("TACSTD2", "ERBB2"),),
        min_evaluable_ctcs=5,
        patient_positive_cell_fraction_cutoff=0.25,
    )

    snap_dir = tmp_path / "adc_ctc_snap"
    merged = merge_adapter_payloads([base, ctc_payload])
    write_multimodal_snapshot(snap_dir, snapshot_id="adc-ctc-demo-v1", **merged)

    with MultimodalSnapshot(snap_dir) as snap:
        res = evaluate_cohort(
            snap,
            CohortRequest(
                cohort_id="ctc-adc-3way",
                required_modalities=("clinical", "ctc_surface_protein"),
                alignment_policy=AlignmentPolicy(max_day_delta=14),
            ),
        )

    by_pid = {p.canonical_patient_id: p for p in res.packets}
    assert res.ready_count == 2
    assert res.unknown_count == 1

    # Low-CTC patient is strictly blocked as QC_FAILED (UNEVALUABLE), NEVER labeled NEGATIVE
    assert by_pid["PT-LOW-CTC"].status == PacketStatus.UNKNOWN
    assert by_pid["PT-LOW-CTC"].reason_codes == (GateReason.QC_FAILED,)

    pos_feat = by_pid["PT-POS"].modalities["ctc_surface_protein"].features
    assert pos_feat["target_calls"]["TACSTD2"]["status"] == "POSITIVE"
    assert pos_feat["same_cell_dual_coexpression"]["TACSTD2+ERBB2"]["same_cell_coexpressing_cells"] == 4

    neg_feat = by_pid["PT-NEG"].modalities["ctc_surface_protein"].features
    assert neg_feat["target_calls"]["TACSTD2"]["status"] == "CONFIRMED_NEGATIVE"


SNAP_DIR = Path(__file__).resolve().parents[1] / "snapshots" / "multimodal" / "cptac3_lung_v1"


@pytest.mark.skipif(
    not (SNAP_DIR / "patients.parquet").exists(),
    reason="Local CPTAC-3 Parquet snapshot not present",
)
def test_real_cptac3_luad_adc_target_and_dual_target_stratification() -> None:
    with MultimodalSnapshot(SNAP_DIR) as snap:
        cohort = evaluate_cohort(
            snap,
            CohortRequest(
                cohort_id="cptac3-luad-adc-test",
                required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
                alignment_policy=AlignmentPolicy(
                    required_sample_type="Primary Tumor",
                    max_day_delta=30,
                    allow_shared_specimen_fallback=True,
                ),
                disease_types=("Adenomas and Adenocarcinomas",),
            ),
        )
    rules = {r.target_gene: r for r in DEFAULT_LUNG_ADC_RULES}
    trop2 = evaluate_adc_target_cohort(cohort, rules["TACSTD2"])
    assert trop2.denominator_ready_patients == 80
    assert trop2.evaluable_paired_patients == 80
    assert (trop2.discordant_rna_high_prot_low + trop2.discordant_rna_low_prot_high) > 0

    dual = evaluate_dual_target_adc(cohort, rules["EGFR"], rules["MET"])
    assert dual["evaluable_patients"] == 80
    assert dual["dual_positive_count"] >= 0
