#!/usr/bin/env python3
"""JSON API shim for the dashboard Prompts settings.

Reads one JSON command from stdin and writes one JSON response to stdout. Keeps
registry validation and atomic writes in ``core/prompt_registry`` rather than
duplicating them in Node.

See ``specs/prompts-ui-model-scoped-editing.md`` §8 for the endpoint contracts.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import prompt_registry as pr  # noqa: E402
from core import prompt_template as pt  # noqa: E402


def _fail(message: str, *, code: int = 400, error: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": False, "error": error or message, "code": code}
    if error and error != message:
        payload["message"] = message
    payload.update(extra)
    return payload


# Curated base chapter prompts, slug -> registry label. A base is an ordinary
# version whose label starts with ``base: <slug>`` (see
# specs/base-prompts-and-model-strategy-tuning.md §4.2/§4.3).
BASE_PROMPTS: Dict[str, str] = {
    "sentence": "base: sentence",
    "readable": "base: readable",
    "readable-low": "base: readable-low",
}


def _base_label_matches(label: str, slug: str) -> bool:
    """True when ``label`` is ``base: <slug>`` or ``base: <slug> <suffix>``.

    The trailing separator check keeps ``readable`` from matching
    ``base: readable-low``.
    """
    text = str(label or "").strip()
    prefix = f"base: {slug}"
    return text == prefix or text.startswith(prefix + " ")


def list_base_prompts(*, stage: str = "chapter", path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Resolve each ``BASE_PROMPTS`` slug to its version record.

    Raises ``pr.PromptRegistryError`` when a slug resolves to zero or more than
    one version, so an ambiguous or missing base fails loudly rather than
    silently picking one.
    """
    versions = [
        v for v in pr.list_versions(stage, path=path)
        if pr.version_scope(v)["kind"] == "general"
    ]
    bases: List[Dict[str, Any]] = []
    for slug in BASE_PROMPTS:
        matches = [v for v in versions if _base_label_matches(v.get("label"), slug)]
        if len(matches) != 1:
            raise pr.PromptRegistryError(
                f"base {slug!r} must resolve to exactly one version, found {len(matches)}"
            )
        version = matches[0]
        bases.append({
            "slug": slug,
            "version_id": version.get("id", ""),
            "label": version.get("label", ""),
            "components": dict(version.get("components") or {}),
            "system_prompt_fingerprint": version.get("system_prompt_fingerprint", ""),
        })
    return bases


def _stage_filter(command: Dict[str, Any]) -> str:
    stage = str(command.get("stage") or "").strip()
    return stage if stage in pr.STAGES else ""


def _candidates_path() -> Path:
    return ROOT / "data" / "candidates.json"


def _models_info() -> Tuple[List[str], Dict[str, List[str]]]:
    """Return (sorted model ids, model -> profile keys) from the candidate store."""
    try:
        data = json.loads(_candidates_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    profiles = data.get("profiles") if isinstance(data, dict) else {}
    profiles = profiles if isinstance(profiles, dict) else {}
    models = set((data.get("models") or {}).keys()) if isinstance(data, dict) else set()
    by_model: Dict[str, List[str]] = {}
    for key, spec in profiles.items():
        model = ""
        if isinstance(spec, dict):
            model = str((spec.get("chapter_stage") or {}).get("model") or "")
        if not model:
            continue
        models.add(model)
        by_model.setdefault(model, []).append(str(key))
    return sorted(str(m) for m in models if m), {m: sorted(v) for m, v in by_model.items()}


class _ScopeError(Exception):
    def __init__(self, message: str, *, error: str = "invalid_scope", field: str = "scope"):
        super().__init__(message)
        self.error = error
        self.field = field


def _validate_scope(raw: Any) -> Dict[str, Any]:
    try:
        scope = pr.normalize_scope(raw)
    except pr.InvalidScopeError as exc:
        raise _ScopeError(str(exc), error="invalid_scope") from exc
    if scope["kind"] == "general":
        return scope
    models, by_model = _models_info()
    if scope["model"] not in models:
        raise _ScopeError(f"Unknown model: {scope['model']!r}", error="unknown_model", field="scope_model")
    bad = [p for p in (scope.get("profiles") or []) if p not in by_model.get(scope["model"], [])]
    if bad:
        raise _ScopeError(
            f"Profile(s) not registered for {scope['model']}: {', '.join(bad)}",
            error="unknown_profile",
            field="scope_profiles",
        )
    return scope


def _component_tables(stage: str) -> Dict[str, Dict[str, str]]:
    import candidate_spec

    return candidate_spec.chapter_component_tables() if stage == "chapter" else candidate_spec.composer_component_tables()


def _clean_components(stage: str, raw: Any) -> Dict[str, str]:
    tables = _component_tables(stage)
    components: Dict[str, str] = {}
    if raw is None:
        return components
    if not isinstance(raw, dict):
        raise _ScopeError("components must be an object", error="invalid_components", field="components")
    for key, value in raw.items():
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        if key not in tables or text not in tables[key]:
            raise _ScopeError(
                f"Unknown component {text!r} for dimension {key!r}",
                error="invalid_components",
                field="components",
            )
        components[str(key)] = text
    return components


def _clean_component_text(stage: str, raw: Any) -> Dict[str, str]:
    tables = _component_tables(stage)
    text_map: Dict[str, str] = {}
    if raw is None:
        return text_map
    if not isinstance(raw, dict):
        raise _ScopeError("component_text must be an object", error="invalid_component_text", field="component_text")
    for key, value in raw.items():
        if key not in tables:
            raise _ScopeError(f"Unknown dimension {key!r}", error="invalid_component_text", field="component_text")
        if isinstance(value, str) and value.strip():
            text_map[str(key)] = value
    return text_map


def _allowlists(stage: str) -> Dict[str, Any]:
    entry = pt.ALLOWLISTS[stage]
    return {"system": list(entry["system"]), "user": list(entry["user"])}


def _template_field_errors(stage: str, system_template: str, user_template: str,
                           components: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    fields: Dict[str, str] = {}
    warnings: List[str] = []
    allow = _allowlists(stage)

    def _check(text: str, field: str, allowed: List[str]) -> None:
        if not str(text or "").strip():
            return
        if len(str(text).encode("utf-8")) > pt.MAX_TEMPLATE_BYTES:
            fields[field] = "Template exceeds 64 KB"
            return
        try:
            pt.validate(text, allowed)
        except pt.UnbalancedTemplate:
            fields[field] = "Unbalanced '{{' / '}}' delimiters"
        except pt.UnknownPlaceholder as exc:
            fields[field] = "Unknown placeholder(s): " + ", ".join(exc.names) + " — allowed: " + ", ".join(allowed)

    _check(system_template, "system_template", allow["system"])
    _check(user_template, "user_template", allow["user"])
    if "system_template" not in fields and str(system_template or "").strip():
        names = pt.extract_placeholders(system_template)
        if "components" in names and not components:
            warnings.append("system_template uses {{components}} but no components are selected")
    if "user_template" not in fields and str(user_template or "").strip():
        required = "source_md" if stage == "chapter" else "chapter_summaries"
        if required not in pt.extract_placeholders(user_template):
            fields["user_template"] = f"Template must contain {{{{{required}}}}}"
    return fields, warnings


def _template_error_code(field_errors: Dict[str, str]) -> str:
    for message in field_errors.values():
        if str(message).startswith("Unknown placeholder"):
            return "unknown_placeholder"
    return "invalid_template"


def _validate_label(label: str) -> Optional[str]:
    if not label.strip():
        return "Label is required"
    if len(label) > 120:
        return "Label must be 120 characters or fewer"
    return None


def _sample_spec(stage: str, components: Dict[str, str], component_text: Optional[Dict[str, str]] = None,
                 system_template: str = "", user_template: str = ""):
    import candidate_spec

    profile = "30m_gpt-5.6-luna_effort-minimal"
    profiles = list(candidate_spec.PROFILE_CANDIDATES.keys())
    if profile not in profiles and profiles:
        profile = profiles[0]
    spec = copy.deepcopy(candidate_spec.PROFILE_CANDIDATES[profile])
    stage_obj = getattr(spec, f"{stage}_stage")
    stage_obj.prompt_components = {**(stage_obj.prompt_components or {}), **components}
    stage_obj.component_text = dict(component_text or {})
    stage_obj.system_template = str(system_template or "") or None
    stage_obj.user_template = str(user_template or "") or None
    return spec


def _render_draft(stage: str, *, components: Dict[str, str], component_text: Dict[str, str],
                  system_template: str, user_template: str, target_words: int,
                  sample: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import candidate_spec

    sample = sample or {}
    spec = _sample_spec(stage, components, component_text, system_template, user_template)
    source_md = str(sample.get("source_md") or "Chapter source text.")
    chapter_summaries_md = str(sample.get("chapter_summaries") or "## Chapter 1\n\nSummary text.")
    book_title = str(sample.get("book_title") or "Sample Book")
    chapter_title = str(sample.get("chapter_title") or "Sample Chapter")
    toc_md = str(sample.get("toc_md") or "")
    book_metadata = str(sample.get("book_metadata") or "")
    if stage == "composer":
        system_prompt = candidate_spec.render_composer_system(spec)
        user_prompt = candidate_spec.render_composer_user(
            spec, chapter_summaries_md=chapter_summaries_md, target_words=target_words,
            book_title=book_title, toc_md=toc_md, book_metadata=book_metadata,
        )
    else:
        system_prompt = candidate_spec.render_chapter_system(spec)
        user_prompt = candidate_spec.render_chapter_user(
            spec, source_md=source_md, target_words=target_words,
            book_title=book_title, chapter_title=chapter_title, toc_md=toc_md, book_metadata=book_metadata,
        )
    _, warnings = _template_field_errors(stage, system_template, user_template, components)
    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "fingerprint": pr.fingerprint(system_prompt),
        "warnings": warnings,
        "placeholders": {
            "system": pt.extract_placeholders(system_template),
            "user": pt.extract_placeholders(user_template),
        },
    }


def _component_text_library() -> Dict[str, Dict[str, Dict[str, str]]]:
    return {
        "chapter": {dim: dict(table) for dim, table in _component_tables("chapter").items()},
        "composer": {dim: dict(table) for dim, table in _component_tables("composer").items()},
    }


def _dimension_help() -> Dict[str, str]:
    import candidate_spec

    try:
        return candidate_spec.component_help()
    except AttributeError:
        return {}


def _options() -> Dict[str, Any]:
    dims: Dict[str, list] = {}
    for key, table in _component_tables("chapter").items():
        dims[key] = list(table.keys())
    composer_keys = list(_component_tables("composer").keys())
    models, profiles_by_model = _models_info()
    return {
        "dimensions": dims,
        "stage_keys": {
            "chapter": list(_component_tables("chapter").keys()),
            "composer": composer_keys,
        },
        "placeholders": {
            "chapter": _allowlists("chapter"),
            "composer": _allowlists("composer"),
        },
        "component_text": _component_text_library(),
        "dimension_help": _dimension_help(),
        "models": models,
        "profiles_by_model": profiles_by_model,
    }


def _production_status(registry: Dict[str, Any]) -> list:
    report = pr.read_cf_report()
    report_version = str(report.get("prompt_version") or "")
    report_fp = str(report.get("prompt_fingerprint") or "")
    report_model = str(report.get("prompt_model") or "")
    reported_at = str(report.get("reported_at") or "")
    rows = []
    for stage in pr.STAGES:
        stage_slots = (registry.get("slots") or {}).get(stage) or {}
        targets: List[Tuple[Optional[str], Optional[str]]] = [
            (None, (stage_slots.get("general") or {}).get("production"))
        ]
        for model, slots in (stage_slots.get("models") or {}).items():
            targets.append((model, (slots or {}).get("production")))
        for model, selected in targets:
            if model and not selected:
                continue
            selected_version = pr.get_version(selected) if selected else None
            selected_fp = str((selected_version or {}).get("system_prompt_fingerprint") or "")
            row_model = model or ""
            matches = bool(report_fp) and report_version == (selected or "") and report_model == row_model
            rows.append({
                "stage": stage,
                "model": model,
                "selected_id": selected or "",
                "selected_fingerprint": selected_fp,
                "reported_id": report_version,
                "reported_fingerprint": report_fp,
                "reported_at": reported_at,
                "in_sync": bool(selected_fp and report_fp and selected_fp == report_fp and matches),
                "never_reported": not bool(report_fp),
            })
    return rows


def _effective_map(registry: Dict[str, Any], models: List[str]) -> Dict[str, Any]:
    effective: Dict[str, Any] = {}
    for stage in pr.STAGES:
        entries = {}
        generic, _ = pr.resolve_version(stage=stage, slot="local")
        entries["general"] = {"version_id": generic or "", "source": "registry_general" if generic else "baseline"}
        for model in models:
            vid, source = pr.resolve_version(stage=stage, slot="local", model=model)
            entries[model] = {"version_id": vid or "", "source": source}
        effective[stage] = entries
    return effective


BASE_SWEEP_DIR = ROOT / "artifacts" / "autoresearch" / "base_sweep"
JOBS_DIR = ROOT / "artifacts" / "jobs"

_SWEEP_JOB_WINDOW_S = 6 * 3600
_PROFILE_RE = re.compile(r"--profile\s+(\S+)")
_BENCH_RE = re.compile(r"--bench\s+(\S+)")
_JOB_ID_RE = re.compile(r'"jobId"\s*:\s*"([^"]+)"')
_CREATED_RE = re.compile(r'"createdAt"\s*:\s*"([^"]+)"')
_SWEEP_JOB_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _artifact_dir(explicit: Optional[Any] = None) -> Path:
    """Resolve the base-sweep artifact directory.

    Priority: an explicit path, then ``BASE_SWEEP_ARTIFACT_DIR`` (used by tests
    and alternate checkouts), then the repo default.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("BASE_SWEEP_ARTIFACT_DIR")
    return Path(env) if env else BASE_SWEEP_DIR


def _jobs_dir(explicit: Optional[Any] = None) -> Path:
    """Resolve the job-log directory (env override keeps tests isolated)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("BASE_SWEEP_JOBS_DIR")
    return Path(env) if env else JOBS_DIR


def _safe_comparison_id(value: Any) -> str:
    """Reject comparison ids that could escape the artifact directory."""
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or text.startswith("."):
        return ""
    return text


def _parse_iso(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (with optional Z / millis) into epoch seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan_sweep_job(log_path: Path) -> Optional[Dict[str, Any]]:
    """Parse a job log into a lightweight sweep record, or None if not a sweep.

    Results are cached by (path, mtime, size) so repeated listing calls stay cheap.
    """
    try:
        st = log_path.stat()
    except OSError:
        return None
    key = f"{log_path}:{st.st_mtime_ns}:{st.st_size}"
    if key in _SWEEP_JOB_CACHE:
        return _SWEEP_JOB_CACHE[key]
    record: Optional[Dict[str, Any]] = None
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(4_000_000)
    except OSError:
        text = ""
    first = text.splitlines()[0] if text else ""
    if '"toolId":"sweep_base_prompts"' in first:
        profile = _PROFILE_RE.search(text)
        bench = _BENCH_RE.search(text)
        job_id = _JOB_ID_RE.search(first)
        created = _CREATED_RE.search(first)
        record = {
            "job_id": job_id.group(1) if job_id else log_path.stem,
            "created_at": created.group(1) if created else "",
            "mtime": st.st_mtime,
            "profile": profile.group(1) if profile else "",
            "bench": bench.group(1) if bench else "",
            "log_path": str(log_path),
        }
    _SWEEP_JOB_CACHE[key] = record
    return record


def resolve_comparison_job(data: Dict[str, Any],
                           jobs_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Find the job that produced a sweep artifact.

    Uses the recorded ``job_id`` when present; otherwise falls back to matching
    a sweep job log by profile + bench within a time window around
    ``generated_at`` (this recovers runs launched from the CLI/Makefile).
    """
    recorded = str(data.get("job_id") or "")
    directory = _jobs_dir(jobs_dir)
    if recorded:
        log_path = directory / f"{recorded}.log"
        return {
            "job_id": recorded,
            "resolved": False,
            "log_path": str(log_path) if log_path.is_file() else "",
        }
    profile = str(data.get("profile") or "")
    bench = str(data.get("bench") or "")
    if not profile and not bench:
        return {"job_id": "", "resolved": False, "log_path": ""}
    if not directory.is_dir():
        return {"job_id": "", "resolved": False, "log_path": ""}
    generated = _parse_iso(data.get("generated_at"))
    candidates: List[Dict[str, Any]] = []
    for log_path in directory.glob("*.log"):
        rec = _scan_sweep_job(log_path)
        if not rec or rec["profile"] != profile or rec["bench"] != bench:
            continue
        candidates.append(rec)
    if not candidates:
        return {"job_id": "", "resolved": False, "log_path": ""}

    def window_ok(rec: Dict[str, Any]) -> bool:
        if generated is None:
            return True
        created = _parse_iso(rec["created_at"])
        lo = (created if created is not None else rec["mtime"] - _SWEEP_JOB_WINDOW_S) - _SWEEP_JOB_WINDOW_S
        hi = rec["mtime"] + _SWEEP_JOB_WINDOW_S
        return lo <= generated <= hi

    in_window = [c for c in candidates if window_ok(c)]
    pool = in_window or candidates
    target = generated if generated is not None else 0.0
    if generated is None:
        best = max(pool, key=lambda c: c["mtime"])
    else:
        best = min(pool, key=lambda c: abs(c["mtime"] - target))
    return {"job_id": best["job_id"], "resolved": True, "log_path": best["log_path"]}


def _comparison_summary(json_path: Path, data: Dict[str, Any],
                        job: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    job = job or {"job_id": str(data.get("job_id") or ""), "resolved": False, "log_path": ""}
    job_id = str(job.get("job_id") or "")
    return {
        "id": json_path.stem,
        "profile": str(data.get("profile") or ""),
        "bench": str(data.get("bench") or ""),
        "generated_at": str(data.get("generated_at") or ""),
        "winner": str(data.get("winner") or ""),
        "repeat": data.get("repeat"),
        "tolerance_pct": data.get("tolerance_pct"),
        "job_id": job_id,
        "has_log": bool(job_id),
        "log_resolved": bool(job.get("resolved")),
        "log_path": str(job.get("log_path") or ""),
        "artifact": str(json_path),
        "markdown": str(json_path.with_suffix(".md")),
    }


def list_comparisons(*, artifact_dir: Optional[Path] = None,
                     jobs_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    """List every base-prompt sweep artifact as a comparison run, newest first."""
    directory = _artifact_dir(artifact_dir)
    if not directory.is_dir():
        return []
    summaries: List[Dict[str, Any]] = []
    for json_path in directory.glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "rows" not in data:
            continue
        job = resolve_comparison_job(data, jobs_dir=jobs_dir)
        summaries.append(_comparison_summary(json_path, data, job))
    summaries.sort(key=lambda s: (s["generated_at"], s["id"]), reverse=True)
    return summaries


def get_comparison(comparison_id: str, *, artifact_dir: Optional[Path] = None,
                   jobs_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Return one sweep's parsed JSON plus its rendered markdown summary."""
    safe = _safe_comparison_id(comparison_id)
    if not safe:
        return None
    directory = _artifact_dir(artifact_dir)
    json_path = directory / f"{safe}.json"
    if not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    markdown = ""
    md_path = json_path.with_suffix(".md")
    if md_path.is_file():
        try:
            markdown = md_path.read_text(encoding="utf-8")
        except OSError:
            markdown = ""
    job = resolve_comparison_job(data, jobs_dir=jobs_dir) if isinstance(data, dict) else None
    return {
        "id": safe,
        "summary": _comparison_summary(json_path, data, job) if isinstance(data, dict) else {},
        "data": data,
        "markdown": markdown,
    }


def handle(command: Dict[str, Any]) -> Dict[str, Any]:
    op = str(command.get("op") or "").strip()
    regex_path = command.get("path")
    path = Path(regex_path) if regex_path else None
    artifact_arg = command.get("artifact_dir")
    comparison_dir = Path(artifact_arg) if artifact_arg else None
    jobs_arg = command.get("jobs_dir")
    comparison_jobs_dir = Path(jobs_arg) if jobs_arg else None

    if op == "list":
        registry = pr.load_registry(path)
        versions = list(registry.get("versions") or [])
        stage = _stage_filter(command)
        if stage:
            versions = [v for v in versions if v.get("stage") == stage]
        model_filter = str(command.get("model") or "").strip()
        if model_filter:
            versions = [
                v for v in versions
                if pr.version_scope(v)["kind"] == "general" or pr.version_scope(v).get("model") == model_filter
            ]
        models, profiles_by_model = _models_info()
        try:
            bases = list_base_prompts(stage=stage or "chapter", path=path)
            bases_error = ""
        except pr.PromptRegistryError as exc:
            bases, bases_error = [], str(exc)
        try:
            comparisons, comparisons_error = list_comparisons(
                artifact_dir=comparison_dir, jobs_dir=comparison_jobs_dir), ""
        except OSError as exc:
            comparisons, comparisons_error = [], str(exc)
        return {
            "ok": True,
            "registry_sha": registry.get("registry_sha", ""),
            "versions": versions,
            "slots": registry.get("slots", {}),
            "bases": bases,
            "bases_error": bases_error,
            "comparisons": comparisons,
            "comparisons_error": comparisons_error,
            "production_status": _production_status(registry),
            "effective": _effective_map(registry, models),
            "models": models,
            "profiles_by_model": profiles_by_model,
        }

    if op == "list_bases":
        stage = _stage_filter(command) or "chapter"
        return {
            "ok": True,
            "stage": stage,
            "bases": list_base_prompts(stage=stage, path=path),
        }

    if op == "list_comparisons":
        return {"ok": True, "comparisons": list_comparisons(
            artifact_dir=comparison_dir, jobs_dir=comparison_jobs_dir)}

    if op == "get_comparison":
        result = get_comparison(str(command.get("id") or ""), artifact_dir=comparison_dir,
                                jobs_dir=comparison_jobs_dir)
        if result is None:
            return _fail("unknown_comparison", code=404)
        return {"ok": True, **result}

    if op == "options":
        return {"ok": True, **_options()}

    if op == "get":
        version = pr.get_version(str(command.get("id") or ""), path)
        if version is None:
            return _fail("unknown_prompt_version", code=404)
        text, fp = pr.version_text(version)
        stage = str(version.get("stage") or "chapter")
        spec = _sample_spec(stage, version.get("components") or {}, version.get("component_text") or {},
                            version.get("system_template") or "", version.get("user_template") or "")
        import candidate_spec
        if stage == "composer":
            user_prompt = candidate_spec.render_composer_user(spec, chapter_summaries_md="## Chapter 1\n\nSummary text.", target_words=int(command.get("target_words") or 1200))
        else:
            user_prompt = candidate_spec.render_chapter_user(spec, source_md="Chapter source text.", target_words=int(command.get("target_words") or 1200))
        return {
            "ok": True,
            "version": version,
            "rendered_system_prompt": text,
            "rendered_user_prompt": user_prompt,
            "system_prompt_sha256": pr.sha256_text(text),
            "fingerprint": fp,
        }

    if op == "preview_draft":
        stage = str(command.get("stage") or "")
        if stage not in pr.STAGES:
            return _fail("stage must be 'chapter' or 'composer'", fieldErrors={"stage": "Invalid stage"})
        try:
            components = _clean_components(stage, command.get("components"))
            component_text = _clean_component_text(stage, command.get("component_text"))
        except _ScopeError as exc:
            return _fail(str(exc), error=exc.error, fieldErrors={exc.field: str(exc)})
        system_template = str(command.get("system_template") or "")
        user_template = str(command.get("user_template") or "")
        field_errors, warnings = _template_field_errors(stage, system_template, user_template, components)
        if field_errors:
            return _fail(_template_error_code(field_errors), fieldErrors=field_errors)
        target_words = int(command.get("target_words") or 1200)
        body = _render_draft(
            stage, components=components, component_text=component_text,
            system_template=system_template, user_template=user_template,
            target_words=target_words, sample=command.get("sample"),
        )
        return {"ok": True, **body}

    if op == "create":
        stage = str(command.get("stage") or "")
        field_errors: Dict[str, str] = {}
        if stage not in pr.STAGES:
            return _fail("stage must be 'chapter' or 'composer'", fieldErrors={"stage": "Invalid stage"})
        label = str(command.get("label") or "").strip()
        label_error = _validate_label(label)
        if label_error:
            field_errors["label"] = label_error
        try:
            components = _clean_components(stage, command.get("components"))
            component_text = _clean_component_text(stage, command.get("component_text"))
        except _ScopeError as exc:
            field_errors[exc.field] = str(exc)
        system_template = str(command.get("system_template") or command.get("template") or "")
        user_template = str(command.get("user_template") or "")
        tpl_errors, _warnings = _template_field_errors(stage, system_template, user_template, components)
        field_errors.update(tpl_errors)
        scope = None
        scope_error: Optional[str] = None
        if command.get("scope") is not None:
            try:
                scope = _validate_scope(command.get("scope"))
            except _ScopeError as exc:
                field_errors[exc.field] = str(exc)
                scope_error = exc.error
        if field_errors:
            return _fail(scope_error or _template_error_code(field_errors), fieldErrors=field_errors)
        derived_from = str(command.get("derived_from") or "").strip()
        if derived_from:
            source = pr.get_version(derived_from, path)
            if source is None:
                field_errors["derived_from"] = "Unknown source version"
            elif str(source.get("stage") or "") != stage:
                field_errors["derived_from"] = "Source version is a different stage"
        if field_errors:
            return _fail(
                scope_error or _template_error_code(field_errors),
                error="invalid_derived_from" if "derived_from" in field_errors else None,
                fieldErrors=field_errors,
            )
        body = {k: v for k, v in dict(command).items() if k not in {"op", "path", "stage", "label"}}
        record_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if record_bytes > pt.MAX_VERSION_BYTES:
            return _fail("payload_too_large", code=400, fieldErrors={"label": "Version body exceeds 256 KB"})
        version = pr.create_version(
            stage=stage,
            label=label,
            components=components,
            component_text=component_text,
            system_template=system_template,
            user_template=user_template,
            scope=scope,
            template=command.get("template") or None,
            notes=str(command.get("notes") or ""),
            derived_from=derived_from or None,
            path=path,
        )
        return {"ok": True, "version": version}

    if op == "revise":
        source_id = str(command.get("id") or "")
        source = pr.get_version(source_id, path)
        if source is None:
            return _fail("unknown_prompt_version", code=404)
        stage = str(source.get("stage") or "chapter")
        field_errors: Dict[str, str] = {}
        label = command.get("label")
        if label is not None:
            label = str(label).strip()
            err = _validate_label(label)
            if err:
                field_errors["label"] = err
        components = None
        if command.get("components") is not None:
            try:
                components = _clean_components(stage, command.get("components"))
            except _ScopeError as exc:
                field_errors[exc.field] = str(exc)
        component_text = None
        if command.get("component_text") is not None:
            try:
                component_text = _clean_component_text(stage, command.get("component_text"))
            except _ScopeError as exc:
                field_errors[exc.field] = str(exc)
        scope = None
        scope_error: Optional[str] = None
        if command.get("scope") is not None:
            try:
                scope = _validate_scope(command.get("scope"))
            except _ScopeError as exc:
                field_errors[exc.field] = str(exc)
                scope_error = exc.error
        effective_components = components if components is not None else dict(source.get("components") or {})
        system_template = str(
            command.get("system_template")
            if command.get("system_template") is not None
            else source.get("system_template") or ""
        )
        user_template = str(
            command.get("user_template")
            if command.get("user_template") is not None
            else source.get("user_template") or ""
        )
        tpl_errors, _warnings = _template_field_errors(stage, system_template, user_template, effective_components)
        field_errors.update(tpl_errors)
        if field_errors:
            return _fail(scope_error or _template_error_code(field_errors), fieldErrors=field_errors)
        version = pr.revise_version(
            source_id,
            label=(label if label is not None else None),
            components=components,
            component_text=component_text,
            system_template=(system_template or None) if command.get("system_template") is not None else None,
            user_template=(user_template or None) if command.get("user_template") is not None else None,
            scope=scope,
            notes=(str(command.get("notes")) if command.get("notes") is not None else None),
            path=path,
        )
        return {"ok": True, "version": version}

    if op == "update":
        body = command.get("body") or {}
        content_keys = set(body) - {"label", "notes"}
        if content_keys:
            return _fail("version content is immutable; create a new version", code=409)
        if "label" in body and _validate_label(str(body.get("label") or "")):
            return _fail("label must be 1-120 characters", fieldErrors={"label": "Invalid label"})
        version = pr.update_version(
            str(command.get("id") or ""),
            label=body.get("label"),
            notes=body.get("notes"),
            path=path,
        )
        return {"ok": True, "version": version}

    if op == "delete":
        try:
            pr.delete_version(str(command.get("id") or ""), path=path)
        except pr.VersionInUseError as exc:
            return _fail(str(exc), error="version_in_use", code=409)
        except pr.UnknownVersionError as exc:
            return _fail(str(exc), code=404)
        return {"ok": True}

    if op == "set_slot":
        model = str(command.get("model") or "").strip() or None
        if model:
            models, _ = _models_info()
            if model not in models:
                return _fail(f"Unknown model: {model!r}", error="unknown_model", fieldErrors={"model": "Unknown model"})
        try:
            result = pr.set_slot(
                stage=str(command.get("stage") or ""),
                slot=str(command.get("slot") or ""),
                version_id=str(command.get("version_id") or ""),
                model=model,
                expected_sha=command.get("expected_sha") or None,
                path=path,
            )
        except pr.RegistryConflictError as exc:
            return _fail(str(exc), error="registry_changed", code=409)
        except pr.ScopeMismatchError as exc:
            return _fail(str(exc), error="scope_mismatch", code=400, fieldErrors={"version_id": str(exc)})
        except pr.UnknownVersionError as exc:
            return _fail(str(exc), code=404, allowed=exc.allowed)
        except pr.PromptRegistryError as exc:
            return _fail(str(exc), code=getattr(exc, "status_code", 400))
        return {"ok": True, "slots": result["slots"], "registry_sha": result["registry_sha"]}

    if op == "preview":
        version = pr.get_version(str(command.get("id") or ""), path)
        if version is None:
            return _fail("unknown_prompt_version", code=404)
        target_words = int(command.get("target_words") or 1200)
        stage = str(command.get("stage") or version.get("stage") or "chapter")
        body = _render_draft(
            stage,
            components=dict(version.get("components") or {}),
            component_text=dict(version.get("component_text") or {}),
            system_template=str(version.get("system_template") or ""),
            user_template=str(version.get("user_template") or ""),
            target_words=target_words,
            sample=command.get("sample"),
        )
        return {"ok": True, "system_prompt": body["system_prompt"], "user_prompt": body["user_prompt"], "fingerprint": body["fingerprint"]}

    if op == "resolve":
        stage = str(command.get("stage") or "")
        if stage not in pr.STAGES:
            return _fail("stage must be 'chapter' or 'composer'", fieldErrors={"stage": "Invalid stage"})
        slot = str(command.get("slot") or "local")
        model = str(command.get("model") or "").strip() or None
        profile = str(command.get("profile") or "").strip() or None
        version_id, source = pr.resolve_version(stage=stage, slot=slot, model=model, profile=profile, path=path)
        version = pr.get_version(version_id, path) if version_id else None
        system_text, fp = pr.version_text(version) if version is not None else ("", "")
        return {
            "ok": True,
            "version_id": version_id or "",
            "source": source,
            "fingerprint": fp,
            "rendered_system_prompt": system_text,
        }

    if op == "sync":
        registry = pr.load_registry(path)
        slots = registry.get("slots") or {}
        production_version = ((slots.get("chapter") or {}).get("general") or {}).get("production", "")
        scoped = sum(
            1
            for stage in pr.STAGES
            for slots_map in ((slots.get(stage) or {}).get("models") or {}).values()
            if any((slots_map or {}).get(s) for s in pr.SLOTS)
        )
        bundle = ROOT / "cloud_function" / "prompts" / "registry.json"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        tmp = bundle.with_suffix(".tmp")
        tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(bundle)
        return {
            "ok": True,
            "copied": str(bundle),
            "production_version": production_version,
            "registry_sha": registry.get("registry_sha", ""),
            "scoped_versions": scoped,
        }

    return _fail(f"unknown op: {op!r}")


def main() -> int:
    raw = sys.stdin.read()
    try:
        command = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps(_fail(f"invalid JSON command: {exc}")))
        return 0
    try:
        result = handle(command)
    except _ScopeError as exc:
        result = _fail(str(exc), error=exc.error, fieldErrors={exc.field: str(exc)})
    except pt.TemplateError as exc:
        code = "unknown_placeholder" if isinstance(exc, pt.UnknownPlaceholder) else "invalid_template"
        result = _fail(str(exc), error=code, fieldErrors={"template": str(exc)})
    except pr.PromptRegistryError as exc:
        result = _fail(str(exc), code=getattr(exc, "status_code", 400), error=getattr(exc, "error", "prompt_registry_error"))
    except Exception as exc:  # pragma: no cover - defensive
        result = _fail(f"{type(exc).__name__}: {exc}", code=500)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
