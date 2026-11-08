"""docs/demo_plan.json is tools/demo_plan.py's output, read by the twin's bench demo. It must match.

    python tools/demo_plan.py --json > docs/demo_plan.json     # regenerate after changing a chip
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import demo_plan  # noqa: E402


class DemoPlanJson(unittest.TestCase):
    def test_the_twins_copy_is_current(self):
        saved = json.loads((ROOT / "docs" / "demo_plan.json").read_text())
        fresh = json.loads(json.dumps(demo_plan.plans()))
        self.assertEqual(saved, fresh, "docs/demo_plan.json is stale: python tools/demo_plan.py --json > docs/demo_plan.json")


if __name__ == "__main__":
    unittest.main()
