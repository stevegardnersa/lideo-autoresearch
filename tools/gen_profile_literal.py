#!/usr/bin/env python3
"""
Regenerate candidate_spec.py Profile literal + PROFILE_CANDIDATES from data/candidates.json.
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


def load_profiles() -> dict:
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("profiles", {})


def build_literal(profile_keys: list[str]) -> str:
    quoted = [f'"{v}"' for v in profile_keys]
    lines, line = [], ""
    for q in quoted:
        if line and len(line) + len(q) + 2 > 120:
            lines.append(line)
            line = ""
        line += (", " if line else "") + q
    if line:
        lines.append(line)
    return "Profile = Literal[\n    " + ",\n    ".join(lines) + "\n]\n"


def build_candidates_dict(profiles: dict) -> str:
    lines = []
    sorted_keys = sorted(profiles.keys())
    for key in sorted_keys:
        spec = profiles[key]
        name = spec.get("name", key)
        ch = spec.get("chapter_stage", {})
        composer = spec.get("composer_stage", {})
        lc = spec.get("length_control", {})
        ba = spec.get("budget_allocator", {})

        def stage_str(s):
            provider_val = s.get("provider")
            if provider_val is None:
                provider_repr = "None"
            else:
                provider_repr = json.dumps(provider_val)
            parts = [
                f'model="{s.get("model", "")}"',
                f'temperature={s.get("temperature", 0.2)}',
                f'seed={s.get("seed", 42)}',
                f'max_tokens={s.get("max_tokens", 8192)}',
                f'format_mode="{s.get("format_mode", "markdown_sections")}"',
                f'context_mode="{s.get("context_mode", "chapter_plus_toc_and_meta")}"',
                f'prompt_components={json.dumps(s.get("prompt_components", {}))}',
                f'provider={provider_repr}',
            ]
            if s.get("reasoning") is not None:
                parts.append(f'reasoning={json.dumps(s.get("reasoning"))}')
            if s.get("reasoning_effort") is not None:
                parts.append(f'reasoning_effort={json.dumps(s.get("reasoning_effort"))}')
            extra_body_val = s.get("extra_body")
            extra_body_repr = "None" if extra_body_val is None else json.dumps(extra_body_val)
            parts.append(f'extra_body={extra_body_repr}')
            return f"StageConfig({', '.join(parts)})"

        last = key == sorted_keys[-1]
        comma = "" if last else ","

        lines.append(f'    "{key}": CandidateSpec(')
        lines.append(f'        name="{name}",')
        lines.append(f'        profile="{key}",')
        lines.append(f'        chapter_stage={stage_str(ch)},')
        lines.append(f'        composer_stage={stage_str(composer)},')
        lines.append(f'        length_control=LengthControlConfig(')
        lines.append(f'            max_passes={lc.get("max_passes", 5)}, '
                     f'tolerance_pct={lc.get("tolerance_pct", 0.05)}, '
                     f'hard_tolerance_pct={lc.get("hard_tolerance_pct", 0.10)}, '
                     f'repair_strategy="{lc.get("repair_strategy", "edit_existing")}"')
        lines.append(f'        ),')
        lines.append(f'        budget_allocator=BudgetAllocatorConfig(')
        lines.append(f'            words_per_minute={ba.get("words_per_minute", 200)}, '
                     f'allocation_alpha={ba.get("allocation_alpha", 0.90)}, '
                     f'min_chapter_share={ba.get("min_chapter_share", 0.03)}, '
                     f'max_chapter_share={ba.get("max_chapter_share", 0.18)}, '
                     f'chapter_stage_multiplier_30m={ba.get("chapter_stage_multiplier_30m", 1.20)}, '
                     f'chapter_stage_multiplier_60m={ba.get("chapter_stage_multiplier_60m", 1.00)}, '
                     f'max_summary_to_source_ratio={ba.get("max_summary_to_source_ratio", 0.90)}')
        lines.append(f'        ),')
        lines.append(f'        use_json_schema={spec.get("use_json_schema", True)},')
        lines.append(f'        json_schema_name="{spec.get("json_schema_name", "summary_response")}",')
        lines.append(f'        notes="{spec.get("notes", "")}",')
        lines.append(f'        disable_composer={spec.get("disable_composer", False)}')
        lines.append(f'    ){comma}')

    return "\n".join(lines) + "\n"


def find_matching_brace(content: str, start: int) -> int:
    """Find the closing brace that matches the opening brace at position start-1."""
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i - 1


def update_candidate_spec_py(profiles: dict) -> None:
    content = CANDIDATE_SPEC_PY.read_text(encoding="utf-8")
    profile_keys = sorted(profiles.keys())
    literal = build_literal(profile_keys)
    candidates_dict = build_candidates_dict(profiles)

    # Replace Profile literal
    pattern = r"Profile = Literal\[[^\]]+\]\n"
    if re.search(pattern, content):
        content = re.sub(pattern, literal, content, count=1)
    else:
        print("WARNING: Profile literal pattern not found")

    # Replace PROFILE_CANDIDATES dict using brace matching
    marker = "PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {"
    if marker not in content:
        print("WARNING: PROFILE_CANDIDATES marker not found")
    else:
        marker_end = content.index(marker) + len(marker)
        # Check if empty dict {}
        if content[marker_end] == "}":
            # Empty dict: content[:marker_end] keeps the { from the marker
            # content[marker_end+1:] skips the } - but we need to add closing }
            new_dict = f"\n{candidates_dict}\n}}"
            content = content[:marker_end] + new_dict + content[marker_end + 1:]
        else:
            # Non-empty dict - find matching close brace and replace
            close_brace = find_matching_brace(content, marker_end)
            new_dict = f"\n{candidates_dict}\n}}"
            content = content[:marker_end] + new_dict + content[close_brace + 1:]

    CANDIDATE_SPEC_PY.write_text(content, encoding="utf-8")
    print(f"Updated Profile literal in {CANDIDATE_SPEC_PY} ({len(profile_keys)} profiles)")
    print(f"Updated PROFILE_CANDIDATES in {CANDIDATE_SPEC_PY} ({len(profile_keys)} candidates)")


def main() -> None:
    if not CANDIDATES_JSON.exists():
        print(f"No profiles found in data/candidates.json (file does not exist)")
        sys.exit(1)
    profiles = load_profiles()
    if not profiles:
        print("No profiles found in data/candidates.json (empty)")
        sys.exit(1)
    update_candidate_spec_py(profiles)
    print("Done.")


if __name__ == "__main__":
    main()