#!/usr/bin/env python3
"""Sync the local prompt registry into the Cloud Function bundle.

Copies ``data/prompts/registry.json`` (or ``PROMPT_REGISTRY_PATH``) to
``cloud_function/prompts/registry.json`` and prints a diff of version ids and
fingerprints. Validates that each stage's production slot points at an existing
version. Only writes bundle files; it never deploys or calls GCP.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import prompt_registry  # noqa: E402

BUNDLE_PATH = ROOT / "cloud_function" / "prompts" / "registry.json"


def load_bundle() -> dict:
    if not BUNDLE_PATH.exists():
        return {}
    try:
        return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def version_map(registry: dict) -> dict:
    return {v.get("id"): str(v.get("system_prompt_fingerprint") or "") for v in registry.get("versions") or []}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="", help="Only report this stage (both are always copied).")
    parser.add_argument("--dry-run", action="store_true", help="Print the diff without writing the bundle.")
    args = parser.parse_args()

    source_path = prompt_registry.registry_path()
    registry = prompt_registry.load_registry(source_path, seed=True)

    slots = registry.get("slots") or {}

    def general_prod(stage: str):
        return ((slots.get(stage) or {}).get("general") or {}).get("production")

    errors = []
    for stage in prompt_registry.STAGES:
        prod = general_prod(stage)
        if not prod:
            errors.append(f"{stage}: no production slot assigned")
            continue
        if prompt_registry.get_version(prod, source_path) is None:
            errors.append(f"{stage}: production version {prod!r} does not exist")
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2

    old = version_map(load_bundle())
    new = version_map(registry)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])

    print(f"source: {source_path}")
    print(f"bundle: {BUNDLE_PATH}")
    print(f"registry_sha: {registry.get('registry_sha', '')}")
    for stage in prompt_registry.STAGES:
        prod = general_prod(stage)
        print(f"  {stage}.production = {prod}")
        for model, model_slots in (((slots.get(stage) or {}).get("models")) or {}).items():
            if (model_slots or {}).get("production"):
                print(f"  {stage}.{model}.production = {model_slots.get('production')}")
    for label, ids in (("added", added), ("removed", removed), ("changed", changed)):
        for vid in ids:
            print(f"  {label}: {vid} {old.get(vid, '')} -> {new.get(vid, '')}")

    if args.stage and args.stage not in prompt_registry.STAGES:
        print(f"ERROR: unknown --stage {args.stage!r}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("dry-run: bundle not written")
        return 0

    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    BUNDLE_PATH.write_text(payload, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH} ({len(registry.get('versions') or [])} version(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
