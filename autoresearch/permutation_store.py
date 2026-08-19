"""Per-permutation optimized prompt storage — never mutates candidate_spec.py.

Every permutation (fixed model × budget × thinking × time) gets one JSON
file in ``data/optimized_prompts/``. The file records the full history of
evaluated variants with scores, and a ``current_best`` pointer per stage.

``candidate_spec.py``'s ``get_candidate()`` calls this module to apply
overrides for profiles that are not in the v1 static registry (i.e. v2+).

**Lifecycle:** Files auto-clobber old permutations after new best is
found — history entries accumulate, but only the best version's components
are applied at runtime.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

PROMO_DIR = os.path.join(os.getcwd(), "data", "optimized_prompts")


# ── File I/O ────────────────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(PROMO_DIR, exist_ok=True)


def _perm_path(permutation_key: str) -> str:
    return os.path.join(PROMO_DIR, f"{permutation_key}.json")


def extract_permutation_key(candidate_name: str) -> str:
    """Strip version suffix, e.g. '30m_dsv4-flash_notthinking_v3' -> '30m_dsv4-flash_notthinking'."""
    m = re.match(r"^(.+)_v(\d+)$", candidate_name)
    if m:
        return m.group(1)
    return candidate_name


def load_permutation(permutation_key: str) -> dict:
    """Read permutation JSON file, return empty dict if not found."""
    _ensure_dir()
    path = _perm_path(permutation_key)
    if not os.path.exists(path):
        return {
            "permutation_key": permutation_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chapter": {"current_best_version": 0, "history": []},
            "composer": {"current_best_version": 0, "history": []},
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {
            "permutation_key": permutation_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chapter": {"current_best_version": 0, "history": []},
            "composer": {"current_best_version": 0, "history": []},
        }


def save_permutation(permutation_key: str, data: dict):
    """Atomic write: write to temp file, rename."""
    _ensure_dir()
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = _perm_path(permutation_key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_next_version(permutation_key: str, stage: str = "chapter") -> int:
    """Return the next version number for the given stage."""
    data = load_permutation(permutation_key)
    stage_data = data.get(stage, {})
    history = stage_data.get("history", [])
    return len(history) + 1


def add_history_entry(
    permutation_key: str,
    stage: str,
    profile: str,
    components: Dict[str, str],
    changed_dimensions: List[str],
    composite: float,
    quality: float,
    faithfulness: float,
    concept_coverage: float,
    pass_rate: float,
    cost: float,
    samples_scored: int,
    run_id: str = "",
) -> int:
    """Append a history entry to the permutation file. Returns the version number."""
    data = load_permutation(permutation_key)
    stage_data = data.setdefault(stage, {"current_best_version": 0, "history": []})

    version = len(stage_data["history"]) + 1

    entry = {
        "version": version,
        "profile": profile,
        "components": dict(sorted(components.items())),
        "changed_dimensions": list(changed_dimensions),
        "composite": round(composite, 6),
        "quality": round(quality, 4),
        "faithfulness": round(faithfulness, 4),
        "concept_coverage": round(concept_coverage, 4),
        "pass_rate": round(pass_rate, 4),
        "cost": round(cost, 6),
        "samples_scored": samples_scored,
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    stage_data["history"].append(entry)
    save_permutation(permutation_key, data)
    return version


def set_current_best(permutation_key: str, stage: str, version: int):
    """Mark a specific version as the current best for the stage."""
    data = load_permutation(permutation_key)
    stage_data = data.setdefault(stage, {"current_best_version": 0, "history": []})
    stage_data["current_best_version"] = version
    save_permutation(permutation_key, data)


def get_current_best_components(permutation_key: str, stage: str) -> Optional[Dict[str, str]]:
    """Return the prompt_components dict for the current best version, or None."""
    data = load_permutation(permutation_key)
    stage_data = data.get(stage, {})
    best_version = stage_data.get("current_best_version", 0)
    if best_version == 0:
        return None
    history = stage_data.get("history", [])
    for entry in history:
        if entry.get("version") == best_version:
            return dict(entry.get("components", {}))
    return None


def get_prompt_override(profile: str, stage: str) -> Optional[Dict[str, str]]:
    """Resolve the prompt component override for a given profile and stage.

    Called by candidate_spec.get_candidate() when the profile key is not
    in the static PROFILE_CANDIDATES dict (i.e., v2+ variants).
    """
    perm_key = extract_permutation_key(profile)
    data = load_permutation(perm_key)
    stage_data = data.get(stage, {})
    history = stage_data.get("history", [])

    # Try exact profile match first
    for entry in reversed(history):
        if entry.get("profile") == profile:
            return dict(entry.get("components", {}))

    # Fall back to current_best
    return get_current_best_components(perm_key, stage)


def resolve_variant(profile: str):
    """Build a CandidateSpec for a variant from permutation history + v1 baseline.

    This is used when get_candidate() receives a profile key not in
    PROFILE_CANDIDATES (e.g., '30m_dsv4_flash_notthinking_v3').

    It takes the v1 baseline CandidateSpec and overlays the best
    prompt components from the permutation history.

    Returns None if neither the v1 baseline nor permutation data exists.
    """
    from candidate_spec import PROFILE_CANDIDATES
    import copy

    perm_key = extract_permutation_key(profile)

    # Find v1 baseline
    v1_profile = f"{perm_key}_v1"
    base_spec = None
    if v1_profile in PROFILE_CANDIDATES:
        base_spec = copy.deepcopy(PROFILE_CANDIDATES[v1_profile])
    elif perm_key in PROFILE_CANDIDATES:
        base_spec = copy.deepcopy(PROFILE_CANDIDATES[perm_key])

    if base_spec is None:
        return None

    # Apply prompt component overrides from permutation history
    # Check the specific version in the profile, or use current_best
    m = re.match(r"^(.+)_v(\d+)$", profile)
    target_version = int(m.group(2)) if m else None

    data = load_permutation(perm_key)
    for stage_name, attr_name in [("chapter", "chapter_stage"), ("composer", "composer_stage")]:
        stage_data = data.get(stage_name, {})
        history = stage_data.get("history", [])
        best_entry = None

        if target_version is not None:
            for entry in history:
                if entry.get("version") == target_version:
                    best_entry = entry
                    break
        else:
            # Use current best
            best_version = stage_data.get("current_best_version", 0)
            for entry in history:
                if entry.get("version") == best_version:
                    best_entry = entry
                    break

        if best_entry:
            stage = getattr(base_spec, attr_name)
            stage.prompt_components = dict(best_entry.get("components", {}))

    return base_spec


def list_all_permutations() -> List[dict]:
    """Return summary of all permutation files."""
    _ensure_dir()
    results: List[dict] = []
    try:
        for fname in sorted(os.listdir(PROMO_DIR)):
            if fname.endswith(".json") and not fname.endswith(".tmp"):
                path = os.path.join(PROMO_DIR, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    key = fname[:-5]
                    chapter_best = data.get("chapter", {}).get("current_best_version", 0)
                    composer_best = data.get("composer", {}).get("current_best_version", 0)
                    n_history = len(data.get("chapter", {}).get("history", []))
                    results.append({
                        "key": key,
                        "chapter_best_version": chapter_best,
                        "composer_best_version": composer_best,
                        "total_chapter_history": n_history,
                        "total_composer_history": len(data.get("composer", {}).get("history", [])),
                        "updated_at": data.get("updated_at", ""),
                    })
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        pass
    return sorted(results, key=lambda r: r.get("updated_at", ""), reverse=True)
