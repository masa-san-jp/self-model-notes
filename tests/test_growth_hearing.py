import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.test_validation import make_entity
from tools.kb import Entity, load_yaml, parse_markdown, serialize_markdown
from tools.growth_tasks import (
    GrowthTaskError,
    HEARING_LOG_CONTRACT,
    HEARING_PACKET_CONTRACT,
    _atomic_write,
    _dump_yaml,
    _hearing_packet_base,
    generate,
    hearing_answer,
    hearing_config_text_fields,
    hearing_open,
    hearing_skip,
    load_hearing_config,
    load_question_bank,
    question_bank_forbidden_tokens,
    queue_path,
)


CONTRACTS_ROOT = Path(__file__).resolve().parent / "contracts"
ROOT = Path(__file__).resolve().parents[1]


PLURAL = {
    "subject": "subjects",
    "source": "sources",
    "event": "events",
    "claim": "claims",
    "pattern": "patterns",
    "measurement": "measurements",
}

CONSENTED_CONVERSATION_CONSENT = {
    "obtained": True,
    "obtained_at": "2026-09-01",
    "purposes": ["self-reflection", "research", "artistic-research"],
    "allowed_operations": ["store-reference", "analyze", "derive", "export-signals"],
    "expires_at": None,
    "revoked_at": None,
    "notes": None,
}


class GrowthHearingTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="growth-hearing-")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve() / "profile"
        for kind in PLURAL.values():
            (self.profile / "entities" / kind).mkdir(parents=True)
        (self.profile / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-growth-hearing\n"
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

    def write_consented_source(self, slug: str = "conversation-20260901", **overrides) -> Path:
        fields = {
            "subject": "subject/fixture",
            "source_kind": "conversation",
            "captured_at": "2026-09-01T10:00:00+09:00",
            "consent": dict(CONSENTED_CONVERSATION_CONSENT),
        }
        fields.update(overrides)
        return self.write(make_entity("source", slug, **fields))

    def load_queue(self):
        return load_yaml(queue_path(self.profile))

    def hearings_lines(self) -> list[dict]:
        path = self.profile / "data" / "hearings.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def event_block(self, slug: str = "planning", *, trigger: str = "締切が変わった") -> str:
        return (
            f"[event: {slug}]\n"
            'observed_at: "2026-09-20T09:00:00+09:00"\n'
            "precision: minute\n"
            "domain: creative-practice\n"
            "social: alone\n"
            "uncertainty: unknown\n"
            "control: self-directed\n"
            "fatigue: null\n"
            "stress: unknown\n"
            f"trigger: {trigger}\n"
            "observed_fact: 作業順序を組み替えた\n"
            'raw_voice: "自分で決めたい"\n'
            "appraisal: []\n"
            "emotion: []\n"
            "body: []\n"
            "cognition: []\n"
            "action: 計画を変更した\n"
            "immediate_outcome: 作業を再開した\n"
            "delayed_outcome: []\n"
        )

    def export_signals_cli(self, requester: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "tools/export_signals.py",
                "--subject", "subject/fixture",
                "--purpose", "artistic-research",
                "--profile-root", str(self.profile),
                "--requester", requester,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )


class HearingConfigTests(GrowthHearingTestCase):
    def test_hearing_config_has_required_fields(self):
        config = load_hearing_config()
        self.assertEqual("hearing/v1", config["contract_version"])
        self.assertEqual(1, config["max_questions_per_run"])
        self.assertGreater(config["skip_cooldown_days"], 0)
        self.assertTrue(config["intent"])
        self.assertTrue(config["skip_ack"])
        for section in (
            "dominant_triggers",
            "dominant_rewards",
            "avoidance_targets",
            "protective_factors",
            "context_dependencies",
            "tensions",
        ):
            self.assertIn(section, config["section_reasons"])
        for slot in ("emotion", "body"):
            self.assertIn(slot, config["slot_reasons"])

    def test_no_hearing_config_text_contains_a_forbidden_token(self):
        forbidden = [token.lower() for token in question_bank_forbidden_tokens()]
        for text in hearing_config_text_fields():
            for token in forbidden:
                self.assertNotIn(token, text.lower(), text)

    def test_every_section_question_has_a_hearing_text_without_forbidden_tokens(self):
        forbidden = [token.lower() for token in question_bank_forbidden_tokens()]
        bank = load_question_bank()
        section_question_ids = {"trigger", "rewards", "avoidance", "protective", "context", "tension"}
        for question_id in section_question_ids:
            hearing_text = bank[question_id].get("hearing_text")
            self.assertTrue(hearing_text, question_id)
            lowered = hearing_text.lower()
            for token in forbidden:
                self.assertNotIn(token, lowered, f"{question_id} leaks {token!r}")


class HearingOpenTests(GrowthHearingTestCase):
    def test_offered_packet_has_every_field_and_logs_one_line(self):
        self.write_consented_source()
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        self.assertEqual("growth-hearing-packet/v1", packet["contract_version"])
        self.assertEqual("offered", packet["outcome"])
        self.assertIsNone(packet["reason"])
        self.assertEqual("run-1", packet["requester"])
        self.assertEqual("subject/fixture", packet["subject"])
        self.assertEqual("artistic-research", packet["purpose"])
        self.assertTrue(packet["queue_sha256"])
        self.assertTrue(packet["task_id"])
        self.assertEqual("avoidance", packet["question_id"])
        self.assertEqual("source/conversation-20260901", packet["source_ref"])
        self.assertEqual("event-block", packet["answer_format"])
        self.assertTrue(packet["intent"])
        self.assertEqual(
            load_hearing_config()["section_reasons"]["avoidance_targets"], packet["why"]
        )
        self.assertEqual(load_question_bank()["avoidance"]["hearing_text"], packet["question"])
        self.assertEqual([], packet["anchors"])
        self.assertEqual(load_hearing_config()["skip_ack"], packet["skip_ack"])
        self.assertEqual(4, len(packet["constraints"]))

        lines = self.hearings_lines()
        self.assertEqual(1, len(lines))
        self.assertEqual("growth-hearing/v1", lines[0]["contract_version"])
        self.assertEqual("offered", lines[0]["outcome"])
        self.assertEqual("run-1", lines[0]["requester"])
        self.assertEqual(packet["task_id"], lines[0]["task_id"])
        self.assertEqual("avoidance", lines[0]["question_id"])
        self.assertIsNone(lines[0]["entity"])

        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn(str(self.profile), serialized)

    def test_queue_is_not_mutated_by_open(self):
        self.write_consented_source()
        before = queue_path(self.profile).read_bytes() if queue_path(self.profile).exists() else None
        hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        after = queue_path(self.profile).read_bytes()
        if before is not None:
            self.assertEqual(before, after)
        queue = self.load_queue()
        self.assertTrue(all(task["status"] == "ready" for task in queue["tasks"]))

    def test_second_open_same_requester_is_already_offered_and_does_not_log(self):
        self.write_consented_source()
        hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        second = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        self.assertEqual("unavailable", second["outcome"])
        self.assertEqual("ALREADY_OFFERED_THIS_RUN", second["reason"])
        self.assertEqual("subject/fixture", second["subject"])
        self.assertIsNone(second["task_id"])
        self.assertIsNone(second["queue_sha256"])
        self.assertEqual(1, len(self.hearings_lines()))

    def test_no_consented_source_is_unavailable_and_does_not_log(self):
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("unavailable", packet["outcome"])
        self.assertEqual("NO_CONSENTED_SOURCE", packet["reason"])
        self.assertEqual([], self.hearings_lines())

    def test_revoked_source_is_not_selected(self):
        self.write_consented_source(consent={**CONSENTED_CONVERSATION_CONSENT, "revoked_at": "2026-09-02"})
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("unavailable", packet["outcome"])
        self.assertEqual("NO_CONSENTED_SOURCE", packet["reason"])

    def test_wrong_source_kind_is_not_selected(self):
        self.write_consented_source(source_kind="behavior-log")
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("unavailable", packet["outcome"])
        self.assertEqual("NO_CONSENTED_SOURCE", packet["reason"])

    def test_section_task_anchors_use_the_persons_own_recent_raw_voice(self):
        self.write_consented_source()
        self.write(
            make_entity(
                "event",
                "older",
                subject="subject/fixture",
                source_refs=["source/conversation-20260901"],
                raw_voice=[{"text": "古い方の発言", "source_ref": "source/conversation-20260901"}],
                time={"observed_at": "2026-09-01T09:00:00+09:00", "precision": "minute"},
            )
        )
        self.write(
            make_entity(
                "event",
                "newer",
                subject="subject/fixture",
                source_refs=["source/conversation-20260901"],
                raw_voice=[{"text": "新しい方の発言", "source_ref": "source/conversation-20260901"}],
                time={"observed_at": "2026-09-15T09:00:00+09:00", "precision": "minute"},
            )
        )
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual([
            {"event": "event/newer", "text": "新しい方の発言"},
            {"event": "event/older", "text": "古い方の発言"},
        ], packet["anchors"])

    def test_slot_task_anchors_use_the_target_events_raw_voice(self):
        self.write_consented_source()
        self.write(
            make_entity(
                "event",
                "existing",
                subject="subject/fixture",
                source_refs=["source/conversation-20260901"],
                emotion=None,
                raw_voice=[
                    {"text": "一つ目の発言", "source_ref": "source/conversation-20260901"},
                    {"text": "二つ目の発言", "source_ref": "source/conversation-20260901"},
                    {"text": "三つ目の発言", "source_ref": "source/conversation-20260901"},
                ],
            )
        )
        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("emotion-slot", packet["question_id"])
        self.assertEqual([
            {"event": "event/existing", "text": "一つ目の発言"},
            {"event": "event/existing", "text": "二つ目の発言"},
        ], packet["anchors"])

    def test_queue_busy_is_unavailable(self):
        self.write_consented_source()
        generate(self.profile)
        path = queue_path(self.profile)
        queue = load_yaml(path)
        queue["tasks"][0]["status"] = "in-progress"
        _atomic_write(path, _dump_yaml(queue))

        packet = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("unavailable", packet["outcome"])
        self.assertEqual("QUEUE_BUSY", packet["reason"])

    def test_all_ready_tasks_skipped_recently_is_unavailable(self):
        self.write_consented_source()
        run_index = 0
        packet = None
        while True:
            run_index += 1
            packet = hearing_open(self.profile, requester=f"run-{run_index}", purpose="artistic-research")
            if packet["outcome"] != "offered":
                break
            hearing_skip(self.profile, packet["task_id"], requester=f"run-{run_index}", reason="skipped")
        self.assertEqual("unavailable", packet["outcome"])
        self.assertEqual("ALL_SKIPPED_RECENTLY", packet["reason"])


class HearingSkipTests(GrowthHearingTestCase):
    def test_skip_appends_line_without_touching_queue(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        before = queue_path(self.profile).read_bytes()

        result = hearing_skip(self.profile, offered["task_id"], requester="run-1", reason="skipped")

        after = queue_path(self.profile).read_bytes()
        self.assertEqual(before, after)
        self.assertEqual("skipped", result["outcome"])
        self.assertEqual("skipped", result["reason"])
        self.assertEqual(offered["task_id"], result["task_id"])
        self.assertEqual(offered["question_id"], result["question_id"])

        lines = self.hearings_lines()
        self.assertEqual(2, len(lines))
        self.assertEqual("skipped", lines[1]["outcome"])

    def test_skip_no_response_reason(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        result = hearing_skip(self.profile, offered["task_id"], requester="run-1", reason="no-response")
        self.assertEqual("no-response", result["reason"])

    def test_skip_on_unknown_task_id_still_logs_without_error(self):
        result = hearing_skip(self.profile, "GT-9999", requester="run-1", reason="skipped")
        self.assertEqual("skipped", result["outcome"])
        self.assertIsNone(result["question_id"])
        self.assertEqual(1, len(self.hearings_lines()))


class HearingContractFixtureTests(unittest.TestCase):
    def load_fixture(self) -> dict:
        path = CONTRACTS_ROOT / "growth-hearing-v1.fixture.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_fixture_packets_match_the_packet_field_set(self):
        fixture = self.load_fixture()
        expected_fields = set(_hearing_packet_base(requester="x", subject="x", purpose="x"))
        for key in ("packet_offered", "packet_unavailable"):
            packet = fixture[key]
            self.assertEqual(HEARING_PACKET_CONTRACT, packet["contract_version"])
            self.assertEqual(expected_fields, set(packet), key)
        self.assertEqual("offered", fixture["packet_offered"]["outcome"])
        self.assertEqual("unavailable", fixture["packet_unavailable"]["outcome"])
        for field, value in fixture["packet_unavailable"].items():
            if field in {"contract_version", "outcome", "reason", "requester", "subject", "purpose"}:
                continue
            self.assertIsNone(value, field)

    def test_fixture_log_lines_match_the_log_field_set(self):
        fixture = self.load_fixture()
        expected_fields = {
            "contract_version", "ts", "requester", "subject", "purpose",
            "task_id", "question_id", "outcome", "reason", "entity",
        }
        for key in ("log_offered", "log_answered", "log_skipped", "log_no_response"):
            record = fixture[key]
            self.assertEqual(HEARING_LOG_CONTRACT, record["contract_version"])
            self.assertEqual(expected_fields, set(record), key)

    def test_fixture_contains_no_absolute_path(self):
        fixture = self.load_fixture()
        serialized = json.dumps(fixture, ensure_ascii=False)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn(str(Path.home()), serialized)

    def test_fixture_question_text_has_no_forbidden_token(self):
        fixture = self.load_fixture()
        forbidden = [token.lower() for token in question_bank_forbidden_tokens()]
        for text in (fixture["packet_offered"]["question"], fixture["packet_offered"]["why"], *fixture["packet_offered"]["intent"]):
            lowered = text.lower()
            for token in forbidden:
                self.assertNotIn(token, lowered, text)


class HearingAnswerTests(GrowthHearingTestCase):
    def test_event_block_answer_creates_event_marks_task_done_and_logs(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=self.event_block("planning"),
            expected_queue_sha256=offered["queue_sha256"],
            purpose="artistic-research",
        )

        self.assertEqual("answered", result["outcome"])
        self.assertIsNone(result["reason"])
        self.assertEqual(offered["task_id"], result["task_id"])
        self.assertEqual(offered["question_id"], result["question_id"])
        entity_id = result["entity"]
        self.assertTrue(entity_id.startswith("event/hearing-"))
        self.assertTrue(entity_id.endswith("-planning"))

        event_path = self.profile / "entities" / "events" / f"{entity_id.split('/', 1)[1]}.md"
        self.assertTrue(event_path.exists())
        entity = parse_markdown(event_path)
        self.assertEqual(["source/conversation-20260901"], entity.meta["source_refs"])
        self.assertEqual("source/conversation-20260901", entity.meta["raw_voice"][0]["source_ref"])

        queue = self.load_queue()
        task = next(t for t in queue["tasks"] if t["id"] == offered["task_id"])
        self.assertEqual("done", task["status"])
        self.assertIsNone(task["claim"])
        self.assertEqual([entity_id], task["evidence"]["entities"])

        lines = self.hearings_lines()
        self.assertEqual(2, len(lines))
        self.assertEqual("answered", lines[1]["outcome"])
        self.assertEqual(entity_id, lines[1]["entity"])

    def test_stdin_content_never_appears_in_the_result(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        block = self.event_block("planning", trigger="非常に固有な合言葉トリガー12345")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=block,
            expected_queue_sha256=offered["queue_sha256"],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("非常に固有な合言葉トリガー12345", serialized)
        self.assertNotIn(str(self.profile), serialized)

    def test_empty_stdin_is_unavailable_without_writing(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        before = queue_path(self.profile).read_bytes()

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text="   \n",
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertEqual("ANSWER_INVALID:empty", result["reason"])
        self.assertEqual(before, queue_path(self.profile).read_bytes())
        self.assertEqual(1, len(self.hearings_lines()))

    def test_oversized_stdin_is_unavailable_without_writing(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text="x" * (1_000_001),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertEqual("ANSWER_INVALID:too-large", result["reason"])

    def test_multiple_blocks_are_rejected_without_writing(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=self.event_block("a") + self.event_block("b"),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertTrue(result["reason"].startswith("ANSWER_INVALID:"))
        self.assertEqual(0, len(list((self.profile / "entities" / "events").glob("*.md"))))

    def test_direct_identifier_is_rejected_without_writing(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=self.event_block("planning", trigger="test@example.com からの連絡"),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertTrue(result["reason"].startswith("ANSWER_INVALID:"))
        self.assertEqual(0, len(list((self.profile / "entities" / "events").glob("*.md"))))

    def test_queue_conflict_on_sha_mismatch_writes_nothing(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=self.event_block("planning"),
            expected_queue_sha256="0" * 64,
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertEqual("QUEUE_CONFLICT", result["reason"])
        self.assertEqual(0, len(list((self.profile / "entities" / "events").glob("*.md"))))

    def test_existing_event_id_is_rejected(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        slug = "dup"
        existing_id = f"event/hearing-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{slug}"
        self.write(make_entity("event", existing_id.split("/", 1)[1], subject="subject/fixture", source_refs=["source/conversation-20260901"]))

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text=self.event_block(slug),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertEqual("ANSWER_INVALID:event-exists", result["reason"])

    def test_verification_failure_rolls_back_new_event_and_leaves_queue_unchanged(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        before = queue_path(self.profile).read_bytes()

        with mock.patch("tools.growth_tasks._run_checks", side_effect=GrowthTaskError("CHECK_FAILED", "boom")):
            result = hearing_answer(
                self.profile,
                offered["task_id"],
                requester="run-1",
                stdin_text=self.event_block("planning"),
                expected_queue_sha256=offered["queue_sha256"],
            )

        self.assertEqual("unavailable", result["outcome"])
        self.assertEqual("ANSWER_INVALID:CHECK_FAILED", result["reason"])
        self.assertEqual(before, queue_path(self.profile).read_bytes())
        self.assertEqual(0, len(list((self.profile / "entities" / "events").glob("*.md"))))
        self.assertEqual(1, len(self.hearings_lines()))

    def test_slot_answer_updates_only_target_slot_and_appends_raw_voice(self):
        self.write_consented_source()
        self.write(
            make_entity(
                "event",
                "existing",
                subject="subject/fixture",
                source_refs=["source/conversation-20260901"],
                emotion=None,
                body=[],
                raw_voice=[],
            )
        )
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        self.assertEqual("emotion-slot", offered["question_id"])

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text='emotion: ["驚き"]\nraw_voice: ["思ったより強く反応した"]\n',
            expected_queue_sha256=offered["queue_sha256"],
        )

        self.assertEqual("answered", result["outcome"])
        self.assertEqual("event/existing", result["entity"])
        entity = parse_markdown(self.profile / "entities" / "events" / "existing.md")
        self.assertEqual(["驚き"], entity.meta["emotion"])
        self.assertEqual([], entity.meta["body"])
        self.assertEqual(
            [{"text": "思ったより強く反応した", "source_ref": "source/conversation-20260901"}],
            entity.meta["raw_voice"],
        )

    def test_slot_answer_rejects_malformed_yaml_without_writing(self):
        self.write_consented_source()
        self.write(
            make_entity(
                "event",
                "existing",
                subject="subject/fixture",
                source_refs=["source/conversation-20260901"],
                emotion=None,
                body=[],
                raw_voice=[],
            )
        )
        offered = hearing_open(self.profile, requester="run-1", purpose="artistic-research")
        before = (self.profile / "entities" / "events" / "existing.md").read_bytes()

        result = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-1",
            stdin_text="not: [valid, yaml",
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", result["outcome"])
        self.assertTrue(result["reason"].startswith("ANSWER_INVALID:"))
        self.assertEqual(before, (self.profile / "entities" / "events" / "existing.md").read_bytes())


class HearingRunEntryEndToEndTests(GrowthHearingTestCase):
    """Issue #118: whatever the hearing outcome, the run always reaches export."""

    def assert_export_allowed(self, requester: str) -> None:
        result = self.export_signals_cli(requester)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("research-signal-export/v1", payload["contract_version"])
        self.assertIn("signal_count", payload)

    def test_answered_path_then_export_succeeds(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-answered", purpose="artistic-research")
        answer = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-answered",
            stdin_text=self.event_block("planning"),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("answered", answer["outcome"])
        self.assert_export_allowed("run-answered")

    def test_skipped_path_then_export_succeeds(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-skipped", purpose="artistic-research")
        skip = hearing_skip(self.profile, offered["task_id"], requester="run-skipped", reason="skipped")
        self.assertEqual("skipped", skip["outcome"])
        self.assert_export_allowed("run-skipped")

    def test_no_response_path_then_export_succeeds(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-no-response", purpose="artistic-research")
        skip = hearing_skip(self.profile, offered["task_id"], requester="run-no-response", reason="no-response")
        self.assertEqual("no-response", skip["reason"])
        self.assert_export_allowed("run-no-response")

    def test_unavailable_no_consented_source_path_then_export_still_succeeds(self):
        # source_kind is not conversation/interview, so hearing_open rejects it, but its
        # consent is otherwise valid, so export_signals accepts it independently.
        self.write_consented_source(source_kind="behavior-log")
        offered = hearing_open(self.profile, requester="run-unavailable", purpose="artistic-research")
        self.assertEqual("unavailable", offered["outcome"])
        self.assertEqual("NO_CONSENTED_SOURCE", offered["reason"])
        self.assert_export_allowed("run-unavailable")

    def test_failed_answer_path_then_export_still_succeeds(self):
        self.write_consented_source()
        offered = hearing_open(self.profile, requester="run-failed-answer", purpose="artistic-research")
        answer = hearing_answer(
            self.profile,
            offered["task_id"],
            requester="run-failed-answer",
            stdin_text=self.event_block("a") + self.event_block("b"),
            expected_queue_sha256=offered["queue_sha256"],
        )
        self.assertEqual("unavailable", answer["outcome"])
        self.assert_export_allowed("run-failed-answer")


if __name__ == "__main__":
    unittest.main()
