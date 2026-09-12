"""Prompt-version registry for the book-summary pipeline.

Pure stdlib file IO (no network). See
``specs/system-prompt-v2-versioned-routing.md`` §5.

A *version* is an immutable, fingerprintable system-prompt definition for one
stage (``chapter`` or ``composer``). Versions may carry a partial ``components``
overlay (applied on top of a profile's component set) and a self-contained
``template`` (the full rendered system prompt) so the Cloud Function can resolve
a version without the local profile.

Two assignable *slots* per stage: ``local`` (direct/local runs) and
``production`` (what the Cloud Function serves by default).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = ROOT / "data" / "prompts" / "registry.json"
SCHEMA_VERSION = 1
STAGES: Tuple[str, ...] = ("chapter", "composer")
SLOTS: Tuple[str, ...] = ("local", "production")

# Representative reference profile used to compute a version's standalone
# template/fingerprint. Any profile with both stages works; the value only
# affects the stored template, never local rendering (which uses the real
# profile's fallback components).
_REFERENCE_PROFILE = "30m_gpt-5.6-luna_effort-minimal"


class PromptRegistryError(RuntimeError):
    """Base class for registry errors."""

    status_code = 400
    error = "prompt_registry_error"


class UnknownVersionError(PromptRegistryError):
    status_code = 404
    error = "unknown_prompt_version"

    def __init__(self, version_id: str, allowed: Optional[List[str]] = None):
        self.version_id = version_id
        self.allowed = allowed or []
        super().__init__(f"Unknown prompt version: {version_id!r}")


class VersionInUseError(PromptRegistryError):
    status_code = 409
    error = "version_in_use"


class RegistryConflictError(PromptRegistryError):
    status_code = 409
    error = "registry_changed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def fingerprint(text: str) -> str:
    """Return the 16-hex-char content fingerprint of a system prompt."""
    return sha256_text(text)[:16]


def registry_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("PROMPT_REGISTRY_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else ROOT / p
    return DEFAULT_REGISTRY_PATH


def _empty_registry() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_sha": "",
        "updated_at": "",
        "versions": [],
        "slots": {stage: {"local": None, "production": None} for stage in STAGES},
    }


def compute_registry_sha(registry: Mapping[str, Any]) -> str:
    payload = {
        "versions": list(registry.get("versions") or []),
        "slots": registry.get("slots") or {},
    }
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _normalize(data: Mapping[str, Any]) -> Dict[str, Any]:
    registry = _empty_registry()
    registry["schema_version"] = int(data.get("schema_version") or SCHEMA_VERSION)
    versions = data.get("versions")
    if isinstance(versions, list):
        registry["versions"] = [dict(v) for v in versions if isinstance(v, Mapping)]
    slots = data.get("slots")
    if isinstance(slots, Mapping):
        for stage in STAGES:
            stage_slots = slots.get(stage)
            if isinstance(stage_slots, Mapping):
                for slot in SLOTS:
                    value = stage_slots.get(slot)
                    registry["slots"][stage][slot] = str(value) if value else None
    registry["registry_sha"] = str(data.get("registry_sha") or compute_registry_sha(registry))
    registry["updated_at"] = str(data.get("updated_at") or "")
    return registry


def load_registry(path: Optional[Path] = None, *, seed: bool = True) -> Dict[str, Any]:
    """Load the registry, seeding a fresh one when absent.

    A corrupt/unreadable file yields an empty structure (with a warning) and is
    never overwritten. Mirrors the permutation-store failure mode.
    """
    p = registry_path(path)
    if not p.exists():
        if not seed:
            return _empty_registry()
        registry = seed_registry()
        try:
            save_registry(registry, p)
        except OSError:
            pass
        return registry
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or "versions" not in raw:
            raise ValueError("missing 'versions'")
        return _normalize(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        import sys

        print(f"WARN: unreadable prompt registry at {p}: {exc}", file=sys.stderr)
        return _empty_registry()


def save_registry(registry: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = registry_path(path)
    registry = dict(registry)
    registry["schema_version"] = SCHEMA_VERSION
    registry["updated_at"] = _now_iso()
    registry["registry_sha"] = compute_registry_sha(registry)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".registry-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, p)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def list_versions(stage: Optional[str] = None, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    registry = load_registry(path)
    versions = list(registry.get("versions") or [])
    if stage:
        versions = [v for v in versions if v.get("stage") == stage]
    return versions


def get_version(version_id: str, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    if not version_id:
        return None
    for version in load_registry(path).get("versions") or []:
        if version.get("id") == version_id:
            return version
    return None


def get_slot(*, stage: str, slot: str, path: Optional[Path] = None) -> Optional[str]:
    registry = load_registry(path)
    return ((registry.get("slots") or {}).get(stage) or {}).get(slot)


def require_version(version_id: str, *, stage: Optional[str] = None, path: Optional[Path] = None) -> Dict[str, Any]:
    version = get_version(version_id, path)
    if version is None or (stage and version.get("stage") != stage):
        allowed = [v["id"] for v in list_versions(stage, path)]
        raise UnknownVersionError(version_id, allowed)
    return version


def _reference_profile(candidate_module) -> str:
    profiles = list(getattr(candidate_module, "PROFILE_CANDIDATES", {}).keys())
    if _REFERENCE_PROFILE in profiles:
        return _REFERENCE_PROFILE
    return profiles[0] if profiles else _REFERENCE_PROFILE


def _render_template(stage: str, components: Mapping[str, str]) -> str:
    """Render a standalone system prompt for a stage/components pair."""
    try:
        import candidate_spec as candidate_module  # local import avoids cycle
    except Exception:  # pragma: no cover - hosts without the module
        return ""
    profile = _reference_profile(candidate_module)
    base = candidate_module.PROFILE_CANDIDATES.get(profile)
    if base is None:
        return ""
    spec = copy.deepcopy(base)
    stage_obj = getattr(spec, f"{stage}_stage", None)
    if stage_obj is None:
        return ""
    stage_obj.prompt_components = {**(stage_obj.prompt_components or {}), **dict(components)}
    render = getattr(candidate_module, f"render_{stage}_system", None)
    if render is None:
        return ""
    return render(spec)


def _next_version_id(stage: str, registry: Mapping[str, Any]) -> str:
    used = set()
    for version in registry.get("versions") or []:
        vid = str(version.get("id") or "")
        if vid.startswith(f"{stage}-p"):
            try:
                used.add(int(vid.split("-p", 1)[1]))
            except ValueError:
                continue
    n = 1
    while n in used:
        n += 1
    return f"{stage}-p{n}"


def _version_record(*, stage: str, version_id: str, label: str, components: Mapping[str, str],
                    template: Optional[str], notes: str, created_by: str) -> Dict[str, Any]:
    components = {k: str(v) for k, v in dict(components).items() if str(v).strip()}
    if template is None:
        template = _render_template(stage, components)
    template = template or ""
    return {
        "id": version_id,
        "stage": stage,
        "label": label,
        "components": components,
        "template": template,
        "system_prompt_sha256": sha256_text(template) if template else "",
        "system_prompt_fingerprint": fingerprint(template) if template else "",
        "created_at": _now_iso(),
        "created_by": created_by,
        "notes": notes,
        "immutable": True,
    }


def create_version(
    *,
    stage: str,
    label: str,
    components: Mapping[str, str],
    template: Optional[str] = None,
    notes: str = "",
    created_by: str = "operator",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    if stage not in STAGES:
        raise PromptRegistryError(f"Unknown stage: {stage!r}")
    registry = load_registry(path)
    version_id = _next_version_id(stage, registry)
    version = _version_record(
        stage=stage,
        version_id=version_id,
        label=label or version_id,
        components=components,
        template=template,
        notes=notes,
        created_by=created_by,
    )
    registry.setdefault("versions", []).append(version)
    save_registry(registry, path)
    return version


def update_version(
    version_id: str,
    *,
    label: Optional[str] = None,
    notes: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    registry = load_registry(path)
    for version in registry.get("versions") or []:
        if version.get("id") == version_id:
            if label is not None:
                version["label"] = str(label)
            if notes is not None:
                version["notes"] = str(notes)
            save_registry(registry, path)
            return dict(version)
    raise UnknownVersionError(version_id)


def delete_version(version_id: str, *, path: Optional[Path] = None) -> None:
    registry = load_registry(path)
    for stage, slots in (registry.get("slots") or {}).items():
        for slot, assigned in (slots or {}).items():
            if assigned == version_id:
                raise VersionInUseError(
                    f"Version {version_id!r} is assigned to {stage}.{slot}; reassign it before deleting."
                )
    versions = registry.get("versions") or []
    remaining = [v for v in versions if v.get("id") != version_id]
    if len(remaining) == len(versions):
        raise UnknownVersionError(version_id)
    registry["versions"] = remaining
    save_registry(registry, path)


def set_slot(
    *,
    stage: str,
    slot: str,
    version_id: str,
    expected_sha: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    if stage not in STAGES:
        raise PromptRegistryError(f"Unknown stage: {stage!r}")
    if slot not in SLOTS:
        raise PromptRegistryError(f"Unknown slot: {slot!r}")
    registry = load_registry(path)
    if expected_sha and expected_sha != registry.get("registry_sha"):
        raise RegistryConflictError("Registry changed since it was loaded; reload and retry.")
    if version_id:
        require_version(version_id, stage=stage, path=path)
        registry.setdefault("slots", {}).setdefault(stage, {})[slot] = version_id
    else:
        registry.setdefault("slots", {}).setdefault(stage, {})[slot] = None
    save_registry(registry, path)
    return registry["slots"]


def seed_registry() -> Dict[str, Any]:
    """Seed the baseline versions. Slots point at the v1 baseline (no v2 promotion)."""
    registry = _empty_registry()
    p1 = _version_record(
        stage="chapter", version_id="chapter-p1", label="v1 baseline (profile defaults)",
        components={}, template=None, notes="Baseline: profile components, no length policy.",
        created_by="seed",
    )
    p2 = _version_record(
        stage="chapter", version_id="chapter-p2", label="v2 target-aware (default)",
        components={"length_policy": "explicit_budget_first"}, template=None,
        notes="Consolidates target-aware length control; static rules only.",
        created_by="seed",
    )
    cp1 = _version_record(
        stage="composer", version_id="composer-p1", label="v1 baseline (profile defaults)",
        components={}, template=None, notes="Baseline composer components.",
        created_by="seed",
    )
    registry["versions"] = [p1, p2, cp1]
    registry["slots"] = {
        "chapter": {"local": "chapter-p1", "production": "chapter-p1"},
        "composer": {"local": "composer-p1", "production": "composer-p1"},
    }
    registry["updated_at"] = _now_iso()
    registry["registry_sha"] = compute_registry_sha(registry)
    return registry


# ── Resolution / rendering ────────────────────────────────────────────────


def resolve_version_id(
    *,
    stage: str,
    prompt_version: Optional[str] = None,
    slot: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[str]:
    """Explicit version wins; else the slot pointer; else None (legacy baseline)."""
    if prompt_version:
        require_version(prompt_version, stage=stage, path=path)
        return prompt_version
    if slot:
        return get_slot(stage=stage, slot=slot, path=path)
    return None


def resolve_components(
    *,
    stage: str,
    profile_components: Mapping[str, str],
    prompt_version: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, str]:
    """Overlay a version's components on the profile components.

    Missing version keys fall back to the profile; an unknown explicit version
    raises before any request is made.
    """
    components = dict(profile_components or {})
    if not prompt_version:
        return components
    version = require_version(prompt_version, stage=stage, path=path)
    components.update({k: str(v) for k, v in (version.get("components") or {}).items()})
    return components


def apply_overlay(spec, *, stage_kind: str, prompt_version: Optional[str], path: Optional[Path] = None) -> Dict[str, str]:
    """Mutate ``spec``'s stage components with the resolved overlay. Returns them."""
    stage_obj = getattr(spec, f"{stage_kind}_stage", None)
    if stage_obj is None:
        return {}
    components = resolve_components(
        stage=stage_kind,
        profile_components=stage_obj.prompt_components or {},
        prompt_version=prompt_version,
        path=path,
    )
    stage_obj.prompt_components = components
    return components


def render_system(
    *,
    spec,
    stage_kind: str,
    prompt_version: Optional[str] = None,
    candidate_module=None,
    path: Optional[Path] = None,
) -> Tuple[str, str]:
    """Render a stage system prompt with the version overlay. Returns (text, fingerprint)."""
    if candidate_module is None:
        import candidate_spec as candidate_module  # type: ignore
    stage_obj = getattr(spec, f"{stage_kind}_stage")
    components = resolve_components(
        stage=stage_kind,
        profile_components=stage_obj.prompt_components or {},
        prompt_version=prompt_version,
        path=path,
    )
    local_spec = copy.copy(spec)
    setattr(local_spec, f"{stage_kind}_stage", dataclasses.replace(stage_obj, prompt_components=components))
    render = getattr(candidate_module, f"render_{stage_kind}_system")
    text = render(local_spec)
    return text, fingerprint(text)


def version_text(version: Mapping[str, Any], *, path: Optional[Path] = None) -> Tuple[str, str]:
    """Return the self-contained system-prompt text + fingerprint for a version."""
    template = str(version.get("template") or "")
    if not template:
        template = _render_template(str(version.get("stage") or "chapter"), version.get("components") or {})
    return template, fingerprint(template) if template else ""


def cf_report_path() -> Path:
    return registry_path().parent / "cf_last_report.json"


def read_cf_report() -> Dict[str, Any]:
    p = cf_report_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_cf_report(report: Dict[str, Any]) -> None:
    p = cf_report_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload.setdefault("reported_at", _now_iso())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
