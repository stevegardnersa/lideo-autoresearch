from __future__ import annotations

from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple


DEFAULT_BENCHMARK_MANIFEST: Dict[str, Any] = {
    "benchmark_version": "booksum-v2",
    "benchmark_label": "nonfiction-book-summary-genre-aware",
    "created_at_utc": "2026-04-19T00:00:00Z",
    "corpus_version": "corpus-2026-04-19",
    "rubric_version": "rubrics-v1",
    "scoring_version": "scoring-v1",
    "judge_version": "judge-absolute-v1",
    "split_seed": 42,
    "words_per_minute": 200,
    "notes": (
        "Genre-aware benchmark version. Increment benchmark_version whenever the corpus, split membership logic, "
        "rubric builder, scoring rules, judge prompt/model, logging schema, or visible-word-count behavior changes."
    ),
}


REQUIRED_MANIFEST_KEYS = (
    "benchmark_version",
    "corpus_version",
    "rubric_version",
    "scoring_version",
    "judge_version",
    "split_seed",
)


def stable_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(payload: Any) -> str:
    return sha256_text(stable_json_dumps(payload))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitize_slug(text: str, *, limit: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-._")
    return (slug[:limit] or "item").strip("-._") or "item"


def load_benchmark_manifest(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Benchmark manifest not found: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Benchmark manifest must be a JSON object: {path}")
    for key, value in DEFAULT_BENCHMARK_MANIFEST.items():
        manifest.setdefault(key, value)
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise ValueError(f"Benchmark manifest missing required keys {missing}: {path}")
    return manifest


def ensure_default_benchmark_manifest(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_BENCHMARK_MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def derive_price_snapshot_from_catalog(catalog: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    for model_id, info in catalog.items():
        pricing = getattr(info, "pricing", ()) or ()
        tiers = []
        for tier in pricing:
            tiers.append(
                {
                    "min_context": int(getattr(tier, "min_context", 0) or 0),
                    "input_cost_per_million": float(getattr(tier, "prompt", 0.0) or 0.0) * 1_000_000.0,
                    "output_cost_per_million": float(getattr(tier, "completion", 0.0) or 0.0) * 1_000_000.0,
                    "cached_input_cost_per_million": float(getattr(tier, "input_cache_read", 0.0) or 0.0) * 1_000_000.0,
                    "request_cost": float(getattr(tier, "request", 0.0) or 0.0),
                }
            )
        chosen = sorted(tiers, key=lambda item: item["min_context"])[0] if tiers else {
            "min_context": 0,
            "input_cost_per_million": 0.0,
            "output_cost_per_million": 0.0,
            "cached_input_cost_per_million": 0.0,
            "request_cost": 0.0,
        }
        snapshot[str(model_id)] = {
            "context_length": int(getattr(info, "context_length", 0) or 0),
            "supported_parameters": list(getattr(info, "supported_parameters", ()) or ()),
            "input_cost_per_million": float(chosen["input_cost_per_million"]),
            "output_cost_per_million": float(chosen["output_cost_per_million"]),
            "cached_input_cost_per_million": float(chosen["cached_input_cost_per_million"]),
            "request_cost": float(chosen["request_cost"]),
            "pricing_tiers": tiers,
        }
    return snapshot


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_prompt_hashes(candidate_module, spec) -> Dict[str, str]:
    hashes: Dict[str, str] = {
        "candidate_dict_sha256": sha256_json(spec.to_dict()),
        "chapter_stage_sha256": sha256_json(asdict(spec.chapter_stage)),
        "composer_stage_sha256": sha256_json(asdict(spec.composer_stage)),
        "length_control_sha256": sha256_json(asdict(spec.length_control)),
        "budget_allocator_sha256": sha256_json(asdict(spec.budget_allocator)),
    }

    if hasattr(candidate_module, "render_chapter_system"):
        hashes["chapter_system_prompt_sha256"] = sha256_text(candidate_module.render_chapter_system(spec))
    if hasattr(candidate_module, "render_composer_system"):
        hashes["composer_system_prompt_sha256"] = sha256_text(candidate_module.render_composer_system(spec))

    for function_name in (
        "render_chapter_user",
        "render_repair_user",
        "render_composer_user",
        "build_openrouter_request",
        "allocate_chapter_targets",
    ):
        function = getattr(candidate_module, function_name, None)
        if function is None:
            continue
        try:
            source = inspect.getsource(function)
        except (OSError, TypeError):
            continue
        hashes[f"{function_name}_sha256"] = sha256_text(source)
    return hashes


def build_run_id(*, timestamp: str, benchmark_version: str, bench_name: str, profile: str, candidate_name: str) -> str:
    return "__".join(
        [
            sanitize_slug(timestamp, limit=32),
            sanitize_slug(benchmark_version, limit=32),
            sanitize_slug(bench_name, limit=32),
            sanitize_slug(profile, limit=16),
            sanitize_slug(candidate_name, limit=64),
        ]
    )
