import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import canonical_json, write_snapshot


def _sha_obj(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _binary_merkle(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    cur = list(leaves)
    while len(cur) > 1:
        nxt = []
        for idx in range(0, len(cur), 2):
            left = bytes.fromhex(cur[idx])
            right = bytes.fromhex(cur[idx + 1] if idx + 1 < len(cur) else cur[idx])
            nxt.append(hashlib.sha256(left + right).hexdigest())
        cur = nxt
    return cur[0]


def _make_test_envelope_bundle() -> tuple[dict, dict]:
    raw_records = {
        "GBM-1": {"patient_id": "GBM-1", "source_id": "SYN_GBM"},
        "GBM-2": {"patient_id": "GBM-2", "source_id": "SYN_GBM"},
        "GC-1": {"case_id": "GC-1", "patient_id": "GBM-1", "histology": "GBM", "stage": "GRADE_IV"},
        "GC-2": {"case_id": "GC-2", "patient_id": "GBM-2", "histology": "GBM", "stage": "GRADE_IV"},
        "GA-1": {"assay_id": "GA-1", "patient_id": "GBM-1", "biomarker": "IDH1", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "R132H_POSITIVE"},
        "GA-2": {"assay_id": "GA-2", "patient_id": "GBM-2", "biomarker": "IDH1", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "WT_NEGATIVE"},
    }
    snap_id = "SYN-GBM-SNAP"
    snap_sha = _sha_obj(raw_records)
    bundle = {
        "source": "SYN_GBM",
        "fixture_id": snap_id,
        "coverage": {"SYN_GBM": {"scope": "gbm", "complete": True, "expected_total": 2, "retrieved_count": 2}},
        "entities": [
            {"entity_id": "SYN_GBM:patient:GBM-1", "kind": "patient", "source": "SYN_GBM", "source_id": "GBM-1", "attributes": {}},
            {"entity_id": "SYN_GBM:patient:GBM-2", "kind": "patient", "source": "SYN_GBM", "source_id": "GBM-2", "attributes": {}},
            {"entity_id": "SYN_GBM:case:GC-1", "kind": "case", "source": "SYN_GBM", "source_id": "GC-1", "attributes": {"patient_id": "GBM-1", "histology": "GBM", "stage": "GRADE_IV"}},
            {"entity_id": "SYN_GBM:case:GC-2", "kind": "case", "source": "SYN_GBM", "source_id": "GC-2", "attributes": {"patient_id": "GBM-2", "histology": "GBM", "stage": "GRADE_IV"}},
            {"entity_id": "SYN_GBM:assay:GA-1", "kind": "assay", "source": "SYN_GBM", "source_id": "GA-1", "attributes": {"patient_id": "GBM-1", "biomarker": "IDH1", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "R132H_POSITIVE"}},
            {"entity_id": "SYN_GBM:assay:GA-2", "kind": "assay", "source": "SYN_GBM", "source_id": "GA-2", "attributes": {"patient_id": "GBM-2", "biomarker": "IDH1", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "WT_NEGATIVE"}},
        ],
        "relationships": [
            {"subject_id": "SYN_GBM:patient:GBM-1", "predicate": "HAS_CASE", "object_id": "SYN_GBM:case:GC-1", "source": "SYN_GBM", "source_record_id": "1"},
            {"subject_id": "SYN_GBM:patient:GBM-2", "predicate": "HAS_CASE", "object_id": "SYN_GBM:case:GC-2", "source": "SYN_GBM", "source_record_id": "2"},
            {"subject_id": "SYN_GBM:case:GC-1", "predicate": "HAS_ASSAY", "object_id": "SYN_GBM:assay:GA-1", "source": "SYN_GBM", "source_record_id": "3"},
            {"subject_id": "SYN_GBM:case:GC-2", "predicate": "HAS_ASSAY", "object_id": "SYN_GBM:assay:GA-2", "source": "SYN_GBM", "source_record_id": "4"},
        ],
        "evidence": [],
        "pathology_reports": [],
        "identity_mappings": [],
        "provenance_metadata": {
            "declared_snapshot_id": snap_id,
            "declared_snapshot_sha256": snap_sha,
            "is_frozen": True,
            "freeze_timestamp": "2026-06-15T00:00:00Z",
            "supported_entity_levels": ["patient", "case", "sample", "aliquot", "assay", "file"],
            "trusted_signing_keys": [
                {"key_id": "KEY-SYN-TRUSTED", "algorithm": "ED25519_TEST_ENVELOPE", "status": "ACTIVE_TRUSTED"}
            ],
            "tables": {"assays": {"declared_completeness": "COMPLETE"}},
            "integrity_errors": [],
            "raw_tables_by_id": raw_records,
        },
    }
    query = {
        "operation": "cohort_count",
        "entity_level": "patient",
        "denominator_basis": "gbm_idh1",
        "predicate": {"histology": "GBM", "biomarker": "IDH1", "assay_status": "COMPLETED", "qc_status": "PASS", "result": "R132H_POSITIVE"},
    }
    result = {
        "status": "SUPPORTED",
        "claim_holds": None,
        "entity_level": "patient",
        "denominator_basis": "gbm_idh1",
        "numerator_count": 1,
        "denominator_count": 2,
    }
    supp_ids = ["GA-1", "GBM-1"]
    leaves = [_sha_obj({"evidence_id": eid, "record": raw_records[eid]}) for eid in sorted(supp_ids)]
    merkle = _binary_merkle(leaves)
    ev_refs = {
        "supporting_evidence_ids": supp_ids,
        "contradicting_evidence_ids": [],
        "witness_leaves_sha256": leaves,
    }
    digests_wo = {"query_sha256": _sha_obj(query), "result_sha256": _sha_obj(result), "evidence_merkle_root": merkle}
    body = {
        "certificate_id": "CERT-GBM-1",
        "snapshot_id": snap_id,
        "snapshot_sha256": snap_sha,
        "issued_at": "2026-06-15T00:00:00Z",
        "signer_key_id": "KEY-SYN-TRUSTED",
        "query": query,
        "result": result,
        "evidence_references": ev_refs,
        "digests_without_body_hash": digests_wo,
    }
    cert = {
        "certificate_id": "CERT-GBM-1",
        "snapshot_id": snap_id,
        "snapshot_sha256": snap_sha,
        "issued_at": "2026-06-15T00:00:00Z",
        "signer_key_id": "KEY-SYN-TRUSTED",
        "query": query,
        "result": result,
        "evidence_references": ev_refs,
        "digests": {**digests_wo, "certificate_body_sha256": _sha_obj(body)},
        "signature": {
            "signature_kind": "SYNTHETIC_EXAM_TEST_ENVELOPE",
            "key_id": "KEY-SYN-TRUSTED",
            "algorithm": "ED25519_TEST_ENVELOPE",
            "hex": "a" * 128,
        },
    }
    return bundle, cert


class ProvenanceAndTamperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_provenance_integrity_partial_and_unsupported_ontology_gates(self):
        bundle, _ = _make_test_envelope_bundle()
        clean_snap = self.root / "clean"
        write_snapshot(bundle, clean_snap)

        # Unsupported ontology level -> ERROR / UNSUPPORTED_ENTITY_LEVEL
        bad_lvl = evaluate(
            clean_snap,
            {"operation": "cohort_count", "entity_level": "mitochondria", "denominator_basis": "all", "predicate": {"biomarker": "IDH1"}},
        )
        self.assertEqual(bad_lvl["status"], "ERROR")
        self.assertEqual(bad_lvl["reason_code"], "UNSUPPORTED_ENTITY_LEVEL")

        # Partial table ingest -> UNKNOWN / INCOMPLETE_SNAPSHOT
        partial_bundle = copy.deepcopy(bundle)
        partial_bundle["provenance_metadata"]["tables"]["assays"]["declared_completeness"] = "PARTIAL_INGEST"
        partial_snap = self.root / "partial"
        write_snapshot(partial_bundle, partial_snap)
        partial_ans = evaluate(
            partial_snap,
            {"operation": "claim_verification", "entity_level": "patient", "denominator_basis": "all", "predicate": {"biomarker": "IDH1", "claim_numerator_count": 0}},
        )
        self.assertEqual(partial_ans["status"], "UNKNOWN")
        self.assertEqual(partial_ans["reason_code"], "INCOMPLETE_SNAPSHOT")

        # Integrity error + unfrozen snapshot -> ERROR
        broken_bundle = copy.deepcopy(bundle)
        broken_bundle["provenance_metadata"]["integrity_errors"] = [
            {"code": "CHECKSUM_MISMATCH", "reason": "SNAPSHOT_MISMATCH"}
        ]
        broken_snap = self.root / "broken"
        write_snapshot(broken_bundle, broken_snap, complete=False)
        broken_ans = evaluate(
            broken_snap,
            {"operation": "cohort_count", "entity_level": "patient", "denominator_basis": "gbm_idh1", "predicate": {"biomarker": "IDH1"}},
        )
        self.assertEqual(broken_ans["status"], "ERROR")
        self.assertEqual(broken_ans["reason_code"], "SNAPSHOT_MISMATCH")

    def test_synthetic_test_envelope_replay_and_tamper_modes(self):
        bundle, cert = _make_test_envelope_bundle()
        snap = self.root / "gbm"
        write_snapshot(bundle, snap)

        valid_cert = self.root / "valid.json"
        valid_cert.write_text(json.dumps(cert), encoding="utf-8")
        ok = replay_certificate(snap, valid_cert)
        self.assertTrue(ok["valid"], ok)
        self.assertEqual(ok["status"], "SUPPORTED")
        self.assertTrue(ok["replay_verified"])

        # 1. Tampered query
        t_query = copy.deepcopy(cert)
        t_query["query"]["predicate"]["histology"] = "LGG"
        p1 = self.root / "t_query.json"
        p1.write_text(json.dumps(t_query), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, p1)["valid"])

        # 2. Tampered result
        t_res = copy.deepcopy(cert)
        t_res["result"]["numerator_count"] = 2
        p2 = self.root / "t_res.json"
        p2.write_text(json.dumps(t_res), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, p2)["valid"])

        # 3. Tampered evidence list
        t_ev = copy.deepcopy(cert)
        t_ev["evidence_references"]["supporting_evidence_ids"] = ["GA-2", "GBM-1"]
        p3 = self.root / "t_ev.json"
        p3.write_text(json.dumps(t_ev), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, p3)["valid"])

        # 4. Tampered snapshot digest
        t_snap = copy.deepcopy(cert)
        t_snap["snapshot_sha256"] = "0" * 64
        p4 = self.root / "t_snap.json"
        p4.write_text(json.dumps(t_snap), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, p4)["valid"])

        # 5. Untrusted signer key
        t_key = copy.deepcopy(cert)
        t_key["signer_key_id"] = "KEY-ROGUE"
        t_key["signature"]["key_id"] = "KEY-ROGUE"
        p5 = self.root / "t_key.json"
        p5.write_text(json.dumps(t_key), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, p5)["valid"])

        # 6. Selectively omitted contradicting evidence (valid digests & key, but wrong SQL replay answer)
        conflict_bundle = copy.deepcopy(bundle)
        conflict_bundle["pathology_reports"] = [
            {"report_id": "RP-1", "patient_id": "GBM-1", "case_id": "GC-1", "stage": "GRADE_II", "record_status": "ACTIVE_FINAL"},
            {"report_id": "RP-2", "patient_id": "GBM-1", "case_id": "GC-1", "stage": "GRADE_IV", "record_status": "ACTIVE_FINAL"},
        ]
        conflict_bundle["provenance_metadata"]["raw_tables_by_id"]["RP-1"] = conflict_bundle["pathology_reports"][0]
        conflict_bundle["provenance_metadata"]["raw_tables_by_id"]["RP-2"] = conflict_bundle["pathology_reports"][1]
        conf_snap = self.root / "conf_snap"
        write_snapshot(conflict_bundle, conf_snap)
        omit_q = {"operation": "claim_verification", "subject_ref": {"patient_id": "GBM-1"}, "predicate": {"field": "stage", "expected_value": "GRADE_II"}}
        omit_res = {"status": "SUPPORTED", "claim_holds": True, "entity_level": "patient", "denominator_basis": None, "numerator_count": None, "denominator_count": None}
        omit_leaves = [_sha_obj({"evidence_id": "RP-1", "record": conflict_bundle["pathology_reports"][0]})]
        omit_ev = {"supporting_evidence_ids": ["RP-1"], "contradicting_evidence_ids": [], "witness_leaves_sha256": omit_leaves}
        omit_wo = {"query_sha256": _sha_obj(omit_q), "result_sha256": _sha_obj(omit_res), "evidence_merkle_root": _binary_merkle(omit_leaves)}
        omit_body = {
            "certificate_id": "CERT-OMIT",
            "snapshot_id": bundle["provenance_metadata"]["declared_snapshot_id"],
            "snapshot_sha256": bundle["provenance_metadata"]["declared_snapshot_sha256"],
            "issued_at": "2026-06-15T00:00:00Z",
            "signer_key_id": "KEY-SYN-TRUSTED",
            "query": omit_q,
            "result": omit_res,
            "evidence_references": omit_ev,
            "digests_without_body_hash": omit_wo,
        }
        omit_cert = {
            "certificate_id": "CERT-OMIT",
            "snapshot_id": bundle["provenance_metadata"]["declared_snapshot_id"],
            "snapshot_sha256": bundle["provenance_metadata"]["declared_snapshot_sha256"],
            "issued_at": "2026-06-15T00:00:00Z",
            "signer_key_id": "KEY-SYN-TRUSTED",
            "query": omit_q,
            "result": omit_res,
            "evidence_references": omit_ev,
            "digests": {**omit_wo, "certificate_body_sha256": _sha_obj(omit_body)},
            "signature": cert["signature"],
        }
        p6 = self.root / "t_omit.json"
        p6.write_text(json.dumps(omit_cert), encoding="utf-8")
        r6 = replay_certificate(conf_snap, p6)
        self.assertFalse(r6["valid"])
        self.assertEqual(r6["reason_code"], "RESULT_MISMATCH")
