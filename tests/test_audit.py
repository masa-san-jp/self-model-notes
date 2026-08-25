import tempfile
import unittest
from pathlib import Path

from tests.test_validation import make_entity
from tools.audit import audit_artifact_is_current, audit_json, audit_report, findings
from tools.kb import validate_entities


def audit_entities():
    subject = make_entity("subject", "example")
    source = make_entity("source", "interview-001")
    event_one = make_entity("event", "example-001")
    event_two = make_entity("event", "example-002")
    event_two.meta["time"]["observed_at"] = "2026-08-12T10:05:00+09:00"
    claim = make_entity(
        "claim",
        "trait-candidate",
        scope="trait-candidate",
        supporting_evidence=["event/example-001", "event/example-002"],
        counterevidence=[],
    )
    pattern = make_entity(
        "pattern",
        "single-context",
        evidence=["event/example-001", "event/example-002"],
        claim_refs=["claim/trait-candidate"],
        counterevidence=[],
    )
    return [subject, source, event_one, event_two, claim, pattern]


class AuditTests(unittest.TestCase):
    def test_audit_reports_source_context_counterevidence_and_trait_breadth(self):
        result = findings(audit_entities(), "subject/example")
        codes = {finding["code"] for finding in result}

        self.assertIn("SOURCE_CONCENTRATION", codes)
        self.assertIn("CONTEXT_BIAS", codes)
        self.assertIn("NO_COUNTEREVIDENCE", codes)
        self.assertIn("TRAIT_BREADTH_REVIEW", codes)

    def test_soft_findings_do_not_break_structural_validation(self):
        entities = audit_entities()

        self.assertEqual(validate_entities(entities), [])
        self.assertTrue(all(finding["severity"] == "review" for finding in findings(entities)))

    def test_findings_propose_observations_without_diagnostic_conclusions(self):
        report = audit_report(audit_entities(), "subject/example")

        self.assertIn("observations", report["policy"])
        self.assertTrue(all("diagnos" not in finding["next_check"].lower() for finding in report["findings"]))
        self.assertTrue(all("next_check" in finding for finding in report["findings"]))

    def test_empty_repository_has_a_stable_empty_report(self):
        self.assertEqual(
            audit_report([], None),
            {
                "schema_version": 1,
                "subject": None,
                "policy": "soft review; findings propose observations and do not make diagnostic conclusions",
                "findings": [],
            },
        )

    def test_audit_artifact_check_distinguishes_current_stale_and_missing(self):
        expected = audit_json([], None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"

            path.write_text(expected, encoding="utf-8")
            self.assertTrue(audit_artifact_is_current(path, expected))

            path.write_text("stale\n", encoding="utf-8")
            self.assertFalse(audit_artifact_is_current(path, expected))
            self.assertEqual("stale\n", path.read_text(encoding="utf-8"))

            path.unlink()
            self.assertFalse(audit_artifact_is_current(path, expected))


if __name__ == "__main__":
    unittest.main()
