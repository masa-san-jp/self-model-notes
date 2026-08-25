from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import task_harness
from tools.task_harness import (
    CLAIM_SAME_TASK_LOCK,
    CheckFailure,
    PATH_DISALLOWED,
    PathGuardError,
    claim_task,
    complete_task,
    context_task,
    release_task,
    selectable_tasks,
    verify_task,
    verify_paths,
)


ROOT = Path(__file__).resolve().parents[1]
ROOT_QUEUE = ROOT / "execution" / "tasks.yaml"


class HarnessRemote:
    def __init__(self, test_case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        test_case.addCleanup(shutil.rmtree, self.root, True)
        self.bare = self.root / "origin.git"
        self.agent = self.root / "agent"
        self._git(self.root, "init", "--bare", str(self.bare))
        self.agent.mkdir()
        self._git(self.agent, "init", "-b", "main")
        self._git(self.agent, "config", "user.name", "Harness E2E")
        self._git(self.agent, "config", "user.email", "harness-e2e@example.invalid")
        (self.agent / "execution").mkdir()
        self.write_queue(self._queue())
        (self.agent / "README.md").write_text("anonymous harness fixture\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "fixture base")
        self.git("remote", "add", "origin", str(self.bare))
        self.git("push", "-u", "origin", "main")

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
            raise AssertionError(f"fixture git failed: {args}: {result.stderr}")
        return result.stdout.strip()

    def git(self, *args: str) -> str:
        return self._git(self.agent, *args)

    def clone(self, name: str) -> Path:
        target = self.root / name
        self._git(self.root, "clone", "-b", "main", str(self.bare), str(target))
        self._git(target, "config", "user.name", "Harness E2E")
        self._git(target, "config", "user.email", "harness-e2e@example.invalid")
        return target

    def write_queue(self, queue: dict) -> None:
        path = self.agent / "execution/tasks.yaml"
        path.write_text(
            yaml.safe_dump(queue, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @staticmethod
    def _queue() -> dict:
        return {
            "version": 3,
            "updated_at": "2026-08-25",
            "policy": {
                "selection": "lowest-ready-id",
                "unit": "one-task-one-agent-one-pr",
                "shared_locks": [],
                "status_values": ["blocked", "ready", "in-progress", "review", "done"],
            },
            "tasks": [
                {
                    "id": "SM-901",
                    "title": "Anonymous E2E implementation",
                    "phase": 10,
                    "status": "ready",
                    "depends_on": [],
                    "issue": "https://github.com/masa-san-jp/self-model-notes/issues/901",
                    "allowed_paths": ["execution/tasks.yaml", "implementation.txt"],
                    "claim": None,
                    "acceptance": ["implementation exists"],
                    "checks": ["python3 -c \"raise SystemExit(7)\""],
                    "stop_if": ["fixture requires a service"],
                    "evidence": [],
                },
                {
                    "id": "SM-902",
                    "title": "Anonymous E2E dependent task",
                    "phase": 10,
                    "status": "ready",
                    "depends_on": ["SM-901"],
                    "issue": "https://github.com/masa-san-jp/self-model-notes/issues/902",
                    "allowed_paths": ["execution/tasks.yaml", "next.txt"],
                    "claim": None,
                    "acceptance": ["next task is selectable"],
                    "checks": ["python3 -c pass"],
                    "stop_if": ["fixture requires a service"],
                    "evidence": [],
                },
            ],
        }

    def queue(self) -> dict:
        with (self.agent / "execution/tasks.yaml").open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def lock_refs(self) -> str:
        return self.git("ls-remote", "--refs", "origin", "refs/heads/harness-lock/*")


class HarnessEndToEndTests(unittest.TestCase):
    def test_full_claim_to_release_lifecycle_uses_only_temporary_git_state(self):
        outside_queue = ROOT_QUEUE.read_bytes()
        outside_refs = subprocess.run(
            ["git", "show-ref"], cwd=ROOT, text=True, capture_output=True, shell=False
        ).stdout
        fixture = HarnessRemote(self)
        queue_path = fixture.agent / "execution/tasks.yaml"
        base = fixture.git("rev-parse", "HEAD")
        self.assertEqual([], task_harness.validate_queue(fixture.queue()))
        self.assertEqual(["SM-901"], [task["id"] for task in selectable_tasks(fixture.queue())])

        claimed = claim_task(
            "SM-901",
            "alice",
            "origin",
            base,
            repo=fixture.agent,
            queue_path=queue_path,
        )
        self.assertEqual("agent/sm-901-alice", claimed["claim"]["branch"])
        self.assertEqual("refs/heads/harness-lock/sm-901", claimed["claim"]["lock_ref"])

        competing = fixture.clone("competing")
        competing_queue = competing / "execution/tasks.yaml"
        competing_queue_before = competing_queue.read_bytes()
        with self.assertRaises(task_harness.HarnessError) as caught:
            claim_task(
                "SM-901",
                "bob",
                "origin",
                base,
                repo=competing,
                queue_path=competing_queue,
            )
        self.assertEqual(CLAIM_SAME_TASK_LOCK, caught.exception.code)
        self.assertEqual("main", self._git(competing, "branch", "--show-current"))
        self.assertEqual(competing_queue_before, competing_queue.read_bytes())

        first_context = context_task("SM-901", queue_path=queue_path)
        second_context = context_task("SM-901", queue_path=queue_path)
        self.assertEqual(first_context, second_context)
        context_json = json.dumps(first_context, ensure_ascii=False)
        self.assertNotIn(str(fixture.root), context_json)
        self.assertNotIn("raw_voice", context_json)
        self.assertNotIn("credentials", context_json)

        (fixture.agent / "implementation.txt").write_text("anonymous implementation\n", encoding="utf-8")
        (fixture.agent / "disallowed.txt").write_text("test-local only\n", encoding="utf-8")
        with self.assertRaises(PathGuardError) as path_error:
            verify_paths("SM-901", repo=fixture.agent, queue_path=queue_path)
        self.assertEqual(PATH_DISALLOWED, path_error.exception.exit_code)
        self.assertEqual(["disallowed.txt"], path_error.exception.paths)
        (fixture.agent / "disallowed.txt").unlink()

        fixture.git("add", "execution/tasks.yaml", "implementation.txt")
        fixture.git("commit", "-m", "implementation with claim")
        implementation_commit = fixture.git("rev-parse", "HEAD")
        before_failed_completion = queue_path.read_bytes()
        with self.assertRaises(CheckFailure) as failed_check:
            complete_task(
                "SM-901",
                901,
                implementation_commit,
                repo=fixture.agent,
                queue_path=queue_path,
            )
        self.assertEqual("CHECK_FAILED", failed_check.exception.code)
        self.assertEqual(before_failed_completion, queue_path.read_bytes())
        self.assertEqual(1, len(fixture.lock_refs().splitlines()))

        queue = fixture.queue()
        task = next(item for item in queue["tasks"] if item["id"] == "SM-901")
        task["checks"] = ["python3 -c pass"]
        fixture.write_queue(queue)
        fixture.git("add", "execution/tasks.yaml")
        fixture.git("commit", "-m", "make declared check pass")
        passing_commit = fixture.git("rev-parse", "HEAD")
        verified = verify_task("SM-901", repo=fixture.agent, queue_path=queue_path)
        self.assertEqual("passed", verified["status"])

        release_repo = fixture.root / "release-operator"
        self._git(fixture.root, "clone", "--local", str(fixture.agent), str(release_repo))
        self._git(release_repo, "remote", "set-url", "origin", str(fixture.bare))
        self._git(release_repo, "config", "user.name", "Harness E2E")
        self._git(release_repo, "config", "user.email", "harness-e2e@example.invalid")
        self.assertEqual("agent/sm-901-alice", self._git(release_repo, "branch", "--show-current"))

        completed = complete_task(
            "SM-901",
            901,
            passing_commit,
            repo=fixture.agent,
            queue_path=queue_path,
        )
        self.assertEqual("done", completed["status"])
        fixture.git("add", "execution/tasks.yaml")
        fixture.git("commit", "-m", "record completion evidence")
        evidence_commit = fixture.git("rev-parse", "HEAD")
        self.assertNotEqual(passing_commit, evidence_commit)
        fixture.git("switch", "main")
        fixture.git("merge", "--ff-only", "agent/sm-901-alice")
        fixture.git("push", "origin", "main")

        released = release_task(
            "SM-901",
            "alice",
            "origin",
            repo=release_repo,
            queue_path=release_repo / "execution/tasks.yaml",
        )
        self.assertTrue(released["released"])
        self.assertEqual("", fixture.lock_refs())
        self.assertEqual("SM-902", task_harness.selectable_tasks(fixture.queue())[0]["id"])

        next_stdout = io.StringIO()
        with contextlib.redirect_stdout(next_stdout):
            self.assertEqual(
                0,
                task_harness.main(["next", "--queue", str(queue_path), "--json"]),
            )
        self.assertEqual("SM-902", json.loads(next_stdout.getvalue())["task"]["id"])
        self.assertEqual(outside_queue, ROOT_QUEUE.read_bytes())
        current_outside_refs = subprocess.run(
            ["git", "show-ref"], cwd=ROOT, text=True, capture_output=True, shell=False
        ).stdout
        self.assertEqual(outside_refs, current_outside_refs)

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, shell=False
        )
        if result.returncode:
            raise AssertionError(f"fixture git failed: {args}: {result.stderr}")
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
