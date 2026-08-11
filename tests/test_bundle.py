import unittest

from tests.test_validation import make_entity, valid_entities
from tools.bundle import render_bundle
from tools.build_self_model import build_model


class BundleTests(unittest.TestCase):
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

    def test_bundle_separates_observations_from_inference(self):
        entities = self.entities_with_history()
        event = next(entity for entity in entities if entity.id == "event/example-001")
        event.meta["raw_voice"] = [{"text": "synthetic voice", "source_ref": "source/interview-001"}]
        model = build_model(entities, "subject/example", source_commit="abc123")

        bundle = render_bundle(model)

        self.assertLess(bundle.index("## Observations"), bundle.index("## Inference"))
        self.assertIn("Observed facts", bundle)
        self.assertIn("### `claim/revised-control`", bundle)
        self.assertIn("### `claim/rejected-control`", bundle)
        self.assertIn("Evidence refs: `event/example-001`", bundle)
        self.assertNotIn("synthetic voice", bundle)
        self.assertIn("Source commit: `abc123`", bundle)

    def test_bundle_preserves_unknowns_without_turning_them_into_conclusions(self):
        entities = self.entities_with_history()
        unknown = next(entity for entity in entities if entity.id == "claim/example-control")
        unknown.meta["confidence"] = "unknown"
        model = build_model(entities, "subject/example", source_commit="abc123")

        bundle = render_bundle(model)

        self.assertIn("## Unknowns", bundle)
        self.assertIn("confidence is unknown", bundle)


if __name__ == "__main__":
    unittest.main()
