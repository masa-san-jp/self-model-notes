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


EXPORT_CONTRACT = "research-signal-export/v1"
EXPORT_REPOSITORY = "self-model"
ADAPTER_VERSION = "1.0.0"
# 本人の長期的な傾向は半年で見直す（アートリサーチ仕様書 9.2 の鮮度表）
REVALIDATE_DAYS = 180
# このKBの確度語彙 → 境界の確度語彙
CERTAINTY_LEVEL = {"high": "observed", "medium": "inferred", "low": "uncertain", "unknown": "unknown"}
GROUP_FIELDS = ("seeks", "protects", "avoids", "tensions", "recurring_patterns", "traits", "states", "contexts")


def _statements(groups: dict, field: str) -> list[str]:
    """境界は文字列の並びを受け取る。記録ごとの確度と出所は塊側に残す。"""
    return [item["statement"] for item in groups.get(field, []) if isinstance(item, dict) and item.get("statement")]


def _group_of(signal: dict[str, Any]) -> str:
    """境界の欄のうち、この記録が属するもの。build_research_signals の振り分けと同じ規則。"""
    if signal.get("kind") == "pattern":
        return "recurring_patterns"
    return {
        "motivation": "seeks",
        "behavioral-principle": "protects",
        "tension": "tensions",
    }.get(signal.get("layer"), "traits")


def build_signal_export(result: dict[str, Any], generated_at: str | None = None) -> dict[str, Any]:
    """同意済みの派生情報を、記録1件ごとに書き出す。

    以前は subject 1件を記録1件に畳んでいた。受け取る側は記録ごとに出所と確度を
    求めるのに、畳むと3件の主張が1つの出所と1つの確度になり、**候補空間では自己像が
    常に同じ1本**になっていた。畳まずに、主張・パターンの単位で出す。

    値は新しく作らない。置き場所を変えるだけなのは以前と同じ。
    """
    grouped = build_research_signals(result)
    if "research_signals" not in grouped:
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
    groups = grouped["research_signals"]
    subject = grouped["subject"]
    as_of = grouped.get("as_of")
    stamp = generated_at or datetime.now(timezone(timedelta(hours=9))).replace(microsecond=0).isoformat()
    checked = f"{as_of}T00:00:00+09:00" if as_of else stamp
    revalidate = (datetime.fromisoformat(checked) + timedelta(days=REVALIDATE_DAYS)).isoformat()

    by_ref = {}
    for field in GROUP_FIELDS:
        for item in groups.get(field, []) or []:
            if isinstance(item, dict) and item.get("entity_ref"):
                by_ref[item["entity_ref"]] = (field, item)

    records: list[dict[str, Any]] = []
    for signal in sorted(result.get("signals", []), key=lambda item: str(item.get("entity_ref"))):
        entity_ref = str(signal.get("entity_ref"))
        field, item = by_ref.get(entity_ref, (_group_of(signal), None))
        statement = (item or signal).get("statement")
        if not statement:
            continue
        certainty = (item or signal).get("certainty", "unknown")
        evidence_refs = list((item or signal).get("evidence_refs") or [])
        unknowns = []
        if not evidence_refs:
            unknowns.append("この記録を支える証拠は entities 側に記録されていない")
        if not groups.get("raw_voice_refs"):
            unknowns.append("本人の生の発話は記録されていない（この書き出しには元から含めない）")
        records.append({
            "signal_id": f"self:{entity_ref}",
            "commit": grouped["source_commit"],
            "entity_id": entity_ref,
            "source_locator": f"entities/{entity_ref}.md",
            "evidence_locator": f"entities/{entity_ref}.md#evidence",
            "evidence_kind": "derived",
            "statement": statement,
            "certainty": {
                "level": CERTAINTY_LEVEL.get(certainty, "unknown"),
                "basis": f"このKBの確度は {certainty!r}。記録ごとの出所は entities 側に残す",
            },
            "unknowns": unknowns or ["この記録の未取得事項は entities 側にある"],
            "constraints": [
                f"{grouped['purpose']} の目的内でのみ利用する",
                "同意の範囲を超えて再利用しない",
                "生の発話を復元しない",
            ],
            "validity": {"status": "valid", "checked_at": checked},
            "freshness": {"status": "current", "retrieved_at": checked, "revalidate_at": revalidate},
            "generated_at": stamp,
            "adapter_version": ADAPTER_VERSION,
            "consent_scope": f"{grouped['purpose']}/{result['operation']}",
            "export_permitted": True,
            "raw_voice_locator": f"self-model://{subject}/raw-voice",
            **{group: ([statement] if group == field else []) for group in GROUP_FIELDS},
        })

    return {
        "contract_version": EXPORT_CONTRACT,
        "source_repository": EXPORT_REPOSITORY,
        "source_commit": grouped["source_commit"],
        "purpose": grouped["purpose"],
        "generated_at": stamp,
        "signal_count": len(records),
        "signals": records,
    }


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
        subjects = [args.subject] if args.subject else sorted(
            entity.id for entity in entities if entity.id.startswith("subject/")
        )
        if not subjects:
            print("Export denied: no subject entity found", file=sys.stderr)
            return 1
        results = [export_signals(entities, subject, args.purpose, operation=args.operation) for subject in subjects]
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
    payload["signal_count"] = len(payload["signals"])
    print(canonical_json(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
