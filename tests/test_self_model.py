import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_validation import make_entity, valid_entities
from tools.build_self_model import build_model, model_path, write_model


class SelfModelTests(unittest.TestCase):
    def entities_with_history(self):
        entities = valid_entities()
        entities.append(
            make_entity(
                "claim",
                "revised-control",
                statement="A revised explanation",
                supporting_evidence=["event/example-001"],
                status="revised",
                supersedes="claim/example-control",
            )
        )
        entities.append(
            make_entity(
                "claim",
                "rejected-control",
                statement="A rejected explanation",
                supporting_evidence=["event/example-002"],
                status="rejected",
            )
        )
        return entities

    def test_snapshot_records_metadata_and_evidence_for_each_derived_statement(self):
        model = build_model(self.entities_with_history(), "subject/example", source_commit="abc123")

        self.assertEqual(model["as_of"], "2026-08-11")
        self.assertEqual(model["source_commit"], "abc123")
        self.assertIn("claim/revised-control", model["derived_from"])
        self.assertEqual(
            {claim["status"] for claim in model["claim_history"]},
            {"hypothesis", "revised", "rejected"},
        )
        self.assertTrue(any(claim["supersedes"] == "claim/example-control" for claim in model["claim_history"]))

        statements = [
            statement
            for section in ("emotions", "motivations", "behavioral_principles", "tensions", "patterns")
            for statement in model[section]
        ]
        self.assertTrue(statements)
        self.assertTrue(all(statement["evidence_refs"] for statement in statements))
        self.assertEqual(model["evidence_coverage"]["event_refs"], ["event/example-001", "event/example-002"])

    def test_unknown_confidence_is_preserved_as_an_unknown(self):
        entities = valid_entities()
        entities.append(
            make_entity(
                "claim",
                "unknown-confidence",
                confidence="unknown",
                supporting_evidence=["event/example-001"],
            )
        )

        model = build_model(entities, "subject/example", source_commit="abc123")

        self.assertEqual(model["unknowns"][0]["entity_ref"], "claim/unknown-confidence")
        self.assertEqual(model["unknowns"][0]["evidence_refs"], ["event/example-001"])

    def test_snapshot_is_deterministic_for_entity_order(self):
        entities = self.entities_with_history()

        first = build_model(entities, "subject/example", source_commit="abc123")
        second = build_model(list(reversed(entities)), "subject/example", source_commit="abc123")

        self.assertEqual(first, second)

    def test_snapshot_writer_uses_canonical_json(self):
        model = build_model(valid_entities(), "subject/example", source_commit="abc123")
        with TemporaryDirectory() as directory:
            path = model_path("subject/example", Path(directory))
            write_model(model, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), model)
            self.assertEqual(path, model_path("subject/example", Path(directory)))


if __name__ == "__main__":
    unittest.main()
