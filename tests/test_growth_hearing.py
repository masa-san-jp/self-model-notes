import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_validation import make_entity
from tools.kb import Entity, load_yaml, serialize_markdown
from tools.growth_tasks import (
    _atomic_write,
    _dump_yaml,
    generate,
    hearing_config_text_fields,
    hearing_open,
    hearing_skip,
    load_hearing_config,
    load_question_bank,
    question_bank_forbidden_tokens,
    queue_path,
)


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
        self.assertEqual(load_question_bank()["avoidance"]["text"], packet["question"])
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


if __name__ == "__main__":
    unittest.main()
