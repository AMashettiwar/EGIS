import copy
import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import certificate_digest, replay_certificate
from cancer_evidence.snapshot import write_snapshot


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
    for field, val in attrs.items():
        bundle["evidence"].append(
            {
                "evidence_id": f"ev:{eid}:{field}",
                "entity_id": eid,
                "field_name": field,
                "raw_value": val,
                "canonical_value": val,
                "source_record_id": source_id,
            }
        )
    return eid


def _link(bundle: dict, sub: str, pred: str, obj: str, rec: str, source: str = "SYN_GDC") -> None:
    bundle["relationships"].append(
        {
            "subject_id": sub,
            "predicate": pred,
            "object_id": obj,
            "source": source,
            "source_record_id": rec,
        }
    )


def _base_molecular_bundle(*, explicit_negatives: bool, locus_cov: bool, acquired_genes: list[str] | None = None) -> dict:
    return {
        "source": "SYN_GDC",
        "cohort_id": "syn_luad_mol_v1",
        "coverage": {"SYN_GDC": {"scope": "unit-test", "complete": True}},
        "entities": [],
        "relationships": [],
        "evidence": [],
        "provenance_metadata": {
            "named_universes": {
                "syn_primary_tumor_wxs_samples": "Eligible Primary Tumor samples with WXS MAF file or molecular call",
            },
            "molecular_coverage": {
                "acquired_gene_symbols": acquired_genes if acquired_genes is not None else ["EGFR"],
                "explicit_negative_calls_present": explicit_negatives,
                "locus_level_callable_coverage_present": locus_cov,
            },
            "tables": {
                "cases": {"declared_completeness": "COMPLETE"},
                "samples": {"declared_completeness": "COMPLETE"},
                "aliquots": {"declared_completeness": "COMPLETE"},
                "assays": {"declared_completeness": "COMPLETE"},
                "files": {"declared_completeness": "COMPLETE"},
            },
            "supported_entity_levels": ["case", "sample", "aliquot", "assay", "file"],
        },
    }


class MolecularEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _eval_and_replay(self, snap: Path, query: dict, expected_status: str, expected_reason: str | None = None) -> dict:
        ans = evaluate(snap, query)
        self.assertEqual(ans["status"], expected_status, ans)
        if expected_reason is not None:
            self.assertEqual(ans["reason_code"], expected_reason, ans)
        cert_path = self.root / f"cert_{len(list(self.root.glob('*.json')))}.json"
        issue_certificate(snap, query, cert_path)
        rep = replay_certificate(snap, cert_path)
        self.assertTrue(rep["valid"], (ans, rep))
        self.assertEqual(rep["status"], expected_status)
        return ans

    def test_duplicate_and_unmatched_molecular_mappings_rejected_as_conflict(self):
        # 1. Multiply matched variant record linked to two different samples -> CONFLICT
        dup_bundle = _base_molecular_bundle(explicit_negatives=False, locus_cov=False)
        c1 = _add_entity(dup_bundle, "case", "SYN_GDC", "C1", project_id="TCGA-LUAD")
        c2 = _add_entity(dup_bundle, "case", "SYN_GDC", "C2", project_id="TCGA-LUAD")
        s1 = _add_entity(dup_bundle, "sample", "SYN_GDC", "S1", sample_type="Primary Tumor")
        s2 = _add_entity(dup_bundle, "sample", "SYN_GDC", "S2", sample_type="Primary Tumor")
        alq1 = _add_entity(dup_bundle, "aliquot", "SYN_GDC", "ALQ1")
        alq2 = _add_entity(dup_bundle, "aliquot", "SYN_GDC", "ALQ2")
        occ_dup = _add_entity(
            dup_bundle,
            "assay",
            "SYN_GDC",
            "OCC-DUP",
            gene_symbol="EGFR",
            hgvsp_short="p.L858R",
            call_status="POSITIVE",
            coverage_completeness="POSITIVE_ONLY_OPEN_ACCESS_MAF",
        )
        _link(dup_bundle, c1, "HAS_SAMPLE", s1, "C1-S1")
        _link(dup_bundle, c2, "HAS_SAMPLE", s2, "C2-S2")
        _link(dup_bundle, s1, "HAS_ALIQUOT", alq1, "S1-ALQ1")
        _link(dup_bundle, s2, "HAS_ALIQUOT", alq2, "S2-ALQ2")
        _link(dup_bundle, alq1, "HAS_ASSAY", occ_dup, "ALQ1-OCC")
        _link(dup_bundle, alq2, "HAS_ASSAY", occ_dup, "ALQ2-OCC")

        snap_dup = self.root / "dup_map"
        write_snapshot(dup_bundle, snap_dup)
        q = {
            "operation": "cohort_count",
            "entity_level": "sample",
            "denominator_basis": "syn_primary_tumor_wxs_samples",
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "call_status": "POSITIVE"},
        }
        ans_dup = self._eval_and_replay(snap_dup, q, "CONFLICT", "CONFLICTING_EVIDENCE")
        self.assertIn("OCC-DUP", ans_dup["witness"])

        # 2. Unmatched molecular variant record (no sample or aliquot link) -> CONFLICT
        unm_bundle = _base_molecular_bundle(explicit_negatives=False, locus_cov=False)
        c3 = _add_entity(unm_bundle, "case", "SYN_GDC", "C3", project_id="TCGA-LUAD")
        s3 = _add_entity(unm_bundle, "sample", "SYN_GDC", "S3", sample_type="Primary Tumor")
        alq3 = _add_entity(unm_bundle, "aliquot", "SYN_GDC", "ALQ3")
        f3 = _add_entity(unm_bundle, "file", "SYN_GDC", "MAF3", data_type="Masked Somatic Mutation")
        _add_entity(
            unm_bundle,
            "assay",
            "SYN_GDC",
            "OCC-UNMATCHED",
            gene_symbol="EGFR",
            hgvsp_short="p.L858R",
            call_status="POSITIVE",
            linkage_status="UNMATCHED",
        )
        _link(unm_bundle, c3, "HAS_SAMPLE", s3, "C3-S3")
        _link(unm_bundle, s3, "HAS_ALIQUOT", alq3, "S3-ALQ3")
        _link(unm_bundle, alq3, "HAS_FILE", f3, "ALQ3-MAF3")
        snap_unm = self.root / "unm_map"
        write_snapshot(unm_bundle, snap_unm)
        ans_unm = self._eval_and_replay(snap_unm, q, "CONFLICT", "CONFLICTING_EVIDENCE")
        self.assertEqual(ans_unm["witness"], ["OCC-UNMATCHED"])

    def test_missing_negative_coverage_and_unacquired_gene_abstain_as_unknown(self):
        # Positive-only MAF snapshot: S1 has p.L858R positive; S2 has WXS MAF file only (no EGFR variant listed)
        bundle = _base_molecular_bundle(explicit_negatives=False, locus_cov=False, acquired_genes=["EGFR"])
        for cid, sid, aid, fid in (("C1", "S1", "ALQ1", "MAF1"), ("C2", "S2", "ALQ2", "MAF2")):
            c_e = _add_entity(bundle, "case", "SYN_GDC", cid, project_id="TCGA-LUAD")
            s_e = _add_entity(bundle, "sample", "SYN_GDC", sid, sample_type="Primary Tumor")
            a_e = _add_entity(bundle, "aliquot", "SYN_GDC", aid)
            f_e = _add_entity(bundle, "file", "SYN_GDC", fid, data_type="Masked Somatic Mutation", experimental_strategy="WXS", data_format="MAF")
            _link(bundle, c_e, "HAS_SAMPLE", s_e, f"{cid}-{sid}")
            _link(bundle, s_e, "HAS_ALIQUOT", a_e, f"{sid}-{aid}")
            _link(bundle, a_e, "HAS_FILE", f_e, f"{aid}-{fid}")

        occ1 = _add_entity(
            bundle,
            "assay",
            "SYN_GDC",
            "OCC-1",
            gene_symbol="EGFR",
            ncbi_build="GRCh38",
            canonical_transcript_id="ENST00000275493",
            hgvsp_short="p.L858R",
            call_status="POSITIVE",
            coverage_completeness="POSITIVE_ONLY_OPEN_ACCESS_MAF",
            linkage_status="UNIQUE_MATCH",
        )
        _link(bundle, "SYN_GDC:aliquot:ALQ1", "HAS_ASSAY", occ1, "ALQ1-OCC1")
        _link(bundle, occ1, "HAS_FILE", "SYN_GDC:file:MAF1", "OCC1-MAF1")

        snap = self.root / "pos_only"
        write_snapshot(bundle, snap)

        # Positive query succeeds: 1/2
        q_pos = {
            "operation": "cohort_count",
            "entity_level": "sample",
            "denominator_basis": "syn_primary_tumor_wxs_samples",
            "predicate": {
                "source": "SYN_GDC",
                "data_type": "Masked Somatic Mutation",
                "experimental_strategy": "WXS",
                "data_format": "MAF",
                "gene_symbol": "EGFR",
                "hgvsp_short": "p.L858R",
                "call_status": "POSITIVE",
            },
        }
        ans_pos = self._eval_and_replay(snap, q_pos, "SUPPORTED", "NONE")
        self.assertEqual(ans_pos["result"]["numerator_count"], 1)
        self.assertEqual(ans_pos["result"]["denominator_count"], 2)
        self.assertEqual(ans_pos["witness"], ["S1"])

        # Asking for NEGATIVE / wild-type count MUST abstain as UNKNOWN (never fabricate 1/2 wild-type)
        q_neg = copy.deepcopy(q_pos)
        q_neg["predicate"]["call_status"] = "NEGATIVE"
        self._eval_and_replay(snap, q_neg, "UNKNOWN", "INCOMPLETE_SNAPSHOT")

        # Requiring explicit negative coverage also abstains
        q_req = copy.deepcopy(q_pos)
        q_req["predicate"]["require_explicit_negative_coverage"] = True
        self._eval_and_replay(snap, q_req, "UNKNOWN", "INCOMPLETE_SNAPSHOT")

        # Single-sample wild-type claim on S2 (unlisted variant) abstains as UNKNOWN
        q_claim_wt = {
            "operation": "claim_verification",
            "subject_ref": {"sample_id": "S2"},
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "expected_call": "NEGATIVE"},
        }
        self._eval_and_replay(snap, q_claim_wt, "UNKNOWN", "INCOMPLETE_SNAPSHOT")

        # Querying unacquired gene KRAS abstains as UNKNOWN
        q_kras = copy.deepcopy(q_pos)
        q_kras["predicate"]["gene_symbol"] = "KRAS"
        q_kras["predicate"]["hgvsp_short"] = "p.G12C"
        self._eval_and_replay(snap, q_kras, "UNKNOWN", "INCOMPLETE_SNAPSHOT")

        # Explicit MISSING_COVERAGE on a sample abstains on cohort_count even for POSITIVE queries
        cov_bundle = copy.deepcopy(bundle)
        occ_miss = _add_entity(
            cov_bundle,
            "assay",
            "SYN_GDC",
            "OCC-MISS",
            gene_symbol="EGFR",
            hgvsp_short="p.L858R",
            call_status="MISSING_COVERAGE",
            coverage_completeness="UNSEQUENCED",
            linkage_status="UNIQUE_MATCH",
        )
        _link(cov_bundle, "SYN_GDC:aliquot:ALQ2", "HAS_ASSAY", occ_miss, "ALQ2-OCCMISS")
        snap_miss = self.root / "miss_cov"
        write_snapshot(cov_bundle, snap_miss)
        self._eval_and_replay(snap_miss, q_pos, "UNKNOWN", "INCOMPLETE_SNAPSHOT")

    def test_explicit_negative_calls_with_complete_locus_coverage_supported_and_claims_verified(self):
        bundle = _base_molecular_bundle(explicit_negatives=True, locus_cov=True, acquired_genes=["EGFR"])
        for cid, sid, aid, fid in (
            ("C1", "S1", "ALQ1", "F1"),
            ("C2", "S2", "ALQ2", "F2"),
            ("C3", "S3", "ALQ3", "F3"),
        ):
            c_e = _add_entity(bundle, "case", "SYN_GDC", cid, project_id="TCGA-LUAD")
            s_e = _add_entity(bundle, "sample", "SYN_GDC", sid, sample_type="Primary Tumor")
            a_e = _add_entity(bundle, "aliquot", "SYN_GDC", aid)
            f_e = _add_entity(bundle, "file", "SYN_GDC", fid, data_type="Callable Locus Coverage", data_format="MAF")
            _link(bundle, c_e, "HAS_SAMPLE", s_e, f"{cid}-{sid}")
            _link(bundle, s_e, "HAS_ALIQUOT", a_e, f"{sid}-{aid}")
            _link(bundle, a_e, "HAS_FILE", f_e, f"{aid}-{fid}")

        occ1 = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-1",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="POSITIVE",
            coverage_completeness="COMPLETE_LOCUS_COVERAGE", linkage_status="UNIQUE_MATCH",
        )
        occ2 = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-2",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="NEGATIVE",
            coverage_completeness="COMPLETE_LOCUS_COVERAGE", linkage_status="UNIQUE_MATCH",
        )
        occ3 = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-3",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="NEGATIVE",
            coverage_completeness="COMPLETE_LOCUS_COVERAGE", linkage_status="UNIQUE_MATCH",
        )
        _link(bundle, "SYN_GDC:aliquot:ALQ1", "HAS_ASSAY", occ1, "ALQ1-OCC1")
        _link(bundle, occ1, "HAS_FILE", "SYN_GDC:file:F1", "OCC1-F1")
        _link(bundle, "SYN_GDC:aliquot:ALQ2", "HAS_ASSAY", occ2, "ALQ2-OCC2")
        _link(bundle, occ2, "HAS_FILE", "SYN_GDC:file:F2", "OCC2-F2")
        _link(bundle, "SYN_GDC:aliquot:ALQ3", "HAS_ASSAY", occ3, "ALQ3-OCC3")
        _link(bundle, occ3, "HAS_FILE", "SYN_GDC:file:F3", "OCC3-F3")

        snap = self.root / "complete_neg"
        write_snapshot(bundle, snap)

        q_neg = {
            "operation": "cohort_count",
            "entity_level": "sample",
            "denominator_basis": "syn_primary_tumor_wxs_samples",
            "predicate": {
                "source": "SYN_GDC",
                "gene_symbol": "EGFR",
                "hgvsp_short": "p.L858R",
                "call_status": "NEGATIVE",
                "require_explicit_negative_coverage": True,
            },
        }
        ans_neg = self._eval_and_replay(snap, q_neg, "SUPPORTED", "NONE")
        self.assertEqual(ans_neg["result"]["numerator_count"], 2)
        self.assertEqual(ans_neg["result"]["denominator_count"], 3)
        self.assertEqual(ans_neg["witness"], ["S2", "S3"])
        self.assertEqual(ans_neg["result"]["supporting_variant_record_ids"], ["OCC-2", "OCC-3"])

        # Claim verification against explicit NEGATIVE call: S2 NEGATIVE -> SUPPORTED; S2 POSITIVE -> CONTRADICTED
        q_claim_neg_true = {
            "operation": "claim_verification",
            "subject_ref": {"sample_id": "S2"},
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "expected_call": "NEGATIVE"},
        }
        self._eval_and_replay(snap, q_claim_neg_true, "SUPPORTED", "NONE")

        q_claim_neg_false = {
            "operation": "claim_verification",
            "subject_ref": {"sample_id": "S2"},
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "expected_call": "POSITIVE"},
        }
        ans_contra = self._eval_and_replay(snap, q_claim_neg_false, "CONTRADICTED", "CLAIM_CONTRADICTED")
        self.assertFalse(ans_contra["result"]["claim_holds"])

    def test_conflicting_positive_and_negative_molecular_calls_on_same_sample(self):
        bundle = _base_molecular_bundle(explicit_negatives=True, locus_cov=True, acquired_genes=["EGFR"])
        c1 = _add_entity(bundle, "case", "SYN_GDC", "C1", project_id="TCGA-LUAD")
        s1 = _add_entity(bundle, "sample", "SYN_GDC", "S1", sample_type="Primary Tumor")
        a1 = _add_entity(bundle, "aliquot", "SYN_GDC", "ALQ1")
        _link(bundle, c1, "HAS_SAMPLE", s1, "C1-S1")
        _link(bundle, s1, "HAS_ALIQUOT", a1, "S1-ALQ1")

        occ_p = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-POS",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="POSITIVE",
            coverage_completeness="COMPLETE_LOCUS_COVERAGE", linkage_status="UNIQUE_MATCH",
        )
        occ_n = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-NEG",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="NEGATIVE",
            coverage_completeness="COMPLETE_LOCUS_COVERAGE", linkage_status="UNIQUE_MATCH",
        )
        _link(bundle, a1, "HAS_ASSAY", occ_p, "ALQ1-POS")
        _link(bundle, a1, "HAS_ASSAY", occ_n, "ALQ1-NEG")

        snap = self.root / "conflict_calls"
        write_snapshot(bundle, snap)

        q_cohort = {
            "operation": "cohort_count",
            "entity_level": "sample",
            "denominator_basis": "syn_primary_tumor_wxs_samples",
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "call_status": "POSITIVE"},
        }
        ans_c = self._eval_and_replay(snap, q_cohort, "CONFLICT", "CONFLICTING_EVIDENCE")
        self.assertEqual(ans_c["witness"], ["S1"])

        q_claim = {
            "operation": "claim_verification",
            "subject_ref": {"sample_id": "S1"},
            "predicate": {"source": "SYN_GDC", "gene_symbol": "EGFR", "hgvsp_short": "p.L858R", "expected_call": "POSITIVE"},
        }
        self._eval_and_replay(snap, q_claim, "CONFLICT", "CONFLICTING_EVIDENCE")

    def test_molecular_certificate_tampering_detected_by_independent_replay(self):
        bundle = _base_molecular_bundle(explicit_negatives=False, locus_cov=False, acquired_genes=["EGFR"])
        c1 = _add_entity(bundle, "case", "SYN_GDC", "C1", project_id="TCGA-LUAD")
        s1 = _add_entity(bundle, "sample", "SYN_GDC", "S1", sample_type="Primary Tumor")
        a1 = _add_entity(bundle, "aliquot", "SYN_GDC", "ALQ1")
        f1 = _add_entity(bundle, "file", "SYN_GDC", "MAF1", data_type="Masked Somatic Mutation", experimental_strategy="WXS", data_format="MAF")
        occ1 = _add_entity(
            bundle, "assay", "SYN_GDC", "OCC-1",
            gene_symbol="EGFR", hgvsp_short="p.L858R", call_status="POSITIVE",
            coverage_completeness="POSITIVE_ONLY_OPEN_ACCESS_MAF", linkage_status="UNIQUE_MATCH",
        )
        _link(bundle, c1, "HAS_SAMPLE", s1, "C1-S1")
        _link(bundle, s1, "HAS_ALIQUOT", a1, "S1-ALQ1")
        _link(bundle, a1, "HAS_FILE", f1, "ALQ1-MAF1")
        _link(bundle, a1, "HAS_ASSAY", occ1, "ALQ1-OCC1")
        _link(bundle, occ1, "HAS_FILE", f1, "OCC1-MAF1")

        snap = self.root / "tamper_mol"
        write_snapshot(bundle, snap)
        query = {
            "operation": "cohort_count",
            "entity_level": "sample",
            "denominator_basis": "syn_primary_tumor_wxs_samples",
            "predicate": {
                "source": "SYN_GDC",
                "data_type": "Masked Somatic Mutation",
                "experimental_strategy": "WXS",
                "data_format": "MAF",
                "gene_symbol": "EGFR",
                "hgvsp_short": "p.L858R",
                "call_status": "POSITIVE",
            },
        }
        cert_path = self.root / "mol_cert.json"
        issue_certificate(snap, query, cert_path)
        valid_cert = json.loads(cert_path.read_text(encoding="utf-8"))

        # 1. Tamper numerator count -> rejected
        t1 = copy.deepcopy(valid_cert)
        t1["result"]["result"]["numerator_count"] = 99
        t1_path = self.root / "t1.json"
        t1_path.write_text(json.dumps(t1), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, t1_path)["valid"])

        # 2. Tamper witness sample ID -> rejected
        t2 = copy.deepcopy(valid_cert)
        t2["result"]["witness"] = ["S-FORGED"]
        t2_path = self.root / "t2.json"
        t2_path.write_text(json.dumps(t2), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, t2_path)["valid"])

        # 3. Tamper supporting_variant_record_ids -> rejected
        t3 = copy.deepcopy(valid_cert)
        t3["result"]["result"]["supporting_variant_record_ids"] = ["OCC-FORGED"]
        t3_path = self.root / "t3.json"
        t3_path.write_text(json.dumps(t3), encoding="utf-8")
        self.assertFalse(replay_certificate(snap, t3_path)["valid"])

        # 4. Tamper query predicate and recompute digest without private key -> SIGNATURE_INVALID
        t4 = copy.deepcopy(valid_cert)
        t4["query"]["predicate"]["hgvsp_short"] = "p.T790M"
        t4["certificate_sha256"] = certificate_digest(t4)
        t4_path = self.root / "t4.json"
        t4_path.write_text(json.dumps(t4), encoding="utf-8")
        rep4 = replay_certificate(snap, t4_path)
        self.assertFalse(rep4["valid"])
        self.assertEqual(rep4["reason_code"], "SIGNATURE_INVALID")
