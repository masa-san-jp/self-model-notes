from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from tools.task_harness import selectable_tasks


ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "execution" / "tasks.yaml"


EXPECTED_DEPENDENCIES = {
    "SM-012": ["SM-011"],
    "SM-013": ["SM-012"],
    "SM-014": ["SM-013"],
    "SM-015": ["SM-014"],
    "SM-016": ["SM-015"],
    "SM-017": ["SM-016"],
    "SM-018": ["SM-017"],
    "SM-019": ["SM-018"],
    "SM-020": ["SM-019"],
    "SM-021": ["SM-020"],
    "SM-022": ["SM-021"],
    "SM-023": ["SM-022"],
    "SM-024": ["SM-023"],
    "SM-025": ["SM-024"],
    "SM-026": ["SM-025"],
}

HARNESS_ISSUES = {
    f"SM-{index:03d}": f"https://github.com/masa-san-jp/self-model-notes/issues/{number}"
    for index, number in zip(range(19, 27), range(53, 61))
}


def load_queue() -> dict:
    with QUEUE.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise AssertionError("execution/tasks.yaml must contain a mapping")
    return value


class ExecutionTaskQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.queue = load_queue()
        cls.tasks = cls.queue["tasks"]
        cls.by_id = {task["id"]: task for task in cls.tasks}

    def test_queue_version_and_unique_contiguous_ids(self):
        self.assertEqual(3, self.queue["version"])
        ids = [task["id"] for task in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([f"SM-{index:03d}" for index in range(1, 27)], ids)

    def test_dependencies_are_existing_and_acyclic_by_id(self):
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                self.assertNotIn(task["id"], task["depends_on"])
                for dependency in task["depends_on"]:
                    self.assertIn(dependency, self.by_id)
                    self.assertLess(int(dependency.split("-")[1]), int(task["id"].split("-")[1]))

    def test_gap_closure_dependency_matrix_is_fixed(self):
        for task_id, dependencies in EXPECTED_DEPENDENCIES.items():
            with self.subTest(task=task_id):
                self.assertEqual(dependencies, self.by_id[task_id]["depends_on"])

    def test_gap_tasks_are_dispatchable_and_have_contract_fields(self):
        allowed_statuses = set(self.queue["policy"]["status_values"])
        for task_id in EXPECTED_DEPENDENCIES:
            task = self.by_id[task_id]
            with self.subTest(task=task_id):
                self.assertIn(task["status"], allowed_statuses)
                self.assertTrue(task["issue"].startswith("https://github.com/"))
                self.assertIn("execution/tasks.yaml", task["allowed_paths"])
                self.assertTrue(task["acceptance"])
                self.assertTrue(task["checks"])
                self.assertTrue(task["stop_if"])
                self.assertIn("evidence", task)

    def test_harness_tasks_have_fixed_issue_links_and_claim_shape(self):
        for task_id, issue in HARNESS_ISSUES.items():
            task = self.by_id[task_id]
            with self.subTest(task=task_id):
                self.assertEqual(issue, task["issue"])
                self.assertIn("claim", task)
                self.assertIsNone(task["claim"])
                self.assertTrue(task["allowed_paths"])
                self.assertTrue(task["acceptance"])
                self.assertTrue(task["checks"])
                self.assertTrue(task["stop_if"])
                self.assertIn("execution/tasks.yaml", task["allowed_paths"])

    def test_selection_is_empty_while_bootstrap_is_in_progress(self):
        if self.by_id["SM-012"]["status"] == "in-progress":
            self.assertEqual([], selectable_tasks(self.queue))

    def test_selection_uses_lowest_ready_task_through_task_lifecycle(self):
        if self.by_id["SM-012"]["status"] == "done":
            eligible = [
                task["id"]
                for task in self.tasks
                if task["status"] == "ready"
                and set(task["depends_on"]).issubset(
                    {candidate["id"] for candidate in self.tasks if candidate["status"] == "done"}
                )
            ]
            expected = [min(eligible)] if eligible else []
            self.assertEqual(expected, [task["id"] for task in selectable_tasks(self.queue)])

    def test_harness_bootstrap_has_only_one_next_task(self):
        if self.by_id["SM-019"]["status"] == "in-progress":
            self.assertEqual([], [task["id"] for task in selectable_tasks(self.queue)])
        elif self.by_id["SM-020"]["status"] == "ready":
            self.assertEqual(["SM-020"], [task["id"] for task in selectable_tasks(self.queue)])
        elif self.by_id["SM-021"]["status"] == "ready":
            self.assertEqual(["SM-021"], [task["id"] for task in selectable_tasks(self.queue)])
        elif self.by_id["SM-021"]["status"] == "done":
            self.assertEqual(["SM-022"], [task["id"] for task in selectable_tasks(self.queue)])
        else:
            self.fail("harness queue must expose exactly one lifecycle task")

    def test_existing_tasks_are_unchanged_in_status_and_evidence_shape(self):
        for task in self.tasks:
            if int(task["id"].split("-")[1]) <= 11:
                with self.subTest(task=task["id"]):
                    self.assertEqual("done", task["status"])
                    self.assertTrue(task["evidence"])


if __name__ == "__main__":
    unittest.main()
