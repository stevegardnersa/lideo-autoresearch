#!/usr/bin/env python3
"""
Auto-probe a model for capability compatibility and append candidate profile(s)
to data/candidates.json.

Probes:
  1. JSON schema support   (use_json_schema=True)
  2. Thinking mode         (extra_body={"thinking": {"type": "enabled"}})
  3. Non-thinking mode     (extra_body={"thinking": {"type": "disabled"}})

Creates one or two profiles (thinking + notthinking variants) based on results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SYS_PROMPT = "You are a helpful assistant that responds briefly."
USER_PROMPT = "Reply with exactly the word 'ok'."


def _load_candidates(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "profiles": {}}


def _save_candidates(path: Path, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _probe_json_schema(client, model: str) -> bool:
    """Return True if model accepts use_json_schema=True with our schema."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": 30,
        "use_json_schema": True,
        "json_schema": {
            "name": "summary_response_30m",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "test": {"type": "string"},
                },
                "required": ["test"],
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


def _probe_thinking(client, model: str, thinking: bool) -> bool:
    """Return True if model accepts the thinking extra_body setting."""
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
        resp = client.chat_completion(payload)
        return True
    except Exception as e:
        print(f" [exception: {str(e)[:100]}]")
        return False


def _slug(model: str) -> str:
    """Convert 'provider/model' to 'provider-model' (e.g. 'deepseek/deepseek-v4-flash' -> 'deepseek-v4-flash')."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _build_stage_config(model: str, thinking: bool, schema_ok: bool) -> Dict[str, Any]:
    extra_body = {"thinking": {"type": "enabled"}} if thinking else {"thinking": {"type": "disabled"}}
    return {
        "model": model,
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
        "extra_body": extra_body,
        "use_json_schema": schema_ok,
    }


def _build_composer_config(model: str, thinking: bool, schema_ok: bool) -> Dict[str, Any]:
    extra_body = {"thinking": {"type": "enabled"}} if thinking else {"thinking": {"type": "disabled"}}
    return {
        "model": model,
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
        "extra_body": extra_body,
        "use_json_schema": schema_ok,
    }


def _build_profile(model: str, thinking: bool, schema_ok: bool) -> Dict[str, Any]:
    mode = "thinking" if thinking else "notthinking"
    slug = _slug(model)
    full_profile = f"30m_{slug}_{mode}"
    return {
        "name": f"{full_profile}_v1",
        "profile": full_profile,
        "chapter_stage": _build_stage_config(model, thinking, schema_ok),
        "composer_stage": _build_composer_config(model, thinking, schema_ok),
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
        "scoring_gates_override": {
            "min_faithfulness": 0.20,
            "min_concept_coverage": 0.10,
        },
        "disable_composer": False,
        "notes": f"Auto-generated: {model} chapter+composer, {mode}, schema={schema_ok}",
    }


def _import_openrouter_client():
    from core.openrouter_client import OpenRouterClient
    return OpenRouterClient


def run_probes(model: str) -> tuple[bool, bool, bool]:
    OpenRouterClient = _import_openrouter_client()
    client = OpenRouterClient.from_env()

    print(f"  Probing JSON schema support for {model}...", end=" ", flush=True)
    schema_ok = _probe_json_schema(client, model)
    print()

    print(f"  Probing thinking mode...", end=" ", flush=True)
    thinking_ok = _probe_thinking(client, model, thinking=True)
    print()

    print(f"  Probing non-thinking mode...", end=" ", flush=True)
    notthinking_ok = _probe_thinking(client, model, thinking=False)
    print()

    return schema_ok, thinking_ok, notthinking_ok


def add_candidates(
    model: str,
    *,
    candidates_path: Path,
    dry_run: bool = False,
    dry_run_only: bool = False,
) -> List[str]:
    print(f"\n=== Probing {model} ===")
    schema_ok, thinking_ok, notthinking_ok = run_probes(model)

    created: List[str] = []

    if not thinking_ok and not notthinking_ok:
        print(f"\nError: Model {model} failed all capability probes.")
        print("  thinking: ✗  non-thinking: ✗")
        print("This model is not compatible with this harness.")
        sys.exit(1)

    if thinking_ok:
        p = _build_profile(model, thinking=True, schema_ok=schema_ok)
        created.append(p["profile"])
        print(f"\n  → Will create profile: {p['profile']}")

    if notthinking_ok:
        p = _build_profile(model, thinking=False, schema_ok=schema_ok)
        created.append(p["profile"])
        print(f"\n  → Will create profile: {p['profile']}")

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

    for profile_name in created:
        profile_data = _build_profile(
            model,
            thinking=("notthinking" not in profile_name),
            schema_ok=schema_ok,
        )
        data["profiles"][profile_name] = profile_data
        print(f"  Added: {profile_name}")

    _save_candidates(candidates_path, data)
    print(f"\nWrote {len(created)} profile(s) to {candidates_path}")
    return created


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-probe a model and add candidate profile(s)")
    parser.add_argument("--provider", help="Provider slug (e.g., deepseek, xai)")
    parser.add_argument("--model", help="Model slug (e.g., deepseek-v4-flash)")
    parser.add_argument("--model-full", help="Full model ID (e.g., deepseek/deepseek-v4-flash), overrides --provider/--model")
    parser.add_argument("--out", type=Path, default=Path("data/candidates.json"),
                        help="Path to candidates JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be created without writing")
    parser.add_argument("--list", action="store_true",
                        help="List all profiles in candidates JSON")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Timeout per probe call in seconds")
    args = parser.parse_args()

    if args.list:
        list_profiles(args.out)
        return

    if not args.model_full and not (args.provider and args.model):
        parser.error("Either --model-full or both --provider and --model are required")

    if args.model_full:
        model = args.model_full
    else:
        model = f"{args.provider}/{args.model}"

    add_candidates(
        model,
        candidates_path=args.out,
        dry_run=args.dry_run,
        dry_run_only=args.dry_run,
    )


if __name__ == "__main__":
    main()