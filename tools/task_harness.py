"""Read-only validation and deterministic selection for execution/tasks.yaml."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "execution" / "tasks.yaml"
SUPPORTED_QUEUE_VERSION = 3
TASK_ID_RE = re.compile(r"^SM-[0-9]{3}$")
ISSUE_URL_RE = re.compile(
    r"^https://github[.]com/[^/]+/[^/]+/issues/[1-9][0-9]*$"
)
STATUS_VALUES = frozenset({"blocked", "ready", "in-progress", "review", "done"})
ROOT_FIELDS = frozenset({"version", "updated_at", "policy", "tasks"})
POLICY_FIELDS = frozenset({"selection", "unit", "shared_locks", "status_values"})
TASK_FIELDS = frozenset(
    {
        "id",
        "title",
        "phase",
        "status",
        "depends_on",
        "issue",
        "allowed_paths",
        "external_paths",
        "claim",
        "acceptance",
        "checks",
        "stop_if",
        "evidence",
    }
)
TASK_OUTPUT_FIELDS = (
    "id",
    "title",
    "issue",
    "allowed_paths",
    "acceptance",
    "checks",
    "stop_if",
)
LEGACY_TASK_FIELDS = frozenset(
    {
        "id",
        "title",
        "phase",
        "status",
        "depends_on",
        "allowed_paths",
        "acceptance",
        "checks",
        "stop_if",
        "evidence",
    }
)


class QueueError(ValueError):
    """A deterministic, user-safe queue validation error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _error(errors: list[str], location: str, message: str) -> None:
    errors.append(f"{location}: {message}")


def _is_string_list(value: Any, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, list):
        return False
    if not allow_empty and not value:
        return False
    return all(isinstance(item, str) and bool(item) for item in value)


def _is_relative_path_pattern(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise QueueError("queue: cannot read valid YAML") from exc


def load_queue(path: Path = DEFAULT_QUEUE) -> dict[str, Any]:
    value = _load_yaml(path)
    if not isinstance(value, dict):
        raise QueueError("queue: expected mapping")
    return value


def _validate_claim(
    task: dict[str, Any],
    errors: list[str],
) -> None:
    task_id = task.get("id", "<missing>")
    status = task.get("status")
    claim = task.get("claim")
    if status in {"ready", "done", "blocked", "review"} and claim is not None:
        _error(errors, f"task {task_id}.claim", "must be null")
        return
    if status == "in-progress" and claim is not None and not isinstance(claim, dict):
        _error(errors, f"task {task_id}.claim", "must be a mapping or null")
    if status == "in-progress" and isinstance(claim, dict):
        for field in ("actor", "base", "branch", "remote"):
            if not isinstance(claim.get(field), str) or not claim[field]:
                _error(errors, f"task {task_id}.claim.{field}", "must be a non-empty string")
    if status == "in-progress" and claim is None and task_id != "SM-020":
        _error(errors, f"task {task_id}.claim", "must be present for in-progress task")


def _validate_dependencies(
    tasks: list[dict[str, Any]],
    errors: list[str],
) -> None:
    by_id = {
        task["id"]: task
        for task in tasks
        if isinstance(task.get("id"), str)
    }
    graph: dict[str, list[str]] = {}
    for task in tasks:
        task_id = task.get("id", "<missing>")
        if not isinstance(task_id, str):
            continue
        dependencies = task.get("depends_on")
        if not _is_string_list(dependencies, allow_empty=True):
            _error(errors, f"task {task_id}.depends_on", "must be a list of strings")
            graph[task_id] = []
            continue
        graph[task_id] = list(dependencies)
        for dependency in dependencies:
            if dependency not in by_id:
                _error(
                    errors,
                    f"task {task_id}.depends_on",
                    f"unknown dependency {dependency}",
                )
            if dependency == task_id:
                _error(errors, f"task {task_id}.depends_on", "cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: set[tuple[str, ...]] = set()

    def visit(task_id: str, trail: tuple[str, ...]) -> None:
        if task_id in visiting:
            start = trail.index(task_id) if task_id in trail else 0
            cycle = trail[start:] + (task_id,)
            cycles.add(cycle)
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph.get(task_id, []):
            if dependency in graph:
                visit(dependency, trail + (task_id,))
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(graph):
        visit(task_id, ())
    for cycle in sorted(cycles):
        _error(errors, "queue.dependencies", f"cycle {' -> '.join(cycle)}")


def validate_queue(queue: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown_root = sorted(set(queue) - ROOT_FIELDS)
    for field in unknown_root:
        _error(errors, f"queue.{field}", "unknown field")
    for field in sorted(ROOT_FIELDS - set(queue)):
        _error(errors, f"queue.{field}", "required field is missing")

    if queue.get("version") != SUPPORTED_QUEUE_VERSION:
        _error(errors, "queue.version", f"must be {SUPPORTED_QUEUE_VERSION}")
    if not isinstance(queue.get("updated_at"), str) or not queue.get("updated_at"):
        _error(errors, "queue.updated_at", "must be a non-empty string")

    policy = queue.get("policy")
    if not isinstance(policy, dict):
        _error(errors, "queue.policy", "must be a mapping")
    else:
        for field in sorted(set(policy) - POLICY_FIELDS):
            _error(errors, f"queue.policy.{field}", "unknown field")
        for field in sorted(POLICY_FIELDS - set(policy)):
            _error(errors, f"queue.policy.{field}", "required field is missing")
        if policy.get("selection") != "lowest-ready-id":
            _error(errors, "queue.policy.selection", "must be lowest-ready-id")
        if policy.get("unit") != "one-task-one-agent-one-pr":
            _error(errors, "queue.policy.unit", "must be one-task-one-agent-one-pr")
        if not _is_string_list(policy.get("shared_locks"), allow_empty=True):
            _error(errors, "queue.policy.shared_locks", "must be a list of strings")
        status_values = policy.get("status_values")
        if not _is_string_list(status_values):
            _error(errors, "queue.policy.status_values", "must be a list of strings")
        elif set(status_values) != STATUS_VALUES:
            _error(errors, "queue.policy.status_values", "must contain the supported statuses exactly")

    tasks = queue.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        _error(errors, "queue.tasks", "must be a non-empty list")
        return sorted(set(errors))
    if not all(isinstance(task, dict) for task in tasks):
        _error(errors, "queue.tasks", "each task must be a mapping")
        return sorted(set(errors))

    seen: set[Any] = set()
    for task in tasks:
        task_id = task.get("id", "<missing>")
        location = f"task {task_id}"
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            _error(errors, f"{location}.id", "must match SM-NNN")
        elif task_id in seen:
            _error(errors, f"{location}.id", "duplicate task ID")
        else:
            seen.add(task_id)

        for field in sorted(set(task) - TASK_FIELDS):
            _error(errors, f"{location}.{field}", "unknown field")
        for field in sorted(LEGACY_TASK_FIELDS - set(task)):
            _error(errors, f"{location}.{field}", "required field is missing")

        if not isinstance(task.get("title"), str) or not task.get("title"):
            _error(errors, f"{location}.title", "must be a non-empty string")
        if not isinstance(task.get("phase"), int) or isinstance(task.get("phase"), bool):
            _error(errors, f"{location}.phase", "must be an integer")
        elif task["phase"] < 0:
            _error(errors, f"{location}.phase", "must not be negative")

        status = task.get("status")
        if status not in STATUS_VALUES:
            _error(errors, f"{location}.status", "unsupported status")
        if not _is_string_list(task.get("allowed_paths")):
            _error(errors, f"{location}.allowed_paths", "must be a non-empty list of strings")
        else:
            for path in task["allowed_paths"]:
                if not _is_relative_path_pattern(path):
                    _error(errors, f"{location}.allowed_paths", f"invalid relative pattern {path}")

        if "external_paths" in task and not _is_string_list(
            task.get("external_paths"), allow_empty=True
        ):
            _error(errors, f"{location}.external_paths", "must be a list of strings")
        if not _is_string_list(task.get("acceptance")):
            _error(errors, f"{location}.acceptance", "must be a non-empty list of strings")
        if not _is_string_list(task.get("checks")):
            _error(errors, f"{location}.checks", "must be a non-empty list of strings")
        if not _is_string_list(task.get("stop_if"), allow_empty=True):
            _error(errors, f"{location}.stop_if", "must be a list of strings")
        if "issue" in task and (
            not isinstance(task.get("issue"), str)
            or not ISSUE_URL_RE.fullmatch(task["issue"])
        ):
            _error(errors, f"{location}.issue", "must be a GitHub issue URL")
        if not isinstance(task.get("evidence"), list):
            _error(errors, f"{location}.evidence", "must be a list")
        elif status == "done" and not task["evidence"]:
            _error(errors, f"{location}.evidence", "done task requires evidence")
        elif status == "ready" and task["evidence"]:
            _error(errors, f"{location}.evidence", "ready task must have no evidence")
        _validate_claim(task, errors)

        if isinstance(task.get("phase"), int) and task["phase"] >= 10:
            for field in sorted(set(task) - TASK_FIELDS):
                _error(errors, f"{location}.{field}", "unknown Phase 10 field")
            if "claim" not in task:
                _error(errors, f"{location}.claim", "required for Phase 10 task")
            if "issue" not in task or not isinstance(task.get("issue"), str) or not ISSUE_URL_RE.fullmatch(task["issue"]):
                _error(errors, f"{location}.issue", "required GitHub issue URL")
            if not _is_string_list(task.get("acceptance")):
                _error(errors, f"{location}.acceptance", "required for Phase 10 task")
            if not _is_string_list(task.get("checks")):
                _error(errors, f"{location}.checks", "required for Phase 10 task")

    _validate_dependencies(tasks, errors)
    return sorted(set(errors))


def selectable_tasks(queue: dict[str, Any]) -> list[dict[str, Any]]:
    done = {task["id"] for task in queue["tasks"] if task["status"] == "done"}
    candidates = [
        task
        for task in queue["tasks"]
        if task["status"] == "ready"
        and set(task["depends_on"]).issubset(done)
    ]
    return sorted(candidates, key=lambda task: int(task["id"].split("-")[1]))


def task_output(task: dict[str, Any]) -> dict[str, Any]:
    return {field: task[field] for field in TASK_OUTPUT_FIELDS}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task_harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "next"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
        subparser.add_argument("--json", action="store_true")
    return parser


def _write_result(
    value: dict[str, Any], *, as_json: bool, stream: Any | None = None
) -> None:
    if stream is None:
        stream = sys.stdout
    if as_json:
        stream.write(canonical_json(value) + "\n")
    else:
        if "task" in value and value["task"] is not None:
            stream.write(f"{value['task']['id']}: {value['task']['title']}\n")
        elif value.get("status") == "valid":
            stream.write("valid\n")
        else:
            stream.write("no selectable task\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        queue = load_queue(args.queue)
        errors = validate_queue(queue)
    except QueueError as exc:
        errors = [str(exc)]
        queue = None
    if errors:
        if args.json:
            sys.stderr.write(canonical_json({"errors": sorted(errors)}) + "\n")
        else:
            for error in sorted(errors):
                sys.stderr.write(error + "\n")
        return 2

    if args.command == "validate":
        _write_result(
            {
                "queue_version": queue["version"],
                "status": "valid",
                "task_count": len(queue["tasks"]),
            },
            as_json=args.json,
        )
        return 0

    candidates = selectable_tasks(queue)
    if not candidates:
        result = {
            "queue_version": queue["version"],
            "reason": "no-selectable-task",
            "task": None,
        }
        _write_result(result, as_json=args.json, stream=sys.stdout if args.json else sys.stderr)
        return 3
    _write_result(
        {"queue_version": queue["version"], "task": task_output(candidates[0])},
        as_json=args.json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
