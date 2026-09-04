from __future__ import annotations

import shutil
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


class ProfileRootTests(unittest.TestCase):
    def make_profile(self, parent: Path, *, name: str = "profile") -> Path:
        root = parent / name
        shutil.copytree(FIXTURE, root)
        return root

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


if __name__ == "__main__":
    unittest.main()
