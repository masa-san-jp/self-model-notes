#!/usr/bin/env python3
"""Consent-gated, raw-voice-free research signal export."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities, validate_entities
except ModuleNotFoundError:  # Imported as tools.export_signals by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities, validate_entities


def _list_refs(meta: dict[str, Any], field: str) -> list[str]:
    value = meta.get(field)
    if isinstance(value, list):
        return sorted({item for item in value if isinstance(item, str)})
    if isinstance(value, str):
        return [value]
    return []


def _selected(entities, subject: str):
    return [entity for entity in entities if entity.id == subject or entity.meta.get("subject") == subject]


def _source_ids(selected) -> list[str]:
    source_ids = {entity.id for entity in selected if entity.type == "source"}
    for entity in selected:
        if entity.type == "subject":
            source_ids.update(_list_refs(entity.meta, "consent_refs"))
        elif entity.type == "event":
            source_ids.update(_list_refs(entity.meta, "source_refs"))
        elif entity.type == "measurement":
            source_ids.update(_list_refs(entity.meta, "source_ref"))
    return sorted(source_ids)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
    return None


def _denial(source: str, rule: str, message: str) -> dict[str, str]:
    return {"source": source, "rule": rule, "message": message}


def _consent_denials(source, purpose: str, operation: str, today: date) -> list[dict[str, str]]:
    consent = source.meta.get("consent")
    if not isinstance(consent, dict):
        return [_denial(source.id, "consent.present", "Source consent is missing or not a mapping.")]

    denials: list[dict[str, str]] = []
    if consent.get("obtained") is not True:
        denials.append(_denial(source.id, "consent.obtained", "Consent was not obtained."))
    if "revoked_at" not in consent or consent.get("revoked_at") is not None:
        denials.append(_denial(source.id, "consent.revoked_at", "Consent is revoked or its revocation state is unknown."))

    purposes = consent.get("purposes")
    if not isinstance(purposes, list) or purpose not in purposes:
        denials.append(_denial(source.id, "consent.purposes", "Requested purpose is not explicitly permitted."))

    operations = consent.get("allowed_operations")
    if not isinstance(operations, list) or operation not in operations:
        denials.append(_denial(source.id, "consent.allowed_operations", "Requested operation is not explicitly permitted."))

    if "expires_at" not in consent:
        denials.append(_denial(source.id, "consent.expires_at", "Consent expiry is missing."))
    elif consent.get("expires_at") is not None:
        expiry = _as_date(consent.get("expires_at"))
        if expiry is None:
            denials.append(_denial(source.id, "consent.expires_at", "Consent expiry is invalid."))
        elif expiry <= today:
            denials.append(_denial(source.id, "consent.expires_at", "Consent has expired."))
    return denials


def _source_commit(root: Path = ROOT) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _signals(selected) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entity in selected:
        if entity.type == "claim":
            result.append(
                {
                    "entity_ref": entity.id,
                    "kind": "claim",
                    "statement": entity.meta.get("statement"),
                    "certainty": entity.meta.get("confidence", "unknown"),
                    "evidence_refs": _list_refs(entity.meta, "supporting_evidence"),
                }
            )
        elif entity.type == "pattern":
            result.append(
                {
                    "entity_ref": entity.id,
                    "kind": "pattern",
                    "statement": entity.meta.get("condition"),
                    "certainty": entity.meta.get("confidence", "unknown"),
                    "evidence_refs": _list_refs(entity.meta, "evidence"),
                }
            )
    result.sort(key=lambda item: item["entity_ref"])
    return result


def export_signals(
    entities,
    subject: str,
    purpose: str,
    *,
    operation: str = "export-signals",
    today: date | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    selected = _selected(entities, subject)
    if not any(entity.id == subject and entity.type == "subject" for entity in selected):
        return {
            "allowed": False,
            "subject": subject,
            "purpose": purpose,
            "operation": operation,
            "denials": [_denial(subject, "subject.exists", "Subject is missing.")],
        }

    by_id = {entity.id: entity for entity in selected}
    source_ids = _source_ids(selected)
    denials: list[dict[str, str]] = []
    if not source_ids:
        denials.append(_denial("repository", "evidence.sources", "No evidence Source is available for consent checks."))
    for source_id in source_ids:
        source = by_id.get(source_id)
        if source is None or source.type != "source":
            denials.append(_denial(source_id, "evidence.source_exists", "Evidence Source is missing."))
            continue
        denials.extend(_consent_denials(source, purpose, operation, today or date.today()))

    if denials:
        denials.sort(key=lambda item: (item["source"], item["rule"]))
        return {
            "allowed": False,
            "subject": subject,
            "purpose": purpose,
            "operation": operation,
            "denials": denials,
        }

    return {
        "allowed": True,
        "subject": subject,
        "purpose": purpose,
        "operation": operation,
        "source_commit": source_commit if source_commit is not None else _source_commit(),
        "signals": _signals(selected),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--operation", default="export-signals")
    args = parser.parse_args()
    entities = discover_entities()
    validation_errors = validate_entities(entities)
    if validation_errors:
        result = {
            "allowed": False,
            "subject": args.subject,
            "purpose": args.purpose,
            "operation": args.operation,
            "denials": [_denial("repository", "structural-validation", "Entity validation failed; export is blocked.")],
        }
    else:
        result = export_signals(entities, args.subject, args.purpose, operation=args.operation)
    if not result["allowed"]:
        print("Export denied", file=sys.stderr)
        print(canonical_json(result), file=sys.stderr, end="")
        return 1
    print(canonical_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
