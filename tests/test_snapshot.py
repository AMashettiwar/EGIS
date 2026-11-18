import json
import tempfile
import unittest
from pathlib import Path

from evidence_engine.fixture import create_synthetic_snapshot
from evidence_engine.snapshot import read_snapshot


class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_manifest_hashes_and_typed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            manifest = create_synthetic_snapshot(snapshot)
            self.assertEqual(manifest["publication"], "READY")
            self.assertTrue((snapshot / "READY").exists())
            read_manifest, connection = read_snapshot(snapshot)
            try:
                self.assertEqual(read_manifest["snapshot_id"], manifest["snapshot_id"])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 13)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM relationships").fetchone()[0], 10)
            finally:
                connection.close()

    def test_incomplete_snapshot_is_readable_but_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            from evidence_engine.snapshot import write_snapshot
            write_snapshot({"entities": [], "relationships": [], "evidence": [], "coverage": {}}, snapshot, complete=False)
            manifest, connection = read_snapshot(snapshot)
            try:
                self.assertFalse(manifest["complete"])
            finally:
                connection.close()

    def test_cross_source_relationship_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            from evidence_engine.snapshot import write_snapshot
            bundle = {"coverage": {}, "entities": [{"entity_id": "gdc:case:1", "kind": "case", "source": "gdc", "source_id": "1", "attributes": {}}, {"entity_id": "pdc:assay:1", "kind": "assay", "source": "pdc", "source_id": "1", "attributes": {}}], "relationships": [{"subject_id": "gdc:case:1", "predicate": "HAS_ASSAY", "object_id": "pdc:assay:1", "source": "gdc", "source_record_id": "bad"}], "evidence": []}
            with self.assertRaises(ValueError):
                write_snapshot(bundle, Path(directory) / "snapshot")

    def test_raw_evidence_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            create_synthetic_snapshot(snapshot)
            (snapshot / "raw.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_snapshot(snapshot)

    def test_duplicate_source_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            from evidence_engine.snapshot import write_snapshot
            bundle = {"coverage": {}, "entities": [{"entity_id": "gdc:case:1", "kind": "case", "source": "gdc", "source_id": "1", "attributes": {}}, {"entity_id": "gdc:case:other", "kind": "case", "source": "gdc", "source_id": "1", "attributes": {}}], "relationships": [], "evidence": []}
            with self.assertRaises(ValueError):
                write_snapshot(bundle, Path(directory) / "snapshot")

