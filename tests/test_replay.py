import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import create_synthetic_snapshot
from cancer_evidence.engine import evaluate
from cancer_evidence.snapshot import write_snapshot
from cancer_evidence.replay import replay_certificate


class ReplayTests(unittest.TestCase):
    def test_valid_certificate_replays_and_tamper_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            query = {"operation": "count_entities", "selector": {"source": "gdc"}}
            issue_certificate(snapshot, query, certificate)
            self.assertTrue(replay_certificate(snapshot, certificate)["valid"])
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["result"]["result"]["count"] = 99
            certificate.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(replay_certificate(snapshot, certificate)["valid"])

    def test_replay_rejects_scope_tamper_even_with_recomputed_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["evidence_scope"]["source"] = "pdc"
            unsigned = dict(data)
            unsigned.pop("certificate_sha256")
            from cancer_evidence.snapshot import canonical_json, sha256_bytes
            data["certificate_sha256"] = sha256_bytes(canonical_json(unsigned).encode("utf-8"))
            certificate.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(replay_certificate(snapshot, certificate)["valid"])

    def test_replay_accepts_incomplete_abstention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            from cancer_evidence.fixture import synthetic_bundle
            bundle = synthetic_bundle()
            bundle["coverage"]["gdc"]["complete"] = False
            write_snapshot(bundle, snapshot)
            query = {"operation": "count_entities", "selector": {"source": "gdc"}}
            issue_certificate(snapshot, query, certificate)
            self.assertEqual(evaluate(snapshot, query)["reason_code"], "INCOMPLETE_SNAPSHOT")
            self.assertTrue(replay_certificate(snapshot, certificate)["valid"])

    def test_replay_accepts_conflict_and_invalid_inventory_abstentions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            from cancer_evidence.fixture import synthetic_bundle
            bundle = synthetic_bundle()
            bundle["evidence"].append({"evidence_id": "ev:gdc:assay:A001:conflict2", "entity_id": "gdc:assay:A001", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "A001-conflict2"})
            write_snapshot(bundle, snapshot)
            for index, query in enumerate((
                {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}},
                {"operation": "compare_inventories", "sources": ["gdc", "gdc"]},
            )):
                certificate = root / f"c{index}.json"
                issue_certificate(snapshot, query, certificate)
                self.assertTrue(replay_certificate(snapshot, certificate)["valid"])

    def test_missing_source_coverage_abstains_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            from cancer_evidence.fixture import synthetic_bundle
            bundle = synthetic_bundle()
            del bundle["coverage"]["gdc"]
            write_snapshot(bundle, snapshot)
            query = {"operation": "count_entities", "selector": {"source": "gdc"}}
            issue_certificate(snapshot, query, certificate)
            self.assertEqual(json.loads(certificate.read_text(encoding="utf-8"))["result"]["reason_code"], "INCOMPLETE_SNAPSHOT")
            self.assertTrue(replay_certificate(snapshot, certificate)["valid"])

