import tempfile
import unittest
from pathlib import Path

from cancer_evidence.evaluator import run_hidden
from cancer_evidence.fixture import create_synthetic_snapshot


class EvaluatorRunnerTests(unittest.TestCase):
    def test_runner_marks_network_disabled_and_keeps_expected_external(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            queries = root / "queries"
            output = root / "output"
            queries.mkdir()
            (queries / "q.json").write_text('{"operation":"count_entities","selector":{"source":"gdc"}}', encoding="utf-8")
            create_synthetic_snapshot(snapshot)
            report = run_hidden(snapshot, queries, output)
            self.assertEqual(report["network"], "disabled")
            self.assertFalse(report["expected_answers_consumed"])
            self.assertTrue(report["results"][0]["replay_valid"])

