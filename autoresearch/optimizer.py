"""Prompt-component optimizer driven by human chapter notes.

Two strategies:
1. **Hill-climb**: change one dimension at a time, re-benchmark, accept
   the variant if score improves.
2. **Grid search**: for dimensions with negative sentiment, try all
   available options exhaustively.

The optimizer NEVER mutates ``candidate_spec.py``. Instead it:
- Reads v1 baseline from candidate_spec (via subprocess)
- Writes temp candidate_spec_variant.py for benchmark evaluation
- Records all results in ``data/optimized_prompts/<perm>.json``
- Mark best version per stage in the permutation file

``candidate_spec.get_candidate()`` auto-resolves v2+ profiles from
permutation history, so the benchmark sees the right components.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tempfile
import shutil
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
from . import permutation_store


# ── Data types ──────────────────────────────────────────────────────────

@dataclass
class Variant:
    """A candidate variant with its prompt component configuration."""
    name: str
    profile: str
    base_name: str
    components: Dict[str, str]
    changed_dimensions: List[str] = field(default_factory=list)
    source_variant: Optional[str] = None
    stage: str = "chapter"


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
    model_name: str
    time_budget: str
    thinking: str
    base_profile: str
    base_variant_name: str
    stage: str = "chapter"
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    steps: List[OptimizationStep] = field(default_factory=list)
    best_variant: Optional[Variant] = None
    best_result: Optional[EvaluationResult] = None


# ── Candidate spec reading (read-only, via subprocess) ──────────────────

def read_spec_file() -> Dict[str, dict]:
    """Read candidate_spec.py PROFILE_CANDIDATES as dict via subprocess."""
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


# ── Component manipulation ──────────────────────────────────────────────

def make_components_from_spec(spec: dict) -> Dict[str, str]:
    chapter = spec.get("chapter_stage", {})
    return dict(chapter.get("prompt_components", {}))


def components_to_dimensions(components: Dict[str, str]) -> Dict[str, str]:
    dims: Dict[str, str] = {}
    for dim, policy_key in DIMENSION_TO_POLICY_KEY.items():
        val = components.get(policy_key)
        if val and val in VALID_OPTIONS.get(dim, set()):
            dims[dim] = val
    return dims


def dimensions_to_components(dims: Dict[str, str]) -> Dict[str, str]:
    comps: Dict[str, str] = {}
    for dim, option_id in dims.items():
        key = DIMENSION_TO_POLICY_KEY.get(dim)
        if key and option_id in VALID_OPTIONS.get(dim, set()):
            comps[key] = option_id
    return comps


def parse_variant_name(name: str) -> Optional[Tuple[str, int]]:
    m = re.match(r"^(.+)_v(\d+)$", name)
    if m:
        return m.group(1), int(m.group(2))
    return None


# ── Temp-file evaluation ────────────────────────────────────────────────

def _create_temp_spec_file(variant_profile: str, variant_components: Dict[str, str]) -> str:
    """Create a temp candidate_spec.py with the variant's prompt components patched in.

    Strategy: copy the real candidate_spec.py to a temp dir, add a new PROFILE_CANDIDATES
    entry for the variant profile, and make it importable for the benchmark.

    Returns the temp dir path.
    """
    tmpdir = tempfile.mkdtemp(prefix="autoresearch_eval_")
    src_spec = os.path.join(os.getcwd(), "candidate_spec.py")

    # Copy the source spec to temp dir
    shutil.copy2(src_spec, tmpdir)
    tmp_spec = os.path.join(tmpdir, "candidate_spec.py")

    # Determine permutation key from variant name
    perm_key = permutation_store.extract_permutation_key(variant_profile)

    # Read the temp spec file to find entry point
    with open(tmp_spec, "r", encoding="utf-8") as f:
        content = f.read()

    # Add import for permutation_store at end of candidate_spec.py
    # and modify get_candidate() to find this variant
    # But actually it's simpler: add the variant as a PROFILE_CANDIDATES entry

    # Build the entry using the v1 baseline + override components
    v1_key = f"{perm_key}_v1"
    # We'll inject via a separate mechanism. For now, just add a simple patch.
    patch = f"""

# ── Autoresearch temp patch ─────────────────────────────
# This file was created for evaluating variant: {variant_profile}
from autoresearch.permutation_store import resolve_variant as _resolve_patched_variant
_ORIGINAL_GET_CANDIDATE = get_candidate

def get_candidate(profile):
    spec = _resolve_patched_variant(profile)
    if spec is not None:
        return spec
    return _ORIGINAL_GET_CANDIDATE(profile)
"""

    with open(tmp_spec, "a", encoding="utf-8") as f:
        f.write(patch)

    # Make sure the variant is in the permutation store so resolve_variant can find it
    permutation_store.add_history_entry(
        permutation_key=perm_key,
        stage="chapter",
        profile=variant_profile,
        components=variant_components,
        changed_dimensions=[],
        composite=0,
        quality=0,
        faithfulness=0,
        concept_coverage=0,
        pass_rate=0,
        cost=0,
        samples_scored=0,
        run_id="",
    )

    return tmpdir


def evaluate_variant_tempfile(
    variant_name: str,
    variant_components: Dict[str, str],
    run_candidate_script: str = "core/run_candidate.py",
) -> EvaluationResult:
    """Run benchmark for a variant using a temp patched candidate_spec.py."""
    result = EvaluationResult(variant_name=variant_name)
    tmpdir = None
    try:
        tmpdir = _create_temp_spec_file(variant_name, variant_components)

        proc = subprocess.run(
            [sys.executable, run_candidate_script, variant_name],
            capture_output=True, text=True,
            cwd=tmpdir,
            timeout=3600,
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
    finally:
        if tmpdir and os.path.exists(tmpdir):
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    # Parse the run manifest from the main runs/ directory
    result = _parse_latest_run_manifest(variant_name, result)
    return result


def evaluate_variant(
    variant_name: str,
    run_candidate_script: str = "core/run_candidate.py",
) -> EvaluationResult:
    """Run benchmark for a variant that already exists in candidate_spec.

    For v1 baselines already in PROFILE_CANDIDATES.
    """
    result = EvaluationResult(variant_name=variant_name)
    try:
        proc = subprocess.run(
            [sys.executable, run_candidate_script, variant_name],
            capture_output=True, text=True,
            cwd=os.getcwd(),
            timeout=3600,
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

    result = _parse_latest_run_manifest(variant_name, result)
    return result


def _parse_latest_run_manifest(variant_name: str, result: EvaluationResult) -> EvaluationResult:
    runs_dir = Path(os.getcwd()) / "runs"
    if not runs_dir.exists():
        result.error = "runs/ directory not found"
        return result

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


# ── Variant generation ─────────────────────────────────────────────────

def generate_variants_hill_climb(
    base_spec: dict,
    signals: Signals,
    candidate_name: str,
    stage: str = "chapter",
) -> List[Variant]:
    """Generate variants by changing one dimension at a time."""
    components = make_components_from_spec(base_spec)
    current_dims = components_to_dimensions(components)
    active_dims = get_active_dimensions(signals, candidate_name)

    variants: List[Variant] = []
    base_name = base_spec.get("name", "")
    profile = base_spec.get("profile", "")
    parsed = parse_variant_name(candidate_name)
    base_root = parsed[0] if parsed else candidate_name
    perm_key = permutation_store.extract_permutation_key(candidate_name)
    next_v = permutation_store.get_next_version(perm_key, stage)

    for dim in active_dims:
        options = DIMENSION_OPTIONS.get(dim, [])
        if len(options) < 2:
            continue
        current = current_dims.get(dim)
        if current is None:
            continue
        try:
            current_idx = options.index(current)
            next_idx = (current_idx + 1) % len(options)
        except ValueError:
            next_idx = 0
        new_dims = dict(current_dims)
        new_dims[dim] = options[next_idx]
        new_components = dict(components)
        new_components.update(dimensions_to_components(new_dims))

        vn = next_v
        variant_name = f"{base_root}_v{vn}"
        next_v += 1

        variants.append(Variant(
            name=variant_name,
            profile=variant_name,
            base_name=base_name,
            components=new_components,
            changed_dimensions=[dim],
            source_variant=candidate_name,
            stage=stage,
        ))

    return variants


def generate_variants_grid_search(
    base_spec: dict,
    signals: Signals,
    candidate_name: str,
    max_variants: int = 12,
    stage: str = "chapter",
) -> List[Variant]:
    """For dimensions with negative sentiment, try ALL available options."""
    components = make_components_from_spec(base_spec)
    current_dims = components_to_dimensions(components)
    parsed = parse_variant_name(candidate_name)
    base_root = parsed[0] if parsed else candidate_name
    perm_key = permutation_store.extract_permutation_key(candidate_name)
    next_v = permutation_store.get_next_version(perm_key, stage)

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
    negative_dims.sort(key=lambda x: x[1])

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
            vn = next_v
            variant_name = f"{base_root}_v{vn}"
            next_v += 1
            variants.append(Variant(
                name=variant_name,
                profile=variant_name,
                base_name=base_name,
                components=new_components,
                changed_dimensions=[dim],
                source_variant=candidate_name,
                stage=stage,
            ))
            if len(variants) >= max_variants:
                return variants

    return variants
