from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.profile_root import (
    ROOT,
    ProfileRootError,
    resolve_profile_root,
    validate_repository,
)


FIXTURE = Path(__file__).parent / "fixtures" / "profile-root" / "valid"
E2E_ENTITIES = Path(__file__).parent / "fixtures" / "e2e" / "entities"


class ProfileRootTests(unittest.TestCase):
    def make_profile(self, parent: Path, *, name: str = "profile") -> Path:
        root = parent / name
        shutil.copytree(FIXTURE, root)
        return root

    def make_runnable_profile(self, parent: Path, *, name: str = "runnable") -> Path:
        root = parent / name
        (root / "entities").mkdir(parents=True)
        shutil.copytree(E2E_ENTITIES, root / "entities", dirs_exist_ok=True)
        (root / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-e2e\n"
            "subject_ids: [subject/fixture]\n"
            "storage_scope: private\n",
            encoding="utf-8",
        )
        return root

    def run_tool(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / script), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_valid_external_profile_is_resolved_from_materialized_fixture(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            root = self.make_profile(Path(directory))
            layout = resolve_profile_root(root)

            self.assertEqual(root.resolve(), layout.root)
            self.assertEqual(("subject/fixture",), layout.subject_ids)
            self.assertEqual(root.resolve() / "entities", layout.entity_root)
            self.assertEqual(root.resolve() / "data", layout.data_root)
            self.assertEqual(root.resolve() / "overviews", layout.overview_root)

    def test_missing_or_relative_root_fails_without_absolute_path(self):
        with self.assertRaises(ProfileRootError) as missing:
            resolve_profile_root(None)
        self.assertEqual("PROFILE_ROOT_REQUIRED", missing.exception.code)
        self.assertNotIn(str(ROOT), str(missing.exception))

        with self.assertRaises(ProfileRootError) as relative:
            resolve_profile_root(Path("relative-profile"))
        self.assertEqual("PROFILE_ROOT_NOT_ABSOLUTE", relative.exception.code)
        self.assertNotIn("relative-profile", str(relative.exception))

    def test_repository_and_projection_overlap_are_rejected(self):
        with self.assertRaises(ProfileRootError) as repository:
            resolve_profile_root(ROOT)
        self.assertEqual("PROFILE_ROOT_REPOSITORY_OVERLAP", repository.exception.code)

        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            parent = Path(directory)
            root = self.make_profile(parent)
            projection = root / "projection"
            projection.mkdir()
            with self.assertRaises(ProfileRootError) as public_projection:
                resolve_profile_root(root, projection_roots=[projection])
            self.assertEqual("PROFILE_ROOT_PROJECTION_OVERLAP", public_projection.exception.code)

    def test_other_worktree_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            parent = Path(directory)
            root = self.make_profile(parent)
            with self.assertRaises(ProfileRootError) as error:
                resolve_profile_root(root, worktree_roots=[root])
            self.assertEqual("PROFILE_ROOT_REPOSITORY_OVERLAP", error.exception.code)

    def test_symlink_alias_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            parent = Path(directory)
            real = self.make_profile(parent, name="real")
            alias = parent / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ProfileRootError) as error:
                resolve_profile_root(alias)
            self.assertEqual("PROFILE_ROOT_INVALID", error.exception.code)

    def test_existing_generated_roots_must_be_normal_directories(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            parent = Path(directory)
            root = self.make_profile(parent)
            target = parent / "outside-generated"
            target.mkdir()
            (root / "data").symlink_to(target, target_is_directory=True)
            with self.assertRaises(ProfileRootError) as error:
                resolve_profile_root(root)
            self.assertEqual("PROFILE_LAYOUT_INVALID", error.exception.code)

    def test_closed_contract_and_subject_matching_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            root = self.make_profile(Path(directory))
            profile = root / "profile.yaml"
            profile.write_text(
                "contract_version: self-model-profile/v1\n"
                "profile_id: synthetic-fixture\n"
                "subject_ids: [subject/missing]\n"
                "storage_scope: private\n"
                "extra: forbidden\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProfileRootError) as error:
                resolve_profile_root(root)
            self.assertEqual("PROFILE_UNKNOWN_FIELD", error.exception.code)
            self.assertNotIn(str(root), str(error.exception))

            profile.write_text(
                "contract_version: self-model-profile/v1\n"
                "profile_id: synthetic-fixture\n"
                "subject_ids: [subject/missing]\n"
                "storage_scope: private\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProfileRootError) as mismatch:
                resolve_profile_root(root)
            self.assertEqual("PROFILE_SUBJECT_MISMATCH", mismatch.exception.code)

    def test_validate_repository_reports_status_without_record_content(self):
        result = validate_repository(tracked_paths=["entities/subjects/legacy.md"])
        self.assertEqual("BLOCKED_LEGACY_PROFILE", result["status"])
        self.assertEqual(1, result["legacy_record_count"])
        self.assertEqual("PASS", validate_repository(tracked_paths=["entities/subjects/README.md"])["status"])

    def test_real_data_clis_fail_closed_without_explicit_profile_root(self):
        commands = (
            ("build_graph.py", ()),
            ("build_self_model.py", ("--subject", "subject/fixture")),
            ("bundle.py", ("--subject", "subject/fixture")),
            ("audit.py", ()),
            ("export_signals.py", ("--purpose", "artistic-research")),
            ("new_entity.py", ("event", "fixture-event", "--subject", "subject/fixture")),
            (
                "intake_conversation.py",
                (
                    str(Path("tests/fixtures/intake/valid/transcript.txt")),
                    "--metadata",
                    str(Path("tests/fixtures/intake/valid/metadata.yaml")),
                    "--output-dir",
                    "data/intake",
                ),
            ),
        )
        for script, args in commands:
            with self.subTest(script=script):
                result = self.run_tool(script, *args)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("PROFILE_ROOT_REQUIRED", result.stderr)
                self.assertNotIn(str(ROOT), result.stderr)

    def test_external_profile_clis_write_only_relative_profile_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="profile-root-test-") as directory:
            root = self.make_runnable_profile(Path(directory))
            graph = self.run_tool("build_graph.py", "--profile-root", str(root))
            self.assertEqual(0, graph.returncode, graph.stderr)
            graph_check = self.run_tool("build_graph.py", "--profile-root", str(root), "--check")
            self.assertEqual(0, graph_check.returncode, graph_check.stderr)

            model = self.run_tool(
                "build_self_model.py",
                "--subject",
                "subject/fixture",
                "--profile-root",
                str(root),
            )
            self.assertEqual(0, model.returncode, model.stderr)
            bundle = self.run_tool("bundle.py", "--all", "--profile-root", str(root))
            self.assertEqual(0, bundle.returncode, bundle.stderr)
            audit = self.run_tool("audit.py", "--profile-root", str(root))
            self.assertEqual(0, audit.returncode, audit.stderr)
            export = self.run_tool(
                "export_signals.py",
                "--subject",
                "subject/fixture",
                "--purpose",
                "artistic-research",
                "--operation",
                "export-signals",
                "--profile-root",
                str(root),
                "--output",
                str(root / "data" / "export.json"),
            )
            self.assertEqual(0, export.returncode, export.stderr)
            new_entity = self.run_tool(
                "new_entity.py",
                "event",
                "synthetic-cli-event",
                "--subject",
                "subject/fixture",
                "--profile-root",
                str(root),
            )
            self.assertEqual(0, new_entity.returncode, new_entity.stderr)

            for artifact in (
                root / "data" / "graph.json",
                root / "data" / "coverage.json",
                root / "data" / "audit.json",
                root / "data" / "export.json",
                root / "data" / "self-models" / "subject" / "fixture.json",
                root / "data" / "self-models" / "subject" / "fixture.md",
                root / "overviews" / "coverage.md",
            ):
                self.assertTrue(artifact.is_file(), artifact)
                self.assertNotIn(str(root), artifact.read_text(encoding="utf-8"))
            graph_payload = json.loads((root / "data" / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual("entities/events/first.md", next(node["path"] for node in graph_payload["nodes"] if node["id"] == "event/first"))
            self.assertTrue((root / "entities" / "events" / "synthetic-cli-event.md").is_file())


if __name__ == "__main__":
    unittest.main()
