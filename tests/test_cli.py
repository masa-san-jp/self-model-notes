import json
import subprocess
import sys
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

    def test_audit_check_accepts_current_artifact(self):
        result = self.run_cli("tools/audit.py", "--check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("data/audit.json is current", result.stdout)

    def test_audit_check_and_dry_run_are_mutually_exclusive(self):
        result = self.run_cli("tools/audit.py", "--check", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

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

    def test_tracked_self_model_and_bundle_checks_are_current(self):
        model = self.run_cli("tools/build_self_model.py", "--subject", "subject/masa", "--check")
        bundle = self.run_cli("tools/bundle.py", "--subject", "subject/masa", "--check")
        all_bundles = self.run_cli("tools/bundle.py", "--all", "--check")

        self.assertEqual(model.returncode, 0, model.stderr)
        self.assertEqual(bundle.returncode, 0, bundle.stderr)
        self.assertEqual(all_bundles.returncode, 0, all_bundles.stderr)

    def test_bundle_subject_and_all_are_mutually_exclusive(self):
        result = self.run_cli("tools/bundle.py", "--subject", "subject/masa", "--all")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_snapshot_source_commit_is_entity_commit_not_artifact_head(self):
        snapshot = json.loads((ROOT / "data" / "self-models" / "subject" / "masa.json").read_text(encoding="utf-8"))
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
        ).stdout.strip()

        self.assertRegex(snapshot["source_commit"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(snapshot["source_commit"], head)

    def test_runbook_documents_non_destructive_recovery_and_collision_rules(self):
        runbook = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

        self.assertIn("手編集せず", runbook)
        self.assertIn("1 task、1 agent、1 branch、1 PR", runbook)
        self.assertIn("共有lock", runbook)
        self.assertIn("raw voice本文は既定でexportしない", runbook)

    def test_harness_policy_workflow_uses_trusted_read_only_checkouts(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")

        self.assertIn("harness-policy:", workflow)
        self.assertIn("path: trusted-base", workflow)
        self.assertIn("path: candidate", workflow)
        self.assertIn("trusted-base/tools/task_harness.py verify-pr", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("Bootstrap policy verifier", workflow)
        self.assertIn("PR_REF", workflow)


if __name__ == "__main__":
    unittest.main()
