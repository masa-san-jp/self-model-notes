#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

try:
    from kb import ROOT, _refs, canonical_json, discover_entities, validate_entities
except ModuleNotFoundError:  # Imported as tools.build_graph by the test suite.
    from tools.kb import ROOT, _refs, canonical_json, discover_entities, validate_entities


ENTITY_KINDS = ("subject", "source", "event", "claim", "pattern", "measurement")

# These are the reference fields that form an evidence lineage from a Source to
# a Pattern. Structural references such as Subject consent are deliberately not
# part of this lineage.
TRACE_FIELDS = {
    "event": {"source_refs"},
    "claim": {"supporting_evidence", "counterevidence"},
    "pattern": {"evidence", "claim_refs", "counterevidence"},
}

# Coverage is intentionally field-based. A missing key means that a field was
# not observed at all; null/unknown preserve an explicitly unknown observation;
# [] and {} preserve a checked-but-empty observation.
COVERAGE_FIELDS = {
    "subject": ("consent_refs",),
    "source": (
        "captured_at",
        "locator",
        "raw_content_stored",
        "consent.obtained",
        "consent.purposes",
        "consent.allowed_operations",
        "consent.expires_at",
        "consent.revoked_at",
        "reliability_notes",
    ),
    "event": (
        "time.observed_at",
        "time.precision",
        "context.domains",
        "context.social",
        "context.uncertainty",
        "context.control",
        "state.fatigue",
        "state.stress",
        "trigger",
        "observed_facts",
        "raw_voice",
        "appraisal",
        "emotion",
        "body",
        "cognition",
        "action",
        "immediate_outcome",
        "delayed_outcome",
        "source_refs",
    ),
    "claim": (
        "layer",
        "scope",
        "statement",
        "conditions",
        "supporting_evidence",
        "counterevidence",
        "alternative_explanations",
        "confidence",
        "status",
        "supersedes",
        "superseded_by",
    ),
    "pattern": (
        "condition",
        "recurring_appraisal",
        "recurring_drive",
        "recurring_action",
        "reinforcement",
        "contexts_seen",
        "evidence",
        "claim_refs",
        "counterevidence",
        "confidence",
        "status",
    ),
    "measurement": (
        "instrument.name",
        "instrument.version",
        "instrument.official",
        "instrument.scoring_reference",
        "administered_at",
        "source_ref",
        "scores",
        "interpretation_claim_refs",
    ),
}

COVERAGE_STATES = ("unobserved", "unknown", "confirmed-empty", "observed")
_MISSING = object()


def _direct_edges(entities) -> list[dict[str, str]]:
    edges = [
        {"from": entity.id, "field": field, "to": ref}
        for entity in entities
        for field, ref in _refs(entity)
    ]
    edges.sort(key=lambda edge: (edge["from"], edge["field"], edge["to"]))
    return edges


def _lineage(entities) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Return reverse evidence edges and all simple Source-to-Pattern paths."""
    by_id = {entity.id: entity for entity in entities}
    reverse: dict[str, list[str]] = {}
    trace_edges: list[dict[str, str]] = []
    for entity in sorted(entities, key=lambda item: item.id):
        for field, ref in _refs(entity):
            if field not in TRACE_FIELDS.get(entity.type, set()):
                continue
            reverse.setdefault(ref, []).append(entity.id)
            trace_edges.append({"from": ref, "field": field, "to": entity.id})

    for targets in reverse.values():
        targets.sort()
    trace_edges.sort(key=lambda edge: (edge["from"], edge["field"], edge["to"]))

    paths: list[dict[str, Any]] = []

    def visit(source_id: str, current_id: str, path: list[str]) -> None:
        for next_id in reverse.get(current_id, []):
            if next_id in path or next_id not in by_id:
                continue
            next_path = [*path, next_id]
            if by_id[next_id].type == "pattern":
                paths.append({"source": source_id, "pattern": next_id, "path": next_path})
            else:
                visit(source_id, next_id, next_path)

    for source in sorted((entity for entity in entities if entity.type == "source"), key=lambda item: item.id):
        visit(source.id, source.id, [source.id])
    paths.sort(key=lambda item: (item["source"], item["pattern"], item["path"]))
    return trace_edges, paths


def _value_at(meta: dict[str, Any], field_path: str) -> Any:
    value: Any = meta
    for key in field_path.split("."):
        if value is None:
            return None
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _coverage_state(value: Any) -> str:
    if value is _MISSING:
        return "unobserved"
    if value is None or value == "unknown":
        return "unknown"
    if isinstance(value, (list, dict)) and not value:
        return "confirmed-empty"
    return "observed"


def build_coverage(entities) -> dict[str, Any]:
    counts = {kind: sum(1 for entity in entities if entity.type == kind) for kind in ENTITY_KINDS}
    field_coverage: dict[str, dict[str, int]] = {}
    for kind in ENTITY_KINDS:
        kind_entities = [entity for entity in entities if entity.type == kind]
        for field_path in COVERAGE_FIELDS[kind]:
            states = {state: 0 for state in COVERAGE_STATES}
            for entity in kind_entities:
                states[_coverage_state(_value_at(entity.meta, field_path))] += 1
            field_coverage[f"{kind}.{field_path}"] = states
    return {"schema_version": 1, "counts": counts, "field_coverage": field_coverage}


def build(entities):
    nodes = [
        {"id": entity.id, "type": entity.type, "path": str(entity.path.relative_to(ROOT))}
        for entity in sorted(entities, key=lambda item: item.id)
    ]
    edges = _direct_edges(entities)
    trace_edges, trace_paths = _lineage(entities)
    graph_payload = {
        "nodes": nodes,
        "edges": edges,
        "trace_edges": trace_edges,
        "trace_paths": {"source_to_pattern": trace_paths},
    }
    digest = hashlib.sha256(canonical_json(graph_payload).encode()).hexdigest()
    graph = {"schema_version": 1, "content_sha256": digest, **graph_payload}
    return graph, build_coverage(entities)


def coverage_markdown(coverage):
    count_rows = "\n".join(f"| {kind} | {count} |" for kind, count in coverage["counts"].items())
    field_rows = "\n".join(
        "| {field} | {unobserved} | {unknown} | {confirmed_empty} | {observed} |".format(
            field=field,
            unobserved=states["unobserved"],
            unknown=states["unknown"],
            confirmed_empty=states["confirmed-empty"],
            observed=states["observed"],
        )
        for field, states in coverage["field_coverage"].items()
    )
    return (
        "# Coverage\n\n"
        "<!-- generated by tools/build_graph.py; do not edit -->\n\n"
        "## Entity counts\n\n"
        "| Entity | Count |\n|---|---:|\n"
        f"{count_rows}\n\n"
        "## Field coverage\n\n"
        "| Field | Unobserved | Unknown | Confirmed empty | Observed |\n"
        "|---|---:|---:|---:|---:|\n"
        f"{field_rows}\n"
    )


def generated_outputs(graph, coverage, root: Path = ROOT) -> dict[Path, str]:
    return {
        root / "data" / "graph.json": canonical_json(graph),
        root / "data" / "coverage.json": canonical_json(coverage),
        root / "overviews" / "coverage.md": coverage_markdown(coverage),
    }


def stale_generated_files(expected: dict[Path, str]) -> list[Path]:
    return [path for path, content in expected.items() if not path.exists() or path.read_text(encoding="utf-8") != content]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()
    entities = discover_entities(args.root) if args.root else discover_entities()
    errors = validate_entities(entities)
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    graph, coverage = build(entities)
    if args.root:
        print(f"OK: {len(entities)} entities")
        return 0
    expected = generated_outputs(graph, coverage)
    if args.check:
        stale = stale_generated_files(expected)
        if stale:
            for path in stale:
                print(f"ERROR stale generated file: {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print(f"OK: {len(entities)} entities; generated files are current")
        return 0
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"built {len(entities)} entities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
