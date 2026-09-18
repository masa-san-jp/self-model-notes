from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.profile_root import ROOT, validate_repository


class RepositoryPrivacyTests(unittest.TestCase):
    def test_real_repository_passes_with_no_blocked_category(self):
        result = validate_repository()
        self.assertEqual("PASS", result["status"])
        self.assertEqual({}, result["blocked"])
        self.assertEqual(0, result["legacy_record_count"])

    def make_git_repository(self, directory: Path) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=directory, check=True)

    def commit(self, directory: Path, path: str) -> None:
        subprocess.run(["git", "add", path], cwd=directory, check=True)

    def assert_blocked(self, directory: Path, *, category: str, count: int = 1) -> None:
        result = validate_repository(directory)
        self.assertEqual("BLOCKED_PERSONAL_RECORD", result["status"])
        self.assertEqual({category: count}, result["blocked"])
        self.assertEqual(0, result["legacy_record_count"])
        serialized = str(result)
        self.assertNotIn(str(directory), serialized)

    def test_entity_record_outside_entities_tree_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "scratch" / "leaked-event.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "id: event/real-slug\n"
                "type: event\n"
                "subject: subject/real-person\n"
                "---\n"
                "\n",
                encoding="utf-8",
            )
            self.commit(repository, "scratch/leaked-event.md")
            self.assert_blocked(repository, category="entity-record")

    def test_entity_record_fixture_with_subject_fixture_is_exempt(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "tests" / "fixtures" / "growth" / "sample.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "id: event/sample\n"
                "type: event\n"
                "subject: subject/fixture\n"
                "---\n"
                "\n",
                encoding="utf-8",
            )
            self.commit(repository, "tests/fixtures/growth/sample.md")
            result = validate_repository(repository)
            self.assertEqual("PASS", result["status"])

    def test_entity_record_fixture_with_leaked_identifier_is_blocked_as_raw_quote(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "tests" / "fixtures" / "growth" / "sample.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "id: event/sample\n"
                "type: event\n"
                "subject: subject/fixture\n"
                "trigger: \"contact real.person@example.com about it\"\n"
                "---\n"
                "\n",
                encoding="utf-8",
            )
            self.commit(repository, "tests/fixtures/growth/sample.md")
            self.assert_blocked(repository, category="raw-quote")

    def test_profile_contract_outside_fixtures_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "scratch" / "profile.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                "contract_version: self-model-profile/v1\n"
                "profile_id: private-profile-001\n"
                "subject_ids: [subject/real-person]\n"
                "storage_scope: external-local\n",
                encoding="utf-8",
            )
            self.commit(repository, "scratch/profile.yaml")
            self.assert_blocked(repository, category="profile-contract")

    def test_profile_contract_fixture_with_synthetic_id_is_exempt(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "tests" / "fixtures" / "growth" / "profile.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                "contract_version: self-model-profile/v1\n"
                "profile_id: synthetic-growth\n"
                "subject_ids: [subject/fixture]\n"
                "storage_scope: external-local\n",
                encoding="utf-8",
            )
            self.commit(repository, "tests/fixtures/growth/profile.yaml")
            result = validate_repository(repository)
            self.assertEqual("PASS", result["status"])

    def test_schema_documentation_yaml_is_not_misclassified_as_profile_contract(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "config" / "profile-root-schema.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                (ROOT / "config" / "profile-root-schema.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.commit(repository, "config/profile-root-schema.yaml")
            result = validate_repository(repository)
            self.assertEqual("PASS", result["status"])

    def test_growth_miss_jsonl_outside_fixtures_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "data" / "misses.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"contract_version":"growth-miss/v1","ts":"2026-01-01T00:00:00Z",'
                '"requester":null,"subject":"subject/real-person","purpose":"artistic-research",'
                '"section":"avoids","reason":"empty","evidence_count":0}\n',
                encoding="utf-8",
            )
            self.commit(repository, "data/misses.jsonl")
            self.assert_blocked(repository, category="growth-log")

    def test_growth_hearing_jsonl_outside_fixtures_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "data" / "hearings.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"contract_version":"growth-hearing/v1","ts":"2026-01-01T00:00:00Z",'
                '"requester":"run-1","subject":"subject/real-person","purpose":"artistic-research",'
                '"task_id":"GT-0004","question_id":"emotion-slot","outcome":"skipped",'
                '"reason":"skipped","entity":null}\n',
                encoding="utf-8",
            )
            self.commit(repository, "data/hearings.jsonl")
            self.assert_blocked(repository, category="growth-log")

    def test_growth_queue_yaml_outside_fixtures_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "growth" / "queue.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                "contract_version: growth-queue/v1\n"
                "generated_from:\n"
                "  misses_sha256: null\n"
                "  audit_sha256: abc\n"
                "  coverage_sha256: abc\n"
                "  entities_sha256: abc\n"
                "tasks: []\n",
                encoding="utf-8",
            )
            self.commit(repository, "growth/queue.yaml")
            self.assert_blocked(repository, category="growth-log")

    def test_growth_queue_schema_documentation_is_not_misclassified(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "config" / "growth-queue-schema.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                (ROOT / "config" / "growth-queue-schema.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.commit(repository, "config/growth-queue-schema.yaml")
            result = validate_repository(repository)
            self.assertEqual("PASS", result["status"])

    def test_intake_draft_suffix_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "scratch" / "conversation-20260901--planning.event.draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nid: event/conversation-20260901--planning\ntype: event\n---\n\n# Intake draft\n",
                encoding="utf-8",
            )
            self.commit(repository, "scratch/conversation-20260901--planning.event.draft.md")
            self.assert_blocked(repository, category="entity-record")

    def test_intake_draft_heading_without_suffix_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "scratch" / "draft-copy.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Intake draft\n\nHuman review is required.\n", encoding="utf-8")
            self.commit(repository, "scratch/draft-copy.md")
            self.assert_blocked(repository, category="intake-draft")

    def test_multiple_categories_are_each_counted(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            event_path = repository / "scratch" / "leaked-event.md"
            event_path.parent.mkdir(parents=True)
            event_path.write_text(
                "---\nid: event/real-slug\ntype: event\nsubject: subject/real-person\n---\n\n",
                encoding="utf-8",
            )
            draft_path = repository / "scratch" / "draft-copy.md"
            draft_path.write_text("# Intake draft\n\nHuman review is required.\n", encoding="utf-8")
            self.commit(repository, "scratch/leaked-event.md")
            self.commit(repository, "scratch/draft-copy.md")
            result = validate_repository(repository)
            self.assertEqual("BLOCKED_PERSONAL_RECORD", result["status"])
            self.assertEqual({"entity-record": 1, "intake-draft": 1}, result["blocked"])

    def test_legacy_entity_tree_check_still_takes_priority(self):
        with tempfile.TemporaryDirectory(prefix="repo-privacy-") as directory:
            repository = Path(directory)
            self.make_git_repository(repository)
            path = repository / "entities" / "events" / "real-slug.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nid: event/real-slug\ntype: event\nsubject: subject/real-person\n---\n\n",
                encoding="utf-8",
            )
            self.commit(repository, "entities/events/real-slug.md")
            result = validate_repository(repository)
            self.assertEqual("BLOCKED_LEGACY_PROFILE", result["status"])
            self.assertEqual(1, result["legacy_record_count"])
            self.assertEqual({}, result["blocked"])


if __name__ == "__main__":
    unittest.main()
