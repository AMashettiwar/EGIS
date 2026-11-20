import copy
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import write_snapshot


def _base_bundle(source: str = "SYN_LAB") -> dict:
    return {
        "source": source,
        "coverage": {source: {"scope": "unit-test", "complete": True}},
        "entities": [],
        "relationships": [],
        "evidence": [],
        "pathology_reports": [],
        "identity_mappings": [],
    }


def _add_entity(bundle: dict, kind: str, source: str, source_id: str, **attrs) -> str:
    eid = f"{source}:{kind}:{source_id}"
    bundle["entities"].append(
        {
            "entity_id": eid,
            "kind": kind,
            "source": source,
            "source_id": source_id,
            "attributes": attrs,
        }
    )
    return eid


def _link(bundle: dict, sub: str, pred: str, obj: str, source: str, rec: str) -> None:
    bundle["relationships"].append(
        {
            "subject_id": sub,
            "predicate": pred,
            "object_id": obj,
            "source": source,
            "source_record_id": rec,
        }
    )


class ClaimsAndMappingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _assert_eval_and_replay(self, snap: Path, query: dict, expected_status: str) -> dict:
        ans = evaluate(snap, query)
        self.assertEqual(ans["status"], expected_status, ans)
        cert_path = self.root / f"cert_{len(list(self.root.glob('*.json')))}.json"
        issue_certificate(snap, query, cert_path)
        rep = replay_certificate(snap, cert_path)
        self.assertTrue(rep["valid"], (ans, rep))
        self.assertEqual(rep["status"], expected_status)
        return ans

    def test_biomarker_single_subject_contradicted_untested_and_conflict(self):
        bundle = _base_bundle("OV_SYN")
        p1 = _add_entity(bundle, "patient", "OV_SYN", "OV-1")
        c1 = _add_entity(bundle, "case", "OV_SYN", "C-1", patient_id="OV-1", histology="HGSOC", stage="STAGE_III")
        a1 = _add_entity(
            bundle,
            "assay",
            "OV_SYN",
            "ASY-1",
            patient_id="OV-1",
            biomarker="BRCA1_SOMATIC",
            assay_status="COMPLETED",
            qc_status="PASS",
            result="WT_NEGATIVE",
        )
        f1 = _add_entity(bundle, "file", "OV_SYN", "FIL-1", file_type="VCF")
        _link(bundle, p1, "HAS_CASE", c1, "OV_SYN", "1")
        _link(bundle, c1, "HAS_ASSAY", a1, "OV_SYN", "2")
        _link(bundle, a1, "HAS_FILE", f1, "OV_SYN", "3")

        snap = self.root / "ov_neg"
        write_snapshot(bundle, snap)
        q_pos = {
            "operation": "claim_verification",
            "subject_ref": {"patient_id": "OV-1"},
            "predicate": {"biomarker": "BRCA1_SOMATIC", "expected_result": "POSITIVE"},
        }
        ans = self._assert_eval_and_replay(snap, q_pos, "CONTRADICTED")
        self.assertFalse(ans["result"]["claim_holds"])
        self.assertEqual(ans["result"]["contradicting_evidence_ids"], ["ASY-1", "FIL-1"])

        # Untested assay -> UNKNOWN (never CONTRADICTED)
        untested = copy.deepcopy(bundle)
        untested["entities"][2]["attributes"]["assay_status"] = "NOT_PERFORMED"
        untested["entities"][2]["attributes"]["result"] = None
        snap_untested = self.root / "ov_untested"
        write_snapshot(untested, snap_untested)
        q_neg = {
            "operation": "claim_verification",
            "subject_ref": {"patient_id": "OV-1"},
            "predicate": {"biomarker": "BRCA1_SOMATIC", "expected_result": "WT_NEGATIVE"},
        }
        self._assert_eval_and_replay(snap_untested, q_neg, "UNKNOWN")

        # Conflicting replicates -> CONFLICT
        conflict = copy.deepcopy(bundle)
        a2 = _add_entity(
            conflict,
            "assay",
            "OV_SYN",
            "ASY-2",
            patient_id="OV-1",
            biomarker="BRCA1_SOMATIC",
            assay_status="COMPLETED",
            qc_status="PASS",
            result="POSITIVE",
            adjudication_status="UNRESOLVED",
        )
        _link(conflict, c1, "HAS_ASSAY", a2, "OV_SYN", "4")
        snap_conf = self.root / "ov_conf"
        write_snapshot(conflict, snap_conf)
        self._assert_eval_and_replay(snap_conf, q_pos, "CONFLICT")

    def test_universal_zero_claim_partial_vs_counterexample_vs_all_negative(self):
        bundle = _base_bundle("CRC_SYN")
        for idx, (st, res) in enumerate(
            [("COMPLETED", "WT_NEGATIVE"), ("COMPLETED", "POSITIVE"), ("NOT_PERFORMED", None)], start=1
        ):
            p = _add_entity(bundle, "patient", "CRC_SYN", f"CRC-{idx}")
            c = _add_entity(bundle, "case", "CRC_SYN", f"CC-{idx}", patient_id=f"CRC-{idx}")
            a = _add_entity(
                bundle,
                "assay",
                "CRC_SYN",
                f"CA-{idx}",
                patient_id=f"CRC-{idx}",
                biomarker="NRAS_Q61",
                assay_status=st,
                qc_status="PASS" if st == "COMPLETED" else "NOT_APPLICABLE",
                result=res,
            )
            _link(bundle, p, "HAS_CASE", c, "CRC_SYN", f"p{idx}")
            _link(bundle, c, "HAS_ASSAY", a, "CRC_SYN", f"a{idx}")

        q = {
            "operation": "claim_verification",
            "entity_level": "patient",
            "denominator_basis": "all_3_patients",
            "predicate": {"biomarker": "NRAS_Q61", "claim_numerator_count": 0},
        }
        snap_counter = self.root / "crc_counter"
        write_snapshot(bundle, snap_counter)
        ans = self._assert_eval_and_replay(snap_counter, q, "CONTRADICTED")
        self.assertEqual(ans["result"]["contradicting_evidence_ids"], ["CA-2", "CRC-2"])

        # Change positive patient to WT_NEGATIVE while 3rd stays NOT_PERFORMED -> UNKNOWN
        partial = copy.deepcopy(bundle)
        for e in partial["entities"]:
            if e["source_id"] == "CA-2":
                e["attributes"]["result"] = "WT_NEGATIVE"
        snap_partial = self.root / "crc_partial"
        write_snapshot(partial, snap_partial)
        self._assert_eval_and_replay(snap_partial, q, "UNKNOWN")

        # Complete all 3 negative -> SUPPORTED
        all_neg = copy.deepcopy(partial)
        for e in all_neg["entities"]:
            if e["source_id"] == "CA-3":
                e["attributes"]["assay_status"] = "COMPLETED"
                e["attributes"]["qc_status"] = "PASS"
                e["attributes"]["result"] = "WT_NEGATIVE"
        snap_all_neg = self.root / "crc_all_neg"
        write_snapshot(all_neg, snap_all_neg)
        ok_ans = self._assert_eval_and_replay(snap_all_neg, q, "SUPPORTED")
        self.assertTrue(ok_ans["result"]["claim_holds"])
        self.assertEqual(ok_ans["result"]["numerator_count"], 0)
        self.assertEqual(ok_ans["result"]["denominator_count"], 3)

    def test_pathology_supersession_and_active_conflict(self):
        bundle = _base_bundle("PATH_SYN")
        _add_entity(bundle, "patient", "PATH_SYN", "P-9")
        bundle["pathology_reports"] = [
            {
                "report_id": "R-V1",
                "case_id": "C-9",
                "patient_id": "P-9",
                "stage": "STAGE_II",
                "record_status": "SUPERSEDED",
                "superseded_by": "R-V2",
            },
            {
                "report_id": "R-V2",
                "case_id": "C-9",
                "patient_id": "P-9",
                "stage": "STAGE_IV",
                "record_status": "ACTIVE_FINAL",
                "supersedes": "R-V1",
            },
        ]
        snap = self.root / "path_sup"
        write_snapshot(bundle, snap)

        ok = self._assert_eval_and_replay(
            snap,
            {"operation": "claim_verification", "subject_ref": {"patient_id": "P-9"}, "predicate": {"field": "stage", "expected_value": "STAGE_IV"}},
            "SUPPORTED",
        )
        self.assertEqual(ok["result"]["supporting_evidence_ids"], ["R-V2"])

        bad = self._assert_eval_and_replay(
            snap,
            {"operation": "claim_verification", "subject_ref": {"patient_id": "P-9"}, "predicate": {"field": "stage", "expected_value": "STAGE_II"}},
            "CONTRADICTED",
        )
        self.assertEqual(bad["result"]["contradicting_evidence_ids"], ["R-V1", "R-V2"])

        # Two conflicting ACTIVE_FINAL reports -> CONFLICT
        conf_bundle = copy.deepcopy(bundle)
        conf_bundle["pathology_reports"][0]["record_status"] = "ACTIVE_FINAL"
        conf_bundle["pathology_reports"][0].pop("superseded_by", None)
        snap_conf = self.root / "path_conf"
        write_snapshot(conf_bundle, snap_conf)
        self._assert_eval_and_replay(
            snap_conf,
            {"operation": "claim_verification", "subject_ref": {"patient_id": "P-9"}, "predicate": {"required_stage": "STAGE_IV"}},
            "CONFLICT",
        )

    def test_cross_source_mapping_governance_states(self):
        base = {
            "source": "MULTI",
            "coverage": {
                "COHORT_X": {"scope": "x", "complete": True, "expected_total": 2, "retrieved_count": 2},
                "COHORT_Y": {"scope": "y", "complete": True, "expected_total": 2, "retrieved_count": 2},
            },
            "entities": [
                {"entity_id": "COHORT_X:patient:X1", "kind": "patient", "source": "COHORT_X", "source_id": "X1", "attributes": {}},
                {"entity_id": "COHORT_X:patient:X2", "kind": "patient", "source": "COHORT_X", "source_id": "X2", "attributes": {}},
                {"entity_id": "COHORT_X:case:CX1", "kind": "case", "source": "COHORT_X", "source_id": "CX1", "attributes": {}},
                {"entity_id": "COHORT_X:case:CX2", "kind": "case", "source": "COHORT_X", "source_id": "CX2", "attributes": {}},
                {"entity_id": "COHORT_Y:patient:Y1", "kind": "patient", "source": "COHORT_Y", "source_id": "Y1", "attributes": {}},
                {"entity_id": "COHORT_Y:patient:Y2", "kind": "patient", "source": "COHORT_Y", "source_id": "Y2", "attributes": {}},
                {"entity_id": "COHORT_Y:case:CY1", "kind": "case", "source": "COHORT_Y", "source_id": "CY1", "attributes": {}},
                {"entity_id": "COHORT_Y:case:CY2", "kind": "case", "source": "COHORT_Y", "source_id": "CY2", "attributes": {}},
            ],
            "relationships": [
                {"subject_id": "COHORT_X:patient:X1", "predicate": "HAS_CASE", "object_id": "COHORT_X:case:CX1", "source": "COHORT_X", "source_record_id": "1"},
                {"subject_id": "COHORT_X:patient:X2", "predicate": "HAS_CASE", "object_id": "COHORT_X:case:CX2", "source": "COHORT_X", "source_record_id": "2"},
                {"subject_id": "COHORT_Y:patient:Y1", "predicate": "HAS_CASE", "object_id": "COHORT_Y:case:CY1", "source": "COHORT_Y", "source_record_id": "3"},
                {"subject_id": "COHORT_Y:patient:Y2", "predicate": "HAS_CASE", "object_id": "COHORT_Y:case:CY2", "source": "COHORT_Y", "source_record_id": "4"},
            ],
            "evidence": [],
            "identity_mappings": [],
        }
        q = {
            "operation": "cross_source_overlap_count",
            "entity_level": "patient",
            "denominator_basis": "deduplicated_union",
            "requested_sources": ["COHORT_X", "COHORT_Y"],
        }

        # No mapping -> UNKNOWN
        s_none = self.root / "xs_none"
        write_snapshot(base, s_none)
        self._assert_eval_and_replay(s_none, q, "UNKNOWN")

        # Draft mapping -> UNKNOWN
        b_draft = copy.deepcopy(base)
        b_draft["identity_mappings"] = [{"mapping_id": "M1", "source_a": "COHORT_X", "source_b": "COHORT_Y", "source_a_id": "X1", "source_b_id": "Y1", "approval_status": "DRAFT_UNAPPROVED", "cardinality": "ONE_TO_ONE_EXCLUSIVE"}]
        s_draft = self.root / "xs_draft"
        write_snapshot(b_draft, s_draft)
        self._assert_eval_and_replay(s_draft, q, "UNKNOWN")

        # 1:N ambiguous -> AMBIGUOUS
        b_ambig = copy.deepcopy(base)
        b_ambig["identity_mappings"] = [{"mapping_id": "M1", "source_a": "COHORT_X", "source_b": "COHORT_Y", "source_a_id": "X1", "source_b_id": "Y1", "approval_status": "APPROVED_CANDIDATE_SET", "cardinality": "ONE_TO_MANY", "resolution": "AMBIGUOUS_ONE_TO_MANY"}]
        s_ambig = self.root / "xs_ambig"
        write_snapshot(b_ambig, s_ambig)
        self._assert_eval_and_replay(s_ambig, q, "AMBIGUOUS")

        # Conflicting approved -> CONFLICT
        b_conf = copy.deepcopy(base)
        b_conf["identity_mappings"] = [
            {"mapping_id": "M1", "source_a": "COHORT_X", "source_b": "COHORT_Y", "source_a_id": "X1", "source_b_id": "Y1", "approval_status": "APPROVED"},
            {"mapping_id": "M2", "source_a": "COHORT_X", "source_b": "COHORT_Y", "source_a_id": "X1", "source_b_id": "Y2", "approval_status": "APPROVED"},
        ]
        s_conf = self.root / "xs_conf"
        write_snapshot(b_conf, s_conf)
        self._assert_eval_and_replay(s_conf, q, "CONFLICT")

        # Approved 1:1 -> SUPPORTED (1 matched pair out of 3 deduplicated union patients)
        b_ok = copy.deepcopy(base)
        b_ok["identity_mappings"] = [{"mapping_id": "M1", "source_a": "COHORT_X", "source_b": "COHORT_Y", "source_a_id": "X1", "source_b_id": "Y1", "approval_status": "APPROVED", "cardinality": "ONE_TO_ONE_EXCLUSIVE"}]
        s_ok = self.root / "xs_ok"
        write_snapshot(b_ok, s_ok)
        ok = self._assert_eval_and_replay(s_ok, q, "SUPPORTED")
        self.assertEqual(ok["result"]["numerator_count"], 1)
        self.assertEqual(ok["result"]["denominator_count"], 3)

    def test_sample_file_boundaries_empty_subcohort_dedup_and_alias(self):
        bundle = _base_bundle("SAR")
        p1 = _add_entity(bundle, "patient", "SAR", "SP-1", legacy_subject_alias="ALIAS-X")
        p2 = _add_entity(bundle, "patient", "SAR", "SP-2", legacy_subject_alias="ALIAS-X")
        c1a = _add_entity(bundle, "case", "SAR", "SC-1A", patient_id="SP-1", histology="SYNOVIAL", stage="STAGE_III", is_duplicate_case_entry=False)
        c1b = _add_entity(bundle, "case", "SAR", "SC-1B", patient_id="SP-1", histology="SYNOVIAL", stage="STAGE_III", is_duplicate_case_entry=True)
        c2 = _add_entity(bundle, "case", "SAR", "SC-2", patient_id="SP-2", histology="SYNOVIAL", stage="STAGE_III", is_duplicate_case_entry=False)
        s1 = _add_entity(bundle, "sample", "SAR", "SS-1", tumor_purity=0.20, collection_timestamp="2026-05-01T00:00:00Z")
        s2 = _add_entity(bundle, "sample", "SAR", "SS-2", tumor_purity=0.19, collection_timestamp="2026-05-01T00:00:00Z")
        a1 = _add_entity(bundle, "assay", "SAR", "SA-1", patient_id="SP-1", biomarker="SS18_SSX", assay_status="COMPLETED", qc_status="PASS", result="POSITIVE")
        a2 = _add_entity(bundle, "assay", "SAR", "SA-2", patient_id="SP-2", biomarker="SS18_SSX", assay_status="COMPLETED", qc_status="PASS", result="WT_NEGATIVE")
        f1 = _add_entity(bundle, "file", "SAR", "SF-1", file_type="VCF")
        f2 = _add_entity(bundle, "file", "SAR", "SF-2", file_type="VCF")
        _link(bundle, p1, "HAS_CASE", c1a, "SAR", "l1")
        _link(bundle, p1, "HAS_CASE", c1b, "SAR", "l2")
        _link(bundle, p2, "HAS_CASE", c2, "SAR", "l3")
        _link(bundle, c1a, "HAS_SAMPLE", s1, "SAR", "l4")
        _link(bundle, c2, "HAS_SAMPLE", s2, "SAR", "l5")
        _link(bundle, s1, "HAS_ASSAY", a1, "SAR", "l6")
        _link(bundle, s2, "HAS_ASSAY", a2, "SAR", "l7")
        _link(bundle, a1, "HAS_FILE", f1, "SAR", "l8")
        _link(bundle, a2, "HAS_FILE", f2, "SAR", "l9")

        snap = self.root / "sarcoma"
        write_snapshot(bundle, snap)

        # Sample boundary
        smp_ans = self._assert_eval_and_replay(
            snap,
            {"operation": "cohort_count", "entity_level": "sample", "denominator_basis": "all_samples", "predicate": {"min_tumor_purity": 0.2, "max_collection_timestamp": "2026-05-01T00:00:00Z"}},
            "SUPPORTED",
        )
        self.assertEqual(smp_ans["result"]["numerator_count"], 1)
        self.assertEqual(smp_ans["result"]["denominator_count"], 2)
        self.assertEqual(smp_ans["result"]["supporting_evidence_ids"], ["SS-1"])

        # File count
        file_ans = self._assert_eval_and_replay(
            snap,
            {"operation": "cohort_count", "entity_level": "file", "denominator_basis": "vcf_files", "predicate": {"biomarker": "SS18_SSX", "result": "POSITIVE", "file_type": "VCF"}},
            "SUPPORTED",
        )
        self.assertEqual(file_ans["result"]["numerator_count"], 1)
        self.assertEqual(file_ans["result"]["denominator_count"], 2)
        self.assertEqual(file_ans["result"]["supporting_evidence_ids"], ["SF-1"])

        # Deduplicated patient count emits only canonical patient witness IDs
        dedup_ans = self._assert_eval_and_replay(
            snap,
            {"operation": "cohort_count", "entity_level": "patient", "denominator_basis": "distinct_canonical_patients_in_snapshot", "predicate": {"histology": "SYNOVIAL", "biomarker": "SS18_SSX", "result": "POSITIVE"}},
            "SUPPORTED",
        )
        self.assertEqual(dedup_ans["result"]["numerator_count"], 1)
        self.assertEqual(dedup_ans["result"]["denominator_count"], 2)
        self.assertEqual(dedup_ans["result"]["supporting_evidence_ids"], ["SP-1"])

        # Valid 0 / N empty subcohort returns SUPPORTED (0/2), whereas 0/0 prevalence claim returns UNKNOWN
        zero_sub = self._assert_eval_and_replay(
            snap,
            {"operation": "cohort_count", "entity_level": "patient", "denominator_basis": "tested", "predicate": {"biomarker": "SS18_SSX", "result": "RARE_VARIANT"}},
            "SUPPORTED",
        )
        self.assertEqual(zero_sub["result"]["numerator_count"], 0)
        self.assertEqual(zero_sub["result"]["denominator_count"], 2)
        self.assertEqual(zero_sub["result"]["supporting_evidence_ids"], ["SA-1", "SA-2"])

        self._assert_eval_and_replay(
            snap,
            {"operation": "claim_verification", "entity_level": "patient", "denominator_basis": "rare_subgroup", "predicate": {"claimed_prevalence": 0.0, "subcohort_er": "NEGATIVE"}},
            "UNKNOWN",
        )

        # Colliding legacy alias -> AMBIGUOUS
        self._assert_eval_and_replay(
            snap,
            {"operation": "entity_attribute_query", "entity_level": "patient", "subject_ref": {"legacy_subject_alias": "ALIAS-X"}},
            "AMBIGUOUS",
        )
