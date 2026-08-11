import copy
import json
import unittest
from datetime import date
from pathlib import Path

from tools.export_signals import export_signals
from tools.kb import parse_markdown


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "consent" / "valid" / "entities"


def fixture_entities():
    return [parse_markdown(path) for path in sorted(FIXTURE_ROOT.glob("*/*.md"))]


def source(entities, source_id):
    return next(entity for entity in entities if entity.id == source_id)


class ConsentTests(unittest.TestCase):
    def test_valid_export_checks_all_sources_and_excludes_raw_voice(self):
        result = export_signals(
            fixture_entities(),
            "subject/fixture",
            "artistic-research",
            operation="export-signals",
            today=date(2026, 8, 11),
            source_commit="abc123",
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["source_commit"], "abc123")
        self.assertEqual(result["signals"][0]["evidence_refs"], ["event/observation"])
        self.assertNotIn("raw_voice", result)
        self.assertNotIn("synthetic voice content", json.dumps(result, ensure_ascii=False))

    def test_missing_consent_denies_without_leaking_source_content(self):
        entities = fixture_entities()
        del source(entities, "source/evidence").meta["consent"]

        result = export_signals(entities, "subject/fixture", "artistic-research", today=date(2026, 8, 11))

        self.assertFalse(result["allowed"])
        self.assertTrue(any(denial["source"] == "source/evidence" and denial["rule"] == "consent.present" for denial in result["denials"]))
        self.assertNotIn("evidence-fixture", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("synthetic voice content", json.dumps(result, ensure_ascii=False))

    def test_revoked_and_expired_consent_deny(self):
        revoked = fixture_entities()
        source(revoked, "source/evidence").meta["consent"]["revoked_at"] = "2026-08-10"
        revoked_result = export_signals(revoked, "subject/fixture", "artistic-research", today=date(2026, 8, 11))

        expired = fixture_entities()
        source(expired, "source/evidence").meta["consent"]["expires_at"] = "2026-08-10"
        expired_result = export_signals(expired, "subject/fixture", "artistic-research", today=date(2026, 8, 11))

        self.assertFalse(revoked_result["allowed"])
        self.assertTrue(any(denial["rule"] == "consent.revoked_at" for denial in revoked_result["denials"]))
        self.assertFalse(expired_result["allowed"])
        self.assertTrue(any(denial["rule"] == "consent.expires_at" for denial in expired_result["denials"]))

    def test_purpose_and_operation_must_be_allowed_by_every_source(self):
        purpose_entities = fixture_entities()
        source(purpose_entities, "source/evidence").meta["consent"]["purposes"] = ["self-reflection"]
        purpose_result = export_signals(purpose_entities, "subject/fixture", "artistic-research", today=date(2026, 8, 11))

        operation_entities = copy.deepcopy(fixture_entities())
        source(operation_entities, "source/evidence").meta["consent"]["allowed_operations"] = ["analyze"]
        operation_result = export_signals(operation_entities, "subject/fixture", "artistic-research", today=date(2026, 8, 11))

        self.assertFalse(purpose_result["allowed"])
        self.assertTrue(any(denial["rule"] == "consent.purposes" for denial in purpose_result["denials"]))
        self.assertFalse(operation_result["allowed"])
        self.assertTrue(any(denial["rule"] == "consent.allowed_operations" for denial in operation_result["denials"]))

    def test_missing_subject_fails_closed(self):
        result = export_signals(fixture_entities(), "subject/missing", "artistic-research", today=date(2026, 8, 11))

        self.assertFalse(result["allowed"])
        self.assertEqual(result["denials"][0]["rule"], "subject.exists")


if __name__ == "__main__":
    unittest.main()
