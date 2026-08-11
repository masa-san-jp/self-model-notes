#!/usr/bin/env python3
"""Build a versioned, evidence-traceable derived Self Model snapshot."""

from __future__ import annotations

import argparse
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
        "scope": meta.get("scope"),
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
    }


def _observation_record(entity: Entity) -> dict[str, Any]:
    meta = entity.meta
    context = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    return {
        "entity_ref": entity.id,
        "observed_facts": _list_value(meta, "observed_facts"),
        "actions": _list_value(meta, "action"),
        "immediate_outcomes": _list_value(meta, "immediate_outcome"),
        "delayed_outcomes": _list_value(meta, "delayed_outcome"),
        "context": {
            "domains": _list_value(context, "domains"),
            "social": _list_value(context, "social"),
            "uncertainty": context.get("uncertainty"),
            "control": context.get("control"),
        },
        "source_refs": _refs(meta, "source_refs"),
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

    grouped = {field: [] for field in MODEL_FIELDS}
    layer_to_field = {
        "emotion": "emotions",
        "motivation": "motivations",
        "behavioral-principle": "behavioral_principles",
        "tension": "tensions",
    }
    for claim in claims:
        field = layer_to_field.get(claim["layer"])
        if field:
            grouped[field].append(claim)
    grouped["patterns"] = patterns

    unknowns = [
        {
            "entity_ref": statement["entity_ref"],
            "reason": "confidence is unknown",
            "evidence_refs": statement["evidence_refs"],
        }
        for statement in [*claims, *patterns]
        if statement["confidence"] == "unknown"
    ]
    unknowns.sort(key=lambda item: item["entity_ref"])

    return {
        "schema_version": 1,
        "subject": subject_id,
        "as_of": _as_of(selected),
        "source_commit": source_commit if source_commit is not None else current_source_commit(),
        "derived_from": sorted(entity.id for entity in selected),
        "observations": observations,
        "measurements": measurements,
        **grouped,
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
