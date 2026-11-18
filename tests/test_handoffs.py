import unittest
from pathlib import Path


class HandoffTests(unittest.TestCase):
    def test_seven_stage_handoffs_exist(self):
        handoffs = Path("handoffs")
        self.assertEqual(sorted(path.name for path in handoffs.glob("stage-*-handoff.md")), [f"stage-{index}-handoff.md" for index in range(7)])

