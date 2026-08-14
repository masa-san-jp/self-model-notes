import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def run_cli(self, script, *args):
        return subprocess.run(
            [sys.executable, script, *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_operator_commands_expose_help(self):
        for script in (
            "tools/new_entity.py",
            "tools/build_graph.py",
            "tools/audit.py",
            "tools/build_self_model.py",
            "tools/bundle.py",
            "tools/export_signals.py",
        ):
            with self.subTest(script=script):
                result = self.run_cli(script, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())

    def test_validation_and_audit_commands_are_executable(self):
        graph = self.run_cli("tools/build_graph.py", "--check")
        audit = self.run_cli("tools/audit.py", "--dry-run")

        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(audit.returncode, 0, audit.stderr)
        report = json.loads(audit.stdout)
        self.assertIsInstance(report.get("findings"), list)
        self.assertEqual(
            report.get("policy"),
            "soft review; findings propose observations and do not make diagnostic conclusions",
        )

    def test_model_bundle_and_export_fail_closed_for_unknown_subject(self):
        model = self.run_cli("tools/build_self_model.py", "--subject", "subject/missing")
        bundle = self.run_cli("tools/bundle.py", "--subject", "subject/missing")
        export = self.run_cli(
            "tools/export_signals.py",
            "--subject",
            "subject/missing",
            "--purpose",
            "artistic-research",
            "--operation",
            "export-signals",
        )

        self.assertNotEqual(model.returncode, 0)
        self.assertNotEqual(bundle.returncode, 0)
        self.assertNotEqual(export.returncode, 0)
        self.assertIn("Export denied", export.stderr)

    def test_export_cli_supports_common_options(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "signals.json"
            result = self.run_cli(
                "tools/export_signals.py",
                "--purpose",
                "artistic-research",
                "--output",
                str(output_path),
                "--limit",
                "1",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], "research-signal-export/v1")
            self.assertEqual(payload["signal_count"], len(payload["signals"]))
            self.assertEqual(payload["signal_count"], 1)

    def test_runbook_documents_non_destructive_recovery_and_collision_rules(self):
        runbook = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

        self.assertIn("手編集せず", runbook)
        self.assertIn("1 task、1 agent、1 branch、1 PR", runbook)
        self.assertIn("共有lock", runbook)
        self.assertIn("raw voice本文は既定でexportしない", runbook)


if __name__ == "__main__":
    unittest.main()
