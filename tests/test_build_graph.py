import tempfile
import unittest
from pathlib import Path

from tools.build_graph import build, build_coverage, coverage_markdown, stale_generated_files
from tools.kb import ROOT


PLURAL_PATHS = {
    "subject": "subjects",
    "source": "sources",
    "event": "events",
    "claim": "claims",
    "pattern": "patterns",
    "measurement": "measurements",
}


def make_entity(kind, slug, **fields):
    return type(
        "FixtureEntity",
        (),
        {
            "id": f"{kind}/{slug}",
            "type": kind,
            "path": ROOT / "entities" / PLURAL_PATHS[kind] / f"{slug}.md",
            "meta": {"id": f"{kind}/{slug}", "type": kind, **fields},
        },
    )()


class BuildGraphTests(unittest.TestCase):
    def test_graph_traces_source_to_pattern_through_event_and_claim(self):
        subject = make_entity("subject", "example")
        source = make_entity("source", "interview", subject="subject/example")
        event = make_entity(
            "event",
            "observed",
            subject="subject/example",
            source_refs=["source/interview"],
        )
        claim = make_entity(
            "claim",
            "hypothesis",
            subject="subject/example",
            supporting_evidence=["event/observed"],
        )
        pattern = make_entity(
            "pattern",
            "recurring",
            subject="subject/example",
            evidence=["event/observed"],
            claim_refs=["claim/hypothesis"],
        )

        graph, _ = build([pattern, claim, event, source, subject])

        self.assertEqual([node["id"] for node in graph["nodes"]], [
            "claim/hypothesis",
            "event/observed",
            "pattern/recurring",
            "source/interview",
            "subject/example",
        ])
        self.assertIn(
            {"from": "event/observed", "field": "source_refs", "to": "source/interview"},
            graph["edges"],
        )
        paths = graph["trace_paths"]["source_to_pattern"]
        self.assertEqual({tuple(path["path"]) for path in paths}, {
            ("source/interview", "event/observed", "pattern/recurring"),
            ("source/interview", "event/observed", "claim/hypothesis", "pattern/recurring"),
        })

    def test_coverage_distinguishes_unobserved_unknown_and_confirmed_empty(self):
        event = make_entity(
            "event",
            "coverage",
            observed_facts=None,
            raw_voice=[],
            action=["reordered the plan"],
            context={"uncertainty": "unknown"},
        )

        coverage = build_coverage([event])

        self.assertEqual(coverage["field_coverage"]["event.trigger"]["unobserved"], 1)
        self.assertEqual(coverage["field_coverage"]["event.observed_facts"]["unknown"], 1)
        self.assertEqual(coverage["field_coverage"]["event.raw_voice"]["confirmed-empty"], 1)
        self.assertEqual(coverage["field_coverage"]["event.action"]["observed"], 1)
        self.assertEqual(coverage["field_coverage"]["event.context.uncertainty"]["unknown"], 1)

        markdown = coverage_markdown(coverage)
        self.assertIn("Unobserved", markdown)
        self.assertIn("Unknown", markdown)
        self.assertIn("Confirmed empty", markdown)

    def test_repeated_builds_are_byte_identical(self):
        entities = [
            make_entity("source", "stable", subject="subject/example"),
            make_entity("subject", "example", consent_refs=[]),
        ]

        first_graph, first_coverage = build(entities)
        second_graph, second_coverage = build(list(reversed(entities)))

        self.assertEqual(first_graph, second_graph)
        self.assertEqual(first_coverage, second_coverage)
        self.assertEqual(coverage_markdown(first_coverage), coverage_markdown(second_coverage))

    def test_stale_generated_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text("old\n", encoding="utf-8")

            self.assertEqual(stale_generated_files({path: "new\n"}), [path])
            path.write_text("new\n", encoding="utf-8")
            self.assertEqual(stale_generated_files({path: "new\n"}), [])


if __name__ == "__main__":
    unittest.main()
