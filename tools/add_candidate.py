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
new-style reasoning configs in place with ``--migrate-legacy-thinking``.
"""

from __future__ import annotations

import argparse
import json
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


def _probe_effort(client, model: str, effort: str, style: str) -> bool:
    """Return True if the model accepts one reasoning-effort config style.

    ``effort == "none"`` probes a plain request (no reasoning param) — the
    highest-compatibility baseline every reachable model must pass.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": scaled_max_tokens(20, effort),
    }
    if effort != "none":
        if style == "reasoning":
            payload["reasoning"] = {"effort": effort}
        else:
            payload["reasoning_effort"] = effort
    try:
        client.chat_completion(payload)
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _probe_efforts(client, model: str, style: str) -> List[str]:
    ok: List[str] = []
    for effort in REASONING_EFFORTS:
        print(f"    effort '{effort}' ({style})...", end=" ", flush=True)
        if _probe_effort(client, model, effort, style=style):
            ok.append(effort)
            print("ok")
        else:
            print("unsupported")
    return ok


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
    efforts = _probe_efforts(client, model, style)
    if not efforts:
        style = "reasoning_effort"
        print(f"  Structured param rejected; probing effort tiers ({style}):")
        efforts = _probe_efforts(client, model, style)

    caps: Dict[str, Any] = {
        "schema": schema_ok,
        "efforts": efforts,
        "effort_style": style if efforts else None,
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
) -> Dict[str, Any]:
    cfg = _stage_base(role)
    cfg["model"] = model
    cfg["provider"] = provider
    cfg["use_json_schema"] = schema_ok
    if legacy:
        cfg["extra_body"] = {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
    else:
        cfg["max_tokens"] = scaled_max_tokens(8192, effort)
        if effort != "none":
            if style == "reasoning":
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
) -> Dict[str, Any]:
    return _build_stage_config(
        model, effort, schema_ok, provider, style=style, role="composer", legacy=legacy
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
) -> Dict[str, Any]:
    slug = _slug(model)
    full_profile = profile_name_for(time_budget, slug, effort)
    mode = "legacy-thinking" if legacy and effort != "none" else ("legacy-notthinking" if legacy else effort_style_label(effort))
    return {
        "name": f"{full_profile}_v1",
        "profile": full_profile,
        "chapter_stage": _build_stage_config(model, effort, schema_ok, provider, style=style, role="chapter", legacy=legacy),
        "composer_stage": _build_stage_config(model, effort, schema_ok, provider, style=style, role="composer", legacy=legacy),
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


def migrate_legacy_thinking(
    model: str,
    *,
    candidates_path: Path,
    time_budgets: Optional[List[str]] = None,
    provider: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    effort: str = DEFAULT_THINKING_EFFORT,
) -> List[str]:
    """Rewrite a model's legacy ``_thinking``/``_notthinking`` profiles to
    new-style reasoning configs in place."""
    if time_budgets is None:
        time_budgets = ["30m", "60m"]

    slug = _slug(model)
    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})

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
        resolved = resolve_effort(target_effort, caps.get("efforts") or ())
        if resolved != target_effort:
            print(f"  {name}: requested effort '{target_effort}' unsupported; using '{resolved}'")
        profiles[name] = _build_profile(
            model,
            effort=resolved,
            schema_ok=caps.get("schema", True),
            time_budget=name.split("_")[0],
            provider=provider,
            style=caps.get("effort_style", "reasoning"),
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
    dry_run: bool = False,
) -> List[str]:
    import re
    data = _load_candidates(candidates_path)
    profiles = data.get("profiles", {})
    compiled = re.compile(pattern)
    matched = [k for k in profiles if compiled.search(k)]

    run_files = _find_runs_by_pattern(pattern, runs_dir)

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
    parser.add_argument("--timeout", type=int, default=60,
                        help="Timeout per probe call in seconds")
    parser.add_argument("--efforts", nargs="+", default=None,
                        help="Restrict created profiles to these effort tiers "
                             "(e.g. 'none medium max'). Default: all supported.")
    parser.add_argument("--probe-legacy", action="store_true",
                        help="Also probe the legacy extra_body thinking param and record it in capabilities")
    parser.add_argument("--migrate-legacy-thinking", action="store_true",
                        help="Rewrite existing _thinking/_notthinking profiles for this model to "
                             "new-style reasoning configs in place (probes the model, does not add profiles)")
    args = parser.parse_args()

    if args.list:
        list_profiles(args.out)
        return

    if args.remove is not None:
        remove_candidates(
            args.remove,
            candidates_path=args.out,
            dry_run=args.dry_run,
        )
        return

    if args.migrate_legacy_thinking:
        if not args.model_full and not (args.provider and args.model):
            parser.error("--migrate-legacy-thinking requires --model-full or --provider/--model")
        model = args.model_full or f"{args.provider}/{args.model}"
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