import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class MutationResistanceTests(unittest.TestCase):
    def test_replay_kills_at_least_three_of_four_engine_mutations(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path("src").resolve())
        completed = subprocess.run([sys.executable, "scripts/mutation_check.py"], capture_output=True, text=True, check=True, env=env)
        report = json.loads(completed.stdout)
        self.assertGreaterEqual(report["killed"], 3)
        self.assertEqual(report["total"], 4)
