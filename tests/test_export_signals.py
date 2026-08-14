import copy
import json
import unittest
from datetime import date
from pathlib import Path

from tests.test_consent import fixture_entities
from tools.export_signals import (
    build_research_signals,
    export_signals,
    validate_research_signals,
)


SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "research-signals-v1.schema.json"


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
        payload = build_research_signals(
            self.approved_result(),
            generated_at="2026-08-14T10:00:00Z",
        )
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema["$id"], payload["contract_version"])
        self.assertEqual(validate_research_signals(payload), [])
        self.assertEqual(payload["source_repository"], "self-model")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertEqual(payload["generated_at"], "2026-08-14T10:00:00Z")
        self.assertEqual(payload["signal_count"], len(payload["signals"]))
        self.assertEqual(payload["signals"][0]["evidence_refs"], ["event/observation"])
        self.assertNotIn("synthetic voice content", json.dumps(payload, ensure_ascii=False))

    def test_every_signal_item_has_certainty_and_evidence_refs(self):
        payload = build_research_signals(self.approved_result())

        for item in payload["signals"]:
            self.assertIn(item["certainty"], {"unknown", "low", "medium", "high"})
            self.assertTrue(item["evidence_refs"])

    def test_unknown_major_schema_version_fails_closed(self):
        payload = build_research_signals(self.approved_result())
        payload["contract_version"] = "research-signal-export/v2"

        with self.assertRaisesRegex(ValueError, "Unsupported research-signal-export contract version"):
            validate_research_signals(payload)

    def test_invalid_signal_without_evidence_is_rejected(self):
        payload = build_research_signals(self.approved_result())
        broken = copy.deepcopy(payload)
        broken["signals"][0]["evidence_refs"] = []

        errors = validate_research_signals(broken)

        self.assertTrue(any("non-empty list" in error for error in errors))

    def test_signal_count_mismatch_is_rejected(self):
        payload = build_research_signals(self.approved_result())
        payload["signal_count"] += 1

        errors = validate_research_signals(payload)

        self.assertIn("signal_count must equal the number of signals", errors)

    def test_source_repository_must_use_boundary_identifier(self):
        payload = build_research_signals(self.approved_result())
        payload["source_repository"] = "masa-san-jp/self-model-notes"

        errors = validate_research_signals(payload)

        self.assertIn("source_repository must be 'self-model'", errors)

    def test_all_subject_export_and_limit(self):
        result = export_signals(
            fixture_entities(),
            None,
            "artistic-research",
            operation="export-signals",
            today=date(2026, 8, 11),
            source_commit="a" * 40,
        )

        payload = build_research_signals(result, generated_at="2026-08-14T10:00:00Z", limit=1)

        self.assertTrue(result["allowed"])
        self.assertEqual(result["subjects"], ["subject/fixture"])
        self.assertEqual(payload["signal_count"], 1)
        self.assertEqual(len(payload["signals"]), 1)


if __name__ == "__main__":
    unittest.main()
