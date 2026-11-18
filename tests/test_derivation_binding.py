import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import create_synthetic_snapshot
from cancer_evidence.replay import replay_certificate


class DerivationBindingTests(unittest.TestCase):
    def test_normalized_edit_fails_as_derivation_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            normalized = json.loads((snapshot / "normalized.json").read_text(encoding="utf-8"))
            normalized["entities"][0]["attributes"]["project_id"] = "TAMPERED"
            (snapshot / "normalized.json").write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = replay_certificate(snapshot, certificate)
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason_code"], "DERIVATION_MISMATCH")
