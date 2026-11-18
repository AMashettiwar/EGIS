import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.fixture import create_synthetic_snapshot, synthetic_bundle
from cancer_evidence.replay import replay_certificate


class SyntheticEngineTests(unittest.TestCase):
    def test_demo_overlap_is_case_level_and_file_membership_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            create_synthetic_snapshot(snapshot)
            query = {
                "operation": "assay_overlap",
                "entity_level": "case",
                "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"},
                "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"},
                "denominator_mode": "left",
            }
            result = evaluate(snapshot, query)
            self.assertEqual(result["status"], "SUPPORTED")
            self.assertEqual(result["result"]["numerator"], 1)
            self.assertEqual(result["result"]["denominator"], 1)
            self.assertEqual(result["witness"], ["gdc:case:C001"])
            self.assertTrue(result["evidence_refs"])

            bundle = synthetic_bundle()
            bundle["relationships"] = [
                relation
                for relation in bundle["relationships"]
                if not (relation["subject_id"] == "gdc:assay:A001" and relation["predicate"] == "HAS_FILE")
            ]
            missing_file_snapshot = Path(directory) / "missing-file"
            from cancer_evidence.snapshot import write_snapshot
            write_snapshot(bundle, missing_file_snapshot)
            changed = evaluate(missing_file_snapshot, query)
            self.assertEqual(changed["status"], "UNKNOWN")
            self.assertEqual(changed["reason_code"], "ZERO_DENOMINATOR")

    def test_synthetic_certificate_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            query = {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}}
            issue_certificate(snapshot, query, certificate)
            replay = replay_certificate(snapshot, certificate)
            self.assertEqual(replay, {"valid": True, "status": "SUPPORTED", "reason_code": "NONE"})
