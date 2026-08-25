import unittest
from dataclasses import replace
from pathlib import Path

from tools.kb import Entity, validate_entities


ROOT = Path(__file__).resolve().parents[1]


def make_entity(entity_type, slug, **updates):
    plural = {
        "subject": "subjects",
        "source": "sources",
        "event": "events",
        "claim": "claims",
        "pattern": "patterns",
        "measurement": "measurements",
    }[entity_type]
    meta = {
        "id": f"{entity_type}/{slug}",
        "type": entity_type,
        "created": "2026-08-11",
        "updated": "2026-08-11",
    }
    templates = {
        "subject": {
            "pseudonym": "S-001",
            "direct_identifiers_stored": False,
            "consent_refs": [],
            "allowed_purposes": ["self-reflection"],
            "prohibited_purposes": ["clinical-diagnosis"],
            "status": "active",
        },
        "source": {
            "subject": "subject/example",
            "source_kind": "interview",
            "captured_at": "2026-08-11T10:00:00+09:00",
            "locator": "gdrive://opaque-locator",
            "raw_content_stored": False,
            "consent": {
                "obtained": True,
                "obtained_at": "2026-08-11",
                "purposes": ["self-reflection"],
                "allowed_operations": ["analyze"],
                "expires_at": None,
                "revoked_at": None,
                "notes": None,
            },
            "reliability_notes": None,
        },
        "event": {
            "subject": "subject/example",
            "time": {"observed_at": "2026-08-11T10:05:00+09:00", "precision": "minute"},
            "context": {"domains": ["work"], "social": ["alone"], "uncertainty": "unknown", "control": "unknown"},
            "state": {"fatigue": None, "stress": "unknown"},
            "trigger": "期限が外部から変更された",
            "observed_facts": ["本人が作業順序を組み替えた"],
            "raw_voice": [],
            "appraisal": [],
            "emotion": [],
            "body": [],
            "cognition": [],
            "action": ["作業計画を再設計した"],
            "immediate_outcome": ["作業を再開した"],
            "delayed_outcome": [],
            "source_refs": ["source/interview-001"],
        },
        "claim": {
            "subject": "subject/example",
            "layer": "motivation",
            "scope": "state",
            "statement": "外部制約時に制御感の回復を求める可能性がある",
            "conditions": ["自律を制限されたと知覚したとき"],
            "supporting_evidence": ["event/example-001"],
            "counterevidence": [],
            "alternative_explanations": ["締切順へ最適化した", "説明責任を優先した"],
            "confidence": "low",
            "status": "hypothesis",
            "supersedes": None,
            "superseded_by": None,
        },
        "pattern": {
            "subject": "subject/example",
            "condition": "外部から意思決定を制限されたと知覚する",
            "recurring_appraisal": ["選択可能性が失われた"],
            "recurring_drive": ["D5"],
            "recurring_action": ["決定可能な構造へ再設計する"],
            "reinforcement": ["制御感の回復"],
            "contexts_seen": ["work"],
            "evidence": ["event/example-001", "event/example-002"],
            "claim_refs": ["claim/example-control"],
            "counterevidence": [],
            "confidence": "low",
            "status": "hypothesis",
        },
        "measurement": {
            "subject": "subject/example",
            "instrument": {"name": "formal-scale", "version": "1", "official": True, "scoring_reference": "gdrive://scoring"},
            "administered_at": "2026-08-11T10:00:00+09:00",
            "source_ref": "source/formal-measurement-001",
            "scores": {},
            "interpretation_claim_refs": [],
        },
    }
    meta.update(templates[entity_type])
    meta.update(updates)
    if entity_type == "claim" and "motivation_direction" not in meta:
        meta["motivation_direction"] = "unknown" if meta["layer"] == "motivation" else None
    path = ROOT / "entities" / plural / f"{slug}.md"
    return Entity(path=path, meta=meta, body="\n")


def valid_entities():
    return [
        make_entity("subject", "example"),
        make_entity("source", "interview-001"),
        make_entity("source", "formal-measurement-001", source_kind="formal-measurement"),
        make_entity("event", "example-001"),
        make_entity("event", "example-002"),
        make_entity("claim", "example-control"),
        make_entity("pattern", "example-control-recovery"),
        make_entity("measurement", "example-001"),
    ]


class ValidationTest(unittest.TestCase):
    def test_complete_entities_pass_structural_validation(self):
        self.assertEqual(validate_entities(valid_entities()), [])

    def test_missing_field_error_has_file_field_and_remediation(self):
        subject = make_entity("subject", "example")
        del subject.meta["pseudonym"]

        errors = validate_entities([subject])

        error = next(error for error in errors if error.field == "pseudonym")
        rendered = str(error)
        self.assertIn("entities/subjects/example.md", rendered)
        self.assertIn("pseudonym", rendered)
        self.assertIn("add `pseudonym`", rendered)

    def test_id_path_type_and_vocabulary_are_validated(self):
        wrong_path = make_entity("source", "right")
        wrong_path = replace(wrong_path, path=ROOT / "entities" / "sources" / "wrong.md")
        wrong_type = make_entity("event", "wrong-type")
        wrong_type.meta["id"] = "source/wrong-type"
        invalid_vocab = make_entity("subject", "invalid-vocabulary", allowed_purposes=["not-a-purpose"])

        errors = validate_entities([wrong_path, wrong_type, invalid_vocab])

        self.assertTrue(any(error.field == "id" and "ID/path mismatch" in error.message for error in errors))
        self.assertTrue(any(error.field == "id" and "prefix" in error.message for error in errors))
        self.assertTrue(any(error.field == "allowed_purposes" and "closed vocabulary" in error.message for error in errors))

    def test_reference_target_type_missing_target_and_subject_mismatch_fail(self):
        subject_a = make_entity("subject", "a")
        subject_b = make_entity("subject", "b")
        source_b = make_entity("source", "source-b", subject="subject/b")
        wrong_type = make_entity("event", "wrong-ref", subject="subject/a", source_refs=["subject/a"])
        missing = make_entity("event", "missing-ref", subject="subject/a", source_refs=["source/missing"])
        mismatch = make_entity("event", "mismatch", subject="subject/a", source_refs=["source/source-b"])

        errors = validate_entities([subject_a, subject_b, source_b, wrong_type, missing, mismatch])

        self.assertTrue(any(error.field == "source_refs" and "expected source" in error.message for error in errors))
        self.assertTrue(any(error.field == "source_refs" and "missing reference" in error.message for error in errors))
        self.assertTrue(any(error.field == "source_refs" and "subject mismatch" in error.message for error in errors))

    def test_circular_supersession_fails(self):
        entities = [make_entity("subject", "example"), make_entity("event", "example-001")]
        claim_a = make_entity("claim", "a", supersedes="claim/b")
        claim_b = make_entity("claim", "b", supersedes="claim/a")
        entities.extend([claim_a, claim_b])

        errors = validate_entities(entities)

        self.assertTrue(any(error.field == "supersedes" and "circular" in error.message for error in errors))

    def test_consistent_supersession_pair_is_acyclic(self):
        entities = [make_entity("subject", "example"), make_entity("event", "example-001")]
        claim_a = make_entity("claim", "a", supersedes="claim/b")
        claim_b = make_entity("claim", "b", superseded_by="claim/a")
        entities.extend([claim_a, claim_b])

        errors = validate_entities(entities)

        self.assertFalse(any(error.field == "supersedes" and "circular" in error.message for error in errors))

    def test_nested_field_types_and_context_vocabulary_fail(self):
        source = make_entity("source", "bad-consent")
        source.meta["consent"]["purposes"] = ["not-a-purpose"]
        event = make_entity("event", "bad-context")
        event.meta["context"]["domains"] = ["not-a-domain"]
        claim = make_entity("claim", "bad-claim", alternative_explanations="only-one")

        errors = validate_entities([source, event, claim])

        self.assertTrue(any(error.field == "consent.purposes" for error in errors))
        self.assertTrue(any(error.field == "context.domains" for error in errors))
        self.assertTrue(any(error.field == "alternative_explanations" and "list" in error.message for error in errors))

    def test_null_list_fields_remain_unknown_and_empty_lists_remain_no_items(self):
        entities = valid_entities()
        event = next(entity for entity in entities if entity.id == "event/example-001")
        claim = next(entity for entity in entities if entity.id == "claim/example-control")
        event.meta["delayed_outcome"] = None
        claim.meta["counterevidence"] = None

        errors = validate_entities(entities)

        self.assertEqual(errors, [])
        self.assertEqual(event.meta["delayed_outcome"], None)
        self.assertEqual(claim.meta["counterevidence"], None)

    def test_null_claim_evidence_and_pattern_evidence_are_rejected(self):
        claim = make_entity("claim", "no-evidence", supporting_evidence=None)
        pattern = make_entity("pattern", "no-evidence", evidence=None)

        errors = validate_entities([claim, pattern])

        self.assertTrue(any(error.field == "supporting_evidence" and "no evidence" in error.message for error in errors))
        self.assertTrue(any(error.field == "evidence" and "multiple distinct Events" in error.message for error in errors))

    def test_motivation_direction_accepts_all_fixed_values(self):
        for direction in ("seek", "protect", "avoid", "mixed", "unknown"):
            with self.subTest(direction=direction):
                claim = make_entity("claim", f"direction-{direction}", motivation_direction=direction)
                errors = validate_entities([claim])
                self.assertFalse(any(error.field == "motivation_direction" for error in errors))

    def test_motivation_direction_is_required_and_layer_scoped(self):
        missing = make_entity("claim", "direction-missing")
        del missing.meta["motivation_direction"]
        invalid_value = make_entity("claim", "direction-invalid", motivation_direction="not-a-direction")
        non_motivation = make_entity("claim", "direction-tension", layer="tension", motivation_direction="avoid")
        non_motivation_null = make_entity("claim", "direction-tension-null", layer="tension", motivation_direction=None)

        errors = validate_entities([missing, invalid_value, non_motivation, non_motivation_null])

        self.assertTrue(any(error.path.endswith("direction-missing.md") and error.field == "motivation_direction" and "required field" in error.message for error in errors))
        self.assertTrue(any(error.path.endswith("direction-invalid.md") and error.field == "motivation_direction" and "closed vocabulary" in error.message for error in errors))
        self.assertTrue(any(error.path.endswith("direction-tension.md") and error.field == "motivation_direction" and "must be null" in error.message for error in errors))
        self.assertFalse(any(error.path.endswith("direction-tension-null.md") and error.field == "motivation_direction" for error in errors))


if __name__ == "__main__":
    unittest.main()
