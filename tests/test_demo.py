import tempfile
import unittest
from pathlib import Path

from cancer_evidence.cli import main


class DemoTests(unittest.TestCase):
    def test_demo_command(self):
        with tempfile.TemporaryDirectory() as directory:
            main(["demo", "--out", str(Path(directory) / "demo")])

