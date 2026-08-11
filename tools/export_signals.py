#!/usr/bin/env python3
"""Consent-gated, raw-voice-free research signal export."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities, validate_entities
except ModuleNotFoundError:  # Imported as tools.export_signals by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities, validate_entities


RESEARCH_SIGNALS_SCHEMA = "urn:self-model-notes:research-signals:v1"
SOURCE_REPOSITORY = "masa-san-jp/self-model-notes"


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


def _as_of(selected) -> str | None:
    values = []
    for entity in selected:
        for field in ("updated", "created"):
            value = entity.meta.get(field)
            if value is None:
                continue
            values.append(value.isoformat() if hasattr(value, "isoformat") else str(value))
    return max(values) if values else None


def _signals(selected) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entity in selected:
        if entity.type == "claim":
            result.append(
                {
                    "entity_ref": entity.id,
                    "kind": "claim",
                    "layer": entity.meta.get("layer"),
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
                    "layer": "pattern",
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
        "as_of": _as_of(selected),
        "source_commit": source_commit if source_commit is not None else _source_commit(),
        "signals": _signals(selected),
    }


def _signal_item(signal: dict[str, Any]) -> dict[str, Any]:
    certainty = signal.get("certainty")
    return {
        "entity_ref": signal["entity_ref"],
        "statement": signal["statement"],
        "certainty": certainty if isinstance(certainty, str) and certainty in {"unknown", "low", "medium", "high"} else "unknown",
        "evidence_refs": signal["evidence_refs"],
    }


def build_research_signals(result: dict[str, Any]) -> dict[str, Any]:
    """Translate a consent-approved internal result to research_signals v1."""
    if not result.get("allowed"):
        return result
    signal_groups = {
        "seeks": [],
        "protects": [],
        "avoids": [],
        "reacts_against": [],
        "drawn_toward": [],
        "influenced_by": [],
        "tensions": [],
        "recurring_patterns": [],
        "emotional_material": [],
        "raw_voice_refs": [],
    }
    for signal in result.get("signals", []):
        item = _signal_item(signal)
        if signal["kind"] == "pattern":
            signal_groups["recurring_patterns"].append(item)
        elif signal.get("layer") == "emotion":
            signal_groups["emotional_material"].append(item)
        elif signal.get("layer") == "motivation":
            signal_groups["seeks"].append(item)
        elif signal.get("layer") == "behavioral-principle":
            signal_groups["protects"].append(item)
        elif signal.get("layer") == "tension":
            signal_groups["tensions"].append(item)
    for values in signal_groups.values():
        values.sort(key=lambda item: item["entity_ref"])
    evidence_refs = sorted({ref for values in signal_groups.values() for item in values for ref in item["evidence_refs"]})
    certainties = {item["certainty"] for values in signal_groups.values() for item in values}
    certainty = certainties.pop() if len(certainties) == 1 else "unknown"
    payload = {
        "schema": RESEARCH_SIGNALS_SCHEMA,
        "subject": result["subject"],
        "as_of": result.get("as_of"),
        "purpose": result["purpose"],
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": result.get("source_commit"),
        "research_signals": {
            **signal_groups,
            "certainty": certainty,
            "evidence_refs": evidence_refs,
        },
    }
    errors = validate_research_signals(payload)
    if errors:
        raise ValueError("research_signals v1 validation failed: " + "; ".join(errors))
    return payload


def validate_research_signals(payload: dict[str, Any]) -> list[str]:
    schema = payload.get("schema") if isinstance(payload, dict) else None
    match = re.fullmatch(r"urn:self-model-notes:research-signals:v(?P<major>\d+)", str(schema))
    if match is None or match.group("major") != "1":
        raise ValueError(f"Unsupported research_signals schema major version: {schema!r}")
    errors: list[str] = []
    required = ("schema", "subject", "as_of", "purpose", "source_repository", "source_commit", "research_signals")
    errors.extend(f"unknown top-level field: {field}" for field in set(payload) - set(required))
    errors.extend(f"missing required field: {field}" for field in required if field not in payload)
    if not isinstance(payload.get("subject"), str):
        errors.append("subject must be a string")
    if not isinstance(payload.get("purpose"), str):
        errors.append("purpose must be a string")
    as_of = payload.get("as_of")
    if as_of is not None:
        try:
            date.fromisoformat(as_of)
        except (TypeError, ValueError):
            errors.append("as_of must be an ISO date or null")
    if not isinstance(payload.get("source_repository"), str):
        errors.append("source_repository must be a string")
    if not isinstance(payload.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_commit"))):
        errors.append("source_commit must be a 40-character hexadecimal SHA")
    groups = payload.get("research_signals")
    signal_fields = (
        "seeks", "protects", "avoids", "reacts_against", "drawn_toward", "influenced_by",
        "tensions", "recurring_patterns", "emotional_material", "raw_voice_refs",
    )
    if not isinstance(groups, dict):
        errors.append("research_signals must be an object")
        return errors
    allowed_group_fields = set((*signal_fields, "certainty", "evidence_refs"))
    errors.extend(f"unknown research_signals field: {field}" for field in set(groups) - allowed_group_fields)
    errors.extend(f"missing research_signals field: {field}" for field in (*signal_fields, "certainty", "evidence_refs") if field not in groups)
    if groups.get("certainty") not in {"unknown", "low", "medium", "high"}:
        errors.append("research_signals.certainty is outside the closed vocabulary")
    if not isinstance(groups.get("evidence_refs"), list) or not all(isinstance(ref, str) for ref in groups.get("evidence_refs", [])):
        errors.append("research_signals.evidence_refs must be a list of strings")
    elif len(groups["evidence_refs"]) != len(set(groups["evidence_refs"])):
        errors.append("research_signals.evidence_refs must contain unique references")
    for field in signal_fields:
        values = groups.get(field)
        if not isinstance(values, list):
            errors.append(f"research_signals.{field} must be a list")
            continue
        for index, item in enumerate(values):
            if field == "raw_voice_refs":
                if not isinstance(item, str):
                    errors.append(f"research_signals.{field}[{index}] must be a string reference")
                continue
            if not isinstance(item, dict):
                errors.append(f"research_signals.{field}[{index}] must be an object")
                continue
            allowed_item_fields = {"entity_ref", "statement", "certainty", "evidence_refs"}
            errors.extend(f"unknown field in research_signals.{field}[{index}]: {item_field}" for item_field in set(item) - allowed_item_fields)
            for item_field in ("entity_ref", "statement", "certainty", "evidence_refs"):
                if item_field not in item:
                    errors.append(f"research_signals.{field}[{index}] missing {item_field}")
            if not isinstance(item.get("entity_ref"), str):
                errors.append(f"research_signals.{field}[{index}].entity_ref must be a string")
            if not isinstance(item.get("statement"), (str, type(None))):
                errors.append(f"research_signals.{field}[{index}].statement must be a string or null")
            if item.get("certainty") not in {"unknown", "low", "medium", "high"}:
                errors.append(f"research_signals.{field}[{index}].certainty is outside the closed vocabulary")
            if not isinstance(item.get("evidence_refs"), list) or not item.get("evidence_refs") or not all(isinstance(ref, str) for ref in item.get("evidence_refs", [])):
                errors.append(f"research_signals.{field}[{index}].evidence_refs must be a non-empty list of strings")
            elif len(item["evidence_refs"]) != len(set(item["evidence_refs"])):
                errors.append(f"research_signals.{field}[{index}].evidence_refs must contain unique references")
    return errors


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
    try:
        contract = build_research_signals(result)
    except ValueError as error:
        print(f"Export denied: {error}", file=sys.stderr)
        return 1
    print(canonical_json(contract), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
