import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import synthetic_bundle
from cancer_evidence.replay import replay_certificate
from cancer_evidence.snapshot import write_snapshot


class ReplayMatrixTests(unittest.TestCase):
    def _snapshot(self, root: Path, *, conflict: bool = False, incomplete: bool = False) -> Path:
        bundle = synthetic_bundle()
        if conflict:
            bundle["evidence"].append({"evidence_id": "ev:conflict", "entity_id": "gdc:assay:A001", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "conflict"})
        if incomplete:
            bundle["coverage"]["gdc"]["complete"] = False
        path = root / f"snapshot-{len(list(root.iterdir()))}"
        write_snapshot(bundle, path)
        return path

    def _round_trip(self, snapshot: Path, query: dict) -> dict:
        certificate = snapshot.parent / f"{snapshot.name}.certificate.json"
        issue_certificate(snapshot, query, certificate)
        return replay_certificate(snapshot, certificate)

    def test_all_six_statuses_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supported = {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}}
            unknown = {"operation": "count_entities", "selector": {"source": "gdc"}}
            ambiguous = {"operation": "assay_availability", "selector": {"assay_type": "rna_expression"}}
            out_of_scope = {"operation": "not_supported"}
            error = {"operation": "assay_availability", "selector": []}
            conflict = {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}}
            cases = [
                (supported, "SUPPORTED", False, False),
                (unknown, "UNKNOWN", False, True),
                (ambiguous, "AMBIGUOUS", False, False),
                (out_of_scope, "OUT_OF_SCOPE", False, False),
                (error, "ERROR", False, False),
                (conflict, "CONFLICT", True, False),
            ]
            for index, (query, status, has_conflict, incomplete) in enumerate(cases):
                snapshot = self._snapshot(root, conflict=has_conflict, incomplete=incomplete)
                certificate = root / f"matrix-{index}.json"
                issue_certificate(snapshot, query, certificate)
                result = replay_certificate(snapshot, certificate)
                self.assertTrue(result["valid"], (status, result))
                self.assertEqual(result["status"], status)

    def test_fixture_and_cohort_mismatch_round_trip_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root)
            for field in ("fixture_id", "cohort_id"):
                query = {"operation": "count_entities", "selector": {"source": "gdc"}, field: "wrong"}
                result = self._round_trip(snapshot, query)
                self.assertTrue(result["valid"], (field, result))
                self.assertEqual(result["status"], "UNKNOWN")

    def test_malformed_certificate_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root)
            certificate = root / "malformed.json"
            certificate.write_text("not json", encoding="utf-8")
            result = replay_certificate(snapshot, certificate)
            self.assertEqual(result, {"valid": False, "reason_code": "MALFORMED_CERTIFICATE"})

    def test_null_signature_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root)
            certificate = root / "null-signature.json"
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["signature"] = None
            certificate.write_text(json.dumps(data), encoding="utf-8")
            result = replay_certificate(snapshot, certificate)
            self.assertEqual(result["reason_code"], "SIGNATURE_INVALID")

    def test_broad_availability_query_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root)
            query = {"operation": "assay_availability", "selector": {"source": "gdc"}}
            result = self._round_trip(snapshot, query)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["status"], "SUPPORTED")

    def test_non_object_query_round_trips_as_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root)
            certificate = root / "non-object.json"
            issue_certificate(snapshot, ["not", "an", "object"], certificate)
            result = replay_certificate(snapshot, certificate)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["status"], "ERROR")
