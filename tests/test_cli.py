import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.certificate import issue_certificate
from cancer_evidence.fixture import create_synthetic_snapshot, synthetic_bundle


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

    def test_snapshot_import_creates_verifiable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = synthetic_bundle()
            bundle_path = root / "bundle.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            snapshot = root / "imported-snapshot"
            certificate = root / "certificate.json"
            imp = subprocess.run(
                [sys.executable, "-m", "cancer_evidence", "snapshot", "import", "--bundle", str(bundle_path), "--out", str(snapshot)],
                check=True,
                capture_output=True,
                text=True,
                env=self._env(),
            )
            manifest = json.loads(imp.stdout)
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["publication"], "READY")
            issue_certificate(snapshot, {"operation": "count_entities", "selector": {"source": "gdc"}}, certificate)
            ver = subprocess.run(
                [sys.executable, "-m", "cancer_evidence", "verify", "--snapshot", str(snapshot), "--certificate", str(certificate)],
                check=True,
                capture_output=True,
                text=True,
                env=self._env(),
            )
            self.assertTrue(json.loads(ver.stdout)["valid"])

