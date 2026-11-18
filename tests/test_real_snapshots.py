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
