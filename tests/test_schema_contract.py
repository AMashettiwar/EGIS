import json
import tempfile
import unittest
from pathlib import Path

from cancer_evidence.schema import write_schemas


class SchemaContractTests(unittest.TestCase):
    def test_generated_schemas_match_checked_in_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_schemas(output)
            for name in ("query", "certificate", "normalized", "result", "snapshot"):
                generated = json.loads((output / f"{name}.schema.json").read_text(encoding="utf-8"))
                checked_in = json.loads((Path("schemas") / f"{name}.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(generated, checked_in)

        snapshot = json.loads(Path("schemas/snapshot.schema.json").read_text(encoding="utf-8"))
        self.assertIn("derivation", snapshot["required"])
        self.assertIn("normalized_content_sha256", snapshot["required"])
