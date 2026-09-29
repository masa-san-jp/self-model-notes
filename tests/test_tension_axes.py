import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.test_validation import make_entity
from tools.audit import claim_consent_status, findings, tension_axis_summary
from tools.growth_tasks import generate
from tools.kb import Entity, discover_entities, serialize_markdown


PLURAL = {
    "subject": "subjects",
    "source": "sources",
    "event": "events",
    "claim": "claims",
    "pattern": "patterns",
    "measurement": "measurements",
}


class TensionAxisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tension-axis-")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve() / "profile"
        for kind in PLURAL.values():
            (self.profile / "entities" / kind).mkdir(parents=True)
        (self.profile / "profile.yaml").write_text(
            "contract_version: self-model-profile/v1\n"
            "profile_id: synthetic-tension-axis\n"
            "subject_ids: [subject/example]\n"
            "storage_scope: external-local\n",
            encoding="utf-8",
        )
        self.write(make_entity("subject", "example"))

    def write(self, entity: Entity) -> None:
        path = self.profile / "entities" / PLURAL[entity.type] / f"{entity.id.split('/', 1)[1]}.md"
        path.write_text(serialize_markdown(replace(entity, path=path)), encoding="utf-8")

    def consented_source(self, slug="interview-20260901", **updates):
        consent = {
            "obtained": True,
            "obtained_at": "2026-09-01",
            "purposes": ["self-reflection", "research", "artistic-research"],
            "allowed_operations": ["store-reference", "analyze", "derive", "bundle", "export-signals"],
            "expires_at": None,
            "revoked_at": None,
            "notes": None,
        }
        consent.update(updates.pop("consent", {}))
        source = make_entity(
            "source", slug, subject="subject/example", source_kind="interview", consent=consent, **updates
        )
        self.write(source)
        return source

    def tension_claim(self, slug, event_id, axis=None):
        updates = {
            "subject": "subject/example",
            "layer": "tension",
            "motivation_direction": None,
            "scope": "state",
            "status": "supported",
            "supporting_evidence": [event_id],
            "counterevidence": [event_id],
        }
        if axis is not None:
            updates["axis"] = axis
        claim = make_entity("claim", slug, **updates)
        self.write(claim)
        return claim

    def test_axis_counts_are_explicit_and_unclassified_is_not_inferred(self):
        entities = [make_entity("subject", "example")]
        event = make_entity("event", "e1", subject="subject/example", source_refs=[])
        entities.append(event)
        entities.append(self.tension_claim("not-written", "event/e1", axis=None))

        # Use in-memory entities so the assertion is independent of profile I/O.
        claim = make_entity(
            "claim", "written", subject="subject/example", layer="tension", motivation_direction=None,
            status="supported", supporting_evidence=["event/e1"], counterevidence=["event/e1"], axis="proximity",
        )
        invalid = make_entity(
            "claim", "invalid", subject="subject/example", layer="tension", motivation_direction=None,
            status="supported", supporting_evidence=["event/e1"], counterevidence=["event/e1"], axis="not-an-axis",
        )
        summary = tension_axis_summary([*entities, claim, invalid], "subject/example")

        self.assertEqual({"proximity": 1, "unclassified": 2}, summary["axis_counts"])
        self.assertEqual(1, summary["distinct_axes"])
        self.assertEqual(["claim/invalid"], summary["invalid_claims"])
        self.assertFalse(summary["biased"])

    def test_bias_finding_and_growth_task_ask_for_a_different_axis(self):
        self.consented_source()
        for index in (1, 2):
            event_id = f"event/tension-{index}"
            self.write(make_entity("event", f"tension-{index}", subject="subject/example", source_refs=["source/interview-20260901"]))
            self.tension_claim(f"tension-{index}", event_id, axis="proximity")

        entities = discover_entities(self.profile / "entities")
        summary = tension_axis_summary(entities, "subject/example")
        self.assertTrue(summary["biased"])
        codes = {finding["code"] for finding in findings(entities, "subject/example")}
        self.assertIn("TENSION_AXIS_BIAS", codes)

        queue = generate(self.profile)
        task = next(task for task in queue["tasks"] if task["gap_key"].startswith("audit:TENSION_AXIS_BIAS:"))
        self.assertEqual("acquire-event", task["kind"])
        self.assertEqual("tensions", task["target"]["section"])
        self.assertEqual("tension-axis-diversity", task["question_id"])
        self.assertNotIn("proximity", task["question"])

    def test_claim_consent_follows_event_source_refs_and_fails_closed(self):
        source = make_entity(
            "source", "consented", subject="subject/example", source_kind="interview",
            consent={
                "obtained": True, "obtained_at": "2026-09-01",
                "purposes": ["artistic-research"],
                "allowed_operations": ["export-signals"],
                "expires_at": None, "revoked_at": None, "notes": None,
            },
        )
        event = make_entity("event", "e1", subject="subject/example", source_refs=["source/consented"])
        claim = make_entity(
            "claim", "tension", subject="subject/example", layer="tension", motivation_direction=None,
            supporting_evidence=["event/e1"], counterevidence=["event/e1"],
        )
        allowed = claim_consent_status(
            claim, [make_entity("subject", "example"), source, event], today=date(2026, 9, 30)
        )
        self.assertTrue(allowed["allowed"])
        self.assertEqual(["source/consented"], allowed["source_refs"])

        source.meta["consent"]["revoked_at"] = "2026-09-29"
        denied = claim_consent_status(
            claim, [make_entity("subject", "example"), source, event], today=date(2026, 9, 30)
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual({"consent.revoked_at"}, {item["rule"] for item in denied["denials"]})


if __name__ == "__main__":
    unittest.main()
