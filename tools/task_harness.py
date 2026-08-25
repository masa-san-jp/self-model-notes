"""Read-only validation and deterministic selection for execution/tasks.yaml."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
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
        raise HarnessError("INVALID_BASE", f"{field} must be a 40-character commit", CLAIM_INVALID)


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
        rf"(?ms)^  - id: {re.escape(task_id)}\n.*?(?=^  - id: |\Z)"
    )
    match = block_pattern.search(text)
    if match is None:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task block is missing")
    block = match.group(0)
    status_pattern = re.compile(r"^    status: ready$", re.MULTILINE)
    if len(status_pattern.findall(block)) != 1:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task status is not ready")
    if block.count("    claim: null") != 1:
        raise HarnessError(CLAIM_MUTATION_FAILED, "selected task claim is not null")
    updated = status_pattern.sub("    status: in-progress", block, count=1)
    claim_text = (
        "    claim:\n"
        f"      actor: {claim['actor']}\n"
        f"      base: {claim['base']}\n"
        f"      branch: {claim['branch']}\n"
        f"      remote: {claim['remote']}\n"
        f"      lock_ref: {claim['lock_ref']}\n"
        f"      claimed_at: '{claim['claimed_at']}'\n"
    )
    updated = updated.replace("    claim: null", claim_text, 1)
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
