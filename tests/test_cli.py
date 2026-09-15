import json
import shutil
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

    def runnable_profile(self):
        temporary = tempfile.TemporaryDirectory(prefix="cli-profile-test-")
        root = Path(temporary.name) / "profile"
        (root / "entities").mkdir(parents=True)
        shutil.copytree(
            ROOT / "tests" / "fixtures" / "e2e" / "entities",
            root / "entities",
            dirs_exist_ok=True,
        )
        (root / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-cli\n"
            "subject_ids: [subject/fixture]\n"
            "storage_scope: external-local\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "add", "entities", "profile.yaml"], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=synthetic-fixture",
                "-c",
                "user.email=synthetic-fixture@example.invalid",
                "commit",
                "-qm",
                "synthetic profile",
            ],
            cwd=root,
            check=True,
        )
        return temporary, root

    def test_operator_commands_expose_help(self):
        for script in (
            "tools/new_entity.py",
            "tools/build_graph.py",
            "tools/audit.py",
            "tools/build_self_model.py",
            "tools/bundle.py",
            "tools/export_signals.py",
            "tools/intake_conversation.py",
            "tools/migrate_profile.py",
        ):
            with self.subTest(script=script):
                result = self.run_cli(script, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())

    def test_validation_and_audit_commands_are_executable(self):
        temporary, profile = self.runnable_profile()
        self.addCleanup(temporary.cleanup)
        built_graph = self.run_cli("tools/build_graph.py", "--profile-root", str(profile))
        self.assertEqual(0, built_graph.returncode, built_graph.stderr)
        graph = self.run_cli("tools/build_graph.py", "--check", "--profile-root", str(profile))
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
        temporary, profile = self.runnable_profile()
        self.addCleanup(temporary.cleanup)
        built = self.run_cli("tools/audit.py", "--profile-root", str(profile))
        self.assertEqual(0, built.returncode, built.stderr)
        result = self.run_cli("tools/audit.py", "--check", "--profile-root", str(profile))

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
        self.assertIn("PROFILE_ROOT_REQUIRED", export.stderr)

    def test_tracked_self_model_and_bundle_checks_are_current(self):
        temporary, profile = self.runnable_profile()
        self.addCleanup(temporary.cleanup)
        built_model = self.run_cli(
            "tools/build_self_model.py",
            "--subject",
            "subject/fixture",
            "--profile-root",
            str(profile),
        )
        built_bundle = self.run_cli(
            "tools/bundle.py",
            "--subject",
            "subject/fixture",
            "--profile-root",
            str(profile),
        )
        self.assertEqual(0, built_model.returncode, built_model.stderr)
        self.assertEqual(0, built_bundle.returncode, built_bundle.stderr)
        model = self.run_cli(
            "tools/build_self_model.py",
            "--subject",
            "subject/fixture",
            "--check",
            "--profile-root",
            str(profile),
        )
        bundle = self.run_cli(
            "tools/bundle.py",
            "--subject",
            "subject/fixture",
            "--check",
            "--profile-root",
            str(profile),
        )
        all_bundles = self.run_cli(
            "tools/bundle.py", "--all", "--check", "--profile-root", str(profile)
        )

        self.assertEqual(model.returncode, 0, model.stderr)
        self.assertEqual(bundle.returncode, 0, bundle.stderr)
        self.assertEqual(all_bundles.returncode, 0, all_bundles.stderr)

    def test_bundle_subject_and_all_are_mutually_exclusive(self):
        result = self.run_cli("tools/bundle.py", "--subject", "subject/masa", "--all")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_snapshot_source_commit_is_entity_commit_not_artifact_head(self):
        temporary, profile = self.runnable_profile()
        self.addCleanup(temporary.cleanup)
        result = self.run_cli(
            "tools/build_self_model.py",
            "--subject",
            "subject/fixture",
            "--profile-root",
            str(profile),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        snapshot = json.loads(
            (profile / "data" / "self-models" / "subject" / "fixture.json").read_text(
                encoding="utf-8"
            )
        )
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
        self.assertIn("--profile-root /absolute/path/to/profile", runbook)
        self.assertIn("MIGRATION_DESTINATION_NOT_EMPTY", (ROOT / "tests" / "test_migrate_profile.py").read_text(encoding="utf-8"))

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
