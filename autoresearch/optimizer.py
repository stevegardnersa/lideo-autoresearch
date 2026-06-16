"""Prompt-component optimizer driven by human chapter notes.

Two strategies:
1. **Hill-climb**: change one dimension at a time, re-benchmark, accept
   the variant if score improves.
2. **Grid search**: for dimensions with negative sentiment, try all
   available options exhaustively.

The optimizer edits ``candidate_spec.py`` directly (the only file the
autoresearch agent should mutate) and calls the benchmark harness to
evaluate each variant.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .notes_reader import (
    DIMENSION_OPTIONS,
    DIMENSION_SLUGS,
    DIMENSION_TO_POLICY_KEY,
    VALID_OPTIONS,
    Signals,
    get_active_dimensions,
    get_current_option,
    get_dimension_sentiment,
)


# ── Data types ──────────────────────────────────────────────────────────

@dataclass
class Variant:
    """A candidate variant with its prompt component configuration."""
    name: str                         # e.g. "30m_deepseek-v4-flash_notthinking_v2"
    profile: str                      # e.g. "30m_deepseek-v4-flash_notthinking"
    base_name: str                    # original variant name (v1)
    components: Dict[str, str]        # dimension -> option_id
    changed_dimensions: List[str] = field(default_factory=list)
    source_variant: Optional[str] = None  # parent variant name


@dataclass
class EvaluationResult:
    """Score summary after running a variant through the benchmark."""
    variant_name: str
    success: bool = False
    avg_quality: float = 0.0
    avg_faithfulness: float = 0.0
    avg_concept_coverage: float = 0.0
    pass_rate: float = 0.0
    total_cost: float = 0.0
    samples_scored: int = 0
    raw_manifest_file: Optional[str] = None
    error: str = ""

    @property
    def composite_score(self) -> float:
        """Weighted composite for hill-climb comparisons."""
        return (
            0.35 * self.avg_faithfulness +
            0.25 * self.avg_quality +
            0.25 * self.avg_concept_coverage +
            0.15 * self.pass_rate
        )


@dataclass
class OptimizationStep:
    variant: Variant
    result: Optional[EvaluationResult] = None


@dataclass
class OptimizationRun:
    model_name: str                   # e.g. "deepseek-v4-flash"
    time_budget: str                  # "30m" or "60m"
    thinking: str                     # "notthinking" or "thinking"
    base_profile: str
    base_variant_name: str
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    steps: List[OptimizationStep] = field(default_factory=list)
    best_variant: Optional[Variant] = None
    best_result: Optional[EvaluationResult] = None


# ── Component manipulation ──────────────────────────────────────────────

def make_components_from_spec(spec: dict) -> Dict[str, str]:
    """Extract chapter-stage prompt_components from a CandidateSpec dict."""
    chapter = spec.get("chapter_stage", {})
    return dict(chapter.get("prompt_components", {}))


def components_to_dimensions(components: Dict[str, str]) -> Dict[str, str]:
    """Convert policy_key -> option_id to dimension -> option_id."""
    dims: Dict[str, str] = {}
    for dim, policy_key in DIMENSION_TO_POLICY_KEY.items():
        val = components.get(policy_key)
        if val and val in VALID_OPTIONS.get(dim, set()):
            dims[dim] = val
    return dims


def dimensions_to_components(dims: Dict[str, str]) -> Dict[str, str]:
    """Convert dimension -> option_id to policy_key -> option_id."""
    comps: Dict[str, str] = {}
    for dim, option_id in dims.items():
        key = DIMENSION_TO_POLICY_KEY.get(dim)
        if key and option_id in VALID_OPTIONS.get(dim, set()):
            comps[key] = option_id
    return comps


def parse_variant_name(name: str) -> Optional[Tuple[str, int]]:
    """Return (base, version) from e.g. '30m_deepseek-v4-flash_notthinking_v2'."""
    m = re.match(r"^(.+)_v(\d+)$", name)
    if m:
        return m.group(1), int(m.group(2))
    return None


# ── candidate_spec.py editing ───────────────────────────────────────────

def read_spec_file() -> Dict[str, dict]:
    """Read candidate_spec.py and extract PROFILE_CANDIDATES as dict."""
    # Use a subprocess that imports the module and serializes to JSON
    script = """
import json, sys
sys.path.insert(0, '.')
from candidate_spec import PROFILE_CANDIDATES
from dataclasses import asdict
out = {k: asdict(v) for k, v in PROFILE_CANDIDATES.items()}
print(json.dumps(out))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read candidate_spec.py: {proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse candidate_spec JSON: {e}") from e


def write_spec_file(specs: Dict[str, dict], backup: bool = True) -> str:
    """Write PROFILE_CANDIDATES back to candidate_spec.py. Returns file path."""
    filepath = os.path.join(os.getcwd(), "candidate_spec.py")
    if backup:
        backup_path = filepath + f".bak.{int(time.time())}"
        shutil.copy2(filepath, backup_path)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the PROFILE_CANDIDATES block and replace it
    # Strategy: find "PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {" and replace everything to the matching closing brace at the top level
    start_pattern = "PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {"
    start_idx = content.find(start_pattern)
    if start_idx == -1:
        raise RuntimeError("Could not find PROFILE_CANDIDATES in candidate_spec.py")

    brace_start = content.index("{", start_idx)
    # Track braces to find matching closing brace
    depth = 0
    end_idx = brace_start
    for i in range(brace_start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break

    # Generate new entries
    entries: List[str] = []
    for profile, spec in specs.items():
        cname = spec.get("name", "")
        chapter = spec.get("chapter_stage", {})
        composer = spec.get("composer_stage", {})
        lc = spec.get("length_control", {})
        ba = spec.get("budget_allocator", {})

        ch = _format_stage_config(chapter)
        co = _format_stage_config(composer)

        lc_str = (
            f"LengthControlConfig(\n"
            f"            max_passes={lc.get('max_passes', 5)}, "
            f"tolerance_pct={lc.get('tolerance_pct', 0.05)}, "
            f"hard_tolerance_pct={lc.get('hard_tolerance_pct', 0.10)}, "
            f"repair_strategy={_quote(lc.get('repair_strategy', 'edit_existing'))}, "
            f"repair_more_prompt_id={_quote(lc.get('repair_more_prompt_id', 'expand_missing_detail'))}, "
            f"repair_less_prompt_id={_quote(lc.get('repair_less_prompt_id', 'shrink_dedup_first'))}\n"
            f"        )"
        )

        ba_str = (
            f"BudgetAllocatorConfig(\n"
            f"            words_per_minute={ba.get('words_per_minute', 200)}, "
            f"allocation_alpha={ba.get('allocation_alpha', 0.9)}, "
            f"min_chapter_share={ba.get('min_chapter_share', 0.03)}, "
            f"max_chapter_share={ba.get('max_chapter_share', 0.18)}, "
            f"chapter_stage_multiplier_30m={ba.get('chapter_stage_multiplier_30m', 1.2)}, "
            f"chapter_stage_multiplier_60m={ba.get('chapter_stage_multiplier_60m', 1.0)}, "
            f"max_summary_to_source_ratio={ba.get('max_summary_to_source_ratio', 0.9)}\n"
            f"        )"
        )

        entry = (
            f'    {_quote(profile)}: CandidateSpec(\n'
            f'        name={_quote(cname)},\n'
            f'        profile={_quote(profile)},\n'
            f'        chapter_stage={ch},\n'
            f'        composer_stage={co},\n'
            f'        length_control={lc_str},\n'
            f'        budget_allocator={ba_str},\n'
            f'        use_json_schema={spec.get("use_json_schema", True)},\n'
            f'        json_schema_name={_quote(spec.get("json_schema_name", "summary_response"))},\n'
            f'        notes={_quote(spec.get("notes", ""))},\n'
            f'        disable_composer={spec.get("disable_composer", False)}\n'
            f'    )'
        )
        entries.append(entry)

    new_block = "PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {\n" + ",\n".join(entries) + ",\n}"
    new_content = content[:start_idx] + new_block + content[end_idx:]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    return filepath


def _format_stage_config(cfg: dict) -> str:
    """Format a StageConfig dict to Python source."""
    components = cfg.get("prompt_components", {})
    comp_parts: List[str] = []
    for k, v in components.items():
        comp_parts.append(f"{_quote(k)}: {_quote(v)}")
    comp_str = "{" + ", ".join(comp_parts) + "}"
    provider = cfg.get("provider")
    provider_str = "None" if provider is None else repr(provider)
    use_schema = cfg.get("use_json_schema")
    use_schema_str = "None" if use_schema is None else str(use_schema)
    extra_body = cfg.get("extra_body")
    extra_body_str = "None" if extra_body is None else repr(extra_body)
    return (
        f"StageConfig(model={_quote(cfg.get('model', ''))}, "
        f"temperature={cfg.get('temperature', 0.2)}, "
        f"seed={cfg.get('seed', 42)}, "
        f"max_tokens={cfg.get('max_tokens', 8192)}, "
        f"format_mode={_quote(cfg.get('format_mode', 'markdown_sections'))}, "
        f"context_mode={_quote(cfg.get('context_mode', 'chapter_plus_toc_and_meta'))}, "
        f"prompt_components={comp_str}, "
        f"provider={provider_str}, "
        f"use_json_schema={use_schema_str}, "
        f"extra_body={extra_body_str})"
    )


def _quote(s: str) -> str:
    return json.dumps(s)


# ── Variant generation ─────────────────────────────────────────────────

def generate_variants_hill_climb(
    base_spec: dict,
    signals: Signals,
    candidate_name: str,
) -> List[Variant]:
    """Generate variants by changing one dimension at a time.

    Returns one variant per dimension that has feedback. Each variant
    changes the option to the *next* option in the dimension's option
    list (cycling through available options).
    """
    components = make_components_from_spec(base_spec)
    current_dims = components_to_dimensions(components)
    active_dims = get_active_dimensions(signals, candidate_name)

    variants: List[Variant] = []
    base_name = base_spec.get("name", "")
    profile = base_spec.get("profile", "")
    parsed = parse_variant_name(candidate_name)
    base_root = parsed[0] if parsed else candidate_name

    for dim in active_dims:
        options = DIMENSION_OPTIONS.get(dim, [])
        if len(options) < 2:
            continue
        current = current_dims.get(dim)
        if current is None:
            continue
        # Cycle to the next option
        try:
            current_idx = options.index(current)
            next_idx = (current_idx + 1) % len(options)
        except ValueError:
            next_idx = 0
        new_dims = dict(current_dims)
        new_dims[dim] = options[next_idx]
        new_components = dict(components)
        new_components.update(dimensions_to_components(new_dims))

        # Determine version number
        existing_versions = _next_version(base_root)

        variant = Variant(
            name=f"{base_root}_v{existing_versions}",
            profile=profile,
            base_name=base_name,
            components=new_components,
            changed_dimensions=[dim],
            source_variant=candidate_name,
        )
        variants.append(variant)

    return variants


def generate_variants_grid_search(
    base_spec: dict,
    signals: Signals,
    candidate_name: str,
    max_variants: int = 12,
) -> List[Variant]:
    """For dimensions with negative sentiment, try ALL available options.

    Generates one variant per (negative_sentiment_dimension × option)
    combination, up to max_variants.
    """
    components = make_components_from_spec(base_spec)
    current_dims = components_to_dimensions(components)
    parsed = parse_variant_name(candidate_name)
    base_root = parsed[0] if parsed else candidate_name
    profile = base_spec.get("profile", "")

    # Find dimensions with negative sentiment
    negative_dims: List[Tuple[str, float]] = []
    cand_signals = signals.candidates.get(candidate_name)
    if cand_signals:
        for dim in DIMENSION_SLUGS:
            fb = cand_signals.dimensions.get(dim)
            if fb and fb.total_signals > 0:
                score = get_dimension_sentiment(fb)
                if score < 0:
                    negative_dims.append((dim, score))
    negative_dims.sort(key=lambda x: x[1])  # most negative first

    variants: List[Variant] = []
    seen_components: Set[str] = set()
    base_name = base_spec.get("name", "")

    base_comp_sig = json.dumps(dict(sorted(components.items())))
    seen_components.add(base_comp_sig)

    for dim, _ in negative_dims:
        options = DIMENSION_OPTIONS.get(dim, [])
        current = current_dims.get(dim)
        for opt in options:
            if opt == current:
                continue
            new_dims = dict(current_dims)
            new_dims[dim] = opt
            new_components = dict(components)
            new_components.update(dimensions_to_components(new_dims))
            sig = json.dumps(dict(sorted(new_components.items())))
            if sig in seen_components:
                continue
            seen_components.add(sig)
            vn = _next_version(base_root)
            variants.append(Variant(
                name=f"{base_root}_v{vn}",
                profile=profile,
                base_name=base_name,
                components=new_components,
                changed_dimensions=[dim],
                source_variant=candidate_name,
            ))
            if len(variants) >= max_variants:
                return variants

    return variants


def _next_version(base_root: str) -> int:
    """Scan existing specs for the next available version number."""
    try:
        specs = read_spec_file()
    except Exception:
        return 2
    max_v = 1
    for profile, spec in specs.items():
        name = spec.get("name", "")
        if name.startswith(base_root):
            parsed = parse_variant_name(name)
            if parsed and parsed[0] == base_root:
                max_v = max(max_v, parsed[1])
    return max_v + 1


def add_variant_to_specs(
    variant: Variant,
    base_spec: dict,
    specs: Dict[str, dict],
) -> Dict[str, dict]:
    """Insert a variant into the specs dict under its profile key.

    Creates a new entry: specs[variant.profile] = new_spec.
    This overwrites any existing variant for that profile.
    """
    import copy
    new_spec = copy.deepcopy(base_spec)
    new_spec["name"] = variant.name
    new_spec["profile"] = variant.profile
    chapter = new_spec["chapter_stage"]
    chapter["prompt_components"] = dict(variant.components)
    new_spec["notes"] = (
        f"Auto-generated from {variant.source_variant}, "
        f"changed: {', '.join(variant.changed_dimensions)}"
    )
    specs[variant.profile] = new_spec
    return specs


# ── Evaluation stubs (to be wired to the actual benchmark) ──────────────

def evaluate_variant(
    variant_name: str,
    run_candidate_script: str = "core/run_candidate.py",
) -> EvaluationResult:
    """Run the benchmark for a single variant and parse results.

    This uses the existing ``core/run_candidate.py`` entry point.
    Returns an EvaluationResult with scores aggregated from the run manifest.
    """
    result = EvaluationResult(variant_name=variant_name)
    try:
        proc = subprocess.run(
            [sys.executable, run_candidate_script, variant_name],
            capture_output=True, text=True,
            cwd=os.getcwd(),
            timeout=3600,  # 1 hour timeout
        )
        if proc.returncode != 0:
            result.error = f"Benchmark failed (exit {proc.returncode}): {proc.stderr[:500]}"
            return result
    except subprocess.TimeoutExpired:
        result.error = "Benchmark timed out (>1 hour)"
        return result
    except Exception as e:
        result.error = str(e)
        return result

    # Try to find and parse the run manifest
    result = _parse_latest_run_manifest(variant_name, result)
    return result


def _parse_latest_run_manifest(variant_name: str, result: EvaluationResult) -> EvaluationResult:
    """Find the most recent run manifest and extract aggregate scores."""
    runs_dir = Path(os.getcwd()) / "runs"
    if not runs_dir.exists():
        result.error = "runs/ directory not found"
        return result

    # Find the most recent run directory for this variant
    best_time = 0.0
    best_manifest = None
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir() or "mock" in run_dir.name:
            continue
        for f in run_dir.iterdir():
            if f.name.endswith(".json") and not f.name.endswith(".state.json") and variant_name in f.name:
                mtime = f.stat().st_mtime
                if mtime > best_time:
                    best_time = mtime
                    best_manifest = f

    if best_manifest is None:
        result.error = f"No run manifest found for {variant_name}"
        return result

    try:
        with open(best_manifest, "r") as f:
            data = json.load(f)
    except Exception as e:
        result.error = f"Failed to read manifest: {e}"
        return result

    result.raw_manifest_file = str(best_manifest)
    result.success = True

    scores = data.get("sample_scores", [])
    if not scores:
        result.error = "No sample scores in manifest"
        result.success = False
        return result

    total_scores = len(scores)
    result.samples_scored = total_scores

    qualities = []
    faiths = []
    concepts = []
    passes = 0
    total_cost = 0.0

    for s in scores:
        quality = s.get("quality", 0) or 0
        faith = s.get("resolved_faithfulness", 0) or 0
        concept = s.get("resolved_concept_coverage", 0) or 0
        qualities.append(float(quality))
        faiths.append(float(faith))
        concepts.append(float(concept))
        if not s.get("hard_fail", False):
            passes += 1
        total_cost += float(s.get("generation_cost", 0) or 0)

    result.avg_quality = sum(qualities) / len(qualities) if qualities else 0
    result.avg_faithfulness = sum(faiths) / len(faiths) if faiths else 0
    result.avg_concept_coverage = sum(concepts) / len(concepts) if concepts else 0
    result.pass_rate = passes / total_scores if total_scores else 0
    result.total_cost = total_cost

    return result
