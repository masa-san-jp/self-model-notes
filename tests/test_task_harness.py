from __future__ import annotations

import contextlib
import copy
import concurrent.futures
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import task_harness
from tools.task_harness import (
    CLAIM_LOCK_RACE,
    CLAIM_MUTATION_FAILED,
    CLAIM_PATH_CONFLICT,
    CLAIM_PRECONDITION,
    CLAIM_SAME_TASK_LOCK,
    CLAIM_SHARED_LOCK,
    CheckFailure,
    HarnessError,
    PATH_DISALLOWED,
    PolicyError,
    PathGuardError,
    claim_task,
    complete_task,
    context_task,
    release_task,
    verify_task,
    verify_paths,
    verify_pr,
)


ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "execution" / "tasks.yaml"


class TaskHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.queue = task_harness.load_queue(QUEUE_PATH)

    def queue_copy(self) -> dict:
        return copy.deepcopy(self.queue)

    def write_queue(self, queue: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "tasks.yaml"
        path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return path

    def test_current_queue_validates_and_selects_sm020(self):
        self.assertEqual([], task_harness.validate_queue(self.queue))
        selected = task_harness.selectable_tasks(self.queue)
        if self.queue_task("SM-020")["status"] == "ready":
            expected_id = "SM-020"
        elif self.queue_task("SM-021")["status"] == "ready":
            expected_id = "SM-021"
        else:
            if self.queue_task("SM-022")["status"] == "ready":
                expected_id = "SM-022"
            elif self.queue_task("SM-023")["status"] == "ready":
                expected_id = "SM-023"
            else:
                expected_id = "SM-024" if self.queue_task("SM-024")["status"] == "ready" else "SM-025"
        self.assertEqual([expected_id], [task["id"] for task in selected])
        self.assertEqual(
            {
                "id",
                "title",
                "issue",
                "allowed_paths",
                "acceptance",
                "checks",
                "stop_if",
            },
            set(task_harness.task_output(selected[0])),
        )

    def queue_task(self, task_id: str) -> dict:
        return next(task for task in self.queue["tasks"] if task["id"] == task_id)

    def test_selection_is_independent_of_yaml_task_order(self):
        reversed_queue = self.queue_copy()
        reversed_queue["tasks"].reverse()
        self.assertEqual([], task_harness.validate_queue(reversed_queue))
        if self.queue_task("SM-020")["status"] == "ready":
            expected_id = "SM-020"
        elif self.queue_task("SM-021")["status"] == "ready":
            expected_id = "SM-021"
        else:
            if self.queue_task("SM-022")["status"] == "ready":
                expected_id = "SM-022"
            elif self.queue_task("SM-023")["status"] == "ready":
                expected_id = "SM-023"
            else:
                expected_id = "SM-024" if self.queue_task("SM-024")["status"] == "ready" else "SM-025"
        self.assertEqual(
            [expected_id],
            [task["id"] for task in task_harness.selectable_tasks(reversed_queue)],
        )

    def test_validation_reports_duplicate_missing_dependency_and_cycle(self):
        malformed = self.queue_copy()
        malformed["tasks"][19]["id"] = "SM-001"
        malformed["tasks"][20]["depends_on"] = ["SM-021"]
        malformed["tasks"][21]["depends_on"] = ["SM-020"]
        malformed["tasks"][22]["depends_on"] = ["SM-999"]
        errors = task_harness.validate_queue(malformed)
        self.assertEqual(errors, sorted(errors))
        self.assertTrue(any("duplicate task ID" in error for error in errors))
        self.assertTrue(any("unknown dependency SM-999" in error for error in errors))
        self.assertTrue(any("cycle" in error for error in errors))

    def test_validation_rejects_bad_status_path_unknown_field_and_empty_done_evidence(self):
        malformed = self.queue_copy()
        malformed["tasks"][19]["status"] = "unknown"
        malformed["tasks"][19]["allowed_paths"] = ["/absolute"]
        malformed["tasks"][19]["unexpected"] = True
        malformed["tasks"][18]["evidence"] = []
        errors = task_harness.validate_queue(malformed)
        self.assertTrue(any("unsupported status" in error for error in errors))
        self.assertTrue(any("invalid relative pattern" in error for error in errors))
        self.assertTrue(any("unknown field" in error for error in errors))
        self.assertTrue(any("done task requires evidence" in error for error in errors))

    def test_next_json_is_stable_and_does_not_write_queue(self):
        before = QUEUE_PATH.read_bytes()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                0,
                task_harness.main(["next", "--queue", str(QUEUE_PATH), "--json"]),
            )
        first = stdout.getvalue()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                0,
                task_harness.main(["next", "--queue", str(QUEUE_PATH), "--json"]),
            )
        self.assertEqual(first, stdout.getvalue())
        self.assertEqual(before, QUEUE_PATH.read_bytes())
        result = json.loads(first)
        self.assertEqual(3, result["queue_version"])
        if self.queue_task("SM-020")["status"] == "ready":
            expected_id = "SM-020"
        elif self.queue_task("SM-021")["status"] == "ready":
            expected_id = "SM-021"
        else:
            if self.queue_task("SM-022")["status"] == "ready":
                expected_id = "SM-022"
            elif self.queue_task("SM-023")["status"] == "ready":
                expected_id = "SM-023"
            else:
                expected_id = "SM-024" if self.queue_task("SM-024")["status"] == "ready" else "SM-025"
        self.assertEqual(expected_id, result["task"]["id"])
        self.assertEqual(
            {
                "id",
                "title",
                "issue",
                "allowed_paths",
                "acceptance",
                "checks",
                "stop_if",
            },
            set(result["task"]),
        )

    def test_invalid_yaml_returns_two_without_traceback(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / "invalid.yaml"
        path.write_text("version: [", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = task_harness.main(
                ["validate", "--queue", str(path), "--json"]
            )
        self.assertEqual(2, result)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(["errors"], list(json.loads(stderr.getvalue())))

    def test_no_candidate_returns_three_and_null_task(self):
        queue = self.queue_copy()
        selected = task_harness.selectable_tasks(queue)
        self.assertTrue(selected)
        selected_id = selected[0]["id"]
        next(task for task in queue["tasks"] if task["id"] == selected_id)["status"] = "blocked"
        path = self.write_queue(queue)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = task_harness.main(["next", "--queue", str(path), "--json"])
        self.assertEqual(3, result)
        self.assertEqual(
            {"queue_version": 3, "reason": "no-selectable-task", "task": None},
            json.loads(stdout.getvalue()),
        )


class TemporaryRemote:
    def __init__(self, test_case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        test_case.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "agent"
        self.bare = self.root / "remote.git"
        self.repo.mkdir()
        self._git(self.root, "init", "--bare", str(self.bare))
        self._git(self.repo, "init", "-b", "main")
        self._git(self.repo, "config", "user.name", "Harness Test")
        self._git(self.repo, "config", "user.email", "harness@example.invalid")
        (self.repo / "execution").mkdir()
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "task_harness.py").write_text(
            "# harness fixture\n", encoding="utf-8"
        )
        queue_path = self.repo / "execution" / "tasks.yaml"
        queue_path.write_bytes(QUEUE_PATH.read_bytes())
        queue = task_harness.load_queue(queue_path)
        task = next(item for item in queue["tasks"] if item["id"] == "SM-021")
        task["status"] = "ready"
        task["claim"] = None
        task["evidence"] = []
        queue_path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self._git(self.repo, "add", "execution/tasks.yaml", "tools/task_harness.py")
        self._git(self.repo, "commit", "-m", "fixture base")
        self._git(self.repo, "remote", "add", "origin", str(self.bare))
        self._git(self.repo, "push", "-u", "origin", "main")

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            shell=False,
        )
        if result.returncode:
            raise AssertionError(f"git fixture setup failed: {args}")
        return result.stdout.strip()

    def clone(self, name: str) -> Path:
        target = self.root / name
        self._git(self.root, "clone", "-b", "main", str(self.bare), str(target))
        self._git(target, "config", "user.name", "Harness Test")
        self._git(target, "config", "user.email", "harness@example.invalid")
        return target

    def base(self, repo: Path | None = None) -> str:
        return self._git(repo or self.repo, "rev-parse", "HEAD")

    def lock_output(self, repo: Path | None = None) -> str:
        return self._git(
            repo or self.repo,
            "ls-remote",
            "--refs",
            "origin",
            "refs/heads/harness-lock/*",
        )


class TaskHarnessLifecycleTests(unittest.TestCase):
    def test_claim_creates_fixed_lowercase_lock_branch_and_context(self):
        fixture = TemporaryRemote(self)
        base = fixture.base()
        result = claim_task(
            "SM-021",
            "alice",
            "origin",
            base,
            repo=fixture.repo,
            queue_path=fixture.repo / "execution/tasks.yaml",
        )
        self.assertEqual("agent/sm-021-alice", result["claim"]["branch"])
        self.assertEqual("refs/heads/harness-lock/sm-021", result["claim"]["lock_ref"])
        self.assertEqual("agent/sm-021-alice", fixture._git(fixture.repo, "branch", "--show-current"))
        self.assertEqual(1, len(fixture.lock_output().splitlines()))
        first = context_task("SM-021", queue_path=fixture.repo / "execution/tasks.yaml")
        second = context_task("SM-021", queue_path=fixture.repo / "execution/tasks.yaml")
        self.assertEqual(first, second)
        self.assertEqual("in-progress", task_harness.load_queue(fixture.repo / "execution/tasks.yaml")["tasks"][-6]["status"])

    def test_concurrent_claims_have_exactly_one_winner(self):
        fixture = TemporaryRemote(self)
        first = fixture.clone("first")
        second = fixture.clone("second")
        base = fixture.base(first)

        def attempt(repo: Path, actor: str):
            try:
                claim_task(
                    "SM-021",
                    actor,
                    "origin",
                    base,
                    repo=repo,
                    queue_path=repo / "execution/tasks.yaml",
                )
                return "success"
            except HarnessError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda item: attempt(*item),
                    ((first, "alice"), (second, "bob")),
                )
            )
        self.assertEqual(1, outcomes.count("success"))
        self.assertEqual(1, len([outcome for outcome in outcomes if outcome != "success"]))
        self.assertIn(outcomes[0] if outcomes[0] != "success" else outcomes[1], {CLAIM_SAME_TASK_LOCK, CLAIM_LOCK_RACE})
        self.assertEqual(1, len(fixture.lock_output().splitlines()))

    def test_shared_and_overlapping_paths_have_distinct_stable_errors(self):
        queue = task_harness.load_queue(QUEUE_PATH)
        by_id = {task["id"]: task for task in queue["tasks"]}
        shared = task_harness._lock_conflict(queue, by_id["SM-021"], {"SM-022"})
        self.assertEqual((CLAIM_SHARED_LOCK, "task uses a policy shared-lock path"), shared)
        queue["policy"]["shared_locks"] = []
        by_id["SM-021"]["allowed_paths"] = ["docs/plan/**"]
        by_id["SM-022"]["allowed_paths"] = ["docs/plan/file.md"]
        overlap = task_harness._lock_conflict(queue, by_id["SM-021"], {"SM-022"})
        self.assertEqual((CLAIM_PATH_CONFLICT, "task allowed paths overlap a remote lock"), overlap)
        queue["policy"]["shared_locks"] = []
        by_id["SM-021"]["allowed_paths"] = ["alpha/**"]
        by_id["SM-022"]["allowed_paths"] = ["beta/**"]
        by_id["SM-021"]["status"] = "ready"
        by_id["SM-021"]["evidence"] = []
        by_id["SM-022"]["status"] = "ready"
        by_id["SM-022"]["evidence"] = []
        by_id["SM-022"]["depends_on"] = []
        self.assertIsNone(task_harness._lock_conflict(queue, by_id["SM-022"], {"SM-021"}))
        self.assertEqual(
            "SM-022",
            task_harness._current_task(queue, "SM-022", {"SM-021"})["id"],
        )

    def test_post_lock_mutation_failure_removes_only_new_lock_and_restores_bytes(self):
        fixture = TemporaryRemote(self)
        queue_path = fixture.repo / "execution/tasks.yaml"
        before = queue_path.read_bytes()
        base = fixture.base()

        def fail_mutation(path: Path, claim: dict[str, str]) -> None:
            raise HarnessError(CLAIM_MUTATION_FAILED, "forced test mutation failure")

        with self.assertRaises(HarnessError) as caught:
            claim_task(
                "SM-021",
                "alice",
                "origin",
                base,
                repo=fixture.repo,
                queue_path=queue_path,
                mutate=fail_mutation,
            )
        self.assertEqual(CLAIM_MUTATION_FAILED, caught.exception.code)
        self.assertEqual(before, queue_path.read_bytes())
        self.assertEqual("main", fixture._git(fixture.repo, "branch", "--show-current"))
        self.assertEqual("", fixture.lock_output())

    def test_claim_rejects_invalid_actor_dirty_and_stale_base_without_mutation(self):
        fixture = TemporaryRemote(self)
        queue_path = fixture.repo / "execution/tasks.yaml"
        base = fixture.base()
        with self.assertRaises(HarnessError) as caught:
            claim_task("SM-021", "Alice", "origin", base, repo=fixture.repo, queue_path=queue_path)
        self.assertEqual("INVALID_ACTOR", caught.exception.code)
        (fixture.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            claim_task("SM-021", "alice", "origin", base, repo=fixture.repo, queue_path=queue_path)
        self.assertEqual(CLAIM_PRECONDITION, caught.exception.code)
        (fixture.repo / "untracked.txt").unlink()
        with self.assertRaises(HarnessError) as caught:
            claim_task("SM-021", "alice", "origin", "0" * 40, repo=fixture.repo, queue_path=queue_path)
        self.assertEqual(CLAIM_PRECONDITION, caught.exception.code)
        self.assertEqual("", fixture.lock_output())
        self.assertEqual("main", fixture._git(fixture.repo, "branch", "--show-current"))

    def test_claim_rejects_detached_diverged_missing_remote_and_no_selection(self):
        detached = TemporaryRemote(self)
        base = detached.base()
        detached._git(detached.repo, "switch", "--detach", base)
        with self.assertRaises(HarnessError) as caught:
            claim_task(
                "SM-021",
                "alice",
                "origin",
                base,
                repo=detached.repo,
                queue_path=detached.repo / "execution/tasks.yaml",
            )
        self.assertEqual(CLAIM_PRECONDITION, caught.exception.code)

        diverged = TemporaryRemote(self)
        diverged._git(diverged.repo, "switch", "main")
        (diverged.repo / "local.txt").write_text("ahead\n", encoding="utf-8")
        diverged._git(diverged.repo, "add", "local.txt")
        diverged._git(diverged.repo, "commit", "-m", "local divergence")
        with self.assertRaises(HarnessError) as caught:
            claim_task(
                "SM-021",
                "alice",
                "origin",
                diverged.base(),
                repo=diverged.repo,
                queue_path=diverged.repo / "execution/tasks.yaml",
            )
        self.assertEqual(CLAIM_PRECONDITION, caught.exception.code)

        missing_remote = TemporaryRemote(self)
        with self.assertRaises(HarnessError) as caught:
            claim_task(
                "SM-021",
                "alice",
                "missing",
                missing_remote.base(),
                repo=missing_remote.repo,
                queue_path=missing_remote.repo / "execution/tasks.yaml",
            )
        self.assertEqual(CLAIM_PRECONDITION, caught.exception.code)

        no_selection = TemporaryRemote(self)
        queue_path = no_selection.repo / "execution/tasks.yaml"
        queue = task_harness.load_queue(queue_path)
        next(task for task in queue["tasks"] if task["id"] == "SM-021")["status"] = "blocked"
        queue_path.write_text(yaml.safe_dump(queue, sort_keys=False), encoding="utf-8")
        no_selection._git(no_selection.repo, "add", "execution/tasks.yaml")
        no_selection._git(no_selection.repo, "commit", "-m", "block task")
        no_selection._git(no_selection.repo, "push", "origin", "main")
        with self.assertRaises(HarnessError) as caught:
            claim_task(
                "SM-021",
                "alice",
                "origin",
                no_selection.base(),
                repo=no_selection.repo,
                queue_path=queue_path,
            )
        self.assertEqual("TASK_NOT_SELECTABLE", caught.exception.code)

    def _complete_remote_task(self, fixture: TemporaryRemote) -> str:
        maint = fixture.clone("maint")
        implementation = maint / "implementation.txt"
        implementation.write_text("implemented\n", encoding="utf-8")
        fixture._git(maint, "add", "implementation.txt")
        fixture._git(maint, "commit", "-m", "implementation")
        implementation_commit = fixture._git(maint, "rev-parse", "HEAD")
        queue_path = maint / "execution/tasks.yaml"
        text = queue_path.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^(?P<indent> *)- id: SM-021\n.*?(?=^(?P=indent)- id: |\Z)",
            text,
        )
        self.assertIsNotNone(match)
        start, end = match.span()
        indent = match.group("indent")
        block = match.group(0)
        block = block.replace(f"{indent}  status: ready", f"{indent}  status: done", 1)
        block = block.replace(
            f"{indent}  evidence: []",
            f"{indent}  evidence:\n{indent}    - commit: {implementation_commit}\n{indent}      checks:\n{indent}        - simulated implementation\n",
            1,
        )
        queue_path.write_text(text[:start] + block + text[end:], encoding="utf-8")
        fixture._git(maint, "add", "execution/tasks.yaml")
        fixture._git(maint, "commit", "-m", "completion evidence")
        fixture._git(maint, "push", "origin", "main")
        return implementation_commit

    def test_release_requires_matching_actor_and_merged_evidence_then_deletes_only_lock(self):
        fixture = TemporaryRemote(self)
        queue_path = fixture.repo / "execution/tasks.yaml"
        base = fixture.base()
        claim_task("SM-021", "alice", "origin", base, repo=fixture.repo, queue_path=queue_path)
        with self.assertRaises(HarnessError) as caught:
            release_task("SM-021", "bob", "origin", repo=fixture.repo, queue_path=queue_path)
        self.assertEqual("RELEASE_ACTOR", caught.exception.code)
        with self.assertRaises(HarnessError) as caught:
            release_task("SM-021", "alice", "origin", repo=fixture.repo, queue_path=queue_path)
        self.assertEqual("RELEASE_PRECONDITION", caught.exception.code)
        self._complete_remote_task(fixture)
        result = release_task("SM-021", "alice", "origin", repo=fixture.repo, queue_path=queue_path)
        self.assertTrue(result["released"])
        self.assertEqual("", fixture.lock_output())
        self.assertTrue(
            fixture._git(fixture.repo, "show-ref", "--verify", "--quiet", "refs/heads/agent/sm-021-alice")
            == ""
        )

    def test_release_with_missing_evidence_keeps_lock(self):
        fixture = TemporaryRemote(self)
        queue_path = fixture.repo / "execution/tasks.yaml"
        base = fixture.base()
        claim_task("SM-021", "alice", "origin", base, repo=fixture.repo, queue_path=queue_path)
        maint = fixture.clone("maint-missing")
        remote_queue = maint / "execution/tasks.yaml"
        text = remote_queue.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^(?P<indent> *)- id: SM-021\n.*?(?=^(?P=indent)- id: |\Z)",
            text,
        )
        self.assertIsNotNone(match)
        start, end = match.span()
        indent = match.group("indent")
        block = match.group(0).replace(
            f"{indent}  status: ready", f"{indent}  status: done", 1
        )
        remote_queue.write_text(text[:start] + block + text[end:], encoding="utf-8")
        fixture._git(maint, "add", "execution/tasks.yaml")
        fixture._git(maint, "commit", "-m", "incomplete completion")
        fixture._git(maint, "push", "origin", "main")
        with self.assertRaises(HarnessError) as caught:
            release_task("SM-021", "alice", "origin", repo=fixture.repo, queue_path=queue_path)
        self.assertEqual("RELEASE_PRECONDITION", caught.exception.code)
        self.assertEqual(1, len(fixture.lock_output().splitlines()))


class PathGuardTests(unittest.TestCase):
    def test_allowed_committed_path_and_repeated_json_are_stable(self):
        fixture = TemporaryRemote(self)
        base = fixture.base()
        tool = fixture.repo / "tools" / "task_harness.py"
        tool.write_text(tool.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        fixture._git(fixture.repo, "add", "tools/task_harness.py")
        fixture._git(fixture.repo, "commit", "-m", "allowed harness change")
        head = fixture.base()
        first = verify_paths(
            "SM-022",
            base=base,
            head=head,
            committed_only=True,
            repo=fixture.repo,
            queue_path=fixture.repo / "execution/tasks.yaml",
        )
        second = verify_paths(
            "SM-022",
            base=base,
            head=head,
            committed_only=True,
            repo=fixture.repo,
            queue_path=fixture.repo / "execution/tasks.yaml",
        )
        self.assertEqual(first, second)
        self.assertEqual({"task", "base", "head", "paths"}, set(first))
        self.assertEqual("SM-022", first["task"])
        self.assertEqual(["tools/task_harness.py"], first["paths"])

    def test_disallowed_path_is_exit_four_and_json_is_safe(self):
        fixture = TemporaryRemote(self)
        base = fixture.base()
        readme = fixture.repo / "README.md"
        readme.write_text("synthetic disallowed path\n", encoding="utf-8")
        fixture._git(fixture.repo, "add", "README.md")
        fixture._git(fixture.repo, "commit", "-m", "disallowed path")
        head = fixture.base()
        with self.assertRaises(PathGuardError) as caught:
            verify_paths(
                "SM-022",
                base=base,
                head=head,
                committed_only=True,
                repo=fixture.repo,
                queue_path=fixture.repo / "execution/tasks.yaml",
            )
        self.assertEqual(PATH_DISALLOWED, caught.exception.exit_code)
        self.assertEqual(["README.md"], caught.exception.paths)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = task_harness.main(
                [
                    "verify-paths",
                    "SM-022",
                    "--base",
                    base,
                    "--head",
                    head,
                    "--committed-only",
                    "--queue",
                    str(fixture.repo / "execution/tasks.yaml"),
                    "--json",
                ]
            )
        self.assertEqual(PATH_DISALLOWED, result)
        self.assertEqual("", stdout.getvalue())
        failure = json.loads(stderr.getvalue())
        self.assertEqual({"task", "rule", "paths"}, set(failure))
        self.assertEqual("SM-022", failure["task"])
        self.assertEqual("PATH_DISALLOWED", failure["rule"])
        self.assertEqual(["README.md"], failure["paths"])

    def test_pattern_matching_is_root_anchored_and_table_driven(self):
        cases = [
            ("tools/task_harness.py", "tools/task_harness.py", True),
            ("tools/task_harness.py", "tools/other.py", False),
            ("docs/*.md", "docs/plan.md", True),
            ("docs/*.md", "docs/sub/plan.md", False),
            ("tests/test_?.py", "tests/test_a.py", True),
            ("tests/test_?.py", "tests/test_ab.py", False),
            ("tests/**", "tests/test.py", True),
            ("tests/**", "tests/deep/test.py", True),
            ("tests/**", "tests", False),
            ("**/*.py", "tools/task_harness.py", True),
            ("**/*.py", "README.md", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(expected, task_harness._path_allowed(path, [pattern]))

    def test_complete_worktree_states_and_rename_sources_are_visible(self):
        fixture = TemporaryRemote(self)
        (fixture.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        (fixture.repo / "rename-source.txt").write_text("rename\n", encoding="utf-8")
        fixture._git(fixture.repo, "add", "tracked.txt", "rename-source.txt")
        fixture._git(fixture.repo, "commit", "-m", "state fixture")
        base = fixture.base()
        (fixture.repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        fixture._git(fixture.repo, "add", "staged.txt")
        (fixture.repo / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
        (fixture.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        (fixture.repo / "tracked.txt").unlink()
        fixture._git(fixture.repo, "mv", "rename-source.txt", "rename-target.txt")
        shutil.copyfile(
            fixture.repo / "tools" / "task_harness.py",
            fixture.repo / "copy.txt",
        )
        fixture._git(fixture.repo, "add", "copy.txt")
        paths = task_harness._changed_git_paths(
            fixture.repo,
            base,
            base,
            committed_only=False,
        )
        for path in (
            "staged.txt",
            "unstaged.txt",
            "untracked.txt",
            "tracked.txt",
            "rename-source.txt",
            "rename-target.txt",
            "copy.txt",
        ):
            self.assertIn(path, paths)
        self.assertIn("conflicted.txt", task_harness._parse_status_paths("UU conflicted.txt\0"))
        self.assertEqual(
            ["new.txt", "old.txt"],
            sorted(task_harness._parse_diff_paths("R100\0new.txt\0old.txt\0")),
        )

    def test_base_must_be_a_commit_ancestor(self):
        fixture = TemporaryRemote(self)
        head = fixture.base()
        tree = fixture._git(fixture.repo, "rev-parse", "HEAD^{tree}")
        unrelated = fixture._git(
            fixture.repo,
            "commit-tree",
            tree,
            "-m",
            "unrelated",
        )
        with self.assertRaises(PathGuardError) as caught:
            verify_paths(
                "SM-022",
                base=unrelated,
                head=head,
                committed_only=True,
                repo=fixture.repo,
                queue_path=fixture.repo / "execution/tasks.yaml",
            )
        self.assertEqual("BASE_NOT_ANCESTOR", caught.exception.code)


class CompletionRemote:
    def __init__(self, test_case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        test_case.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "agent"
        self.bare = self.root / "remote.git"
        self.repo.mkdir()
        TemporaryRemote._git(self.root, "init", "--bare", str(self.bare))
        TemporaryRemote._git(self.repo, "init", "-b", "main")
        TemporaryRemote._git(self.repo, "config", "user.name", "Harness Test")
        TemporaryRemote._git(self.repo, "config", "user.email", "harness@example.invalid")
        (self.repo / "execution").mkdir()
        (self.repo / "tools").mkdir()
        queue_path = self.repo / "execution/tasks.yaml"
        queue_path.write_bytes(QUEUE_PATH.read_bytes())
        (self.repo / "tools/task_harness.py").write_text(
            "# completion fixture\n", encoding="utf-8"
        )
        TemporaryRemote._git(self.repo, "add", ".")
        TemporaryRemote._git(self.repo, "commit", "-m", "completion base")
        TemporaryRemote._git(self.repo, "remote", "add", "origin", str(self.bare))
        TemporaryRemote._git(self.repo, "push", "-u", "origin", "main")
        self.base = TemporaryRemote._git(self.repo, "rev-parse", "HEAD")
        TemporaryRemote._git(self.repo, "switch", "-c", "agent/sm-023-alice")
        queue = task_harness.load_queue(queue_path)
        task = next(item for item in queue["tasks"] if item["id"] == "SM-023")
        task["status"] = "in-progress"
        task["evidence"] = []
        task["claim"] = {
            "actor": "alice",
            "base": self.base,
            "branch": "agent/sm-023-alice",
            "remote": "origin",
            "lock_ref": "refs/heads/harness-lock/sm-023",
            "claimed_at": "test-claim",
        }
        task["checks"] = ["python3 -c pass", "python3 -c pass"]
        queue_path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        TemporaryRemote._git(self.repo, "add", "execution/tasks.yaml")
        TemporaryRemote._git(self.repo, "commit", "-m", "claim SM-023")
        self.head = TemporaryRemote._git(self.repo, "rev-parse", "HEAD")
        self.queue_path = queue_path

    def git(self, *args: str) -> str:
        return TemporaryRemote._git(self.repo, *args)


class CheckAndCompletionTests(unittest.TestCase):
    def test_safe_check_parser_rejects_shell_and_assignment_syntax(self):
        self.assertEqual(["python3", "-c", "pass"], task_harness._safe_check_argv("python3 -c pass"))
        for command in (
            "python3 -c pass && echo unsafe",
            "python3 -c pass > output",
            "FLAG=value python3 -c pass",
            "python3 -c $(unsafe)",
        ):
            with self.subTest(command=command):
                with self.assertRaises(CheckFailure) as caught:
                    task_harness._run_declared_checks("SM-023", [command], repo=Path("."))
                self.assertEqual("UNSAFE_CHECK", caught.exception.code)

    def test_checks_run_in_yaml_order_and_stop_on_first_failure(self):
        completed = [
            subprocess.CompletedProcess(["first"], 0),
            subprocess.CompletedProcess(["second"], 3),
        ]
        with mock.patch("tools.task_harness.subprocess.run", side_effect=completed) as runner:
            with self.assertRaises(CheckFailure) as caught:
                task_harness._run_declared_checks(
                    "SM-023",
                    ["python3 -c first", "python3 -c second", "python3 -c never"],
                    repo=Path("."),
                )
        self.assertEqual("CHECK_FAILED", caught.exception.code)
        self.assertEqual(2, runner.call_count)
        self.assertEqual(["python3", "-c", "first"], runner.call_args_list[0].args[0])
        self.assertFalse(runner.call_args_list[0].kwargs["shell"])
        self.assertEqual(["python3", "-c", "second"], runner.call_args_list[1].args[0])

    def test_missing_executable_and_timeout_are_stable_failures(self):
        with self.assertRaises(CheckFailure) as missing:
            task_harness._run_declared_checks(
                "SM-023",
                ["definitely-not-an-installed-executable"],
                repo=Path("."),
            )
        self.assertEqual("MISSING_EXECUTABLE", missing.exception.code)
        with mock.patch(
            "tools.task_harness.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["python3"], 60),
        ):
            with self.assertRaises(CheckFailure) as timeout:
                task_harness._run_declared_checks(
                    "SM-023",
                    ["python3 -c pass"],
                    repo=Path("."),
                )
        self.assertEqual("CHECK_TIMEOUT", timeout.exception.code)

    def test_verify_runs_path_guard_and_declared_checks(self):
        fixture = CompletionRemote(self)
        result = verify_task(
            "SM-023",
            repo=fixture.repo,
            queue_path=fixture.queue_path,
        )
        self.assertEqual("passed", result["status"])
        self.assertEqual(2, len(result["checks"]))
        self.assertTrue(all(item["status"] == "passed" for item in result["checks"]))
        self.assertNotIn("stdout", json.dumps(result))
        self.assertNotIn("stderr", json.dumps(result))

    def test_failed_verify_leaves_queue_byte_identical(self):
        fixture = CompletionRemote(self)
        queue = task_harness.load_queue(fixture.queue_path)
        task = next(item for item in queue["tasks"] if item["id"] == "SM-023")
        task["checks"] = ["python3 -c \"raise SystemExit(3)\"", "python3 -c pass"]
        fixture.queue_path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        fixture.git("add", "execution/tasks.yaml")
        fixture.git("commit", "-m", "failing check")
        before = fixture.queue_path.read_bytes()
        with self.assertRaises(CheckFailure) as caught:
            verify_task("SM-023", repo=fixture.repo, queue_path=fixture.queue_path)
        self.assertEqual("CHECK_FAILED", caught.exception.code)
        self.assertEqual(before, fixture.queue_path.read_bytes())


class PolicyCandidate:
    def __init__(self, test_case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        test_case.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "candidate"
        shutil.copytree(
            ROOT,
            self.repo,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".DS_Store"),
        )
        queue_path = self.repo / "execution/tasks.yaml"
        queue = task_harness.load_queue(queue_path)
        task = next(item for item in queue["tasks"] if item["id"] == "SM-024")
        task["status"] = "ready"
        task["claim"] = None
        task["evidence"] = []
        queue_path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self.trusted_queue = self.root / "trusted-tasks.yaml"
        self.trusted_queue.write_bytes(queue_path.read_bytes())
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "policy base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "agent/sm-024-alice")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            shell=False,
        )
        if result.returncode:
            raise AssertionError(f"policy fixture git failed: {args}: {result.stderr}")
        return result.stdout.strip()

    def write_queue(self, queue: dict) -> None:
        (self.repo / "execution/tasks.yaml").write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def queue(self) -> dict:
        return task_harness.load_queue(self.repo / "execution/tasks.yaml")

    def set_task(self, task_id: str, **changes: object) -> None:
        queue = self.queue()
        task = next(item for item in queue["tasks"] if item["id"] == task_id)
        task.update(changes)
        self.write_queue(queue)

    def claim(self) -> str:
        self.set_task(
            "SM-024",
            status="in-progress",
            claim={
                "actor": "alice",
                "base": self.base,
                "branch": "agent/sm-024-alice",
                "remote": "origin",
                "lock_ref": "refs/heads/harness-lock/sm-024",
                "claimed_at": "test-claim",
            },
            evidence=[],
        )
        self.git("add", "execution/tasks.yaml")
        self.git("commit", "-m", "claim SM-024")
        return self.git("rev-parse", "HEAD")

    def complete(self, *, intermediate: bool = True, extra_path: str | None = None) -> str:
        implementation_commit = self.git("rev-parse", "HEAD")
        if not intermediate:
            self.set_task(
                "SM-024",
                status="done",
                claim=None,
                evidence=[
                    {"pr": 999, "commit": self.base, "checks": ["simulated"]}
                ],
            )
        else:
            self.set_task(
                "SM-024",
                status="done",
                claim=None,
                evidence=[
                    {"pr": 999, "commit": implementation_commit, "checks": ["simulated"]}
                ],
            )
        if extra_path is not None:
            path = self.repo / extra_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("candidate content\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "complete SM-024")
        return self.git("rev-parse", "HEAD")

    def policy(self, head: str, **overrides: str) -> dict:
        values = {
            "repo": self.repo,
            "task_id": "SM-024",
            "base": self.base,
            "head": head,
            "ref": "agent/sm-024-alice",
            "title": "[SM-024] Enforce one-task PR policy",
        }
        values.update(overrides)
        with mock.patch.object(task_harness, "DEFAULT_QUEUE", self.trusted_queue):
            return verify_pr(**values)


class PolicyVerifierTests(unittest.TestCase):
    def test_valid_lifecycle_uses_base_contract_and_runs_checks(self):
        fixture = PolicyCandidate(self)
        fixture.claim()
        head = fixture.complete()
        with mock.patch.object(
            task_harness,
            "_run_declared_checks",
            return_value=[{"command": "simulated", "exit_code": 0, "status": "passed"}],
        ) as checks:
            result = fixture.policy(head)
        self.assertEqual("passed", result["status"])
        self.assertEqual("SM-024", result["task"])
        self.assertEqual(["execution/tasks.yaml"], result["paths"])
        checks.assert_called_once()

    def test_branch_and_title_identity_are_required(self):
        fixture = PolicyCandidate(self)
        head = fixture.claim()
        for overrides, code in (
            ({"ref": "agent/sm-025-alice"}, "TASK_BRANCH_MISMATCH"),
            ({"ref": "agent/sm-024-ALICE"}, "BRANCH_INVALID"),
            ({"title": "SM-024 Enforce one-task PR policy"}, "TITLE_MISMATCH"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(PolicyError) as caught:
                    fixture.policy(head, **overrides)
                self.assertEqual(code, caught.exception.code)

    def test_direct_done_transition_and_rollback_are_rejected(self):
        direct = PolicyCandidate(self)
        direct_head = direct.complete(intermediate=False)
        with self.assertRaises(PolicyError) as caught:
            direct.policy(direct_head)
        self.assertEqual("TRANSITION_INVALID", caught.exception.code)

        rollback = PolicyCandidate(self)
        rollback.claim()
        rollback.set_task("SM-024", status="ready", claim=None, evidence=[])
        rollback.git("add", "execution/tasks.yaml")
        rollback.git("commit", "-m", "rollback claim")
        rollback_head = rollback.complete()
        with self.assertRaises(PolicyError) as caught:
            rollback.policy(rollback_head)
        self.assertEqual("TRANSITION_INVALID", caught.exception.code)

    def test_contract_metadata_and_evidence_changes_are_rejected(self):
        contract = PolicyCandidate(self)
        contract.claim()
        contract.set_task("SM-025", title="weakened contract")
        contract_head = contract.complete()
        with self.assertRaises(PolicyError) as caught:
            contract.policy(contract_head)
        self.assertEqual("TASK_CONTRACT_CHANGED", caught.exception.code)

        metadata = PolicyCandidate(self)
        metadata.claim()
        queue = metadata.queue()
        queue["policy"]["shared_locks"] = []
        metadata.write_queue(queue)
        metadata.git("add", "execution/tasks.yaml")
        metadata.git("commit", "-m", "weaken queue metadata")
        metadata_head = metadata.complete()
        with self.assertRaises(PolicyError) as caught:
            metadata.policy(metadata_head)
        self.assertEqual("QUEUE_METADATA_CHANGED", caught.exception.code)

        evidence = PolicyCandidate(self)
        evidence.claim()
        evidence.set_task("SM-024", status="done", claim=None, evidence=[])
        evidence.git("add", "execution/tasks.yaml")
        evidence.git("commit", "-m", "delete evidence")
        with self.assertRaises(PolicyError) as caught:
            evidence.policy(evidence.git("rev-parse", "HEAD"))
        self.assertEqual("QUEUE_INVALID", caught.exception.code)

    def test_trusted_policy_cannot_be_bypassed_by_candidate_verifier_or_path(self):
        fixture = PolicyCandidate(self)
        fixture.claim()
        (fixture.repo / "tools/task_harness.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        head = fixture.complete(extra_path="README.md")
        with mock.patch.object(task_harness, "_run_declared_checks") as checks:
            with self.assertRaises(PolicyError) as caught:
                fixture.policy(head)
        self.assertEqual("PATH_DISALLOWED", caught.exception.code)
        self.assertEqual(PATH_DISALLOWED, caught.exception.exit_code)
        self.assertEqual(["README.md"], caught.exception.paths)
        checks.assert_not_called()

    def test_declared_policy_check_failure_is_returned_with_check_exit_code(self):
        fixture = PolicyCandidate(self)
        fixture.claim()
        head = fixture.complete()
        failed = CheckFailure(
            "CHECK_FAILED",
            "declared check failed",
            [{"command": "simulated", "exit_code": 1, "status": "failed"}],
        )
        with mock.patch.object(task_harness, "_run_declared_checks", side_effect=failed):
            with self.assertRaises(CheckFailure) as caught:
                fixture.policy(head)
        self.assertEqual("CHECK_FAILED", caught.exception.code)
        self.assertEqual(5, caught.exception.exit_code)

    def test_policy_json_reports_only_safe_rule_and_paths(self):
        fixture = PolicyCandidate(self)
        fixture.claim()
        head = fixture.complete(extra_path="README.md")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.object(
            task_harness, "DEFAULT_QUEUE", fixture.trusted_queue
        ):
            result = task_harness.main(
                [
                    "verify-pr",
                    "--repo",
                    str(fixture.repo),
                    "--task",
                    "SM-024",
                    "--base",
                    fixture.base,
                    "--head",
                    head,
                    "--ref",
                    "agent/sm-024-alice",
                    "--title",
                    "[SM-024] Enforce one-task PR policy",
                    "--json",
                ]
            )
        self.assertEqual(PATH_DISALLOWED, result)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            {"task", "rule", "paths"}, set(json.loads(stderr.getvalue()))
        )

    def test_complete_is_atomic_and_records_one_evidence_then_rejects_duplicate(self):
        fixture = CompletionRemote(self)
        result = complete_task(
            "SM-023",
            123,
            fixture.head,
            repo=fixture.repo,
            queue_path=fixture.queue_path,
        )
        self.assertEqual("done", result["status"])
        queue = task_harness.load_queue(fixture.queue_path)
        task = next(item for item in queue["tasks"] if item["id"] == "SM-023")
        self.assertEqual("done", task["status"])
        self.assertIsNone(task["claim"])
        self.assertEqual(1, len(task["evidence"]))
        self.assertEqual(123, task["evidence"][0]["pr"])
        self.assertNotIn("stdout", json.dumps(task["evidence"]))
        self.assertNotIn("stderr", json.dumps(task["evidence"]))
        expected_next = "SM-024" if next(
            item for item in task_harness.load_queue(QUEUE_PATH)["tasks"] if item["id"] == "SM-024"
        )["status"] == "ready" else "SM-025"
        self.assertEqual(expected_next, task_harness.selectable_tasks(queue)[0]["id"])
        with self.assertRaises(HarnessError) as caught:
            complete_task(
                "SM-023",
                123,
                fixture.head,
                repo=fixture.repo,
                queue_path=fixture.queue_path,
            )
        self.assertEqual("CLAIM_REQUIRED", caught.exception.code)

    def test_dirty_completion_and_wrong_commit_are_rejected_without_queue_change(self):
        fixture = CompletionRemote(self)
        before = fixture.queue_path.read_bytes()
        with self.assertRaises(HarnessError) as wrong:
            complete_task(
                "SM-023",
                123,
                "0" * 40,
                repo=fixture.repo,
                queue_path=fixture.queue_path,
            )
        self.assertEqual("COMMIT_MISMATCH", wrong.exception.code)
        (fixture.repo / "tools/task_harness.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(HarnessError) as dirty:
            complete_task(
                "SM-023",
                123,
                fixture.head,
                repo=fixture.repo,
                queue_path=fixture.queue_path,
            )
        self.assertEqual("WORKTREE_DIRTY", dirty.exception.code)
        self.assertEqual(before, fixture.queue_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
