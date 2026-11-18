import copy
import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import certificate_digest, issue_certificate
from cancer_evidence.engine import evaluate
from cancer_evidence.replay import replay_certificate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "examples" / "synthetic_fixture.json"
QUERY_PATH = ROOT / "examples" / "synthetic_overlap.json"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class SyntheticEngineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = read_json(FIXTURE_PATH)
        self.query = read_json(QUERY_PATH)

    def test_fixture_is_explicitly_synthetic(self):
        metadata = self.fixture["fixture_metadata"]
        self.assertTrue(metadata["synthetic_only"])
        self.assertIn("NOT TCGA", metadata["label"])
        self.assertIn("NOT CPTAC", metadata["label"])

    def test_case_overlap_returns_supported_counts_filters_and_evidence(self):
        result = evaluate(self.fixture, self.query)

        self.assertEqual(result["status"], "SUPPORTED")
        self.assertEqual(result["numerator"], 1)
        self.assertEqual(result["denominator"], 2)
        self.assertEqual(result["filters"]["entity_level"], "case")
        self.assertEqual(result["overlap_case_ids"], ["CASE-001"])
        self.assertTrue(result["evidence_references"])
        self.assertIn("SRC-ASSAY-L-002", result["evidence_references"])

    def test_duplicate_files_do_not_inflate_case_counts(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["files"].append(
            {
                "record_type": "file",
                "file_id": "FILE-L-003-DUP",
                "assay_record_id": "ASSAY-L-003",
                "source_record_id": "SRC-FILE-L-003-DUP",
            }
        )
        fixture["source_records"].append(
            {
                "record_type": "source-record",
                "source_record_id": "SRC-FILE-L-003-DUP",
                "entity_id": "FILE-L-003-DUP",
                "field": "assay_record_id",
                "value": "ASSAY-L-003",
            }
        )

        result = evaluate(fixture, self.query)

        self.assertEqual((result["numerator"], result["denominator"]), (1, 2))

    def test_case_level_join_does_not_confuse_sample_ids(self):
        result = evaluate(self.fixture, self.query)
        left_samples = {"SAMPLE-001-A", "SAMPLE-002-A"}
        right_samples = {"SAMPLE-001-B", "SAMPLE-003-A"}

        self.assertEqual(left_samples & right_samples, set())
        self.assertEqual(result["overlap_case_ids"], ["CASE-001"])
        self.assertEqual(
            evaluate({**self.fixture}, {**self.query, "entity_level": "sample"})["status"],
            "AMBIGUOUS",
        )

    def test_incomplete_fixture_is_unknown_and_never_zero(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["fixture_metadata"]["complete"] = False

        result = evaluate(fixture, self.query)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["numerator"])
        self.assertIsNone(result["denominator"])

    def test_missing_evidence_is_unknown_and_never_zero(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["source_records"] = [
            record
            for record in fixture["source_records"]
            if record["source_record_id"] != "SRC-SAMPLE-002-A"
        ]

        result = evaluate(fixture, self.query)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["numerator"])
        self.assertIsNone(result["denominator"])

    def test_conflicting_evidence_abstains(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["source_records"].append(
            {
                "record_type": "source-record",
                "source_record_id": "SRC-ASSAY-L-003-CONFLICT",
                "entity_id": "ASSAY-L-003",
                "field": "sample_id",
                "value": "SAMPLE-001-A",
            }
        )

        result = evaluate(fixture, self.query)

        self.assertEqual(result["status"], "CONFLICT")
        self.assertIsNone(result["numerator"])
        self.assertIsNone(result["denominator"])

    def test_certificate_rejects_stale_fixture_hash(self):
        certificate = issue_certificate(self.fixture, self.query)
        stale_fixture = copy.deepcopy(self.fixture)
        stale_fixture["cases"][0]["display_name"] = "changed"

        replay = replay_certificate(stale_fixture, certificate)

        self.assertFalse(replay["valid"])
        self.assertEqual(replay["reason_code"], "FIXTURE_HASH_MISMATCH")

    def test_certificate_rejects_altered_result(self):
        certificate = issue_certificate(self.fixture, self.query)
        certificate["result"]["numerator"] = 99

        replay = replay_certificate(self.fixture, certificate)

        self.assertFalse(replay["valid"])
        self.assertEqual(replay["reason_code"], "CERTIFICATE_DIGEST_MISMATCH")

    def test_replay_rejects_arithmetically_correct_wrong_cohort(self):
        certificate = issue_certificate(self.fixture, self.query)
        certificate["result"]["overlap_case_ids"] = ["CASE-003"]
        certificate["result"]["evidence_references"] = ["SRC-CASE-003"]
        certificate["certificate_sha256"] = certificate_digest(certificate)

        replay = replay_certificate(self.fixture, certificate)

        self.assertFalse(replay["valid"])
        self.assertEqual(replay["reason_code"], "RESULT_MISMATCH")

    def test_statuses_for_ambiguous_out_of_scope_and_error(self):
        self.assertEqual(
            evaluate(self.fixture, {"operation": "case_overlap"})["status"],
            "AMBIGUOUS",
        )
        self.assertEqual(
            evaluate(self.fixture, {"operation": "not_supported"})["status"],
            "OUT_OF_SCOPE",
        )
        self.assertEqual(
            evaluate(self.fixture, {"operation": "case_overlap", "left": []})["status"],
            "ERROR",
        )


if __name__ == "__main__":
    unittest.main()
