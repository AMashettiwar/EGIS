from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from cancer_evidence.multimodal import (
    AdapterPayload,
    AlignmentPolicy,
    CohortRequest,
    GateReason,
    MappingStatus,
    MultimodalSnapshot,
    PacketStatus,
    QCStatus,
    ReportStatus,
    SnapshotIntegrityError,
    evaluate_cohort,
    export_cohort_result,
    merge_adapter_payloads,
    write_multimodal_snapshot,
)


def _run(
    run_id: str,
    modality: str,
    pid: str,
    source: str,
    *,
    specimen: str = "SPEC-1",
    episode: str | None = "EP-1",
    sample_type: str = "Primary Tumor",
    day: int | None = 0,
    qc: str = QCStatus.PASS.value,
    report: str = ReportStatus.ACTIVE_FINAL.value,
    features: dict | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "modality": modality,
        "canonical_patient_id": pid,
        "specimen_id": f"{source}:{specimen}",
        "source_specimen_id": specimen,
        "episode_id": episode,
        "source": source,
        "source_run_id": run_id,
        "qc_status": qc,
        "report_status": report,
        "sample_type": sample_type,
        "collection_day_offset": day,
        "pipeline_version": "test-v1",
        "artifact_uri": f"local://{run_id}",
        "artifact_sha256": "0" * 64,
        "features_json": json.dumps(features or {}, sort_keys=True),
    }


def _build_fixture_snapshot(tmp_path: Path) -> Path:
    """Build a synthetic 3-silo fixture exercising every cross-silo & temporal gate."""
    patients = [
        # P1: Clean 3-modality timestamp-aligned patient (Delta = 5 days) -> READY
        {"canonical_patient_id": "PT-001", "source": "gdc", "source_patient_id": "PT-001", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 22000},
        # P2: Missing timestamps, but exact shared physical specimen ID across GDC & PDC -> READY (SHARED_SPECIMEN_ID)
        {"canonical_patient_id": "PT-002", "source": "gdc", "source_patient_id": "PT-002", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage I", "gender": "male", "vital_status": "Alive", "age_at_diagnosis_days": 24000},
        # P3: Temporal misalignment (Day 0 RNA vs Day 410 Proteomics) -> UNKNOWN (TEMPORAL_MISALIGNMENT)
        {"canonical_patient_id": "PT-003", "source": "gdc", "source_patient_id": "PT-003", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage III", "gender": "male", "vital_status": "Dead", "age_at_diagnosis_days": 25000},
        # P4: Sample-type mismatch (Primary Tumor RNA vs Solid Tissue Normal Proteomics) -> UNKNOWN (SAMPLE_TYPE_MISMATCH)
        {"canonical_patient_id": "PT-004", "source": "gdc", "source_patient_id": "PT-004", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 21000},
        # P5: Missing proteomics completely -> UNKNOWN (MISSING_MODALITY)
        {"canonical_patient_id": "PT-005", "source": "gdc", "source_patient_id": "PT-005", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage I", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 20000},
        # P6: Proteomics QC failed -> UNKNOWN (QC_FAILED)
        {"canonical_patient_id": "PT-006", "source": "gdc", "source_patient_id": "PT-006", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "male", "vital_status": "Alive", "age_at_diagnosis_days": 23000},
        # P7: Draft (unapproved) PDC identity mapping -> UNKNOWN (UNAPPROVED_MAPPING)
        {"canonical_patient_id": "PT-007", "source": "gdc", "source_patient_id": "PT-007", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage I", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 21500},
        # P8: Conflicting PDC identity mapping -> CONFLICT (CONFLICTING_MAPPING)
        {"canonical_patient_id": "PT-008", "source": "gdc", "source_patient_id": "PT-008", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage III", "gender": "male", "vital_status": "Alive", "age_at_diagnosis_days": 26000},
        # P9: Two conflicting ACTIVE_FINAL pathology reports on same specimen -> CONFLICT (CONFLICTING_ACTIVE_REPORTS)
        {"canonical_patient_id": "PT-009", "source": "gdc", "source_patient_id": "PT-009", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 22500},
        # P10: Missing timestamps AND different specimen IDs -> UNKNOWN (TEMPORAL_MISALIGNMENT)
        {"canonical_patient_id": "PT-010", "source": "gdc", "source_patient_id": "PT-010", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "male", "vital_status": "Alive", "age_at_diagnosis_days": 23500},
        # P11: Different disease type (LUSC) -> excluded by disease filter
        {"canonical_patient_id": "PT-011", "source": "gdc", "source_patient_id": "PT-011", "is_control_or_pool": False, "disease_type": "LUSC", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "male", "vital_status": "Alive", "age_at_diagnosis_days": 24500},
        # CTRL-1: Non-patient pooled reference control (mirrors real PDC Internal Reference!)
        {"canonical_patient_id": "CTRL-IR-1", "source": "pdc", "source_patient_id": "Internal Reference", "is_control_or_pool": True, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": None, "gender": None, "vital_status": None, "age_at_diagnosis_days": None},
    ]

    crosswalk = []
    for i in range(1, 12):
        pid = f"PT-{i:03d}"
        crosswalk.append({"canonical_patient_id": pid, "source": "gdc", "source_patient_id": pid, "mapping_status": MappingStatus.APPROVED.value, "authority": "GDC", "evidence_note": "primary"})
        if pid == "PT-007":
            crosswalk.append({"canonical_patient_id": pid, "source": "pdc", "source_patient_id": f"PDC-{pid}", "mapping_status": MappingStatus.DRAFT.value, "authority": "PDC", "evidence_note": "unapproved draft"})
        elif pid == "PT-008":
            crosswalk.append({"canonical_patient_id": pid, "source": "pdc", "source_patient_id": "PDC-A", "mapping_status": MappingStatus.APPROVED.value, "authority": "PDC", "evidence_note": "map A"})
            crosswalk.append({"canonical_patient_id": pid, "source": "pdc", "source_patient_id": "PDC-B", "mapping_status": MappingStatus.APPROVED.value, "authority": "PDC", "evidence_note": "map B"})
        elif pid != "PT-005":
            crosswalk.append({"canonical_patient_id": pid, "source": "pdc", "source_patient_id": f"PDC-{pid}", "mapping_status": MappingStatus.APPROVED.value, "authority": "PDC", "evidence_note": "approved"})

    runs = [
        # PT-001: Superseded clinical report ignored; ACTIVE_FINAL clinical + RNA (day 0) + Proteomics (day 5)
        _run("R-001-CLIN-OLD", "clinical", "PT-001", "gdc", specimen="S1", episode="EP1", day=0, report=ReportStatus.SUPERSEDED.value, features={"stage": "Stage I"}),
        _run("R-001-CLIN-NEW", "clinical", "PT-001", "gdc", specimen="S1", episode="EP1", day=0, features={"stage": "Stage II"}),
        _run("R-001-RNA", "rna", "PT-001", "gdc", specimen="S1", episode="EP1", day=2, features={"TACSTD2_tpm": 85.4, "ERBB2_tpm": 41.2}),
        _run("R-001-PROT", "proteomics", "PT-001", "pdc", specimen="S1", episode="EP1", day=5, features={"TACSTD2_log2": 1.8, "ERBB2_log2": 0.4}),
        # PT-002: Missing timestamps, exact same physical specimen ID S2 across GDC & PDC
        _run("R-002-CLIN", "clinical", "PT-002", "gdc", specimen="S2", episode=None, day=None, features={"stage": "Stage I"}),
        _run("R-002-RNA", "rna", "PT-002", "gdc", specimen="S2", episode=None, day=None, features={"TACSTD2_tpm": 12.0}),
        _run("R-002-PROT", "proteomics", "PT-002", "pdc", specimen="S2", episode=None, day=None, features={"TACSTD2_log2": -0.9}),
        # PT-003: Day 0 vs Day 410 -> temporal misalignment!
        _run("R-003-CLIN", "clinical", "PT-003", "gdc", specimen="S3A", episode="EP1", day=0, features={"stage": "Stage III"}),
        _run("R-003-RNA", "rna", "PT-003", "gdc", specimen="S3A", episode="EP1", day=0, features={"TACSTD2_tpm": 50.0}),
        _run("R-003-PROT", "proteomics", "PT-003", "pdc", specimen="S3B", episode="EP1", day=410, features={"TACSTD2_log2": 2.1}),
        # PT-004: Tumor RNA vs Solid Tissue Normal Proteomics -> sample-type mismatch!
        _run("R-004-CLIN", "clinical", "PT-004", "gdc", specimen="S4T", episode="EP1", day=0),
        _run("R-004-RNA", "rna", "PT-004", "gdc", specimen="S4T", episode="EP1", day=0),
        _run("R-004-PROT", "proteomics", "PT-004", "pdc", specimen="S4N", episode="EP1", day=0, sample_type="Solid Tissue Normal"),
        # PT-005: Missing proteomics
        _run("R-005-CLIN", "clinical", "PT-005", "gdc", specimen="S5", episode="EP1", day=0),
        _run("R-005-RNA", "rna", "PT-005", "gdc", specimen="S5", episode="EP1", day=0),
        # PT-006: Proteomics QC failed
        _run("R-006-CLIN", "clinical", "PT-006", "gdc", specimen="S6", episode="EP1", day=0),
        _run("R-006-RNA", "rna", "PT-006", "gdc", specimen="S6", episode="EP1", day=0),
        _run("R-006-PROT", "proteomics", "PT-006", "pdc", specimen="S6", episode="EP1", day=0, qc=QCStatus.FAIL.value),
        # PT-007: Unapproved mapping
        _run("R-007-CLIN", "clinical", "PT-007", "gdc", specimen="S7", episode="EP1", day=0),
        _run("R-007-RNA", "rna", "PT-007", "gdc", specimen="S7", episode="EP1", day=0),
        _run("R-007-PROT", "proteomics", "PT-007", "pdc", specimen="S7", episode="EP1", day=0),
        # PT-008: Conflicting mapping
        _run("R-008-CLIN", "clinical", "PT-008", "gdc", specimen="S8", episode="EP1", day=0),
        _run("R-008-RNA", "rna", "PT-008", "gdc", specimen="S8", episode="EP1", day=0),
        _run("R-008-PROT", "proteomics", "PT-008", "pdc", specimen="S8", episode="EP1", day=0),
        # PT-009: Two conflicting ACTIVE_FINAL clinical reports on same specimen
        _run("R-009-CLIN-A", "clinical", "PT-009", "gdc", specimen="S9", episode="EP1", day=0, features={"stage": "Stage II"}),
        _run("R-009-CLIN-B", "clinical", "PT-009", "gdc", specimen="S9", episode="EP1", day=0, features={"stage": "Stage IV"}),
        _run("R-009-RNA", "rna", "PT-009", "gdc", specimen="S9", episode="EP1", day=0),
        _run("R-009-PROT", "proteomics", "PT-009", "pdc", specimen="S9", episode="EP1", day=0),
        # PT-010: Missing timestamps AND different specimen IDs across silos -> cannot align
        _run("R-010-CLIN", "clinical", "PT-010", "gdc", specimen="S10-GDC", episode=None, day=None),
        _run("R-010-RNA", "rna", "PT-010", "gdc", specimen="S10-GDC", episode=None, day=None),
        _run("R-010-PROT", "proteomics", "PT-010", "pdc", specimen="S10-PDC", episode=None, day=None),
        # PT-011: LUSC (should be excluded by disease_types=("LUAD",))
        _run("R-011-CLIN", "clinical", "PT-011", "gdc", specimen="S11", episode="EP1", day=0),
        _run("R-011-RNA", "rna", "PT-011", "gdc", specimen="S11", episode="EP1", day=0),
        _run("R-011-PROT", "proteomics", "PT-011", "pdc", specimen="S11", episode="EP1", day=0),
    ]

    snap_dir = tmp_path / "snap_v1"
    write_multimodal_snapshot(
        snap_dir,
        snapshot_id="test-luad-mm-v1",
        patients=patients,
        identity_crosswalk=crosswalk,
        specimens=[],
        modality_runs=runs,
        source_provenance={"fixture": "synthetic_multimodal_gates"},
    )
    return snap_dir


def test_multimodal_gating_alignment_and_packet_export(tmp_path: Path) -> None:
    snap_dir = _build_fixture_snapshot(tmp_path)

    with MultimodalSnapshot(snap_dir) as snap:
        req = CohortRequest(
            cohort_id="luad-3mod-primary",
            required_modalities=("clinical", "rna", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=True,
            ),
            disease_types=("LUAD",),
        )
        res = evaluate_cohort(snap, req)

    assert res.total_raw_records == 12
    assert res.excluded_controls == 1
    assert res.eligible_human_patients == 10
    assert res.ready_count == 2
    assert res.unknown_count == 6
    assert res.conflict_count == 2

    by_pid = {p.canonical_patient_id: p for p in res.packets}
    assert "CTRL-IR-1" not in by_pid
    assert "PT-011" not in by_pid

    # PT-001: clean timestamp window (5 days), superseded report ignored
    assert by_pid["PT-001"].status == PacketStatus.READY
    assert by_pid["PT-001"].alignment_mode == "TIMESTAMP_WINDOW"
    assert by_pid["PT-001"].max_observed_day_delta == 5
    assert by_pid["PT-001"].modalities["clinical"].run_id == "R-001-CLIN-NEW"

    # PT-002: missing timestamps rescued ONLY by exact shared specimen ID
    assert by_pid["PT-002"].status == PacketStatus.READY
    assert by_pid["PT-002"].alignment_mode == "SHARED_SPECIMEN_ID"

    # PT-003: Day 0 vs Day 410 blocked!
    assert by_pid["PT-003"].status == PacketStatus.UNKNOWN
    assert by_pid["PT-003"].reason_codes == (GateReason.TEMPORAL_MISALIGNMENT,)
    assert by_pid["PT-003"].max_observed_day_delta == 410

    # PT-004: Primary Tumor vs Solid Tissue Normal blocked!
    assert by_pid["PT-004"].status == PacketStatus.UNKNOWN
    assert by_pid["PT-004"].reason_codes == (GateReason.SAMPLE_TYPE_MISMATCH,)

    # PT-005..PT-010 exact gate reasons
    assert by_pid["PT-005"].reason_codes == (GateReason.MISSING_MODALITY,)
    assert by_pid["PT-006"].reason_codes == (GateReason.QC_FAILED,)
    assert by_pid["PT-007"].reason_codes == (GateReason.UNAPPROVED_MAPPING,)
    assert by_pid["PT-008"].status == PacketStatus.CONFLICT
    assert by_pid["PT-008"].reason_codes == (GateReason.CONFLICTING_MAPPING,)
    assert by_pid["PT-009"].status == PacketStatus.CONFLICT
    assert by_pid["PT-009"].reason_codes == (GateReason.CONFLICTING_ACTIVE_REPORTS,)
    assert by_pid["PT-010"].status == PacketStatus.UNKNOWN
    assert by_pid["PT-010"].reason_codes == (GateReason.TEMPORAL_MISALIGNMENT,)

    # Export verified packets and check Parquet output contains ONLY READY patients
    out_dir = tmp_path / "exported"
    manifest = export_cohort_result(res, out_dir)
    assert manifest["ready_count"] == 2
    table = pq.read_table(out_dir / "ready_patient_packets.parquet")
    assert table.num_rows == 2
    assert set(table.column("canonical_patient_id").to_pylist()) == {"PT-001", "PT-002"}


def test_pluggable_fourth_modality_adapter_requires_zero_engine_changes(tmp_path: Path) -> None:
    p_clin = AdapterPayload(
        modality_name="clinical",
        source_name="gdc",
        patients=[{"canonical_patient_id": "PT-100", "source": "gdc", "source_patient_id": "PT-100", "is_control_or_pool": False, "disease_type": "LUAD", "primary_site": "Lung", "ajcc_stage": "Stage II", "gender": "female", "vital_status": "Alive", "age_at_diagnosis_days": 22000}],
        identity_crosswalk=[{"canonical_patient_id": "PT-100", "source": "gdc", "source_patient_id": "PT-100", "mapping_status": "APPROVED", "authority": "GDC", "evidence_note": "ok"}],
        modality_runs=[_run("R-CLIN", "clinical", "PT-100", "gdc", specimen="S100", day=0)],
    )
    p_vision = AdapterPayload(
        modality_name="vision_slide",
        source_name="idc",
        identity_crosswalk=[{"canonical_patient_id": "PT-100", "source": "idc", "source_patient_id": "PT-100", "mapping_status": "APPROVED", "authority": "IDC", "evidence_note": "ok"}],
        modality_runs=[_run("R-SLIDE", "vision_slide", "PT-100", "idc", specimen="S100", day=0, features={"encoder": "vinid/plip", "embedding_dim": 512, "l2_norm": 1.0})],
    )
    merged = merge_adapter_payloads([p_clin, p_vision])
    snap_dir = tmp_path / "snap_4mod"
    write_multimodal_snapshot(snap_dir, snapshot_id="plug-v1", **merged)

    with MultimodalSnapshot(snap_dir) as snap:
        res = evaluate_cohort(
            snap,
            CohortRequest(cohort_id="vision-check", required_modalities=("clinical", "vision_slide")),
        )
    assert res.ready_count == 1
    assert res.packets[0].modalities["vision_slide"].features["encoder"] == "vinid/plip"


def test_snapshot_integrity_blocks_tampering_and_unfrozen(tmp_path: Path) -> None:
    snap_dir = _build_fixture_snapshot(tmp_path)
    # Tamper with Parquet file bytes
    p = snap_dir / "patients.parquet"
    p.write_bytes(p.read_bytes() + b"tamper")
    with pytest.raises(SnapshotIntegrityError, match="SHA-256 mismatch"):
        MultimodalSnapshot(snap_dir)

    # Unfrozen snapshot must also fail closed
    unfrozen = tmp_path / "unfrozen"
    write_multimodal_snapshot(
        unfrozen,
        snapshot_id="unfrozen-v1",
        patients=[],
        identity_crosswalk=[],
        specimens=[],
        modality_runs=[],
        is_frozen=False,
    )
    with pytest.raises(SnapshotIntegrityError, match="not frozen"):
        MultimodalSnapshot(unfrozen)
