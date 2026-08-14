#!/usr/bin/env python3
"""Consent-gated, raw-voice-free research signal export."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities, validate_entities
except ModuleNotFoundError:  # Imported as tools.export_signals by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities, validate_entities


EXPORT_CONTRACT_VERSION = "research-signal-export/v1"
SOURCE_REPOSITORY = "self-model"


def _list_refs(meta: dict[str, Any], field: str) -> list[str]:
    value = meta.get(field)
    if isinstance(value, list):
        return sorted({item for item in value if isinstance(item, str)})
    if isinstance(value, str):
        return [value]
    return []


def _selected(entities, subject: str | None):
    if subject is None:
        return list(entities)
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
    subject: str | None,
    purpose: str,
    *,
    operation: str = "export-signals",
    today: date | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    if subject is None:
        subject_ids = sorted(entity.id for entity in entities if entity.type == "subject")
        if not subject_ids:
            return {
                "allowed": False,
                "subject": None,
                "purpose": purpose,
                "operation": operation,
                "denials": [_denial("repository", "subject.exists", "No Subject is available for export.")],
            }

        results = [
            export_signals(
                entities,
                subject_id,
                purpose,
                operation=operation,
                today=today,
                source_commit=source_commit,
            )
            for subject_id in subject_ids
        ]
        denials = [denial for result in results for denial in result.get("denials", [])]
        if denials:
            return {
                "allowed": False,
                "subject": None,
                "subjects": subject_ids,
                "purpose": purpose,
                "operation": operation,
                "denials": denials,
            }

        signals = [signal for result in results for signal in result["signals"]]
        signals.sort(key=lambda item: item["entity_ref"])
        return {
            "allowed": True,
            "subject": None,
            "subjects": subject_ids,
            "purpose": purpose,
            "operation": operation,
            "as_of": max((result.get("as_of") for result in results if result.get("as_of")), default=None),
            "source_commit": source_commit if source_commit is not None else _source_commit(),
            "signals": signals,
        }

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


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_research_signals(
    result: dict[str, Any],
    *,
    generated_at: str | None = None,
    limit: int = 0,
) -> dict[str, Any]:
    """Translate a consent-approved internal result to research-signal-export/v1."""
    if not result.get("allowed"):
        return result
    if limit < 0:
        raise ValueError("limit must be zero or a positive integer")
    signals = list(result.get("signals", []))
    if limit:
        signals = signals[:limit]
    payload = {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": result.get("source_commit"),
        "purpose": result["purpose"],
        "generated_at": generated_at if generated_at is not None else _generated_at(),
        "signal_count": len(signals),
        "signals": signals,
    }
    errors = validate_research_signals(payload)
    if errors:
        raise ValueError("research-signal-export/v1 validation failed: " + "; ".join(errors))
    return payload


def validate_research_signals(payload: dict[str, Any]) -> list[str]:
    contract_version = payload.get("contract_version") if isinstance(payload, dict) else None
    if contract_version != EXPORT_CONTRACT_VERSION:
        raise ValueError(f"Unsupported research-signal-export contract version: {contract_version!r}")
    errors: list[str] = []
    required = ("contract_version", "source_repository", "source_commit", "purpose", "generated_at", "signal_count", "signals")
    errors.extend(f"unknown top-level field: {field}" for field in set(payload) - set(required))
    errors.extend(f"missing required field: {field}" for field in required if field not in payload)
    if payload.get("source_repository") != SOURCE_REPOSITORY:
        errors.append("source_repository must be 'self-model'")
    if not isinstance(payload.get("purpose"), str) or not payload.get("purpose"):
        errors.append("purpose must be a string")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        errors.append("generated_at must be an RFC3339 timestamp")
    else:
        try:
            parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if parsed_generated_at.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("generated_at must be an RFC3339 timestamp")
    if not isinstance(payload.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_commit"))):
        errors.append("source_commit must be a 40-character hexadecimal SHA")
    signal_count = payload.get("signal_count")
    if isinstance(signal_count, bool) or not isinstance(signal_count, int) or signal_count < 0:
        errors.append("signal_count must be a non-negative integer")
    signals = payload.get("signals")
    if not isinstance(signals, list):
        errors.append("signals must be a list")
        return errors
    if isinstance(signal_count, int) and not isinstance(signal_count, bool) and signal_count != len(signals):
        errors.append("signal_count must equal the number of signals")
    allowed_signal_fields = {"entity_ref", "kind", "layer", "statement", "certainty", "evidence_refs"}
    required_signal_fields = allowed_signal_fields
    for index, item in enumerate(signals):
        if not isinstance(item, dict):
            errors.append(f"signals[{index}] must be an object")
            continue
        errors.extend(f"unknown field in signals[{index}]: {field}" for field in set(item) - allowed_signal_fields)
        errors.extend(f"signals[{index}] missing {field}" for field in required_signal_fields if field not in item)
        if not isinstance(item.get("entity_ref"), str):
            errors.append(f"signals[{index}].entity_ref must be a string")
        if item.get("kind") not in {"claim", "pattern"}:
            errors.append(f"signals[{index}].kind is outside the closed vocabulary")
        if not isinstance(item.get("layer"), (str, type(None))):
            errors.append(f"signals[{index}].layer must be a string or null")
        if not isinstance(item.get("statement"), (str, type(None))):
            errors.append(f"signals[{index}].statement must be a string or null")
        if item.get("certainty") not in {"unknown", "low", "medium", "high"}:
            errors.append(f"signals[{index}].certainty is outside the closed vocabulary")
        if not isinstance(item.get("evidence_refs"), list) or not item.get("evidence_refs") or not all(isinstance(ref, str) for ref in item.get("evidence_refs", [])):
            errors.append(f"signals[{index}].evidence_refs must be a non-empty list of strings")
        elif len(item["evidence_refs"]) != len(set(item["evidence_refs"])):
            errors.append(f"signals[{index}].evidence_refs must contain unique references")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject")
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--operation", default="export-signals")
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or a positive integer")
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
        contract = build_research_signals(result, limit=args.limit)
    except ValueError as error:
        print(f"Export denied: {error}", file=sys.stderr)
        return 1
    output = canonical_json(contract)
    if args.output:
        try:
            Path(args.output).write_text(output, encoding="utf-8")
        except OSError as error:
            print(f"Export failed: {error}", file=sys.stderr)
            return 1
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
