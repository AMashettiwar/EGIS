from __future__ import annotations

import json
from pathlib import Path

from cancer_evidence.multimodal import (
    AdapterPayload,
    CohortRequest,
    MultimodalSnapshot,
    PacketStatus,
    QCStatus,
    ReportStatus,
    evaluate_cohort,
    merge_adapter_payloads,
    write_multimodal_snapshot,
)
from cancer_evidence.multimodal.demo_ui import compute_rna_protein_summary, write_multimodal_demo_ui
from cancer_evidence.multimodal.vision import EmbeddingRecord, VisionEmbeddingAdapter


def test_vision_embedding_adapter_and_demo_ui_rendering(tmp_path: Path) -> None:
    p_base = AdapterPayload(
        modality_name="clinical",
        source_name="gdc",
        patients=[
            {"canonical_patient_id": f"PT-{i}", "source": "gdc", "source_patient_id": f"PT-{i}", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 22000}
            for i in range(1, 5)
        ],
        identity_crosswalk=[
            {"canonical_patient_id": f"PT-{i}", "source": s, "source_patient_id": f"PT-{i}", "mapping_status": "APPROVED", "authority": s.upper(), "evidence_note": "ok"}
            for i in range(1, 5)
            for s in ("gdc", "pdc")
        ],
        modality_runs=[
            {
                "run_id": f"clin-{i}",
                "modality": "clinical",
                "canonical_patient_id": f"PT-{i}",
                "specimen_id": f"gdc:S{i}",
                "source_specimen_id": f"S{i}",
                "episode_id": "EP1",
                "source": "gdc",
                "source_run_id": f"clin-{i}",
                "qc_status": QCStatus.PASS.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": "Primary Tumor",
                "collection_day_offset": 0,
                "pipeline_version": "v1",
                "artifact_uri": "local://clin",
                "artifact_sha256": "a" * 64,
                "features_json": "{}",
            }
            for i in range(1, 5)
        ]
        + [
            {
                "run_id": f"rna-{i}",
                "modality": "rna",
                "canonical_patient_id": f"PT-{i}",
                "specimen_id": f"gdc:S{i}",
                "source_specimen_id": f"S{i}",
                "episode_id": "EP1",
                "source": "gdc",
                "source_run_id": f"rna-{i}",
                "qc_status": QCStatus.PASS.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": "Primary Tumor",
                "collection_day_offset": 0,
                "pipeline_version": "v1",
                "artifact_uri": "local://rna",
                "artifact_sha256": "b" * 64,
                "features_json": json.dumps({"tpm": {"TACSTD2": float(10 * i), "ERBB2": float(50 / i)}}),
            }
            for i in range(1, 5)
        ]
        + [
            {
                "run_id": f"prot-{i}",
                "modality": "proteomics",
                "canonical_patient_id": f"PT-{i}",
                "specimen_id": f"pdc:S{i}",
                "source_specimen_id": f"S{i}",
                "episode_id": "EP1",
                "source": "pdc",
                "source_run_id": f"prot-{i}",
                "qc_status": QCStatus.PASS.value,
                "report_status": ReportStatus.ACTIVE_FINAL.value,
                "sample_type": "Primary Tumor",
                "collection_day_offset": 0,
                "pipeline_version": "v1",
                "artifact_uri": "local://prot",
                "artifact_sha256": "c" * 64,
                "features_json": json.dumps({"protein_log2_ratio": {"TACSTD2": float(i - 2.5), "ERBB2": float(i - 2.0)}}),
            }
            for i in range(1, 5)
        ],
    )

    vision_records = [
        EmbeddingRecord(
            canonical_patient_id=f"PT-{i}",
            source_specimen_id=f"S{i}",
            sample_type="Primary Tumor",
            slide_id=f"SLIDE-{i}",
            model_id="vinid/plip",
            device="cpu",
            image_sha256="d" * 64,
            embedding_dim=4,
            l2_norm=1.0,
            vector=(0.5, 0.5, 0.5, 0.5) if i < 4 else (0.0, 0.0, 0.0, 0.0),  # PT-4 zero vector -> QC_FAILED
            collection_day_offset=0,
            episode_id="EP1",
        )
        for i in range(1, 5)
    ]
    p_vision = VisionEmbeddingAdapter().ingest(records=vision_records)

    snap_dir = tmp_path / "vision_snap"
    merged = merge_adapter_payloads([p_base, p_vision])
    write_multimodal_snapshot(snap_dir, snapshot_id="vision-test-v1", **merged)

    with MultimodalSnapshot(snap_dir) as snap:
        res = evaluate_cohort(
            snap,
            CohortRequest(
                cohort_id="4mod-with-vision",
                required_modalities=("clinical", "rna", "proteomics", "vision_slide"),
            ),
        )

    assert res.ready_count == 3
    assert res.unknown_count == 1
    by_pid = {p.canonical_patient_id: p for p in res.packets}
    assert by_pid["PT-4"].status == PacketStatus.UNKNOWN  # zero-norm embedding failed QC

    summary = compute_rna_protein_summary(res)
    genes = {r["gene"]: r for r in summary}
    assert "TACSTD2" in genes and genes["TACSTD2"]["paired_patients"] == 3
    assert "ERBB2" in genes and genes["ERBB2"]["discordant_count"] >= 1

    html_out = write_multimodal_demo_ui(res, tmp_path / "demo.html")
    html_text = html_out.read_text(encoding="utf-8")
    assert "EGIS Multimodal Evidence Umbrella" in html_text
    assert "TACSTD2" in html_text
    assert "default-src 'none'" in html_text
