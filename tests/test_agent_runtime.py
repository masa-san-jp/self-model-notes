from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import agent_runtime


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "agent_runtime.py"


class AgentRuntimeTests(unittest.TestCase):
    def make_interpreters(self) -> tuple[Path, Path, Path, Path]:
        directory = Path(tempfile.mkdtemp())
        virtualenv = directory / ".venv" / "bin" / "python"
        virtualenv.parent.mkdir(parents=True)
        virtualenv.touch()
        virtualenv.chmod(0o755)
        current = directory / "current-python"
        current.touch()
        current.chmod(0o755)
        path_python = directory / "path-python"
        path_python.touch()
        path_python.chmod(0o755)
        return directory, virtualenv, current, path_python

    def test_project_virtualenv_is_preferred(self):
        directory, virtualenv, current, _ = self.make_interpreters()
        self.addCleanup(shutil.rmtree, directory)

        selected = agent_runtime.select_interpreter(
            directory,
            current=str(current),
            which=lambda _: None,
            probe=lambda interpreter: True,
        )

        self.assertEqual(virtualenv, selected)

    def test_current_interpreter_is_used_when_virtualenv_lacks_yaml(self):
        directory, virtualenv, current, _ = self.make_interpreters()
        self.addCleanup(shutil.rmtree, directory)

        selected = agent_runtime.select_interpreter(
            directory,
            current=str(current),
            which=lambda _: None,
            probe=lambda interpreter: interpreter == current,
        )

        self.assertEqual(current, selected)
        self.assertNotEqual(virtualenv, selected)

    def test_missing_yaml_runtime_is_reported_without_absolute_paths(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)

        with mock.patch.object(agent_runtime, "select_interpreter", return_value=None):
            with mock.patch("sys.stderr") as stderr:
                result = agent_runtime.main(["tools/task_harness.py", "validate"])

        self.assertEqual(agent_runtime.RUNTIME_ERROR, result)
        message = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("no Python interpreter with PyYAML is available", message)
        self.assertIn(".venv/bin/python", message)
        self.assertNotIn(str(directory), message)

    def test_runtime_entrypoint_runs_harness_without_activation(self):
        python3 = shutil.which("python3") or sys.executable
        result = subprocess.run(
            [python3, str(RUNTIME), "tools/task_harness.py", "validate"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("valid\n", result.stdout)

    def test_agent_contract_documents_explicit_fresh_clone_setup(self):
        contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("python3 -m venv .venv", contract)
        self.assertIn(".venv/bin/python -m pip install -e .", contract)
        self.assertIn("install、network access、credential、secretは通常の", contract)
        self.assertIn("python3 tools/agent_runtime.py", contract)
        self.assertIn("秘密", contract)
        self.assertIn("認証情報", contract)
        self.assertIn("raw_voice", contract)

    def test_target_script_is_resolved_inside_repository(self):
        target = agent_runtime._target_argv(["tools/task_harness.py", "validate"])
        self.assertEqual(str(ROOT / "tools" / "task_harness.py"), target[0])

        with self.assertRaises(ValueError):
            agent_runtime._target_argv(["../outside.py"], root=ROOT)


if __name__ == "__main__":
    unittest.main()
