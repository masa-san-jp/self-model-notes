#!/usr/bin/env python3
"""Plan and apply a metadata-audited external profile migration.

The migration boundary is deliberately narrow.  A plan reports only relative
root markers, counts, digests, and the digest of the existing generated tree.
Apply copies ``profile.yaml`` and canonical ``entities/`` files into a new or
empty destination, never overwrites a destination file, and never modifies
the source.  Real-profile apply remains an explicit human-approved operation;
the repository tests exercise only synthetic temporary profiles.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable

try:  # Script execution from the repository root.
    from profile_root import (
        ROOT,
        ProfileLayout,
        ProfileRootError,
        profile_root_error,
        resolve_profile_root,
        validate_external_directory,
    )
except ImportError:  # Module execution from the repository package.
    from tools.profile_root import (
        ROOT,
        ProfileLayout,
        ProfileRootError,
        profile_root_error,
        resolve_profile_root,
        validate_external_directory,
    )


PLAN_VERSION = "self-model-profile-migration/v1"
APPROVAL_FIELDS = frozenset({"approved", "scope", "plan_sha256"})
APPROVAL_SCOPES = frozenset({"synthetic-profile-migration", "real-profile-migration"})
SHA256_RE = r"^[0-9a-f]{64}$"


class MigrationError(ValueError):
    """A stable, content-free migration error."""

    def __init__(self, code: str, remediation: str):
        self.code = code
        self.remediation = remediation
        super().__init__(f"{code}: {remediation}")


@dataclass(frozen=True)
class Manifest:
    """Digest metadata for one set of files."""

    entries: tuple[tuple[str, int, str], ...]
    digest: str

    @property
    def file_count(self) -> int:
        return len(self.entries)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except (OSError, UnicodeError) as exc:
        raise MigrationError(
            "MIGRATION_SOURCE_UNREADABLE",
            "a canonical profile file could not be read; keep the source unchanged and repair it",
        ) from exc
    return digest.hexdigest()


def _metadata_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    payload = [
        {"path": path, "size": size, "sha256": digest}
        for path, size, digest in sorted(entries)
    ]
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _regular_file(path: Path, *, code: str, remediation: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise MigrationError(code, remediation) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise MigrationError(code, remediation)


def _canonical_file_paths(layout: ProfileLayout) -> list[Path]:
    """Return profile metadata and canonical entity files, rejecting links."""

    _regular_file(
        layout.profile_file,
        code="MIGRATION_SOURCE_UNSAFE",
        remediation="source profile.yaml must be a regular file without symlink aliases",
    )
    paths = [layout.profile_file]
    try:
        candidates = sorted(layout.entity_root.rglob("*"))
    except OSError as exc:
        raise MigrationError(
            "MIGRATION_SOURCE_UNREADABLE",
            "source entities could not be enumerated; keep the source unchanged and repair it",
        ) from exc
    for path in candidates:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise MigrationError(
                "MIGRATION_SOURCE_UNREADABLE",
                "source entities could not be inspected; keep the source unchanged and repair it",
            ) from exc
        if stat.S_ISLNK(mode):
            raise MigrationError(
                "MIGRATION_SOURCE_UNSAFE",
                "source canonical entities must not contain symlink aliases",
            )
        if stat.S_ISREG(mode):
            paths.append(path)
        elif not stat.S_ISDIR(mode):
            raise MigrationError(
                "MIGRATION_SOURCE_UNSAFE",
                "source canonical entities must contain only regular files and directories",
            )
    return paths


def _manifest_for_paths(root: Path, paths: Iterable[Path]) -> Manifest:
    entries: list[tuple[str, int, str]] = []
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
        except (OSError, ValueError) as exc:
            raise MigrationError(
                "MIGRATION_SOURCE_UNREADABLE",
                "source metadata could not be read; keep the source unchanged and repair it",
            ) from exc
        entries.append((relative, size, _file_sha256(path)))
    ordered = tuple(sorted(entries))
    return Manifest(entries=ordered, digest=_metadata_digest(ordered))


def _generated_manifest(root: Path) -> Manifest:
    """Digest existing generated files without copying or printing them."""

    entries: list[tuple[str, int, str]] = []
    for directory_name in ("data", "overviews"):
        directory = root / directory_name
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise MigrationError(
                "MIGRATION_SOURCE_UNSAFE",
                "generated data and overviews must be normal directories",
            )
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise MigrationError(
                    "MIGRATION_SOURCE_UNSAFE",
                    "generated data and overviews must not contain symlink aliases",
                )
            if path.is_dir():
                continue
            _regular_file(
                path,
                code="MIGRATION_SOURCE_UNSAFE",
                remediation="generated data and overviews must contain only regular files",
            )
            try:
                relative = path.relative_to(root).as_posix()
                size = path.stat().st_size
            except (OSError, ValueError) as exc:
                raise MigrationError(
                    "MIGRATION_SOURCE_UNREADABLE",
                    "generated artifact metadata could not be read; keep the source unchanged",
                ) from exc
            entries.append((relative, size, _file_sha256(path)))
    ordered = tuple(sorted(entries))
    return Manifest(entries=ordered, digest=_metadata_digest(ordered))


def _source_manifest(source: str | Path) -> tuple[ProfileLayout, Manifest, Manifest]:
    try:
        layout = resolve_profile_root(source, repository_root=ROOT)
    except ProfileRootError:
        raise
    paths = _canonical_file_paths(layout)
    return layout, _manifest_for_paths(layout.root, paths), _generated_manifest(layout.root)


def _destination_files(destination: Path) -> Manifest:
    if not destination.exists():
        return Manifest(entries=(), digest=_metadata_digest(()))
    if destination.is_symlink() or not destination.is_dir():
        raise MigrationError(
            "MIGRATION_DESTINATION_INVALID",
            "destination must be a new or empty normal directory",
        )
    entries: list[tuple[str, int, str]] = []
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise MigrationError(
                "MIGRATION_DESTINATION_INVALID",
                "destination must not contain symlink aliases",
            )
        if path.is_dir():
            continue
        _regular_file(
            path,
            code="MIGRATION_DESTINATION_INVALID",
            remediation="destination must be a new or empty normal directory",
        )
        # The destination is only used for emptiness checks.  Its file bytes
        # never enter the user-visible plan.
        try:
            relative = path.relative_to(destination).as_posix()
            size = path.stat().st_size
        except (OSError, ValueError) as exc:
            raise MigrationError(
                "MIGRATION_DESTINATION_INVALID",
                "destination metadata could not be inspected safely",
            ) from exc
        entries.append((relative, size, _file_sha256(path)))
    ordered = tuple(sorted(entries))
    return Manifest(entries=ordered, digest=_metadata_digest(ordered))


def _require_empty_destination(destination: Path) -> Manifest:
    manifest = _destination_files(destination)
    if manifest.entries:
        raise MigrationError(
            "MIGRATION_DESTINATION_NOT_EMPTY",
            "choose a new or empty destination; existing files are never overwritten",
        )
    return manifest


def _plan_payload(
    source_manifest: Manifest,
    destination_manifest: Manifest,
    generated_digest: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation": "profile-migration-plan",
        "contract_version": PLAN_VERSION,
        "source": {
            "relative_root": ".",
            "file_count": source_manifest.file_count,
            "sha256": source_manifest.digest,
        },
        "destination": {
            "relative_root": ".",
            "file_count": destination_manifest.file_count,
            "sha256": destination_manifest.digest,
        },
        "expected_generated_digest": generated_digest,
    }
    payload["plan_sha256"] = _sha256_bytes(_canonical_json(payload).encode("utf-8"))
    return payload


def plan(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Produce a deterministic metadata-only migration plan."""

    layout, source_manifest, generated = _source_manifest(source)
    destination_path = validate_external_directory(
        destination,
        repository_root=ROOT,
        require_existing=False,
    )
    if _same_or_overlapping(layout.root, destination_path):
        raise MigrationError(
            "MIGRATION_DESTINATION_OVERLAP",
            "source and destination must be distinct non-overlapping external directories",
        )
    destination_manifest = _require_empty_destination(destination_path)
    return _plan_payload(source_manifest, destination_manifest, generated.digest)


def _same_or_overlapping(left: Path, right: Path) -> bool:
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


def _load_approval(path: str | Path, plan_sha256: str) -> dict[str, Any]:
    approval = Path(path)
    if not approval.is_absolute() or approval.is_symlink() or not approval.is_file():
        raise MigrationError(
            "MIGRATION_APPROVAL_INVALID",
            "pass an explicit regular approval file with approved: true and a migration scope",
        )
    try:
        import yaml

        document = yaml.safe_load(approval.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MigrationError(
            "MIGRATION_APPROVAL_INVALID",
            "approval file must be readable YAML with approved: true and a migration scope",
        ) from exc
    if not isinstance(document, dict) or set(document) - APPROVAL_FIELDS:
        raise MigrationError(
            "MIGRATION_APPROVAL_INVALID",
            "approval file may contain only approved, scope, and optional plan_sha256",
        )
    if document.get("approved") is not True or document.get("scope") not in APPROVAL_SCOPES:
        raise MigrationError(
            "MIGRATION_APPROVAL_INVALID",
            "approval file must set approved: true and a recognized migration scope",
        )
    digest = document.get("plan_sha256")
    if digest is not None and (
        not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise MigrationError(
            "MIGRATION_APPROVAL_INVALID",
            "optional plan_sha256 must be a lowercase SHA-256 digest",
        )
    if digest is not None and digest != plan_sha256:
        raise MigrationError(
            "MIGRATION_APPROVAL_MISMATCH",
            "approval plan_sha256 does not match the current source and destination",
        )
    return document


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    """Copy one file without ever replacing an existing destination file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            with source.open("rb") as origin:
                shutil.copyfileobj(origin, target)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError as exc:
        raise MigrationError(
            "MIGRATION_DESTINATION_COLLISION",
            "destination changed during migration; no existing file was overwritten",
        ) from exc
    except OSError as exc:
        raise MigrationError(
            "MIGRATION_COPY_FAILED",
            "copy could not complete; source was not modified and destination remains isolated",
        ) from exc


def _copy_manifest(source: Path, destination: Path, manifest: Manifest) -> None:
    for relative, _size, _digest in manifest.entries:
        source_path = source / relative
        destination_path = destination / relative
        _copy_file_exclusive(source_path, destination_path)


def apply(
    source: str | Path,
    destination: str | Path,
    approval_file: str | Path,
) -> dict[str, Any]:
    """Apply a create-only migration after validating an explicit approval."""

    layout, source_manifest, generated = _source_manifest(source)
    destination_path = validate_external_directory(
        destination,
        repository_root=ROOT,
        require_existing=False,
    )
    if _same_or_overlapping(layout.root, destination_path):
        raise MigrationError(
            "MIGRATION_DESTINATION_OVERLAP",
            "source and destination must be distinct non-overlapping external directories",
        )
    destination_manifest = _require_empty_destination(destination_path)
    plan_payload = _plan_payload(source_manifest, destination_manifest, generated.digest)
    approval = _load_approval(approval_file, plan_payload["plan_sha256"])
    is_synthetic = layout.profile["profile_id"].startswith("synthetic-")
    expected_scope = "synthetic-profile-migration" if is_synthetic else "real-profile-migration"
    if approval["scope"] != expected_scope:
        raise MigrationError(
            "MIGRATION_APPROVAL_SCOPE",
            "approval scope does not match the selected profile migration boundary",
        )

    created_destination = not destination_path.exists()
    staging: Path | None = None
    target = destination_path
    try:
        if created_destination:
            try:
                staging = Path(
                    tempfile.mkdtemp(prefix=".profile-migration-", dir=destination_path.parent)
                )
            except OSError as exc:
                raise MigrationError(
                    "MIGRATION_DESTINATION_INVALID",
                    "destination staging directory could not be created safely",
                ) from exc
            target = staging
        _copy_manifest(layout.root, target, source_manifest)

        # Verify the source after copying.  A concurrent source change never
        # changes what was copied into the isolated destination.
        _current_layout, current_manifest, _current_generated = _source_manifest(layout.root)
        if current_manifest.digest != source_manifest.digest:
            raise MigrationError(
                "MIGRATION_SOURCE_CHANGED",
                "source changed during migration; inspect the isolated destination and retry",
            )

        try:
            resolve_profile_root(target, repository_root=ROOT)
        except ProfileRootError as exc:
            raise MigrationError(
                "MIGRATION_DESTINATION_INVALID",
                "copied destination does not satisfy the external profile contract",
            ) from exc

        if created_destination:
            if destination_path.exists() or destination_path.is_symlink():
                raise MigrationError(
                    "MIGRATION_DESTINATION_COLLISION",
                    "destination appeared during migration; no existing directory was replaced",
                )
            try:
                os.replace(target, destination_path)
            except FileExistsError as exc:
                raise MigrationError(
                    "MIGRATION_DESTINATION_COLLISION",
                    "destination appeared during migration; no existing file was overwritten",
                ) from exc
            except OSError as exc:
                raise MigrationError(
                    "MIGRATION_DESTINATION_INVALID",
                    "destination could not be committed without replacing existing data",
                ) from exc
            staging = None
    except MigrationError:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise MigrationError(
            "MIGRATION_COPY_FAILED",
            "copy could not complete; source was not modified and destination remains isolated",
        ) from exc

    return {
        "operation": "profile-migration-apply",
        "contract_version": PLAN_VERSION,
        "source": {
            "relative_root": ".",
            "file_count": source_manifest.file_count,
            "sha256": source_manifest.digest,
        },
        "destination": {
            "relative_root": ".",
            "file_count": source_manifest.file_count,
            "sha256": source_manifest.digest,
        },
        "copied_file_count": source_manifest.file_count,
        "expected_generated_digest": generated.digest,
        "source_unchanged": True,
        "status": "APPLIED",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--source", required=True, type=Path)
        subparser.add_argument("--destination", required=True, type=Path)
        if command == "apply":
            subparser.add_argument("--approval-file", required=True, type=Path)
        subparser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.source, args.destination)
        else:
            result = apply(args.source, args.destination, args.approval_file)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2
    except MigrationError as error:
        print(f"ERROR {error.code}: {error.remediation}", file=sys.stderr)
        return 2
    if args.json:
        print(_canonical_json(result))
    else:
        print("PASS: migration plan is ready" if args.command == "plan" else "PASS: profile migration applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
