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
ENTITY_TYPES = ("subject", "source", "event", "claim", "pattern", "measurement")
ID_RE = re.compile(rf"^(?P<type>{'|'.join(ENTITY_TYPES)})/[a-z0-9]+(?:-[a-z0-9]+)*$")
STATUS_VALUES = ("draft", "active", "revised", "rejected", "archived")

# field -> (allowed target types, is_list)
REFERENCE_SPECS: dict[str, dict[str, tuple[set[str], bool]]] = {
    "subject": {"consent_refs": ({"source"}, True)},
    "source": {"subject": ({"subject"}, False)},
    "event": {"subject": ({"subject"}, False), "source_refs": ({"source"}, True)},
    "claim": {
        "subject": ({"subject"}, False),
        "supporting_evidence": ({"event"}, True),
        "counterevidence": ({"event", "claim"}, True),
        "supersedes": ({"claim"}, False),
        "superseded_by": ({"claim"}, False),
    },
    "pattern": {
        "subject": ({"subject"}, False),
        "evidence": ({"event"}, True),
        "claim_refs": ({"claim"}, True),
        "counterevidence": ({"event", "claim"}, True),
    },
    "measurement": {
        "subject": ({"subject"}, False),
        "source_ref": ({"source"}, False),
        "interpretation_claim_refs": ({"claim"}, True),
    },
}


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
        raise ValueError(f"{path}: missing opening YAML frontmatter delimiter")
    try:
        raw_meta, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: missing closing YAML frontmatter delimiter") from exc
    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    return Entity(path=path, meta=meta, body=body)


def serialize_markdown(entity: Entity) -> str:
    """Serialize an Entity without changing frontmatter meaning or body text."""
    frontmatter = yaml.safe_dump(entity.meta, allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n{entity.body}"


def discover_entities(root: Path = ENTITY_ROOT) -> list[Entity]:
    entities: list[Entity] = []
    for path in sorted(root.glob("*/*.md")):
        if path.name == "README.md":
            continue
        entities.append(parse_markdown(path))
    return entities


def vocabularies() -> dict[str, Any]:
    vocab = load_yaml(CONFIG_ROOT / "vocabularies.yaml")
    vocab["context_dimensions"] = load_yaml(CONFIG_ROOT / "contexts.yaml")["dimensions"]
    vocab["drive_systems"] = load_yaml(CONFIG_ROOT / "drive-systems.yaml")["systems"].keys()
    return vocab


def _error(entity: Entity, field: str, message: str, remediation: str) -> ValidationError:
    try:
        path = str(entity.path.relative_to(ROOT))
    except ValueError:
        # External profile roots must never appear as absolute paths in a
        # validation error.  Keep the canonical protocol-relative suffix.
        parts = entity.path.parts
        try:
            entity_index = max(index for index, part in enumerate(parts) if part == "entities")
        except ValueError:
            path = "<external-entity>"
        else:
            path = str(Path(*parts[entity_index:]))
    return ValidationError(path, field, message, remediation)


def _require(entity: Entity, fields: Iterable[str]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for field in fields:
        if field not in entity.meta:
            errors.append(_error(entity, field, "required field is missing", f"add `{field}` per docs/schema.md"))
    return errors


def _validate_list(entity: Entity, field: str, *, allow_none: bool = True) -> list[ValidationError]:
    if field not in entity.meta:
        return []
    value = entity.meta.get(field)
    if value is None and allow_none:
        return []
    if not isinstance(value, list):
        return [_error(entity, field, "must be a list", f"use a YAML list for `{field}`")]
    return []


def _validate_mapping(entity: Entity, field: str, *, allow_none: bool = False) -> list[ValidationError]:
    if field not in entity.meta:
        return []
    value = entity.meta.get(field)
    if value is None and allow_none:
        return []
    if not isinstance(value, dict):
        return [_error(entity, field, "must be a mapping", f"use a YAML mapping for `{field}`")]
    return []


def _validate_enum(entity: Entity, field: str, allowed: Iterable[str], *, allow_none: bool = False) -> list[ValidationError]:
    if field not in entity.meta:
        return []
    value = entity.meta.get(field)
    if value is None and allow_none:
        return []
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        return [_error(entity, field, f"value {value!r} is outside the closed vocabulary", f"use one of: {allowed_values}")]
    return []


def _validate_enum_list(entity: Entity, field: str, allowed: Iterable[str]) -> list[ValidationError]:
    if field not in entity.meta:
        return []
    errors = _validate_list(entity, field)
    if errors:
        return errors
    if entity.meta[field] is None:
        return []
    allowed_set = set(allowed)
    errors = []
    for value in entity.meta[field]:
        if value not in allowed_set:
            allowed_values = ", ".join(sorted(allowed_set))
            errors.append(_error(entity, field, f"value {value!r} is outside the closed vocabulary", f"use one of: {allowed_values}"))
    return errors


def _validate_dates(entity: Entity) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for field in ("created", "updated"):
        if field not in entity.meta:
            continue
        value = entity.meta[field]
        if value is None:
            errors.append(_error(entity, field, "must be an ISO 8601 date", f"set `{field}` to YYYY-MM-DD"))
        elif isinstance(value, date):
            continue
        elif isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(_error(entity, field, "must be an ISO 8601 date", f"set `{field}` to YYYY-MM-DD"))
        else:
            errors.append(_error(entity, field, "must be an ISO 8601 date", f"set `{field}` to YYYY-MM-DD"))
    return errors


def _validate_entity_fields(entity: Entity, vocab: dict[str, Any]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if entity.type == "subject":
        errors += _validate_list(entity, "consent_refs")
        errors += _validate_enum_list(entity, "allowed_purposes", vocab["allowed_purposes"])
        errors += _validate_enum_list(entity, "prohibited_purposes", vocab["prohibited_purposes"])
        errors += _validate_enum(entity, "status", STATUS_VALUES, allow_none=True)
        if "direct_identifiers_stored" in entity.meta and not isinstance(entity.meta["direct_identifiers_stored"], bool):
            errors.append(_error(entity, "direct_identifiers_stored", "must be boolean", "use true or false"))
    elif entity.type == "source":
        errors += _validate_enum(entity, "source_kind", vocab["source_kinds"])
        errors += _validate_mapping(entity, "consent")
        if "raw_content_stored" in entity.meta and not isinstance(entity.meta["raw_content_stored"], bool):
            errors.append(_error(entity, "raw_content_stored", "must be boolean", "use true or false"))
        consent = entity.meta.get("consent")
        if isinstance(consent, dict):
            for field in ("obtained", "obtained_at", "purposes", "allowed_operations", "expires_at", "revoked_at", "notes"):
                if field not in consent:
                    errors.append(_error(entity, f"consent.{field}", "required field is missing", f"add `consent.{field}` per docs/schema.md"))
            if "obtained" in consent and not isinstance(consent["obtained"], bool):
                errors.append(_error(entity, "consent.obtained", "must be boolean", "use true or false"))
            for field, allowed in (("purposes", vocab["allowed_purposes"]), ("allowed_operations", vocab["allowed_operations"])):
                if field in consent:
                    if not isinstance(consent[field], list):
                        errors.append(_error(entity, f"consent.{field}", "must be a list", f"use a YAML list for `consent.{field}`"))
                    else:
                        for value in consent[field]:
                            if value not in allowed:
                                errors.append(_error(entity, f"consent.{field}", f"value {value!r} is outside the closed vocabulary", "use config/vocabularies.yaml"))
    elif entity.type == "event":
        errors += _validate_mapping(entity, "time")
        errors += _validate_mapping(entity, "context")
        errors += _validate_mapping(entity, "state")
        for field in ("observed_facts", "raw_voice", "appraisal", "emotion", "body", "cognition", "action", "immediate_outcome", "delayed_outcome", "source_refs"):
            errors += _validate_list(entity, field)
        context = entity.meta.get("context")
        if isinstance(context, dict):
            for field in ("domains", "social"):
                if field in context:
                    values = context[field]
                    if not isinstance(values, list):
                        errors.append(_error(entity, f"context.{field}", "must be a list", f"use a YAML list for `context.{field}`"))
                    else:
                        allowed = vocab["context_dimensions"][field]
                        for value in values:
                            if value not in allowed:
                                errors.append(_error(entity, f"context.{field}", f"value {value!r} is outside the closed vocabulary", "use config/contexts.yaml"))
            for field in ("uncertainty", "control"):
                if field in context:
                    errors += _validate_enum_value(entity, f"context.{field}", context[field], vocab["context_dimensions"][field])
    elif entity.type == "claim":
        if "raw_voice" in entity.meta:
            errors.append(_error(entity, "raw_voice", "raw voice belongs on an Event, not a Claim", "move the raw voice to an Event and reference it as evidence"))
        errors += _validate_enum(entity, "layer", vocab["claim_layers"])
        errors += _validate_enum(entity, "scope", vocab["scopes"])
        errors += _validate_enum(entity, "confidence", vocab["confidence"])
        errors += _validate_enum(entity, "status", vocab["claim_statuses"])
        if entity.meta.get("layer") == "motivation":
            errors += _validate_enum(entity, "motivation_direction", vocab["motivation_directions"])
        elif entity.meta.get("motivation_direction") is not None:
            errors.append(
                _error(
                    entity,
                    "motivation_direction",
                    "must be null for non-motivation Claims",
                    "set `motivation_direction: null` outside the motivation layer",
                )
            )
        errors += _validate_list(entity, "conditions")
        errors += _validate_list(entity, "counterevidence")
        errors += _validate_list(entity, "alternative_explanations")
        alternatives = entity.meta.get("alternative_explanations")
        if not isinstance(alternatives, list) or len(alternatives) < 2:
            errors.append(_error(entity, "alternative_explanations", "at least two alternatives are required", "add materially different explanations"))
        errors += _validate_list(entity, "supporting_evidence")
        supporting_evidence = entity.meta.get("supporting_evidence")
        if not isinstance(supporting_evidence, list) or not supporting_evidence:
            errors.append(_error(entity, "supporting_evidence", "claim has no evidence", "reference at least one Event"))
    elif entity.type == "pattern":
        for field in ("recurring_appraisal", "recurring_drive", "recurring_action", "reinforcement", "contexts_seen", "evidence", "claim_refs", "counterevidence"):
            errors += _validate_list(entity, field)
        errors += _validate_enum_list(entity, "recurring_drive", vocab["drive_systems"])
        errors += _validate_enum(entity, "confidence", vocab["confidence"])
        errors += _validate_enum(entity, "status", vocab["claim_statuses"])
        evidence = entity.meta.get("evidence")
        if not isinstance(evidence, list) or len(set(evidence)) < 2:
            errors.append(_error(entity, "evidence", "pattern requires multiple distinct Events", "keep it as a Claim until repeated"))
    elif entity.type == "measurement":
        errors += _validate_mapping(entity, "instrument")
        errors += _validate_mapping(entity, "scores")
        errors += _validate_list(entity, "interpretation_claim_refs")
        instrument = entity.meta.get("instrument")
        if isinstance(instrument, dict) and "official" in instrument and not isinstance(instrument["official"], bool):
            errors.append(_error(entity, "instrument.official", "must be boolean", "use true or false"))
        scores = entity.meta.get("scores") or {}
        if scores and isinstance(instrument, dict) and (not instrument.get("official") or not instrument.get("scoring_reference")):
            errors.append(_error(entity, "scores", "numeric scores require an official instrument and scoring reference", "remove inferred scores or add formal metadata"))
    return errors


def _validate_enum_value(entity: Entity, field: str, value: Any, allowed: Iterable[str]) -> list[ValidationError]:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        return [_error(entity, field, f"value {value!r} is outside the closed vocabulary", f"use one of: {allowed_values}")]
    return []


def validate_entities(entities: list[Entity], root: Path = ROOT) -> list[ValidationError]:
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
        id_match = ID_RE.fullmatch(entity.id)
        if id_match is not None and id_match.group("type") != entity.type:
            errors.append(_error(entity, "id", "ID type prefix does not match frontmatter type", "make the ID prefix and `type` identical"))
        slug = entity.id.split("/", 1)[-1]
        expected = Path("entities") / plurals[entity.type] / f"{slug}.md"
        try:
            actual = entity.path.relative_to(root)
        except ValueError:
            actual = entity.path
        if actual != expected:
            errors.append(_error(entity, "id", f"ID/path mismatch; expected {expected}", "move or rename the file"))
        errors += _validate_dates(entity)
        required_by_type = {
            "subject": ["pseudonym", "direct_identifiers_stored", "consent_refs", "allowed_purposes", "prohibited_purposes", "status"],
            "source": ["subject", "source_kind", "captured_at", "locator", "raw_content_stored", "consent", "reliability_notes"],
            "event": ["subject", "time", "context", "state", "trigger", "observed_facts", "raw_voice", "appraisal", "emotion", "body", "cognition", "action", "immediate_outcome", "delayed_outcome", "source_refs"],
            "claim": ["subject", "layer", "scope", "statement", "conditions", "supporting_evidence", "counterevidence", "alternative_explanations", "confidence", "status", "supersedes", "superseded_by", "motivation_direction"],
            "pattern": ["subject", "condition", "recurring_appraisal", "recurring_drive", "recurring_action", "reinforcement", "contexts_seen", "evidence", "claim_refs", "counterevidence", "confidence", "status"],
            "measurement": ["subject", "instrument", "administered_at", "source_ref", "scores", "interpretation_claim_refs"],
        }
        errors += _require(entity, required_by_type[entity.type])
        errors += _validate_entity_fields(entity, vocab)

    errors += validate_references(entities, by_id)
    return errors


def _refs(entity: Entity) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for field in REFERENCE_SPECS.get(entity.type, {}):
        value = entity.meta.get(field)
        values = value if isinstance(value, list) else [value]
        for ref in values:
            if isinstance(ref, str) and ID_RE.fullmatch(ref):
                result.append((field, ref))
    return result


def _subject_of(entity: Entity) -> str | None:
    return entity.id if entity.type == "subject" else entity.meta.get("subject")


def _reference_values(entity: Entity, field: str, is_list: bool) -> tuple[list[Any], list[ValidationError]]:
    if field not in entity.meta:
        return [], []
    value = entity.meta[field]
    if is_list:
        if value is None:
            return [], []
        if not isinstance(value, list):
            return [], [_error(entity, field, "must be a list of entity IDs", f"use a YAML list for `{field}`")]
        return value, []
    if isinstance(value, list):
        return [], [_error(entity, field, "must be a single entity ID or null", f"use one entity ID for `{field}`")]
    return [value], []


def _supersession_cycles(entities: list[Entity]) -> list[ValidationError]:
    claims = {entity.id: entity for entity in entities if entity.type == "claim"}
    graph: dict[str, set[str]] = {claim_id: set() for claim_id in claims}
    for claim in claims.values():
        for field in ("supersedes", "superseded_by"):
            ref = claim.meta.get(field)
            if not isinstance(ref, str) or ref not in claims:
                continue
            if field == "supersedes":
                graph[claim.id].add(ref)
            else:
                graph[ref].add(claim.id)

    visited: set[str] = set()
    active: list[str] = []
    cycles: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        if node in active:
            cycles.add(frozenset(active[active.index(node):]))
            return
        if node in visited:
            return
        active.append(node)
        for target in graph[node]:
            visit(target)
        active.pop()
        visited.add(node)

    for claim_id in graph:
        visit(claim_id)

    errors: list[ValidationError] = []
    for cycle in cycles:
        claim_id = sorted(cycle)[0]
        errors.append(_error(claims[claim_id], "supersedes", "circular supersession is not allowed", "keep supersession history acyclic"))
    return errors


def validate_references(entities: list[Entity], by_id: dict[str, Entity] | None = None) -> list[ValidationError]:
    by_id = by_id or {e.id: e for e in entities}
    errors: list[ValidationError] = []
    for entity in entities:
        for field, (allowed_types, is_list) in REFERENCE_SPECS.get(entity.type, {}).items():
            values, field_errors = _reference_values(entity, field, is_list)
            errors += field_errors
            for ref in values:
                if ref is None:
                    continue
                if not isinstance(ref, str) or not ID_RE.fullmatch(ref):
                    errors.append(_error(entity, field, f"invalid entity reference {ref!r}", "use a valid <type>/<kebab-case-slug> ID"))
                    continue
                target = by_id.get(ref)
                if target is None:
                    errors.append(_error(entity, field, f"missing reference {ref}", "create the referenced entity or remove the reference"))
                    continue
                if target.type not in allowed_types:
                    expected = ", ".join(sorted(allowed_types))
                    errors.append(_error(entity, field, f"reference {ref} has type {target.type!r}, expected {expected}", "reference an entity of the allowed type"))
                    continue
                owner_subject = _subject_of(entity)
                target_subject = _subject_of(target)
                if owner_subject and target_subject and owner_subject != target_subject:
                    errors.append(_error(entity, field, f"subject mismatch for {ref}", "reference evidence belonging to the same Subject"))
    errors += _supersession_cycles(entities)
    return errors


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
