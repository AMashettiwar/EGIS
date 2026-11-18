import tempfile
import unittest
from pathlib import Path

from cancer_evidence.engine import evaluate
from cancer_evidence.fixture import create_synthetic_snapshot


class DenominatorUniverseTests(unittest.TestCase):
    def test_eligible_denominator_is_snapshot_derived(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            create_synthetic_snapshot(snapshot)
            query = {
                "operation": "assay_overlap",
                "entity_level": "case",
                "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"},
                "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"},
                "cohort_filter": {"source": "gdc", "project_id": "TCGA-LUAD"},
                "denominator_mode": "eligible",
            }
            result = evaluate(snapshot, query)
            self.assertEqual(result["status"], "SUPPORTED")
            self.assertEqual(result["result"]["numerator"], 1)
            self.assertEqual(result["result"]["denominator"], 2)
            self.assertEqual(result["result"]["eligible_set_size"], 2)
            self.assertEqual(result["result"]["denominator_population"], "snapshot_eligible_cases")

    def test_wrong_cohort_never_becomes_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            create_synthetic_snapshot(snapshot)
            query = {
                "operation": "assay_overlap",
                "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"},
                "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"},
                "cohort_filter": {"source": "gdc", "project_id": "NOT-A-REAL-COHORT"},
                "denominator_mode": "eligible",
            }
            result = evaluate(snapshot, query)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason_code"], "ZERO_DENOMINATOR")
            self.assertEqual(result["result"], {"numerator": 0, "denominator": 0})
