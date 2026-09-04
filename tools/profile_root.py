#!/usr/bin/env python3
"""Resolve and validate the external Self Model profile storage boundary.

The protocol repository owns schemas, tools, and documentation.  A profile
root owns profile.yaml, canonical entities, and generated artifacts.  This
module is the only resolver for that boundary: callers must pass an explicit
absolute profile root and may not fall back to the repository's legacy
entities/ tree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "profile-root-schema.yaml"
PROFILE_FILE = "profile.yaml"
PROFILE_ENTITIES = "entities"
PROFILE_DATA = "data"
PROFILE_OVERVIEWS = "overviews"
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUBJECT_ID_RE = re.compile(r"^subject/[a-z0-9]+(?:-[a-z0-9]+)*$")
CONTRACT_VERSION = "self-model-profile/v1"
STORAGE_SCOPE = "private"
PROFILE_FIELDS = frozenset(
    {"contract_version", "profile_id", "subject_ids", "storage_scope"}
)


class ProfileRootError(ValueError):
    """A stable, safe-to-display profile boundary error."""

    def __init__(self, code: str, remediation: str):
        self.code = code
        self.remediation = remediation
        super().__init__(f"{code}: {remediation}")


@dataclass(frozen=True)
class ProfileLayout:
    """Validated paths for one external profile."""

    root: Path
    profile: dict[str, Any]

    @property
    def profile_file(self) -> Path:
        return self.root / PROFILE_FILE

    @property
    def entity_root(self) -> Path:
        return self.root / PROFILE_ENTITIES

    @property
    def data_root(self) -> Path:
        return self.root / PROFILE_DATA

    @property
    def overview_root(self) -> Path:
        return self.root / PROFILE_OVERVIEWS

    @property
    def subject_ids(self) -> tuple[str, ...]:
        return tuple(self.profile["subject_ids"])


def _schema() -> dict[str, Any]:
    try:
        value = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProfileRootError(
            "PROFILE_SCHEMA_UNAVAILABLE",
            "repository profile schema cannot be read; repair config/profile-root-schema.yaml",
        ) from exc
    if not isinstance(value, dict):
        raise ProfileRootError(
            "PROFILE_SCHEMA_INVALID",
            "repository profile schema is invalid; repair config/profile-root-schema.yaml",
        )
    return value


def _safe_yaml(path: Path, code: str, remediation: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProfileRootError(code, remediation) from exc


def _is_within(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""

    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _normal_directory(path: Path, code: str, remediation: str) -> Path:
    if not path.is_absolute():
        raise ProfileRootError("PROFILE_ROOT_NOT_ABSOLUTE", "pass --profile-root as an absolute directory")
    if path.is_symlink():
        raise ProfileRootError(code, remediation)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ProfileRootError(code, remediation) from exc
    if not stat.S_ISDIR(mode):
        raise ProfileRootError(code, remediation)
    return path


def _validate_profile_document(document: Any, *, subject_ids: set[str]) -> dict[str, Any]:
    schema = _schema()
    if not isinstance(document, dict):
        raise ProfileRootError(
            "PROFILE_CONTRACT_INVALID",
            "profile.yaml must be a mapping with contract_version, profile_id, subject_ids, and storage_scope",
        )
    unknown = set(document) - PROFILE_FIELDS
    if unknown:
        raise ProfileRootError(
            "PROFILE_UNKNOWN_FIELD",
            "remove unknown profile.yaml fields; the self-model-profile/v1 contract is closed",
        )
    missing = PROFILE_FIELDS - set(document)
    if missing:
        raise ProfileRootError(
            "PROFILE_REQUIRED_FIELD",
            "add every required profile.yaml field from config/profile-root-schema.yaml",
        )
    if document.get("contract_version") != schema.get("contract_version", CONTRACT_VERSION):
        raise ProfileRootError(
            "PROFILE_CONTRACT_VERSION",
            "set contract_version to self-model-profile/v1",
        )
    profile_id = document.get("profile_id")
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfileRootError(
            "PROFILE_ID_INVALID",
            "set profile_id to a lowercase kebab-case opaque profile identifier",
        )
    declared_subjects = document.get("subject_ids")
    if (
        not isinstance(declared_subjects, list)
        or not declared_subjects
        or any(not isinstance(value, str) or not SUBJECT_ID_RE.fullmatch(value) for value in declared_subjects)
        or len(set(declared_subjects)) != len(declared_subjects)
    ):
        raise ProfileRootError(
            "PROFILE_SUBJECT_IDS_INVALID",
            "set subject_ids to a non-empty unique list of subject/<kebab-case-slug> IDs",
        )
    if not isinstance(document.get("storage_scope"), str) or document["storage_scope"] not in set(
        schema.get("storage_scopes", [STORAGE_SCOPE])
    ):
        raise ProfileRootError(
            "PROFILE_STORAGE_SCOPE_INVALID",
            "set storage_scope to private; public projections are a separate boundary",
        )
    if subject_ids and not set(declared_subjects).issubset(subject_ids):
        raise ProfileRootError(
            "PROFILE_SUBJECT_MISMATCH",
            "every profile subject_id must match a canonical external subject entity",
        )
    return {
        "contract_version": document["contract_version"],
        "profile_id": profile_id,
        "subject_ids": list(declared_subjects),
        "storage_scope": document["storage_scope"],
    }


def _frontmatter(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return None
    raw = text[4:].split("\n---\n", 1)[0]
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    return value if isinstance(value, dict) else None


def _external_subject_ids(entity_root: Path) -> set[str]:
    subjects = entity_root / "subjects"
    if not subjects.is_dir() or subjects.is_symlink():
        return set()
    result: set[str] = set()
    for path in sorted(subjects.glob("*.md")):
        if path.name == "README.md" or path.is_symlink() or not path.is_file():
            continue
        meta = _frontmatter(path)
        entity_id = meta.get("id") if isinstance(meta, dict) else None
        if (
            isinstance(entity_id, str)
            and meta.get("type") == "subject"
            and SUBJECT_ID_RE.fullmatch(entity_id)
            and path.stem == entity_id.split("/", 1)[1]
        ):
            result.add(entity_id)
    return result


def _validate_layout(layout: ProfileLayout) -> None:
    if layout.profile["subject_ids"]:
        available = _external_subject_ids(layout.entity_root)
        if not set(layout.profile["subject_ids"]).issubset(available):
            raise ProfileRootError(
                "PROFILE_SUBJECT_MISMATCH",
                "every profile subject_id must match a canonical external subject entity",
            )


def _git_worktree_roots(repository_root: Path) -> list[Path]:
    """Read worktree roots without exposing their paths in an error."""

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    roots: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line.removeprefix("worktree "))
            if candidate.is_absolute():
                roots.append(candidate)
    return roots


def resolve_profile_root(
    value: str | Path | None,
    *,
    repository_root: Path = ROOT,
    projection_roots: Iterable[Path] | None = None,
    worktree_roots: Iterable[Path] | None = None,
) -> ProfileLayout:
    """Resolve one explicit, external, contract-valid profile root."""

    if value is None:
        raise ProfileRootError(
            "PROFILE_ROOT_REQUIRED",
            "pass --profile-root with the approved external profile directory; repository fallback is disabled",
        )
    raw = Path(value)
    if not raw.is_absolute():
        raise ProfileRootError(
            "PROFILE_ROOT_NOT_ABSOLUTE",
            "pass --profile-root as an absolute directory",
        )
    root = _normal_directory(
        raw,
        "PROFILE_ROOT_INVALID",
        "profile root must be an existing normal directory, not a symlink or special file",
    )
    try:
        resolved_root = root.resolve(strict=True)
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ProfileRootError(
            "PROFILE_ROOT_INVALID",
            "profile root and protocol repository must be existing normal directories",
        ) from exc
    repository_roots = [resolved_repository]
    repository_roots.extend(
        worktree.resolve(strict=False)
        for worktree in (list(worktree_roots) if worktree_roots is not None else _git_worktree_roots(repository_root))
    )
    if any(_is_within(resolved_root, candidate) for candidate in repository_roots):
        raise ProfileRootError(
            "PROFILE_ROOT_REPOSITORY_OVERLAP",
            "profile root must be outside the protocol repository and all of its worktrees",
        )
    projections = list(projection_roots or ())
    projections.extend(
        repository_root / name
        for name in ("public", "projection", "projections")
    )
    for projection in projections:
        try:
            if _is_within(resolved_root, projection.resolve(strict=False)):
                raise ProfileRootError(
                    "PROFILE_ROOT_PROJECTION_OVERLAP",
                    "profile root must not overlap a public projection",
                )
        except OSError:
            continue

    profile_file = root / PROFILE_FILE
    if profile_file.is_symlink() or not profile_file.is_file():
        raise ProfileRootError(
            "PROFILE_FILE_INVALID",
            "profile root must contain a regular profile.yaml file",
        )
    document = _safe_yaml(
        profile_file,
        "PROFILE_FILE_INVALID",
        "repair profile.yaml using config/profile-root-schema.yaml",
    )
    entity_root = root / PROFILE_ENTITIES
    if entity_root.exists() and entity_root.is_symlink():
        raise ProfileRootError(
            "PROFILE_LAYOUT_INVALID",
            "profile entities/ must be a normal directory owned by the profile root",
        )
    if not entity_root.is_dir():
        raise ProfileRootError(
            "PROFILE_LAYOUT_INVALID",
            "profile root must contain an entities/ directory",
        )
    for generated_name in (PROFILE_DATA, PROFILE_OVERVIEWS):
        generated_root = root / generated_name
        if generated_root.exists() and (
            generated_root.is_symlink() or not generated_root.is_dir()
        ):
            raise ProfileRootError(
                "PROFILE_LAYOUT_INVALID",
                f"profile {generated_name}/ must be a normal directory when present",
            )
    available_subjects = _external_subject_ids(entity_root)
    profile = _validate_profile_document(document, subject_ids=available_subjects)
    layout = ProfileLayout(root=resolved_root, profile=profile)
    _validate_layout(layout)
    return layout


def add_profile_root_argument(parser: argparse.ArgumentParser) -> None:
    """Add the common explicit profile-root CLI option."""

    parser.add_argument(
        "--profile-root",
        type=Path,
        help="absolute external profile directory; no repository fallback is used",
    )


def profile_root_error(error: ProfileRootError, *, stream: Any = sys.stderr) -> None:
    """Render only stable remediation, never the supplied absolute path."""

    print(f"ERROR {error.code}: {error.remediation}", file=stream)


def validate_repository(
    repository_root: Path = ROOT,
    *,
    tracked_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Classify the protocol tree without exposing legacy entity content."""

    if tracked_paths is None:
        tracked_paths = []
        git_dir = repository_root / ".git"
        if git_dir.exists():
            result = subprocess.run(
                ["git", "ls-files", "entities"],
                cwd=repository_root,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                tracked_paths = result.stdout.splitlines()
    legacy_records = sorted(
        path
        for path in tracked_paths
        if path.startswith("entities/")
        and path.endswith(".md")
        and not path.endswith("/README.md")
    )
    if legacy_records:
        return {
            "status": "BLOCKED_LEGACY_PROFILE",
            "remediation": "complete the human-approved external profile migration before removing tracked records",
            "legacy_record_count": len(legacy_records),
        }
    return {
        "status": "PASS",
        "remediation": "protocol tree contains no tracked real profile record",
        "legacy_record_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    add_profile_root_argument(parser)
    parser.add_argument("command", nargs="?", choices=("resolve", "validate-repository"), default="resolve")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-repository":
        result = validate_repository()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(f"{result['status']}: {result['remediation']}")
        return 0
    try:
        layout = resolve_profile_root(args.profile_root)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2
    result = {
        "contract_version": layout.profile["contract_version"],
        "profile_id": layout.profile["profile_id"],
        "subject_count": len(layout.subject_ids),
        "status": "PASS",
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("PASS: profile root is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
