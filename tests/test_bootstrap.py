import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTest(unittest.TestCase):
    def test_graph_check_is_current_after_a_build(self):
        # data/ is gitignored (local-only; see docs/operations.md#ローカル専用データ), so a
        # fresh checkout has none of it committed. Build once, then --check must agree.
        build = subprocess.run([sys.executable, "tools/build_graph.py"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(build.returncode, 0, build.stderr)
        result = subprocess.run([sys.executable, "tools/build_graph.py", "--check"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_is_fail_closed(self):
        result = subprocess.run([sys.executable, "tools/export_signals.py", "--subject", "subject/x", "--purpose", "artistic-research"], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Export denied", result.stderr)


if __name__ == "__main__":
    unittest.main()

