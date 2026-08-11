import unittest

from tests.test_validation import make_entity, valid_entities
from tools.kb import validate_entities


class EpistemicRulesTest(unittest.TestCase):
    def test_claim_without_evidence_confidence_or_two_alternatives_fails(self):
        missing_evidence = make_entity("claim", "missing-evidence", supporting_evidence=None)
        missing_confidence = make_entity("claim", "missing-confidence")
        del missing_confidence.meta["confidence"]
        one_alternative = make_entity("claim", "one-alternative", alternative_explanations=["only one explanation"])

        errors = validate_entities([missing_evidence, missing_confidence, one_alternative])

        self.assertTrue(any(error.field == "supporting_evidence" and "no evidence" in error.message for error in errors))
        self.assertTrue(any(error.field == "confidence" and "required field" in error.message for error in errors))
        self.assertTrue(any(error.field == "alternative_explanations" and "two alternatives" in error.message for error in errors))

    def test_single_event_pattern_fails(self):
        pattern = make_entity("pattern", "single-event", evidence=["event/example-001"])

        errors = validate_entities([pattern])

        self.assertTrue(any(error.field == "evidence" and "multiple distinct Events" in error.message for error in errors))

    def test_unofficial_measurement_with_numeric_scores_fails(self):
        measurement = make_entity(
            "measurement",
            "unofficial-score",
            instrument={"name": "conversation", "version": "1", "official": False, "scoring_reference": None},
            scores={"score": 0.8},
        )

        errors = validate_entities([measurement])

        self.assertTrue(any(error.field == "scores" and "official instrument" in error.message for error in errors))

    def test_raw_voice_on_claim_fails(self):
        claim = make_entity(
            "claim",
            "raw-voice",
            raw_voice=[{"text": "本人の原文", "source_ref": "source/interview-001"}],
        )

        errors = validate_entities([claim])

        self.assertTrue(any(error.field == "raw_voice" and "belongs on an Event" in error.message for error in errors))

    def test_trait_candidate_is_not_auto_promoted(self):
        entities = valid_entities()
        claim = next(entity for entity in entities if entity.id == "claim/example-control")
        claim.meta["scope"] = "trait-candidate"

        validate_entities(entities)

        self.assertEqual(claim.meta["scope"], "trait-candidate")


if __name__ == "__main__":
    unittest.main()
