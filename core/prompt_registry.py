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
SCOPE_KINDS: Tuple[str, ...] = ("general", "model")

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


class ScopeMismatchError(PromptRegistryError):
    status_code = 400
    error = "scope_mismatch"


class UnknownModelError(PromptRegistryError):
    status_code = 400
    error = "unknown_model"


class UnknownProfileError(PromptRegistryError):
    status_code = 400
    error = "unknown_profile"


class InvalidScopeError(PromptRegistryError):
    status_code = 400
    error = "invalid_scope"


class InvalidTemplateError(PromptRegistryError):
    status_code = 400
    error = "invalid_template"


def normalize_scope(raw: Any) -> Dict[str, Any]:
    """Normalize a version scope to ``{kind, model?, profiles?}``."""
    if not isinstance(raw, Mapping):
        return {"kind": "general"}
    kind = str(raw.get("kind") or "general").strip().lower()
    if kind not in SCOPE_KINDS:
        raise InvalidScopeError(f"Unknown scope kind: {kind!r}")
    if kind == "general":
        return {"kind": "general"}
    model = str(raw.get("model") or "").strip()
    if not model:
        raise InvalidScopeError("scope.model is required when scope.kind is 'model'")
    profiles_raw = raw.get("profiles") or []
    if isinstance(profiles_raw, str):
        profiles_raw = [profiles_raw]
    if not isinstance(profiles_raw, (list, tuple)):
        raise InvalidScopeError("scope.profiles must be a list")
    profiles = [str(p) for p in profiles_raw if str(p).strip()]
    scope: Dict[str, Any] = {"kind": "model", "model": model}
    if profiles:
        scope["profiles"] = profiles
    return scope


def version_scope(version: Mapping[str, Any]) -> Dict[str, Any]:
    return normalize_scope(version.get("scope"))


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


def _empty_stage_slots() -> Dict[str, Any]:
    return {"general": {slot: None for slot in SLOTS}, "models": {}}


def _empty_registry() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_sha": "",
        "updated_at": "",
        "versions": [],
        "slots": {stage: _empty_stage_slots() for stage in STAGES},
    }


def compute_registry_sha(registry: Mapping[str, Any]) -> str:
    payload = {
        "versions": list(registry.get("versions") or []),
        "slots": registry.get("slots") or {},
    }
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _clean_component_text(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}


def _normalize_version(raw: Mapping[str, Any]) -> Dict[str, Any]:
    version = dict(raw)
    try:
        version["scope"] = normalize_scope(raw.get("scope"))
    except InvalidScopeError:
        version["scope"] = {"kind": "general"}
    version["component_text"] = _clean_component_text(raw.get("component_text"))
    return version


def _normalize_stage_slots(raw: Any) -> Dict[str, Any]:
    result = _empty_stage_slots()
    if not isinstance(raw, Mapping):
        return result
    nested = "general" in raw or "models" in raw
    if not nested:
        result["general"] = {slot: (str(raw.get(slot)) if raw.get(slot) else None) for slot in SLOTS}
        return result
    general = raw.get("general")
    if isinstance(general, Mapping):
        result["general"] = {slot: (str(general.get(slot)) if general.get(slot) else None) for slot in SLOTS}
    models = raw.get("models")
    if isinstance(models, Mapping):
        for model, slots in models.items():
            if not model or not isinstance(slots, Mapping):
                continue
            result["models"][str(model)] = {
                slot: (str(slots.get(slot)) if slots.get(slot) else None) for slot in SLOTS
            }
    return result


def _normalize(data: Mapping[str, Any]) -> Dict[str, Any]:
    registry = _empty_registry()
    registry["schema_version"] = int(data.get("schema_version") or SCHEMA_VERSION)
    versions = data.get("versions")
    if isinstance(versions, list):
        registry["versions"] = [_normalize_version(v) for v in versions if isinstance(v, Mapping)]
    slots = data.get("slots")
    if isinstance(slots, Mapping):
        for stage in STAGES:
            registry["slots"][stage] = _normalize_stage_slots(slots.get(stage))
    registry["registry_sha"] = compute_registry_sha(registry)
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


def get_slot(*, stage: str, slot: str, model: Optional[str] = None, path: Optional[Path] = None) -> Optional[str]:
    registry = load_registry(path)
    stage_slots = (registry.get("slots") or {}).get(stage) or _empty_stage_slots()
    if model:
        return (stage_slots.get("models", {}).get(model) or {}).get(slot)
    return (stage_slots.get("general") or {}).get(slot)


def assignments_for(version_id: str, path: Optional[Path] = None) -> List[str]:
    """Return ``stage.model.slot`` references for a version (model '' = general)."""
    registry = load_registry(path)
    refs: List[str] = []
    for stage, stage_slots in (registry.get("slots") or {}).items():
        general = (stage_slots or {}).get("general") or {}
        for slot, assigned in general.items():
            if assigned == version_id:
                refs.append(f"{stage}.general.{slot}")
        for model, slots in ((stage_slots or {}).get("models") or {}).items():
            for slot, assigned in (slots or {}).items():
                if assigned == version_id:
                    refs.append(f"{stage}.{model}.{slot}")
    return refs


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


def _render_template(stage: str, components: Mapping[str, str],
                     component_text: Optional[Mapping[str, str]] = None,
                     system_template: Optional[str] = None) -> str:
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
    stage_obj.component_text = dict(component_text or {})
    stage_obj.system_template = system_template or None
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


def _clean_reasoning(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Normalize a baked ``reasoning`` directive to a plain dict (or {})."""
    if not isinstance(raw, Mapping) or not raw:
        return {}
    return {str(k): v for k, v in dict(raw).items()}


def _clean_mapping(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Normalize a baked mapping of JSON scalars (or {}), dropping blanks.

    Used for the length-control config and the repair templates so a version
    record can never carry a value the Cloud Function cannot render.
    """
    if not isinstance(raw, Mapping) or not raw:
        return {}
    out: Dict[str, Any] = {}
    for key, value in dict(raw).items():
        name = str(key)
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, (int, float)):
            out[name] = value
        elif isinstance(value, str):
            if value.strip():
                out[name] = value
    return out


def _version_record(*, stage: str, version_id: str, label: str, components: Mapping[str, str],
                    template: Optional[str], notes: str, created_by: str,
                    scope: Optional[Mapping[str, Any]] = None,
                    component_text: Optional[Mapping[str, str]] = None,
                    system_template: Optional[str] = None,
                    user_template: Optional[str] = None,
                    derived_from: Optional[str] = None,
                    model: Optional[str] = None,
                    reasoning: Optional[Mapping[str, Any]] = None,
                    length_control: Optional[Mapping[str, Any]] = None,
                    repair_templates: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    components = {k: str(v) for k, v in dict(components).items() if str(v).strip()}
    clean_component_text = _clean_component_text(component_text)
    system_tmpl = str(system_template or "")
    user_tmpl = str(user_template or "")
    if system_tmpl.strip():
        rendered = _render_template(stage, components, clean_component_text, system_tmpl)
    elif template is not None:
        rendered = template
    else:
        rendered = _render_template(stage, components, clean_component_text, None)
    rendered = rendered or ""
    record: Dict[str, Any] = {
        "id": version_id,
        "stage": stage,
        "label": label,
        "scope": normalize_scope(scope),
        "components": components,
        "component_text": clean_component_text,
        "system_template": system_tmpl,
        "user_template": user_tmpl,
        "template": rendered,
        "system_prompt_sha256": sha256_text(rendered) if rendered else "",
        "system_prompt_fingerprint": fingerprint(rendered) if rendered else "",
        "user_template_fingerprint": fingerprint(user_tmpl) if user_tmpl.strip() else "",
        "created_at": _now_iso(),
        "created_by": created_by,
        "notes": notes,
        "immutable": True,
    }
    baked_model = str(model or "").strip()
    if baked_model:
        record["model"] = baked_model
    baked_reasoning = _clean_reasoning(reasoning)
    if baked_reasoning:
        record["reasoning"] = baked_reasoning
    baked_length_control = _clean_mapping(length_control)
    if baked_length_control:
        record["length_control"] = baked_length_control
    baked_repair_templates = {
        str(k): str(v) for k, v in _clean_mapping(repair_templates).items()
    }
    if baked_repair_templates:
        record["repair_templates"] = baked_repair_templates
    if derived_from:
        record["derived_from"] = derived_from
    return record


def create_version(
    *,
    stage: str,
    label: str,
    components: Mapping[str, str],
    template: Optional[str] = None,
    notes: str = "",
    created_by: str = "operator",
    scope: Optional[Mapping[str, Any]] = None,
    component_text: Optional[Mapping[str, str]] = None,
    system_template: Optional[str] = None,
    user_template: Optional[str] = None,
    derived_from: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[Mapping[str, Any]] = None,
    length_control: Optional[Mapping[str, Any]] = None,
    repair_templates: Optional[Mapping[str, Any]] = None,
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
        scope=scope,
        component_text=component_text,
        system_template=system_template,
        user_template=user_template,
        derived_from=derived_from,
        model=model,
        reasoning=reasoning,
        length_control=length_control,
        repair_templates=repair_templates,
    )
    registry.setdefault("versions", []).append(version)
    save_registry(registry, path)
    return version


def revise_version(
    version_id: str,
    *,
    label: Optional[str] = None,
    components: Optional[Mapping[str, str]] = None,
    template: Optional[str] = None,
    notes: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    component_text: Optional[Mapping[str, str]] = None,
    system_template: Optional[str] = None,
    user_template: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[Mapping[str, Any]] = None,
    length_control: Optional[Mapping[str, Any]] = None,
    repair_templates: Optional[Mapping[str, Any]] = None,
    created_by: str = "operator",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Copy-on-write edit: create a new version derived from ``version_id``."""
    source = require_version(version_id, path=path)
    stage = str(source.get("stage") or "chapter")
    new_label = label if label is not None else f"{source.get('label') or version_id} (copy)"
    merged_components = dict(source.get("components") or {})
    if components is not None:
        merged_components.update({k: str(v) for k, v in dict(components).items()})
    merged_component_text = dict(source.get("component_text") or {})
    if component_text is not None:
        for key, value in dict(component_text).items():
            if isinstance(value, str) and value.strip():
                merged_component_text[str(key)] = value
            else:
                merged_component_text.pop(str(key), None)
    merged_scope = dict(source.get("scope") or {"kind": "general"})
    if scope is not None:
        merged_scope = dict(scope)
    system_tmpl = source.get("system_template") if system_template is None else system_template
    user_tmpl = source.get("user_template") if user_template is None else user_template
    effective_template = source.get("template") if template is None else template
    if components is not None or component_text is not None:
        # Re-render from the merged components unless the caller pinned a template.
        effective_template = template
    if system_tmpl and str(system_tmpl).strip():
        effective_template = None
    return create_version(
        stage=stage,
        label=new_label or version_id,
        components=merged_components,
        template=effective_template,
        notes=notes if notes is not None else str(source.get("notes") or ""),
        created_by=created_by,
        scope=merged_scope,
        component_text=merged_component_text,
        system_template=system_tmpl,
        user_template=user_tmpl,
        derived_from=version_id,
        path=path,
        model=model if model is not None else str(source.get("model") or ""),
        reasoning=reasoning if reasoning is not None else (source.get("reasoning") or None),
        length_control=length_control if length_control is not None else (source.get("length_control") or None),
        repair_templates=repair_templates if repair_templates is not None else (source.get("repair_templates") or None),
    )


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
    refs = assignments_for(version_id, path)
    if refs:
        raise VersionInUseError(
            f"Version {version_id!r} is assigned to {', '.join(refs)}; reassign it before deleting."
        )
    versions = registry.get("versions") or []
    remaining = [v for v in versions if v.get("id") != version_id]
    if len(remaining) == len(versions):
        raise UnknownVersionError(version_id)
    registry["versions"] = remaining
    save_registry(registry, path)


def _model_slot_target(registry: Dict[str, Any], stage: str, model: str) -> Dict[str, Any]:
    stage_slots = registry.setdefault("slots", {}).setdefault(stage, _empty_stage_slots())
    stage_slots.setdefault("general", {slot: None for slot in SLOTS})
    stage_slots.setdefault("models", {})
    return stage_slots["models"].setdefault(model, {slot: None for slot in SLOTS})


def set_slot(
    *,
    stage: str,
    slot: str,
    version_id: str,
    model: Optional[str] = None,
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
    version = None
    if version_id:
        version = require_version(version_id, stage=stage, path=path)
        if model:
            scope = version_scope(version)
            if scope["kind"] == "model" and scope.get("model") != model:
                raise ScopeMismatchError(
                    f"Version {version_id!r} is scoped to {scope.get('model')!r}, not {model!r}."
                )
        else:
            if version_scope(version)["kind"] != "general":
                raise ScopeMismatchError(
                    f"Model-scoped version {version_id!r} cannot be assigned to the general slot."
                )
    if model:
        target = _model_slot_target(registry, stage, model)
        target[slot] = version_id or None
    else:
        registry.setdefault("slots", {}).setdefault(stage, _empty_stage_slots())
        registry["slots"][stage].setdefault("general", {slot: None for slot in SLOTS})
        registry["slots"][stage]["general"][slot] = version_id or None
    save_registry(registry, path)
    return {"slots": registry["slots"], "registry_sha": compute_registry_sha(registry)}


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
        "chapter": {"general": {"local": "chapter-p1", "production": "chapter-p1"}, "models": {}},
        "composer": {"general": {"local": "composer-p1", "production": "composer-p1"}, "models": {}},
    }
    registry["updated_at"] = _now_iso()
    registry["registry_sha"] = compute_registry_sha(registry)
    return registry


# ── Resolution / rendering ────────────────────────────────────────────────


def _scoped_versions(versions: List[Mapping[str, Any]], stage: str, model: str,
                     profile: Optional[str]) -> Optional[Mapping[str, Any]]:
    candidates = [
        v for v in versions
        if v.get("stage") == stage
        and version_scope(v)["kind"] == "model"
        and version_scope(v).get("model") == model
    ]
    if not candidates:
        return None
    if profile:
        profiled = [v for v in candidates if profile in (version_scope(v).get("profiles") or [])]
        if profiled:
            return profiled[-1]
    return None


def resolve_version(
    *,
    stage: str,
    prompt_version: Optional[str] = None,
    slot: Optional[str] = None,
    model: Optional[str] = None,
    profile: Optional[str] = None,
    path: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """Resolve a prompt version id + source label (spec §6.3).

    Returns ``(version_id, source)`` where source is one of ``override``,
    ``registry_model``, ``registry_general`` or ``baseline``.
    """
    if prompt_version:
        require_version(prompt_version, stage=stage, path=path)
        return prompt_version, "override"
    if not slot:
        return None, "baseline"
    registry = load_registry(path)
    versions = list(registry.get("versions") or [])
    if model:
        discovered = _scoped_versions(versions, stage, model, profile)
        if discovered is not None:
            return str(discovered.get("id")), "registry_model"
        stage_slots = (registry.get("slots") or {}).get(stage) or {}
        model_slot = (stage_slots.get("models", {}).get(model) or {}).get(slot)
        if model_slot:
            version = get_version(model_slot, path)
            if version is not None:
                scope = version_scope(version)
                profiles = scope.get("profiles") or []
                if not profiles or (profile and profile in profiles):
                    return model_slot, "registry_model"
    general_slot = ((registry.get("slots") or {}).get(stage) or {}).get("general", {}).get(slot)
    if general_slot:
        return general_slot, "registry_general"
    return None, "baseline"


def resolve_version_id(
    *,
    stage: str,
    prompt_version: Optional[str] = None,
    slot: Optional[str] = None,
    model: Optional[str] = None,
    profile: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[str]:
    """Explicit version wins; else the slot pointer; else None (legacy baseline)."""
    version_id, _source = resolve_version(
        stage=stage, prompt_version=prompt_version, slot=slot, model=model, profile=profile, path=path
    )
    return version_id


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
    """Mutate ``spec``'s stage with the resolved overlay. Returns the components."""
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
    version = get_version(prompt_version, path) if prompt_version else None
    if version is not None:
        stage_obj.component_text = dict(version.get("component_text") or {})
        stage_obj.system_template = str(version.get("system_template") or "") or None
        stage_obj.user_template = str(version.get("user_template") or "") or None
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
    version = get_version(prompt_version, path) if prompt_version else None
    local_spec = copy.copy(spec)
    replacements: Dict[str, Any] = {"prompt_components": components}
    if version is not None:
        replacements["component_text"] = dict(version.get("component_text") or {})
        replacements["system_template"] = str(version.get("system_template") or "") or None
        replacements["user_template"] = str(version.get("user_template") or "") or None
    setattr(local_spec, f"{stage_kind}_stage", dataclasses.replace(stage_obj, **replacements))
    render = getattr(candidate_module, f"render_{stage_kind}_system")
    text = render(local_spec)
    return text, fingerprint(text)


def version_text(version: Mapping[str, Any], *, path: Optional[Path] = None) -> Tuple[str, str]:
    """Return the self-contained system-prompt text + fingerprint for a version."""
    stage = str(version.get("stage") or "chapter")
    template = str(version.get("template") or "")
    if not template:
        template = _render_template(
            stage,
            version.get("components") or {},
            version.get("component_text") or {},
            str(version.get("system_template") or "") or None,
        )
    return template, fingerprint(template) if template else ""


@dataclasses.dataclass
class ResolvedPrompt:
    stage: str
    version_id: str
    system_prompt: str
    fingerprint: str
    source: str
    placeholders: Dict[str, List[str]]
    user_template: str = ""
    model: str = ""
    reasoning: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def render_user(self, values: Mapping[str, object]) -> str:
        from core import prompt_template
        return prompt_template.render(self.user_template, values)


def resolve_prompt(
    *,
    stage: str,
    version_id: Optional[str] = None,
    slot: Optional[str] = None,
    model: Optional[str] = None,
    profile: Optional[str] = None,
    path: Optional[Path] = None,
) -> ResolvedPrompt:
    """Resolve the effective prompt for a run (spec §5.5 / §6.3)."""
    from core import prompt_template
    resolved_id, source = resolve_version(
        stage=stage, prompt_version=version_id, slot=slot, model=model, profile=profile, path=path
    )
    version = get_version(resolved_id, path) if resolved_id else None
    system_prompt = ""
    user_template = ""
    placeholders: Dict[str, List[str]] = {
        "system": list(prompt_template.ALLOWLISTS[stage]["system"]),
        "user": list(prompt_template.ALLOWLISTS[stage]["user"]),
    }
    if version is not None:
        system_prompt, _fp = version_text(version)
        user_template = str(version.get("user_template") or "")
        placeholders = {
            "system": prompt_template.extract_placeholders(version.get("system_template") or ""),
            "user": prompt_template.extract_placeholders(user_template),
        }
    return ResolvedPrompt(
        stage=stage,
        version_id=resolved_id or "",
        system_prompt=system_prompt,
        fingerprint=fingerprint(system_prompt) if system_prompt else "",
        source=source,
        placeholders=placeholders,
        user_template=user_template,
        model=str((version or {}).get("model") or ""),
        reasoning=dict((version or {}).get("reasoning") or {}),
    )


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
