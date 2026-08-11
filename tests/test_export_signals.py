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
        payload = build_research_signals(self.approved_result())
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema["$id"], payload["schema"])
        self.assertEqual(validate_research_signals(payload), [])
        self.assertEqual(payload["source_repository"], "masa-san-jp/self-model-notes")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertEqual(payload["as_of"], "2026-08-02")
        self.assertEqual(payload["research_signals"]["seeks"][0]["evidence_refs"], ["event/observation"])
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

    def test_unknown_major_schema_version_fails_closed(self):
        payload = build_research_signals(self.approved_result())
        payload["schema"] = "urn:self-model-notes:research-signals:v2"

        with self.assertRaisesRegex(ValueError, "Unsupported research_signals schema major version"):
            validate_research_signals(payload)

    def test_invalid_signal_without_evidence_is_rejected(self):
        payload = build_research_signals(self.approved_result())
        broken = copy.deepcopy(payload)
        broken["research_signals"]["seeks"][0]["evidence_refs"] = []

        errors = validate_research_signals(broken)

        self.assertTrue(any("non-empty list" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
