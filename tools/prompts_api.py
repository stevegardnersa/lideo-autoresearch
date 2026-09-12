#!/usr/bin/env python3
"""JSON API shim for the dashboard Prompts settings.

Reads one JSON command from stdin and writes one JSON response to stdout. Keeps
registry validation and atomic writes in ``core/prompt_registry`` rather than
duplicating them in Node.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import prompt_registry as pr  # noqa: E402


def _fail(error: str, *, code: int = 400, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": False, "error": error, "code": code}
    payload.update(extra)
    return payload


def _stage_filter(command: Dict[str, Any]) -> str:
    stage = str(command.get("stage") or "").strip()
    return stage if stage in pr.STAGES else ""


def _production_status(registry: Dict[str, Any]) -> list:
    report = pr.read_cf_report()
    rows = []
    for stage in pr.STAGES:
        selected = ((registry.get("slots") or {}).get(stage) or {}).get("production")
        selected_version = pr.get_version(selected) if selected else None
        selected_fp = str((selected_version or {}).get("system_prompt_fingerprint") or "")
        # The report is a single last-CF-call snapshot; match by version.
        reported_version = str(report.get("prompt_version") or "")
        reported_fp = str(report.get("prompt_fingerprint") or "")
        rows.append({
            "stage": stage,
            "selected_id": selected or "",
            "selected_fingerprint": selected_fp,
            "reported_id": reported_version,
            "reported_fingerprint": reported_fp,
            "reported_at": str(report.get("reported_at") or ""),
            "in_sync": bool(selected_fp and reported_fp and selected_fp == reported_fp),
            "never_reported": not bool(reported_fp),
        })
    return rows


def _options() -> Dict[str, Any]:
    import candidate_spec

    dims: Dict[str, list] = {}
    for key, table in candidate_spec.chapter_component_tables().items():
        dims[key] = list(table.keys())
    composer_keys = list(candidate_spec.composer_component_tables().keys())
    return {
        "dimensions": dims,
        "stage_keys": {
            "chapter": list(candidate_spec.chapter_component_tables().keys()),
            "composer": composer_keys,
        },
    }


def _sample_spec(stage: str, components: Dict[str, str]):
    import candidate_spec

    profile = "30m_gpt-5.6-luna_effort-minimal"
    profiles = list(candidate_spec.PROFILE_CANDIDATES.keys())
    if profile not in profiles and profiles:
        profile = profiles[0]
    spec = copy.deepcopy(candidate_spec.PROFILE_CANDIDATES[profile])
    stage_obj = getattr(spec, f"{stage}_stage")
    stage_obj.prompt_components = {**(stage_obj.prompt_components or {}), **components}
    return spec


def handle(command: Dict[str, Any]) -> Dict[str, Any]:
    op = str(command.get("op") or "").strip()
    regex_path = command.get("path")
    path = Path(regex_path) if regex_path else None

    if op == "list":
        registry = pr.load_registry(path)
        versions = list(registry.get("versions") or [])
        stage = _stage_filter(command)
        if stage:
            versions = [v for v in versions if v.get("stage") == stage]
        return {
            "ok": True,
            "registry_sha": registry.get("registry_sha", ""),
            "versions": versions,
            "slots": registry.get("slots", {}),
            "production_status": _production_status(registry),
        }

    if op == "options":
        return {"ok": True, **_options()}

    if op == "get":
        version = pr.get_version(str(command.get("id") or ""), path)
        if version is None:
            return _fail("unknown_prompt_version", code=404)
        text, fp = pr.version_text(version)
        return {
            "ok": True,
            "version": version,
            "rendered_system_prompt": text,
            "system_prompt_sha256": pr.sha256_text(text),
            "fingerprint": fp,
        }

    if op == "create":
        stage = str(command.get("stage") or "")
        if stage not in pr.STAGES:
            return _fail("stage must be 'chapter' or 'composer'", fieldErrors={"stage": "Invalid stage"})
        label = str(command.get("label") or "").strip()
        if not label:
            return _fail("label is required", fieldErrors={"label": "Label is required"})
        components = command.get("components") or {}
        if not isinstance(components, dict):
            return _fail("components must be an object", fieldErrors={"components": "Invalid components"})
        version = pr.create_version(
            stage=stage,
            label=label,
            components=components,
            template=command.get("template") or None,
            notes=str(command.get("notes") or ""),
            path=path,
        )
        return {"ok": True, "version": version}

    if op == "update":
        body = command.get("body") or {}
        content_keys = set(body) - {"label", "notes"}
        if content_keys:
            return _fail("version content is immutable; create a new version", code=409)
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
            return _fail(str(exc), code=409)
        except pr.UnknownVersionError as exc:
            return _fail(str(exc), code=404)
        return {"ok": True}

    if op == "set_slot":
        try:
            slots = pr.set_slot(
                stage=str(command.get("stage") or ""),
                slot=str(command.get("slot") or ""),
                version_id=str(command.get("version_id") or ""),
                expected_sha=command.get("expected_sha") or None,
                path=path,
            )
        except pr.RegistryConflictError as exc:
            return _fail(str(exc), code=409)
        except pr.UnknownVersionError as exc:
            return _fail(str(exc), code=404, allowed=exc.allowed)
        except pr.PromptRegistryError as exc:
            return _fail(str(exc), code=getattr(exc, "status_code", 400))
        return {"ok": True, "slots": slots}

    if op == "preview":
        version = pr.get_version(str(command.get("id") or ""), path)
        if version is None:
            return _fail("unknown_prompt_version", code=404)
        target_words = int(command.get("target_words") or 1200)
        stage = str(command.get("stage") or version.get("stage") or "chapter")
        try:
            spec = _sample_spec(stage, version.get("components") or {})
        except Exception as exc:  # pragma: no cover - defensive
            return _fail(str(exc))
        import candidate_spec
        if stage == "composer":
            system_prompt = candidate_spec.render_composer_system(spec)
            user_prompt = candidate_spec.render_composer_user(
                spec, chapter_summaries_md="## Chapter 1\n\nSummary text.", target_words=target_words
            )
        else:
            system_prompt = candidate_spec.render_chapter_system(spec)
            user_prompt = candidate_spec.render_chapter_user(
                spec, source_md="Chapter source text.", target_words=target_words
            )
        return {
            "ok": True,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "fingerprint": pr.fingerprint(system_prompt),
        }

    if op == "sync":
        registry = pr.load_registry(path)
        slots = registry.get("slots") or {}
        production_version = (slots.get("chapter") or {}).get("production", "")
        bundle = ROOT / "cloud_function" / "prompts" / "registry.json"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "copied": str(bundle),
            "production_version": production_version,
            "registry_sha": registry.get("registry_sha", ""),
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
    except pr.PromptRegistryError as exc:
        result = _fail(str(exc), code=getattr(exc, "status_code", 400))
    except Exception as exc:  # pragma: no cover - defensive
        result = _fail(f"{type(exc).__name__}: {exc}", code=500)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
