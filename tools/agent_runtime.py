#!/usr/bin/env python3
"""Select the repository Python runtime before running a repository command.

This launcher intentionally imports only the standard library.  Agents can
invoke it with the system Python without activating a virtual environment:

    python3 tools/agent_runtime.py tools/task_harness.py validate

The repository virtual environment wins when it has the declared PyYAML
dependency.  Otherwise the interpreter that launched this process is used if
it has PyYAML.  No installation or network access is performed here.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT_SECONDS = 5
RUNTIME_ERROR = 9


def _candidate_interpreters(
    root: Path = ROOT,
    *,
    current: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Path]:
    """Return interpreters in deterministic preference order without probing."""

    candidates: list[Path] = []
    seen: set[str] = set()

    def add(value: str | Path | None) -> None:
        if value is None:
            return
        path = Path(value)
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    virtualenv_python = root / ".venv" / "bin" / "python"
    if virtualenv_python.is_file() and os.access(virtualenv_python, os.X_OK):
        add(virtualenv_python)
    add(current or sys.executable)
    add(which("python3"))
    add(which("python"))
    return candidates


def _can_import_yaml(interpreter: Path, *, root: Path = ROOT) -> bool:
    """Probe only the declared runtime dependency in an isolated subprocess."""

    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
    }
    try:
        result = subprocess.run(
            [str(interpreter), "-c", "import yaml"],
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_interpreter(
    root: Path = ROOT,
    *,
    current: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[Path], bool] | None = None,
) -> Path | None:
    """Select the first usable interpreter, preferring the project venv."""

    probe = probe or (lambda interpreter: _can_import_yaml(interpreter, root=root))
    return next(
        (
            interpreter
            for interpreter in _candidate_interpreters(root, current=current, which=which)
            if probe(interpreter)
        ),
        None,
    )


def _target_argv(argv: Sequence[str], *, root: Path = ROOT) -> list[str]:
    """Resolve repository script paths so trusted-base execution stays trusted."""

    if not argv:
        raise ValueError(
            "usage: python3 tools/agent_runtime.py <python-arguments>"
        )
    if argv[0].startswith("-"):
        return list(argv)

    target = Path(argv[0])
    if target.suffix != ".py":
        return list(argv)

    resolved = target.resolve() if target.is_absolute() else (root / target).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Python script must be inside the repository") from exc
    return [str(resolved), *argv[1:]]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        target = _target_argv(arguments)
    except ValueError as error:
        sys.stderr.write(f"agent runtime: {error}\n")
        return 2

    interpreter = select_interpreter()
    if interpreter is None:
        sys.stderr.write(
            "agent runtime: no Python interpreter with PyYAML is available; "
            "provide .venv/bin/python or install the project dependency\n"
        )
        return RUNTIME_ERROR

    os.execv(str(interpreter), [str(interpreter), *target])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
