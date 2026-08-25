#!/usr/bin/env python3
"""Build a versioned, evidence-traceable derived Self Model snapshot."""

from __future__ import annotations

import argparse
from copy import deepcopy
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, Entity, canonical_json, discover_entities, validate_entities
except ModuleNotFoundError:  # Imported as tools.build_self_model by the test suite.
    from tools.kb import ROOT, Entity, canonical_json, discover_entities, validate_entities


MODEL_FIELDS = (
    "emotions",
    "motivations",
    "behavioral_principles",
    "tensions",
    "patterns",
)

MODEL_SCHEMA_VERSION = 2


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _list_value(meta: dict[str, Any], field: str) -> list[Any]:
    value = meta.get(field)
    if isinstance(value, list):
        return list(value)
    if value is None:
        return []
    return [value]


def _refs(meta: dict[str, Any], field: str) -> list[str]:
    return sorted({value for value in _list_value(meta, field) if isinstance(value, str)})


def _raw_voice_refs(meta: dict[str, Any]) -> list[str]:
    raw_voice = meta.get("raw_voice")
    if not isinstance(raw_voice, list):
        return []
    return sorted(
        {
            item["source_ref"]
            for item in raw_voice
            if isinstance(item, dict) and isinstance(item.get("source_ref"), str)
        }
    )


def current_source_commit(root: Path = ROOT) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def model_path(subject_id: str, root: Path = ROOT) -> Path:
    kind, slug = subject_id.split("/", 1)
    return root / "data" / "self-models" / kind / f"{slug}.json"


def _as_of(entities: list[Entity]) -> str | None:
    dates = [
        normalized
        for entity in entities
        for field in ("updated", "created")
        if (normalized := _iso(entity.meta.get(field))) is not None
    ]
    return max(dates) if dates else None


def _claim_record(entity: Entity) -> dict[str, Any]:
    meta = entity.meta
    return {
        "entity_ref": entity.id,
        "statement": meta.get("statement"),
        "layer": meta.get("layer"),
        "motivation_direction": meta.get("motivation_direction"),
        "scope": meta.get("scope"),
        "conditions": _list_value(meta, "conditions"),
        "confidence": meta.get("confidence"),
        "status": meta.get("status"),
        "evidence_refs": _refs(meta, "supporting_evidence"),
        "counterevidence_refs": _refs(meta, "counterevidence"),
        "alternative_explanations": _list_value(meta, "alternative_explanations"),
        "supersedes": meta.get("supersedes"),
        "superseded_by": meta.get("superseded_by"),
    }


def _pattern_record(entity: Entity) -> dict[str, Any]:
    meta = entity.meta
    return {
        "entity_ref": entity.id,
        "statement": meta.get("condition"),
        "confidence": meta.get("confidence"),
        "status": meta.get("status"),
        "evidence_refs": _refs(meta, "evidence"),
        "claim_refs": _refs(meta, "claim_refs"),
        "counterevidence_refs": _refs(meta, "counterevidence"),
        "contexts_seen": _list_value(meta, "contexts_seen"),
        "recurring_actions": _list_value(meta, "recurring_action"),
        "recurring_drives": _list_value(meta, "recurring_drive"),
        "reinforcement": _list_value(meta, "reinforcement"),
    }


def _observation_record(entity: Entity) -> dict[str, Any]:
    meta = entity.meta
    context = meta.get("context")
    return {
        "entity_ref": entity.id,
        "trigger": deepcopy(meta.get("trigger")),
        "observed_facts": deepcopy(meta.get("observed_facts")),
        "appraisal": deepcopy(meta.get("appraisal")),
        "emotion": deepcopy(meta.get("emotion")),
        "body": deepcopy(meta.get("body")),
        "cognition": deepcopy(meta.get("cognition")),
        "actions": deepcopy(meta.get("action")),
        "immediate_outcomes": deepcopy(meta.get("immediate_outcome")),
        "delayed_outcomes": deepcopy(meta.get("delayed_outcome")),
        "context": deepcopy(context),
        "state": deepcopy(meta.get("state")),
        "source_refs": _refs(meta, "source_refs"),
        "raw_voice_refs": _raw_voice_refs(meta),
    }


def _measurement_record(entity: Entity) -> dict[str, Any]:
    meta = entity.meta
    instrument = meta.get("instrument") if isinstance(meta.get("instrument"), dict) else {}
    return {
        "entity_ref": entity.id,
        "instrument": {
            "name": instrument.get("name"),
            "version": instrument.get("version"),
            "official": instrument.get("official"),
        },
        "administered_at": _iso(meta.get("administered_at")),
        "source_refs": _refs(meta, "source_ref"),
        "scores": meta.get("scores"),
        "interpretation_claim_refs": _refs(meta, "interpretation_claim_refs"),
    }


def _current_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        claim
        for claim in claims
        if claim["status"] != "rejected" and claim["superseded_by"] is None
    ]


def _current_patterns(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pattern for pattern in patterns if pattern["status"] != "rejected"]


def _derived_record(statement: dict[str, Any], value: Any) -> dict[str, Any]:
    return {
        "entity_ref": statement["entity_ref"],
        "statement": deepcopy(value),
        "confidence": statement["confidence"],
        "status": statement["status"],
        "evidence_refs": list(statement["evidence_refs"]),
        "counterevidence_refs": list(statement["counterevidence_refs"]),
    }


def _record_sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["entity_ref"], canonical_json(record["statement"])


def _dominant_triggers(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_derived_record(pattern, pattern["statement"]) for pattern in patterns]


def _dominant_rewards(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for pattern in patterns:
        for reward in pattern["reinforcement"]:
            record = _derived_record(pattern, reward)
            records[(record["entity_ref"], canonical_json(record["statement"]))] = record
    return sorted(records.values(), key=_record_sort_key)


def _context_dependencies(
    claims: list[dict[str, Any]], patterns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for claim in claims:
        if claim["scope"] == "context-bound":
            for condition in claim["conditions"]:
                record = _derived_record(claim, condition)
                record["source_field"] = "conditions"
                records.append(record)
    for pattern in patterns:
        for context in pattern["contexts_seen"]:
            record = _derived_record(pattern, context)
            record["source_field"] = "contexts_seen"
            records.append(record)
    return sorted(records, key=lambda record: (record["entity_ref"], record["source_field"], canonical_json(record["statement"])))


def _direction_records(
    claims: list[dict[str, Any]], direction: str
) -> list[dict[str, Any]]:
    return [
        _derived_record(claim, claim["statement"])
        for claim in claims
        if claim["layer"] == "motivation" and claim["motivation_direction"] == direction
    ]


def _field_unknown(field: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "field-unobserved",
        "field": field,
        "entity_ref": None,
        "reason": reason,
        "evidence_refs": [],
    }


def _unknowns(
    claims: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    dominant_triggers: list[dict[str, Any]],
    dominant_rewards: list[dict[str, Any]],
    avoidance_targets: list[dict[str, Any]],
    protective_factors: list[dict[str, Any]],
    context_dependencies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unknowns: list[dict[str, Any]] = []
    for statement in [*claims, *patterns]:
        if statement["confidence"] == "unknown":
            unknowns.append(
                {
                    "kind": "confidence-unknown",
                    "field": None,
                    "entity_ref": statement["entity_ref"],
                    "reason": "confidence is unknown",
                    "evidence_refs": list(statement["evidence_refs"]),
                }
            )
    for claim in claims:
        direction = claim.get("motivation_direction")
        if claim["layer"] == "motivation" and direction in {"mixed", "unknown"}:
            unknowns.append(
                {
                    "kind": "motivation-direction-unresolved",
                    "field": None,
                    "entity_ref": claim["entity_ref"],
                    "reason": f"motivation_direction is {direction}",
                    "evidence_refs": list(claim["evidence_refs"]),
                }
            )
    if not dominant_triggers:
        unknowns.append(_field_unknown("dominant_triggers", "no current Pattern provides explicit evidence"))
    if not dominant_rewards:
        unknowns.append(_field_unknown("dominant_rewards", "no current Pattern provides explicit reinforcement"))
    if not avoidance_targets:
        unknowns.append(_field_unknown("avoidance_targets", "no current Claim has explicit motivation_direction: avoid"))
    if not protective_factors:
        unknowns.append(_field_unknown("protective_factors", "no current Claim has explicit motivation_direction: protect"))
    if not context_dependencies:
        unknowns.append(_field_unknown("context_dependencies", "no current Claim or Pattern provides explicit context dependency"))
    return sorted(
        unknowns,
        key=lambda item: (item["kind"], item["field"] or "", item["entity_ref"] or ""),
    )


def _evidence_coverage(claims: list[dict[str, Any]], patterns: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, Any]:
    claim_evidence = {ref for claim in claims for ref in claim["evidence_refs"]}
    pattern_evidence = {ref for pattern in patterns for ref in pattern["evidence_refs"]}
    event_refs = claim_evidence | pattern_evidence
    source_refs = {ref for observation in observations for ref in observation["source_refs"]}
    return {
        "claim_count": len(claims),
        "pattern_count": len(patterns),
        "event_refs": sorted(event_refs),
        "source_refs": sorted(source_refs),
    }


def build_model(
    entities: list[Entity],
    subject_id: str,
    *,
    source_commit: str | None = None,
) -> dict[str, Any]:
    selected = [
        entity
        for entity in entities
        if entity.id == subject_id or entity.meta.get("subject") == subject_id
    ]
    claims = [_claim_record(entity) for entity in selected if entity.type == "claim"]
    patterns = [_pattern_record(entity) for entity in selected if entity.type == "pattern"]
    observations = [_observation_record(entity) for entity in selected if entity.type == "event"]
    measurements = [_measurement_record(entity) for entity in selected if entity.type == "measurement"]
    claims.sort(key=lambda item: item["entity_ref"])
    patterns.sort(key=lambda item: item["entity_ref"])
    observations.sort(key=lambda item: item["entity_ref"])
    measurements.sort(key=lambda item: item["entity_ref"])

    current_claims = _current_claims(claims)
    current_patterns = _current_patterns(patterns)
    grouped = {field: [] for field in MODEL_FIELDS}
    layer_to_field = {
        "emotion": "emotions",
        "motivation": "motivations",
        "behavioral-principle": "behavioral_principles",
        "tension": "tensions",
    }
    for claim in current_claims:
        field = layer_to_field.get(claim["layer"])
        if field:
            grouped[field].append(claim)
    grouped["patterns"] = current_patterns

    dominant_triggers = _dominant_triggers(current_patterns)
    dominant_rewards = _dominant_rewards(current_patterns)
    avoidance_targets = _direction_records(current_claims, "avoid")
    protective_factors = _direction_records(current_claims, "protect")
    context_dependencies = _context_dependencies(current_claims, current_patterns)
    unknowns = _unknowns(
        current_claims,
        current_patterns,
        dominant_triggers,
        dominant_rewards,
        avoidance_targets,
        protective_factors,
        context_dependencies,
    )

    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "subject": subject_id,
        "as_of": _as_of(selected),
        "source_commit": source_commit if source_commit is not None else current_source_commit(),
        "derived_from": sorted(entity.id for entity in selected),
        "observations": observations,
        "measurements": measurements,
        **grouped,
        "dominant_triggers": dominant_triggers,
        "dominant_rewards": dominant_rewards,
        "avoidance_targets": avoidance_targets,
        "protective_factors": protective_factors,
        "context_dependencies": context_dependencies,
        "claim_history": claims,
        "unknowns": unknowns,
        "evidence_coverage": _evidence_coverage(claims, patterns, observations),
    }


def write_model(model: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(model), encoding="utf-8")


def _load_valid_entities() -> list[Entity]:
    entities = discover_entities()
    errors = validate_entities(entities)
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        raise SystemExit(1)
    return entities


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    args = parser.parse_args()
    entities = _load_valid_entities()
    if not any(entity.id == args.subject and entity.type == "subject" for entity in entities):
        raise SystemExit(f"Subject not found: {args.subject}")
    model = build_model(entities, args.subject)
    path = model_path(args.subject)
    write_model(model, path)
    print(f"built {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
