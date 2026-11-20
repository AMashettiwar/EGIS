import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import write_snapshot


ROOT = Path(__file__).resolve().parents[1]
GDC = ROOT / "snapshots" / "real" / "gdc_tcga_luad"
GDC_V2 = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v2"
GDC_V3 = ROOT / "snapshots" / "real" / "gdc_tcga_luad_v3"
PDC = ROOT / "snapshots" / "real" / "pdc_pdc000153"


class RealSnapshotTests(unittest.TestCase):
    def test_gdc_case_count_replays_from_frozen_snapshot(self):
        manifest = json.loads((GDC / "manifest.json").read_text(encoding="utf-8"))
        query = {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}}
        result = evaluate(GDC, query)
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertEqual(result["result"]["count"], manifest["coverage"]["gdc"]["expected_total"])
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "certificate.json"
            issue_certificate(GDC, query, certificate)
            self.assertTrue(replay_certificate(GDC, certificate)["valid"])

    def test_removing_one_page_is_unknown_not_a_smaller_total(self):
        raw = json.loads((GDC / "raw.json").read_text(encoding="utf-8"))
        removed = json.loads(json.dumps(raw))
        removed["entities"] = removed["entities"][200:]
        removed["evidence"] = [row for row in removed["evidence"] if row["entity_id"] in {entity["entity_id"] for entity in removed["entities"]}]
        removed["coverage"]["gdc"]["complete"] = False
        with tempfile.TemporaryDirectory() as directory:
            incomplete = Path(directory) / "incomplete"
            write_snapshot(removed, incomplete, source="gdc", complete=False, request={"replay_fixture": "one-page-removed"})
            result = evaluate(incomplete, {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}})
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason_code"], "INCOMPLETE_SNAPSHOT")
            self.assertIsNone(result.get("result", {}).get("count"))

    def test_pdc_is_separate_namespace_and_has_expected_count(self):
        manifest = json.loads((PDC / "manifest.json").read_text(encoding="utf-8"))
        raw = json.loads((PDC / "raw.json").read_text(encoding="utf-8"))
        study_response = raw["raw_capture"]["study_response"]["data"]["study"]
        study = study_response[0] if isinstance(study_response, list) else study_response
        self.assertEqual(study["pdc_study_id"], "PDC000153")
        self.assertEqual(study["analytical_fraction"], "Proteome")
        self.assertEqual(study["experiment_type"], "TMT10")
        result = evaluate(PDC, {"operation": "count_entities", "selector": {"source": "pdc", "study_id": "f1c59a53-ab7c-11e9-9a07-0a80fada099c"}})
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertEqual(result["result"]["count"], manifest["coverage"]["pdc"]["expected_total"])

    def test_gdc_v2_sample_level_rnaseq_file_availability_matches_raw_and_replays(self):
        import socket
        import tempfile
        from cancer_evidence.certificate import issue_certificate
        from cancer_evidence.replay import replay_certificate

        orig_socket = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("network disabled")

        socket.socket = _blocked
        try:
            manifest = json.loads((GDC_V2 / "manifest.json").read_text(encoding="utf-8"))
            raw = json.loads((GDC_V2 / "raw.json").read_text(encoding="utf-8"))
            primary_samples: set[str] = set()
            for page in raw["raw_capture"]["case_pages"]:
                for case in page["response"]["data"]["hits"]:
                    for sample in case.get("samples", []) or []:
                        if sample.get("sample_type") == "Primary Tumor":
                            primary_samples.add(sample["sample_id"])
            expected_witness: set[str] = set()
            for page in raw["raw_capture"]["file_pages"]:
                for fl in page["response"]["data"]["hits"]:
                    if (
                        fl.get("data_category") == "Transcriptome Profiling"
                        and fl.get("data_type") == "Gene Expression Quantification"
                        and fl.get("experimental_strategy") == "RNA-Seq"
                        and fl.get("data_format") == "TSV"
                        and fl.get("access") == "open"
                        and fl.get("state") == "released"
                    ):
                        for case in fl.get("cases", []) or []:
                            for sample in case.get("samples", []) or []:
                                sid = sample.get("sample_id")
                                if sid in primary_samples:
                                    expected_witness.add(sid)

            query = {
                "operation": "cohort_count",
                "snapshot_id": manifest["snapshot_id"],
                "cohort_id": manifest["cohort_id"],
                "entity_level": "sample",
                "denominator_basis": "tcga_luad_gdc_r46_primary_tumor_samples",
                "predicate": {
                    "source": "gdc",
                    "project_id": "TCGA-LUAD",
                    "sample_type": "Primary Tumor",
                    "data_category": "Transcriptome Profiling",
                    "data_type": "Gene Expression Quantification",
                    "experimental_strategy": "RNA-Seq",
                    "data_format": "TSV",
                    "access": "open",
                    "state": "released",
                },
            }
            result = evaluate(GDC_V2, query)
            self.assertEqual(result["status"], "SUPPORTED")
            self.assertEqual(result["result"]["numerator_count"], len(expected_witness))
            self.assertEqual(result["result"]["denominator_count"], len(primary_samples))
            self.assertEqual(sorted(result["witness"]), sorted(expected_witness))
            self.assertEqual(sorted(result["evidence_refs"]), sorted(expected_witness))
            with tempfile.TemporaryDirectory() as tmp:
                cert_path = Path(tmp) / "gdc_v2_supported.json"
                issue_certificate(GDC_V2, query, cert_path)
                replay = replay_certificate(GDC_V2, cert_path)
                self.assertTrue(replay["valid"])
        finally:
            socket.socket = orig_socket

    def test_gdc_v2_abstains_on_unacquired_assay_qc_or_biomarker_query(self):
        import socket
        import tempfile
        from cancer_evidence.certificate import issue_certificate
        from cancer_evidence.replay import replay_certificate

        orig_socket = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("network disabled")

        socket.socket = _blocked
        try:
            manifest = json.loads((GDC_V2 / "manifest.json").read_text(encoding="utf-8"))
            query = {
                "operation": "cohort_count",
                "snapshot_id": manifest["snapshot_id"],
                "cohort_id": manifest["cohort_id"],
                "entity_level": "sample",
                "denominator_basis": "tcga_luad_gdc_r46_primary_tumor_samples_with_completed_EGFR_assay",
                "predicate": {
                    "source": "gdc",
                    "project_id": "TCGA-LUAD",
                    "sample_type": "Primary Tumor",
                    "biomarker": "EGFR_ACTIVATING",
                    "assay_status": "COMPLETED",
                    "qc_status": "PASS",
                    "positive_results_in": ["L858R_POSITIVE", "Ex19del_POSITIVE"],
                },
            }
            result = evaluate(GDC_V2, query)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason_code"], "INCOMPLETE_SNAPSHOT")
            self.assertEqual(result["witness"], [])
            self.assertEqual(result["evidence_refs"], [])
            with tempfile.TemporaryDirectory() as tmp:
                cert_path = Path(tmp) / "gdc_v2_abstaining.json"
                issue_certificate(GDC_V2, query, cert_path)
                replay = replay_certificate(GDC_V2, cert_path)
                self.assertTrue(replay["valid"])
        finally:
            socket.socket = orig_socket

    def test_gdc_v3_molecular_egfr_activating_variants_match_raw_and_replay(self):
        import socket

        orig_socket = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("network disabled")

        socket.socket = _blocked
        try:
            manifest = json.loads((GDC_V3 / "manifest.json").read_text(encoding="utf-8"))
            raw = json.loads((GDC_V3 / "raw.json").read_text(encoding="utf-8"))

            aliquot_to_sample: dict[str, str] = {}
            for page in raw["raw_capture"]["case_pages"]:
                for case in page["response"]["data"]["hits"]:
                    for smp in case.get("samples", []) or []:
                        for port in smp.get("portions", []) or []:
                            for an in port.get("analytes", []) or []:
                                for alq in an.get("aliquots", []) or []:
                                    aliquot_to_sample[alq["aliquot_id"]] = smp["sample_id"]

            wxs_primary_samples: set[str] = set()
            for page in raw["raw_capture"]["snv_file_pages"]:
                for fl in page["response"]["data"]["hits"]:
                    if (
                        fl.get("data_category") == "Simple Nucleotide Variation"
                        and fl.get("data_type") == "Masked Somatic Mutation"
                        and fl.get("experimental_strategy") == "WXS"
                        and fl.get("data_format") == "MAF"
                        and fl.get("access") == "open"
                        and fl.get("state") == "released"
                    ):
                        for case in fl.get("cases", []) or []:
                            for smp in case.get("samples", []) or []:
                                if smp.get("sample_type") == "Primary Tumor":
                                    wxs_primary_samples.add(smp["sample_id"])

            expected_pos_samples: set[str] = set()
            for page in raw["raw_capture"]["egfr_ssm_pages"]:
                for occ in page["response"]["data"]["hits"]:
                    obs = (occ.get("case") or {}).get("observation") or []
                    t_alqs = {(o.get("sample") or {}).get("tumor_sample_uuid") for o in obs}
                    smps = {aliquot_to_sample[a] for a in t_alqs if a in aliquot_to_sample}
                    for cons in (occ.get("ssm") or {}).get("consequence", []) or []:
                        tr = cons.get("transcript") or {}
                        ann = tr.get("annotation") or {}
                        if (
                            (tr.get("gene") or {}).get("symbol") == "EGFR"
                            and tr.get("is_canonical") is True
                            and tr.get("transcript_id") == "ENST00000275493"
                            and ann.get("hgvsp_short") in {"p.L858R", "p.E746_A750del"}
                        ):
                            expected_pos_samples.update(smps & wxs_primary_samples)

            query = {
                "operation": "cohort_count",
                "snapshot_id": manifest["snapshot_id"],
                "cohort_id": manifest["cohort_id"],
                "entity_level": "sample",
                "denominator_basis": "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples",
                "predicate": {
                    "source": "gdc",
                    "project_id": "TCGA-LUAD",
                    "sample_type": "Primary Tumor",
                    "data_category": "Simple Nucleotide Variation",
                    "data_type": "Masked Somatic Mutation",
                    "experimental_strategy": "WXS",
                    "data_format": "MAF",
                    "access": "open",
                    "state": "released",
                    "gene_symbol": "EGFR",
                    "ncbi_build": "GRCh38",
                    "canonical_transcript_id": "ENST00000275493",
                    "hgvsp_short_in": ["p.L858R", "p.E746_A750del"],
                    "call_status": "POSITIVE",
                },
            }
            result = evaluate(GDC_V3, query)
            self.assertEqual(result["status"], "SUPPORTED")
            self.assertEqual(result["result"]["numerator_count"], len(expected_pos_samples))
            self.assertEqual(result["result"]["denominator_count"], len(wxs_primary_samples))
            self.assertEqual(sorted(result["witness"]), sorted(expected_pos_samples))
            self.assertEqual(len(result["result"]["supporting_variant_record_ids"]), len(expected_pos_samples))
            with tempfile.TemporaryDirectory() as tmp:
                cert_path = Path(tmp) / "gdc_v3_supported.json"
                issue_certificate(GDC_V3, query, cert_path)
                replay = replay_certificate(GDC_V3, cert_path)
                self.assertTrue(replay["valid"])
        finally:
            socket.socket = orig_socket

    def test_gdc_v3_abstains_on_negative_wildtype_without_locus_coverage(self):
        import socket

        orig_socket = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("network disabled")

        socket.socket = _blocked
        try:
            manifest = json.loads((GDC_V3 / "manifest.json").read_text(encoding="utf-8"))
            query = {
                "operation": "cohort_count",
                "snapshot_id": manifest["snapshot_id"],
                "cohort_id": manifest["cohort_id"],
                "entity_level": "sample",
                "denominator_basis": "tcga_luad_gdc_r46_wxs_masked_ssm_primary_tumor_samples",
                "predicate": {
                    "source": "gdc",
                    "project_id": "TCGA-LUAD",
                    "sample_type": "Primary Tumor",
                    "data_category": "Simple Nucleotide Variation",
                    "data_type": "Masked Somatic Mutation",
                    "experimental_strategy": "WXS",
                    "data_format": "MAF",
                    "access": "open",
                    "state": "released",
                    "gene_symbol": "EGFR",
                    "ncbi_build": "GRCh38",
                    "canonical_transcript_id": "ENST00000275493",
                    "hgvsp_short_in": ["p.L858R", "p.E746_A750del"],
                    "call_status": "NEGATIVE",
                    "require_explicit_negative_coverage": True,
                },
            }
            result = evaluate(GDC_V3, query)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason_code"], "INCOMPLETE_SNAPSHOT")
            self.assertEqual(result["witness"], [])
            self.assertEqual(result["evidence_refs"], [])
            with tempfile.TemporaryDirectory() as tmp:
                cert_path = Path(tmp) / "gdc_v3_abstaining.json"
                issue_certificate(GDC_V3, query, cert_path)
                replay = replay_certificate(GDC_V3, cert_path)
                self.assertTrue(replay["valid"])
        finally:
            socket.socket = orig_socket
