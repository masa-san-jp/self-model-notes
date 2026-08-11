#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from kb import discover_entities


def findings(entities, subject=None):
    selected = [e for e in entities if not subject or e.meta.get("subject") == subject or e.id == subject]
    result = []
    for entity in selected:
        if entity.type == "claim":
            if not entity.meta.get("counterevidence"):
                result.append({"severity": "review", "entity": entity.id, "code": "NO_COUNTEREVIDENCE", "next_check": "Search for an event under the same condition with a different action or outcome."})
            if entity.meta.get("scope") in ("trait-candidate", "trait"):
                result.append({"severity": "review", "entity": entity.id, "code": "TRAIT_BREADTH_REVIEW", "next_check": "Verify evidence spans multiple times and contexts; do not auto-promote."})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = findings(discover_entities(), args.subject)
    print(json.dumps({"findings": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

