import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.test_consent import fixture_entities
from tools.export_signals import (
    FIXED_SIGNAL_FIELDS,
    GROWTH_MISS_CONTRACT,
    GROWTH_MISS_SECTIONS,
    _group_misses,
    append_growth_miss,
    build_research_signals,
    build_signal_export,
    canonical_json,
    export_signals,
    validate_signal_export,
    validate_research_signals,
)


SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "research-signals-v1.schema.json"
ROOT = Path(__file__).resolve().parents[1]


class ExportSignalsContractTests(unittest.TestCase):
    def approved_result(self):
        return export_signals(
            fixture_entities(),
            "subject/fixture",
            "artistic-research",
            operation="export-signals",
            today=date(2026, 8, 11),
            source_commit="a" * 40,
        )

    def test_output_matches_versioned_contract_and_has_provenance(self):
        payload = build_research_signals(self.approved_result())
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema["$id"], payload["schema"])
        self.assertEqual(validate_research_signals(payload), [])
        self.assertEqual(payload["source_repository"], "masa-san-jp/self-model-notes")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertEqual(payload["as_of"], "2026-08-02")
        self.assertEqual(payload["research_signals"]["seeks"], [])
        self.assertEqual(payload["research_signals"]["tensions"][0]["evidence_refs"], ["event/observation"])
        self.assertEqual(payload["research_signals"]["raw_voice_refs"], [])
        self.assertNotIn("synthetic voice content", json.dumps(payload, ensure_ascii=False))

    def test_every_signal_item_has_certainty_and_evidence_refs(self):
        payload = build_research_signals(self.approved_result())

        for field, values in payload["research_signals"].items():
            if field == "raw_voice_refs" or field in {"certainty", "evidence_refs"}:
                continue
            for item in values:
                self.assertIn(item["certainty"], {"unknown", "low", "medium", "high"})
                self.assertTrue(item["evidence_refs"])

    def test_the_export_carries_one_record_per_claim_or_pattern(self):
        """A subject folded into one record gives the candidate space one self, whatever the study holds."""
        export = build_signal_export(self.approved_result(), generated_at="2026-08-11T00:00:00+09:00")

        self.assertEqual(export["signal_count"], len(export["signals"]))
        self.assertGreater(export["signal_count"], 1)

    def test_each_record_is_addressable_on_its_own(self):
        export = build_signal_export(self.approved_result(), generated_at="2026-08-11T00:00:00+09:00")

        ids = [signal["signal_id"] for signal in export["signals"]]
        self.assertEqual(len(set(ids)), len(ids))
        for signal in export["signals"]:
            self.assertTrue(signal["entity_id"])
            self.assertIn(signal["entity_id"], signal["source_locator"])
            self.assertEqual(set(signal), set(FIXED_SIGNAL_FIELDS))
        self.assertEqual(validate_signal_export(export), [])

    def test_a_record_carries_its_own_statement_not_a_summary_of_all(self):
        export = build_signal_export(self.approved_result(), generated_at="2026-08-11T00:00:00+09:00")

        statements = {signal["statement"] for signal in export["signals"]}
        self.assertEqual(len(statements), len(export["signals"]))

    def test_consent_denial_still_produces_no_records(self):
        denied = dict(self.approved_result())
        denied["allowed"] = False

        export = build_signal_export(denied, generated_at="2026-08-11T00:00:00+09:00")

        self.assertEqual(0, export["signal_count"])

    def test_unknown_major_schema_version_fails_closed(self):
        payload = build_research_signals(self.approved_result())
        payload["schema"] = "urn:self-model-notes:research-signals:v2"

        with self.assertRaisesRegex(ValueError, "Unsupported research_signals schema major version"):
            validate_research_signals(payload)

    def test_invalid_signal_without_evidence_is_rejected(self):
        payload = build_research_signals(self.approved_result())
        broken = copy.deepcopy(payload)
        broken["research_signals"]["tensions"][0]["evidence_refs"] = []

        errors = validate_research_signals(broken)

        self.assertTrue(any("non-empty list" in error for error in errors))

    def test_domain_and_scope_mappings_are_explicit_and_non_promoting(self):
        def signal(entity_ref, **updates):
            value = {
                "entity_ref": entity_ref,
                "kind": "claim",
                "layer": "motivation",
                "statement": entity_ref,
                "certainty": "medium",
                "motivation_direction": "unknown",
                "scope": None,
                "conditions": [],
                "evidence_refs": ["event/observation"],
            }
            value.update(updates)
            return value

        result = dict(self.approved_result())
        result["signals"] = [
            signal("claim/seek", motivation_direction="seek", scope="state"),
            signal("claim/protect", motivation_direction="protect", scope="trait"),
            signal("claim/avoid", motivation_direction="avoid", scope="context-bound", conditions=["studio"]),
            signal("claim/mixed", motivation_direction="mixed", scope="trait-candidate"),
            signal("claim/unknown", motivation_direction="unknown"),
            signal("claim/tension", layer="tension", motivation_direction=None, scope="state"),
            {
                "entity_ref": "pattern/repeat",
                "kind": "pattern",
                "layer": "pattern",
                "statement": "repeat",
                "certainty": "low",
                "scope": None,
                "contexts_seen": ["studio", "home"],
                "evidence_refs": ["event/observation"],
            },
        ]

        export = build_signal_export(result, generated_at="2026-08-11T00:00:00+09:00")
        by_id = {record["entity_id"]: record for record in export["signals"]}

        self.assertEqual(by_id["claim/seek"]["seeks"], ["claim/seek"])
        self.assertEqual(by_id["claim/seek"]["states"], ["claim/seek"])
        self.assertEqual(by_id["claim/protect"]["protects"], ["claim/protect"])
        self.assertEqual(by_id["claim/protect"]["traits"], ["claim/protect"])
        self.assertEqual(by_id["claim/avoid"]["avoids"], ["claim/avoid"])
        self.assertEqual(by_id["claim/avoid"]["contexts"], ["studio"])
        self.assertEqual(by_id["claim/mixed"]["seeks"], [])
        self.assertEqual(by_id["claim/mixed"]["protects"], [])
        self.assertEqual(by_id["claim/mixed"]["avoids"], [])
        self.assertIn("motivation direction is mixed", by_id["claim/mixed"]["unknowns"])
        self.assertIn("trait remains a candidate", by_id["claim/mixed"]["unknowns"])
        self.assertEqual(by_id["claim/unknown"]["seeks"], [])
        self.assertIn("motivation direction is unknown", by_id["claim/unknown"]["unknowns"])
        self.assertEqual(by_id["claim/tension"]["tensions"], ["claim/tension"])
        self.assertEqual(by_id["pattern/repeat"]["recurring_patterns"], ["repeat"])
        self.assertEqual(by_id["pattern/repeat"]["contexts"], ["home", "studio"])
        self.assertEqual(validate_signal_export(export), [])

    def test_rejected_and_superseded_entities_are_not_exported(self):
        entities = fixture_entities()
        next(entity for entity in entities if entity.id == "claim/observation").meta["status"] = "rejected"
        next(entity for entity in entities if entity.id == "claim/tension").meta["superseded_by"] = "claim/replacement"

        result = export_signals(
            entities,
            "subject/fixture",
            "artistic-research",
            today=date(2026, 8, 11),
            source_commit="a" * 40,
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["signals"], [])
        self.assertEqual(build_signal_export(result, generated_at="2026-08-11T00:00:00+09:00")["signal_count"], 0)

    def test_unknown_confidence_constraints_and_freshness_are_fixed(self):
        result = dict(self.approved_result())
        result["signals"] = [{
            "entity_ref": "claim/unknown",
            "kind": "claim",
            "layer": "other",
            "statement": "unknown confidence",
            "certainty": "unknown",
            "motivation_direction": None,
            "scope": "context-bound",
            "conditions": ["z-condition", "a-condition", "a-condition"],
            "evidence_refs": ["event/observation"],
        }]

        export = build_signal_export(result, generated_at="2026-08-11T00:00:00+09:00")
        record = export["signals"][0]
        self.assertEqual(record["certainty"], {
            "level": "unknown",
            "basis": "Derived Claim claim/unknown with confidence unknown.",
        })
        self.assertEqual(record["unknowns"], ["confidence is unknown"])
        self.assertEqual(record["constraints"], [
            "Condition: a-condition",
            "Condition: z-condition",
            "Use only for artistic-research under approved-derived-only consent.",
        ])
        self.assertEqual(record["validity"], {"status": "valid", "checked_at": record["generated_at"]})
        self.assertEqual(record["freshness"], {
            "status": "unknown",
            "retrieved_at": record["generated_at"],
            "revalidate_at": record["generated_at"],
        })

    def test_same_inputs_produce_byte_identical_json(self):
        result = self.approved_result()
        first = build_signal_export(result, generated_at="2026-08-11T00:00:00+09:00")
        second = build_signal_export(result, generated_at="2026-08-11T00:00:00+09:00")

        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_dirty_worktree_fails_closed_before_any_signal_is_shaped(self):
        from unittest.mock import patch

        with patch("tools.export_signals._worktree_is_dirty", return_value=True):
            result = export_signals(fixture_entities(), "subject/fixture", "artistic-research")

        self.assertFalse(result["allowed"])
        self.assertEqual(result["denials"][0]["rule"], "worktree.clean")
        self.assertNotIn("signals", result)

    def test_pinned_consumer_fixture_is_normalized_v1_input(self):
        fixture_path = Path(__file__).resolve().parent / "contracts" / "agentic-art-research-consumer-v1.fixture.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(fixture["contract_version"], "normalized-research-signal/v1")
        self.assertEqual(fixture["signal_kind"], "self")
        self.assertEqual(fixture["source"]["repository"], "self-model")
        self.assertEqual(fixture["evidence_refs"][0]["kind"], "derived")
        self.assertEqual(set(fixture["domain"]["self_model"]), {
            "consent_scope", "export_permitted", "seeks", "protects", "avoids", "tensions",
            "recurring_patterns", "raw_voice_locator", "traits", "states", "contexts",
        })
        serialized = json.dumps(fixture, ensure_ascii=False)
        self.assertNotIn("raw_voice_refs", serialized)
        self.assertNotIn("gdrive://", serialized)


class GrowthMissTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="growth-miss-")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve() / "profile"
        shutil.copytree(ROOT / "tests/fixtures/e2e/entities", self.profile / "entities")
        (self.profile / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-growth-miss\n"
            "subject_ids: [subject/fixture]\n"
            "storage_scope: external-local\n",
            encoding="utf-8",
        )

    def misses_path(self):
        return self.profile / "data" / "misses.jsonl"

    def read_misses(self):
        path = self.misses_path()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "tools/export_signals.py", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_group_misses_reports_empty_and_unknown(self):
        signals = [
            {"seeks": [], "protects": [], "avoids": ["x"], "tensions": [], "recurring_patterns": [],
             "contexts": [], "certainty": {"level": "unknown"}},
        ]
        misses = _group_misses(signals)

        self.assertEqual(misses["seeks"], ("empty", 0))
        self.assertEqual(misses["avoids"], ("unknown", 1))
        self.assertNotIn("traits", misses)
        self.assertNotIn("states", misses)

    def test_group_misses_absent_when_observed(self):
        signals = [{"avoids": ["x"], "certainty": {"level": "inferred"}}]

        misses = _group_misses(signals)

        self.assertNotIn("avoids", misses)

    def test_append_growth_miss_writes_one_jsonl_line(self):
        append_growth_miss(
            self.profile / "data",
            requester="run-1",
            subject="subject/fixture",
            purpose="artistic-research",
            section="avoids",
            reason="empty",
            evidence_count=0,
        )

        lines = self.read_misses()
        self.assertEqual(1, len(lines))
        record = lines[0]
        self.assertEqual(record["contract_version"], GROWTH_MISS_CONTRACT)
        self.assertEqual(record["requester"], "run-1")
        self.assertEqual(record["section"], "avoids")
        self.assertEqual(record["reason"], "empty")
        self.assertEqual(record["evidence_count"], 0)
        self.assertNotIn("synthetic", json.dumps(record, ensure_ascii=False))

    def test_cli_appends_misses_on_successful_export(self):
        result = self.cli(
            "--profile-root", str(self.profile), "--purpose", "artistic-research",
            "--requester", "run-a",
        )
        self.assertEqual(0, result.returncode, result.stderr)

        lines = self.read_misses()
        self.assertTrue(lines)
        for record in lines:
            self.assertEqual(record["requester"], "run-a")
            self.assertEqual(record["subject"], "subject/fixture")
            self.assertIn(record["section"], GROWTH_MISS_SECTIONS)
            self.assertIn(record["reason"], ("empty", "unknown"))

    def test_cli_two_runs_are_identical_except_timestamp(self):
        self.cli("--profile-root", str(self.profile), "--purpose", "artistic-research", "--requester", "run-a")
        first = self.read_misses()
        self.cli("--profile-root", str(self.profile), "--purpose", "artistic-research", "--requester", "run-a")
        second = self.read_misses()

        self.assertEqual(len(first) * 2, len(second))
        for before, after in zip(first, second[len(first):]):
            self.assertEqual({k: v for k, v in before.items() if k != "ts"}, {k: v for k, v in after.items() if k != "ts"})

    def test_cli_without_profile_root_writes_no_miss(self):
        result = subprocess.run(
            [sys.executable, "tools/export_signals.py", "--purpose", "artistic-research"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.misses_path().exists())

    def test_cli_denied_export_records_denied_miss(self):
        result = self.cli(
            "--profile-root", str(self.profile), "--purpose", "clinical-diagnosis",
            "--requester", "run-denied",
        )
        self.assertNotEqual(0, result.returncode)

        lines = self.read_misses()
        self.assertTrue(any(record["reason"] == "denied" and record["section"] == "all" for record in lines))


if __name__ == "__main__":
    unittest.main()
