#!/usr/bin/env python3
"""Consent-gated, raw-voice-free research signal export."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, canonical_json, discover_entities, validate_entities
    from profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        path_is_within,
        profile_root_error,
        resolve_profile_root,
    )
except ModuleNotFoundError:  # Imported as tools.export_signals by the test suite.
    from tools.kb import ROOT, canonical_json, discover_entities, validate_entities
    from tools.profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        path_is_within,
        profile_root_error,
        resolve_profile_root,
    )


RESEARCH_SIGNALS_SCHEMA = "urn:self-model-notes:research-signals:v1"
SOURCE_REPOSITORY = "masa-san-jp/self-model-notes"
SIGNAL_ID_RE = re.compile(r"^[a-z0-9]+(?:[._:-][a-z0-9]+)*$")


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
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ""


def _worktree_is_dirty(root: Path = ROOT) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            text=True,
            capture_output=True,
        )
    except OSError:
        return False
    if result.returncode != 0 and "not a git repository" in result.stderr.lower():
        return False
    return result.returncode != 0 or bool(result.stdout.strip())


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
        if entity.type == "claim" and entity.meta.get("status") != "rejected" and entity.meta.get("superseded_by") is None:
            result.append(
                {
                    "entity_ref": entity.id,
                    "kind": "claim",
                    "layer": entity.meta.get("layer"),
                    "statement": entity.meta.get("statement"),
                    "certainty": entity.meta.get("confidence", "unknown"),
                    "motivation_direction": entity.meta.get("motivation_direction"),
                    "scope": entity.meta.get("scope"),
                    "conditions": _list_refs(entity.meta, "conditions"),
                    "evidence_refs": _list_refs(entity.meta, "supporting_evidence"),
                }
            )
        elif entity.type == "pattern" and entity.meta.get("status") != "rejected":
            result.append(
                {
                    "entity_ref": entity.id,
                    "kind": "pattern",
                    "layer": "pattern",
                    "statement": entity.meta.get("condition"),
                    "certainty": entity.meta.get("confidence", "unknown"),
                    "scope": None,
                    "contexts_seen": _list_refs(entity.meta, "contexts_seen"),
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
    root: Path = ROOT,
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

    if source_commit is None and _worktree_is_dirty(root):
        return {
            "allowed": False,
            "subject": subject,
            "purpose": purpose,
            "operation": operation,
            "denials": [_denial("repository", "worktree.clean", "Working tree is dirty; export requires a clean committed state.")],
        }
    resolved_commit = source_commit if source_commit is not None else _source_commit(root)
    if not resolved_commit:
        return {
            "allowed": False,
            "subject": subject,
            "purpose": purpose,
            "operation": operation,
            "denials": [_denial("repository", "commit.available", "A 40-character HEAD commit is required for export.")],
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
        "source_commit": resolved_commit,
        "signals": _signals(selected),
    }


def _signal_item(signal: dict[str, Any]) -> dict[str, Any]:
    certainty = _certainty_value(signal)
    return {
        "entity_ref": signal["entity_ref"],
        "statement": signal["statement"],
        "certainty": certainty,
        "evidence_refs": signal["evidence_refs"],
    }


def _certainty_value(signal: dict[str, Any]) -> str:
    certainty = signal.get("certainty")
    return certainty if isinstance(certainty, str) and certainty in {"unknown", "low", "medium", "high"} else "unknown"


def _domain_group(signal: dict[str, Any]) -> str | None:
    if signal.get("kind") == "pattern":
        return "recurring_patterns"
    if signal.get("layer") == "tension":
        return "tensions"
    if signal.get("layer") == "emotion":
        return "emotional_material"
    if signal.get("layer") != "motivation":
        return None
    return {
        "seek": "seeks",
        "protect": "protects",
        "avoid": "avoids",
    }.get(signal.get("motivation_direction"))


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
        group = _domain_group(signal)
        if group:
            signal_groups[group].append(item)
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


EXPORT_CONTRACT = "research-signal-export/v1"
EXPORT_REPOSITORY = "self-model"
ADAPTER_VERSION = "1.0.0"
# 本人の長期的な傾向は半年で見直す（アートリサーチ仕様書 9.2 の鮮度表）
REVALIDATE_DAYS = 180
# このKBの確度語彙 → 境界の確度語彙
CERTAINTY_LEVEL = {"high": "inferred", "medium": "inferred", "low": "uncertain", "unknown": "unknown"}
GROUP_FIELDS = ("seeks", "protects", "avoids", "tensions", "recurring_patterns", "traits", "states", "contexts")
FIXED_SIGNAL_FIELDS = (
    "signal_id",
    "repository",
    "commit",
    "entity_id",
    "source_locator",
    "evidence_locator",
    "evidence_kind",
    "statement",
    "certainty",
    "unknowns",
    "constraints",
    "validity",
    "freshness",
    "generated_at",
    "adapter_version",
    "consent_scope",
    "export_permitted",
    "seeks",
    "protects",
    "avoids",
    "tensions",
    "recurring_patterns",
    "raw_voice_locator",
    "traits",
    "states",
    "contexts",
)
FIXED_CONSTRAINT = "Use only for artistic-research under approved-derived-only consent."


def _statements(groups: dict, field: str) -> list[str]:
    """境界は文字列の並びを受け取る。記録ごとの確度と出所は塊側に残す。"""
    return [item["statement"] for item in groups.get(field, []) if isinstance(item, dict) and item.get("statement")]


def _group_of(signal: dict[str, Any]) -> str:
    """境界の欄のうち、この記録が属するもの。build_research_signals の振り分けと同じ規則。"""
    return _domain_group(signal) or "traits"


def _signal_groups(signal: dict[str, Any], statement: Any) -> dict[str, list[Any]]:
    groups = {field: [] for field in GROUP_FIELDS}
    domain = _domain_group(signal)
    if domain in groups and isinstance(statement, str):
        groups[domain].append(statement)
    scope = signal.get("scope")
    if scope == "trait" and isinstance(statement, str):
        groups["traits"].append(statement)
    elif scope == "state" and isinstance(statement, str):
        groups["states"].append(statement)
    elif scope == "context-bound":
        values = signal.get("conditions")
        groups["contexts"].extend(value for value in values or [] if isinstance(value, str))
    elif signal.get("kind") == "pattern":
        groups["contexts"].extend(value for value in signal.get("contexts_seen", []) if isinstance(value, str))
    return {field: sorted(set(values)) for field, values in groups.items()}


def _signal_unknowns(signal: dict[str, Any]) -> list[str]:
    unknowns = []
    certainty = _certainty_value(signal)
    if certainty in {"low", "unknown"}:
        unknowns.append(f"confidence is {certainty}")
    direction = signal.get("motivation_direction")
    if signal.get("layer") == "motivation" and direction in {"mixed", "unknown"}:
        unknowns.append(f"motivation direction is {direction}")
    if signal.get("scope") == "trait-candidate":
        unknowns.append("trait remains a candidate")
    return sorted(set(unknowns))


def _signal_constraints(signal: dict[str, Any]) -> list[str]:
    constraints = [FIXED_CONSTRAINT]
    if signal.get("kind") == "claim":
        constraints.extend(f"Condition: {value}" for value in signal.get("conditions", []) if isinstance(value, str))
    elif signal.get("kind") == "pattern":
        constraints.extend(f"Context: {value}" for value in signal.get("contexts_seen", []) if isinstance(value, str))
    return sorted(set(constraints))


def build_signal_export(result: dict[str, Any], generated_at: str | None = None) -> dict[str, Any]:
    """同意済みの派生情報を、記録1件ごとに書き出す。

    以前は subject 1件を記録1件に畳んでいた。受け取る側は記録ごとに出所と確度を
    求めるのに、畳むと3件の主張が1つの出所と1つの確度になり、**候補空間では自己像が
    常に同じ1本**になっていた。畳まずに、主張・パターンの単位で出す。

    値は新しく作らない。置き場所を変えるだけなのは以前と同じ。
    """
    if not result.get("allowed"):
        # Consent was refused. The refusal is the answer; do not shape it into
        # an export that merely happens to be empty.
        return {
            "contract_version": EXPORT_CONTRACT,
            "source_repository": EXPORT_REPOSITORY,
            "source_commit": result.get("source_commit"),
            "purpose": result.get("purpose"),
            "generated_at": generated_at or datetime.now(timezone(timedelta(hours=9))).replace(microsecond=0).isoformat(),
            "signal_count": 0,
            "signals": [],
        }
    source_commit = result.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("signal export requires a 40-character source commit")
    stamp = generated_at or datetime.now(timezone(timedelta(hours=9))).replace(microsecond=0).isoformat()
    checked = stamp

    records: list[dict[str, Any]] = []
    for signal in sorted(result.get("signals", []), key=lambda item: str(item.get("entity_ref"))):
        entity_ref = str(signal.get("entity_ref"))
        statement = signal.get("statement")
        certainty = _certainty_value(signal)
        domain_groups = _signal_groups(signal, statement)
        kind_label = "Claim" if signal.get("kind") == "claim" else "Pattern"
        records.append({
            "signal_id": f"self:{entity_ref.replace('/', ':')}",
            "repository": EXPORT_REPOSITORY,
            "commit": source_commit,
            "entity_id": entity_ref,
            "source_locator": f"self-model://{entity_ref}",
            "evidence_locator": f"self-model://{entity_ref}#evidence",
            "evidence_kind": "derived",
            "statement": statement,
            "certainty": {
                "level": CERTAINTY_LEVEL.get(certainty, "unknown"),
                "basis": f"Derived {kind_label} {entity_ref} with confidence {certainty}.",
            },
            "unknowns": _signal_unknowns(signal),
            "constraints": _signal_constraints(signal),
            "validity": {"status": "valid", "checked_at": checked},
            "freshness": {"status": "unknown", "retrieved_at": checked, "revalidate_at": checked},
            "generated_at": stamp,
            "adapter_version": ADAPTER_VERSION,
            "consent_scope": "approved-derived-only",
            "export_permitted": True,
            "raw_voice_locator": f"self-model://{entity_ref}#raw-voice-not-exported",
            **domain_groups,
        })

    payload = {
        "contract_version": EXPORT_CONTRACT,
        "source_repository": EXPORT_REPOSITORY,
        "source_commit": source_commit,
        "purpose": result.get("purpose"),
        "generated_at": stamp,
        "signal_count": len(records),
        "signals": records,
    }
    errors = validate_signal_export(payload)
    if errors:
        raise ValueError("signal export validation failed: " + "; ".join(errors))
    return payload


def validate_signal_export(payload: dict[str, Any]) -> list[str]:
    required = {"contract_version", "source_repository", "source_commit", "purpose", "generated_at", "signal_count", "signals"}
    errors: list[str] = []
    errors.extend(f"unknown top-level field: {field}" for field in set(payload) - required)
    errors.extend(f"missing required field: {field}" for field in required if field not in payload)
    if payload.get("contract_version") != EXPORT_CONTRACT:
        errors.append("contract_version must be research-signal-export/v1")
    if payload.get("source_repository") != EXPORT_REPOSITORY:
        errors.append("source_repository must be self-model")
    if not isinstance(payload.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_commit"))):
        errors.append("source_commit must be a 40-character hexadecimal SHA")
    if not isinstance(payload.get("purpose"), str):
        errors.append("purpose must be a string")
    if not isinstance(payload.get("generated_at"), str):
        errors.append("generated_at must be a timestamp string")
    signals = payload.get("signals")
    if not isinstance(signals, list):
        return [*errors, "signals must be a list"]
    if payload.get("signal_count") != len(signals):
        errors.append("signal_count must equal len(signals)")
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    for index, record in enumerate(signals):
        prefix = f"signals[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{prefix} must be an object")
            continue
        errors.extend(f"unknown field in {prefix}: {field}" for field in set(record) - set(FIXED_SIGNAL_FIELDS))
        errors.extend(f"missing field in {prefix}: {field}" for field in set(FIXED_SIGNAL_FIELDS) - set(record))
        signal_id = record.get("signal_id")
        entity_id = record.get("entity_id")
        expected_signal_id = f"self:{entity_id.replace('/', ':')}" if isinstance(entity_id, str) else None
        if not isinstance(signal_id, str) or SIGNAL_ID_RE.fullmatch(signal_id) is None or signal_id != expected_signal_id:
            errors.append(f"{prefix}.signal_id is invalid")
        elif signal_id in seen_ids:
            errors.append(f"{prefix}.signal_id is not unique")
        else:
            seen_ids.add(signal_id)
        if not isinstance(entity_id, str):
            errors.append(f"{prefix}.entity_id must be a string")
        elif entity_id in seen_entities:
            errors.append(f"{prefix}.entity_id is not unique")
        else:
            seen_entities.add(entity_id)
        if record.get("repository") != EXPORT_REPOSITORY:
            errors.append(f"{prefix}.repository must be self-model")
        if record.get("commit") != payload.get("source_commit"):
            errors.append(f"{prefix}.commit must match source_commit")
        expected_locators = {
            "source_locator": f"self-model://{entity_id}",
            "evidence_locator": f"self-model://{entity_id}#evidence",
            "raw_voice_locator": f"self-model://{entity_id}#raw-voice-not-exported",
        }
        for field, expected in expected_locators.items():
            if record.get(field) != expected:
                errors.append(f"{prefix}.{field} must be the fixed opaque locator")
        if record.get("evidence_kind") != "derived":
            errors.append(f"{prefix}.evidence_kind must be derived")
        if not isinstance(record.get("statement"), (str, type(None))):
            errors.append(f"{prefix}.statement must be a string or null")
        certainty = record.get("certainty")
        if not isinstance(certainty, dict) or set(certainty) - {"level", "basis"} or certainty.get("level") not in {"observed", "inferred", "uncertain", "unknown"} or not isinstance(certainty.get("basis"), str):
            errors.append(f"{prefix}.certainty is invalid")
        for field in ("unknowns", "constraints"):
            values = record.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                errors.append(f"{prefix}.{field} must be a list of strings")
        for field in ("validity", "freshness"):
            values = record.get(field)
            if not isinstance(values, dict):
                errors.append(f"{prefix}.{field} must be an object")
        validity = record.get("validity", {})
        if isinstance(validity, dict) and (set(validity) != {"status", "checked_at"} or validity.get("status") != "valid" or validity.get("checked_at") != record.get("generated_at")):
            errors.append(f"{prefix}.validity is invalid")
        freshness = record.get("freshness", {})
        if isinstance(freshness, dict) and (set(freshness) != {"status", "retrieved_at", "revalidate_at"} or freshness.get("status") != "unknown" or freshness.get("retrieved_at") != record.get("generated_at") or freshness.get("revalidate_at") != record.get("generated_at")):
            errors.append(f"{prefix}.freshness is invalid")
        if record.get("generated_at") != payload.get("generated_at"):
            errors.append(f"{prefix}.generated_at must match envelope generated_at")
        if record.get("adapter_version") != ADAPTER_VERSION:
            errors.append(f"{prefix}.adapter_version is invalid")
        if record.get("consent_scope") != "approved-derived-only" or record.get("export_permitted") is not True:
            errors.append(f"{prefix} consent fields are invalid")
        for field in GROUP_FIELDS:
            values = record.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                errors.append(f"{prefix}.{field} must be a list of strings")
    return errors


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
    parser.add_argument("--subject", help="省略時はこのKBの全 subject を出す")
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--operation", default="export-signals")
    parser.add_argument("--limit", type=int, default=0, help="正数なら出力record数を制限し、0は無制限")
    parser.add_argument("--output", type=Path, help="成功時のJSON出力先。省略時はstdout")
    add_profile_root_argument(parser)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or positive")
    if args.profile_root is None:
        profile_root_error(
            ProfileRootError(
                "PROFILE_ROOT_REQUIRED",
                "pass --profile-root for real profile reads and exports; repository fallback is disabled",
            )
        )
        return 2
    try:
        layout = resolve_profile_root(args.profile_root)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2
    if args.subject is not None and args.subject not in layout.subject_ids:
        profile_root_error(
            ProfileRootError(
                "PROFILE_SUBJECT_NOT_DECLARED",
                "choose a subject_id declared by the external profile contract",
            )
        )
        return 2
    if args.output is not None and not path_is_within(args.output, layout.data_root):
        profile_root_error(
            ProfileRootError(
                "PROFILE_OUTPUT_INVALID",
                "--output must be inside the external profile data/ directory",
            )
        )
        return 2
    entities = discover_entities(layout.entity_root)
    validation_errors = validate_entities(entities, root=layout.root)
    if validation_errors:
        result = {
            "allowed": False,
            "subject": args.subject,
            "purpose": args.purpose,
            "operation": args.operation,
            "denials": [_denial("repository", "structural-validation", "Entity validation failed; export is blocked.")],
        }
    else:
        subjects = [args.subject] if args.subject else list(layout.subject_ids)
        if not subjects:
            print("Export denied: no subject entity found", file=sys.stderr)
            return 1
        source_commit = _source_commit(ROOT)
        results = [
            export_signals(
                entities,
                subject,
                args.purpose,
                operation=args.operation,
                source_commit=source_commit,
                root=layout.root,
            )
            for subject in subjects
        ]
        denied = next((item for item in results if not item["allowed"]), None)
        result = denied if denied is not None else results[0]
    if not result["allowed"]:
        print("Export denied", file=sys.stderr)
        print(canonical_json(result), file=sys.stderr, end="")
        return 1
    try:
        payloads = [build_signal_export(item) for item in results]
    except ValueError as error:
        print(f"Export denied: {error}", file=sys.stderr)
        return 1
    payload = payloads[0]
    payload["signals"] = [record for item in payloads for record in item["signals"]]
    if args.limit:
        payload["signals"] = payload["signals"][:args.limit]
    payload["signal_count"] = len(payload["signals"])
    serialized = canonical_json(payload)
    if args.output:
        atomic_write_text(args.output, serialized)
        print(f"wrote {args.output.resolve().relative_to(layout.root)}")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
