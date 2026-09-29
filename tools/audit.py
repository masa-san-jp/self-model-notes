#!/usr/bin/env python3
"""Soft epistemic audit: report review questions without changing validation."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities, load_yaml
    from export_signals import _consent_denials
    from profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        profile_root_error,
        resolve_profile_root,
    )
except ModuleNotFoundError:  # Imported as tools.audit by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities, load_yaml
    from tools.export_signals import _consent_denials
    from tools.profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        profile_root_error,
        resolve_profile_root,
    )


TENSION_AXES_PATH = ROOT / "config" / "tension-axes.yaml"
TENSION_AXIS_FIELD = "axis"
UNCLASSIFIED_TENSION_AXIS = "unclassified"


def load_tension_axes() -> dict[str, Any]:
    """Load the closed internal vocabulary and bias policy for tension Claims."""

    value = load_yaml(TENSION_AXES_PATH)
    if not isinstance(value, dict) or value.get("contract_version") != "tension-axes/v1":
        raise ValueError("config/tension-axes.yaml must declare tension-axes/v1")
    axes = value.get("axes")
    if not isinstance(axes, list) or not axes or any(not isinstance(axis, str) or not axis for axis in axes):
        raise ValueError("config/tension-axes.yaml axes must be a non-empty list of strings")
    if len(set(axes)) != len(axes) or UNCLASSIFIED_TENSION_AXIS in axes:
        raise ValueError("config/tension-axes.yaml axes must be unique and reserve unclassified")
    bias = value.get("bias")
    if not isinstance(bias, dict):
        raise ValueError("config/tension-axes.yaml bias must be a mapping")
    minimum_claims = bias.get("minimum_claims")
    maximum_share = bias.get("maximum_majority_share")
    minimum_distinct = bias.get("minimum_distinct_axes")
    if not isinstance(minimum_claims, int) or minimum_claims < 1:
        raise ValueError("bias.minimum_claims must be a positive integer")
    if not isinstance(maximum_share, (int, float)) or not 0 < maximum_share <= 1:
        raise ValueError("bias.maximum_majority_share must be between 0 and 1")
    if not isinstance(minimum_distinct, int) or minimum_distinct < 1:
        raise ValueError("bias.minimum_distinct_axes must be a positive integer")
    return {"axes": tuple(axes), "bias": dict(bias)}


def _active_tension_claims(entities, subject: str | None = None) -> list[Any]:
    selected = _selected(entities, subject)
    return [
        entity
        for entity in selected
        if entity.type == "claim"
        and entity.meta.get("layer") == "tension"
        and entity.meta.get("status") != "rejected"
        and entity.meta.get("superseded_by") is None
    ]


def tension_axis_summary(entities, subject: str | None = None) -> dict[str, Any]:
    """Count explicit tension axes without inferring from wording or identifiers."""

    config = load_tension_axes()
    allowed = set(config["axes"])
    claims = _active_tension_claims(entities, subject)
    axis_counts = {axis: 0 for axis in config["axes"]}
    axis_counts[UNCLASSIFIED_TENSION_AXIS] = 0
    invalid_claims: list[str] = []
    for claim in claims:
        value = claim.meta.get(TENSION_AXIS_FIELD)
        if value in allowed:
            axis_counts[value] += 1
        else:
            axis_counts[UNCLASSIFIED_TENSION_AXIS] += 1
            if value is not None:
                invalid_claims.append(claim.id)

    classified = {axis: count for axis, count in axis_counts.items() if axis != UNCLASSIFIED_TENSION_AXIS and count}
    classified_total = sum(classified.values())
    majority_axis = max(classified, key=lambda axis: (classified[axis], axis)) if classified else None
    majority_count = classified.get(majority_axis, 0) if majority_axis else 0
    majority_share = majority_count / classified_total if classified_total else 0.0
    bias = config["bias"]
    biased = (
        classified_total >= bias["minimum_claims"]
        and (majority_share > bias["maximum_majority_share"] or len(classified) < bias["minimum_distinct_axes"])
    )
    return {
        "total_claims": len(claims),
        "classified_claims": classified_total,
        "unclassified_claims": axis_counts[UNCLASSIFIED_TENSION_AXIS],
        "axis_counts": {axis: axis_counts[axis] for axis in (*config["axes"], UNCLASSIFIED_TENSION_AXIS) if axis_counts[axis]},
        "distinct_axes": len(classified),
        "majority_axis": majority_axis,
        "majority_count": majority_count,
        "majority_share": majority_share,
        "biased": biased,
        "invalid_claims": sorted(invalid_claims),
        "thresholds": {
            "minimum_claims": bias["minimum_claims"],
            "maximum_majority_share": bias["maximum_majority_share"],
            "minimum_distinct_axes": bias["minimum_distinct_axes"],
        },
    }


def _claim_source_refs(claim, entities) -> list[str]:
    by_id = {entity.id: entity for entity in entities}
    refs = set(_list_refs(claim.meta, "source_refs"))
    for evidence_ref in _list_refs(claim.meta, "supporting_evidence"):
        evidence = by_id.get(evidence_ref)
        if evidence is not None:
            refs.update(_list_refs(evidence.meta, "source_refs"))
            if evidence.type == "source":
                refs.add(evidence.id)
    return sorted(refs)


def claim_consent_status(
    claim,
    entities,
    *,
    purpose: str = "artistic-research",
    operation: str = "export-signals",
    today: date | None = None,
) -> dict[str, Any]:
    """Evaluate a Claim's downstream consent through its evidence graph.

    This deliberately delegates field-level consent rules to export_signals;
    the Claim layer only resolves evidence Events to their Source references.
    """

    today = today or date.today()
    source_refs = _claim_source_refs(claim, entities)
    by_id = {entity.id: entity for entity in entities}
    denials: list[dict[str, str]] = []
    if not source_refs:
        denials.append({"source": claim.id, "rule": "evidence.sources", "message": "No evidence Source is available for consent checks."})
    for source_ref in source_refs:
        source = by_id.get(source_ref)
        if source is None or source.type != "source":
            denials.append({"source": source_ref, "rule": "evidence.source_exists", "message": "Evidence Source is missing."})
            continue
        denials.extend(_consent_denials(source, purpose, operation, today))
    return {"claim": claim.id, "source_refs": source_refs, "allowed": bool(source_refs) and not denials, "denials": denials}


def consent_status_for_claim(claim, entities, **kwargs: Any) -> dict[str, Any]:
    """Stable descriptive alias for callers that do not use the shorter name."""

    return claim_consent_status(claim, entities, **kwargs)


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

    axis_summary = tension_axis_summary(selected)
    if axis_summary["invalid_claims"]:
        result.append(
            _finding(
                audit_entity,
                "TENSION_AXIS_INVALID",
                "One or more tension Claims use an axis outside the closed internal vocabulary.",
                "Record an explicit axis from the configured vocabulary or leave it unclassified.",
                claims=axis_summary["invalid_claims"],
            )
        )
    if axis_summary["biased"]:
        result.append(
            _finding(
                audit_entity,
                "TENSION_AXIS_BIAS",
                "Tension Claims are concentrated in one axis.",
                "Ask one tension question that can reveal a different axis before generalizing.",
                axis_counts=axis_summary["axis_counts"],
                distinct_axes=axis_summary["distinct_axes"],
                majority_axis=axis_summary["majority_axis"],
                majority_share=axis_summary["majority_share"],
            )
        )

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
    report = {
        "schema_version": 1,
        "subject": subject,
        "policy": "soft review; findings propose observations and do not make diagnostic conclusions",
        "findings": findings(entities, subject),
    }
    axis_summary = tension_axis_summary(entities, subject)
    if axis_summary["total_claims"]:
        report["tension_axes"] = axis_summary
        selected = _selected(entities, subject)
        report["tension_consent"] = [
            claim_consent_status(claim, selected)
            for claim in _active_tension_claims(selected)
        ]
    return report


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
    add_profile_root_argument(parser)
    args = parser.parse_args()
    if args.profile_root is not None:
        try:
            layout = resolve_profile_root(args.profile_root)
        except ProfileRootError as error:
            profile_root_error(error)
            return 2
        output_root = layout.root
        entity_root = layout.entity_root
    elif args.dry_run or args.check:
        # Read-only compatibility for the pre-migration tracked artifact.
        output_root = ROOT
        entity_root = ROOT / "entities"
    else:
        profile_root_error(
            ProfileRootError(
                "PROFILE_ROOT_REQUIRED",
                "pass --profile-root for real profile reads and writes; repository fallback is read-only check mode",
            )
        )
        return 2
    entities = discover_entities(entity_root)
    report = audit_report(entities, args.subject)
    if args.dry_run:
        print(canonical_json(report), end="")
        return 0
    path = output_root / "data" / "audit.json"
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
    atomic_write_text(path, audit_json(entities, args.subject))
    try:
        display = path.relative_to(output_root)
    except ValueError:
        display = Path("<profile-output>")
    print(f"built {display}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
