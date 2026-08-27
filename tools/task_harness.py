"""Read-only validation and deterministic selection for execution/tasks.yaml."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "execution" / "tasks.yaml"
SUPPORTED_QUEUE_VERSION = 3
TASK_ID_RE = re.compile(r"^SM-[0-9]{3}$")
ISSUE_URL_RE = re.compile(
    r"^https://github[.]com/[^/]+/[^/]+/issues/[1-9][0-9]*$"
)
STATUS_VALUES = frozenset({"blocked", "ready", "in-progress", "review", "done"})
ACTOR_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CLAIM_FIELDS = frozenset({"actor", "base", "branch", "remote", "lock_ref", "claimed_at"})
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


class HarnessError(RuntimeError):
    """A stable, user-safe lifecycle error."""

    def __init__(self, code: str, message: str, exit_code: int = 8):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class PathGuardError(HarnessError):
    """A path guard error with safe, sorted path details."""

    def __init__(
        self,
        rule: str,
        message: str,
        *,
        paths: list[str] | None = None,
        exit_code: int = 2,
    ):
        super().__init__(rule, message, exit_code)
        self.paths = sorted(set(paths or []))


class CheckFailure(HarnessError):
    """A safe check failure with a non-persistent summary."""

    def __init__(
        self,
        code: str,
        message: str,
        results: list[dict[str, Any]],
    ):
        super().__init__(code, message, CHECK_FAILED)
        self.results = results


class PolicyError(HarnessError):
    """A trusted PR-policy error with safe path details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        paths: list[str] | None = None,
        exit_code: int = 2,
    ):
        super().__init__(code, message, exit_code)
        self.paths = sorted(set(paths or []))


CLAIM_INVALID = 2
CLAIM_SAME_TASK_LOCK = "CLAIM_SAME_TASK_LOCK"
CLAIM_SHARED_LOCK = "CLAIM_SHARED_LOCK"
CLAIM_PATH_CONFLICT = "CLAIM_PATH_CONFLICT"
CLAIM_LOCK_RACE = "CLAIM_LOCK_RACE"
CLAIM_PRECONDITION = "CLAIM_PRECONDITION"
CLAIM_MUTATION_FAILED = "CLAIM_MUTATION_FAILED"
CLAIM_CLEANUP_FAILED = "CLAIM_CLEANUP_FAILED"
RELEASE_PRECONDITION = "RELEASE_PRECONDITION"
RELEASE_ACTOR = "RELEASE_ACTOR"
RELEASE_LOCK = "RELEASE_LOCK"
PATH_INVALID = 2
PATH_DISALLOWED = 4
CHECK_FAILED = 5
CHECK_TIMEOUT_SECONDS = 60


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
        for field in sorted(set(claim) - CLAIM_FIELDS):
            _error(errors, f"task {task_id}.claim.{field}", "unknown field")
        for field in sorted(CLAIM_FIELDS - set(claim)):
            _error(errors, f"task {task_id}.claim.{field}", "required field is missing")
        for field in CLAIM_FIELDS:
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


def _run_git(
    repo: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_text,
            text=True,
            capture_output=True,
            shell=False,
            env=env,
        )
    except OSError as exc:
        raise HarnessError("GIT_UNAVAILABLE", "git is unavailable") from exc


def _git_output(
    repo: Path,
    args: list[str],
    *,
    code: str = CLAIM_PRECONDITION,
    message: str = "git precondition failed",
) -> str:
    result = _run_git(repo, args)
    if result.returncode != 0:
        raise HarnessError(code, message)
    return result.stdout.strip()


def _validated_queue(queue_path: Path) -> dict[str, Any]:
    try:
        queue = load_queue(queue_path)
    except QueueError as exc:
        raise HarnessError("QUEUE_INVALID", "queue cannot be read", CLAIM_INVALID) from exc
    if validate_queue(queue):
        raise HarnessError("QUEUE_INVALID", "queue validation failed", CLAIM_INVALID)
    return queue


def _task_for_id(queue: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = next((item for item in queue["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise HarnessError("TASK_NOT_FOUND", "task is not registered", CLAIM_INVALID)
    return task


def _current_task(
    queue: dict[str, Any],
    task_id: str,
    excluded_ids: set[str] | None = None,
) -> dict[str, Any]:
    task = _task_for_id(queue, task_id)
    excluded_ids = excluded_ids or set()
    selected = [
        candidate
        for candidate in selectable_tasks(queue)
        if candidate["id"] not in excluded_ids
    ]
    baseline = selectable_tasks(queue)
    task_is_current_locked = (
        task_id in excluded_ids
        and baseline
        and baseline[0]["id"] == task_id
    )
    if task["status"] != "ready" or (
        not task_is_current_locked
        and (not selected or selected[0]["id"] != task_id)
    ):
        raise HarnessError("TASK_NOT_SELECTABLE", "task is not the current selectable task")
    return task


def _validate_actor(actor: str) -> None:
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise HarnessError("INVALID_ACTOR", "actor must be a lowercase slug", CLAIM_INVALID)


def _validate_sha(value: str, field: str) -> None:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        code = "INVALID_COMMIT" if field == "commit" else "INVALID_BASE"
        raise HarnessError(code, f"{field} must be a 40-character commit", CLAIM_INVALID)


def _require_clean_main(repo: Path, remote: str, base: str) -> str:
    status = _run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode != 0 or status.stdout.strip():
        raise HarnessError(CLAIM_PRECONDITION, "worktree and index must be clean")
    branch = _git_output(repo, ["branch", "--show-current"])
    if branch != "main":
        raise HarnessError(CLAIM_PRECONDITION, "current branch must be main")
    upstream = _git_output(
        repo,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
    )
    if upstream != f"{remote}/main":
        raise HarnessError(CLAIM_PRECONDITION, "main upstream does not match remote")
    divergence = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if divergence != "0\t0" and divergence != "0 0":
        raise HarnessError(CLAIM_PRECONDITION, "main is not synchronized with its upstream")
    head = _git_output(repo, ["rev-parse", "HEAD"])
    if not SHA_RE.fullmatch(head) or head != base:
        raise HarnessError(CLAIM_PRECONDITION, "base must equal local HEAD")
    remote_url = _run_git(repo, ["remote", "get-url", remote])
    if remote_url.returncode != 0:
        raise HarnessError(CLAIM_PRECONDITION, "remote is not configured")
    reachable = _run_git(repo, ["ls-remote", "--refs", remote])
    if reachable.returncode != 0:
        raise HarnessError(CLAIM_PRECONDITION, "remote is not reachable")
    return branch


def _lock_refs(repo: Path, remote: str) -> dict[str, str]:
    result = _run_git(repo, ["ls-remote", "--refs", remote, "refs/heads/harness-lock/*"])
    if result.returncode != 0:
        raise HarnessError(CLAIM_PRECONDITION, "remote lock refs cannot be enumerated")
    refs: dict[str, str] = {}
    prefix = "refs/heads/harness-lock/"
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not SHA_RE.fullmatch(fields[0]) or not fields[1].startswith(prefix):
            raise HarnessError(CLAIM_PRECONDITION, "remote lock ref is malformed")
        lock_name = fields[1][len(prefix) :]
        if not re.fullmatch(r"sm-[0-9]{3}", lock_name):
            raise HarnessError(CLAIM_PRECONDITION, "remote lock ref is malformed")
        task_id = lock_name.upper()
        refs[task_id] = fields[0]
    return refs


def _glob_prefix(pattern: str) -> str:
    match = re.search(r"[*?[]", pattern)
    return pattern if match is None else pattern[: match.start()]


def _patterns_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    left_prefix = _glob_prefix(left)
    right_prefix = _glob_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def _lock_conflict(
    queue: dict[str, Any],
    task: dict[str, Any],
    locked_ids: set[str],
) -> tuple[str, str] | None:
    by_id = {item["id"]: item for item in queue["tasks"]}
    shared_locks = queue["policy"]["shared_locks"]
    for locked_id in sorted(locked_ids):
        if locked_id == task["id"]:
            return CLAIM_SAME_TASK_LOCK, "task already has a remote lock"
        locked_task = by_id.get(locked_id)
        if locked_task is None:
            raise HarnessError(CLAIM_PRECONDITION, "remote lock references unknown task")
        if any(
            _patterns_overlap(path, shared)
            for path in task["allowed_paths"] + locked_task["allowed_paths"]
            for shared in shared_locks
        ):
            return CLAIM_SHARED_LOCK, "task uses a policy shared-lock path"
        if any(
            _patterns_overlap(left, right)
            for left in task["allowed_paths"]
            for right in locked_task["allowed_paths"]
        ):
            return CLAIM_PATH_CONFLICT, "task allowed paths overlap a remote lock"
    return None


def _claimed_at() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    nanoseconds = time.time_ns() % 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond:06d}{nanoseconds:03d}Z"


def _lock_ref(task_id: str) -> str:
    return f"refs/heads/harness-lock/{task_id.lower()}"


def _lock_commit(
    repo: Path,
    base: str,
    payload: dict[str, str],
) -> str:
    tree = _git_output(repo, ["rev-parse", "HEAD^{tree}"], message="lock tree cannot be read")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "agent-harness",
            "GIT_AUTHOR_EMAIL": "agent-harness@localhost",
            "GIT_COMMITTER_NAME": "agent-harness",
            "GIT_COMMITTER_EMAIL": "agent-harness@localhost",
        }
    )
    result = _run_git(
        repo,
        ["commit-tree", tree, "-p", base, "-m", canonical_json(payload)],
        env=environment,
    )
    if result.returncode != 0 or not SHA_RE.fullmatch(result.stdout.strip()):
        raise HarnessError(CLAIM_PRECONDITION, "lock commit cannot be created")
    return result.stdout.strip()


def _push_lock(repo: Path, remote: str, commit: str, ref: str) -> None:
    result = _run_git(repo, ["push", remote, f"{commit}:{ref}"])
    if result.returncode != 0:
        raise HarnessError(CLAIM_LOCK_RACE, "remote lock was claimed concurrently")


def _delete_lock(repo: Path, remote: str, ref: str, expected: str) -> None:
    refs = _lock_refs(repo, remote)
    task_id = ref.rsplit("/", 1)[-1].upper()
    if refs.get(task_id) != expected:
        raise HarnessError(CLAIM_CLEANUP_FAILED, "new lock ref changed before cleanup")
    result = _run_git(repo, ["push", remote, f":{ref}"])
    if result.returncode != 0:
        raise HarnessError(CLAIM_CLEANUP_FAILED, "new lock ref could not be removed")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise HarnessError(CLAIM_MUTATION_FAILED, "queue mutation failed") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _write_claim(path: Path, claim: dict[str, str]) -> None:
    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(CLAIM_MUTATION_FAILED, "queue mutation failed") from exc
    task_id = claim["task_id"]
    block_pattern = re.compile(
        rf"(?ms)^(?P<indent> *)- id: {re.escape(task_id)}\n"
        rf".*?(?=^(?P=indent)- id: |\Z)"
    )
    match = block_pattern.search(text)
    if match is None:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task block is missing")
    block = match.group(0)
    field_indent = match.group("indent") + "  "
    nested_indent = field_indent + "  "
    status_pattern = re.compile(
        rf"^{re.escape(field_indent)}status: ready$", re.MULTILINE
    )
    if len(status_pattern.findall(block)) != 1:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task status is not ready")
    claim_null = f"{field_indent}claim: null"
    if block.count(claim_null) != 1:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task claim is not null")
    updated = status_pattern.sub(f"{field_indent}status: in-progress", block, count=1)
    claim_text = (
        f"{field_indent}claim:\n"
        f"{nested_indent}actor: {claim['actor']}\n"
        f"{nested_indent}base: {claim['base']}\n"
        f"{nested_indent}branch: {claim['branch']}\n"
        f"{nested_indent}remote: {claim['remote']}\n"
        f"{nested_indent}lock_ref: {claim['lock_ref']}\n"
        f"{nested_indent}claimed_at: '{claim['claimed_at']}'\n"
    )
    updated = updated.replace(claim_null, claim_text, 1)
    _atomic_write(path, (text[: match.start()] + updated + text[match.end() :]).encode("utf-8"))


def claim_task(
    task_id: str,
    actor: str,
    remote: str,
    base: str,
    *,
    repo: Path = ROOT,
    queue_path: Path | None = None,
    mutate: Callable[[Path, dict[str, str]], None] = _write_claim,
) -> dict[str, Any]:
    queue_path = queue_path or repo / "execution" / "tasks.yaml"
    _validate_actor(actor)
    _validate_sha(base, "base")
    queue = _validated_queue(queue_path)
    original_branch = _require_clean_main(repo, remote, base)
    branch = f"agent/{task_id.lower()}-{actor}"
    branch_check = _run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if branch_check.returncode == 0:
        raise HarnessError(CLAIM_PRECONDITION, "working branch already exists")
    locked = _lock_refs(repo, remote)
    task = _current_task(queue, task_id, set(locked))
    if conflict := _lock_conflict(queue, task, set(locked)):
        raise HarnessError(conflict[0], conflict[1])

    claimed_at = _claimed_at()
    ref = _lock_ref(task_id)
    payload = {
        "task_id": task_id,
        "actor": actor,
        "branch": branch,
        "base_commit": base,
        "claimed_at": claimed_at,
    }
    claim = {
        "task_id": task_id,
        "actor": actor,
        "base": base,
        "branch": branch,
        "remote": remote,
        "lock_ref": ref,
        "claimed_at": claimed_at,
    }
    original_bytes = queue_path.read_bytes()
    commit = _lock_commit(repo, base, payload)
    _push_lock(repo, remote, commit, ref)
    switched = False
    try:
        switched_result = _run_git(repo, ["switch", "-c", branch])
        if switched_result.returncode != 0:
            raise HarnessError(CLAIM_MUTATION_FAILED, "working branch could not be created")
        switched = True
        mutate(queue_path, claim)
        updated = _validated_queue(queue_path)
        updated_task = _task_for_id(updated, task_id)
        if updated_task["status"] != "in-progress" or updated_task.get("claim") != {
            key: value for key, value in claim.items() if key != "task_id"
        }:
            raise HarnessError(CLAIM_MUTATION_FAILED, "queue claim mutation is invalid")
        return {
            "queue_version": updated["version"],
            "task": task_output(updated_task),
            "claim": updated_task["claim"],
        }
    except HarnessError as error:
        cleanup_error: HarnessError | None = None
        if switched:
            try:
                _atomic_write(queue_path, original_bytes)
            except HarnessError as restore_error:
                cleanup_error = restore_error
            switched_back = _run_git(repo, ["switch", original_branch])
            if switched_back.returncode != 0:
                cleanup_error = HarnessError(CLAIM_CLEANUP_FAILED, "original branch could not be restored")
        try:
            _delete_lock(repo, remote, ref, commit)
        except HarnessError as lock_error:
            cleanup_error = lock_error
        if cleanup_error is not None:
            raise cleanup_error from error
        raise


def context_task(
    task_id: str,
    *,
    queue_path: Path = DEFAULT_QUEUE,
) -> dict[str, Any]:
    queue = _validated_queue(queue_path)
    task = _task_for_id(queue, task_id)
    return {
        "queue_version": queue["version"],
        "task": task_output(task),
        "claim": task.get("claim"),
    }


def _fetch_remote_file(repo: Path, remote: str, revision: str, path: str) -> tuple[str, str]:
    fetched = _run_git(repo, ["fetch", "--quiet", remote, revision])
    if fetched.returncode != 0:
        raise HarnessError(RELEASE_PRECONDITION, "remote main cannot be fetched")
    commit = _git_output(repo, ["rev-parse", "FETCH_HEAD"], code=RELEASE_PRECONDITION, message="remote main cannot be resolved")
    result = _run_git(repo, ["show", f"{commit}:{path}"])
    if result.returncode != 0:
        raise HarnessError(RELEASE_PRECONDITION, "remote queue cannot be read")
    return commit, result.stdout


def _lock_payload(repo: Path, remote: str, ref: str) -> tuple[str, dict[str, str]]:
    refs = _lock_refs(repo, remote)
    task_id = ref.rsplit("/", 1)[-1].upper()
    commit = refs.get(task_id)
    if commit is None:
        raise HarnessError(RELEASE_LOCK, "matching lock ref is missing")
    fetched = _run_git(repo, ["fetch", "--quiet", remote, ref])
    if fetched.returncode != 0:
        raise HarnessError(RELEASE_LOCK, "matching lock payload cannot be fetched")
    message = _git_output(
        repo,
        ["show", "-s", "--format=%B", commit],
        code=RELEASE_LOCK,
        message="matching lock payload cannot be read",
    )
    lines = [line for line in message.splitlines() if line]
    if len(lines) != 1:
        raise HarnessError(RELEASE_LOCK, "matching lock payload is malformed")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise HarnessError(RELEASE_LOCK, "matching lock payload is malformed") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"task_id", "actor", "branch", "base_commit", "claimed_at"}
        or canonical_json(payload) != lines[0]
        or not all(isinstance(payload.get(field), str) and payload[field] for field in payload)
        or not TASK_ID_RE.fullmatch(payload["task_id"])
        or not SHA_RE.fullmatch(payload["base_commit"])
    ):
        raise HarnessError(RELEASE_LOCK, "matching lock payload is malformed")
    return commit, payload


def _evidence_commits(task: dict[str, Any]) -> list[str]:
    commits: list[str] = []
    for evidence in task.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        for key in ("merge_commit", "commit"):
            value = evidence.get(key)
            if isinstance(value, str) and SHA_RE.fullmatch(value):
                commits.append(value)
    return commits


def release_task(
    task_id: str,
    actor: str,
    remote: str,
    *,
    repo: Path = ROOT,
    queue_path: Path | None = None,
) -> dict[str, Any]:
    queue_path = queue_path or repo / "execution" / "tasks.yaml"
    _validate_actor(actor)
    local_queue = _validated_queue(queue_path)
    local_task = _task_for_id(local_queue, task_id)
    local_claim = local_task.get("claim")
    if local_task["status"] != "in-progress" or not isinstance(local_claim, dict):
        raise HarnessError(RELEASE_PRECONDITION, "local task is not actively claimed")
    current_branch = _git_output(repo, ["branch", "--show-current"], code=RELEASE_PRECONDITION, message="current branch cannot be read")
    ref = _lock_ref(task_id)
    lock_commit, payload = _lock_payload(repo, remote, ref)
    if payload["task_id"] != task_id or payload["actor"] != actor:
        raise HarnessError(RELEASE_ACTOR, "actor or task does not match lock payload")
    if payload["branch"] != current_branch or local_claim.get("branch") != payload["branch"]:
        raise HarnessError(RELEASE_ACTOR, "current branch does not match lock payload")
    if (
        local_claim.get("actor") != actor
        or local_claim.get("base") != payload["base_commit"]
        or local_claim.get("remote") != remote
    ):
        raise HarnessError(RELEASE_ACTOR, "local claim does not match lock payload")

    main_commit, main_text = _fetch_remote_file(repo, remote, "refs/heads/main", "execution/tasks.yaml")
    try:
        remote_queue_value = yaml.safe_load(main_text)
    except yaml.YAMLError as exc:
        raise HarnessError(RELEASE_PRECONDITION, "remote queue is malformed") from exc
    if not isinstance(remote_queue_value, dict) or validate_queue(remote_queue_value):
        raise HarnessError(RELEASE_PRECONDITION, "remote queue is invalid")
    remote_task = _task_for_id(remote_queue_value, task_id)
    if remote_task["status"] != "done" or remote_task.get("claim") is not None or not remote_task["evidence"]:
        raise HarnessError(RELEASE_PRECONDITION, "remote task is not a completed task with evidence")
    evidence_commits = _evidence_commits(remote_task)
    if not evidence_commits:
        raise HarnessError(RELEASE_PRECONDITION, "remote task has no full evidence commit")
    if not any(
        _run_git(repo, ["merge-base", "--is-ancestor", commit, main_commit]).returncode == 0
        for commit in evidence_commits
    ):
        raise HarnessError(RELEASE_PRECONDITION, "remote main does not contain task evidence commit")
    _delete_lock(repo, remote, ref, lock_commit)
    return {"task_id": task_id, "lock_ref": ref, "released": True}


def _safe_path_for_error(path: str) -> str:
    if (
        not isinstance(path, str)
        or path.startswith("/")
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path)
    ):
        return "<invalid-path>"
    return path


def _normalize_git_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or path.startswith("/")
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise PathGuardError(
            "INVALID_PATH",
            "Git returned an invalid relative path",
            paths=[_safe_path_for_error(path)],
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathGuardError(
            "INVALID_PATH",
            "Git returned an invalid relative path",
            paths=[_safe_path_for_error(path)],
        )
    return path


_DIFF_STATUS_RE = re.compile(r"^[ACDMRTUXB][0-9]*$")


def _parse_diff_paths(output: str) -> list[str]:
    paths: list[str] = []
    fields = output.split("\x00")
    index = 0
    while index < len(fields):
        token = fields[index]
        if not token:
            index += 1
            continue
        status: str
        first_path: str | None = None
        if "\t" in token:
            status, first_path = token.split("\t", 1)
            index += 1
        elif _DIFF_STATUS_RE.fullmatch(token):
            status = token
            index += 1
        else:
            index += 1
            continue
        if first_path:
            paths.append(first_path)
        path_count = 2 if status[:1] in {"R", "C"} else 1
        for _ in range(path_count):
            if index >= len(fields) or not fields[index]:
                break
            paths.append(fields[index])
            index += 1
    return paths


def _parse_status_paths(output: str) -> list[str]:
    paths: list[str] = []
    fields = output.split("\x00")
    index = 0
    while index < len(fields):
        token = fields[index]
        index += 1
        if not token:
            continue
        if len(token) < 3 or token[2] != " ":
            raise PathGuardError("INVALID_GIT_STATUS", "Git status format is ambiguous")
        status = token[:2]
        paths.append(token[3:])
        if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}:
            if index >= len(fields) or not fields[index]:
                raise PathGuardError("INVALID_GIT_STATUS", "Git rename status is incomplete")
            paths.append(fields[index])
            index += 1
    return paths


def _git_path_output(repo: Path, args: list[str]) -> str:
    result = _run_git(repo, args)
    if result.returncode != 0:
        raise PathGuardError("GIT_STATE_INVALID", "Git path state cannot be inspected")
    return result.stdout


def _changed_git_paths(
    repo: Path,
    base: str,
    head: str,
    *,
    committed_only: bool,
) -> list[str]:
    raw_paths: list[str] = []
    raw_paths.extend(
        _parse_diff_paths(
            _git_path_output(
                repo,
                ["diff", "--no-ext-diff", "--name-status", "-z", "-M", "-C", base, head],
            )
        )
    )
    if not committed_only:
        raw_paths.extend(
            _parse_status_paths(
                _git_path_output(
                    repo,
                    ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                )
            )
        )
        for diff_args in (
            ["diff", "--no-ext-diff", "--name-status", "-z", "-M", "-C"],
            ["diff", "--no-ext-diff", "--cached", "--name-status", "-z", "-M", "-C"],
        ):
            raw_paths.extend(
                _parse_diff_paths(_git_path_output(repo, diff_args))
            )
    return sorted({_normalize_git_path(path) for path in raw_paths})


def _glob_regex(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    expression = "^"
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            if last:
                expression += r"(?:[^/]+/)*[^/]+"
            else:
                expression += r"(?:[^/]+/)*"
            continue
        for character in segment:
            if character == "*":
                expression += r"[^/]*"
            elif character == "?":
                expression += r"[^/]"
            else:
                expression += re.escape(character)
        if not last:
            expression += "/"
    return re.compile(expression + "$")


def _path_allowed(path: str, allowed_patterns: list[str]) -> bool:
    return any(_glob_regex(pattern).fullmatch(path) for pattern in allowed_patterns)


def _guard_commit(repo: Path, value: str | None, field: str) -> str:
    if value is None:
        result = _run_git(repo, ["rev-parse", "HEAD"])
        if result.returncode != 0:
            raise PathGuardError("GIT_STATE_INVALID", "Git HEAD cannot be resolved")
        value = result.stdout.strip()
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise PathGuardError("INVALID_COMMIT", f"{field} must be a 40-character commit")
    result = _run_git(repo, ["cat-file", "-e", f"{value}^{{commit}}"])
    if result.returncode != 0:
        raise PathGuardError("INVALID_COMMIT", f"{field} is not a commit")
    return value


def verify_paths(
    task_id: str,
    *,
    base: str | None = None,
    head: str | None = None,
    committed_only: bool = False,
    repo: Path = ROOT,
    queue_path: Path | None = None,
    policy_queue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    queue_path = queue_path or repo / "execution" / "tasks.yaml"
    if policy_queue is None:
        try:
            queue = _validated_queue(queue_path)
        except HarnessError as error:
            raise PathGuardError(error.code, error.message) from error
    else:
        queue = policy_queue
    try:
        task = _task_for_id(queue, task_id)
    except HarnessError as error:
        raise PathGuardError(error.code, error.message) from error
    claim = task.get("claim")
    if claim is not None:
        if task["status"] != "in-progress" or not isinstance(claim, dict):
            raise PathGuardError("CLAIM_INVALID", "task claim is invalid")
        current_branch = _run_git(repo, ["branch", "--show-current"])
        if current_branch.returncode != 0 or current_branch.stdout.strip() != claim.get("branch"):
            raise PathGuardError("CLAIM_BRANCH_MISMATCH", "current branch does not match active claim")
        claimed_base = claim.get("base")
        if base is None:
            base = claimed_base
        elif base != claimed_base:
            raise PathGuardError("CLAIM_BASE_MISMATCH", "base does not match active claim")
    elif base is None:
        raise PathGuardError("ACTIVE_CLAIM_REQUIRED", "base is required without an active claim")
    base_commit = _guard_commit(repo, base, "base")
    head_commit = _guard_commit(repo, head, "head")
    ancestor = _run_git(repo, ["merge-base", "--is-ancestor", base_commit, head_commit])
    if ancestor.returncode != 0:
        raise PathGuardError("BASE_NOT_ANCESTOR", "base is not an ancestor of head")
    changed_paths = _changed_git_paths(
        repo,
        base_commit,
        head_commit,
        committed_only=committed_only,
    )
    offending = [
        path for path in changed_paths if not _path_allowed(path, task["allowed_paths"])
    ]
    if offending:
        raise PathGuardError(
            "PATH_DISALLOWED",
            "one or more changed paths are outside the task allowance",
            paths=offending,
            exit_code=PATH_DISALLOWED,
        )
    return {
        "task": task_id,
        "base": base_commit,
        "head": head_commit,
        "paths": changed_paths,
    }


def _active_task_for_verification(
    task_id: str,
    *,
    repo: Path,
    queue_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    queue = _validated_queue(queue_path)
    task = _task_for_id(queue, task_id)
    claim = task.get("claim")
    if task["status"] != "in-progress" or not isinstance(claim, dict):
        raise HarnessError("CLAIM_REQUIRED", "task must have an active claim", CLAIM_INVALID)
    branch = _git_output(
        repo,
        ["branch", "--show-current"],
        code="CLAIM_BRANCH_MISMATCH",
        message="current branch cannot be read",
    )
    if branch == "main" or branch != claim.get("branch"):
        raise HarnessError("CLAIM_BRANCH_MISMATCH", "current branch does not match active claim", CLAIM_INVALID)
    return task, claim


def _safe_check_argv(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise HarnessError("UNSAFE_CHECK", "check command is empty or invalid", CHECK_FAILED)
    if "\n" in command or "\r" in command:
        raise HarnessError("UNSAFE_CHECK", "check command contains a newline", CHECK_FAILED)
    if any(character in command for character in ";|&<>") or chr(96) in command:
        raise HarnessError("UNSAFE_CHECK", "check command contains shell control syntax", CHECK_FAILED)
    if "$(" in command or "$" + "{" in command:
        raise HarnessError("UNSAFE_CHECK", "check command contains command substitution", CHECK_FAILED)
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise HarnessError("UNSAFE_CHECK", "check command cannot be parsed", CHECK_FAILED) from exc
    if not argv or argv[0] == "env":
        raise HarnessError("UNSAFE_CHECK", "check command has no safe executable", CHECK_FAILED)
    control_tokens = {";", "&&", "||", "|", ">", ">>", "<", "<<", "&", "(", ")"}
    if any(token in control_tokens for token in argv):
        raise HarnessError("UNSAFE_CHECK", "check command contains shell control token", CHECK_FAILED)
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) for token in argv):
        raise HarnessError("UNSAFE_CHECK", "check command has an environment assignment", CHECK_FAILED)
    return argv


def _is_nested_verify(argv: list[str], task_id: str) -> bool:
    if argv[0] not in {"python", "python3"}:
        return False
    if len(argv) >= 4 and argv[2] == "verify" and argv[3] == task_id:
        return argv[1].replace("\\", "/").endswith("tools/task_harness.py")
    return (
        len(argv) >= 5
        and argv[1].replace("\\", "/").endswith("tools/agent_runtime.py")
        and argv[2].replace("\\", "/").endswith("tools/task_harness.py")
        and argv[3] == "verify"
        and argv[4] == task_id
    )


def _is_policy_command(argv: list[str]) -> bool:
    return "verify-pr" in argv


def _run_declared_checks(
    task_id: str,
    checks: list[str],
    *,
    repo: Path,
    argv_transform: Callable[[list[str]], list[str]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in checks:
        try:
            argv = _safe_check_argv(command)
        except HarnessError as error:
            results.append({"command": command, "exit_code": None, "status": "failed"})
            raise CheckFailure(error.code, error.message, results) from error
        if argv_transform is not None:
            argv = argv_transform(argv)
        environment = None
        if _is_nested_verify(argv, task_id) or _is_policy_command(argv):
            environment = os.environ.copy()
            if _is_nested_verify(argv, task_id):
                environment["TASK_HARNESS_NESTED_VERIFY"] = "1"
            if _is_policy_command(argv):
                environment["TASK_HARNESS_NESTED_VERIFY_PR"] = "1"
        failure_code = "CHECK_FAILED"
        try:
            completed = subprocess.run(
                argv,
                cwd=repo,
                shell=False,
                timeout=CHECK_TIMEOUT_SECONDS,
                env=environment,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            exit_code: int | None = completed.returncode
            status = "passed" if completed.returncode == 0 else "failed"
        except FileNotFoundError:
            exit_code = None
            status = "failed"
            failure_code = "MISSING_EXECUTABLE"
        except subprocess.TimeoutExpired:
            exit_code = None
            status = "failed"
            failure_code = "CHECK_TIMEOUT"
        result = {"command": command, "exit_code": exit_code, "status": status}
        results.append(result)
        if status == "failed":
            raise CheckFailure(failure_code, "declared check failed", results)
    return results


def verify_task(
    task_id: str,
    *,
    repo: Path = ROOT,
    queue_path: Path | None = None,
) -> dict[str, Any]:
    queue_path = queue_path or repo / "execution" / "tasks.yaml"
    task, claim = _active_task_for_verification(
        task_id,
        repo=repo,
        queue_path=queue_path,
    )
    path_result = verify_paths(
        task_id,
        base=claim["base"],
        repo=repo,
        queue_path=queue_path,
    )
    check_results = _run_declared_checks(task_id, task["checks"], repo=repo)
    return {
        "task": task_id,
        "status": "passed",
        "path_guard": {"status": "passed", "paths": path_result["paths"]},
        "checks": check_results,
    }


def _completion_mutation(
    queue_path: Path,
    before_queue: dict[str, Any],
    task_id: str,
    pr: int,
    commit: str,
    evidence_checks: list[str],
) -> None:
    try:
        original = queue_path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "queue cannot be read", CLAIM_INVALID) from exc
    block_pattern = re.compile(
        rf"(?ms)^(?P<indent> *)- id: {re.escape(task_id)}\n"
        rf".*?(?=^(?P=indent)- id: |\Z)"
    )
    match = block_pattern.search(text)
    if match is None:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "selected task block is missing", CLAIM_INVALID)
    block = match.group(0)
    field_indent = match.group("indent") + "  "
    nested_indent = field_indent + "  "
    status_pattern = re.compile(
        rf"^{re.escape(field_indent)}status: in-progress$", re.MULTILINE
    )
    if len(status_pattern.findall(block)) != 1:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "task is not in progress", CLAIM_INVALID)
    claim_pattern = re.compile(
        rf"^{re.escape(field_indent)}claim:\n"
        rf"(?:^{re.escape(nested_indent)}.*\n)*",
        re.MULTILINE,
    )
    if claim_pattern.search(block) is None:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "active claim mapping is missing", CLAIM_INVALID)
    evidence_line = f"{field_indent}evidence: []"
    if block.count(evidence_line) != 1:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "evidence is not empty", CLAIM_INVALID)
    updated = status_pattern.sub(f"{field_indent}status: done", block, count=1)
    updated = claim_pattern.sub(f"{field_indent}claim: null\n", updated, count=1)
    evidence_text = (
        f"{field_indent}evidence:\n"
        f"{nested_indent}- pr: {pr}\n"
        f"{nested_indent}  commit: {commit}\n"
        f"{nested_indent}  checks:\n"
    )
    evidence_text += "".join(
        f"{nested_indent}    - {json.dumps(check, ensure_ascii=False)}\n"
        for check in evidence_checks
    )
    updated = updated.replace(evidence_line, evidence_text, 1)
    new_text = text[: match.start()] + updated + text[match.end() :]
    try:
        new_queue = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise HarnessError("COMPLETION_MUTATION_FAILED", "resulting queue is invalid", CLAIM_INVALID) from exc
    if not isinstance(new_queue, dict) or validate_queue(new_queue):
        raise HarnessError("COMPLETION_MUTATION_FAILED", "resulting queue is invalid", CLAIM_INVALID)
    before_tasks = {task["id"]: task for task in before_queue["tasks"]}
    after_tasks = {task["id"]: task for task in new_queue["tasks"]}
    if set(before_tasks) != set(after_tasks):
        raise HarnessError("COMPLETION_MUTATION_FAILED", "unrelated tasks changed", CLAIM_INVALID)
    expected_record = {"pr": pr, "commit": commit, "checks": evidence_checks}
    for current_id, before_task in before_tasks.items():
        after_task = after_tasks[current_id]
        if current_id != task_id and before_task != after_task:
            raise HarnessError("COMPLETION_MUTATION_FAILED", "unrelated task changed", CLAIM_INVALID)
        if current_id == task_id:
            expected = dict(before_task)
            expected["status"] = "done"
            expected["claim"] = None
            expected["evidence"] = before_task["evidence"] + [expected_record]
            if after_task != expected:
                raise HarnessError("COMPLETION_MUTATION_FAILED", "completion changed unexpected fields", CLAIM_INVALID)
    for key in before_queue:
        if key != "tasks" and before_queue[key] != new_queue.get(key):
            raise HarnessError("COMPLETION_MUTATION_FAILED", "completion changed queue metadata", CLAIM_INVALID)
    _atomic_write(queue_path, new_text.encode("utf-8"))


def complete_task(
    task_id: str,
    pr: int,
    commit: str,
    *,
    repo: Path = ROOT,
    queue_path: Path | None = None,
) -> dict[str, Any]:
    queue_path = queue_path or repo / "execution" / "tasks.yaml"
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise HarnessError("INVALID_PR", "pr must be a positive integer", CLAIM_INVALID)
    _validate_sha(commit, "commit")
    verification = verify_task(task_id, repo=repo, queue_path=queue_path)
    queue = _validated_queue(queue_path)
    task = _task_for_id(queue, task_id)
    claim = task.get("claim")
    if task["status"] != "in-progress" or not isinstance(claim, dict):
        raise HarnessError("CLAIM_REQUIRED", "task must have an active claim", CLAIM_INVALID)
    branch = _git_output(repo, ["branch", "--show-current"], code="CLAIM_BRANCH_MISMATCH", message="current branch cannot be read")
    if branch == "main" or branch != claim.get("branch"):
        raise HarnessError("CLAIM_BRANCH_MISMATCH", "current branch does not match active claim", CLAIM_INVALID)
    head = _git_output(repo, ["rev-parse", "HEAD"], code="INVALID_COMMIT", message="HEAD cannot be read")
    if head != commit:
        raise HarnessError("COMMIT_MISMATCH", "commit must equal current HEAD", CLAIM_INVALID)
    status = _run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode != 0 or status.stdout.strip():
        raise HarnessError("WORKTREE_DIRTY", "worktree and index must be clean", CLAIM_INVALID)
    if task["evidence"]:
        raise HarnessError("EVIDENCE_ALREADY_PRESENT", "task already has evidence", CLAIM_INVALID)
    evidence_checks = [
        f"python3 tools/agent_runtime.py tools/task_harness.py verify-paths {task_id} --json — passed"
    ] + [f"{result['command']} — passed" for result in verification["checks"]]
    _completion_mutation(queue_path, queue, task_id, pr, commit, evidence_checks)
    return {
        "task": task_id,
        "status": "done",
        "pr": pr,
        "commit": commit,
        "checks": evidence_checks,
    }


AGENT_BRANCH_RE = re.compile(r"^agent/sm-[0-9]{3}-[a-z0-9]+(?:-[a-z0-9]+)*$")
POLICY_IMMUTABLE_TASK_FIELDS = TASK_FIELDS - {"status", "claim", "evidence"}


def _queue_at_commit(repo: Path, commit: str) -> dict[str, Any]:
    result = _run_git(repo, ["show", f"{commit}:execution/tasks.yaml"])
    if result.returncode != 0:
        raise PolicyError("QUEUE_MISSING", "queue is missing at commit")
    try:
        queue = yaml.safe_load(result.stdout)
    except yaml.YAMLError as exc:
        raise PolicyError("QUEUE_INVALID", "queue is invalid at commit") from exc
    if not isinstance(queue, dict) or validate_queue(queue):
        raise PolicyError("QUEUE_INVALID", "queue is invalid at commit")
    return queue


def _policy_claim_matches(
    task: dict[str, Any],
    *,
    base: str,
    ref: str,
) -> bool:
    claim = task.get("claim")
    return (
        task["status"] == "in-progress"
        and isinstance(claim, dict)
        and claim.get("base") == base
        and claim.get("branch") == ref
        and claim.get("lock_ref") == f"refs/heads/harness-lock/{task['id'].lower()}"
    )


def _trusted_policy_argv(argv: list[str]) -> list[str]:
    if "verify-pr" not in argv:
        return argv
    transformed = list(argv)
    trusted_runtime = str(ROOT / "tools" / "agent_runtime.py")
    trusted_script = str(ROOT / "tools" / "task_harness.py")
    for index, token in enumerate(transformed):
        normalized = token.replace("\\", "/")
        if normalized.endswith("tools/agent_runtime.py"):
            transformed[index] = trusted_runtime
        elif normalized.endswith("tools/task_harness.py"):
            transformed[index] = trusted_script
    return transformed


def _policy_queue_contract(
    queue: dict[str, Any],
    trusted_queue: dict[str, Any],
    *,
    task_id: str,
) -> None:
    for field in ROOT_FIELDS - {"tasks"}:
        if queue.get(field) != trusted_queue.get(field):
            raise PolicyError("QUEUE_METADATA_CHANGED", "queue metadata changed")
    trusted_tasks = {task["id"]: task for task in trusted_queue["tasks"]}
    current_tasks = {task["id"]: task for task in queue["tasks"]}
    if set(trusted_tasks) != set(current_tasks):
        raise PolicyError("TASK_SET_CHANGED", "task set changed")
    for current_id in sorted(trusted_tasks):
        before = trusted_tasks[current_id]
        after = current_tasks[current_id]
        if current_id == task_id:
            continue
        if any(before.get(field) != after.get(field) for field in POLICY_IMMUTABLE_TASK_FIELDS):
            raise PolicyError("TASK_CONTRACT_CHANGED", "task contract changed")
        if before != after:
            raise PolicyError("LIFECYCLE_SCOPE_INVALID", "exactly one task lifecycle must change")


def _valid_completion_record(
    repo: Path,
    evidence: list[Any],
    head: str,
) -> bool:
    if not evidence or not isinstance(evidence[-1], dict):
        return False
    record = evidence[-1]
    if set(record) != {"pr", "commit", "checks"}:
        return False
    if not isinstance(record["pr"], int) or isinstance(record["pr"], bool) or record["pr"] <= 0:
        return False
    commit = record["commit"]
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        return False
    if _run_git(repo, ["cat-file", "-e", f"{commit}^{{commit}}"]).returncode != 0:
        return False
    if _run_git(repo, ["merge-base", "--is-ancestor", commit, head]).returncode != 0:
        return False
    return (
        isinstance(record["checks"], list)
        and bool(record["checks"])
        and all(isinstance(check, str) and bool(check) for check in record["checks"])
    )


def _policy_history(
    repo: Path,
    *,
    base: str,
    head: str,
    task_id: str,
    trusted_queue: dict[str, Any],
    ref: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    base_task = _task_for_id(trusted_queue, task_id)
    commits = _run_git(repo, ["rev-list", "--reverse", f"{base}..{head}"])
    if commits.returncode != 0:
        raise PolicyError("HISTORY_INVALID", "commit history cannot be inspected")
    snapshots = [base] + commits.stdout.split()
    seen_in_progress = base_task["status"] == "in-progress"
    seen_done = base_task["status"] == "done"
    previous_task = base_task
    final_queue = trusted_queue
    for commit_id in snapshots[1:]:
        queue = _queue_at_commit(repo, commit_id)
        _policy_queue_contract(queue, trusted_queue, task_id=task_id)
        task = _task_for_id(queue, task_id)
        trusted_task = _task_for_id(trusted_queue, task_id)
        if any(
            trusted_task.get(field) != task.get(field)
            for field in POLICY_IMMUTABLE_TASK_FIELDS
        ):
            raise PolicyError("TASK_CONTRACT_CHANGED", "task contract changed")
        if task["status"] == "ready":
            if seen_in_progress or seen_done or task != base_task:
                raise PolicyError("TRANSITION_INVALID", "lifecycle transition is not allowed")
        elif task["status"] == "in-progress":
            if seen_done or not _policy_claim_matches(task, base=base, ref=ref):
                raise PolicyError("CLAIM_INVALID", "head claim does not match branch and base")
            if task["evidence"] != base_task["evidence"]:
                raise PolicyError("EVIDENCE_INVALID", "in-progress task cannot add evidence")
            seen_in_progress = True
        elif task["status"] == "done":
            if not seen_in_progress or task["claim"] is not None:
                raise PolicyError("TRANSITION_INVALID", "lifecycle transition is not allowed")
            if not _valid_completion_record(repo, task["evidence"], commit_id):
                raise PolicyError("COMPLETION_INVALID", "done transition lacks valid evidence")
            seen_done = True
        else:
            raise PolicyError("TRANSITION_INVALID", "lifecycle transition is not allowed")
        if previous_task["status"] == "done" and task != previous_task:
            raise PolicyError("TRANSITION_INVALID", "done task lifecycle was changed")
        previous_task = task
        final_queue = queue
    return base_task, _task_for_id(final_queue, task_id), seen_in_progress


def verify_pr(
    *,
    repo: Path,
    task_id: str,
    base: str,
    head: str,
    ref: str,
    title: str,
) -> dict[str, Any]:
    try:
        trusted_queue = _validated_queue(DEFAULT_QUEUE)
    except HarnessError as error:
        raise PolicyError(error.code, error.message) from error
    if not TASK_ID_RE.fullmatch(task_id):
        raise PolicyError("TASK_ID_INVALID", "task ID is invalid")
    if not AGENT_BRANCH_RE.fullmatch(ref):
        raise PolicyError("BRANCH_INVALID", "agent branch is invalid")
    if not ref.startswith(f"agent/{task_id.lower()}-"):
        raise PolicyError("TASK_BRANCH_MISMATCH", "branch task ID does not match task")
    if not isinstance(title, str) or not title.startswith(f"[{task_id}]"):
        raise PolicyError("TITLE_MISMATCH", "PR title does not match task")
    base_commit = _guard_commit(repo, base, "base")
    head_commit = _guard_commit(repo, head, "head")
    candidate_head = _run_git(repo, ["rev-parse", "HEAD"])
    if candidate_head.returncode != 0 or candidate_head.stdout.strip() != head_commit:
        raise PolicyError("HEAD_MISMATCH", "candidate HEAD does not match head")
    if _run_git(repo, ["merge-base", "--is-ancestor", base_commit, head_commit]).returncode != 0:
        raise PolicyError("BASE_NOT_ANCESTOR", "base is not an ancestor of head")
    base_queue = _queue_at_commit(repo, base_commit)
    if base_queue != trusted_queue:
        raise PolicyError("TRUSTED_BASE_MISMATCH", "candidate base queue differs from trusted queue")
    head_queue = _queue_at_commit(repo, head_commit)
    base_task = _task_for_id(trusted_queue, task_id)
    head_task = _task_for_id(head_queue, task_id)
    if base_task["phase"] < 10:
        raise PolicyError("TASK_NOT_PHASE10", "task is not a Phase 10 task")

    lifecycle_changes: list[str] = []
    base_tasks = {task["id"]: task for task in trusted_queue["tasks"]}
    head_tasks = {task["id"]: task for task in head_queue["tasks"]}
    if set(base_tasks) != set(head_tasks):
        raise PolicyError("TASK_SET_CHANGED", "task set changed")
    for current_id in sorted(base_tasks):
        before = base_tasks[current_id]
        after = head_tasks[current_id]
        if any(before.get(field) != after.get(field) for field in POLICY_IMMUTABLE_TASK_FIELDS):
            raise PolicyError("TASK_CONTRACT_CHANGED", "task contract changed")
        if any(before.get(field) != after.get(field) for field in ("status", "claim", "evidence")):
            lifecycle_changes.append(current_id)
    if lifecycle_changes != [task_id]:
        raise PolicyError("LIFECYCLE_SCOPE_INVALID", "exactly one task lifecycle must change")

    _policy_queue_contract(head_queue, trusted_queue, task_id=task_id)
    history_base_task, history_head_task, seen_in_progress = _policy_history(
        repo,
        base=base_commit,
        head=head_commit,
        task_id=task_id,
        trusted_queue=trusted_queue,
        ref=ref,
    )
    if history_base_task != base_task or history_head_task != head_task:
        raise PolicyError("HISTORY_INVALID", "commit history does not match the PR queue")
    if base_task["status"] == "ready" and head_task["status"] == "in-progress":
        if not seen_in_progress:
            raise PolicyError("TRANSITION_INVALID", "lifecycle transition is not allowed")
    elif base_task["status"] == "in-progress" and head_task["status"] == "done":
        if not _policy_claim_matches(base_task, base=base_commit, ref=ref):
            raise PolicyError("CLAIM_INVALID", "base claim does not match branch and base")
    elif base_task["status"] == "ready" and head_task["status"] == "done":
        if not seen_in_progress:
            raise PolicyError("TRANSITION_INVALID", "ready to done requires an intermediate in-progress commit")
    else:
        raise PolicyError("TRANSITION_INVALID", "lifecycle transition is not allowed")

    try:
        path_result = verify_paths(
            task_id,
            base=base_commit,
            head=head_commit,
            committed_only=True,
            repo=repo,
            queue_path=repo / "execution/tasks.yaml",
            policy_queue=trusted_queue,
        )
    except PathGuardError as error:
        raise PolicyError(error.code, error.message, paths=error.paths, exit_code=error.exit_code) from error

    expanded_checks = [
        command.replace("<claim-base>", base_commit).replace("<task-branch>", ref)
        for command in base_task["checks"]
    ]
    check_results = _run_declared_checks(
        task_id,
        expanded_checks,
        repo=repo,
        argv_transform=_trusted_policy_argv,
    )
    return {
        "task": task_id,
        "status": "passed",
        "base": base_commit,
        "head": head_commit,
        "ref": ref,
        "paths": path_result["paths"],
        "checks": check_results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task_harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "next"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
        subparser.add_argument("--json", action="store_true")
    claim = subparsers.add_parser("claim")
    claim.add_argument("task_id")
    claim.add_argument("--actor", required=True)
    claim.add_argument("--remote", required=True)
    claim.add_argument("--base", required=True)
    claim.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    claim.add_argument("--json", action="store_true")
    release = subparsers.add_parser("release")
    release.add_argument("task_id")
    release.add_argument("--actor", required=True)
    release.add_argument("--remote", required=True)
    release.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    release.add_argument("--json", action="store_true")
    context = subparsers.add_parser("context")
    context.add_argument("task_id")
    context.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    context.add_argument("--json", action="store_true")
    verify = subparsers.add_parser("verify-paths")
    verify.add_argument("task_id")
    verify.add_argument("--base")
    verify.add_argument("--head")
    verify.add_argument("--committed-only", action="store_true")
    verify.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    verify.add_argument("--json", action="store_true")
    run_checks = subparsers.add_parser("verify")
    run_checks.add_argument("task_id")
    run_checks.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    run_checks.add_argument("--json", action="store_true")
    complete = subparsers.add_parser("complete")
    complete.add_argument("task_id")
    complete.add_argument("--pr", required=True, type=int)
    complete.add_argument("--commit", required=True)
    complete.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    complete.add_argument("--json", action="store_true")
    policy = subparsers.add_parser("verify-pr")
    policy.add_argument("--repo", type=Path, required=True)
    policy.add_argument("--task", dest="task_id", required=True)
    policy.add_argument("--base", required=True)
    policy.add_argument("--head", required=True)
    policy.add_argument("--ref", required=True)
    policy.add_argument("--title", required=True)
    policy.add_argument("--json", action="store_true")
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


def _write_error(error: HarnessError, *, as_json: bool) -> None:
    result = {"error": {"code": error.code, "message": error.message}}
    if as_json:
        sys.stderr.write(canonical_json(result) + "\n")
    else:
        sys.stderr.write(f"{error.code}: {error.message}\n")


def _write_path_guard_error(task_id: str, error: PathGuardError, *, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(
            canonical_json(
                {
                    "task": task_id,
                    "rule": error.code,
                    "paths": sorted(error.paths),
                }
            )
            + "\n"
        )
    else:
        sys.stderr.write(f"{error.code}: {error.message}\n")


def _write_check_failure(task_id: str, error: CheckFailure, *, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(
            canonical_json(
                {
                    "task": task_id,
                    "status": "failed",
                    "checks": error.results,
                }
            )
            + "\n"
        )
    else:
        sys.stderr.write(f"{error.code}: {error.message}\n")


def _write_policy_error(task_id: str, error: PolicyError, *, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(
            canonical_json(
                {
                    "task": task_id,
                    "rule": error.code,
                    "paths": sorted(error.paths),
                }
            )
            + "\n"
        )
    else:
        sys.stderr.write(f"{error.code}: {error.message}\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify-paths":
        try:
            result = verify_paths(
                args.task_id,
                base=args.base,
                head=args.head,
                committed_only=args.committed_only,
                repo=args.queue.parent.parent,
                queue_path=args.queue,
            )
            if args.json:
                sys.stdout.write(canonical_json(result) + "\n")
            else:
                sys.stdout.write(f"{result['task']}: {len(result['paths'])} changed paths allowed\n")
            return 0
        except PathGuardError as error:
            _write_path_guard_error(args.task_id, error, as_json=args.json)
            return error.exit_code
    if args.command == "verify-pr":
        try:
            if os.environ.get("TASK_HARNESS_NESTED_VERIFY_PR") == "1":
                result = {"task": args.task_id, "status": "passed", "checks": []}
            else:
                result = verify_pr(
                    repo=args.repo,
                    task_id=args.task_id,
                    base=args.base,
                    head=args.head,
                    ref=args.ref,
                    title=args.title,
                )
            if args.json:
                sys.stdout.write(canonical_json(result) + "\n")
            else:
                sys.stdout.write(f"{args.task_id}: policy passed\n")
            return 0
        except CheckFailure as error:
            _write_policy_error(
                args.task_id,
                PolicyError(error.code, error.message, exit_code=error.exit_code),
                as_json=args.json,
            )
            return error.exit_code
        except PathGuardError as error:
            _write_policy_error(
                args.task_id,
                PolicyError(
                    error.code,
                    error.message,
                    paths=error.paths,
                    exit_code=error.exit_code,
                ),
                as_json=args.json,
            )
            return error.exit_code
        except PolicyError as error:
            _write_policy_error(args.task_id, error, as_json=args.json)
            return error.exit_code
        except HarnessError as error:
            _write_policy_error(
                args.task_id,
                PolicyError(error.code, error.message, exit_code=error.exit_code),
                as_json=args.json,
            )
            return error.exit_code
    if args.command == "verify":
        try:
            if os.environ.get("TASK_HARNESS_NESTED_VERIFY") == "1":
                task, claim = _active_task_for_verification(
                    args.task_id,
                    repo=args.queue.parent.parent,
                    queue_path=args.queue,
                )
                verify_paths(
                    args.task_id,
                    base=claim["base"],
                    repo=args.queue.parent.parent,
                    queue_path=args.queue,
                )
                result = {"task": args.task_id, "status": "passed", "checks": []}
            else:
                result = verify_task(
                    args.task_id,
                    repo=args.queue.parent.parent,
                    queue_path=args.queue,
                )
            if args.json:
                sys.stdout.write(canonical_json(result) + "\n")
            else:
                sys.stdout.write(f"{args.task_id}: verification passed\n")
            return 0
        except CheckFailure as error:
            _write_check_failure(args.task_id, error, as_json=args.json)
            return error.exit_code
        except PathGuardError as error:
            _write_path_guard_error(args.task_id, error, as_json=args.json)
            return error.exit_code
        except HarnessError as error:
            _write_error(error, as_json=args.json)
            return error.exit_code
    if args.command == "complete":
        try:
            result = complete_task(
                args.task_id,
                args.pr,
                args.commit,
                repo=args.queue.parent.parent,
                queue_path=args.queue,
            )
            if args.json:
                sys.stdout.write(canonical_json(result) + "\n")
            else:
                sys.stdout.write(f"{args.task_id}: completion recorded\n")
            return 0
        except CheckFailure as error:
            _write_check_failure(args.task_id, error, as_json=args.json)
            return error.exit_code
        except PathGuardError as error:
            _write_path_guard_error(args.task_id, error, as_json=args.json)
            return error.exit_code
        except HarnessError as error:
            _write_error(error, as_json=args.json)
            return error.exit_code
    if args.command == "context":
        try:
            _write_result(context_task(args.task_id, queue_path=args.queue), as_json=args.json)
            return 0
        except HarnessError as error:
            _write_error(error, as_json=args.json)
            return error.exit_code
    if args.command == "claim":
        try:
            result = claim_task(
                args.task_id,
                args.actor,
                args.remote,
                args.base,
                repo=args.queue.parent.parent,
                queue_path=args.queue,
            )
            _write_result(result, as_json=args.json)
            return 0
        except HarnessError as error:
            _write_error(error, as_json=args.json)
            return error.exit_code
    if args.command == "release":
        try:
            result = release_task(
                args.task_id,
                args.actor,
                args.remote,
                repo=args.queue.parent.parent,
                queue_path=args.queue,
            )
            _write_result(result, as_json=args.json)
            return 0
        except HarnessError as error:
            _write_error(error, as_json=args.json)
            return error.exit_code
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
