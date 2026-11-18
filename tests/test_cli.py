import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import create_synthetic_snapshot


class CliTests(unittest.TestCase):
    def _env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path("src").resolve())
        return env

    def test_demo_is_offline_and_emits_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "cancer_evidence", "demo", "--out", directory], capture_output=True, text=True, check=True, env=self._env())
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "SUPPORTED")
            self.assertTrue(payload["replay"]["valid"])

    def test_export_contains_relationships_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            export = root / "normalized.json"
            create_synthetic_snapshot(snapshot)
            subprocess.run([sys.executable, "-m", "cancer_evidence", "export", "--snapshot", str(snapshot), "--out", str(export)], check=True, capture_output=True, text=True, env=self._env())
            payload = json.loads(export.read_text(encoding="utf-8"))
            self.assertTrue(payload["relationships"])
            self.assertTrue(payload["evidence"])

    def test_verify_returns_failure_exit_code_for_tampered_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            certificate = root / "certificate.json"
            create_synthetic_snapshot(snapshot)
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            data = json.loads(certificate.read_text(encoding="utf-8"))
            data["result"]["result"]["count"] = 99
            certificate.write_text(json.dumps(data), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "cancer_evidence", "verify", "--snapshot", str(snapshot), "--certificate", str(certificate)], capture_output=True, text=True, env=self._env())
            self.assertNotEqual(result.returncode, 0)

