from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import migrate_profile
from tools.profile_root import ROOT, ProfileRootError, resolve_profile_root


FIXTURE = Path(__file__).parent / "fixtures" / "profile-root" / "valid"


class MigrateProfileTests(unittest.TestCase):
    def make_source(self, parent: Path, *, name: str = "source") -> Path:
        root = parent / name
        shutil.copytree(FIXTURE, root)
        return root

    def approval(self, parent: Path, *, scope: str = "synthetic-profile-migration", plan_sha256: str | None = None) -> Path:
        path = parent / "approval.yaml"
        content = f"approved: true\nscope: {scope}\n"
        if plan_sha256 is not None:
            content += f'plan_sha256: "{plan_sha256}"\n'
        path.write_text(content, encoding="utf-8")
        return path

    def snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "migrate_profile.py"), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_plan_is_deterministic_and_metadata_only(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            first = migrate_profile.plan(source, parent / "destination-a")
            second = migrate_profile.plan(source, parent / "destination-b")

            self.assertEqual(first, second)
            self.assertEqual("profile-migration-plan", first["operation"])
            self.assertEqual(".", first["source"]["relative_root"])
            self.assertEqual(".", first["destination"]["relative_root"])
            self.assertGreater(first["source"]["file_count"], 0)
            self.assertRegex(first["source"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(first["expected_generated_digest"], r"^[0-9a-f]{64}$")
            rendered = json.dumps(first, ensure_ascii=False)
            self.assertNotIn(str(source), rendered)
            self.assertNotIn("Synthetic fixture subject", rendered)
            self.assertNotIn("fixture", rendered)
            self.assertNotIn("/private/", rendered)

            cli = self.run_tool(
                "plan",
                "--source",
                str(source),
                "--destination",
                str(parent / "destination-cli"),
                "--json",
            )
            self.assertEqual(0, cli.returncode, cli.stderr)
            self.assertNotIn(str(source), cli.stdout)
            self.assertEqual(first, json.loads(cli.stdout))

    def test_apply_is_create_only_and_source_preserving(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            before = self.snapshot(source)
            destination = parent / "destination"
            plan = migrate_profile.plan(source, destination)
            approval = self.approval(parent, plan_sha256=plan["plan_sha256"])

            result = migrate_profile.apply(source, destination, approval)

            self.assertEqual("APPLIED", result["status"])
            self.assertTrue(result["source_unchanged"])
            self.assertEqual(plan["source"]["sha256"], result["source"]["sha256"])
            self.assertEqual(before, self.snapshot(source))
            self.assertEqual(before, self.snapshot(destination))
            self.assertTrue(resolve_profile_root(destination).root == destination.resolve())
            self.assertFalse((destination / "data").exists())
            self.assertFalse((destination / "overviews").exists())

    def test_existing_empty_destination_is_allowed_but_nonempty_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            empty = parent / "empty"
            (empty / "entities").mkdir(parents=True)
            approval = self.approval(parent)
            migrate_profile.apply(source, empty, approval)
            self.assertTrue((empty / "profile.yaml").is_file())

            nonempty = parent / "nonempty"
            nonempty.mkdir()
            sentinel = nonempty / "keep.txt"
            sentinel.write_bytes(b"keep")
            before = self.snapshot(source)
            with self.assertRaises(migrate_profile.MigrationError) as error:
                migrate_profile.apply(source, nonempty, approval)
            self.assertEqual("MIGRATION_DESTINATION_NOT_EMPTY", error.exception.code)
            self.assertEqual(b"keep", sentinel.read_bytes())
            self.assertEqual(before, self.snapshot(source))

    def test_approval_is_required_and_bound_to_plan_when_supplied(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            destination = parent / "destination"
            plan = migrate_profile.plan(source, destination)

            with self.assertRaises(migrate_profile.MigrationError) as missing:
                migrate_profile.apply(source, destination, parent / "missing.yaml")
            self.assertEqual("MIGRATION_APPROVAL_INVALID", missing.exception.code)

            wrong = self.approval(parent, plan_sha256="0" * 64)
            with self.assertRaises(migrate_profile.MigrationError) as mismatch:
                migrate_profile.apply(source, destination, wrong)
            self.assertEqual("MIGRATION_APPROVAL_MISMATCH", mismatch.exception.code)

            rejected = parent / "rejected.yaml"
            rejected.write_text("approved: false\nscope: synthetic-profile-migration\n", encoding="utf-8")
            with self.assertRaises(migrate_profile.MigrationError) as denied:
                migrate_profile.apply(source, destination, rejected)
            self.assertEqual("MIGRATION_APPROVAL_INVALID", denied.exception.code)

    def test_synthetic_approval_cannot_be_used_for_non_synthetic_profile(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            profile = source / "profile.yaml"
            profile.write_text(
                profile.read_text(encoding="utf-8").replace(
                    "profile_id: synthetic-fixture", "profile_id: external-profile"
                ),
                encoding="utf-8",
            )
            destination = parent / "destination"
            plan = migrate_profile.plan(source, destination)
            approval = self.approval(parent, plan_sha256=plan["plan_sha256"])
            with self.assertRaises(migrate_profile.MigrationError) as error:
                migrate_profile.apply(source, destination, approval)
            self.assertEqual("MIGRATION_APPROVAL_SCOPE", error.exception.code)
            self.assertFalse(destination.exists())

    def test_partial_copy_leaves_source_unchanged_and_staging_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            destination = parent / "destination"
            approval = self.approval(parent)
            before = self.snapshot(source)
            original = migrate_profile._copy_file_exclusive
            calls = 0

            def fail_after_first(source_path: Path, destination_path: Path) -> None:
                nonlocal calls
                if calls == 0:
                    calls += 1
                    original(source_path, destination_path)
                    return
                raise migrate_profile.MigrationError(
                    "MIGRATION_COPY_FAILED",
                    "synthetic copy failure",
                )

            with mock.patch.object(migrate_profile, "_copy_file_exclusive", fail_after_first):
                with self.assertRaises(migrate_profile.MigrationError) as error:
                    migrate_profile.apply(source, destination, approval)
            self.assertEqual("MIGRATION_COPY_FAILED", error.exception.code)
            self.assertEqual(before, self.snapshot(source))
            self.assertFalse(destination.exists())
            self.assertFalse(any(path.name.startswith(".profile-migration-") for path in parent.iterdir()))

    def test_cli_fail_closed_without_absolute_path_or_source_content(self):
        with tempfile.TemporaryDirectory(prefix="migration-test-") as directory:
            parent = Path(directory)
            source = self.make_source(parent)
            result = self.run_tool(
                "apply",
                "--source",
                str(source),
                "--destination",
                str(parent / "destination"),
                "--approval-file",
                str(parent / "missing.yaml"),
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("MIGRATION_APPROVAL_INVALID", result.stderr)
            self.assertNotIn(str(source), result.stderr)
            self.assertNotIn("Synthetic fixture subject", result.stderr)


if __name__ == "__main__":
    unittest.main()
