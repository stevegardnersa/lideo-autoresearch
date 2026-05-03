#!/usr/bin/env python3
"""
Regenerate candidate_spec.py Profile literal + constants from data/candidates.json.
Run this whenever a new profile is added via add_candidate.py.

Usage:
    python tools/gen_profile_literal.py
"""

import json
import re
import sys
from pathlib import Path

CANDIDATES_JSON = Path("data/candidates.json")
CANDIDATE_SPEC_PY = Path("candidate_spec.py")


def load_profile_keys() -> list[str]:
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    profiles = data.get("profiles", {})
    return sorted(profiles.keys())


def build_literal(values: list[str]) -> str:
    quoted = [f'"{v}"' for v in values]
    # Wrap at ~120 chars per line
    lines, line = [], ""
    for q in quoted:
        if line and len(line) + len(q) + 2 > 120:
            lines.append(line)
            line = ""
        line += (", " if line else "") + q
    if line:
        lines.append(line)
    return "Profile = Literal[\n    " + ",\n    ".join(lines) + "\n]\n"


def update_candidate_spec_py(profile_keys: list[str]) -> None:
    content = CANDIDATE_SPEC_PY.read_text(encoding="utf-8")
    literal = build_literal(profile_keys)

    # Replace existing Profile literal
    pattern = r'(Profile = Literal\[[^\]]+\]\n)'
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, literal, content, count=1, flags=re.DOTALL)
    else:
        # Insert after the first import / type definition block
        # Just append after any existing Literal definition
        content = re.sub(r'Profile = Literal\[.*?\]\n', literal, content, count=1, flags=re.DOTALL)

    CANDIDATE_SPEC_PY.write_text(content, encoding="utf-8")
    print(f"Updated Profile literal in {CANDIDATE_SPEC_PY} ({len(profile_keys)} profiles)")


def main() -> None:
    profile_keys = load_profile_keys()
    update_candidate_spec_py(profile_keys)
    print("Done.")


if __name__ == "__main__":
    main()
