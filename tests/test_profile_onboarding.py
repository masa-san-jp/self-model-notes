from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.kb import discover_entities, parse_markdown, serialize_markdown, validate_entities
from tools.new_entity import template
from tools.profile_root import (
    ROOT, ProfileRootError, init_profile, record_hearing_consent, resolve_profile_root,
)


OPERATIONS = ["store-reference", "analyze", "derive", "export-signals"]


class ProfileOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-onboarding-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "profile"

    def run_tool(self, script, *args, input=None):
        return subprocess.run(
            [sys.executable, "tools/agent_runtime.py", f"tools/{script}.py", *args],
            cwd=ROOT, text=True, input=input, capture_output=True,
        )

    def consent(self, **changes):
        args = dict(subject="fixture", purposes=["artistic-research"],
                    allowed_operations=OPERATIONS, expires_at="none", confirm_owner_consent=True)
        args.update(changes)
        return record_hearing_consent(self.root, **args)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_init_breaks_subject_bootstrap_cycle_without_consent(self):
        layout = init_profile(self.root, subject="fixture")
        self.assertEqual(layout, resolve_profile_root(self.root))
        self.assertEqual(("subject/fixture",), layout.subject_ids)
        self.assertEqual([], validate_entities(discover_entities(layout.entity_root), root=self.root))
        subject = parse_markdown(layout.entity_root / "subjects/fixture.md")
        self.assertEqual([], subject.meta["consent_refs"])
        self.assertEqual([], subject.meta["allowed_purposes"])
        for name in ("growth", "data", "overviews", "entities/sources"):
            self.assertEqual([], list((self.root / name).iterdir()))
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)

    def test_init_refuses_any_existing_directory_without_changes(self):
        self.root.mkdir()
        with self.assertRaises(ProfileRootError) as error:
            init_profile(self.root, subject="fixture")
        self.assertEqual("PROFILE_ROOT_EXISTS", error.exception.code)
        self.assertEqual([], list(self.root.iterdir()))
        (self.root / "sentinel").write_text("synthetic existing content")
        before = self.snapshot()
        with self.assertRaises(ProfileRootError):
            init_profile(self.root, subject="fixture")
        self.assertEqual(before, self.snapshot())

    def test_init_rejects_missing_relative_repository_and_invalid_subject(self):
        for root, subject in ((None, "fixture"), (Path("relative"), "fixture"),
                              (ROOT / "forbidden-profile", "fixture"),
                              (self.root, "../escape"), (self.root, "Fixture"),
                              (self.root, "subject/fixture"), (self.root, None)):
            with self.subTest(root=root, subject=subject), self.assertRaises(ProfileRootError):
                init_profile(root, subject=subject)
        self.assertFalse(self.root.exists())
        self.assertFalse((ROOT / "forbidden-profile").exists())

    def test_init_rejects_symlink_and_symlink_parent(self):
        alias = self.parent / "alias"
        target = self.parent / "real"
        target.mkdir()
        alias.symlink_to(target, target_is_directory=True)
        for root in (alias, alias / "profile"):
            with self.subTest(root=root), self.assertRaises(ProfileRootError):
                init_profile(root, subject="fixture")
        self.assertEqual([], list(target.iterdir()))

    def test_init_rejects_worktree_and_public_projection_before_writing(self):
        worktree = self.parent / "worktree"
        worktree.mkdir()
        with mock.patch("tools.profile_root._git_worktree_roots", return_value=[worktree]):
            with self.assertRaises(ProfileRootError):
                init_profile(worktree / "profile", subject="fixture")
        projection = ROOT / "public"
        with mock.patch("tools.profile_root._is_within", side_effect=lambda a, b: b == projection):
            with self.assertRaises(ProfileRootError) as error:
                init_profile(self.root, subject="fixture")
        self.assertEqual("PROFILE_ROOT_PROJECTION_OVERLAP", error.exception.code)
        self.assertFalse(self.root.exists())

    def test_init_rolls_back_only_its_new_directory_on_write_failure(self):
        with mock.patch("tools.profile_root.atomic_write_text", side_effect=ProfileRootError("TEST", "synthetic failure")):
            with self.assertRaises(ProfileRootError):
                init_profile(self.root, subject="fixture")
        self.assertFalse(self.root.exists())

    def test_successful_init_inside_caller_exception_handler_keeps_profile(self):
        try:
            raise RuntimeError("synthetic caller failure")
        except RuntimeError:
            layout = init_profile(self.root, subject="fixture")
        self.assertEqual(layout, resolve_profile_root(self.root))
        self.assertTrue((self.root / "entities/subjects/fixture.md").is_file())

    def test_successful_consent_inside_caller_exception_handler_keeps_records(self):
        init_profile(self.root, subject="fixture")
        try:
            raise RuntimeError("synthetic caller failure")
        except RuntimeError:
            layout = self.consent()
        source_path = self.root / "entities/sources/hearing-consent-fixture.md"
        source = parse_markdown(source_path)
        subject = parse_markdown(self.root / "entities/subjects/fixture.md")
        self.assertTrue(source.meta["consent"]["obtained"])
        self.assertEqual([source.id], subject.meta["consent_refs"])
        self.assertEqual([], validate_entities(discover_entities(layout.entity_root), root=self.root))

    def test_init_cancellation_rolls_back_and_reraises_original_exception(self):
        cancelled = KeyboardInterrupt("synthetic cancellation")
        with mock.patch("tools.profile_root.atomic_write_text", side_effect=cancelled):
            with self.assertRaises(KeyboardInterrupt) as error:
                init_profile(self.root, subject="fixture")
        self.assertIs(cancelled, error.exception)
        self.assertFalse(self.root.exists())

    def test_consent_cancellation_restores_subject_and_removes_source(self):
        init_profile(self.root, subject="fixture")
        before = self.snapshot()
        cancelled = KeyboardInterrupt("synthetic cancellation")
        # Cancel after both writes; cleanup must preserve the original subject.
        with mock.patch("tools.kb.validate_entities", side_effect=[[], cancelled]):
            with self.assertRaises(KeyboardInterrupt) as error:
                self.consent()
        self.assertIs(cancelled, error.exception)
        self.assertEqual(before, self.snapshot())

    def test_init_rejects_unrelated_git_checkout_and_missing_nested_destination(self):
        checkout = self.parent / "unrelated-checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        for root in (checkout / "profile", checkout / "missing-parent/profile"):
            with self.subTest(root=root), self.assertRaises(ProfileRootError) as error:
                init_profile(root, subject="fixture")
            self.assertEqual("PROFILE_ROOT_REPOSITORY_OVERLAP", error.exception.code)
            self.assertNotIn(str(checkout), str(error.exception))
            self.assertFalse(root.exists())
        self.assertEqual([".git"], sorted(path.name for path in checkout.iterdir()))

    def test_consent_rejects_profile_inside_unrelated_git_checkout_without_writes(self):
        init_profile(self.root, subject="fixture")
        before = self.snapshot()
        # A checkout introduced around an existing profile must also be denied.
        subprocess.run(["git", "init", "-q", str(self.parent)], check=True, capture_output=True)
        with self.assertRaises(ProfileRootError) as error:
            self.consent()
        self.assertEqual("PROFILE_ROOT_REPOSITORY_OVERLAP", error.exception.code)
        self.assertNotIn(str(self.root), str(error.exception))
        self.assertEqual(before, self.snapshot())

    def test_consent_records_exact_scope_and_preserves_subject_body(self):
        init_profile(self.root, subject="fixture")
        subject_path = self.root / "entities/subjects/fixture.md"
        subject = parse_markdown(subject_path)
        subject = replace(subject, body="\n# Synthetic notes\nPreserve this body.\n")
        subject_path.write_text(serialize_markdown(subject))
        future = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
        self.consent(purposes=["artistic-research", "self-reflection"], expires_at=future)
        source = parse_markdown(self.root / "entities/sources/hearing-consent-fixture.md")
        self.assertEqual("conversation", source.meta["source_kind"])
        self.assertFalse(source.meta["raw_content_stored"])
        self.assertEqual(["artistic-research", "self-reflection"], source.meta["consent"]["purposes"])
        self.assertEqual(OPERATIONS, source.meta["consent"]["allowed_operations"])
        self.assertEqual(future, source.meta["consent"]["expires_at"])
        self.assertEqual(datetime.now(timezone.utc).date().isoformat(), source.meta["consent"]["obtained_at"])
        after = parse_markdown(subject_path)
        self.assertEqual(subject.body, after.body)
        self.assertEqual([source.id], after.meta["consent_refs"])
        self.assertEqual([], validate_entities(discover_entities(self.root / "entities"), root=self.root))

    def test_consent_refuses_missing_confirmation_scope_and_invalid_expiry_without_writes(self):
        init_profile(self.root, subject="fixture")
        before = self.snapshot()
        today = datetime.now(timezone.utc).date().isoformat()
        cases = [dict(confirm_owner_consent=False), dict(purposes=None), dict(purposes=[]),
                 dict(purposes=["clinical-diagnosis"]), dict(allowed_operations=None),
                 dict(allowed_operations=["publish"]), dict(expires_at=None),
                 dict(expires_at=today), dict(expires_at="invalid"), dict(subject="missing")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ProfileRootError):
                self.consent(**changes)
            self.assertEqual(before, self.snapshot())

    def test_consent_does_not_silently_add_hearing_permissions(self):
        init_profile(self.root, subject="fixture")
        self.consent(allowed_operations=["analyze"])
        source = parse_markdown(self.root / "entities/sources/hearing-consent-fixture.md")
        self.assertEqual(["analyze"], source.meta["consent"]["allowed_operations"])
        result = self.run_tool("growth_tasks", "hearing", "open", "--profile-root", str(self.root),
                               "--requester", "synthetic-run", "--purpose", "artistic-research", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("NO_CONSENTED_SOURCE", json.loads(result.stdout)["reason"])

    def test_consent_is_create_only_even_after_revocation(self):
        init_profile(self.root, subject="fixture")
        self.consent()
        path = self.root / "entities/sources/hearing-consent-fixture.md"
        entity = parse_markdown(path)
        entity.meta["consent"]["revoked_at"] = datetime.now(timezone.utc).date().isoformat()
        path.write_text(serialize_markdown(entity))
        before = self.snapshot()
        with self.assertRaises(ProfileRootError) as error:
            self.consent()
        self.assertEqual("CONSENT_SOURCE_EXISTS", error.exception.code)
        self.assertEqual(before, self.snapshot())

    def test_consent_rejects_sources_symlink_without_writing_outside(self):
        init_profile(self.root, subject="fixture")
        sources = self.root / "entities/sources"
        sources.rmdir()
        target = self.parent / "outside"
        target.mkdir()
        sources.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ProfileRootError):
            self.consent()
        self.assertEqual([], list(target.iterdir()))

    def test_consent_rolls_back_source_and_subject_on_validation_failure(self):
        init_profile(self.root, subject="fixture")
        before = self.snapshot()
        with mock.patch("tools.kb.validate_entities", side_effect=[[], ["synthetic failure"]]):
            with self.assertRaises(ProfileRootError):
                self.consent()
        self.assertEqual(before, self.snapshot())

    def test_consent_cli_hides_malformed_entity_content_and_preserves_profile(self):
        init_profile(self.root, subject="fixture")
        malformed = self.root / "entities/sources/malformed.md"
        malformed.write_text("---\nsynthetic-private-marker: [\n---\n")
        before = self.snapshot()
        result = self.run_tool("profile_root", "consent", "--profile-root", str(self.root),
                               "--subject", "fixture", "--purpose", "artistic-research",
                               "--allowed-operation", "analyze", "--expires-at", "none",
                               "--confirm-owner-consent", "--json")
        self.assertEqual(2, result.returncode)
        self.assertIn("PROFILE_ENTITIES_INVALID", result.stderr)
        self.assertNotIn("synthetic-private-marker", result.stderr + result.stdout)
        self.assertNotIn(str(self.root), result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_cli_failures_hide_paths_and_do_not_grant_consent_via_init(self):
        for args in (("init", "--subject", "fixture", "--purpose", "artistic-research"),
                     ("init", "--subject", "../escape")):
            result = self.run_tool("profile_root", *args, "--profile-root", str(self.root), "--json")
            self.assertEqual(2, result.returncode)
            self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertFalse(self.root.exists())

    def test_readme_cli_flow_first_answer_is_event_then_one_derived_claim_exports(self):
        # No fixture copying: exactly the new-user commands and stdin format.
        result = self.run_tool("profile_root", "init", "--profile-root", str(self.root), "--subject", "fixture", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(str(self.root), result.stdout)
        result = self.run_tool("growth_tasks", "hearing", "open", "--profile-root", str(self.root),
                               "--requester", "synthetic-before-consent", "--purpose", "artistic-research", "--json")
        self.assertEqual("NO_CONSENTED_SOURCE", json.loads(result.stdout)["reason"])
        consent_args = ["consent", "--profile-root", str(self.root), "--subject", "fixture",
                        "--purpose", "artistic-research", "--expires-at", "none", "--confirm-owner-consent", "--json"]
        for operation in OPERATIONS:
            consent_args.extend(["--allowed-operation", operation])
        result = self.run_tool("profile_root", *consent_args)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(str(self.root), result.stdout)
        result = self.run_tool("growth_tasks", "hearing", "open", "--profile-root", str(self.root),
                               "--requester", "synthetic-first-run", "--purpose", "artistic-research", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        packet = json.loads(result.stdout)
        self.assertEqual("offered", packet["outcome"])
        self.assertEqual("avoidance", packet["question_id"])
        today = datetime.now(timezone.utc).date().isoformat()
        answer = f'''[event: first-observation]
observed_at: "{today}T09:00:00+00:00"
precision: minute
domain: creative-practice
social: alone
uncertainty: unknown
control: self-directed
fatigue: null
stress: unknown
trigger: 作業順序を急に変える必要が出た
observed_fact: 小さく試してから順序を決めた
raw_voice: "急に全部を変えるのは避けたい"
appraisal: null
emotion: null
body: null
cognition: null
action: 小さく試した
immediate_outcome: 作業を再開した
delayed_outcome: null
'''
        result = self.run_tool("growth_tasks", "hearing", "answer", packet["task_id"],
                               "--profile-root", str(self.root), "--requester", "synthetic-first-run",
                               "--expected-queue-sha256", packet["queue_sha256"], "--json", input=answer)
        self.assertEqual(0, result.returncode, result.stderr)
        answered = json.loads(result.stdout)
        self.assertEqual("answered", answered["outcome"])
        self.assertNotIn("急に全部", result.stdout + result.stderr)
        output_path = self.root / "data/self-signals.json"
        exported = self.run_tool("export_signals", "--profile-root", str(self.root),
                                 "--subject", "subject/fixture", "--purpose", "artistic-research",
                                 "--operation", "export-signals", "--requester", "synthetic-first-run",
                                 "--output", str(output_path))
        self.assertEqual(0, exported.returncode, exported.stderr)
        self.assertEqual(0, json.loads(output_path.read_text())["signal_count"])
        # Existing acquisition protocol: the agent derives a scoped hypothesis
        # from the Event, rather than treating the answer itself as a signal.
        event = next(entity for entity in discover_entities(self.root / "entities") if entity.type == "event")
        result = self.run_tool("new_entity", "claim", "first-hypothesis", "--profile-root", str(self.root),
                               "--subject", "subject/fixture")
        self.assertEqual(0, result.returncode, result.stderr)
        claim_path = self.root / "entities/claims/first-hypothesis.md"
        claim_entity = parse_markdown(claim_path)
        claim = claim_entity.meta
        claim.update(layer="motivation", motivation_direction="avoid", scope="state",
                     statement="急な全面変更を避けたい可能性がある", conditions=["作業順序が急に変わる場面"],
                     supporting_evidence=[event.id], counterevidence=[],
                     alternative_explanations=["今回だけ時間が足りなかった", "手順を比較するために試した"],
                     confidence="low", status="hypothesis")
        claim_entity = replace(claim_entity, body="\n# 反証探索\n合成 profile の全 Event を確認し、矛盾する観測は未発見。単発なので低確度。\n")
        claim_path.write_text(serialize_markdown(claim_entity))
        self.assertEqual([], validate_entities(discover_entities(self.root / "entities"), root=self.root))
        exported = self.run_tool("export_signals", "--profile-root", str(self.root),
                                 "--subject", "subject/fixture", "--purpose", "artistic-research",
                                 "--operation", "export-signals", "--requester", "synthetic-next-run",
                                 "--output", str(output_path))
        self.assertEqual(0, exported.returncode, exported.stderr)
        payload = json.loads(output_path.read_text())
        self.assertEqual(1, payload["signal_count"])
        self.assertEqual("uncertain", payload["signals"][0]["certainty"]["level"])
        self.assertTrue(payload["signals"][0]["avoids"])
        self.assertNotIn("急に全部", output_path.read_text())


if __name__ == "__main__":
    unittest.main()
