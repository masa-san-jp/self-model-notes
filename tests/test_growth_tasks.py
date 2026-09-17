import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_validation import make_entity
from tools.export_signals import append_growth_miss
from tools.kb import Entity, serialize_markdown
from tools.growth_tasks import (
    claim_growth_task,
    complete_growth_task,
    event_context_domain_count,
    event_span_days,
    generate,
    growth_report_path,
    load_milestones,
    next_task,
    question_bank_forbidden_tokens,
    queue_path,
    report,
    sections_supported_status,
    snapshot_before,
    load_question_bank,
    GrowthTaskError,
    QUEUE_CONTRACT,
)
from tools.kb import load_yaml


ROOT = Path(__file__).resolve().parents[1]
PLURAL = {
    "subject": "subjects",
    "source": "sources",
    "event": "events",
    "claim": "claims",
    "pattern": "patterns",
    "measurement": "measurements",
}


class GrowthTasksTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="growth-tasks-")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve() / "profile"
        for kind in PLURAL.values():
            (self.profile / "entities" / kind).mkdir(parents=True)
        (self.profile / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-growth-tasks\n"
            "subject_ids: [subject/fixture]\n"
            "storage_scope: external-local\n",
            encoding="utf-8",
        )
        self.write(make_entity("subject", "fixture"))

    def write(self, entity: Entity, body: str = "\n") -> Path:
        path = self.profile / "entities" / PLURAL[entity.type] / f"{entity.id.split('/', 1)[1]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        final = replace(entity, path=path, body=body)
        path.write_text(serialize_markdown(final), encoding="utf-8")
        return path

    def load_queue(self):
        return load_yaml(queue_path(self.profile))


class GenerateTests(GrowthTasksTestCase):
    def test_checks_command_quotes_a_profile_root_containing_spaces(self):
        import shlex

        spaced_root = Path(self.temp.name).resolve() / "Application Support" / "profile"
        shutil.copytree(self.profile, spaced_root)

        queue = generate(spaced_root)

        self.assertTrue(queue["tasks"])
        for task in queue["tasks"]:
            for command in task["checks"]:
                argv = shlex.split(command)
                self.assertIn(str(spaced_root), argv, command)

    def test_regeneration_refreshes_checks_for_still_ready_tasks(self):
        first = generate(self.profile)
        path = queue_path(self.profile)
        queue = load_yaml(path)
        queue["tasks"][0]["checks"] = ["this is a stale, wrong command"]
        path.write_text(json.dumps(queue), encoding="utf-8")

        second = generate(self.profile)

        self.assertEqual(second["tasks"][0]["id"], first["tasks"][0]["id"])
        self.assertNotEqual(second["tasks"][0]["checks"], ["this is a stale, wrong command"])
        self.assertEqual(second["tasks"][0]["checks"], first["tasks"][0]["checks"])

    def test_empty_profile_is_idempotent_and_starts_with_acquire_event(self):
        first = generate(self.profile)
        first_bytes = queue_path(self.profile).read_bytes()
        second = generate(self.profile)
        second_bytes = queue_path(self.profile).read_bytes()

        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first["contract_version"], QUEUE_CONTRACT)
        self.assertTrue(first["tasks"])
        self.assertEqual(first["tasks"][0]["kind"], "acquire-event")
        self.assertEqual(second, first)

    def test_milestone_gaps_cover_all_six_sections(self):
        queue = generate(self.profile)
        sections = {task["target"]["section"] for task in queue["tasks"] if task["kind"] == "acquire-event"}

        self.assertEqual(
            sections,
            {"dominant_triggers", "dominant_rewards", "avoidance_targets", "protective_factors", "context_dependencies", "tensions"},
        )

    def test_regeneration_preserves_ids_and_drops_resolved_gaps(self):
        first = generate(self.profile)
        by_section = {task["target"]["section"]: task["id"] for task in first["tasks"]}
        tensions_id = by_section["tensions"]

        self.write(make_entity("event", "grounding", subject="subject/fixture", source_refs=[]))
        self.write(
            make_entity(
                "claim",
                "tension-one",
                subject="subject/fixture",
                layer="tension",
                motivation_direction=None,
                scope="state",
                status="supported",
                counterevidence=["event/grounding"],
                supporting_evidence=["event/grounding"],
            )
        )
        second = generate(self.profile)
        second_by_section = {task["target"]["section"]: task["id"] for task in second["tasks"]}

        self.assertNotIn("tensions", second_by_section)
        for section, task_id in by_section.items():
            if section == "tensions":
                continue
            self.assertEqual(second_by_section[section], task_id)


class CasTests(GrowthTasksTestCase):
    def test_claim_requires_matching_sha256_and_generate_refuses_while_busy(self):
        first = next_task(self.profile)
        generate(self.profile)  # no-op regenerate, still nothing in-progress
        current = next_task(self.profile)

        with self.assertRaises(GrowthTaskError) as failure:
            claim_growth_task(self.profile, current["task"]["id"], actor="tester", expected_queue_sha256="deadbeef")
        self.assertEqual(failure.exception.code, "QUEUE_CONFLICT")

        claimed = claim_growth_task(
            self.profile, current["task"]["id"], actor="tester", expected_queue_sha256=current["queue_sha256"]
        )
        self.assertEqual(claimed["task"]["status"], "in-progress")

        with self.assertRaises(GrowthTaskError) as busy:
            generate(self.profile)
        self.assertEqual(busy.exception.code, "QUEUE_BUSY")


class QuestionBankTests(unittest.TestCase):
    def test_no_question_contains_a_forbidden_token(self):
        forbidden = question_bank_forbidden_tokens()
        bank = load_question_bank()

        self.assertTrue(forbidden)
        self.assertTrue(bank)
        for question in bank.values():
            lowered = question["text"].lower()
            for token in forbidden:
                self.assertNotIn(token.lower(), lowered, f"{question['id']} leaks {token!r}")


class CompletionTests(GrowthTasksTestCase):
    def claim_by_kind(self, kind, predicate=lambda task: True):
        current = next_task(self.profile)
        while current["task"] is not None and (current["task"]["kind"] != kind or not predicate(current["task"])):
            claimed = claim_growth_task(
                self.profile, current["task"]["id"], actor="tester", expected_queue_sha256=current["queue_sha256"]
            )
            complete_growth_task(
                self.profile,
                current["task"]["id"],
                expected_queue_sha256=claimed["queue_sha256"],
                before_snapshot=snapshot_before(self.profile, claimed["task"]),
            )
            current = next_task(self.profile)
        self.assertIsNotNone(current["task"], f"no ready task of kind {kind}")
        return claim_growth_task(
            self.profile, current["task"]["id"], actor="tester", expected_queue_sha256=current["queue_sha256"]
        )

    def test_acquire_event_completion_fills_section_and_removes_gap(self):
        generate(self.profile)
        claimed = self.claim_by_kind("acquire-event", lambda task: task["target"]["section"] == "avoidance_targets")
        before = snapshot_before(self.profile, claimed["task"])

        self.write(
            make_entity(
                "source",
                "growth-conversation",
                subject="subject/fixture",
                consent={
                    "obtained": True,
                    "obtained_at": "2026-09-16",
                    "purposes": ["self-reflection", "research", "artistic-research"],
                    "allowed_operations": ["store-reference", "analyze", "derive", "bundle", "export-signals"],
                    "expires_at": None,
                    "revoked_at": None,
                    "notes": None,
                },
            )
        )
        self.write(
            make_entity(
                "event",
                "growth-avoidance",
                subject="subject/fixture",
                source_refs=["source/growth-conversation"],
                raw_voice=[{"text": "後回しにした", "source_ref": "source/growth-conversation"}],
            )
        )
        self.write(
            make_entity(
                "claim",
                "growth-avoid",
                subject="subject/fixture",
                layer="motivation",
                motivation_direction="avoid",
                status="supported",
                counterevidence=["event/growth-avoidance"],
                supporting_evidence=["event/growth-avoidance"],
            )
        )

        result = complete_growth_task(
            self.profile,
            claimed["task"]["id"],
            expected_queue_sha256=claimed["queue_sha256"],
            before_snapshot=before,
        )
        self.assertEqual(result["task"]["status"], "done")

        regenerated = generate(self.profile)
        ready_sections = {
            task["target"]["section"]
            for task in regenerated["tasks"]
            if task["kind"] == "acquire-event" and task["status"] == "ready"
        }
        self.assertNotIn("avoidance_targets", ready_sections)

    def test_acquire_event_slot_completion_updates_only_the_slot(self):
        self.write(
            make_entity(
                "source",
                "growth-conversation",
                subject="subject/fixture",
                consent={
                    "obtained": True,
                    "obtained_at": "2026-09-16",
                    "purposes": ["self-reflection", "research", "artistic-research"],
                    "allowed_operations": ["store-reference", "analyze", "derive", "bundle", "export-signals"],
                    "expires_at": None,
                    "revoked_at": None,
                    "notes": None,
                },
            )
        )
        self.write(
            make_entity(
                "event",
                "growth-slot",
                subject="subject/fixture",
                source_refs=["source/growth-conversation"],
                emotion=None,
            )
        )
        queue = generate(self.profile)
        slot_task = next(
            task for task in queue["tasks"]
            if task["kind"] == "acquire-event" and task["target"].get("slot") == "emotion"
        )
        claimed = claim_growth_task(self.profile, slot_task["id"], actor="tester", expected_queue_sha256=next_task(self.profile)["queue_sha256"])
        before = snapshot_before(self.profile, claimed["task"])

        path = self.profile / "entities" / "events" / "growth-slot.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("emotion: null", "emotion: []"), encoding="utf-8")

        result = complete_growth_task(
            self.profile, claimed["task"]["id"], expected_queue_sha256=claimed["queue_sha256"], before_snapshot=before
        )
        self.assertEqual(result["task"]["status"], "done")

    def test_search_counterevidence_completes_without_human_input(self):
        self.write(
            make_entity(
                "event",
                "e1",
                subject="subject/fixture",
                observed_facts=["something happened"],
                source_refs=[],
            )
        )
        self.write(
            make_entity(
                "claim",
                "needs-counterevidence",
                subject="subject/fixture",
                supporting_evidence=["event/e1"],
                counterevidence=[],
            )
        )
        queue = generate(self.profile)
        task = next(task for task in queue["tasks"] if task["kind"] == "search-counterevidence")
        claimed = claim_growth_task(self.profile, task["id"], actor="tester", expected_queue_sha256=next_task(self.profile)["queue_sha256"])

        path = self.profile / "entities" / "claims" / "needs-counterevidence.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("counterevidence: []", "counterevidence: []")
        path.write_text(text + "\n## 反証探索\n\n同条件のEventを探索したが、対立する結果は見つからなかった。\n", encoding="utf-8")

        result = complete_growth_task(
            self.profile, claimed["task"]["id"], expected_queue_sha256=claimed["queue_sha256"],
            before_snapshot=snapshot_before(self.profile, claimed["task"]),
        )
        self.assertEqual(result["task"]["status"], "done")

    def test_derive_claim_completes_without_human_input(self):
        self.write(make_entity("event", "e1", subject="subject/fixture", source_refs=[]))
        self.write(make_entity("event", "e2", subject="subject/fixture", source_refs=[]))
        self.write(
            make_entity(
                "claim",
                "breadth-claim",
                subject="subject/fixture",
                scope="trait-candidate",
                supporting_evidence=["event/e1"],
            )
        )
        queue = generate(self.profile)
        task = next(task for task in queue["tasks"] if task["kind"] == "derive-claim")
        claimed = claim_growth_task(self.profile, task["id"], actor="tester", expected_queue_sha256=next_task(self.profile)["queue_sha256"])
        before = snapshot_before(self.profile, claimed["task"])

        path = self.profile / "entities" / "claims" / "breadth-claim.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("status: hypothesis", "status: revised")
        path.write_text(text, encoding="utf-8")

        result = complete_growth_task(
            self.profile, claimed["task"]["id"], expected_queue_sha256=claimed["queue_sha256"], before_snapshot=before
        )
        self.assertEqual(result["task"]["status"], "done")

    def test_refresh_claim_completes_without_human_input(self):
        self.write(make_entity("event", "e1", subject="subject/fixture", source_refs=[]))
        old_claim = make_entity(
            "claim", "stale-claim", subject="subject/fixture",
            supporting_evidence=["event/e1"], created="2025-01-01", updated="2025-01-01",
        )
        self.write(old_claim)
        queue = generate(self.profile)
        task = next(task for task in queue["tasks"] if task["kind"] == "refresh-claim")
        claimed = claim_growth_task(self.profile, task["id"], actor="tester", expected_queue_sha256=next_task(self.profile)["queue_sha256"])
        before = snapshot_before(self.profile, claimed["task"])

        path = self.profile / "entities" / "claims" / "stale-claim.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("updated: '2025-01-01'", "updated: '2026-09-16'").replace("updated: 2025-01-01", "updated: 2026-09-16")
        path.write_text(text, encoding="utf-8")

        result = complete_growth_task(
            self.profile, claimed["task"]["id"], expected_queue_sha256=claimed["queue_sha256"], before_snapshot=before
        )
        self.assertEqual(result["task"]["status"], "done")


class MissDrivenTests(GrowthTasksTestCase):
    def test_generate_reads_misses_and_task_disappears_once_resolved(self):
        append_growth_miss(
            self.profile / "data",
            requester="run-1",
            subject="subject/fixture",
            purpose="artistic-research",
            section="avoids",
            reason="empty",
            evidence_count=0,
        )
        queue = generate(self.profile)
        avoid_tasks = [task for task in queue["tasks"] if task["target"].get("section") == "avoidance_targets"]
        self.assertTrue(avoid_tasks)


class MilestoneConfigTests(unittest.TestCase):
    def test_growth_milestones_config_defines_the_three_dimensions(self):
        milestones = load_milestones()

        self.assertIn("sections_supported", milestones)
        self.assertNotIn("value", milestones["sections_supported"])
        self.assertEqual(milestones["min_event_contexts"]["value"], 2)
        self.assertEqual(milestones["min_event_span_days"]["value"], 30)


class ReportTests(GrowthTasksTestCase):
    def test_report_on_empty_profile_shows_all_sections_unsatisfied(self):
        content = report(self.profile)
        written = growth_report_path(self.profile).read_text(encoding="utf-8")

        self.assertEqual(content, written)
        self.assertIn("# Growth", content)
        for section in (
            "dominant_triggers", "dominant_rewards", "avoidance_targets",
            "protective_factors", "context_dependencies", "tensions",
        ):
            self.assertIn(f"| {section} | no |", content)
        self.assertIn("observed domains: 0", content)
        self.assertIn("threshold: 2", content)
        self.assertIn("No ready growth task.", content)

    def test_event_context_and_span_helpers(self):
        self.write(
            make_entity(
                "event", "e1", subject="subject/fixture", source_refs=[],
                time={"observed_at": "2026-01-01T00:00:00+09:00", "precision": "day"},
                context={"domains": ["work"], "social": ["alone"], "uncertainty": "unknown", "control": "unknown"},
            )
        )
        self.write(
            make_entity(
                "event", "e2", subject="subject/fixture", source_refs=[],
                time={"observed_at": "2026-02-15T00:00:00+09:00", "precision": "day"},
                context={"domains": ["learning"], "social": ["alone"], "uncertainty": "unknown", "control": "unknown"},
            )
        )
        from tools.kb import discover_entities
        entities = discover_entities(self.profile / "entities")

        self.assertEqual(event_context_domain_count(entities), 2)
        self.assertGreaterEqual(event_span_days(entities), 30)

    def test_sections_supported_status_reflects_current_claims(self):
        from tools.kb import discover_entities

        before = sections_supported_status(discover_entities(self.profile / "entities"))
        self.assertFalse(before["tensions"])

        self.write(make_entity("event", "grounding", subject="subject/fixture", source_refs=[]))
        self.write(
            make_entity(
                "claim", "tension-one", subject="subject/fixture", layer="tension",
                motivation_direction=None, scope="state", status="supported",
                counterevidence=["event/grounding"], supporting_evidence=["event/grounding"],
            )
        )
        after = sections_supported_status(discover_entities(self.profile / "entities"))
        self.assertTrue(after["tensions"])


if __name__ == "__main__":
    unittest.main()
