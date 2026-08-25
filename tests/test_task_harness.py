from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import task_harness


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
        expected_id = "SM-020" if self.queue_task("SM-020")["status"] == "ready" else "SM-021"
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
        expected_id = "SM-020" if self.queue_task("SM-020")["status"] == "ready" else "SM-021"
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
        expected_id = "SM-020" if self.queue_task("SM-020")["status"] == "ready" else "SM-021"
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
        queue["tasks"][19]["status"] = "blocked"
        path = self.write_queue(queue)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = task_harness.main(["next", "--queue", str(path), "--json"])
        self.assertEqual(3, result)
        self.assertEqual(
            {"queue_version": 3, "reason": "no-selectable-task", "task": None},
            json.loads(stdout.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
