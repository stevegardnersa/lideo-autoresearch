"""Resolve curated base chapter prompts for tuning runs.

A *base prompt* is an ordinary chapter prompt-registry version whose label is
``base: <slug>`` (see ``specs/base-prompts-and-model-strategy-tuning.md``).
Tuning should start from a base rather than the profile's shared default so the
search space is a curated set of movable parts.

Resolution order for a profile:

1. explicit ``slug`` argument (``--base``); ``none``/``-`` disables base seeding
2. the winner recorded by a base sweep artifact for that profile
3. ``DEFAULT_BASE_SLUG``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]

BASE_ORDER: List[str] = ["sentence", "readable", "readable-low"]
DEFAULT_BASE_SLUG = "readable"
DISABLE_TOKENS = {"none", "-", "off", "default"}
SWEEP_DIR = ROOT / "artifacts" / "autoresearch" / "base_sweep"


def label_matches(label: Any, slug: str) -> bool:
    """True when ``label`` is ``base: <slug>`` or ``base: <slug> <suffix>``.

    The trailing-separator check keeps ``readable`` from matching
    ``base: readable-low``.
    """
    text = str(label or "").strip()
    prefix = f"base: {slug}"
    return text == prefix or text.startswith(prefix + " ")


def load_bases() -> Dict[str, Dict[str, Any]]:
    """Return ``{slug: {slug, version_id, label, components}}`` for resolvable bases."""
    from core import prompt_registry as pr

    versions = [
        v for v in pr.list_versions("chapter")
        if pr.version_scope(v)["kind"] == "general"
    ]
    bases: Dict[str, Dict[str, Any]] = {}
    for slug in BASE_ORDER:
        hits = [v for v in versions if label_matches(v.get("label"), slug)]
        if len(hits) != 1:
            continue
        version = hits[0]
        bases[slug] = {
            "slug": slug,
            "version_id": version.get("id", ""),
            "label": version.get("label", ""),
            "components": dict(version.get("components") or {}),
        }
    return bases


def sweep_winner(profile: str, bench: Optional[str] = None) -> Optional[str]:
    """Read the base-sweep artifact for ``profile`` and return its winner slug."""
    if not SWEEP_DIR.exists():
        return None
    candidates = sorted(SWEEP_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("profile") or "") != profile:
            continue
        if bench and str(data.get("bench") or "") and str(data.get("bench")) != bench:
            continue
        winner = str(data.get("winner") or "").strip()
        if winner:
            return winner
    return None


def resolve_base(profile: str, slug: Optional[str] = None, bench: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve the base prompt for ``profile``.

    Returns ``{slug, version_id, label, components, source}`` or ``None`` when
    base seeding is disabled (``--base none``) or no base can be resolved.
    """
    requested = (slug or "").strip()
    if requested.lower() in DISABLE_TOKENS:
        return None

    bases = load_bases()
    if not bases:
        return None

    source = "explicit"
    chosen = requested
    if not chosen:
        chosen = sweep_winner(profile, bench) or ""
        source = "sweep" if chosen else "default"
    if not chosen:
        chosen = DEFAULT_BASE_SLUG
        source = "default"

    base = bases.get(chosen)
    if base is None:
        raise ValueError(
            f"unknown base {chosen!r}; known: {', '.join(sorted(bases))} (or 'none' to disable)"
        )
    resolved = dict(base)
    resolved["source"] = source
    return resolved


def seed_components(base_spec: Dict[str, Any], resolved: Optional[Dict[str, Any]], stage: str = "chapter") -> Dict[str, str]:
    """Overlay a resolved base's components onto ``base_spec``'s stage.

    Returns the seeded component dict (empty when ``resolved`` is None).
    """
    if not resolved:
        return {}
    stage_obj = base_spec.get(f"{stage}_stage")
    if not isinstance(stage_obj, dict):
        stage_obj = {}
        base_spec[f"{stage}_stage"] = stage_obj
    components = dict(stage_obj.get("prompt_components") or {})
    components.update(resolved.get("components") or {})
    stage_obj["prompt_components"] = components
    return components
