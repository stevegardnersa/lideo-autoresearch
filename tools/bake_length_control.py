#!/usr/bin/env python3
"""Bake length-control config + repair templates into registry version records.

The Cloud Function runs the length-control loop (`core.length_control`) behind
the `auto_length` request flag. To do that without importing the tuned
`candidate_spec` module, it reads two additive fields from the version record it
serves:

* ``length_control`` — the loop/repair knobs (tolerance, max passes, early-stop,
  repair targeting, ...). Mirrors ``candidate_spec.LengthControlConfig``.
* ``repair_templates`` — ``{"more": <template>, "less": <template>}`` with the
  repair policy + format instructions already inlined; only the dynamic
  placeholders (target/low/high/direction/numeric delta/summary/source) remain.

This migration is **additive and idempotent**: it only fills fields that are
absent unless ``--force`` is passed, and it never rewrites the system/user
prompt templates (so ``system_prompt_fingerprint`` is unchanged). It mutates the
local registry in place, which changes ``registry_sha``; run
``tools/sync_prompts.py`` afterwards so the Cloud Function bundle matches.

Run:  python3 tools/bake_length_control.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import candidate_spec  # noqa: E402
from core import prompt_registry, repair_prompt  # noqa: E402

DEFAULT_FORMAT_MODE = "markdown_sections"


def _length_control_defaults() -> Dict[str, Any]:
    return dataclasses.asdict(candidate_spec.LengthControlConfig())


def _repair_templates(stage: str, length_control: Dict[str, Any]) -> Dict[str, str]:
    more_policy = candidate_spec.REPAIR_MORE_POLICIES.get(
        str(length_control.get("repair_more_prompt_id") or ""),
        repair_prompt.DEFAULT_REPAIR_MORE_POLICY,
    )
    less_policy = candidate_spec.REPAIR_LESS_POLICIES.get(
        str(length_control.get("repair_less_prompt_id") or ""),
        repair_prompt.DEFAULT_REPAIR_LESS_POLICY,
    )
    format_instructions = candidate_spec.FORMAT_INSTRUCTIONS[DEFAULT_FORMAT_MODE]
    if stage == "composer":
        build = repair_prompt.composer_repair_template
    else:
        build = repair_prompt.chapter_repair_template
    return {
        "more": build(policy=more_policy, format_instructions=format_instructions),
        "less": build(policy=less_policy, format_instructions=format_instructions),
    }


def bake(*, dry_run: bool = False, force: bool = False) -> int:
    path = prompt_registry.registry_path()
    registry = prompt_registry.load_registry(path, seed=True)
    versions = registry.get("versions") or []
    if not versions:
        print(f"no versions in {path}; nothing to bake", file=sys.stderr)
        return 2

    touched = 0
    for version in versions:
        stage = str(version.get("stage") or "chapter")
        changed = False
        length_control = version.get("length_control")
        if force or not isinstance(length_control, dict) or not length_control:
            length_control = _length_control_defaults()
            version["length_control"] = length_control
            changed = True
        repair_templates = version.get("repair_templates")
        if force or not isinstance(repair_templates, dict) or not repair_templates:
            version["repair_templates"] = _repair_templates(stage, length_control)
            changed = True
        if changed:
            touched += 1
            print(f"  baked: {version.get('id')} ({stage})")
        else:
            print(f"  ok:    {version.get('id')} ({stage})")

    print(f"{touched} version(s) updated, {len(versions) - touched} already baked")
    if dry_run:
        print("dry-run: registry not written")
        return 0
    if touched == 0:
        print("nothing to write")
        return 0
    prompt_registry.save_registry(registry, path)
    print(f"wrote {path} (registry_sha={registry.get('registry_sha', '')[:12]})")
    print("next: python3 tools/sync_prompts.py")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing baked fields.")
    args = parser.parse_args()
    return bake(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
