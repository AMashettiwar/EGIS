import tempfile
import unittest
from pathlib import Path

from cancer_evidence.engine import evaluate
from cancer_evidence.fixture import create_synthetic_snapshot
from cancer_evidence.fixture import synthetic_bundle
from cancer_evidence.snapshot import write_snapshot


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temp.name) / "snapshot"
        create_synthetic_snapshot(self.snapshot)

    def tearDown(self):
        self.temp.cleanup()

    def test_overlap_has_explicit_denominator(self):
        result = evaluate(self.snapshot, {"operation": "assay_overlap", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}})
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertEqual(result["result"]["numerator"], 1)
        self.assertEqual(result["result"]["denominator"], 1)

    def test_cross_source_join_is_forbidden(self):
        result = evaluate(self.snapshot, {"operation": "assay_overlap", "left": {"source": "gdc", "assay_type": "rna_expression"}, "right": {"source": "pdc", "assay_type": "protein_abundance"}})
        self.assertEqual(result["reason_code"], "CROSS_SOURCE_JOIN_FORBIDDEN")

    def test_unsupported_claim_is_not_a_zero(self):
        result = evaluate(self.snapshot, {"operation": "not_a_real_operation"})
        self.assertEqual(result["status"], "OUT_OF_SCOPE")

    def test_overlap_without_source_is_ambiguous(self):
        result = evaluate(self.snapshot, {"operation": "assay_overlap", "left": {"assay_type": "rna_expression"}, "right": {"assay_type": "protein_abundance"}})
        self.assertEqual(result["reason_code"], "AMBIGUOUS_SELECTOR")

    def test_zero_denominator_is_not_supported_zero(self):
        result = evaluate(self.snapshot, {"operation": "assay_overlap", "left": {"source": "gdc", "assay_type": "missing"}, "right": {"source": "gdc", "assay_type": "protein_abundance"}})
        self.assertEqual(result["reason_code"], "ZERO_DENOMINATOR")

    def test_file_lineage_contains_known_ancestors(self):
        result = evaluate(self.snapshot, {"operation": "file_lineage", "entity_id": "gdc:file:F001"})
        self.assertIn("gdc:assay:A001", result["result"]["lineage"])
        self.assertIn("gdc:case:C001", result["result"]["lineage"])
        self.assertNotIn("gdc:file:F002", result["result"]["lineage"])

    def test_count_respects_top_level_entity_level(self):
        result = evaluate(self.snapshot, {"operation": "count_entities", "entity_level": "file", "selector": {"source": "gdc"}})
        self.assertEqual(result["result"]["entity_level"], "file")
        self.assertEqual(result["result"]["count"], 3)

    def test_assay_availability_without_source_is_ambiguous(self):
        result = evaluate(self.snapshot, {"operation": "assay_availability", "selector": {"assay_type": "rna_expression"}})
        self.assertEqual(result["reason_code"], "AMBIGUOUS_SELECTOR")

    def test_conflicting_evidence_abstains(self):
        bundle = synthetic_bundle()
        bundle["evidence"].append({"evidence_id": "ev:gdc:assay:A001:conflict", "entity_id": "gdc:assay:A001", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "A001-conflict"})
        conflicted = Path(self.temp.name) / "conflicted"
        write_snapshot(bundle, conflicted)
        result = evaluate(conflicted, {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}})
        self.assertEqual(result["reason_code"], "CONFLICTING_EVIDENCE")

    def test_sample_to_assay_hierarchy_counts_availability(self):
        bundle = synthetic_bundle()
        bundle["relationships"] = [edge for edge in bundle["relationships"] if edge["source_record_id"] != "C001-A001"]
        bundle["relationships"].append({"subject_id": "gdc:sample:S001", "predicate": "HAS_ASSAY", "object_id": "gdc:assay:A001", "source": "gdc", "source_record_id": "S001-A001"})
        nested = Path(self.temp.name) / "nested"
        write_snapshot(bundle, nested)
        result = evaluate(nested, {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}})
        self.assertEqual(result["result"]["count"], 1)
        self.assertIn("ev:gdc:assay:A001:assay_type", result["evidence_refs"])
        self.assertIn("rel:S001-A001", result["evidence_refs"])

    def test_existing_file_without_linkage_abstains(self):
        bundle = synthetic_bundle()
        bundle["relationships"] = [edge for edge in bundle["relationships"] if edge["object_id"] != "gdc:file:F001"]
        isolated = Path(self.temp.name) / "missing-lineage"
        write_snapshot(bundle, isolated)
        result = evaluate(isolated, {"operation": "file_lineage", "entity_id": "gdc:file:F001"})
        self.assertEqual(result["reason_code"], "MISSING_LINKAGE")

    def test_eligibility_conflict_is_detected_before_filtering(self):
        bundle = synthetic_bundle()
        for entity in bundle["entities"]:
            if entity["entity_id"] == "gdc:assay:A001":
                entity["attributes"]["eligible"] = False
        bundle["evidence"].append({"evidence_id": "ev:gdc:assay:A001:false", "entity_id": "gdc:assay:A001", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "A001-false"})
        conflicted = Path(self.temp.name) / "eligibility-conflict"
        write_snapshot(bundle, conflicted)
        result = evaluate(conflicted, {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}})
        self.assertEqual(result["reason_code"], "CONFLICTING_EVIDENCE")

    def test_unlinked_conflict_does_not_poison_selected_cohort(self):
        bundle = synthetic_bundle()
        bundle["entities"].append({"entity_id": "gdc:assay:A999", "kind": "assay", "source": "gdc", "source_id": "A999", "attributes": {"assay_type": "rna_expression", "eligible": True}})
        bundle["evidence"].extend([{ "evidence_id": "ev:A999:1", "entity_id": "gdc:assay:A999", "field_name": "eligible", "raw_value": True, "canonical_value": True, "source_record_id": "A999-1"}, {"evidence_id": "ev:A999:2", "entity_id": "gdc:assay:A999", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "A999-2"}])
        isolated = Path(self.temp.name) / "isolated-conflict"
        write_snapshot(bundle, isolated)
        result = evaluate(isolated, {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}})
        self.assertEqual(result["status"], "SUPPORTED")

    def test_unsupported_negation_is_not_ignored(self):
        result = evaluate(self.snapshot, {"operation": "count_entities", "selector": {"source": "gdc", "not": {"project_id": "TCGA-LUAD"}}})
        self.assertEqual(result["reason_code"], "INVALID_QUERY")

    def test_conflicting_project_filter_abstains_before_filtering(self):
        bundle = synthetic_bundle()
        bundle["evidence"].append({"evidence_id": "ev:C001:project-conflict", "entity_id": "gdc:case:C001", "field_name": "project_id", "raw_value": "OTHER", "canonical_value": "OTHER", "source_record_id": "C001-project-conflict"})
        conflicted = Path(self.temp.name) / "project-conflict"
        write_snapshot(bundle, conflicted)
        result = evaluate(conflicted, {"operation": "count_entities", "selector": {"source": "gdc", "project_id": "TCGA-LUAD"}})
        self.assertEqual(result["reason_code"], "CONFLICTING_EVIDENCE")

    def test_unrelated_assay_conflict_does_not_block_rna_availability(self):
        bundle = synthetic_bundle()
        bundle["evidence"].append({"evidence_id": "ev:A002:conflict", "entity_id": "gdc:assay:A002", "field_name": "eligible", "raw_value": False, "canonical_value": False, "source_record_id": "A002-conflict"})
        isolated = Path(self.temp.name) / "unrelated-conflict"
        write_snapshot(bundle, isolated)
        result = evaluate(isolated, {"operation": "assay_availability", "selector": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}})
        self.assertEqual(result["status"], "SUPPORTED")

