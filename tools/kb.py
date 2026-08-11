#!/usr/bin/env python3
"""Shared loader and baseline validation for the Self Model KB.

Issue #1 and docs/schema.md define behavior. This module intentionally implements
only rules already decided there; later execution tasks extend it with tests first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTITY_ROOT = ROOT / "entities"
CONFIG_ROOT = ROOT / "config"
ID_RE = re.compile(r"^(subject|source|event|claim|pattern|measurement)/[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Entity:
    path: Path
    meta: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))


@dataclass(frozen=True)
class ValidationError:
    path: str
    field: str
    message: str
    remediation: str

    def __str__(self) -> str:
        return f"{self.path}: {self.field}: {self.message} ({self.remediation})"


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value or {}


def parse_markdown(path: Path) -> Entity:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing opening YAML frontmatter delimiter")
    try:
        raw_meta, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("missing closing YAML frontmatter delimiter") from exc
    meta = yaml.safe_load(raw_meta)
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return Entity(path=path, meta=meta, body=body)


def discover_entities(root: Path = ENTITY_ROOT) -> list[Entity]:
    entities: list[Entity] = []
    for path in sorted(root.glob("*/*.md")):
        if path.name == "README.md":
            continue
        entities.append(parse_markdown(path))
    return entities


def vocabularies() -> dict[str, Any]:
    return load_yaml(CONFIG_ROOT / "vocabularies.yaml")


def _error(entity: Entity, field: str, message: str, remediation: str) -> ValidationError:
    try:
        path = str(entity.path.relative_to(ROOT))
    except ValueError:
        path = str(entity.path)
    return ValidationError(path, field, message, remediation)


def _require(entity: Entity, fields: Iterable[str]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for field in fields:
        if field not in entity.meta:
            errors.append(_error(entity, field, "required field is missing", f"add `{field}` per docs/schema.md"))
    return errors


def validate_entities(entities: list[Entity]) -> list[ValidationError]:
    vocab = vocabularies()
    plurals = vocab["plural_paths"]
    by_id: dict[str, Entity] = {}
    errors: list[ValidationError] = []

    for entity in entities:
        errors += _require(entity, ["id", "type", "created", "updated"])
        if not ID_RE.fullmatch(entity.id):
            errors.append(_error(entity, "id", "invalid entity ID", "use <type>/<kebab-case-slug>"))
        if entity.id in by_id:
            errors.append(_error(entity, "id", "duplicate entity ID", "choose one canonical entity file"))
        by_id[entity.id] = entity
        if entity.type not in vocab["entity_types"]:
            errors.append(_error(entity, "type", "unknown entity type", "use config/vocabularies.yaml"))
            continue
        expected = Path("entities") / plurals[entity.type] / f"{entity.id.split('/', 1)[-1]}.md"
        try:
            actual = entity.path.relative_to(ROOT)
        except ValueError:
            actual = entity.path
        if actual != expected:
            errors.append(_error(entity, "id", f"ID/path mismatch; expected {expected}", "move or rename the file"))
        for field in ("created", "updated"):
            value = entity.meta.get(field)
            if value is not None and not isinstance(value, (str, date)):
                errors.append(_error(entity, field, "must be an ISO date string", "quote the ISO 8601 value"))

        if entity.type == "subject":
            errors += _require(entity, ["pseudonym", "allowed_purposes", "prohibited_purposes", "consent_refs"])
        elif entity.type == "source":
            errors += _require(entity, ["subject", "source_kind", "locator", "consent"])
            if entity.meta.get("source_kind") not in vocab["source_kinds"]:
                errors.append(_error(entity, "source_kind", "unknown source kind", "use config/vocabularies.yaml"))
        elif entity.type == "event":
            errors += _require(entity, ["subject", "time", "context", "trigger", "observed_facts", "raw_voice", "action", "immediate_outcome", "source_refs"])
        elif entity.type == "claim":
            errors += _require(entity, ["subject", "layer", "scope", "statement", "supporting_evidence", "counterevidence", "alternative_explanations", "confidence", "status"])
            if entity.meta.get("layer") not in vocab["claim_layers"]:
                errors.append(_error(entity, "layer", "unknown claim layer", "use config/vocabularies.yaml"))
            alternatives = entity.meta.get("alternative_explanations", [])
            if not isinstance(alternatives, list) or len(alternatives) < 2:
                errors.append(_error(entity, "alternative_explanations", "at least two alternatives are required", "add materially different explanations"))
            if not entity.meta.get("supporting_evidence"):
                errors.append(_error(entity, "supporting_evidence", "claim has no evidence", "reference at least one Event"))
        elif entity.type == "pattern":
            errors += _require(entity, ["subject", "condition", "recurring_appraisal", "recurring_drive", "recurring_action", "reinforcement", "contexts_seen", "evidence", "claim_refs", "counterevidence", "confidence", "status"])
            evidence = entity.meta.get("evidence", [])
            if not isinstance(evidence, list) or len(set(evidence)) < 2:
                errors.append(_error(entity, "evidence", "pattern requires multiple distinct Events", "keep it as a Claim until repeated"))
        elif entity.type == "measurement":
            errors += _require(entity, ["subject", "instrument", "source_ref", "scores"])
            scores = entity.meta.get("scores") or {}
            instrument = entity.meta.get("instrument") or {}
            if scores and (not instrument.get("official") or not instrument.get("scoring_reference")):
                errors.append(_error(entity, "scores", "numeric scores require an official instrument and scoring reference", "remove inferred scores or add formal metadata"))

    errors += validate_references(entities, by_id)
    return errors


def _refs(entity: Entity) -> list[tuple[str, str]]:
    meta = entity.meta
    fields: list[str]
    if entity.type == "subject":
        fields = ["consent_refs"]
    elif entity.type == "event":
        fields = ["subject", "source_refs"]
    elif entity.type == "claim":
        fields = ["subject", "supporting_evidence", "counterevidence", "supersedes", "superseded_by"]
    elif entity.type == "pattern":
        fields = ["subject", "evidence", "claim_refs", "counterevidence"]
    elif entity.type == "measurement":
        fields = ["subject", "source_ref", "interpretation_claim_refs"]
    elif entity.type == "source":
        fields = ["subject"]
    else:
        fields = []
    result: list[tuple[str, str]] = []
    for field in fields:
        value = meta.get(field)
        values = value if isinstance(value, list) else [value]
        for ref in values:
            if isinstance(ref, str) and ID_RE.fullmatch(ref):
                result.append((field, ref))
    return result


def validate_references(entities: list[Entity], by_id: dict[str, Entity] | None = None) -> list[ValidationError]:
    by_id = by_id or {e.id: e for e in entities}
    errors: list[ValidationError] = []
    for entity in entities:
        for field, ref in _refs(entity):
            target = by_id.get(ref)
            if target is None:
                errors.append(_error(entity, field, f"missing reference {ref}", "create the referenced entity or remove the reference"))
                continue
            subject = entity.meta.get("subject")
            target_subject = target.id if target.type == "subject" else target.meta.get("subject")
            if subject and target_subject and subject != target_subject:
                errors.append(_error(entity, field, f"subject mismatch for {ref}", "reference evidence belonging to the same Subject"))
    return errors


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

