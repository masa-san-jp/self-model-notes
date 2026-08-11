import tempfile
import unittest
from pathlib import Path

from tools.kb import discover_entities, parse_markdown, serialize_markdown


class LoaderTest(unittest.TestCase):
    def test_frontmatter_and_body_round_trip_without_semantic_loss(self):
        content = """---
id: event/example
type: event
subject: subject/example
created: 2026-08-11
updated: 2026-08-11
time:
  observed_at: 2026-08-11T10:05:00+09:00
  precision: minute
state:
  fatigue: null
  stress: unknown
raw_voice:
  - text: \"自分で決め直したい\"
    source_ref: source/interview-001
---

# Notes

本文はそのまま保持する。
"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.md"
            round_tripped = Path(directory) / "round-tripped.md"
            source.write_text(content, encoding="utf-8")

            entity = parse_markdown(source)
            round_tripped.write_text(serialize_markdown(entity), encoding="utf-8")
            restored = parse_markdown(round_tripped)

        self.assertEqual(entity.meta, restored.meta)
        self.assertEqual(entity.body, restored.body)

    def test_malformed_frontmatter_reports_a_loader_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.md"
            path.write_text("---\nid: [broken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid YAML frontmatter"):
                parse_markdown(path)

    def test_discover_entities_ignores_readmes_and_returns_sorted_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subjects").mkdir()
            (root / "subjects" / "README.md").write_text("# ignored", encoding="utf-8")
            (root / "subjects" / "b.md").write_text("---\nid: subject/b\ntype: subject\n---\n", encoding="utf-8")
            (root / "subjects" / "a.md").write_text("---\nid: subject/a\ntype: subject\n---\n", encoding="utf-8")

            entities = discover_entities(root)

        self.assertEqual([entity.id for entity in entities], ["subject/a", "subject/b"])


if __name__ == "__main__":
    unittest.main()
