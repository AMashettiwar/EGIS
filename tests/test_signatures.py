import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import certificate_digest, issue_certificate
from cancer_evidence.fixture import create_synthetic_snapshot
from cancer_evidence.replay import replay_certificate


class SignatureTests(unittest.TestCase):
    def test_recomputed_content_hash_does_not_forge_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["result"]["result"]["count"] = 999
            data["certificate_sha256"] = certificate_digest(data)
            certificate.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = replay_certificate(snapshot, certificate)
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason_code"], "SIGNATURE_INVALID")

    def test_public_key_is_published_and_certificate_verifies(self):
        self.assertTrue(Path("keys/public_key.pem").exists())

    def test_attacker_key_path_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["verification_public_key"] = str(root / "attacker.pem")
            certificate.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = replay_certificate(snapshot, certificate)
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason_code"], "SIGNATURE_INVALID")
