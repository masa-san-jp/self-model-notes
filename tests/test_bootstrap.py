import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTest(unittest.TestCase):
    def test_empty_repository_graph_is_current(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap-profile-") as directory:
            profile = Path(directory)
            shutil.copytree(ROOT / "tests" / "fixtures" / "e2e" / "entities", profile / "entities")
            (profile / "profile.yaml").write_text(
                "contract_version: self-model-profile/v1\n"
                "profile_id: synthetic-bootstrap\n"
                "subject_ids: [subject/fixture]\n"
                "storage_scope: external-local\n",
                encoding="utf-8",
            )
            built = subprocess.run(
                [sys.executable, "tools/build_graph.py", "--profile-root", str(profile)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run(
                [sys.executable, "tools/build_graph.py", "--check", "--profile-root", str(profile)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_is_fail_closed(self):
        result = subprocess.run([sys.executable, "tools/export_signals.py", "--subject", "subject/x", "--purpose", "artistic-research"], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Export denied", result.stderr)


if __name__ == "__main__":
    unittest.main()
