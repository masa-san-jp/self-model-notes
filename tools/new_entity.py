#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import yaml

try:
    from kb import ROOT, vocabularies
    from profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        profile_root_error,
        resolve_profile_root,
    )
except ModuleNotFoundError:  # Imported as tools.new_entity by the test suite.
    from tools.kb import ROOT, vocabularies
    from tools.profile_root import (
        ProfileRootError,
        add_profile_root_argument,
        atomic_write_text,
        profile_root_error,
        resolve_profile_root,
    )


def template(kind: str, slug: str, subject: str | None) -> dict:
    today = date.today().isoformat()
    common = {"id": f"{kind}/{slug}", "type": kind, "created": today, "updated": today}
    if kind == "subject":
        return {**common, "pseudonym": slug, "direct_identifiers_stored": False, "consent_refs": [], "allowed_purposes": ["self-reflection"], "prohibited_purposes": ["clinical-diagnosis", "employment-decision"], "status": "active"}
    if not subject:
        raise SystemExit("--subject is required for non-subject entities")
    if kind == "source":
        return {**common, "subject": subject, "source_kind": "other", "captured_at": None, "locator": None, "raw_content_stored": False, "consent": {"obtained": False, "obtained_at": None, "purposes": [], "allowed_operations": [], "expires_at": None, "revoked_at": None, "notes": None}, "reliability_notes": None}
    if kind == "event":
        return {**common, "subject": subject, "time": {"observed_at": None, "precision": "unknown"}, "context": {"domains": [], "social": []}, "state": {"fatigue": "unknown", "stress": "unknown"}, "trigger": None, "observed_facts": [], "raw_voice": [], "appraisal": [], "emotion": [], "body": [], "cognition": [], "action": [], "immediate_outcome": [], "delayed_outcome": [], "source_refs": []}
    if kind == "claim":
        return {**common, "subject": subject, "layer": "other", "scope": "state", "statement": None, "conditions": [], "supporting_evidence": [], "counterevidence": [], "alternative_explanations": [None, None], "confidence": "unknown", "status": "hypothesis", "supersedes": None, "superseded_by": None, "motivation_direction": None}
    if kind == "pattern":
        return {**common, "subject": subject, "condition": None, "recurring_appraisal": [], "recurring_drive": [], "recurring_action": [], "reinforcement": [], "contexts_seen": [], "evidence": [], "claim_refs": [], "counterevidence": [], "confidence": "unknown", "status": "hypothesis"}
    return {**common, "subject": subject, "instrument": {"name": None, "version": None, "official": True, "scoring_reference": None}, "administered_at": None, "source_ref": None, "scores": {}, "interpretation_claim_refs": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=vocabularies()["entity_types"])
    parser.add_argument("slug")
    parser.add_argument("--subject")
    add_profile_root_argument(parser)
    args = parser.parse_args()
    if args.profile_root is None:
        profile_root_error(
            ProfileRootError(
                "PROFILE_ROOT_REQUIRED",
                "pass --profile-root for real entity writes; repository fallback is disabled",
            )
        )
        return 2
    try:
        layout = resolve_profile_root(args.profile_root)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2
    if args.kind != "subject" and args.subject not in layout.subject_ids:
        profile_root_error(
            ProfileRootError(
                "PROFILE_SUBJECT_NOT_DECLARED",
                "pass a subject declared by the external profile contract",
            )
        )
        return 2
    plural = vocabularies()["plural_paths"][args.kind]
    path = layout.entity_root / plural / f"{args.slug}.md"
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path.relative_to(layout.root)}")
    content = "---\n" + yaml.safe_dump(template(args.kind, args.slug, args.subject), allow_unicode=True, sort_keys=False) + "---\n\n# Notes\n"
    try:
        atomic_write_text(path, content)
    except ProfileRootError as error:
        profile_root_error(error)
        return 2
    print(path.relative_to(layout.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
