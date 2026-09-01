from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.intake_conversation import IntakeError, generate_drafts
from tools.kb import parse_markdown


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "intake"
VALID_METADATA = FIXTURE_ROOT / "valid" / "metadata.yaml"
VALID_TRANSCRIPT = FIXTURE_ROOT / "valid" / "transcript.txt"


def draft_meta(path: Path) -> dict:
    return parse_markdown(path).meta


class ConversationIntakeTests(unittest.TestCase):
    def test_valid_input_creates_separate_source_and_event_drafts_deterministically(self):
        with tempfile.TemporaryDirectory(prefix="intake-test-") as first_dir, tempfile.TemporaryDirectory(prefix="intake-test-") as second_dir:
            first = generate_drafts(VALID_TRANSCRIPT, VALID_METADATA, Path(first_dir))
            second = generate_drafts(VALID_TRANSCRIPT, VALID_METADATA, Path(second_dir))

            first_bytes = {path.name: path.read_bytes() for path in first}
            second_bytes = {path.name: path.read_bytes() for path in second}
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                set(first_bytes),
                {
                    "conversation-fixture-20260901.source.draft.md",
                    "conversation-fixture-20260901--planning.event.draft.md",
                    "conversation-fixture-20260901--review.event.draft.md",
                },
            )

            source = draft_meta(first[0])
            self.assertEqual(source["id"], "source/conversation-fixture-20260901")
            self.assertFalse(source["raw_content_stored"])
            self.assertEqual(source["locator"], "telegram://opaque-transcript-fixture")

            event_ids = {draft_meta(path)["id"] for path in first[1:]}
            self.assertEqual(
                event_ids,
                {"event/conversation-fixture-20260901-planning", "event/conversation-fixture-20260901-review"},
            )
            rendered = b"".join(first_bytes.values()).decode("utf-8")
            self.assertIn("自分で決め直したい", rendered)
            self.assertNotIn("Each event is explicitly separated", rendered)
            self.assertNotIn("Claim", rendered)
            self.assertNotIn("Pattern", rendered)

    def test_null_empty_and_unknown_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="intake-test-") as output_dir:
            drafts = generate_drafts(VALID_TRANSCRIPT, VALID_METADATA, Path(output_dir))
            review = next(path for path in drafts if "--review." in path.name)
            meta = draft_meta(review)

            self.assertIsNone(meta["time"]["observed_at"])
            self.assertEqual(meta["time"]["precision"], "unknown")
            self.assertEqual(meta["context"]["domains"], [])
            self.assertEqual(meta["context"]["uncertainty"], "unknown")
            self.assertIsNone(meta["state"]["fatigue"])
            self.assertEqual(meta["state"]["stress"], "unknown")
            self.assertIsNone(meta["trigger"])
            self.assertEqual(meta["observed_facts"], [])
            self.assertEqual(meta["raw_voice"], [])

    def test_missing_consent_direct_identifier_and_mixed_events_fail_without_output(self):
        invalid_consent_metadata = FIXTURE_ROOT / "invalid-consent" / "metadata.yaml"
        mixed_transcript = FIXTURE_ROOT / "invalid-mixed" / "transcript.txt"
        with tempfile.TemporaryDirectory(prefix="intake-test-") as root_dir:
            root = Path(root_dir)
            with self.assertRaisesRegex(IntakeError, "consent-metadata-missing"):
                generate_drafts(VALID_TRANSCRIPT, invalid_consent_metadata, root / "consent")
            self.assertFalse((root / "consent").exists())

            direct_identifier_transcript = root / "direct.txt"
            direct_identifier_transcript.write_text(
                VALID_TRANSCRIPT.read_text(encoding="utf-8").replace(
                    "自分で決め直したい", "intake-user" + "@example.invalid"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntakeError, "direct-identifier-detected"):
                generate_drafts(direct_identifier_transcript, VALID_METADATA, root / "direct")
            self.assertFalse((root / "direct").exists())

            with self.assertRaisesRegex(IntakeError, "multiple-events-mixed"):
                generate_drafts(mixed_transcript, VALID_METADATA, root / "mixed")
            self.assertFalse((root / "mixed").exists())

    def test_canonical_entities_directory_is_never_an_intake_output(self):
        with self.assertRaisesRegex(IntakeError, "output-path-invalid"):
            generate_drafts(VALID_TRANSCRIPT, VALID_METADATA, ROOT / "entities")


if __name__ == "__main__":
    unittest.main()
