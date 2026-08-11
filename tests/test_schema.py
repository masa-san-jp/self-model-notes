import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schema.md"
VOCABULARY_PATH = ROOT / "config" / "vocabularies.yaml"

ENTITY_HEADINGS = {
    "subject": "Subject",
    "source": "Source",
    "event": "Event",
    "claim": "Claim",
    "pattern": "Pattern",
    "measurement": "Measurement",
}

REQUIRED_FIELDS = {
    "subject": {
        "id", "type", "pseudonym", "direct_identifiers_stored", "consent_refs",
        "allowed_purposes", "prohibited_purposes", "status", "created", "updated",
    },
    "source": {
        "id", "type", "subject", "source_kind", "captured_at", "locator",
        "raw_content_stored", "consent", "reliability_notes", "created", "updated",
    },
    "event": {
        "id", "type", "subject", "time", "context", "state", "trigger",
        "observed_facts", "raw_voice", "appraisal", "emotion", "body", "cognition",
        "action", "immediate_outcome", "delayed_outcome", "source_refs", "created", "updated",
    },
    "claim": {
        "id", "type", "subject", "layer", "scope", "statement", "conditions",
        "supporting_evidence", "counterevidence", "alternative_explanations", "confidence",
        "status", "supersedes", "superseded_by", "created", "updated",
    },
    "pattern": {
        "id", "type", "subject", "condition", "recurring_appraisal", "recurring_drive",
        "recurring_action", "reinforcement", "contexts_seen", "evidence", "claim_refs",
        "counterevidence", "confidence", "status", "created", "updated",
    },
    "measurement": {
        "id", "type", "subject", "instrument", "administered_at", "source_ref",
        "scores", "interpretation_claim_refs", "created", "updated",
    },
}


def section_for(document, heading):
    match = re.search(
        rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
        document,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing schema section: {heading}")
    return match.group("body")


def yaml_blocks(document):
    return re.findall(r"```yaml\n(.*?)```", document, flags=re.DOTALL)


class SchemaExamplesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
        cls.vocabularies = yaml.safe_load(VOCABULARY_PATH.read_text(encoding="utf-8"))

    def examples_for(self, entity_type):
        section = section_for(self.schema_text, ENTITY_HEADINGS[entity_type])
        valid_text, invalid_text = section.split("### Invalid example", maxsplit=1)
        valid = yaml.safe_load(yaml_blocks(valid_text)[0])
        invalid = yaml.safe_load(yaml_blocks(invalid_text)[0])
        return valid, invalid

    def test_six_types_have_complete_valid_and_invalid_examples(self):
        for entity_type, required_fields in REQUIRED_FIELDS.items():
            with self.subTest(entity_type=entity_type):
                valid, invalid = self.examples_for(entity_type)
                self.assertEqual(valid["type"], entity_type)
                self.assertEqual(invalid["type"], entity_type)
                self.assertTrue(required_fields <= valid.keys())
                self.assertTrue(required_fields <= invalid.keys())

    def test_id_path_and_reference_rules_are_explicit(self):
        plural_paths = self.vocabularies["plural_paths"]
        slug_pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

        for entity_type, plural_path in plural_paths.items():
            with self.subTest(entity_type=entity_type):
                valid, _ = self.examples_for(entity_type)
                prefix, slug = valid["id"].split("/", maxsplit=1)
                self.assertEqual(prefix, entity_type)
                self.assertRegex(slug, slug_pattern)
                self.assertIn(
                    f"| {entity_type} | `{entity_type}/` | `entities/{plural_path}/<slug>.md` |",
                    self.schema_text,
                )

        self.assertIn("参照先は存在するentity IDでなければならず", self.schema_text)
        self.assertIn("subject不一致", self.schema_text)

    def test_unknown_state_context_and_trait_semantics_are_testable(self):
        event, _ = self.examples_for("event")
        self.assertIsNone(event["state"]["fatigue"])
        self.assertEqual(event["context"]["uncertainty"], "unknown")
        self.assertEqual(event["context"]["control"], "unknown")

        self.assertIn("`null`は不明、`[]`は確認したが該当なし", self.schema_text)
        self.assertIn("`unknown`は汎用の欠損値ではない", self.schema_text)
        self.assertIn("`scope: state`は「現在の状態に依存するClaim」", self.schema_text)
        self.assertIn("`scope: context-bound`は「特定Contextでのみ検討するClaim」", self.schema_text)
        self.assertIn("`scope: trait-candidate`と`scope: trait`の差", self.schema_text)
        self.assertIn("初期実装は自動昇格させない", self.schema_text)

    def test_invalid_examples_encode_the_rejection_reason(self):
        subject, subject_invalid = self.examples_for("subject")
        self.assertNotRegex(subject_invalid["id"].split("/", maxsplit=1)[1], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

        source, source_invalid = self.examples_for("source")
        self.assertTrue(source["subject"].startswith("subject/"))
        self.assertTrue(source_invalid["subject"].startswith("event/"))

        event, event_invalid = self.examples_for("event")
        self.assertIsNone(event["state"]["fatigue"])
        self.assertEqual(event_invalid["state"]["fatigue"], "maybe")

        claim, claim_invalid = self.examples_for("claim")
        self.assertGreaterEqual(len(claim["alternative_explanations"]), 2)
        self.assertEqual(claim_invalid["supporting_evidence"], [])
        self.assertEqual(len(claim_invalid["alternative_explanations"]), 1)

        pattern, pattern_invalid = self.examples_for("pattern")
        self.assertGreaterEqual(len(pattern["evidence"]), 2)
        self.assertEqual(len(pattern_invalid["evidence"]), 1)

        measurement, measurement_invalid = self.examples_for("measurement")
        self.assertTrue(measurement["instrument"]["official"])
        self.assertFalse(measurement_invalid["instrument"]["official"])
        self.assertTrue(any(isinstance(value, (int, float)) for value in measurement_invalid["scores"].values()))

    def test_schema_has_no_unresolved_decision_marker(self):
        for marker in ("TBD", "TODO", "未決定"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.schema_text)


if __name__ == "__main__":
    unittest.main()
