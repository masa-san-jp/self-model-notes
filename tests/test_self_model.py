import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_validation import make_entity, valid_entities
from tools.bundle import render_bundle
from tools.build_self_model import build_model, model_path, write_model
from tools.kb import discover_entities


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "tests" / "contracts" / "self-model-v2.schema.json").read_text(encoding="utf-8"))


def assert_schema(test_case, value, schema, *, path="$", root=SCHEMA):
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].split("/")[1:]:
            target = target[part]
        assert_schema(test_case, value, target, path=path, root=root)
        return

    for subschema in schema.get("allOf", []):
        assert_schema(test_case, value, subschema, path=path, root=root)

    if "const" in schema:
        test_case.assertEqual(value, schema["const"], path)
    if "enum" in schema:
        test_case.assertIn(value, schema["enum"], path)

    expected_types = schema.get("type")
    if expected_types is not None:
        if not isinstance(expected_types, list):
            expected_types = [expected_types]

        def matches(expected_type):
            if expected_type == "null":
                return value is None
            if expected_type == "object":
                return isinstance(value, dict)
            if expected_type == "array":
                return isinstance(value, list)
            if expected_type == "string":
                return isinstance(value, str)
            if expected_type == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            if expected_type == "boolean":
                return isinstance(value, bool)
            return True

        test_case.assertTrue(any(matches(expected_type) for expected_type in expected_types), path)

    if isinstance(value, str):
        if "minLength" in schema:
            test_case.assertGreaterEqual(len(value), schema["minLength"], path)
        if "pattern" in schema:
            test_case.assertRegex(value, re.compile(schema["pattern"]), path)
    if isinstance(value, int) and "minimum" in schema:
        test_case.assertGreaterEqual(value, schema["minimum"], path)
    if isinstance(value, list):
        if "minItems" in schema:
            test_case.assertGreaterEqual(len(value), schema["minItems"], path)
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
            test_case.assertEqual(len(encoded), len(set(encoded)), path)
        if "items" in schema:
            for index, item in enumerate(value):
                assert_schema(test_case, item, schema["items"], path=f"{path}[{index}]", root=root)
    if isinstance(value, dict):
        for field in schema.get("required", []):
            test_case.assertIn(field, value, f"{path}.{field}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            test_case.assertEqual(set(value) - set(properties), set(), path)
        for field, field_schema in properties.items():
            if field in value:
                assert_schema(test_case, value[field], field_schema, path=f"{path}.{field}", root=root)


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
        self.assertEqual(render_bundle(first), render_bundle(second))

    def test_snapshot_writer_uses_canonical_json(self):
        model = build_model(valid_entities(), "subject/example", source_commit="abc123")
        with TemporaryDirectory() as directory:
            path = model_path("subject/example", Path(directory))
            write_model(model, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), model)
            self.assertEqual(path, model_path("subject/example", Path(directory)))

    def test_generated_model_matches_v2_schema(self):
        model = build_model(self.entities_with_history(), "subject/example", source_commit="abc123")

        assert_schema(self, model, SCHEMA)
        self.assertEqual(model["schema_version"], 2)
        self.assertEqual(set(model), set(SCHEMA["required"]))

    def test_current_sections_use_explicit_direction_and_keep_history(self):
        entities = valid_entities()
        entities.extend(
            [
                make_entity(
                    "claim",
                    "seek-control",
                    statement="seek statement",
                    motivation_direction="seek",
                    supporting_evidence=["event/example-001"],
                ),
                make_entity(
                    "claim",
                    "avoid-control",
                    statement="avoid statement",
                    motivation_direction="avoid",
                    scope="context-bound",
                    supporting_evidence=["event/example-001"],
                ),
                make_entity(
                    "claim",
                    "protect-control",
                    statement="protect statement",
                    motivation_direction="protect",
                    supporting_evidence=["event/example-002"],
                ),
                make_entity(
                    "claim",
                    "mixed-control",
                    statement="mixed statement",
                    motivation_direction="mixed",
                    supporting_evidence=["event/example-002"],
                ),
                make_entity(
                    "claim",
                    "rejected-avoid",
                    statement="rejected statement",
                    motivation_direction="avoid",
                    status="rejected",
                    supporting_evidence=["event/example-001"],
                ),
                make_entity(
                    "claim",
                    "superseded-protect",
                    statement="superseded statement",
                    motivation_direction="protect",
                    superseded_by="claim/protect-control",
                    supporting_evidence=["event/example-001"],
                ),
            ]
        )

        model = build_model(entities, "subject/example", source_commit="abc123")
        current_ids = {
            statement["entity_ref"]
            for section in ("motivations", "avoidance_targets", "protective_factors")
            for statement in model[section]
        }

        self.assertIn("claim/seek-control", current_ids)
        self.assertIn("claim/mixed-control", current_ids)
        self.assertEqual([item["entity_ref"] for item in model["avoidance_targets"]], ["claim/avoid-control"])
        self.assertEqual([item["entity_ref"] for item in model["protective_factors"]], ["claim/protect-control"])
        self.assertNotIn("claim/rejected-avoid", current_ids)
        self.assertNotIn("claim/superseded-protect", current_ids)
        history_ids = {claim["entity_ref"] for claim in model["claim_history"]}
        self.assertIn("claim/rejected-avoid", history_ids)
        self.assertIn("claim/superseded-protect", history_ids)
        self.assertTrue(any(item["kind"] == "motivation-direction-unresolved" and item["entity_ref"] == "claim/mixed-control" for item in model["unknowns"]))
        self.assertTrue(model["dominant_triggers"])
        self.assertTrue(model["dominant_rewards"])
        self.assertTrue(any(item["source_field"] == "conditions" for item in model["context_dependencies"]))

    def test_observation_preserves_null_and_empty_fields_without_raw_voice_body(self):
        entities = valid_entities()
        event = next(entity for entity in entities if entity.id == "event/example-001")
        event.meta.update(
            {
                "observed_facts": None,
                "appraisal": [],
                "emotion": None,
                "body": [],
                "cognition": None,
                "action": [],
                "immediate_outcome": None,
                "delayed_outcome": [],
                "raw_voice": [{"text": "synthetic voice", "source_ref": "source/interview-001"}],
            }
        )

        model = build_model(entities, "subject/example", source_commit="abc123")
        observation = next(item for item in model["observations"] if item["entity_ref"] == "event/example-001")

        self.assertIsNone(observation["observed_facts"])
        self.assertEqual(observation["appraisal"], [])
        self.assertIsNone(observation["emotion"])
        self.assertEqual(observation["body"], [])
        self.assertIsNone(observation["cognition"])
        self.assertEqual(observation["actions"], [])
        self.assertIsNone(observation["immediate_outcomes"])
        self.assertEqual(observation["delayed_outcomes"], [])
        self.assertEqual(observation["raw_voice_refs"], ["source/interview-001"])
        self.assertNotIn("synthetic voice", json.dumps(model, ensure_ascii=False))

    def test_real_unknown_direction_does_not_fill_specialized_sections(self):
        model = build_model(discover_entities(), "subject/masa", source_commit="abc123")

        self.assertEqual(model["dominant_triggers"], [])
        self.assertEqual(model["dominant_rewards"], [])
        self.assertEqual(model["avoidance_targets"], [])
        self.assertEqual(model["protective_factors"], [])
        self.assertTrue(
            any(
                item["kind"] == "motivation-direction-unresolved"
                and item["entity_ref"] == "claim/proximity-drives-attention"
                for item in model["unknowns"]
            )
        )


if __name__ == "__main__":
    unittest.main()
