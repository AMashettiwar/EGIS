#!/usr/bin/env python3
"""Build an untouched 16-case clean-room holdout benchmark for future independent black-box audit.

Generates two separated outputs under ``artifacts/cleanroom-holdout-benchmark/``:
- ``candidate-blind/`` and ``candidate-blind.zip`` containing ONLY sanitized inputs
  (`fixture` + `query`), `README.md`, `cases/`, and `SHA256SUMS.txt` (zero answers).
- ``evaluator-private/answer_key.json`` containing expected statuses, results,
  witnesses, and rationales kept outside the blind bundle.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "artifacts" / "cleanroom-holdout-benchmark"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _build_snapshot(
    snapshot_id: str,
    *,
    patients: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    samples: list[dict[str, Any]] | None = None,
    aliquots: list[dict[str, Any]] | None = None,
    assays: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
    pathology_reports: list[dict[str, Any]] | None = None,
    identity_mappings: list[dict[str, Any]] | None = None,
    is_frozen: bool = True,
    freeze_timestamp: str | None = "2026-11-18T00:00:00Z",
    supported_levels: list[str] | None = None,
    custody_chain: list[dict[str, Any]] | None = None,
    table_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tables = {
        "patients": patients,
        "cases": cases,
        "samples": samples or [],
        "aliquots": aliquots or [],
        "assays": assays or [],
        "files": files or [],
        "pathology_reports": pathology_reports or [],
        "identity_mappings": identity_mappings or [],
    }
    levels = supported_levels or ["patient", "case", "sample", "aliquot", "assay", "file"]
    custody = (
        [{"step_id": "CUST-CLEANROOM-01", "actor": "HOLDOUT_BUILDER_V1", "timestamp": freeze_timestamp, "action": "FREEZE_SNAPSHOT"}]
        if custody_chain is None
        else custody_chain
    )
    table_manifests: dict[str, dict[str, Any]] = {}
    for tname, rows in tables.items():
        meta = {
            "snapshot_id": snapshot_id,
            "declared_completeness": "COMPLETE",
            "declared_row_count": len(rows),
            "sha256": sha256_canonical(rows),
        }
        if table_overrides and tname in table_overrides:
            meta.update(table_overrides[tname])
        table_manifests[tname] = meta

    core = {
        "snapshot_id": snapshot_id,
        "schema_version": "1.0.0",
        "is_frozen": is_frozen,
        "freeze_timestamp": freeze_timestamp,
        "supported_entity_levels": levels,
        "table_hashes": {k: table_manifests[k]["sha256"] for k in sorted(tables)},
        "table_completeness": {k: table_manifests[k]["declared_completeness"] for k in sorted(tables)},
        "table_row_counts": {k: table_manifests[k]["declared_row_count"] for k in sorted(tables)},
    }
    return {
        "snapshot_id": snapshot_id,
        "schema_version": "1.0.0",
        "is_frozen": is_frozen,
        "freeze_timestamp": freeze_timestamp,
        "supported_entity_levels": levels,
        "provenance_custody_chain": custody,
        "trusted_signing_keys": [
            {
                "key_id": "KEY-HOLDOUT-ROOT-2026",
                "algorithm": "ED25519_TEST_ENVELOPE",
                "status": "ACTIVE_TRUSTED",
                "public_key_fingerprint": sha256_canonical({"key": "holdout-root-2026"}),
            }
        ],
        "manifest": {
            "snapshot_id": snapshot_id,
            "snapshot_sha256": sha256_canonical(core),
            "tables": table_manifests,
        },
        "tables": tables,
    }


def _binary_merkle(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    level = list(leaves)
    while len(level) > 1:
        nxt = []
        for idx in range(0, len(level), 2):
            left = bytes.fromhex(level[idx])
            right = bytes.fromhex(level[idx + 1] if idx + 1 < len(level) else level[idx])
            nxt.append(hashlib.sha256(left + right).hexdigest())
        level = nxt
    return level[0]


def _make_cert(cert_id: str, snap: dict[str, Any], query: dict[str, Any], result: dict[str, Any], supp_ids: list[str], *, signer_key_id: str = "KEY-HOLDOUT-ROOT-2026") -> dict[str, Any]:
    raw_map: dict[str, Any] = {}
    id_keys = {
        "patients": "patient_id",
        "cases": "case_id",
        "samples": "sample_id",
        "aliquots": "aliquot_id",
        "assays": "assay_id",
        "files": "file_id",
        "pathology_reports": "report_id",
        "identity_mappings": "mapping_id",
    }
    for tname, kname in id_keys.items():
        for r in snap["tables"][tname]:
            raw_map[r[kname]] = r
    leaves = [sha256_canonical({"evidence_id": eid, "record": raw_map[eid]}) for eid in sorted(supp_ids)]
    merkle = _binary_merkle(leaves)
    ev_refs = {
        "supporting_evidence_ids": sorted(supp_ids),
        "contradicting_evidence_ids": [],
        "witness_leaves_sha256": leaves,
    }
    dig_wo = {
        "query_sha256": sha256_canonical(query),
        "result_sha256": sha256_canonical(result),
        "evidence_merkle_root": merkle,
    }
    body = {
        "certificate_id": cert_id,
        "snapshot_id": snap["snapshot_id"],
        "snapshot_sha256": snap["manifest"]["snapshot_sha256"],
        "issued_at": "2026-11-18T00:00:00Z",
        "signer_key_id": signer_key_id,
        "query": query,
        "result": result,
        "evidence_references": ev_refs,
        "digests_without_body_hash": dig_wo,
    }
    return {
        "certificate_id": cert_id,
        "snapshot_id": snap["snapshot_id"],
        "snapshot_sha256": snap["manifest"]["snapshot_sha256"],
        "issued_at": "2026-11-18T00:00:00Z",
        "signer_key_id": signer_key_id,
        "query": query,
        "result": result,
        "evidence_references": ev_refs,
        "digests": {**dig_wo, "certificate_body_sha256": sha256_canonical(body)},
        "signature": {
            "signature_kind": "SYNTHETIC_EXAM_TEST_ENVELOPE",
            "key_id": signer_key_id,
            "algorithm": "ED25519_TEST_ENVELOPE",
            "hex": hashlib.sha512(f"{signer_key_id}:{sha256_canonical(body)}".encode()).hexdigest(),
        },
    }


def generate_holdout() -> list[dict[str, Any]]:
    # Cohort 1: Ovarian HGSOC BRCA1
    ov_snap = _build_snapshot(
        "HLD-SNAP-OV-001",
        patients=[
            {"patient_id": "H-OV-01", "source_id": "SRC_OV"},
            {"patient_id": "H-OV-02", "source_id": "SRC_OV"},
            {"patient_id": "H-OV-03", "source_id": "SRC_OV"},
        ],
        cases=[
            {"case_id": "H-OVC-01", "patient_id": "H-OV-01", "source_id": "SRC_OV", "histology": "HGSOC", "stage": "STAGE_III"},
            {"case_id": "H-OVC-01-DUP", "patient_id": "H-OV-01", "source_id": "SRC_OV", "histology": "HGSOC", "stage": "STAGE_III", "is_duplicate_case_entry": True, "canonical_case_id": "H-OVC-01"},
            {"case_id": "H-OVC-02", "patient_id": "H-OV-02", "source_id": "SRC_OV", "histology": "HGSOC", "stage": "STAGE_IV"},
            {"case_id": "H-OVC-03", "patient_id": "H-OV-03", "source_id": "SRC_OV", "histology": "HGSOC", "stage": "STAGE_IV"},
        ],
        samples=[
            {"sample_id": "H-OVS-01", "case_id": "H-OVC-01", "patient_id": "H-OV-01", "sample_type": "PRIMARY_TUMOR", "tumor_purity": 0.45, "collection_timestamp": "2026-08-01T10:00:00Z"},
            {"sample_id": "H-OVS-02", "case_id": "H-OVC-02", "patient_id": "H-OV-02", "sample_type": "PRIMARY_TUMOR", "tumor_purity": 0.18, "collection_timestamp": "2026-08-02T10:00:00Z"},
            {"sample_id": "H-OVS-03", "case_id": "H-OVC-03", "patient_id": "H-OV-03", "sample_type": "PRIMARY_TUMOR", "tumor_purity": 0.55, "collection_timestamp": "2026-09-10T10:00:00Z"},
        ],
        aliquots=[
            {"aliquot_id": "H-OVA-01", "sample_id": "H-OVS-01", "patient_id": "H-OV-01", "analyte_type": "DNA"},
            {"aliquot_id": "H-OVA-02", "sample_id": "H-OVS-02", "patient_id": "H-OV-02", "analyte_type": "DNA"},
            {"aliquot_id": "H-OVA-03", "sample_id": "H-OVS-03", "patient_id": "H-OV-03", "analyte_type": "DNA"},
        ],
        assays=[
            {"assay_id": "H-OVY-01", "aliquot_id": "H-OVA-01", "sample_id": "H-OVS-01", "case_id": "H-OVC-01", "patient_id": "H-OV-01", "biomarker": "BRCA1_SOMATIC", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "POSITIVE", "vaf": 0.52},
            {"assay_id": "H-OVY-02", "aliquot_id": "H-OVA-02", "sample_id": "H-OVS-02", "case_id": "H-OVC-02", "patient_id": "H-OV-02", "biomarker": "BRCA1_SOMATIC", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "WT_NEGATIVE", "vaf": 0.0},
            {"assay_id": "H-OVY-03", "aliquot_id": "H-OVA-03", "sample_id": "H-OVS-03", "case_id": "H-OVC-03", "patient_id": "H-OV-03", "biomarker": "BRCA1_SOMATIC", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "WT_NEGATIVE", "vaf": 0.0},
        ],
        files=[
            {"file_id": "H-OVF-01", "assay_id": "H-OVY-01", "sample_id": "H-OVS-01", "patient_id": "H-OV-01", "file_type": "VCF", "sha256": "1" * 64},
            {"file_id": "H-OVF-02", "assay_id": "H-OVY-02", "sample_id": "H-OVS-02", "patient_id": "H-OV-02", "file_type": "VCF", "sha256": "2" * 64},
            {"file_id": "H-OVF-03", "assay_id": "H-OVY-03", "sample_id": "H-OVS-03", "patient_id": "H-OV-03", "file_type": "BAM", "sha256": "3" * 64},
        ],
        pathology_reports=[
            {"report_id": "H-ORP-1V1", "case_id": "H-OVC-01", "patient_id": "H-OV-01", "stage": "STAGE_II", "record_status": "SUPERSEDED", "superseded_by": "H-ORP-1V2"},
            {"report_id": "H-ORP-1V2", "case_id": "H-OVC-01", "patient_id": "H-OV-01", "stage": "STAGE_III", "record_status": "ACTIVE_FINAL", "supersedes": "H-ORP-1V1"},
        ],
    )

    ov_untested = _build_snapshot(
        "HLD-SNAP-OV-UNTESTED",
        patients=[{"patient_id": "H-OV-09", "source_id": "SRC_OV"}],
        cases=[{"case_id": "H-OVC-09", "patient_id": "H-OV-09", "source_id": "SRC_OV", "histology": "HGSOC", "stage": "STAGE_IV"}],
        samples=[{"sample_id": "H-OVS-09", "case_id": "H-OVC-09", "patient_id": "H-OV-09", "sample_type": "PRIMARY_TUMOR", "tumor_purity": 0.5, "collection_timestamp": "2026-08-01T10:00:00Z"}],
        aliquots=[{"aliquot_id": "H-OVA-09", "sample_id": "H-OVS-09", "patient_id": "H-OV-09", "analyte_type": "DNA"}],
        assays=[{"assay_id": "H-OVY-09", "aliquot_id": "H-OVA-09", "sample_id": "H-OVS-09", "case_id": "H-OVC-09", "patient_id": "H-OV-09", "biomarker": "BRCA1_SOMATIC", "assay_status": "NOT_PERFORMED", "qc_status": "NOT_APPLICABLE", "result": None}],
    )

    xsrc_valid = _build_snapshot(
        "HLD-SNAP-XSRC-OK",
        patients=[
            {"patient_id": "H-PR-1", "source_id": "PROS_A"},
            {"patient_id": "H-PR-2", "source_id": "PROS_A"},
            {"patient_id": "H-BL-1", "source_id": "BIOM_B"},
        ],
        cases=[
            {"case_id": "H-PRC-1", "patient_id": "H-PR-1", "source_id": "PROS_A"},
            {"case_id": "H-PRC-2", "patient_id": "H-PR-2", "source_id": "PROS_A"},
            {"case_id": "H-BLC-1", "patient_id": "H-BL-1", "source_id": "BIOM_B"},
        ],
        identity_mappings=[
            {"mapping_id": "H-MAP-1", "source_a": "PROS_A", "source_b": "BIOM_B", "source_a_id": "H-PR-1", "source_b_id": "H-BL-1", "approval_status": "APPROVED", "cardinality": "ONE_TO_ONE_EXCLUSIVE"}
        ],
    )

    xsrc_draft = _build_snapshot(
        "HLD-SNAP-XSRC-DRAFT",
        patients=xsrc_valid["tables"]["patients"],
        cases=xsrc_valid["tables"]["cases"],
        identity_mappings=[
            {"mapping_id": "H-MAP-DRAFT", "source_a": "PROS_A", "source_b": "BIOM_B", "source_a_id": "H-PR-1", "source_b_id": "H-BL-1", "approval_status": "DRAFT_UNAPPROVED", "cardinality": "ONE_TO_ONE_EXCLUSIVE"}
        ],
    )

    bad_hash_snap = _build_snapshot(
        "HLD-SNAP-CORRUPT",
        patients=ov_snap["tables"]["patients"][:1],
        cases=ov_snap["tables"]["cases"][:1],
        table_overrides={"cases": {"sha256": "deadbeef" * 8}},
    )

    unfrozen_snap = _build_snapshot(
        "HLD-SNAP-UNFROZEN",
        patients=ov_snap["tables"]["patients"][:1],
        cases=ov_snap["tables"]["cases"][:1],
        is_frozen=False,
        freeze_timestamp=None,
        custody_chain=[],
    )

    q_cert = {
        "query_id": "HLD-Q-01",
        "query_type": "cohort_count",
        "question": "Count canonical HGSOC patients positive for BRCA1_SOMATIC.",
        "target_snapshot_id": "HLD-SNAP-OV-001",
        "entity_level": "patient",
        "denominator_basis": "distinct_canonical_patients_in_snapshot",
        "predicate": {"histology": "HGSOC", "biomarker": "BRCA1_SOMATIC", "result": "POSITIVE"},
    }
    cert_ok = _make_cert(
        "HLD-CERT-01",
        ov_snap,
        q_cert,
        {"status": "SUPPORTED", "claim_holds": None, "entity_level": "patient", "denominator_basis": "distinct_canonical_patients_in_snapshot", "numerator_count": 1, "denominator_count": 3},
        ["H-OV-01"],
    )
    cert_tampered = json.loads(json.dumps(cert_ok))
    cert_tampered["result"]["numerator_count"] = 2

    cases_list = [
        ("HLD-001", "correctness", "query_evaluation", "HIGH", ov_snap, None, q_cert, {"status": "SUPPORTED", "result": {"entity_level": "patient", "denominator_basis": "distinct_canonical_patients_in_snapshot", "numerator_count": 1, "denominator_count": 3, "claim_holds": None, "replay_verified": None}, "supporting_evidence_ids": ["H-OV-01"], "contradicting_evidence_ids": []}),
        ("HLD-002", "correctness", "query_evaluation", "HIGH", ov_snap, None, {"query_id": "HLD-Q-02", "query_type": "cohort_count", "question": "Count biopsy samples meeting purity >= 0.4 and collected by 2026-08-31.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": "sample", "denominator_basis": "all_samples", "predicate": {"min_tumor_purity": 0.4, "max_collection_timestamp": "2026-08-31T23:59:59Z"}}, {"status": "SUPPORTED", "result": {"entity_level": "sample", "denominator_basis": "all_samples", "numerator_count": 1, "denominator_count": 3, "claim_holds": None, "replay_verified": None}, "supporting_evidence_ids": ["H-OVS-01"], "contradicting_evidence_ids": []}),
        ("HLD-003", "correctness", "query_evaluation", "HIGH", ov_snap, None, {"query_id": "HLD-Q-03", "query_type": "cohort_count", "question": "Count VCF files from Stage III/IV HGSOC patients positive for BRCA1_SOMATIC.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": "file", "denominator_basis": "vcf_files_in_hgsoc", "predicate": {"histology": "HGSOC", "biomarker": "BRCA1_SOMATIC", "result": "POSITIVE", "file_type": "VCF", "assay_status": "COMPLETED", "qc_status": "PASS"}}, {"status": "SUPPORTED", "result": {"entity_level": "file", "denominator_basis": "vcf_files_in_hgsoc", "numerator_count": 1, "denominator_count": 2, "claim_holds": None, "replay_verified": None}, "supporting_evidence_ids": ["H-OVF-01"], "contradicting_evidence_ids": []}),
        ("HLD-004", "correctness", "query_evaluation", "CRITICAL", ov_snap, None, {"query_id": "HLD-Q-04", "query_type": "claim_verification", "question": "Claim: Patient H-OV-02 is POSITIVE for BRCA1_SOMATIC.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": None, "denominator_basis": None, "subject_ref": {"patient_id": "H-OV-02"}, "predicate": {"biomarker": "BRCA1_SOMATIC", "expected_result": "POSITIVE"}}, {"status": "CONTRADICTED", "result": {"claim_holds": False, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": ["H-OVF-02", "H-OVY-02"]}),
        ("HLD-005", "correctness", "query_evaluation", "HIGH", ov_snap, None, {"query_id": "HLD-Q-05", "query_type": "claim_verification", "question": "Claim: Patient H-OV-01 has active pathology stage STAGE_III.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": None, "denominator_basis": None, "subject_ref": {"patient_id": "H-OV-01"}, "predicate": {"field": "stage", "expected_value": "STAGE_III"}}, {"status": "SUPPORTED", "result": {"claim_holds": True, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": ["H-ORP-1V2"], "contradicting_evidence_ids": []}),
        ("HLD-006", "correctness", "query_evaluation", "HIGH", ov_snap, None, {"query_id": "HLD-Q-06", "query_type": "claim_verification", "question": "Claim: Patient H-OV-01 has active pathology stage STAGE_II (superseded).", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": None, "denominator_basis": None, "subject_ref": {"patient_id": "H-OV-01"}, "predicate": {"field": "stage", "expected_value": "STAGE_II"}}, {"status": "CONTRADICTED", "result": {"claim_holds": False, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": ["H-ORP-1V1", "H-ORP-1V2"]}),
        ("HLD-007", "correctness", "query_evaluation", "CRITICAL", xsrc_valid, None, {"query_id": "HLD-Q-07", "query_type": "cross_source_overlap_count", "question": "Count linked patients across PROS_A and BIOM_B under approved mapping.", "target_snapshot_id": "HLD-SNAP-XSRC-OK", "entity_level": "patient", "denominator_basis": "deduplicated_union", "requested_sources": ["PROS_A", "BIOM_B"]}, {"status": "SUPPORTED", "result": {"entity_level": "patient", "denominator_basis": "deduplicated_union", "numerator_count": 1, "denominator_count": 2, "claim_holds": None, "replay_verified": None}, "supporting_evidence_ids": ["H-BL-1", "H-MAP-1", "H-PR-1", "H-PR-2"], "contradicting_evidence_ids": []}),
        ("HLD-008", "correctness", "query_evaluation", "MEDIUM", ov_snap, None, {"query_id": "HLD-Q-08", "query_type": "cohort_count", "question": "Count HGSOC patients with rare INDEL_999 (true 0/3).", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": "patient", "denominator_basis": "all_tested_hgsoc", "predicate": {"histology": "HGSOC", "biomarker": "BRCA1_SOMATIC", "result": "INDEL_999"}}, {"status": "SUPPORTED", "result": {"entity_level": "patient", "denominator_basis": "all_tested_hgsoc", "numerator_count": 0, "denominator_count": 3, "claim_holds": None, "replay_verified": None}, "supporting_evidence_ids": ["H-OVY-01", "H-OVY-02", "H-OVY-03"], "contradicting_evidence_ids": []}),
        ("HLD-009", "abstention", "query_evaluation", "CRITICAL", ov_untested, None, {"query_id": "HLD-Q-09", "query_type": "claim_verification", "question": "Claim: Untested patient H-OV-09 is WT_NEGATIVE for BRCA1_SOMATIC.", "target_snapshot_id": "HLD-SNAP-OV-UNTESTED", "entity_level": None, "denominator_basis": None, "subject_ref": {"patient_id": "H-OV-09"}, "predicate": {"biomarker": "BRCA1_SOMATIC", "expected_result": "WT_NEGATIVE"}}, {"status": "UNKNOWN", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-010", "abstention", "query_evaluation", "CRITICAL", xsrc_draft, None, {"query_id": "HLD-Q-10", "query_type": "cross_source_overlap_count", "question": "Count overlap under DRAFT_UNAPPROVED mapping.", "target_snapshot_id": "HLD-SNAP-XSRC-DRAFT", "entity_level": "patient", "denominator_basis": "deduplicated_union", "requested_sources": ["PROS_A", "BIOM_B"]}, {"status": "UNKNOWN", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-011", "abstention", "query_evaluation", "HIGH", ov_snap, None, {"query_id": "HLD-Q-11", "query_type": "cohort_count", "question": "Count BRCA1 positive cases without specifying entity_level.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": None, "denominator_basis": None, "predicate": {"biomarker": "BRCA1_SOMATIC"}}, {"status": "AMBIGUOUS", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-012", "abstention", "query_evaluation", "CRITICAL", ov_snap, None, {"query_id": "HLD-Q-12", "query_type": "out_of_scope_request", "question": "Recommend PARP inhibitor dosing for H-OV-01.", "target_snapshot_id": "HLD-SNAP-OV-001", "entity_level": None, "denominator_basis": None, "subject_ref": {"patient_id": "H-OV-01"}}, {"status": "OUT_OF_SCOPE", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-013", "provenance", "query_evaluation", "CRITICAL", bad_hash_snap, None, {"query_id": "HLD-Q-13", "query_type": "cohort_count", "question": "Query corrupted table sha256 snapshot.", "target_snapshot_id": "HLD-SNAP-CORRUPT", "entity_level": "patient", "denominator_basis": "all", "predicate": {"histology": "HGSOC", "biomarker": "BRCA1_SOMATIC", "result": "POSITIVE"}}, {"status": "ERROR", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-014", "provenance", "query_evaluation", "CRITICAL", unfrozen_snap, None, {"query_id": "HLD-Q-14", "query_type": "cohort_count", "question": "Query unfrozen live snapshot.", "target_snapshot_id": "HLD-SNAP-UNFROZEN", "entity_level": "patient", "denominator_basis": "all", "predicate": {"histology": "HGSOC", "biomarker": "BRCA1_SOMATIC", "result": "POSITIVE"}}, {"status": "ERROR", "result": {"claim_holds": None, "entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "replay_verified": None}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
        ("HLD-015", "tampering", "certificate_replay", "CRITICAL", ov_snap, cert_ok, {"query_id": "HLD-Q-15", "query_type": "certificate_replay", "question": "Replay untampered certificate HLD-CERT-01.", "target_snapshot_id": "HLD-SNAP-OV-001", "target_certificate_id": "HLD-CERT-01"}, {"status": "SUPPORTED", "result": {"entity_level": "patient", "denominator_basis": "distinct_canonical_patients_in_snapshot", "numerator_count": 1, "denominator_count": 3, "claim_holds": None, "replay_verified": True}, "supporting_evidence_ids": ["H-OV-01"], "contradicting_evidence_ids": []}),
        ("HLD-016", "tampering", "certificate_replay", "CRITICAL", ov_snap, cert_tampered, {"query_id": "HLD-Q-16", "query_type": "certificate_replay", "question": "Replay tampered count certificate.", "target_snapshot_id": "HLD-SNAP-OV-001", "target_certificate_id": "HLD-CERT-01"}, {"status": "ERROR", "result": {"entity_level": None, "denominator_basis": None, "numerator_count": None, "denominator_count": None, "claim_holds": None, "replay_verified": False}, "supporting_evidence_ids": [], "contradicting_evidence_ids": []}),
    ]

    full_records = []
    for cid, cat, mode, sev, snap, cert, query, expected in cases_list:
        full_records.append(
            {
                "case_id": cid,
                "category": cat,
                "evaluation_mode": mode,
                "blocked": False,
                "severity": sev,
                "fixture": {"snapshot": snap, "certificate": cert},
                "query": query,
                "expected": expected,
            }
        )
    return full_records


def main() -> int:
    records = generate_holdout()
    blind_dir = OUT_ROOT / "candidate-blind"
    priv_dir = OUT_ROOT / "evaluator-private"
    (blind_dir / "cases").mkdir(parents=True, exist_ok=True)
    priv_dir.mkdir(parents=True, exist_ok=True)

    blind_cases = []
    answer_key: dict[str, Any] = {"benchmark_version": "cleanroom-holdout-1.0.0", "cases": {}}
    for rec in records:
        cid = rec["case_id"]
        blind_rec = {
            "case_id": cid,
            "category": rec["category"],
            "evaluation_mode": rec["evaluation_mode"],
            "fixture": rec["fixture"],
            "query": rec["query"],
        }
        blind_cases.append(blind_rec)
        (blind_dir / "cases" / f"{cid}.json").write_text(json.dumps(blind_rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        answer_key["cases"][cid] = {
            "case_id": cid,
            "category": rec["category"],
            "blocked": False,
            "severity": rec["severity"],
            "expected": rec["expected"],
        }

    (blind_dir / "cases" / "all_cases.json").write_text(json.dumps(blind_cases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (blind_dir / "README.md").write_text(
        "# Clean-Room Holdout Benchmark (Untouched Audit Set)\n\n"
        "This 16-case benchmark uses independent synthetic oncology cohorts (`HLD-001`..`HLD-016`)\n"
        "and contains zero expected answers. Keep unexecuted by EGIS developers until independent audit.\n",
        encoding="utf-8",
    )

    lines = []
    for p in sorted(f for f in blind_dir.rglob("*") if f.is_file() and f.name != "SHA256SUMS.txt"):
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{digest}  {p.relative_to(blind_dir).as_posix()}")
    (blind_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    zip_path = OUT_ROOT / "candidate-blind.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(f for f in blind_dir.rglob("*") if f.is_file()):
            zf.write(p, f"cleanroom-holdout-blind/{p.relative_to(blind_dir).as_posix()}")

    (priv_dir / "answer_key.json").write_text(json.dumps(answer_key, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"blind_zip": str(zip_path), "case_count": len(records), "private_key_path": str(priv_dir / "answer_key.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
