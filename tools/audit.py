#!/usr/bin/env python3
"""Soft epistemic audit: report review questions without changing validation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities
except ModuleNotFoundError:  # Imported as tools.audit by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities


def _list_refs(meta: dict[str, Any], field: str) -> list[str]:
    value = meta.get(field)
    if isinstance(value, list):
        return sorted({item for item in value if isinstance(item, str)})
    if isinstance(value, str):
        return [value]
    return []


def _selected(entities, subject: str | None):
    if not subject:
        return list(entities)
    return [entity for entity in entities if entity.id == subject or entity.meta.get("subject") == subject]


def _event_context(event) -> tuple[set[str], set[str]]:
    context = event.meta.get("context")
    if not isinstance(context, dict):
        return set(), set()
    domains = {item for item in context.get("domains", []) or [] if isinstance(item, str)}
    social = {item for item in context.get("social", []) or [] if isinstance(item, str)}
    return domains, social


def _event_time(event) -> str | None:
    value = event.meta.get("time")
    if not isinstance(value, dict):
        return None
    observed_at = value.get("observed_at")
    if observed_at is None:
        return None
    if hasattr(observed_at, "isoformat"):
        return observed_at.isoformat()
    return str(observed_at)


def _finding(entity: str, code: str, observed: str, next_check: str, **extra: Any) -> dict[str, Any]:
    return {
        "severity": "review",
        "entity": entity,
        "code": code,
        "observed": observed,
        "next_check": next_check,
        **extra,
    }


def findings(entities, subject=None):
    selected = _selected(entities, subject)
    events = [entity for entity in selected if entity.type == "event"]
    claims = [entity for entity in selected if entity.type == "claim"]
    patterns = [entity for entity in selected if entity.type == "pattern"]
    event_by_id = {event.id: event for event in events}
    subject_entity = next((entity for entity in selected if entity.type == "subject"), None)
    audit_entity = subject_entity.id if subject_entity else (subject or "repository")
    result: list[dict[str, Any]] = []

    if len(events) >= 2:
        source_refs = {ref for event in events for ref in _list_refs(event.meta, "source_refs")}
        if len(source_refs) == 1:
            result.append(
                _finding(
                    audit_entity,
                    "SOURCE_CONCENTRATION",
                    f"{len(events)} Events currently point to one Source.",
                    "Collect a comparable observation from another permitted Source before generalizing.",
                    source_refs=sorted(source_refs),
                )
            )

        dimensions: list[str] = []
        domain_values = {value for event in events for value in _event_context(event)[0]}
        social_values = {value for event in events for value in _event_context(event)[1]}
        if domain_values and len(domain_values) == 1:
            dimensions.append("domains")
        if social_values and len(social_values) == 1:
            dimensions.append("social")
        if dimensions:
            result.append(
                _finding(
                    audit_entity,
                    "CONTEXT_BIAS",
                    f"Event context has no observed variation in: {', '.join(dimensions)}.",
                    "Collect an observation in a different context dimension and compare the action or outcome.",
                    dimensions=dimensions,
                )
            )

    for entity in [*claims, *patterns]:
        counterevidence = _list_refs(entity.meta, "counterevidence")
        if not counterevidence:
            result.append(
                _finding(
                    entity.id,
                    "NO_COUNTEREVIDENCE",
                    "No counterevidence reference is recorded.",
                    "Search for an event under the same condition with a different action or outcome.",
                )
            )

    for claim in claims:
        if claim.meta.get("scope") not in ("trait-candidate", "trait"):
            continue
        evidence_events = [event_by_id[ref] for ref in _list_refs(claim.meta, "supporting_evidence") if ref in event_by_id]
        times = {_event_time(event) for event in evidence_events if _event_time(event) is not None}
        domains = {value for event in evidence_events for value in _event_context(event)[0]}
        social = {value for event in evidence_events for value in _event_context(event)[1]}
        missing: list[str] = []
        if len(times) < 2:
            missing.append("temporal breadth")
        if len(domains) < 2 and len(social) < 2:
            missing.append("contextual breadth")
        if missing:
            result.append(
                _finding(
                    claim.id,
                    "TRAIT_BREADTH_REVIEW",
                    f"Trait-scoped Claim lacks {', '.join(missing)}.",
                    "Keep the Claim provisional and collect observations at another time and in another context.",
                    missing=missing,
                    evidence_refs=_list_refs(claim.meta, "supporting_evidence"),
                )
            )

    result.sort(key=lambda item: (item["entity"], item["code"], item.get("dimensions", []), item.get("missing", [])))
    return result


def audit_report(entities, subject=None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "subject": subject,
        "policy": "soft review; findings propose observations and do not make diagnostic conclusions",
        "findings": findings(entities, subject),
    }


def audit_json(entities, subject=None) -> str:
    """Render the canonical audit artifact without writing it."""
    return canonical_json(audit_report(entities, subject))


def audit_artifact_is_current(path: Path, expected: str) -> bool:
    """Return whether an audit artifact exists and exactly matches expected JSON."""
    return path.is_file() and path.read_text(encoding="utf-8") == expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    entities = discover_entities()
    report = audit_report(entities, args.subject)
    if args.dry_run:
        print(canonical_json(report), end="")
        return 0
    path = ROOT / "data" / "audit.json"
    if args.check:
        expected = canonical_json(report)
        if not audit_artifact_is_current(path, expected):
            print(
                "data/audit.json is stale; run .venv/bin/python tools/audit.py",
                file=sys.stderr,
            )
            print("stale generated file: data/audit.json", file=sys.stderr)
            return 1
        print("OK: data/audit.json is current")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(audit_json(entities, args.subject), encoding="utf-8")
    print(f"built {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
