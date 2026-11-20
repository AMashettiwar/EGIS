import copy
import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import certificate_digest, issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate
from cancer_evidence.signing import sign
from cancer_evidence.snapshot import canonical_json, write_snapshot


def _melanoma_bundle() -> dict:
    entities = []
    relationships = []
    evidence = []

    def add(entity_id: str, kind: str, source: str, source_id: str, **attrs) -> None:
        entities.append(
            {
                "entity_id": entity_id,
                "kind": kind,
                "source": source,
                "source_id": source_id,
                "attributes": attrs,
            }
        )
        for field, value in attrs.items():
            evidence.append(
                {
                    "evidence_id": f"ev:{entity_id}:{field}",
                    "entity_id": entity_id,
                    "field_name": field,
                    "raw_value": value,
                    "canonical_value": value,
                    "source_record_id": source_id,
                }
            )

    def link(subject: str, predicate: str, object_id: str, record: str) -> None:
        relationships.append(
            {
                "subject_id": subject,
                "predicate": predicate,
                "object_id": object_id,
                "source": "SYN_MEL",
                "source_record_id": record,
            }
        )

    # M-PT-01: eligible Stage IV CUTANEOUS_MELANOMA, 2 samples, 2 positive BRAF assays -> counts as 1 patient
    add("SYN_MEL:patient:M-PT-01", "patient", "SYN_MEL", "M-PT-01", birth_year=1965, sex="F")
    add("SYN_MEL:case:M-CASE-01", "case", "SYN_MEL", "M-CASE-01", primary_site="SKIN", histology="CUTANEOUS_MELANOMA", stage="STAGE_IV")
    add("SYN_MEL:sample:M-SMP-1A", "sample", "SYN_MEL", "M-SMP-1A", sample_type="PRIMARY")
    add("SYN_MEL:sample:M-SMP-1B", "sample", "SYN_MEL", "M-SMP-1B", sample_type="METASTATIC")
    add("SYN_MEL:aliquot:M-ALQ-1A", "aliquot", "SYN_MEL", "M-ALQ-1A", analyte="DNA")
    add("SYN_MEL:aliquot:M-ALQ-1B", "aliquot", "SYN_MEL", "M-ALQ-1B", analyte="DNA")
    add("SYN_MEL:assay:M-ASY-1A", "assay", "SYN_MEL", "M-ASY-1A", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="V600E_POS")
    add("SYN_MEL:assay:M-ASY-1B", "assay", "SYN_MEL", "M-ASY-1B", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="V600E_POS")
    link("SYN_MEL:patient:M-PT-01", "HAS_CASE", "SYN_MEL:case:M-CASE-01", "M-PT-01->M-CASE-01")
    link("SYN_MEL:case:M-CASE-01", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-1A", "M-CASE-01->M-SMP-1A")
    link("SYN_MEL:case:M-CASE-01", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-1B", "M-CASE-01->M-SMP-1B")
    link("SYN_MEL:sample:M-SMP-1A", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-1A", "M-SMP-1A->M-ALQ-1A")
    link("SYN_MEL:sample:M-SMP-1B", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-1B", "M-SMP-1B->M-ALQ-1B")
    link("SYN_MEL:aliquot:M-ALQ-1A", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-1A", "M-ALQ-1A->M-ASY-1A")
    link("SYN_MEL:aliquot:M-ALQ-1B", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-1B", "M-ALQ-1B->M-ASY-1B")

    # M-PT-02: eligible Stage III CUTANEOUS_MELANOMA, 1 positive V600K assay
    add("SYN_MEL:patient:M-PT-02", "patient", "SYN_MEL", "M-PT-02", birth_year=1972, sex="M")
    add("SYN_MEL:case:M-CASE-02", "case", "SYN_MEL", "M-CASE-02", primary_site="SKIN", histology="CUTANEOUS_MELANOMA", stage="STAGE_III")
    add("SYN_MEL:sample:M-SMP-02", "sample", "SYN_MEL", "M-SMP-02", sample_type="PRIMARY")
    add("SYN_MEL:aliquot:M-ALQ-02", "aliquot", "SYN_MEL", "M-ALQ-02", analyte="DNA")
    add("SYN_MEL:assay:M-ASY-02", "assay", "SYN_MEL", "M-ASY-02", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="V600K_POS")
    link("SYN_MEL:patient:M-PT-02", "HAS_CASE", "SYN_MEL:case:M-CASE-02", "M-PT-02->M-CASE-02")
    link("SYN_MEL:case:M-CASE-02", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-02", "M-CASE-02->M-SMP-02")
    link("SYN_MEL:sample:M-SMP-02", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-02", "M-SMP-02->M-ALQ-02")
    link("SYN_MEL:aliquot:M-ALQ-02", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-02", "M-ALQ-02->M-ASY-02")

    # M-PT-03: eligible Stage IV CUTANEOUS_MELANOMA, WT_NEGATIVE
    add("SYN_MEL:patient:M-PT-03", "patient", "SYN_MEL", "M-PT-03", birth_year=1959, sex="M")
    add("SYN_MEL:case:M-CASE-03", "case", "SYN_MEL", "M-CASE-03", primary_site="SKIN", histology="CUTANEOUS_MELANOMA", stage="STAGE_IV")
    add("SYN_MEL:sample:M-SMP-03", "sample", "SYN_MEL", "M-SMP-03", sample_type="PRIMARY")
    add("SYN_MEL:aliquot:M-ALQ-03", "aliquot", "SYN_MEL", "M-ALQ-03", analyte="DNA")
    add("SYN_MEL:assay:M-ASY-03", "assay", "SYN_MEL", "M-ASY-03", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="WT_NEGATIVE")
    link("SYN_MEL:patient:M-PT-03", "HAS_CASE", "SYN_MEL:case:M-CASE-03", "M-PT-03->M-CASE-03")
    link("SYN_MEL:case:M-CASE-03", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-03", "M-CASE-03->M-SMP-03")
    link("SYN_MEL:sample:M-SMP-03", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-03", "M-SMP-03->M-ALQ-03")
    link("SYN_MEL:aliquot:M-ALQ-03", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-03", "M-ALQ-03->M-ASY-03")

    # M-PT-04: ineligible Stage I CUTANEOUS_MELANOMA even though BRAF positive
    add("SYN_MEL:patient:M-PT-04", "patient", "SYN_MEL", "M-PT-04", birth_year=1980, sex="F")
    add("SYN_MEL:case:M-CASE-04", "case", "SYN_MEL", "M-CASE-04", primary_site="SKIN", histology="CUTANEOUS_MELANOMA", stage="STAGE_I")
    add("SYN_MEL:sample:M-SMP-04", "sample", "SYN_MEL", "M-SMP-04", sample_type="PRIMARY")
    add("SYN_MEL:aliquot:M-ALQ-04", "aliquot", "SYN_MEL", "M-ALQ-04", analyte="DNA")
    add("SYN_MEL:assay:M-ASY-04", "assay", "SYN_MEL", "M-ASY-04", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="V600E_POS")
    link("SYN_MEL:patient:M-PT-04", "HAS_CASE", "SYN_MEL:case:M-CASE-04", "M-PT-04->M-CASE-04")
    link("SYN_MEL:case:M-CASE-04", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-04", "M-CASE-04->M-SMP-04")
    link("SYN_MEL:sample:M-SMP-04", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-04", "M-SMP-04->M-ALQ-04")
    link("SYN_MEL:aliquot:M-ALQ-04", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-04", "M-ALQ-04->M-ASY-04")

    # M-PT-05: ineligible histology MUCOSAL_MELANOMA
    add("SYN_MEL:patient:M-PT-05", "patient", "SYN_MEL", "M-PT-05", birth_year=1954, sex="F")
    add("SYN_MEL:case:M-CASE-05", "case", "SYN_MEL", "M-CASE-05", primary_site="MUCOSA", histology="MUCOSAL_MELANOMA", stage="STAGE_IV")
    add("SYN_MEL:sample:M-SMP-05", "sample", "SYN_MEL", "M-SMP-05", sample_type="PRIMARY")
    add("SYN_MEL:aliquot:M-ALQ-05", "aliquot", "SYN_MEL", "M-ALQ-05", analyte="DNA")
    add("SYN_MEL:assay:M-ASY-05", "assay", "SYN_MEL", "M-ASY-05", biomarker="BRAF_V600", assay_status="COMPLETED", qc_status="PASS", result="WT_NEGATIVE")
    link("SYN_MEL:patient:M-PT-05", "HAS_CASE", "SYN_MEL:case:M-CASE-05", "M-PT-05->M-CASE-05")
    link("SYN_MEL:case:M-CASE-05", "HAS_SAMPLE", "SYN_MEL:sample:M-SMP-05", "M-CASE-05->M-SMP-05")
    link("SYN_MEL:sample:M-SMP-05", "HAS_ALIQUOT", "SYN_MEL:aliquot:M-ALQ-05", "M-SMP-05->M-ALQ-05")
    link("SYN_MEL:aliquot:M-ALQ-05", "HAS_ASSAY", "SYN_MEL:assay:M-ASY-05", "M-ALQ-05->M-ASY-05")

    return {
        "source": "SYN_MEL",
        "coverage": {"SYN_MEL": {"scope": "melanoma-synthetic", "complete": True, "expected_total": 5, "retrieved_count": 5}},
        "entities": entities,
        "relationships": relationships,
        "evidence": evidence,
    }


def _resign_certificate(cert: dict) -> dict:
    unsigned = dict(cert)
    unsigned.pop("certificate_sha256", None)
    unsigned.pop("signature", None)
    cert["signature"] = sign(canonical_json(unsigned).encode("utf-8"))
    cert["certificate_sha256"] = certificate_digest(cert)
    return cert


class CohortCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        write_snapshot(_melanoma_bundle(), self.snapshot)
        self.query = {
            "operation": "cohort_count",
            "entity_level": "patient",
            "denominator_basis": "eligible_stage_III_IV_cutaneous_melanoma_with_complete_BRAF_assay",
            "predicate": {
                "histology_in": ["CUTANEOUS_MELANOMA"],
                "stage_in": ["STAGE_III", "STAGE_IV"],
                "biomarker": "BRAF_V600",
                "assay_status": "COMPLETED",
                "qc_status": "PASS",
                "positive_results_in": ["V600E_POS", "V600K_POS"],
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_patient_cohort_count_deduplicates_multi_assay_patients_and_replays(self):
        answer = evaluate(self.snapshot, self.query)
        self.assertEqual(answer["status"], "SUPPORTED")
        self.assertEqual(answer["reason_code"], "NONE")
        self.assertEqual(answer["result"]["numerator_count"], 2)
        self.assertEqual(answer["result"]["denominator_count"], 3)
        self.assertEqual(answer["result"]["entity_level"], "patient")
        self.assertEqual(
            answer["result"]["denominator_basis"],
            "eligible_stage_III_IV_cutaneous_melanoma_with_complete_BRAF_assay",
        )
        self.assertEqual(answer["result"]["eligible_patient_ids"], ["M-PT-01", "M-PT-02", "M-PT-03"])
        self.assertEqual(answer["witness"], ["M-PT-01", "M-PT-02"])
        self.assertEqual(
            answer["evidence_refs"],
            ["M-ASY-02", "M-ASY-1A", "M-ASY-1B", "M-PT-01", "M-PT-02"],
        )

        cert_path = self.root / "cert.json"
        issue_certificate(self.snapshot, self.query, cert_path)
        replay = replay_certificate(self.snapshot, cert_path)
        self.assertEqual(replay, {"valid": True, "status": "SUPPORTED", "reason_code": "NONE"})

    def test_sql_replay_rejects_resigned_tampered_counts_or_witnesses(self):
        cert_path = self.root / "cert.json"
        base_cert = issue_certificate(self.snapshot, self.query, cert_path)

        for field_path in ("numerator", "denominator", "witness", "evidence_refs"):
            mutated = copy.deepcopy(base_cert)
            if field_path == "numerator":
                mutated["result"]["result"]["numerator"] = 3
                mutated["result"]["result"]["numerator_count"] = 3
            elif field_path == "denominator":
                mutated["result"]["result"]["denominator"] = 5
                mutated["result"]["result"]["denominator_count"] = 5
            elif field_path == "witness":
                mutated["result"]["witness"] = ["M-PT-01", "M-PT-04"]
            elif field_path == "evidence_refs":
                mutated["result"]["evidence_refs"] = ["M-PT-01"]
            _resign_certificate(mutated)
            tampered_path = self.root / f"tampered_{field_path}.json"
            tampered_path.write_text(json.dumps(mutated), encoding="utf-8")
            replay = replay_certificate(self.snapshot, tampered_path)
            self.assertFalse(replay["valid"], field_path)
            self.assertEqual(replay["reason_code"], "RESULT_MISMATCH", field_path)

    def test_incomplete_assay_on_eligible_patient_abstains_and_replays(self):
        bundle = _melanoma_bundle()
        for ent in bundle["entities"]:
            if ent["entity_id"] == "SYN_MEL:assay:M-ASY-03":
                ent["attributes"]["assay_status"] = "NOT_PERFORMED"
                ent["attributes"]["result"] = None
        snap = self.root / "incomplete-assay"
        write_snapshot(bundle, snap)
        answer = evaluate(snap, self.query)
        self.assertEqual(answer["status"], "UNKNOWN")
        self.assertEqual(answer["reason_code"], "INCOMPLETE_SNAPSHOT")

        cert_path = self.root / "incomplete_cert.json"
        cert_data = issue_certificate(snap, self.query, cert_path)
        self.assertEqual(cert_data["result"]["reason_code"], "INCOMPLETE_SNAPSHOT")
        self.assertEqual(
            replay_certificate(snap, cert_path),
            {"valid": True, "status": "UNKNOWN", "reason_code": "NONE"},
        )

    def test_zero_denominator_and_ambiguous_inputs_abstain_and_replay(self):
        zero_query = copy.deepcopy(self.query)
        zero_query["predicate"]["histology_in"] = ["UVEAL_MELANOMA"]
        zero_ans = evaluate(self.snapshot, zero_query)
        self.assertEqual(zero_ans["status"], "UNKNOWN")
        self.assertEqual(zero_ans["reason_code"], "ZERO_DENOMINATOR")
        zero_cert = self.root / "zero_cert.json"
        zero_cert_data = issue_certificate(self.snapshot, zero_query, zero_cert)
        self.assertEqual(zero_cert_data["result"]["reason_code"], "ZERO_DENOMINATOR")
        self.assertEqual(
            replay_certificate(self.snapshot, zero_cert),
            {"valid": True, "status": "UNKNOWN", "reason_code": "NONE"},
        )

        ambig_query = copy.deepcopy(self.query)
        ambig_query["entity_level"] = None
        ambig_ans = evaluate(self.snapshot, ambig_query)
        self.assertEqual(ambig_ans["status"], "AMBIGUOUS")
        self.assertEqual(ambig_ans["reason_code"], "AMBIGUOUS_SELECTOR")
        ambig_cert = self.root / "ambig_cert.json"
        ambig_cert_data = issue_certificate(self.snapshot, ambig_query, ambig_cert)
        self.assertEqual(ambig_cert_data["result"]["reason_code"], "AMBIGUOUS_SELECTOR")
        self.assertEqual(
            replay_certificate(self.snapshot, ambig_cert),
            {"valid": True, "status": "AMBIGUOUS", "reason_code": "NONE"},
        )

    def test_eligible_conflict_abstains_while_ineligible_assay_conflict_does_not_poison(self):
        inelig_bundle = _melanoma_bundle()
        inelig_bundle["evidence"].append(
            {
                "evidence_id": "ev:M-ASY-04:conflict",
                "entity_id": "SYN_MEL:assay:M-ASY-04",
                "field_name": "result",
                "raw_value": "WT_NEGATIVE",
                "canonical_value": "WT_NEGATIVE",
                "source_record_id": "M-ASY-04-conflict",
            }
        )
        inelig_snap = self.root / "inelig-conflict"
        write_snapshot(inelig_bundle, inelig_snap)
        self.assertEqual(evaluate(inelig_snap, self.query)["status"], "SUPPORTED")

        elig_bundle = _melanoma_bundle()
        elig_bundle["evidence"].append(
            {
                "evidence_id": "ev:M-ASY-02:conflict",
                "entity_id": "SYN_MEL:assay:M-ASY-02",
                "field_name": "result",
                "raw_value": "WT_NEGATIVE",
                "canonical_value": "WT_NEGATIVE",
                "source_record_id": "M-ASY-02-conflict",
            }
        )
        elig_snap = self.root / "elig-conflict"
        write_snapshot(elig_bundle, elig_snap)
        ans = evaluate(elig_snap, self.query)
        self.assertEqual(ans["status"], "CONFLICT")
        self.assertEqual(ans["reason_code"], "CONFLICTING_EVIDENCE")
        cert = self.root / "conflict_cert.json"
        cert_data = issue_certificate(elig_snap, self.query, cert)
        self.assertEqual(cert_data["result"]["reason_code"], "CONFLICTING_EVIDENCE")
        self.assertEqual(
            replay_certificate(elig_snap, cert),
            {"valid": True, "status": "CONFLICT", "reason_code": "NONE"},
        )
