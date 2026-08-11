import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTest(unittest.TestCase):
    def test_empty_repository_graph_is_current(self):
        result = subprocess.run([sys.executable, "tools/build_graph.py", "--check"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_is_fail_closed(self):
        result = subprocess.run([sys.executable, "tools/export_signals.py", "--subject", "subject/x", "--purpose", "artistic-research"], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Export denied", result.stderr)


if __name__ == "__main__":
    unittest.main()

