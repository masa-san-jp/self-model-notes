import json
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tools.build_graph import build
from tools.build_self_model import build_model
from tools.bundle import render_bundle
from tools.export_signals import (
    build_research_signals,
    export_signals,
    validate_research_signals,
)
from tools.kb import ROOT, discover_entities, validate_entities


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "e2e" / "entities"
PLURAL_PATHS = {
    "subject": "subjects",
    "source": "sources",
    "event": "events",
    "claim": "claims",
    "pattern": "patterns",
    "measurement": "measurements",
}


def e2e_entities():
    loaded = discover_entities(FIXTURE_ROOT)
    return [
        replace(
            entity,
            path=ROOT / "entities" / PLURAL_PATHS[entity.type] / f"{entity.id.split('/', 1)[1]}.md",
        )
        for entity in loaded
    ]


class EndToEndTests(unittest.TestCase):
    def test_anonymized_fixture_runs_source_to_export(self):
        entities = e2e_entities()
        source_commit = "b" * 40

        self.assertEqual(validate_entities(entities), [])
        graph, coverage = build(entities)
        trace_paths = graph["trace_paths"]["source_to_pattern"]
        self.assertTrue(any(path["path"] == ["source/evidence", "event/first", "pattern/repeat"] for path in trace_paths))
        self.assertEqual(coverage["counts"]["event"], 2)

        model = build_model(entities, "subject/fixture", source_commit=source_commit)
        self.assertEqual(model["as_of"], "2026-08-04")
        self.assertTrue(model["motivations"])
        self.assertTrue(model["patterns"])
        bundle = render_bundle(model)
        self.assertIn("## Observations", bundle)
        self.assertIn("## Inference", bundle)

        consented = export_signals(
            entities,
            "subject/fixture",
            "artistic-research",
            operation="export-signals",
            today=date(2026, 8, 11),
            source_commit=source_commit,
        )
        self.assertTrue(consented["allowed"])
        contract = build_research_signals(consented)
        self.assertEqual(validate_research_signals(contract), [])
        self.assertEqual(contract["signal_count"], len(contract["signals"]))
        self.assertTrue(contract["signals"])

        rendered = json.dumps({"graph": graph, "model": model, "bundle": bundle, "export": contract}, ensure_ascii=False)
        self.assertNotIn("synthetic voice one", rendered)
        self.assertNotIn("synthetic voice two", rendered)

    def test_fixture_and_generated_checks_are_deterministic(self):
        entities = e2e_entities()
        first_graph, first_coverage = build(entities)
        second_graph, second_coverage = build(list(reversed(entities)))

        self.assertEqual(first_graph, second_graph)
        self.assertEqual(first_coverage, second_coverage)
        result = subprocess.run(
            [sys.executable, "tools/build_graph.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_has_no_direct_identifier_or_project_specific_key(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in FIXTURE_ROOT.glob("*/*.md"))

        self.assertNotIn("Masa", text)
        self.assertNotIn("@", text)
        self.assertNotIn("email:", text)
        self.assertNotIn("phone:", text)
        self.assertNotIn("address:", text)


if __name__ == "__main__":
    unittest.main()
