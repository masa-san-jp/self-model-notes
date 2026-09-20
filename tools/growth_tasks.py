#!/usr/bin/env python3
"""Gap-driven local growth queue (Issue #108).

Growth tasks turn what a consuming run could not get (growth-miss/v1),
what the soft audit flags, what coverage shows as unobserved, and what the
derived Self Model milestones still lack into a small, decomposed local
queue -- entirely inside the external profile root. Nothing here reaches
the public repository or a GitHub Issue: the gaps this computes are about
one person's Self Model, not about the harness.

A production run never blocks on this file. Growth sessions are a
separate, optional activity the operator starts whenever they choose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from kb import (
        ROOT as REPO_ROOT,
        Entity,
        discover_entities,
        load_yaml,
        canonical_json,
        parse_markdown,
        serialize_markdown,
        validate_entities,
    )
    from build_graph import build_coverage
    from audit import findings as audit_findings
    from build_self_model import build_model
    from export_signals import _as_date, _group_misses, build_signal_export, export_signals
    from intake_conversation import (
        MAX_RAW_QUOTE_CHARS,
        IntakeError,
        _list_slot,
        _parse_transcript,
        _validate_event_values,
    )
    from profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        profile_root_error,
        resolve_profile_root,
    )
except ModuleNotFoundError:  # Imported as tools.growth_tasks by the test suite.
    from tools.kb import (
        ROOT as REPO_ROOT,
        Entity,
        discover_entities,
        load_yaml,
        canonical_json,
        parse_markdown,
        serialize_markdown,
        validate_entities,
    )
    from tools.build_graph import build_coverage
    from tools.audit import findings as audit_findings
    from tools.build_self_model import build_model
    from tools.export_signals import _as_date, _group_misses, build_signal_export, export_signals
    from tools.intake_conversation import (
        MAX_RAW_QUOTE_CHARS,
        IntakeError,
        _list_slot,
        _parse_transcript,
        _validate_event_values,
    )
    from tools.profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        profile_root_error,
        resolve_profile_root,
    )

QUEUE_CONTRACT = "growth-queue/v1"
QUESTION_BANK_PATH = REPO_ROOT / "config" / "question-bank.yaml"
MILESTONES_PATH = REPO_ROOT / "config" / "growth-milestones.yaml"
HEARING_CONFIG_PATH = REPO_ROOT / "config" / "hearing.yaml"
STALE_CLAIM_DAYS = 180
CHECK_TIMEOUT_SECONDS = 600

# Hearing (Issue #118): a production-run entry that offers at most one
# growth-queue question per run, explains why before asking, and never
# blocks the run -- every outcome (offered/answered/skipped/unavailable)
# still lets the caller proceed to export_signals.py.
HEARING_LOG_CONTRACT = "growth-hearing/v1"
HEARING_PACKET_CONTRACT = "growth-hearing-packet/v1"
HEARING_SOURCE_KINDS = frozenset({"conversation", "interview"})
HEARING_REQUIRED_OPERATIONS = frozenset({"store-reference", "analyze", "derive", "export-signals"})
HEARING_CONSTRAINTS = (
    "1問だけ",
    "言い換え可・項目追加不可",
    "制作テーマ・依頼文を記録に書かない",
    "答えたくない反応が出たらすぐ skip",
)

# growth-miss section -> (question-bank id, self-model/v2 section)
MISS_SECTION_TARGETS = {
    "seeks": ("rewards", "dominant_rewards"),
    "protects": ("protective", "protective_factors"),
    "avoids": ("avoidance", "avoidance_targets"),
    "tensions": ("tension", "tensions"),
    "recurring_patterns": ("trigger", "dominant_triggers"),
    "contexts": ("context", "context_dependencies"),
}
MILESTONE_SECTIONS = (
    "dominant_triggers",
    "dominant_rewards",
    "avoidance_targets",
    "protective_factors",
    "context_dependencies",
    "tensions",
)
# self-model/v2 section -> question-bank id (reuses the same questions as misses)
SECTION_QUESTION = {section: question_id for question_id, section in MISS_SECTION_TARGETS.values()}


class GrowthTaskError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> GrowthTaskError:
    return GrowthTaskError(code, message)


# ---------------------------------------------------------------- hashing --


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entities_sha256(entity_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(entity_root.rglob("*.md")):
        digest.update(str(path.relative_to(entity_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _misses_sha256(data_root: Path) -> str | None:
    path = data_root / "misses.jsonl"
    if not path.exists():
        return None
    return _sha256_bytes(path.read_bytes())


def _audit_sha256(entities: list[Any]) -> str:
    return _sha256_bytes(canonical_json({"findings": audit_findings(entities)}).encode("utf-8"))


def _coverage_sha256(entities: list[Any]) -> str:
    return _sha256_bytes(canonical_json(build_coverage(entities)).encode("utf-8"))


def queue_path(profile_root: Path) -> Path:
    return Path(profile_root) / "growth" / "queue.yaml"


def _queue_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return _sha256_bytes(path.read_bytes())


# ------------------------------------------------------------- question bank --


def load_question_bank() -> dict[str, dict[str, Any]]:
    data = load_yaml(QUESTION_BANK_PATH)
    return {item["id"]: item for item in data.get("questions", [])}


def question_bank_forbidden_tokens() -> list[str]:
    data = load_yaml(QUESTION_BANK_PATH)
    return list(data.get("forbidden_tokens", []))


# -------------------------------------------------------------------- hearing config --


def load_hearing_config() -> dict[str, Any]:
    return load_yaml(HEARING_CONFIG_PATH)


def hearing_config_text_fields(config: dict[str, Any] | None = None) -> list[str]:
    """Every human-facing string in config/hearing.yaml, for the forbidden_tokens check."""
    config = config if config is not None else load_hearing_config()
    texts = list(config.get("intent", []))
    skip_ack = config.get("skip_ack")
    if skip_ack:
        texts.append(skip_ack)
    texts.extend(config.get("section_reasons", {}).values())
    texts.extend(config.get("slot_reasons", {}).values())
    return texts


# ---------------------------------------------------------------- milestones --


def load_milestones() -> dict[str, Any]:
    data = load_yaml(MILESTONES_PATH)
    return data.get("milestones", {})


def _min_event_contexts_threshold() -> int:
    return int(load_milestones().get("min_event_contexts", {}).get("value", 2))


def _min_event_span_days_threshold() -> int:
    return int(load_milestones().get("min_event_span_days", {}).get("value", 30))


# ------------------------------------------------------------------- gaps --


def _target(*, section: str | None = None, entity: str | None = None, slot: str | None = None) -> dict[str, Any]:
    return {"section": section, "entity": entity, "slot": slot}


def _gap(gap_key: str, kind: str, target: dict[str, Any], question_id: str | None = None) -> dict[str, Any]:
    return {"gap_key": gap_key, "kind": kind, "target": target, "question_id": question_id}


def _requested_subject_purposes(data_root: Path) -> set[tuple[str, str]]:
    path = data_root / "misses.jsonl"
    if not path.exists():
        return set()
    pairs: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("reason") == "denied":
            continue
        subject = record.get("subject")
        purpose = record.get("purpose")
        if isinstance(subject, str) and isinstance(purpose, str):
            pairs.add((subject, purpose))
    return pairs


def _miss_gaps(entities: list[Any], data_root: Path) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for subject, purpose in sorted(_requested_subject_purposes(data_root)):
        # source_commit is a placeholder: gap detection only needs which signal
        # groups would be non-empty, not export provenance. A real export still
        # goes through export_signals.py's own commit/worktree checks.
        result = export_signals(entities, subject, purpose, source_commit="0" * 40)
        if not result.get("allowed"):
            continue
        payload = build_signal_export(result)
        for section, (_reason, _count) in sorted(_group_misses(payload["signals"]).items()):
            if section not in MISS_SECTION_TARGETS:
                continue
            question_id, target_section = MISS_SECTION_TARGETS[section]
            gaps.append(
                _gap(
                    f"miss:{section}:{subject}",
                    "acquire-event",
                    _target(section=target_section),
                    question_id,
                )
            )
    return gaps


def _audit_gaps(entities: list[Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for finding in audit_findings(entities):
        code = finding["code"]
        entity_ref = finding["entity"]
        if code == "NO_COUNTEREVIDENCE":
            gaps.append(_gap(f"audit:NO_COUNTEREVIDENCE:{entity_ref}", "search-counterevidence", _target(entity=entity_ref)))
        elif code in ("SOURCE_CONCENTRATION", "CONTEXT_BIAS"):
            gaps.append(
                _gap(
                    f"audit:{code}:{entity_ref}",
                    "acquire-event",
                    _target(section="context_dependencies"),
                    SECTION_QUESTION["context_dependencies"],
                )
            )
        elif code == "TRAIT_BREADTH_REVIEW":
            gaps.append(_gap(f"audit:TRAIT_BREADTH_REVIEW:{entity_ref}", "derive-claim", _target(entity=entity_ref)))
    return gaps


def _coverage_gaps(entities: list[Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    events = [entity for entity in entities if entity.type == "event"]
    for slot in ("emotion", "body"):
        candidates = [event for event in events if event.meta.get(slot) is None]
        if not candidates:
            continue
        candidates.sort(key=lambda event: str(event.meta.get("time", {}).get("observed_at") or ""))
        target_event = candidates[-1]
        question_id = f"{slot}-slot"
        gaps.append(_gap(f"coverage:event.{slot}:{target_event.id}", "acquire-event", _target(entity=target_event.id, slot=slot), question_id))

    claims = [entity for entity in entities if entity.type == "claim"]
    for claim in claims:
        if claim.meta.get("layer") != "motivation":
            continue
        evidence = claim.meta.get("supporting_evidence")
        if isinstance(evidence, list) and len(evidence) == 1:
            gaps.append(
                _gap(
                    f"coverage:pattern-precondition:{claim.id}",
                    "acquire-event",
                    _target(section="dominant_triggers"),
                    SECTION_QUESTION["dominant_triggers"],
                )
            )
    return gaps


def _stale_claim_gaps(entities: list[Any], now: datetime) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for entity in entities:
        if entity.type != "claim":
            continue
        updated = entity.meta.get("updated")
        if updated is None:
            continue
        if hasattr(updated, "isoformat") and not isinstance(updated, datetime):
            updated_dt = datetime(updated.year, updated.month, updated.day, tzinfo=timezone.utc)
        elif isinstance(updated, str):
            try:
                updated_dt = datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        else:
            continue
        if now - updated_dt > timedelta(days=STALE_CLAIM_DAYS):
            gaps.append(_gap(f"staleness:{entity.id}", "refresh-claim", _target(entity=entity.id)))
    return gaps


def sections_supported_status(entities: list[Any]) -> dict[str, bool]:
    """Whether each self-model/v2 section has >=1 supported, counterevidence-checked record.

    Judged entirely by the existing Claim/Pattern status and counterevidence
    fields (docs/schema.md); no new confidence or promotion threshold.
    """
    subjects = sorted({entity.id for entity in entities if entity.type == "subject"})
    satisfied = {section: False for section in MILESTONE_SECTIONS}
    for subject in subjects:
        model = build_model(entities, subject, source_commit="0" * 40)
        for section in MILESTONE_SECTIONS:
            records = model.get(section) or []
            if any(record.get("status") == "supported" and record.get("counterevidence_refs") for record in records):
                satisfied[section] = True
    return satisfied


def event_context_domain_count(entities: list[Any]) -> int:
    domains: set[str] = set()
    for entity in entities:
        if entity.type != "event":
            continue
        context = entity.meta.get("context")
        if isinstance(context, dict):
            domains.update(value for value in context.get("domains", []) or [] if isinstance(value, str))
    return len(domains)


def event_span_days(entities: list[Any]) -> int | None:
    times: list[datetime] = []
    for entity in entities:
        if entity.type != "event":
            continue
        time = entity.meta.get("time")
        if not isinstance(time, dict):
            continue
        observed_at = time.get("observed_at")
        parsed = None
        if hasattr(observed_at, "isoformat") and not isinstance(observed_at, datetime):
            parsed = datetime(observed_at.year, observed_at.month, observed_at.day, tzinfo=timezone.utc)
        elif isinstance(observed_at, datetime):
            parsed = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
        elif isinstance(observed_at, str):
            try:
                parsed = datetime.fromisoformat(observed_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                parsed = None
        if parsed is not None:
            times.append(parsed)
    if len(times) < 2:
        return 0
    return (max(times) - min(times)).days


def _milestone_gaps(entities: list[Any]) -> list[dict[str, Any]]:
    # Only sections_supported feeds task generation: it is the one dimension
    # with a natural single-section acquire-event target. min_event_contexts
    # and min_event_span_days are profile-wide, not section-scoped; they are
    # surfaced in `report` (overviews/growth.md) rather than spawning a task
    # of their own -- the CONTEXT_BIAS audit finding already covers the same
    # ground with a concrete Event target.
    gaps: list[dict[str, Any]] = []
    satisfied = sections_supported_status(entities)
    for section in sorted(section for section, ok in satisfied.items() if not ok):
        gaps.append(_gap(f"milestone:{section}", "acquire-event", _target(section=section), SECTION_QUESTION[section]))
    return gaps


def compute_gaps(entities: list[Any], data_root: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    ordered = [
        *_miss_gaps(entities, data_root),
        *_audit_gaps(entities),
        *_coverage_gaps(entities),
        *_stale_claim_gaps(entities, now),
        *_milestone_gaps(entities),
    ]
    seen_identities: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for gap in ordered:
        if gap["kind"] == "acquire-event":
            identity = ("acquire-event", json.dumps(gap["target"], sort_keys=True))
        else:
            identity = (gap["kind"], gap["target"].get("entity") or "")
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        deduped.append(gap)
    return deduped


# ------------------------------------------------------------------ tasks --


def _task_checks(profile_root: Path) -> list[str]:
    # Build (not --check): a growth task may be the profile's first write, so
    # there is no prior generated data/graph.json to compare against. This
    # step is the structural-validity gate; --check freshness is a separate,
    # normal-operation concern (docs/operations.md).
    # shlex.quote: a real profile root can contain spaces (e.g. under
    # "Application Support"); an unquoted path would be split apart by
    # _run_checks' shlex.split and silently corrupted.
    return [f"python3 tools/agent_runtime.py tools/build_graph.py --profile-root {shlex.quote(str(profile_root))}"]


def _allowed_paths(kind: str, target: dict[str, Any]) -> list[str]:
    if kind == "acquire-event":
        return ["entities/events/*.md"] if target.get("entity") is None else [f"entities/events/{target['entity'].split('/', 1)[1]}.md"]
    entity = target.get("entity")
    slug = entity.split("/", 1)[1] if entity else "*"
    plural = {"claim": "claims"}.get(entity.split("/", 1)[0] if entity else "claim", "claims")
    return [f"entities/{plural}/{slug}.md"]


def _new_task(task_id: str, gap: dict[str, Any], profile_root: Path) -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": gap["kind"],
        "status": "ready",
        "gap_key": gap["gap_key"],
        "target": gap["target"],
        "question": load_question_bank()[gap["question_id"]]["text"] if gap["question_id"] else None,
        "question_id": gap["question_id"],
        "allowed_paths": _allowed_paths(gap["kind"], gap["target"]),
        "checks": _task_checks(profile_root),
        "claim": None,
        "evidence": None,
    }


def build_queue(profile_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    errors = validate_entities(entities, root=layout.root)
    if errors:
        raise _error("ENTITIES_INVALID", "; ".join(str(error) for error in errors))
    data_root = layout.data_root
    gaps = compute_gaps(entities, data_root, now=now)

    existing_path = queue_path(profile_root)
    existing_tasks: dict[str, dict[str, Any]] = {}
    max_seq = 0
    if existing_path.exists():
        existing = load_yaml(existing_path)
        for task in existing.get("tasks", []):
            existing_tasks[task["gap_key"]] = task
            try:
                max_seq = max(max_seq, int(task["id"].split("-", 1)[1]))
            except (KeyError, ValueError, IndexError):
                pass

    kept_in_progress_or_done = [
        task for task in existing_tasks.values() if task["status"] in ("in-progress", "done")
    ]
    kept_gap_keys = {task["gap_key"] for task in kept_in_progress_or_done}

    tasks: list[dict[str, Any]] = list(kept_in_progress_or_done)
    for gap in gaps:
        if gap["gap_key"] in kept_gap_keys:
            continue
        if gap["gap_key"] in existing_tasks:
            # A still-ready gap keeps only its id (Issue #108). checks and
            # allowed_paths are recomputed fresh every time, the same as a
            # brand-new task -- they are pure functions of the gap and the
            # profile root, never customized by hand, so silently freezing
            # a stale value (e.g. a fixed bug) would be worse than recomputing.
            tasks.append(_new_task(existing_tasks[gap["gap_key"]]["id"], gap, profile_root))
            continue
        max_seq += 1
        tasks.append(_new_task(f"GT-{max_seq:04d}", gap, profile_root))

    tasks.sort(key=lambda task: task["id"])
    queue = {
        "contract_version": QUEUE_CONTRACT,
        "generated_from": {
            "misses_sha256": _misses_sha256(data_root),
            "audit_sha256": _audit_sha256(entities),
            "coverage_sha256": _coverage_sha256(entities),
            "entities_sha256": _entities_sha256(layout.entity_root),
        },
        "tasks": tasks,
    }
    return queue


def _dump_yaml(value: Any) -> str:
    import yaml as _yaml

    return _yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        import os

        os.fsync(handle.fileno())
    temp.replace(path)


def generate(profile_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    path = queue_path(profile_root)
    if path.exists():
        current = load_yaml(path)
        if any(task["status"] == "in-progress" for task in current.get("tasks", [])):
            raise _error("QUEUE_BUSY", "a growth task is in-progress; complete or reset it before regenerating")
    queue = build_queue(profile_root, now=now)
    _atomic_write(path, _dump_yaml(queue))
    return queue


def next_task(profile_root: Path) -> dict[str, Any]:
    path = queue_path(profile_root)
    if not path.exists():
        return {"queue_sha256": None, "task": None}
    queue = load_yaml(path)
    ready = sorted(
        (task for task in queue.get("tasks", []) if task["status"] == "ready"),
        key=lambda task: task["id"],
    )
    return {"queue_sha256": _queue_sha256(path), "task": ready[0] if ready else None}


def _load_queue_checked(profile_root: Path, expected_queue_sha256: str | None) -> tuple[Path, dict[str, Any]]:
    path = queue_path(profile_root)
    if not path.exists():
        raise _error("QUEUE_NOT_FOUND", "no growth queue exists; run generate first")
    current_sha = _queue_sha256(path)
    if expected_queue_sha256 is not None and expected_queue_sha256 != current_sha:
        raise _error("QUEUE_CONFLICT", f"queue changed since last read; current sha256 is {current_sha}")
    return path, load_yaml(path)


def claim_growth_task(profile_root: Path, task_id: str, *, actor: str, expected_queue_sha256: str | None) -> dict[str, Any]:
    path, queue = _load_queue_checked(profile_root, expected_queue_sha256)
    tasks = queue.get("tasks", [])
    for task in tasks:
        if task["id"] != task_id:
            continue
        if task["status"] != "ready":
            raise _error("TASK_NOT_READY", f"{task_id} is not ready")
        task["status"] = "in-progress"
        task["claim"] = {"actor": actor, "claimed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "queue_sha256": expected_queue_sha256}
        _atomic_write(path, _dump_yaml(queue))
        return {"task": task, "queue_sha256": _queue_sha256(path)}
    raise _error("TASK_NOT_FOUND", f"{task_id} is not registered")


def _run_checks(checks: list[str], *, repo: Path) -> list[dict[str, Any]]:
    results = []
    for command in checks:
        argv = shlex.split(command)
        try:
            completed = subprocess.run(argv, cwd=repo, shell=False, timeout=CHECK_TIMEOUT_SECONDS, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise _error("CHECK_FAILED", f"check failed to run: {command}") from error
        results.append({"command": command, "exit_code": completed.returncode})
        if completed.returncode != 0:
            raise _error("CHECK_FAILED", f"check failed ({completed.returncode}): {command}")
    return results


def _verify_acquire_event(profile_root: Path, task: dict[str, Any], before_events: set[str]) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    events = {entity.id: entity for entity in entities if entity.type == "event"}
    target = task["target"]
    if target.get("slot") is None:
        new_ids = sorted(set(events) - before_events)
        if len(new_ids) != 1:
            raise _error("EVENT_ACQUISITION_INVALID", "exactly one new Event is required")
        new_event = events[new_ids[0]]
        source_refs = new_event.meta.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            raise _error("EVENT_ACQUISITION_INVALID", "the new Event has no source_refs")
        raw_voice = new_event.meta.get("raw_voice") or []
        for item in raw_voice:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and len(text) > 120:
                raise _error("EVENT_ACQUISITION_INVALID", "raw_voice exceeds 120 characters")
        return {"entities": [new_ids[0]]}
    entity_id = target["entity"]
    slot = target["slot"]
    if entity_id not in events:
        raise _error("EVENT_ACQUISITION_INVALID", "target Event no longer exists")
    if events[entity_id].meta.get(slot) is None:
        raise _error("EVENT_ACQUISITION_INVALID", f"{slot} is still null")
    return {"entities": [entity_id]}


def _verify_derive_claim(profile_root: Path, task: dict[str, Any], before_claims: dict[str, dict[str, Any]]) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    claims = {entity.id: entity for entity in entities if entity.type == "claim"}
    target_id = task["target"]["entity"]
    revised = [
        entity_id
        for entity_id, entity in claims.items()
        if entity_id not in before_claims or entity.meta != before_claims.get(entity_id)
    ]
    if not revised:
        raise _error("CLAIM_DERIVATION_INVALID", "no Claim was added or revised")
    for entity_id in revised:
        meta = claims[entity_id].meta
        alternatives = meta.get("alternative_explanations")
        if not isinstance(alternatives, list) or len(alternatives) < 2:
            raise _error("CLAIM_DERIVATION_INVALID", "alternative_explanations must have at least two entries")
        evidence = meta.get("supporting_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise _error("CLAIM_DERIVATION_INVALID", "supporting_evidence must be non-empty")
        if meta.get("status") not in ("hypothesis", "revised"):
            raise _error("CLAIM_DERIVATION_INVALID", "status must be hypothesis or revised")
    return {"entities": revised, "source_claim": target_id}


def _verify_search_counterevidence(profile_root: Path, task: dict[str, Any]) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    claim_id = task["target"]["entity"]
    claim = next((entity for entity in entities if entity.id == claim_id), None)
    if claim is None:
        raise _error("COUNTEREVIDENCE_SEARCH_INVALID", "target Claim no longer exists")
    if claim.meta.get("counterevidence") is None:
        raise _error("COUNTEREVIDENCE_SEARCH_INVALID", "counterevidence is still null")
    if "反証探索" not in claim.body:
        raise _error("COUNTEREVIDENCE_SEARCH_INVALID", "body must document the search under a 反証探索 heading")
    return {"entities": [claim_id]}


def _verify_refresh_claim(profile_root: Path, task: dict[str, Any], before_updated: str | None) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    claim_id = task["target"]["entity"]
    claim = next((entity for entity in entities if entity.id == claim_id), None)
    if claim is None:
        raise _error("CLAIM_REFRESH_INVALID", "target Claim no longer exists")
    updated = claim.meta.get("updated")
    updated_str = updated.isoformat() if hasattr(updated, "isoformat") else str(updated)
    if updated_str == before_updated:
        raise _error("CLAIM_REFRESH_INVALID", "updated was not advanced")
    if claim.meta.get("status") not in ("supported", "revised", "rejected", "hypothesis"):
        raise _error("CLAIM_REFRESH_INVALID", "status is outside the closed vocabulary")
    return {"entities": [claim_id]}


def complete_growth_task(
    profile_root: Path,
    task_id: str,
    *,
    expected_queue_sha256: str | None,
    before_snapshot: dict[str, Any],
) -> dict[str, Any]:
    path, queue = _load_queue_checked(profile_root, expected_queue_sha256)
    task = next((item for item in queue["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise _error("TASK_NOT_FOUND", f"{task_id} is not registered")
    if task["status"] != "in-progress":
        raise _error("TASK_NOT_IN_PROGRESS", f"{task_id} is not in-progress")

    checks_run = _run_checks(task["checks"], repo=REPO_ROOT)

    kind = task["kind"]
    if kind == "acquire-event":
        details = _verify_acquire_event(profile_root, task, before_snapshot.get("event_ids", set()))
    elif kind == "derive-claim":
        details = _verify_derive_claim(profile_root, task, before_snapshot.get("claims", {}))
    elif kind == "search-counterevidence":
        details = _verify_search_counterevidence(profile_root, task)
    elif kind == "refresh-claim":
        details = _verify_refresh_claim(profile_root, task, before_snapshot.get("claim_updated"))
    else:
        raise _error("TASK_KIND_INVALID", f"unknown task kind: {kind}")

    task["status"] = "done"
    task["claim"] = None
    task["evidence"] = {
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "queue_sha256": expected_queue_sha256,
        "entities": details.get("entities", []),
        "checks": [f"{item['command']} — exit {item['exit_code']}" for item in checks_run],
    }
    _atomic_write(path, _dump_yaml(queue))
    return {"task": task, "queue_sha256": _queue_sha256(path)}


def snapshot_before(profile_root: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Capture the pre-work state complete_growth_task needs to verify a change happened."""
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    kind = task["kind"]
    if kind == "acquire-event":
        return {"event_ids": {entity.id for entity in entities if entity.type == "event"}}
    if kind == "derive-claim":
        return {"claims": {entity.id: dict(entity.meta) for entity in entities if entity.type == "claim"}}
    if kind == "refresh-claim":
        claim = next((entity for entity in entities if entity.id == task["target"]["entity"]), None)
        updated = claim.meta.get("updated") if claim else None
        updated_str = updated.isoformat() if hasattr(updated, "isoformat") else (str(updated) if updated is not None else None)
        return {"claim_updated": updated_str}
    return {}


# ------------------------------------------------------------------ report --


def growth_report_path(profile_root: Path) -> Path:
    return Path(profile_root) / "overviews" / "growth.md"


def build_growth_report(profile_root: Path) -> str:
    layout = resolve_profile_root(profile_root)
    entities = discover_entities(layout.entity_root)
    satisfied = sections_supported_status(entities)
    context_count = event_context_domain_count(entities)
    span_days = event_span_days(entities)
    context_threshold = _min_event_contexts_threshold()
    span_threshold = _min_event_span_days_threshold()
    next_result = next_task(profile_root)

    lines = [
        "# Growth",
        "",
        "<!-- generated by tools/growth_tasks.py report; do not edit -->",
        "",
        "## sections_supported",
        "",
        "| section | satisfied |",
        "|---|---|",
    ]
    for section in MILESTONE_SECTIONS:
        lines.append(f"| {section} | {'yes' if satisfied[section] else 'no'} |")
    lines += [
        "",
        "## min_event_contexts",
        "",
        f"- observed domains: {context_count}",
        f"- threshold: {context_threshold}",
        f"- satisfied: {'yes' if context_count >= context_threshold else 'no'}",
        "",
        "## min_event_span_days",
        "",
        f"- observed span (days): {span_days if span_days is not None else 'unknown'}",
        f"- threshold: {span_threshold}",
        f"- satisfied: {'yes' if (span_days or 0) >= span_threshold else 'no'}",
        "",
        "## next queued task",
        "",
    ]
    task = next_result.get("task")
    if task is None:
        lines.append("No ready growth task.")
    else:
        lines.append(f"- id: {task['id']}")
        lines.append(f"- kind: {task['kind']}")
        lines.append(f"- gap_key: {task['gap_key']}")
    lines.append("")
    return "\n".join(lines)


def report(profile_root: Path) -> str:
    content = build_growth_report(profile_root)
    _atomic_write(growth_report_path(profile_root), content)
    return content


# ----------------------------------------------------------------- hearing --


def _hearings_path(data_root: Path) -> Path:
    return data_root / "hearings.jsonl"


def _hearing_lines(data_root: Path) -> list[dict[str, Any]]:
    path = _hearings_path(data_root)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def append_growth_hearing(
    data_root: Path,
    *,
    requester: str,
    subject: str | None,
    purpose: str,
    task_id: str | None,
    question_id: str | None,
    outcome: str,
    reason: str | None,
    entity: str | None,
) -> dict[str, Any]:
    """Build and append one growth-hearing/v1 line. Never raises; a log failure must not fail a run."""
    record = {
        "contract_version": HEARING_LOG_CONTRACT,
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "requester": requester,
        "subject": subject,
        "purpose": purpose,
        "task_id": task_id,
        "question_id": question_id,
        "outcome": outcome,
        "reason": reason,
        "entity": entity,
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(_hearings_path(data_root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass
    return record


def _requester_line_count(data_root: Path, requester: str) -> int:
    return sum(1 for record in _hearing_lines(data_root) if record.get("requester") == requester)


def _skip_cooldown_active(data_root: Path, question_id: str | None, now: datetime, cooldown_days: int) -> bool:
    if question_id is None:
        return False
    cutoff = now - timedelta(days=cooldown_days)
    for record in _hearing_lines(data_root):
        if record.get("outcome") != "skipped" or record.get("question_id") != question_id:
            continue
        try:
            recorded_at = datetime.fromisoformat(str(record.get("ts")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if recorded_at >= cutoff:
            return True
    return False


def _consent_is_valid_for_hearing(consent: Any, purpose: str, today: date) -> bool:
    if not isinstance(consent, dict):
        return False
    if consent.get("obtained") is not True:
        return False
    if consent.get("revoked_at") is not None:
        return False
    expires_at = consent.get("expires_at")
    if expires_at is not None:
        expiry = _as_date(expires_at)
        if expiry is None or expiry <= today:
            return False
    purposes = consent.get("purposes")
    if not isinstance(purposes, list) or purpose not in purposes:
        return False
    operations = consent.get("allowed_operations")
    if not isinstance(operations, list) or not HEARING_REQUIRED_OPERATIONS.issubset(operations):
        return False
    return True


def _select_hearing_source(entities: list[Any], subject_id: str, purpose: str, *, today: date | None = None) -> Any | None:
    today = today or datetime.now(timezone.utc).date()
    candidates = [
        entity
        for entity in entities
        if entity.type == "source"
        and entity.meta.get("subject") == subject_id
        and entity.meta.get("source_kind") in HEARING_SOURCE_KINDS
        and _consent_is_valid_for_hearing(entity.meta.get("consent"), purpose, today)
    ]
    if not candidates:
        return None

    def _captured_at(entity: Any) -> str:
        value = entity.meta.get("captured_at")
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    return max(candidates, key=_captured_at)


def _select_hearing_task(
    queue: dict[str, Any], data_root: Path, cooldown_days: int, now: datetime
) -> tuple[dict[str, Any] | None, str | None]:
    candidates = sorted(
        (task for task in queue.get("tasks", []) if task["status"] == "ready" and task["kind"] == "acquire-event"),
        key=lambda task: task["id"],
    )
    if not candidates:
        return None, "NO_ACQUIRE_TASK"
    eligible = [task for task in candidates if not _skip_cooldown_active(data_root, task.get("question_id"), now, cooldown_days)]
    if not eligible:
        return None, "ALL_SKIPPED_RECENTLY"
    return eligible[0], None


def _hearing_packet_base(*, requester: str, subject: str | None, purpose: str) -> dict[str, Any]:
    return {
        "contract_version": HEARING_PACKET_CONTRACT,
        "outcome": None,
        "reason": None,
        "requester": requester,
        "subject": subject,
        "purpose": purpose,
        "queue_sha256": None,
        "task_id": None,
        "question_id": None,
        "source_ref": None,
        "answer_format": None,
        "intent": None,
        "why": None,
        "question": None,
        "anchors": None,
        "skip_ack": None,
        "constraints": None,
    }


def _resolve_hearing_subject(layout: Any, subject: str | None) -> str | None:
    if subject is not None:
        return subject if subject in layout.subject_ids else None
    if len(layout.subject_ids) == 1:
        return layout.subject_ids[0]
    return None


def hearing_open(profile_root: Path, *, requester: str, purpose: str, subject: str | None = None) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    hearing_cfg = load_hearing_config()

    def unavailable(reason: str, *, subject_value: str | None) -> dict[str, Any]:
        packet = _hearing_packet_base(requester=requester, subject=subject_value, purpose=purpose)
        packet["outcome"] = "unavailable"
        packet["reason"] = reason
        return packet

    subject_id = _resolve_hearing_subject(layout, subject)
    if subject_id is None:
        return unavailable("SUBJECT_REQUIRED", subject_value=None)

    max_questions = int(hearing_cfg.get("max_questions_per_run", 1))
    if _requester_line_count(layout.data_root, requester) >= max_questions:
        return unavailable("ALREADY_OFFERED_THIS_RUN", subject_value=subject_id)

    try:
        queue = generate(profile_root)
    except GrowthTaskError as error:
        if error.code in ("QUEUE_BUSY", "ENTITIES_INVALID"):
            return unavailable(error.code, subject_value=subject_id)
        raise

    entities = discover_entities(layout.entity_root)
    source = _select_hearing_source(entities, subject_id, purpose)
    if source is None:
        return unavailable("NO_CONSENTED_SOURCE", subject_value=subject_id)

    cooldown_days = int(hearing_cfg.get("skip_cooldown_days", 14))
    now = datetime.now(timezone.utc)
    task, failure_reason = _select_hearing_task(queue, layout.data_root, cooldown_days, now)
    if task is None:
        return unavailable(failure_reason or "NO_ACQUIRE_TASK", subject_value=subject_id)

    question_bank = load_question_bank()
    question_item = question_bank.get(task["question_id"]) or {}
    target = task["target"]
    slot = target.get("slot")
    why = (
        hearing_cfg.get("slot_reasons", {}).get(slot)
        if slot
        else hearing_cfg.get("section_reasons", {}).get(target.get("section"))
    )
    packet = _hearing_packet_base(requester=requester, subject=subject_id, purpose=purpose)
    packet.update(
        {
            "outcome": "offered",
            "reason": None,
            "queue_sha256": _queue_sha256(queue_path(profile_root)),
            "task_id": task["id"],
            "question_id": task["question_id"],
            "source_ref": source.id,
            "answer_format": "slot-values" if slot else "event-block",
            "intent": list(hearing_cfg.get("intent", [])),
            "why": why,
            "question": question_item.get("hearing_text") or question_item.get("text"),
            "anchors": [],
            "skip_ack": hearing_cfg.get("skip_ack"),
            "constraints": list(HEARING_CONSTRAINTS),
        }
    )
    append_growth_hearing(
        layout.data_root,
        requester=requester,
        subject=subject_id,
        purpose=purpose,
        task_id=task["id"],
        question_id=task["question_id"],
        outcome="offered",
        reason=None,
        entity=None,
    )
    return packet


def hearing_skip(
    profile_root: Path,
    task_id: str,
    *,
    requester: str,
    reason: str,
    purpose: str = "artistic-research",
    subject: str | None = None,
) -> dict[str, Any]:
    if reason not in ("skipped", "no-response"):
        raise _error("HEARING_REASON_INVALID", "reason must be skipped or no-response")
    layout = resolve_profile_root(profile_root)
    subject_id = _resolve_hearing_subject(layout, subject)

    question_id = None
    path = queue_path(profile_root)
    if path.exists():
        queue = load_yaml(path)
        task = next((item for item in queue.get("tasks", []) if item["id"] == task_id), None)
        if task is not None:
            question_id = task.get("question_id")

    return append_growth_hearing(
        layout.data_root,
        requester=requester,
        subject=subject_id,
        purpose=purpose,
        task_id=task_id,
        question_id=question_id,
        outcome="skipped",
        reason=reason,
        entity=None,
    )


MAX_HEARING_ANSWER_BYTES = 1_000_000


class _HearingAnswerRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _intake_error_code(error: IntakeError) -> str:
    return str(error).split(":", 1)[0]


def _write_hearing_event(
    layout: Any, stdin_text: str, *, subject_id: str, source: Any
) -> tuple[str, Path]:
    try:
        events = _parse_transcript(stdin_text)
    except IntakeError as error:
        raise _HearingAnswerRejected(_intake_error_code(error)) from error
    if len(events) != 1:
        raise _HearingAnswerRejected("not-a-single-block")
    event = events[0]
    today_str = datetime.now(timezone.utc).date().isoformat()
    event_id = f"event/hearing-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{event['slug']}"
    path = layout.entity_root / "events" / f"{event_id.split('/', 1)[1]}.md"
    if path.exists():
        raise _HearingAnswerRejected("event-exists")
    try:
        _validate_event_values(event, source_id=source.id)
        domains = _list_slot(event["domain"], field="domain", allow_none=False)
        social = _list_slot(event["social"], field="social", allow_none=False)
        raw_voice = _list_slot(event["raw_voice"], field="raw_voice")
    except IntakeError as error:
        raise _HearingAnswerRejected(_intake_error_code(error)) from error
    raw_voice_items = None if raw_voice is None else [{"text": quote, "source_ref": source.id} for quote in raw_voice]
    meta = {
        "id": event_id,
        "type": "event",
        "subject": subject_id,
        "time": {"observed_at": event["observed_at"], "precision": event["precision"]},
        "context": {
            "domains": domains,
            "social": social,
            "uncertainty": event["uncertainty"],
            "control": event["control"],
        },
        "state": {"fatigue": event["fatigue"], "stress": event["stress"]},
        "trigger": event["trigger"],
        "observed_facts": _list_slot(event["observed_fact"], field="observed_fact"),
        "raw_voice": raw_voice_items,
        "appraisal": _list_slot(event["appraisal"], field="appraisal"),
        "emotion": _list_slot(event["emotion"], field="emotion"),
        "body": _list_slot(event["body"], field="body"),
        "cognition": _list_slot(event["cognition"], field="cognition"),
        "action": _list_slot(event["action"], field="action"),
        "immediate_outcome": _list_slot(event["immediate_outcome"], field="immediate_outcome"),
        "delayed_outcome": _list_slot(event["delayed_outcome"], field="delayed_outcome"),
        "source_refs": [source.id],
        "created": today_str,
        "updated": today_str,
    }
    text = serialize_markdown(Entity(path=path, meta=meta, body="\n"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return event_id, path


def _apply_hearing_slot_answer(layout: Any, stdin_text: str, *, task: dict[str, Any], source: Any) -> tuple[str, Path, bytes]:
    try:
        document = yaml.safe_load(stdin_text)
    except yaml.YAMLError as error:
        raise _HearingAnswerRejected("malformed-yaml") from error
    if not isinstance(document, dict):
        raise _HearingAnswerRejected("malformed-yaml")
    slot = task["target"]["slot"]
    if slot not in document:
        raise _HearingAnswerRejected("slot-missing")
    value = document[slot]
    if value == "unknown" or (isinstance(value, list) and all(isinstance(item, str) for item in value)):
        slot_value = value
    else:
        raise _HearingAnswerRejected("slot-value-invalid")
    raw_voice_input = document.get("raw_voice")
    if raw_voice_input is not None:
        if not isinstance(raw_voice_input, list) or not all(isinstance(item, str) for item in raw_voice_input):
            raise _HearingAnswerRejected("raw-voice-invalid")
        for quote in raw_voice_input:
            if len(quote) > MAX_RAW_QUOTE_CHARS:
                raise _HearingAnswerRejected("raw-quote-too-long")

    entity_id = task["target"]["entity"]
    path = layout.entity_root / "events" / f"{entity_id.split('/', 1)[1]}.md"
    if not path.exists():
        raise _HearingAnswerRejected("event-missing")
    original_bytes = path.read_bytes()
    entity = parse_markdown(path)
    if entity.meta.get(slot) is not None:
        raise _HearingAnswerRejected("slot-already-set")
    new_meta = dict(entity.meta)
    new_meta[slot] = slot_value
    if raw_voice_input:
        existing_raw_voice = list(entity.meta.get("raw_voice") or [])
        existing_raw_voice.extend({"text": quote, "source_ref": source.id} for quote in raw_voice_input)
        new_meta["raw_voice"] = existing_raw_voice
    new_meta["updated"] = datetime.now(timezone.utc).date().isoformat()
    text = serialize_markdown(replace(entity, meta=new_meta))
    path.write_text(text, encoding="utf-8")
    return entity_id, path, original_bytes


def hearing_answer(
    profile_root: Path,
    task_id: str,
    *,
    requester: str,
    stdin_text: str,
    expected_queue_sha256: str | None,
    purpose: str = "artistic-research",
    subject: str | None = None,
) -> dict[str, Any]:
    layout = resolve_profile_root(profile_root)
    subject_id = _resolve_hearing_subject(layout, subject)

    def unavailable(reason: str, *, question_id: str | None = None) -> dict[str, Any]:
        return {
            "contract_version": HEARING_LOG_CONTRACT,
            "outcome": "unavailable",
            "reason": reason,
            "requester": requester,
            "subject": subject_id,
            "purpose": purpose,
            "task_id": task_id,
            "question_id": question_id,
            "entity": None,
        }

    if not stdin_text or not stdin_text.strip():
        return unavailable("ANSWER_INVALID:empty")
    if len(stdin_text.encode("utf-8")) > MAX_HEARING_ANSWER_BYTES:
        return unavailable("ANSWER_INVALID:too-large")

    try:
        path, queue = _load_queue_checked(profile_root, expected_queue_sha256)
    except GrowthTaskError as error:
        return unavailable(error.code)

    task = next((item for item in queue.get("tasks", []) if item["id"] == task_id), None)
    if task is None or task["status"] != "ready" or task["kind"] != "acquire-event":
        return unavailable("ANSWER_INVALID:task-not-ready")

    question_id = task.get("question_id")
    entities = discover_entities(layout.entity_root)
    source = _select_hearing_source(entities, subject_id, purpose) if subject_id else None
    if source is None:
        return unavailable("CONSENT_INVALID", question_id=question_id)

    before_snapshot = snapshot_before(profile_root, task)
    slot = task["target"].get("slot")

    try:
        if slot is None:
            entity_id, written_path = _write_hearing_event(layout, stdin_text, subject_id=subject_id, source=source)
            original_bytes: bytes | None = None
        else:
            entity_id, written_path, original_bytes = _apply_hearing_slot_answer(layout, stdin_text, task=task, source=source)
    except _HearingAnswerRejected as error:
        return unavailable(f"ANSWER_INVALID:{error.code}", question_id=question_id)

    def _rollback() -> None:
        if original_bytes is None:
            written_path.unlink(missing_ok=True)
        else:
            written_path.write_bytes(original_bytes)

    try:
        checks_run = _run_checks(task["checks"], repo=REPO_ROOT)
        _verify_acquire_event(profile_root, task, before_snapshot.get("event_ids", set()))
    except GrowthTaskError as error:
        _rollback()
        return unavailable(f"ANSWER_INVALID:{error.code}", question_id=question_id)

    task["status"] = "done"
    task["claim"] = None
    task["evidence"] = {
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "queue_sha256": expected_queue_sha256,
        "entities": [entity_id],
        "checks": [f"{item['command']} — exit {item['exit_code']}" for item in checks_run],
    }
    _atomic_write(path, _dump_yaml(queue))

    return append_growth_hearing(
        layout.data_root,
        requester=requester,
        subject=subject_id,
        purpose=purpose,
        task_id=task_id,
        question_id=question_id,
        outcome="answered",
        reason=None,
        entity=entity_id,
    )


# --------------------------------------------------------------------- CLI --


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    generate_parser = sub.add_parser("generate")
    add_profile_root_argument(generate_parser)

    next_parser = sub.add_parser("next")
    add_profile_root_argument(next_parser)
    next_parser.add_argument("--json", action="store_true")

    claim_parser = sub.add_parser("claim")
    add_profile_root_argument(claim_parser)
    claim_parser.add_argument("task_id")
    claim_parser.add_argument("--actor", required=True)
    claim_parser.add_argument("--expected-queue-sha256")
    claim_parser.add_argument("--json", action="store_true")

    complete_parser = sub.add_parser("complete")
    add_profile_root_argument(complete_parser)
    complete_parser.add_argument("task_id")
    complete_parser.add_argument("--expected-queue-sha256")
    complete_parser.add_argument("--json", action="store_true")

    report_parser = sub.add_parser("report")
    add_profile_root_argument(report_parser)

    hearing_parser = sub.add_parser("hearing")
    hearing_sub = hearing_parser.add_subparsers(dest="hearing_command", required=True)

    hearing_open_parser = hearing_sub.add_parser("open")
    add_profile_root_argument(hearing_open_parser)
    hearing_open_parser.add_argument("--requester", required=True)
    hearing_open_parser.add_argument("--purpose", required=True)
    hearing_open_parser.add_argument("--subject")
    hearing_open_parser.add_argument("--json", action="store_true")

    hearing_skip_parser = hearing_sub.add_parser("skip")
    add_profile_root_argument(hearing_skip_parser)
    hearing_skip_parser.add_argument("task_id")
    hearing_skip_parser.add_argument("--requester", required=True)
    hearing_skip_parser.add_argument("--reason", required=True, choices=("skipped", "no-response"))
    hearing_skip_parser.add_argument("--purpose", default="artistic-research")
    hearing_skip_parser.add_argument("--subject")
    hearing_skip_parser.add_argument("--json", action="store_true")

    hearing_answer_parser = hearing_sub.add_parser("answer")
    add_profile_root_argument(hearing_answer_parser)
    hearing_answer_parser.add_argument("task_id")
    hearing_answer_parser.add_argument("--requester", required=True)
    hearing_answer_parser.add_argument("--expected-queue-sha256")
    hearing_answer_parser.add_argument("--purpose", default="artistic-research")
    hearing_answer_parser.add_argument("--subject")
    hearing_answer_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.profile_root is None:
        profile_root_error(ProfileRootError("PROFILE_ROOT_REQUIRED", "pass --profile-root"))
        return 2
    try:
        resolve_profile_root(args.profile_root)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2

    try:
        if args.command == "generate":
            queue = generate(args.profile_root)
            print(f"generated {len(queue['tasks'])} growth task(s)")
            return 0
        if args.command == "next":
            result = next_task(args.profile_root)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "claim":
            result = claim_growth_task(
                args.profile_root, args.task_id, actor=args.actor, expected_queue_sha256=args.expected_queue_sha256
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "complete":
            path, queue = _load_queue_checked(args.profile_root, args.expected_queue_sha256)
            task = next((item for item in queue["tasks"] if item["id"] == args.task_id), None)
            if task is None:
                raise _error("TASK_NOT_FOUND", f"{args.task_id} is not registered")
            before = snapshot_before(args.profile_root, task)
            result = complete_growth_task(
                args.profile_root,
                args.task_id,
                expected_queue_sha256=args.expected_queue_sha256,
                before_snapshot=before,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "report":
            report(args.profile_root)
            print(f"wrote {growth_report_path(args.profile_root).relative_to(Path(args.profile_root))}")
            return 0
        if args.command == "hearing":
            if args.hearing_command == "open":
                result = hearing_open(args.profile_root, requester=args.requester, purpose=args.purpose, subject=args.subject)
            elif args.hearing_command == "skip":
                result = hearing_skip(
                    args.profile_root,
                    args.task_id,
                    requester=args.requester,
                    reason=args.reason,
                    purpose=args.purpose,
                    subject=args.subject,
                )
            else:
                stdin_text = sys.stdin.read()
                result = hearing_answer(
                    args.profile_root,
                    args.task_id,
                    requester=args.requester,
                    stdin_text=stdin_text,
                    expected_queue_sha256=args.expected_queue_sha256,
                    purpose=args.purpose,
                    subject=args.subject,
                )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
    except GrowthTaskError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
