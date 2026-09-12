#!/usr/bin/env python3
"""
Auto-probe a model for capability compatibility and append candidate profile(s)
to data/candidates.json.

Probes (recommended API, see core/reasoning.py):
  1. JSON schema support   (use_json_schema=True)
  2. Reasoning effort tiers via ``reasoning: {"effort": X}`` (falls back to
     the top-level ``reasoning_effort: X`` style when the structured param is
     rejected)
  c. Legacy thinking/non-thinking params (``--probe-legacy``)

Creates one profile per supported effort. ``effort=none`` maps to the legacy
``notthinking`` suffix; higher efforts get ``effort-<name>`` suffixes that never
collide with the legacy ``_thinking`` names.

Migrates an existing model's legacy ``_thinking``/``_notthinking`` profiles to
native reasoning configs in place with ``--migrate-legacy-thinking`` (deterministic
1:1, whole-file by default; ``--upgrade-efforts`` opts into the probe-based
effort-tier upgrade).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.reasoning import (
    DEFAULT_THINKING_EFFORT,
    REASONING_EFFORTS,
    effort_style_label,
    profile_name_for,
    resolve_effort,
    scaled_max_tokens,
)

SYS_PROMPT = "You are a helpful assistant that responds briefly."
USER_PROMPT = "Reply with exactly the word 'ok'."
SCHEMA_VERSION = 2


def _load_candidates(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "profiles": {}}


def _save_candidates(path: Path, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _probe_json_schema(client, model: str) -> bool:
    """Return True if model accepts response_format json_schema with our schema.

    Sends payload in the same format as build_openrouter_request uses at runtime.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Respond using JSON format matching the provided schema.\n\n" + SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": 30,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "probe",
                "schema": {
                    "type": "object",
                    "properties": {
                        "test": {"type": "string"},
                    },
                    "required": ["test"],
                    "additionalProperties": False,
                },
            },
        },
    }
    try:
        resp = client.chat_completion(payload)
        content = getattr(resp, "summary_md", "") or getattr(resp, "raw_content", "") or ""
        if content is None or len(str(content).strip()) == 0:
            print(" [empty content]")
            return False
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _probe_legacy_thinking(client, model: str, thinking: bool) -> bool:
    """Return True if model accepts the legacy thinking extra_body setting."""
    extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": 20,
        "extra_body": extra_body,
    }
    try:
        client.chat_completion(payload)
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _probe_plain(client, model: str) -> bool:
    """Return True if the model accepts a plain request (no reasoning param)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": scaled_max_tokens(20, "none"),
    }
    try:
        client.chat_completion(payload)
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _probe_effort(client, model: str, effort: str, style: str) -> bool:
    """Return True if the model accepts one reasoning-effort config style.

    ``effort == "none"`` probes the canonical disable form
    ``reasoning={"enabled": false}`` (a ``_notthinking`` profile must mean
    forced-off, not provider default).
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": scaled_max_tokens(20, effort),
    }
    if effort == "none":
        payload["reasoning"] = {"enabled": False}
    elif style == "reasoning":
        payload["reasoning"] = {"effort": effort}
    else:
        payload["reasoning_effort"] = effort
    try:
        client.chat_completion(payload)
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _probe_efforts(client, model: str, style: str) -> Tuple[List[str], Optional[str]]:
    """Probe all effort tiers, returning (accepted efforts, none_style).

    ``none_style`` is ``"enabled_false"`` when the canonical
    ``reasoning={"enabled": false}`` form is accepted (a real ``_notthinking``
    profile is possible), ``"mandatory"`` when the model rejects the disabled
    form but accepts a plain request — meaning reasoning cannot be turned off,
    so ``"none"`` is **not** added to the accepted efforts and no
    ``_notthinking`` profile may be created — and ``None`` when neither the
    disabled form nor a plain request is accepted.
    """
    ok: List[str] = []
    none_style: Optional[str] = None
    for effort in REASONING_EFFORTS:
        print(f"    effort '{effort}' ({style})...", end=" ", flush=True)
        if effort == "none":
            if _probe_effort(client, model, effort, style=style):
                ok.append(effort)
                none_style = "enabled_false"
                print("ok (enabled_false)")
            elif _probe_plain(client, model):
                none_style = "mandatory"
                print("plain ok but reasoning is MANDATORY (enabled=false rejected); no _notthinking")
            else:
                none_style = None
                print("unsupported")
        elif _probe_effort(client, model, effort, style=style):
            ok.append(effort)
            print("ok")
        else:
            print("unsupported")
    return ok, none_style


def probe_effort_capabilities(model: str, *, probe_legacy: bool = False) -> Dict[str, Any]:
    """Probe schema support and effort tiers, returning a capabilities dict."""
    from core.openrouter_client import OpenRouterClient
    client = OpenRouterClient.from_env()

    print(f"  Probing JSON schema support for {model}...", end=" ", flush=True)
    schema_ok = _probe_json_schema(client, model)
    print()

    # Try the structured "reasoning" param style first; fall back to the
    # top-level "reasoning_effort" scalar if every structured probe is rejected.
    style = "reasoning"
    print(f"  Probing effort tiers ({style}):")
    efforts, none_style = _probe_efforts(client, model, style)
    if not efforts:
        style = "reasoning_effort"
        print(f"  Structured param rejected; probing effort tiers ({style}):")
        efforts, none_style = _probe_efforts(client, model, style)

    caps: Dict[str, Any] = {
        "schema": schema_ok,
        "efforts": efforts,
        "effort_style": style if efforts else None,
        "none_style": none_style,
        "thinking_param": None,
        "support_note": None,
    }
    if probe_legacy:
        print(f"  Probing legacy thinking param...", end=" ", flush=True)
        caps["thinking_param"] = _probe_legacy_thinking(client, model, thinking=True)
        print()
    if not efforts:
        caps["support_note"] = "Model failed all effort probes (including plain request)."

    try:
        info = client.fetch_models().get(model)
    except Exception:
        info = None
    if info is not None and getattr(info, "pricing", None):
        tiers = []
        for tier in info.pricing:
            tiers.append(
                {
                    "min_context": int(getattr(tier, "min_context", 0) or 0),
                    "input_cost_per_million": float(getattr(tier, "prompt", 0.0) or 0.0) * 1_000_000.0,
                    "output_cost_per_million": float(getattr(tier, "completion", 0.0) or 0.0) * 1_000_000.0,
                    "cached_input_cost_per_million": float(getattr(tier, "input_cache_read", 0.0) or 0.0) * 1_000_000.0,
                    "request_cost": float(getattr(tier, "request", 0.0) or 0.0),
                }
            )
        tiers.sort(key=lambda item: item["min_context"])
        print(
            "PRICING "
            + json.dumps(
                {
                    "context_length": int(getattr(info, "context_length", 0) or 0),
                    "tiers": tiers,
                },
                ensure_ascii=False,
            )
        )
    return caps


def _slug(model: str) -> str:
    """Convert 'provider/model' to 'provider-model' (e.g. 'deepseek/deepseek-v4-flash' -> 'deepseek-v4-flash')."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _stage_base(role: str) -> Dict[str, Any]:
    if role == "composer":
        return {
            "model": "",
            "temperature": 0.2,
            "seed": 42,
            "max_tokens": 8192,
            "format_mode": "markdown_sections",
            "context_mode": "chapter_plus_toc_and_meta",
            "prompt_components": {
                "system_style": "architectural_synthesizer",
                "synthesis_policy": "thesis_then_frameworks",
                "detail_policy": "balanced_dense",
                "qualifier_policy": "strict",
                "structure_policy": "theme_clustered",
                "terminology_policy": "keep_source_terms",
                "anti_fluff_policy": "hard",
            },
        }
    return {
        "model": "",
        "temperature": 0.2,
        "seed": 42,
        "max_tokens": 8192,
        "format_mode": "markdown_sections",
        "context_mode": "chapter_plus_toc_and_meta",
        "prompt_components": {
            "system_style": "dense_faithful",
            "detail_policy": "mechanisms_first",
            "qualifier_policy": "strict",
            "structure_policy": "heading_aware",
            "example_policy": "explanatory_only",
            "terminology_policy": "keep_source_terms",
            "anti_fluff_policy": "hard",
        },
    }


def _build_stage_config(
    model: str,
    effort: str,
    schema_ok: bool,
    provider: Optional[Dict[str, Any]] = None,
    *,
    style: str = "reasoning",
    role: str = "chapter",
    legacy: bool = False,
    none_style: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = _stage_base(role)
    cfg["model"] = model
    cfg["provider"] = provider
    cfg["use_json_schema"] = schema_ok
    if legacy:
        cfg["extra_body"] = {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
    else:
        cfg["max_tokens"] = scaled_max_tokens(8192, effort)
        if effort == "none":
            if none_style in ("plain", "mandatory"):
                raise ValueError(
                    f"Model {model} cannot disable reasoning (none_style={none_style!r}): "
                    "a _notthinking profile is impossible — the provider rejects "
                    "reasoning.enabled=false and a plain request still reasons at "
                    "default effort. Use an effort-tier profile instead."
                )
            cfg["reasoning"] = {"enabled": False}
        elif style == "reasoning":
            cfg["reasoning"] = {"effort": effort}
        else:
            cfg["reasoning_effort"] = effort
    return cfg


def _build_composer_config(
    model: str,
    effort: str,
    schema_ok: bool,
    provider: Optional[Dict[str, Any]] = None,
    *,
    style: str = "reasoning",
    legacy: bool = False,
    none_style: Optional[str] = None,
) -> Dict[str, Any]:
    return _build_stage_config(
        model, effort, schema_ok, provider, style=style, role="composer", legacy=legacy, none_style=none_style
    )


def _build_profile(
    model: str,
    effort: str,
    schema_ok: bool,
    time_budget: str = "30m",
    provider: Optional[Dict[str, Any]] = None,
    *,
    style: str = "reasoning",
    legacy: bool = False,
    none_style: Optional[str] = None,
) -> Dict[str, Any]:
    slug = _slug(model)
    full_profile = profile_name_for(time_budget, slug, effort)
    mode = "legacy-thinking" if legacy and effort != "none" else ("legacy-notthinking" if legacy else effort_style_label(effort))
    if effort == "none" and none_style in ("plain", "mandatory"):
        mode = "notthinking (effort=none, plain)"
    return {
        "name": f"{full_profile}_v1",
        "profile": full_profile,
        "chapter_stage": _build_stage_config(model, effort, schema_ok, provider, style=style, role="chapter", legacy=legacy, none_style=none_style),
        "composer_stage": _build_stage_config(model, effort, schema_ok, provider, style=style, role="composer", legacy=legacy, none_style=none_style),
        "composer_mode": "summaries_only",
        "length_control": {
            "max_passes": 5,
            "tolerance_pct": 0.08,
            "hard_tolerance_pct": 0.15,
            "repair_strategy": "edit_existing",
            "repair_more_prompt_id": "expand_mechanisms_first",
            "repair_less_prompt_id": "shrink_dedup_first",
        },
        "budget_allocator": {
            "words_per_minute": 200,
            "allocation_alpha": 0.90,
            "min_chapter_share": 0.03,
            "max_chapter_share": 0.18,
            "chapter_stage_multiplier_30m": 1.20,
            "chapter_stage_multiplier_60m": 1.00,
            "max_summary_to_source_ratio": 0.90,
        },
        "disable_composer": False,
        "notes": f"Auto-generated: {model} chapter+composer, {time_budget}, {mode} (effort={effort}), schema={schema_ok}",
    }


def _import_openrouter_client():
    from core.openrouter_client import OpenRouterClient
    return OpenRouterClient


def _resolve_efforts(caps: Dict[str, Any], requested: Optional[List[str]]) -> List[str]:
    supported = list(caps.get("efforts") or ())
    if requested:
        chosen: List[str] = []
        for r in requested:
            r = r.strip().lower()
            if r in supported:
                chosen.append(r)
            elif r == "none":
                # Disabling reasoning must never silently degrade to another
                # effort: that would create a phantom _notthinking profile that
                # reasons at default (or higher) effort.
                none_style = caps.get("none_style")
                if none_style in ("plain", "mandatory"):
                    raise ValueError(
                        f"Model {caps.get('model', 'model')} cannot disable reasoning "
                        f"(none_style={none_style!r}): refusing to create a _notthinking "
                        "profile. The provider rejects reasoning.enabled=false and a plain "
                        "request still reasons at default effort."
                    )
                raise ValueError(
                    f"Effort 'none' is unsupported by {caps.get('model', 'model')} "
                    f"(supported: {supported or 'none'}). Cannot create a _notthinking profile."
                )
            else:
                fallback = resolve_effort(r, supported)
                print(f"  Warning: effort '{r}' unsupported by {caps.get('model', 'model')}; using '{fallback}'")
                chosen.append(fallback)
        return chosen
    return supported


def run_probes(model: str) -> tuple[bool, bool, bool]:
    """Backward-compatible thin wrapper: legacy thinking/notthinking probes."""
    from core.openrouter_client import OpenRouterClient
    client = OpenRouterClient.from_env()
    print(f"  Probing JSON schema support for {model}...", end=" ", flush=True)
    schema_ok = _probe_json_schema(client, model)
    print()
    print(f"  Probing thinking mode...", end=" ", flush=True)
    thinking_ok = _probe_legacy_thinking(client, model, thinking=True)
    print()
    print(f"  Probing non-thinking mode...", end=" ", flush=True)
    notthinking_ok = _probe_legacy_thinking(client, model, thinking=False)
    print()
    return schema_ok, thinking_ok, notthinking_ok


def add_candidates(
    model: str,
    *,
    candidates_path: Path,
    dry_run: bool = False,
    dry_run_only: bool = False,
    time_budgets: Optional[List[str]] = None,
    provider: Optional[Dict[str, Any]] = None,
    efforts: Optional[List[str]] = None,
    probe_legacy: bool = False,
) -> List[str]:
    if time_budgets is None:
        time_budgets = ["30m", "60m"]

    print(f"\n=== Probing {model} ===")
    caps = probe_effort_capabilities(model, probe_legacy=probe_legacy)
    caps["model"] = model

    if not caps.get("efforts"):
        print(f"\nError: Model {model} failed all capability probes.")
        print("  efforts: none accepted (including plain request).")
        print("This model is not compatible with this harness.")
        sys.exit(1)

    chosen = _resolve_efforts(caps, efforts)
    print(f"  Resolved {len(chosen)} effort(s): {', '.join(chosen)}")
    if caps.get("none_style") in ("plain", "mandatory"):
        print(
            f"  Note: reasoning is MANDATORY for {model} (none_style={caps.get('none_style')!r}). "
            "No _notthinking profile is created: disabling reasoning is impossible for this model."
        )

    created: List[str] = []
    for tb in time_budgets:
        for effort in chosen:
            p = _build_profile(
                model,
                effort=effort,
                schema_ok=caps.get("schema", True),
                time_budget=tb,
                provider=provider,
                style=caps.get("effort_style", "reasoning"),
                none_style=caps.get("none_style"),
            )
            created.append(p["profile"])
            print(f"\n  → Will create profile: {p['profile']}  (effort={effort})")

    if dry_run_only:
        print("\n[Dry run] Would append these profiles to candidates JSON:")
        for name in created:
            print(f"  - {name}")
        return created

    if dry_run:
        print("\n[Dry run] Probes complete. Not writing to candidates JSON.")
        return created

    data = _load_candidates(candidates_path)
    if "profiles" not in data:
        data["profiles"] = {}
    data.setdefault("capabilities", {})[model] = {k: v for k, v in caps.items() if k != "model"}
    data["schema_version"] = SCHEMA_VERSION

    existing = set(data.get("profiles", {}).keys())
    added: List[str] = []
    skipped: List[str] = []

    for profile_name in created:
        if profile_name in existing:
            skipped.append(profile_name)
            print(f"  Skipped (already exists): {profile_name}")
        else:
            if profile_name.endswith("_notthinking"):
                effort = "none"
            elif "_effort-" in profile_name:
                effort = profile_name.rsplit("_effort-", 1)[-1]
            else:
                effort = DEFAULT_THINKING_EFFORT
            profile_data = _build_profile(
                model,
                effort=effort,
                schema_ok=caps.get("schema", True),
                time_budget=profile_name.split("_")[0],
                provider=provider,
                style=caps.get("effort_style", "reasoning"),
                none_style=caps.get("none_style"),
            )
            data["profiles"][profile_name] = profile_data
            added.append(profile_name)
            print(f"  Added: {profile_name}")

    _save_candidates(candidates_path, data)
    print(f"\nWrote {len(added)} new profile(s) to {candidates_path}")
    if skipped:
        print(f"Skipped {len(skipped)} existing profile(s)")

    if added:
        print()
        while True:
            response = input("Run 'python3 tools/gen_profile_literal.py' to update candidate_spec.py now? [y/n]: ").strip().lower()
            if response == "y":
                import subprocess
                result = subprocess.run(["python3", "tools/gen_profile_literal.py"], capture_output=False)
                if result.returncode != 0:
                    print("gen_profile_literal.py failed.")
                break
            elif response == "n":
                print("You can run 'python3 tools/gen_profile_literal.py' later to update candidate_spec.py.")
                break
            else:
                print("Please enter 'y' or 'n'.")

    return added


def _rewrite_legacy_profile(profile: Dict[str, Any], reasoning: Optional[Dict[str, Any]], model: str) -> None:
    """Mutate a profile's stages in place to native reasoning config.

    Drops ``extra_body`` (legacy thinking wrapper), sets the given
    ``reasoning`` dict on both stages, and updates ``notes``. ``reasoning=None``
    emits a plain stage (no reasoning config) — used when the model cannot
    disable reasoning (``none_style="mandatory"``); callers should raise instead.
    All other fields (schema, provider, hidden, budget allocator, …) are
    preserved. ``model`` is the full model id used in the notes line.
    """
    for key in ("chapter_stage", "composer_stage"):
        stage = profile.get(key)
        if not isinstance(stage, dict):
            continue
        stage.pop("extra_body", None)
        stage.pop("reasoning_effort", None)
        if reasoning is None:
            stage.pop("reasoning", None)
        else:
            stage["reasoning"] = reasoning
    tb = profile.get("profile", "30m").split("_", 1)[0]
    if reasoning is None:
        label = "notthinking (effort=none, plain)"
    elif reasoning.get("enabled") is False:
        label = "notthinking (effort=none)"
    elif reasoning.get("enabled") is True:
        label = "thinking (enabled=true)"
    else:
        label = f"effort-{reasoning.get('effort')}"
    schema_ok = bool(profile.get("chapter_stage", {}).get("use_json_schema", True))
    profile["notes"] = (
        f"Auto-generated: {model} chapter+composer, {tb}, {label}, schema={schema_ok}"
    )


def _none_style_for(model: str, data: Dict[str, Any]) -> Optional[str]:
    """Read the probe-confirmed none emission style for a model from capabilities."""
    caps = (data.get("capabilities") or {}).get(model) or {}
    return caps.get("none_style") or "enabled_false"


def _legacy_native_reasoning(name: str, thinking_cfg: Any) -> Dict[str, Any]:
    """Map a legacy profile name + thinking config to its native reasoning dict.

    ``_effort-<name>`` profiles carry effort-scaled ``max_tokens`` and a stale
    ``extra_body.thinking.enabled``; the name's effort tier is authoritative.
    Otherwise ``_thinking``/``_notthinking`` map to ``reasoning.enabled``.
    """
    effort_match = re.match(r"^.*_effort-([a-z]+)$", name)
    if effort_match:
        return {"effort": effort_match.group(1)}
    if thinking_cfg is not None:
        return {"enabled": thinking_cfg.get("type") == "enabled"}
    return {"enabled": name.endswith("_thinking")}


def _family_missing_none(profiles: Dict[str, Any]) -> List[str]:
    """Return family keys (``{tb}_{slug}``) that lack a ``_notthinking`` profile."""
    families: Dict[str, List[str]] = {}
    for key in profiles:
        fam = key.rsplit("_", 1)[0]
        families.setdefault(fam, []).append(key)
    return sorted(fam for fam, keys in families.items() if not any(k.endswith("_notthinking") for k in keys))


def migrate_legacy_thinking(
    model: Optional[str] = None,
    *,
    candidates_path: Path,
    time_budgets: Optional[List[str]] = None,
    provider: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    one_to_one: bool = True,
    add_missing_none: bool = True,
    effort: str = DEFAULT_THINKING_EFFORT,
) -> List[str]:
    """Migrate legacy ``_thinking``/``_notthinking`` profiles to native reasoning.

    ``one_to_one=True`` (default) is a deterministic, network-free transform:
    profile keys are unchanged, ``_thinking`` maps to ``reasoning.enabled=true``
    and ``_notthinking``/plain ``_notthinking`` to ``enabled=false``. When
    ``model`` is None it runs over the whole file. ``add_missing_none`` also
    adds a ``_notthinking`` sibling for every family that lacks one.

    ``one_to_one=False`` keeps the old probe-based upgrade: it probes the model,
    maps ``_thinking`` to an effort tier, and renames the profile (opt-in via
    ``--upgrade-efforts``).
    """
    if time_budgets is None:
        time_budgets = ["30m", "60m"]

    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})

    if one_to_one:
        slug = _slug(model) if model else None
        rewrites: List[str] = []
        targets = []
        for name in sorted(profiles):
            if slug and not name.startswith(f"30m_{slug}") and not name.startswith(f"60m_{slug}"):
                continue
            ch = profiles[name].get("chapter_stage") or {}
            thinking_cfg = (ch.get("extra_body") or {}).get("thinking") if isinstance(ch.get("extra_body"), dict) else None
            if thinking_cfg is not None:
                targets.append((name, thinking_cfg))
            elif isinstance(ch.get("reasoning"), dict) or ch.get("reasoning_effort"):
                continue  # already native, skip
            elif name.endswith("_thinking") or name.endswith("_notthinking") or "_effort-" in name:
                targets.append((name, None))
            else:
                continue  # already-native sibling of a different effort, no extra_body

        for name, thinking_cfg in targets:
            p = profiles[name]
            full_model = (p.get("chapter_stage") or {}).get("model") or (model or f"unknown/{name}")
            reasoning = _legacy_native_reasoning(name, thinking_cfg)
            none_style = _none_style_for(full_model, data)
            if reasoning == {"enabled": False} and none_style in ("plain", "mandatory"):
                raise ValueError(
                    f"Cannot rewrite {name}: model {full_model} cannot disable reasoning "
                    f"(none_style={none_style!r}). The provider rejects "
                    "reasoning.enabled=false and a plain request still reasons at default "
                    "effort. Refusing to create/keep a phantom _notthinking profile; "
                    "remove it instead."
                )
            _rewrite_legacy_profile(p, reasoning=reasoning, model=full_model)
            rewrites.append(f"{name}->{p['notes']}")
            print(f"  Rewrote: {name} (reasoning={reasoning})")

        added: List[str] = []
        if add_missing_none:
            for fam in _family_missing_none(profiles):
                tb, slug = fam.split("_", 1)
                sibling = next((profiles[k] for k in profiles if k.startswith(f"{tb}_{slug}_")), None)
                if sibling is None:
                    continue
                ch = sibling.get("chapter_stage") or {}
                full_model = ch.get("model") or f"unknown/{slug}"
                schema_ok = bool(ch.get("use_json_schema", True))
                style = "reasoning" if isinstance(ch.get("reasoning"), dict) else "reasoning_effort"
                p = _build_profile(
                    full_model,
                    effort="none",
                    schema_ok=schema_ok,
                    time_budget=tb,
                    provider=provider,
                    style=style,
                    none_style=_none_style_for(full_model, data),
                )
                key = p["profile"]
                profiles[key] = p
                added.append(key)
                print(f"  Added:   {key} ({'plain' if _none_style_for(full_model, data)=='plain' else 'reasoning.enabled=false'})")

        if dry_run:
            print(f"\n[dry-run] No changes written ({len(rewrites)} rewrites, {len(added)} additions).")
            return rewrites + added

        data["profiles"] = profiles
        data["schema_version"] = SCHEMA_VERSION
        _save_candidates(candidates_path, data)
        print(f"\nWrote {len(rewrites)} rewritten + {len(added)} added profile(s) to {candidates_path}")
        return rewrites + added

    # ---- legacy probe-based upgrade path (--upgrade-efforts) ----
    if model is None:
        raise ValueError("--upgrade-efforts requires --model-full or --provider/--model")

    slug = _slug(model)
    targets: List[str] = []
    for tb in time_budgets:
        for legacy_name, target_effort in (
            (f"{tb}_{slug}_thinking", effort),
            (f"{tb}_{slug}_notthinking", "none"),
        ):
            if legacy_name in profiles:
                targets.append((legacy_name, target_effort))

    if not targets:
        print(f"No legacy profiles found for {model} ({slug}).")
        return []

    print(f"\nMigrating {len(targets)} legacy profile(s) for {model} -> new-style reasoning:")
    caps = probe_effort_capabilities(model, probe_legacy=True)
    caps["model"] = model

    rewrites: List[str] = []
    for name, target_effort in targets:
        supported_efforts = caps.get("efforts") or ()
        if target_effort == "none" and "none" not in supported_efforts:
            none_style = caps.get("none_style")
            raise ValueError(
                f"Cannot migrate {name}: model {model} cannot disable reasoning "
                f"(none_style={none_style!r}). The provider rejects "
                "reasoning.enabled=false and a plain request still reasons. "
                "Refusing to create a phantom _notthinking profile."
            )
        resolved = resolve_effort(target_effort, supported_efforts)
        if resolved != target_effort:
            print(f"  {name}: requested effort '{target_effort}' unsupported; using '{resolved}'")
        profiles[name] = _build_profile(
            model,
            effort=resolved,
            schema_ok=caps.get("schema", True),
            time_budget=name.split("_")[0],
            provider=provider,
            style=caps.get("effort_style", "reasoning"),
            none_style=caps.get("none_style"),
        )
        rewrites.append(f"{name}->{profiles[name]['notes']}")
        print(f"  Rewrote: {name} (effort={resolved}, style={caps.get('effort_style')})")

    if dry_run:
        print("\n[dry-run] No changes written.")
        return rewrites

    data["profiles"] = profiles
    data.setdefault("capabilities", {})[model] = {k: v for k, v in caps.items() if k != "model"}
    data["schema_version"] = SCHEMA_VERSION
    _save_candidates(candidates_path, data)
    print(f"\nWrote {len(rewrites)} rewritten profile(s) to {candidates_path}")
    return rewrites


def list_profiles(candidates_path: Path) -> None:
    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})
    if not profiles:
        print("No profiles found.")
        return
    print(f"{len(profiles)} profiles in {candidates_path}:")
    for name in sorted(profiles.keys()):
        p = profiles[name]
        notes = p.get("notes", "")
        print(f"  {name}: {notes}")


def _find_runs_by_pattern(pattern: str, runs_dir: Path) -> List[Path]:
    import re
    run_files: List[Path] = []
    compiled = re.compile(pattern)
    benchmarks = [d for d in runs_dir.iterdir() if d.is_dir()] if runs_dir.exists() else []
    for benchmark_dir in benchmarks:
        for run_file in benchmark_dir.iterdir():
            if not run_file.is_file():
                continue
            if compiled.search(run_file.name):
                run_files.append(run_file)
    return run_files


_RUN_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _run_id_of_run_file(name: str) -> Optional[str]:
    """Base run-id family key for a run artifact filename.

    A run family is keyed by the manifest run id (e.g. ``<ts>__<bench>__<tv>__<profile>__<candidate>_v1``).
    Judge duplicates append ``__llmj_<judge>`` to that stem; this strips the judge suffix and the
    extension so a matched run id can be searched for in job logs and results.tsv.
    """
    base = name
    for ext in (".samples.jsonl", ".state.json", ".json"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    if "__llmj_" in base:
        base = base.split("__llmj_", 1)[0]
    if not _RUN_ID_TOKEN_RE.match(base):
        return None
    return base


def _find_run_files_for_key(profile_key: str, runs_dir: Path) -> List[Path]:
    """Run artifacts whose run id embeds ``__<profile_key>_v`` (fixed-string match)."""
    marker = f"__{profile_key}_v"
    run_files: List[Path] = []
    benchmarks = [d for d in runs_dir.iterdir() if d.is_dir()] if runs_dir.exists() else []
    for benchmark_dir in benchmarks:
        for run_file in benchmark_dir.iterdir():
            if not run_file.is_file():
                continue
            if marker in run_file.name:
                run_files.append(run_file)
    return run_files


def _collect_run_ids(run_files: List[Path]) -> set:
    run_ids: set = set()
    for rf in run_files:
        rid = _run_id_of_run_file(rf.name)
        if rid:
            run_ids.add(rid)
    return run_ids


def _scan_logs_for_run_ids(run_ids: set, jobs_dir: Path) -> tuple[list, list]:
    """Split matching job logs into (deletable, active).

    A log is deletable when it references one of the run ids AND carries a
    ``[job] finished`` marker. Logs without that marker may belong to a still
    running job and are never deleted (``[job] meta`` lines store no args).
    """
    if not run_ids or not jobs_dir.exists():
        return [], []
    token_re = re.compile(r"\bRun ID:\s*([A-Za-z0-9_.\-]+)")
    deletable: List[Path] = []
    active: List[Path] = []
    for log in sorted(jobs_dir.glob("*.log")):
        try:
            content = log.read_text(errors="replace")
        except Exception:
            continue
        if not any(m.group(1) in run_ids for m in token_re.finditer(content)):
            continue
        if "[job] finished" in content:
            deletable.append(log)
        else:
            active.append(log)
    return deletable, active


def _drop_results_rows(results_path: Path, run_ids: set, dry_run: bool) -> int:
    """Remove results.tsv rows whose run_id belongs to a deleted run family.

    Rewrite is atomic (write temp + os.replace). Returns the dropped count.
    """
    if not run_ids or not results_path.exists():
        return 0
    try:
        lines = results_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    if not lines:
        return 0
    keep = [lines[0]]
    dropped = 0
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) > 1 and cols[1] in run_ids:
            dropped += 1
        else:
            keep.append(line)
    if dropped and not dry_run:
        tmp = results_path.with_name(results_path.name + ".tmp")
        content = "\n".join(keep) + ("\n" if keep else "")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(results_path)
    return dropped


def remove_profile_cascade(
    profile_key: str,
    *,
    candidates_path: Path,
    runs_dir: Path = Path("runs"),
    jobs_dir: Path = Path("artifacts/jobs"),
    results_tsv: Path = Path("results.tsv"),
    dry_run: bool = False,
    remove_profile: bool = True,
) -> Dict[str, Any]:
    """Delete one profile (exact key) plus all of its run artifacts, job logs,
    and results.tsv rows. ``--dry-run`` prints the same summary without writing."""
    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})
    in_profiles = profile_key in profiles

    run_files = _find_run_files_for_key(profile_key, runs_dir)
    run_ids = _collect_run_ids(run_files) if run_files else set()
    deletable_logs, active_logs = _scan_logs_for_run_ids(run_ids, jobs_dir)
    rows = _drop_results_rows(results_tsv, run_ids, dry_run)

    summary = {
        "profile_key": profile_key,
        "run_files": [str(p) for p in run_files],
        "logs": [str(p) for p in deletable_logs],
        "active_logs": [str(p) for p in active_logs],
        "result_rows": rows,
        "removed_profile": in_profiles,
    }

    if not in_profiles and not run_files:
        print(f"No profiles or runs match profile key: {profile_key}")
        return summary

    if in_profiles:
        print(f"Removing 1 profile(s):")
        print(f"  - {profile_key}")

    if run_files:
        print(f"Found {len(run_files)} run file(s) matching '{profile_key}':")
        for rf in run_files:
            print(f"  - {rf}")

    if deletable_logs:
        print(f"Found {len(deletable_logs)} job log(s)")
    if active_logs:
        print(f"Skipped {len(active_logs)} active job log(s):")
        for lg in active_logs:
            print(f"  - {lg}")
    if rows:
        print(f"Found {rows} results.tsv row(s)")

    if dry_run:
        print("(dry-run — no changes written)")
        return summary

    if in_profiles and remove_profile:
        del profiles[profile_key]
        data["profiles"] = profiles
        _save_candidates(candidates_path, data)
        from tools.gen_profile_literal import update_candidate_spec_py
        update_candidate_spec_py(profiles)
        print("Regenerated candidate_spec.py")

    if run_files:
        for rf in run_files:
            rf.unlink()
        print(f"Removed {len(run_files)} run file(s)")
    if deletable_logs:
        for lg in deletable_logs:
            lg.unlink()
        print(f"Removed {len(deletable_logs)} job log(s)")
    if rows:
        print(f"Removed {rows} results.tsv row(s)")

    return summary


def _find_runs_for_profiles(profile_names: List[str], runs_dir: Path) -> List[Path]:
    run_files: List[Path] = []
    benchmarks = [d for d in runs_dir.iterdir() if d.is_dir()] if runs_dir.exists() else []
    for benchmark_dir in benchmarks:
        for run_file in benchmark_dir.iterdir():
            if not run_file.is_file():
                continue
            stem = run_file.stem
            for profile_name in profile_names:
                if profile_name in stem:
                    run_files.append(run_file)
                    break
    return run_files


def remove_candidates(
    pattern: str,
    *,
    candidates_path: Path,
    runs_dir: Path = Path("runs"),
    jobs_dir: Path = Path("artifacts/jobs"),
    results_tsv: Path = Path("results.tsv"),
    dry_run: bool = False,
) -> List[str]:
    import re
    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})
    compiled = re.compile(pattern)
    matched = [k for k in profiles if compiled.search(k)]

    run_files = _find_runs_by_pattern(pattern, runs_dir)
    run_ids = _collect_run_ids(run_files) if run_files else set()
    deletable_logs, active_logs = _scan_logs_for_run_ids(run_ids, jobs_dir)
    rows = _drop_results_rows(results_tsv, run_ids, dry_run)

    if not matched and not run_files:
        print(f"No profiles or runs match pattern: {pattern}")
        return []

    if matched:
        print(f"Removing {len(matched)} profile(s) matching '{pattern}':")
        for name in sorted(matched):
            print(f"  - {name}")

    if run_files:
        print(f"Found {len(run_files)} run file(s) matching '{pattern}':")
        for rf in run_files:
            print(f"  - {rf}")

    if deletable_logs:
        print(f"Found {len(deletable_logs)} job log(s)")
    if active_logs:
        print(f"Skipped {len(active_logs)} active job log(s):")
        for lg in active_logs:
            print(f"  - {lg}")
    if rows:
        print(f"Found {rows} results.tsv row(s)")

    if dry_run:
        print("(dry-run — no changes written)")
        return matched

    for name in matched:
        del profiles[name]
    data["profiles"] = profiles
    candidates_path.write_text(json.dumps(data, indent=2) + "\n")

    if run_files:
        for rf in run_files:
            rf.unlink()
        print(f"Removed {len(run_files)} run file(s)")
    if deletable_logs:
        for lg in deletable_logs:
            lg.unlink()
        print(f"Removed {len(deletable_logs)} job log(s)")
    if rows:
        print(f"Removed {rows} results.tsv row(s)")

    if matched:
        from tools.gen_profile_literal import update_candidate_spec_py
        update_candidate_spec_py(profiles)
        print(f"Regenerated candidate_spec.py")
    return matched


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-probe a model and add candidate profile(s)")
    parser.add_argument("--provider", help="Provider slug (e.g., deepseek, xai)")
    parser.add_argument("--provider-route", help="Provider routing config as JSON (e.g., '{\"order\":[\"deepseek\"]}')")
    parser.add_argument("--model", help="Model slug (e.g., deepseek-v4-flash)")
    parser.add_argument("--model-full", help="Full model ID (e.g., deepseek/deepseek-v4-flash), overrides --provider/--model")
    parser.add_argument(
        "--time-budget",
        nargs="+",
        default=["30m", "60m"],
        help="Time budget(s) to create profiles for (default: 30m 60m). Use '30m' or '60m' or both.",
    )
    parser.add_argument("--out", type=Path, default=Path("data/candidates.json"),
                        help="Path to candidates JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be created without writing")
    parser.add_argument("--list", action="store_true",
                        help="List all profiles in candidates JSON")
    parser.add_argument("--remove", metavar="PATTERN", help="Remove profiles matching regex pattern")
    parser.add_argument("--remove-profile", metavar="KEY",
                        help="Remove a single profile by exact key plus all of its run artifacts, "
                             "referencing job logs, and results.tsv rows")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"),
                        help="Runs root directory (default: runs)")
    parser.add_argument("--jobs-dir", type=Path, default=Path("artifacts/jobs"),
                        help="Dashboard job log directory (default: artifacts/jobs)")
    parser.add_argument("--results-tsv", type=Path, default=Path("results.tsv"),
                        help="Results table path (default: results.tsv)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Timeout per probe call in seconds")
    parser.add_argument("--efforts", nargs="+", default=None,
                        help="Restrict created profiles to these effort tiers "
                             "(e.g. 'none medium max'). Default: all supported.")
    parser.add_argument("--probe-legacy", action="store_true",
                        help="Also probe the legacy extra_body thinking param and record it in capabilities")
    parser.add_argument("--migrate-legacy-thinking", action="store_true",
                        help="Rewrite legacy _thinking/_notthinking profiles to native reasoning "
                             "configs (deterministic 1:1, no probing; whole file when --model omitted). "
                             "Use --upgrade-efforts for the probe-based effort-tier migration.")
    parser.add_argument("--upgrade-efforts", action="store_true",
                        help="With --migrate-legacy-thinking: probe the model and migrate _thinking to "
                             "an effort tier (renames the profile). Requires --model.")
    parser.add_argument("--no-add-missing-none", action="store_true",
                        help="With --migrate-legacy-thinking: do not add missing _notthinking profiles")
    args = parser.parse_args()

    if args.list:
        list_profiles(args.out)
        return

    if args.remove_profile is not None:
        remove_profile_cascade(
            args.remove_profile,
            candidates_path=args.out,
            runs_dir=args.runs_dir,
            jobs_dir=args.jobs_dir,
            results_tsv=args.results_tsv,
            dry_run=args.dry_run,
        )
        return

    if args.remove is not None:
        remove_candidates(
            args.remove,
            candidates_path=args.out,
            runs_dir=args.runs_dir,
            jobs_dir=args.jobs_dir,
            results_tsv=args.results_tsv,
            dry_run=args.dry_run,
        )
        return

    if args.migrate_legacy_thinking:
        if args.upgrade_efforts and not args.model_full and not (args.provider and args.model):
            parser.error("--upgrade-efforts requires --model-full or --provider/--model")
        model = args.model_full or (f"{args.provider}/{args.model}" if args.provider and args.model else None)
        provider = None
        if args.provider_route:
            try:
                provider = json.loads(args.provider_route)
            except Exception:
                print(f"Warning: failed to parse --provider-route as JSON: {args.provider_route}")
        migrate_legacy_thinking(
            model,
            candidates_path=args.out,
            time_budgets=args.time_budget,
            provider=provider,
            dry_run=args.dry_run,
            one_to_one=not args.upgrade_efforts,
            add_missing_none=not args.no_add_missing_none,
        )
        return

    if not args.model_full and not (args.provider and args.model):
        parser.error("Either --model-full or both --provider and --model are required")

    if args.model_full:
        model = args.model_full
    else:
        model = f"{args.provider}/{args.model}"

    provider = None
    if args.provider_route:
        import json as _json
        try:
            provider = _json.loads(args.provider_route)
        except Exception:
            print(f"Warning: failed to parse --provider-route as JSON: {args.provider_route}")

    add_candidates(
        model,
        candidates_path=args.out,
        dry_run=args.dry_run,
        dry_run_only=args.dry_run,
        time_budgets=args.time_budget,
        provider=provider,
        efforts=args.efforts,
        probe_legacy=args.probe_legacy,
    )


if __name__ == "__main__":
    main()