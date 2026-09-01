#!/usr/bin/env python3
"""Create privacy-gated Source/Event drafts from an annotated transcript.

The intake format is deliberately explicit.  This tool does not infer facts
from free-form conversation, call a model, or access a network.  An agent or
human must annotate each event block before the draft can be created.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

import yaml

try:
    from kb import ID_RE, ROOT, vocabularies
except ModuleNotFoundError:  # Imported as tools.intake_conversation by tests.
    from tools.kb import ID_RE, ROOT, vocabularies


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EVENT_HEADER_RE = re.compile(r"^\[event:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*\]$")
FIELD_RE = re.compile(r"^([a-z_]+)\s*:\s*(.*)$")
OPAQUE_LOCATOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s?#]+$")

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[ -])?(?:0\d{1,4}[- ]\d{2,4}[- ]\d{3,4})(?!\d)"
)
URL_RE = re.compile(r"\bhttps?://[^\s]+", re.IGNORECASE)
IDENTIFIER_LABEL_RE = re.compile(
    r"(?im)^\s*(?:name|full_name|email|phone|address|氏名|名前|メール|電話|住所)\s*[:：]"
)

MAX_TRANSCRIPT_BYTES = 1_000_000
MAX_RAW_QUOTE_CHARS = 120

METADATA_REQUIRED = {"source_slug", "subject", "source_kind", "captured_at", "locator", "consent"}
METADATA_OPTIONAL = {"reliability_notes"}
CONSENT_FIELDS = {
    "obtained",
    "obtained_at",
    "purposes",
    "allowed_operations",
    "expires_at",
    "revoked_at",
    "notes",
}

REPEATED_FIELDS = {
    "domain",
    "social",
    "observed_fact",
    "raw_voice",
    "appraisal",
    "emotion",
    "body",
    "cognition",
    "action",
    "immediate_outcome",
    "delayed_outcome",
}
EVENT_FIELDS = {
    "observed_at",
    "precision",
    "domain",
    "social",
    "uncertainty",
    "control",
    "fatigue",
    "stress",
    "trigger",
    *REPEATED_FIELDS,
}
REQUIRED_EVENT_FIELDS = EVENT_FIELDS
LIST_OUTPUT_FIELDS = (
    "observed_fact",
    "appraisal",
    "emotion",
    "body",
    "cognition",
    "action",
    "immediate_outcome",
    "delayed_outcome",
)


class IntakeError(ValueError):
    """A safe, user-actionable rejection that never contains input content."""


def _reject(code: str, remediation: str) -> None:
    raise IntakeError(f"{code}: {remediation}")


def _normalise_yaml(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalise_yaml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise_yaml(item) for item in value]
    return value


def _read_text(path: Path, *, label: str) -> str:
    try:
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            _reject("input-too-large", f"reduce the {label} file below {MAX_TRANSCRIPT_BYTES} bytes and retry")
        return path.read_text(encoding="utf-8")
    except IntakeError:
        raise
    except (OSError, UnicodeError) as error:
        raise IntakeError(f"input-unreadable: provide a readable UTF-8 {label} file") from error


def _load_metadata(path: Path) -> dict[str, Any]:
    text = _read_text(path, label="metadata")
    try:
        value = _normalise_yaml(yaml.safe_load(text))
    except yaml.YAMLError as error:
        raise IntakeError("metadata-invalid: provide YAML or JSON Source metadata") from error
    if not isinstance(value, dict):
        _reject("metadata-invalid", "provide a mapping with the documented Source metadata fields")
    return value


def _scan_direct_identifiers(text: str) -> None:
    if EMAIL_RE.search(text) or PHONE_RE.search(text) or URL_RE.search(text) or IDENTIFIER_LABEL_RE.search(text):
        _reject(
            "direct-identifier-detected",
            "remove names, email addresses, phone numbers, addresses, and URLs before retrying; no draft was written",
        )


def _require_string(value: Any, code: str, remediation: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject(code, remediation)
    return value.strip()


def _parse_date(value: Any, *, field: str) -> tuple[str, date]:
    text = _require_string(value, "metadata-invalid", f"set {field} to an ISO 8601 date or timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as error:
            raise IntakeError(f"metadata-invalid: set {field} to an ISO 8601 date or timestamp") from error
        return parsed_date.isoformat(), parsed_date
    return text, parsed.date()


def _validate_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - METADATA_REQUIRED - METADATA_OPTIONAL
    if unknown:
        _reject("metadata-invalid", "remove unsupported Source metadata fields; do not provide direct identifiers")
    missing = METADATA_REQUIRED - set(raw)
    if missing:
        _reject("consent-metadata-missing", "provide source metadata including consent before generating a draft")

    source_slug = _require_string(
        raw["source_slug"], "metadata-invalid", "set source_slug to an ASCII kebab-case slug"
    )
    if not SLUG_RE.fullmatch(source_slug):
        _reject("metadata-invalid", "set source_slug to an ASCII kebab-case slug")

    subject = _require_string(raw["subject"], "metadata-invalid", "set subject to a subject/<slug> entity ID")
    if not ID_RE.fullmatch(subject) or not subject.startswith("subject/"):
        _reject("metadata-invalid", "set subject to a subject/<slug> entity ID")

    source_kind = _require_string(raw["source_kind"], "metadata-invalid", "use a source_kind from config/vocabularies.yaml")
    if source_kind not in set(vocabularies()["source_kinds"]):
        _reject("metadata-invalid", "use a source_kind from config/vocabularies.yaml")

    captured_at, captured_date = _parse_date(raw["captured_at"], field="captured_at")
    locator = _require_string(raw["locator"], "metadata-invalid", "provide an opaque locator such as gdrive://opaque-id")
    if not OPAQUE_LOCATOR_RE.fullmatch(locator) or locator.lower().startswith("file://") or "@" in locator:
        _reject("metadata-invalid", "provide an opaque locator without a local path or direct identifier")

    consent = raw["consent"]
    if not isinstance(consent, dict):
        _reject("consent-metadata-missing", "provide the complete consent mapping before generating a draft")
    consent = _normalise_yaml(consent)
    if set(consent) != CONSENT_FIELDS:
        _reject("consent-metadata-missing", "provide every consent field, including purposes and allowed_operations")
    if consent["obtained"] is not True:
        _reject("consent-insufficient", "obtain explicit consent before generating a Source/Event draft")
    _parse_date(consent["obtained_at"], field="consent.obtained_at")
    for field in ("expires_at", "revoked_at"):
        value = consent[field]
        if value is not None:
            _, value_date = _parse_date(value, field=f"consent.{field}")
            if field == "expires_at" and value_date < captured_date:
                _reject("consent-insufficient", "use a consent expiry that covers the captured_at date")
            if field == "revoked_at":
                _reject("consent-insufficient", "revoked consent cannot be used to generate a draft")
    for field in ("purposes", "allowed_operations"):
        values = consent[field]
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            _reject("consent-insufficient", f"provide a non-empty consent.{field} list")
    vocab = vocabularies()
    if any(value not in set(vocab["allowed_purposes"]) for value in consent["purposes"]):
        _reject("consent-insufficient", "use only allowed purposes from config/vocabularies.yaml")
    if any(value not in set(vocab["allowed_operations"]) for value in consent["allowed_operations"]):
        _reject("consent-insufficient", "use only allowed operations from config/vocabularies.yaml")
    if "store-reference" not in consent["allowed_operations"]:
        _reject("consent-insufficient", "consent.allowed_operations must include store-reference for intake")
    if consent["notes"] is not None and not isinstance(consent["notes"], str):
        _reject("metadata-invalid", "set consent.notes to text or null")
    reliability_notes = raw.get("reliability_notes")
    if reliability_notes is not None and not isinstance(reliability_notes, str):
        _reject("metadata-invalid", "set reliability_notes to text or null")

    scan_values = dict(raw)
    scan_values["locator"] = ""
    _scan_direct_identifiers(yaml.safe_dump(scan_values, allow_unicode=True, sort_keys=True))

    return {
        "source_slug": source_slug,
        "subject": subject,
        "source_kind": source_kind,
        "captured_at": captured_at,
        "captured_date": captured_date.isoformat(),
        "locator": locator,
        "consent": consent,
        "reliability_notes": reliability_notes,
    }


def _yaml_value(raw_value: str) -> Any:
    try:
        return _normalise_yaml(yaml.safe_load(raw_value))
    except yaml.YAMLError as error:
        raise IntakeError("transcript-format-invalid: quote text containing YAML punctuation and retry") from error


def _append_repeated(values: dict[str, Any], key: str, value: Any) -> None:
    if key not in values:
        if value is None:
            values[key] = None
        elif isinstance(value, list):
            values[key] = list(value)
        else:
            values[key] = [value]
        return
    existing = values[key]
    if existing is None or value is None or existing == []:
        _reject(
            "multiple-events-mixed",
            f"split ambiguous repeated {key} content into separate [event: slug] blocks; no draft was written",
        )
    if isinstance(value, list):
        values[key].extend(value)
    else:
        values[key].append(value)


def _parse_transcript(text: str) -> list[dict[str, Any]]:
    _scan_direct_identifiers(text)
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        header = EVENT_HEADER_RE.fullmatch(line)
        if header:
            if current is not None:
                events.append(current)
            current = {"slug": header.group(1)}
            continue
        if current is None:
            _reject("transcript-format-invalid", "start each event with [event: slug]; no draft was written")
        match = FIELD_RE.fullmatch(line)
        if not match or match.group(1) not in EVENT_FIELDS:
            _reject("transcript-format-invalid", f"line {line_number} must use a documented event field; no draft was written")
        key, raw_value = match.groups()
        value = _yaml_value(raw_value)
        if key in REPEATED_FIELDS:
            _append_repeated(current, key, value)
        elif key in current:
            _reject(
                "multiple-events-mixed",
                f"split duplicate {key} content into separate [event: slug] blocks; no draft was written",
            )
        else:
            current[key] = value
    if current is not None:
        events.append(current)
    if not events:
        _reject("transcript-format-invalid", "provide at least one annotated [event: slug] block; no draft was written")
    seen_slugs: set[str] = set()
    for event in events:
        slug = event["slug"]
        if slug in seen_slugs:
            _reject("multiple-events-mixed", "give each event block a distinct slug; no draft was written")
        seen_slugs.add(slug)
        missing = REQUIRED_EVENT_FIELDS - set(event)
        if missing:
            _reject(
                "transcript-format-invalid",
                "provide every event field explicitly; use null, [], or unknown instead of omitting a field; no draft was written",
            )
    return events


def _scalar(value: Any, *, field: str, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        _reject("transcript-value-invalid", f"set {field} to text or null; no draft was written")
    return value


def _list_slot(value: Any, *, field: str, allow_none: bool = True) -> list[str] | None:
    if value is None and allow_none:
        return None
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(item, str) for item in values):
        _reject("transcript-value-invalid", f"set {field} to text, a text list, or null; no draft was written")
    return list(values)


def _validate_event_values(event: dict[str, Any], *, source_id: str) -> None:
    observed_at = _scalar(event["observed_at"], field="observed_at")
    if observed_at is not None:
        try:
            datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise IntakeError("transcript-value-invalid: set observed_at to an ISO 8601 timestamp or null; no draft was written") from error
    precision = _scalar(event["precision"], field="precision", allow_none=False)
    if precision is None or not precision:
        _reject("transcript-value-invalid", "set precision to a value or unknown; no draft was written")
    for field in ("uncertainty", "control"):
        value = _scalar(event[field], field=field, allow_none=False)
        if value not in set(vocabularies()["context_dimensions"][field]):
            _reject("transcript-value-invalid", f"use a value from config/contexts.yaml for {field}; no draft was written")
    for field in ("domain", "social"):
        values = _list_slot(event[field], field=field, allow_none=False)
        context_field = {"domain": "domains", "social": "social"}[field]
        allowed = set(vocabularies()["context_dimensions"][context_field])
        if values is None:
            _reject("transcript-value-invalid", f"use [] for confirmed empty {field}; do not omit it; no draft was written")
        if any(value not in allowed for value in values):
            _reject("transcript-value-invalid", f"use values from config/contexts.yaml for {field}; no draft was written")
    _scalar(event["fatigue"], field="fatigue")
    _scalar(event["stress"], field="stress")
    _scalar(event["trigger"], field="trigger")
    for field in LIST_OUTPUT_FIELDS + ("raw_voice",):
        values = _list_slot(event[field], field=field)
        if field != "raw_voice":
            continue
        if values is not None:
            for quote in values:
                if not quote.strip():
                    _reject("transcript-value-invalid", "remove empty raw_voice items or use raw_voice: []; no draft was written")
                if len(quote) > MAX_RAW_QUOTE_CHARS:
                    _reject("raw-quote-too-long", f"reduce each raw_voice quote to {MAX_RAW_QUOTE_CHARS} characters or fewer; no draft was written")
    del source_id  # Keep the validation signature explicit about the source boundary.


def _source_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    source_id = f"source/{metadata['source_slug']}"
    return {
        "id": source_id,
        "type": "source",
        "subject": metadata["subject"],
        "source_kind": metadata["source_kind"],
        "captured_at": metadata["captured_at"],
        "locator": metadata["locator"],
        "raw_content_stored": False,
        "consent": metadata["consent"],
        "reliability_notes": metadata["reliability_notes"],
        "created": metadata["captured_date"],
        "updated": metadata["captured_date"],
    }


def _event_meta(event: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    source_id = f"source/{metadata['source_slug']}"
    _validate_event_values(event, source_id=source_id)
    event_id = f"event/{metadata['source_slug']}-{event['slug']}"
    domains = _list_slot(event["domain"], field="domain", allow_none=False)
    social = _list_slot(event["social"], field="social", allow_none=False)
    raw_voice = _list_slot(event["raw_voice"], field="raw_voice")
    raw_voice_items = None if raw_voice is None else [
        {"text": quote, "source_ref": source_id} for quote in raw_voice
    ]
    return {
        "id": event_id,
        "type": "event",
        "subject": metadata["subject"],
        "time": {"observed_at": event["observed_at"], "precision": event["precision"]},
        "context": {
            "domains": domains,
            "social": social,
            "uncertainty": event["uncertainty"],
            "control": event["control"],
        },
        "state": {"fatigue": event["fatigue"], "stress": event["stress"]},
        "trigger": event["trigger"],
        "observed_facts": _list_slot(event["observed_fact"], field="observed_fact"),
        "raw_voice": raw_voice_items,
        "appraisal": _list_slot(event["appraisal"], field="appraisal"),
        "emotion": _list_slot(event["emotion"], field="emotion"),
        "body": _list_slot(event["body"], field="body"),
        "cognition": _list_slot(event["cognition"], field="cognition"),
        "action": _list_slot(event["action"], field="action"),
        "immediate_outcome": _list_slot(event["immediate_outcome"], field="immediate_outcome"),
        "delayed_outcome": _list_slot(event["delayed_outcome"], field="delayed_outcome"),
        "source_refs": [source_id],
        "created": metadata["captured_date"],
        "updated": metadata["captured_date"],
    }


def _render_draft(meta: dict[str, Any]) -> str:
    frontmatter = yaml.safe_dump(
        meta,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    return "---\n" + frontmatter + "---\n\n# Intake draft\n\nHuman review is required before adoption into canonical entities/.\n"


def _write_drafts(output_dir: Path, drafts: list[tuple[str, str]]) -> list[Path]:
    resolved_output = output_dir.resolve()
    entity_root = (ROOT / "entities").resolve()
    try:
        resolved_output.relative_to(entity_root)
    except ValueError:
        pass
    else:
        _reject("output-path-invalid", "write drafts outside canonical entities/ and adopt them only after human review")
    if output_dir.exists() and not output_dir.is_dir():
        _reject("output-path-invalid", "choose a directory for draft output")
    if any((output_dir / name).exists() for name, _ in drafts):
        _reject("output-collision", "choose an empty or non-colliding draft output directory; no draft was written")
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".intake-", dir=output_dir.parent))
    except OSError as error:
        raise IntakeError("output-unwritable: choose a writable draft output directory") from error
    try:
        for name, content in drafts:
            (temporary / name).write_text(content, encoding="utf-8", newline="\n")
        for name, _ in drafts:
            os.replace(temporary / name, output_dir / name)
    except OSError as error:
        raise IntakeError("output-unwritable: no canonical entity was changed; retry with a writable draft directory") from error
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return [output_dir / name for name, _ in drafts]


def generate_drafts(transcript_path: Path, metadata_path: Path, output_dir: Path) -> list[Path]:
    """Validate input and atomically write Source/Event draft files."""

    transcript = _read_text(transcript_path, label="transcript")
    metadata = _validate_metadata(_load_metadata(metadata_path))
    events = _parse_transcript(transcript)
    source = _source_meta(metadata)
    event_metas = [_event_meta(event, metadata) for event in events]
    drafts = [(f"{metadata['source_slug']}.source.draft.md", _render_draft(source))]
    drafts.extend(
        (
            f"{metadata['source_slug']}--{event['slug']}.event.draft.md",
            _render_draft(event_meta),
        )
        for event, event_meta in zip(events, event_metas)
    )
    return _write_drafts(output_dir, drafts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create consent-gated Source/Event drafts from an annotated transcript without network access."
    )
    parser.add_argument("transcript", type=Path, help="annotated UTF-8 transcript text file")
    parser.add_argument("--metadata", required=True, type=Path, help="Source metadata YAML/JSON file")
    parser.add_argument("--output-dir", required=True, type=Path, help="draft output directory outside entities/")
    args = parser.parse_args(argv)
    try:
        paths = generate_drafts(args.transcript, args.metadata, args.output_dir)
    except IntakeError as error:
        print(f"intake rejected: {error}", file=sys.stderr)
        return 2
    print(f"created {len(paths)} draft(s)")
    for path in paths:
        print(f"- {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
