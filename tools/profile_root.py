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
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, date, timezone
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
STORAGE_SCOPE = "external-local"
PROFILE_FIELDS = frozenset(
    {"contract_version", "profile_id", "subject_ids", "storage_scope"}
)

# Repository privacy guard (Issue #119): categories of tracked file that must
# never carry a real personal record, independent of the legacy entities/
# tree check below. Every category is content-based (parsed structure, not a
# text search) so protocol docs and config/*-schema.yaml files that merely
# mention these contract names in prose are not misclassified.
FIXTURE_PATH_PREFIX = "tests/fixtures/"
ENTITY_RECORD_TYPES = frozenset(
    {"subject", "source", "event", "claim", "pattern", "measurement"}
)
GROWTH_LOG_JSONL_CONTRACTS = frozenset({"growth-miss/v1", "growth-hearing/v1"})
GROWTH_QUEUE_CONTRACT = "growth-queue/v1"


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
            "set storage_scope to external-local; public projections are a separate boundary",
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


def _is_fixture_path(path: str) -> bool:
    return path.startswith(FIXTURE_PATH_PREFIX)


def _yaml_document_or_none(path: Path) -> Any:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    return value


def _first_nonempty_line(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    return stripped
    except (OSError, UnicodeError):
        return None
    return None


def _identifier_pattern_hit(meta: dict[str, Any]) -> bool:
    """Reuse intake_conversation's direct-identifier patterns on fixture text fields.

    A deferred import avoids a circular import: intake_conversation imports
    this module at load time.
    """
    try:
        from .intake_conversation import EMAIL_RE, IDENTIFIER_LABEL_RE, PHONE_RE
    except ImportError:  # pragma: no cover - exercised when run as a script
        from intake_conversation import EMAIL_RE, IDENTIFIER_LABEL_RE, PHONE_RE

    texts: list[str] = []
    trigger = meta.get("trigger")
    if isinstance(trigger, str):
        texts.append(trigger)
    observed_facts = meta.get("observed_facts")
    if isinstance(observed_facts, list):
        texts.extend(value for value in observed_facts if isinstance(value, str))
    raw_voice = meta.get("raw_voice")
    if isinstance(raw_voice, list):
        for item in raw_voice:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    return any(
        EMAIL_RE.search(text) or PHONE_RE.search(text) or IDENTIFIER_LABEL_RE.search(text)
        for text in texts
    )


def _classify_privacy_path(repository_root: Path, path: str) -> str | None:
    """Classify one tracked file for the repository privacy guard (Issue #119).

    Returns a category name when the file looks like a real personal record,
    profile contract, growth log, or intake draft, or None when it is exempt
    (a synthetic fixture) or not one of the recognized shapes. Content is
    always parsed structurally; a substring match on prose is never enough.
    """
    if Path(path).name == "README.md":
        return None
    lower = path.lower()
    file_path = repository_root / path

    # Draft suffixes are a stronger, explicit signal than an entity-shaped
    # frontmatter block.  Check them first so a copied intake draft is
    # reported as intake-draft rather than being silently folded into the
    # legacy entity-record category.
    if lower.endswith((".source.draft.md", ".event.draft.md")):
        return "intake-draft"

    # The heading rule applies to any tracked file, not only Markdown.  Read
    # failures are treated as non-matches; Git's tracked path list may include
    # binary or otherwise undecodable files that are handled by other guards.
    try:
        body = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        body = ""
    if any(line.strip() == "# Intake draft" for line in body.splitlines()):
        return "intake-draft"

    if lower.endswith(".md"):
        meta = _frontmatter(file_path)
        if isinstance(meta, dict) and meta.get("type") in ENTITY_RECORD_TYPES:
            fixture_subject = meta.get("id") if meta.get("type") == "subject" else meta.get("subject")
            if _is_fixture_path(path) and fixture_subject == "subject/fixture":
                if _identifier_pattern_hit(meta):
                    return "raw-quote"
                return None
            return "entity-record"
        return None

    if lower.endswith((".yaml", ".yml")):
        document = _yaml_document_or_none(file_path)
        if not isinstance(document, dict):
            return None
        if path.startswith("config/") and Path(path).stem.endswith("-schema"):
            return None
        if document.get("contract_version") == CONTRACT_VERSION:
            if _is_fixture_path(path) and str(document.get("profile_id", "")).startswith("synthetic-"):
                return None
            return "profile-contract"
        if document.get("contract_version") == GROWTH_QUEUE_CONTRACT:
            # config/*-schema.yaml documents describe the contract and carry
            # a storage: field; they are not a profile's queue log.
            if _is_fixture_path(path):
                return None
            return "growth-log"
        if _contains_element_record(document):
            return "growth-log"
        return None

    if lower.endswith(".json"):
        try:
            document = json.loads(body)
        except (ValueError, TypeError):
            return None
        if _contains_element_record(document):
            return "growth-log"
        return None

    if lower.endswith(".jsonl"):
        # Element requests/answers can appear after an unrelated or malformed
        # first record. Inspect every line, including nested relay envelopes.
        for line in body.splitlines():
            try:
                element_record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if _contains_element_record(element_record):
                return "growth-log"
        first_line = _first_nonempty_line(file_path)
        if first_line is None:
            return None
        is_fixture = _is_fixture_path(path)
        try:
            record = json.loads(first_line)
        except json.JSONDecodeError:
            record = None
        if isinstance(record, dict) and record.get("contract_version") in GROWTH_LOG_JSONL_CONTRACTS:
            return None if is_fixture else "growth-log"
        if _contains_element_record(record):
            return "growth-log"
        if any(f'"contract_version":"{contract}"' in first_line for contract in GROWTH_LOG_JSONL_CONTRACTS):
            return None if is_fixture else "growth-log"
        return None

    return None


def _contains_element_record(value: Any) -> bool:
    if isinstance(value, dict):
        version = value.get("contract_version")
        return (isinstance(version, str) and version in {
            "growth-elements/v1", "element-request/v1", "element-answer/v1"
        }) or any(_contains_element_record(item) for item in value.values())
    return isinstance(value, list) and any(_contains_element_record(item) for item in value)


def _list_all_tracked_paths(repository_root: Path) -> list[str]:
    git_dir = repository_root / ".git"
    if not git_dir.exists():
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repository_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


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


def atomic_write_text(path: Path, content: str) -> None:
    """Write a generated text artifact without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
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
        raise ProfileRootError(
            "PROFILE_OUTPUT_UNWRITABLE",
            "profile output could not be written atomically; choose a writable profile root",
        ) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def path_is_within(path: Path, root: Path) -> bool:
    """Check containment after resolving an existing or proposed path."""

    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def validate_external_directory(
    value: str | Path,
    *,
    repository_root: Path = ROOT,
    require_existing: bool = False,
) -> Path:
    """Validate an external profile path before creating or using it.

    Migration destinations may not exist yet.  Their existing parent must be
    a normal directory, and the canonical candidate must be outside the
    protocol repository, its worktrees, and its projection boundaries.
    """

    candidate = Path(value)
    if not candidate.is_absolute():
        raise ProfileRootError(
            "PROFILE_ROOT_NOT_ABSOLUTE",
            "pass the external profile directory as an absolute path",
        )

    if candidate.exists() or candidate.is_symlink():
        if candidate.is_symlink() or not candidate.is_dir():
            raise ProfileRootError(
                "PROFILE_ROOT_INVALID",
                "external profile directory must be a normal directory, not a symlink or special file",
            )
        if require_existing:
            _normal_directory(
                candidate,
                "PROFILE_ROOT_INVALID",
                "external profile directory must be a normal directory, not a symlink or special file",
            )
    else:
        parent = candidate.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.exists() or parent.is_symlink() or not parent.is_dir():
            raise ProfileRootError(
                "PROFILE_ROOT_INVALID",
                "external profile destination must have a normal existing parent directory",
            )

    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ProfileRootError(
            "PROFILE_ROOT_INVALID",
            "external profile path could not be resolved safely",
        ) from exc

    repository_roots = [resolved_repository]
    repository_roots.extend(
        worktree.resolve(strict=False) for worktree in _git_worktree_roots(repository_root)
    )
    if any(_is_within(resolved_candidate, root) for root in repository_roots):
        raise ProfileRootError(
            "PROFILE_ROOT_REPOSITORY_OVERLAP",
            "external profile directory must be outside the protocol repository and all worktrees",
        )
    for projection_name in ("public", "projection", "projections"):
        projection = repository_root / projection_name
        try:
            if _is_within(resolved_candidate, projection.resolve(strict=False)):
                raise ProfileRootError(
                    "PROFILE_ROOT_PROJECTION_OVERLAP",
                    "external profile directory must not overlap a public projection",
                )
        except OSError:
            continue
    return resolved_candidate


def _onboarding_imports():
    # Deferred: kb/new_entity also import this resolver.
    try:
        from .kb import discover_entities, validate_entities, vocabularies
        from .new_entity import template
    except ImportError:  # CLI entry
        from kb import discover_entities, validate_entities, vocabularies
        from new_entity import template
    return discover_entities, validate_entities, vocabularies, template


def _onboarding_root(value: str | Path | None) -> Path:
    if value is None:
        raise ProfileRootError("PROFILE_ROOT_REQUIRED", "pass an explicit absolute external profile root")
    raw = Path(value)
    root = validate_external_directory(raw)
    if any(component.is_symlink() for component in (raw, *raw.parents)):
        raise ProfileRootError("PROFILE_ROOT_INVALID", "use a canonical directory path without symlink aliases")
    existing_parent = root
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=existing_parent,
            capture_output=True, check=False,
        )
    except OSError as exc:
        raise ProfileRootError("PROFILE_ROOT_INVALID", "external profile repository boundary could not be checked") from exc
    if repository.returncode == 0:
        raise ProfileRootError("PROFILE_ROOT_REPOSITORY_OVERLAP", "onboarding profile root must be outside every Git checkout")
    return root


def _onboarding_slug(slug: str | None) -> str:
    if not isinstance(slug, str) or not PROFILE_ID_RE.fullmatch(slug):
        raise ProfileRootError("PROFILE_SUBJECT_IDS_INVALID", "pass --subject as an opaque lowercase kebab-case slug")
    return slug


def _entity_text(meta: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n# Notes\n"


def _onboarding_entities(layout: ProfileLayout, discover, validate):
    # Never follow an entity alias or display parser errors containing text.
    if any(path.is_symlink() for path in layout.entity_root.iterdir()) or any(
        path.is_symlink() for path in layout.entity_root.glob("*/*.md")
    ):
        raise ProfileRootError("PROFILE_LAYOUT_INVALID", "onboarding requires canonical entity paths without symlinks")
    try:
        entities = discover(layout.entity_root)
        errors = validate(entities, root=layout.root)
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise ProfileRootError("PROFILE_ENTITIES_INVALID", "repair the external entities before recording consent") from exc
    if errors:
        raise ProfileRootError("PROFILE_ENTITIES_INVALID", "repair the external entities before recording consent")
    return entities


def init_profile(value: str | Path | None, *, subject: str | None) -> ProfileLayout:
    """Create a subject and profile together, without inventing consent."""
    slug = _onboarding_slug(subject)
    root = _onboarding_root(value)
    if root.exists():
        raise ProfileRootError("PROFILE_ROOT_EXISTS", "init requires a new directory; existing directories are never overwritten")
    if not root.parent.is_dir():
        raise ProfileRootError("PROFILE_ROOT_INVALID", "create the normal external parent directory before init")
    discover, validate, vocabularies, template = _onboarding_imports()
    created = False
    succeeded = False
    try:
        root.mkdir(mode=0o700)
        created = True
        for plural in vocabularies()["plural_paths"].values():
            (root / PROFILE_ENTITIES / plural).mkdir(parents=True, mode=0o700)
        for name in ("growth", PROFILE_DATA, PROFILE_OVERVIEWS):
            (root / name).mkdir(mode=0o700)
        meta = template("subject", slug, None)
        meta["allowed_purposes"] = []
        atomic_write_text(root / PROFILE_ENTITIES / "subjects" / f"{slug}.md", _entity_text(meta))
        profile = {"contract_version": CONTRACT_VERSION, "profile_id": slug,
                   "subject_ids": [f"subject/{slug}"], "storage_scope": STORAGE_SCOPE}
        atomic_write_text(root / PROFILE_FILE, yaml.safe_dump(profile, sort_keys=False))
        layout = resolve_profile_root(root)
        if validate(discover(layout.entity_root), root=root):
            raise ProfileRootError("PROFILE_ENTITIES_INVALID", "initial subject did not pass the existing entity validator")
        succeeded = True
    except BaseException as exc:
        # Only our exclusively-created root is removed after a failed init.
        if created and not succeeded:
            shutil.rmtree(root)
        if isinstance(exc, OSError):
            raise ProfileRootError("PROFILE_OUTPUT_UNWRITABLE", "profile could not be created; choose a new writable external directory") from exc
        raise
    return layout


def record_hearing_consent(
    value: str | Path | None, *, subject: str | None, purposes: list[str] | None,
    allowed_operations: list[str] | None, expires_at: str | None,
    confirm_owner_consent: bool = False,
) -> ProfileLayout:
    """Record the owner's explicit scope as a conversation Source, create-only.

    The flag attests to prior owner confirmation; it is not an agent's grant
    of consent. No purpose, operation, or expiry is defaulted or added.
    """
    slug = _onboarding_slug(subject)
    root = _onboarding_root(value)
    layout = resolve_profile_root(root)
    if f"subject/{slug}" not in layout.subject_ids:
        raise ProfileRootError("PROFILE_SUBJECT_NOT_DECLARED", "choose a subject declared by this profile")
    discover, validate, vocabularies, template = _onboarding_imports()
    vocab = vocabularies()
    if not confirm_owner_consent:
        raise ProfileRootError("CONSENT_CONFIRMATION_REQUIRED", "obtain the owner's explicit confirmation before using --confirm-owner-consent")
    if (not purposes or not allowed_operations or expires_at is None
        or any(purpose not in vocab["allowed_purposes"] for purpose in purposes)
        or any(operation not in vocab["allowed_operations"] for operation in allowed_operations)):
        raise ProfileRootError("CONSENT_SCOPE_INVALID", "pass explicit purposes, allowed operations, and expiry using the existing consent vocabulary")
    expiry = None
    if expires_at != "none":
        try:
            expiry = date.fromisoformat(expires_at)
            if expiry.isoformat() != expires_at or expiry <= datetime.now(timezone.utc).date():
                raise ValueError
        except ValueError as exc:
            raise ProfileRootError("CONSENT_EXPIRY_INVALID", "use a future YYYY-MM-DD date, or explicitly choose none for no expiry") from exc
    entities = _onboarding_entities(layout, discover, validate)
    subject_entity = next(entity for entity in entities if entity.id == f"subject/{slug}")
    sources = root / PROFILE_ENTITIES / "sources"
    if sources.is_symlink() or not sources.is_dir() or subject_entity.path.is_symlink():
        raise ProfileRootError("PROFILE_LAYOUT_INVALID", "subject and sources must be normal paths inside the external profile")
    source_slug = f"hearing-consent-{slug}"
    source_path = sources / f"{source_slug}.md"
    if source_path.exists() or source_path.is_symlink():
        raise ProfileRootError("CONSENT_SOURCE_EXISTS", "consent Source already exists; review its scope, revocation and expiry without overwriting it")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    meta = template("source", source_slug, f"subject/{slug}")
    meta.update(source_kind="conversation", captured_at=now.isoformat())
    meta["consent"] = {"obtained": True, "obtained_at": now.date().isoformat(),
                       "purposes": list(dict.fromkeys(purposes)),
                       "allowed_operations": list(dict.fromkeys(allowed_operations)),
                       "expires_at": expiry.isoformat() if expiry else None,
                       "revoked_at": None, "notes": None}
    # Preserve the subject body and only add this explicitly confirmed scope.
    original = subject_entity.path.read_text(encoding="utf-8")
    subject_meta = dict(subject_entity.meta)
    subject_meta["consent_refs"] = [*subject_meta["consent_refs"], meta["id"]]
    subject_meta["allowed_purposes"] = list(dict.fromkeys([*subject_meta["allowed_purposes"], *purposes]))
    subject_meta["updated"] = now.date().isoformat()
    subject_text = "---\n" + yaml.safe_dump(subject_meta, allow_unicode=True, sort_keys=False) + "---\n" + subject_entity.body
    created = False
    updated = False
    succeeded = False
    try:
        # O_EXCL prevents a second writer from replacing any prior Source.
        descriptor = os.open(source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_entity_text(meta))
            handle.flush()
            os.fsync(handle.fileno())
        atomic_write_text(subject_entity.path, subject_text)
        updated = True
        _onboarding_entities(layout, discover, validate)
        succeeded = True
    except BaseException as exc:
        if not succeeded:
            try:
                if updated:
                    atomic_write_text(subject_entity.path, original)
            finally:
                if created:
                    source_path.unlink()
        if isinstance(exc, OSError):
            raise ProfileRootError("PROFILE_OUTPUT_UNWRITABLE", "consent could not be recorded in the external profile") from exc
        raise
    return layout


def validate_repository(
    repository_root: Path = ROOT,
    *,
    tracked_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Classify the protocol tree without exposing legacy entity content.

    Two checks run in order. The first (unchanged since before Issue #119)
    looks only at tracked entities/**/*.md paths and returns
    BLOCKED_LEGACY_PROFILE, exactly as before. Only when that check finds
    nothing does the second, broader guard run: it classifies every tracked
    file that could be a real personal record, self-model-profile/v1
    contract, growth-miss/growth-hearing/growth-queue log, or intake draft
    (Issue #119), and returns BLOCKED_PERSONAL_RECORD with per-category
    counts only -- never a path or file content -- when any is found.
    """

    if tracked_paths is None:
        all_tracked_paths = _list_all_tracked_paths(repository_root)
        if not all_tracked_paths:
            # Without a trustworthy Git index, classify any legacy-looking
            # record conservatively.  This is a read-only migration gate.
            entity_root = repository_root / "entities"
            if entity_root.is_dir() and not entity_root.is_symlink():
                all_tracked_paths = [
                    path.relative_to(repository_root).as_posix()
                    for path in entity_root.rglob("*.md")
                    if path.is_file() and not path.is_symlink()
                ]
    else:
        all_tracked_paths = list(tracked_paths)

    legacy_records = sorted(
        path
        for path in all_tracked_paths
        if path.startswith("entities/")
        and path.endswith(".md")
        and not path.endswith("/README.md")
    )
    if legacy_records:
        return {
            "status": "BLOCKED_LEGACY_PROFILE",
            "remediation": "complete the human-approved external profile migration before removing tracked records",
            "legacy_record_count": len(legacy_records),
            "blocked": {},
        }

    blocked: dict[str, int] = {}
    for path in all_tracked_paths:
        category = _classify_privacy_path(repository_root, path)
        if category is not None:
            blocked[category] = blocked.get(category, 0) + 1
    if blocked:
        return {
            "status": "BLOCKED_PERSONAL_RECORD",
            "remediation": "remove the tracked personal record, profile contract, growth log, or intake draft; real records belong only in the external profile root",
            "legacy_record_count": 0,
            "blocked": blocked,
        }
    return {
        "status": "PASS",
        "remediation": "protocol tree contains no tracked real profile record",
        "legacy_record_count": 0,
        "blocked": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    add_profile_root_argument(parser)
    parser.add_argument("command", nargs="?", choices=("resolve", "validate-repository", "init", "consent"), default="resolve")
    parser.add_argument("--subject", help="opaque lowercase kebab-case subject slug for init/consent")
    parser.add_argument("--purpose", action="append", help="owner-confirmed purpose; repeat for each purpose")
    parser.add_argument("--allowed-operation", action="append", help="owner-confirmed operation; repeat for each operation")
    parser.add_argument("--expires-at", help="owner-confirmed future YYYY-MM-DD, or explicit none")
    parser.add_argument("--confirm-owner-consent", action="store_true", help="attest that the owner explicitly confirmed this exact scope")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-repository":
        result = validate_repository()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(f"{result['status']}: {result['remediation']}")
        return 0 if result["status"] == "PASS" else 2
    try:
        if args.command == "init":
            if args.purpose or args.allowed_operation or args.expires_at or args.confirm_owner_consent:
                raise ProfileRootError("CONSENT_SCOPE_INVALID", "init creates no consent; use the separate consent command after owner confirmation")
            layout = init_profile(args.profile_root, subject=args.subject)
        elif args.command == "consent":
            layout = record_hearing_consent(args.profile_root, subject=args.subject, purposes=args.purpose,
                allowed_operations=args.allowed_operation, expires_at=args.expires_at,
                confirm_owner_consent=args.confirm_owner_consent)
        else:
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
