import unittest

from evidence_engine.fixture import synthetic_bundle


class NormalizationTests(unittest.TestCase):
    def test_sources_are_namespaced_and_assays_are_typed(self):
        bundle = synthetic_bundle()
        sources = {entity["source"] for entity in bundle["entities"]}
        self.assertEqual(sources, {"gdc", "pdc"})
        assays = [entity for entity in bundle["entities"] if entity["kind"] == "assay"]
        self.assertIn("rna_expression", {item["attributes"]["assay_type"] for item in assays})
        self.assertIn("protein_abundance", {item["attributes"]["assay_type"] for item in assays})

    def test_no_cross_source_relationship_is_present(self):
        bundle = synthetic_bundle()
        entity_source = {item["entity_id"]: item["source"] for item in bundle["entities"]}
        self.assertTrue(all(entity_source[edge["subject_id"]] == entity_source[edge["object_id"]] for edge in bundle["relationships"]))

