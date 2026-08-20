"""Autoresearch agent — main coordinator loop.

Orchestrates:
1. Read chapter notes from data/chapter_notes.jsonl
2. Parse signals per candidate x dimension
3. Generate optimization variants (hill-climb or grid search)
4. Run benchmarks for each variant via temp-file evaluation
5. Record results in permutation files (data/optimized_prompts/)
6. Mark best version per stage
7. Generate report

Usage:
    python -m autoresearch.agent [--model MODEL] [--budget 30m|60m]
        [--thinking thinking|notthinking] [--stage chapter|composer]
        [--mode hill_climb|grid_search|auto]

If no flags given, operates on all models with notes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .notes_reader import (
    Signals,
    get_active_dimensions,
    parse_notes_file,
)
from .optimizer import (
    EvaluationResult,
    OptimizationRun,
    OptimizationStep,
    Variant,
    generate_variants_grid_search,
    generate_variants_hill_climb,
    read_spec_file,
    evaluate_variant,
    evaluate_variant_tempfile,
    parse_variant_name,
    make_components_from_spec,
    components_to_dimensions,
)
from . import permutation_store
from .permutation_store import extract_permutation_key


NOTES_FILE = os.path.join(os.getcwd(), "data", "chapter_notes.jsonl")


# ── Candidate resolution ────────────────────────────────────────────────

def find_candidates_for_model(
    specs: Dict[str, dict],
    model_name: str,
    time_budget: Optional[str] = None,
    thinking: Optional[str] = None,
) -> List[str]:
    matches: List[str] = []
    for profile, spec in specs.items():
        name = spec.get("name", "")
        chapter = spec.get("chapter_stage", {})
        mdl = chapter.get("model", "")
        if model_name not in mdl:
            continue
        if time_budget and not name.startswith(time_budget + "_"):
            continue
        if thinking is not None:
            expected = f"_{thinking}"
            if not name.endswith(expected):
                parts = name.split("_")
                if thinking not in parts:
                    continue
        matches.append(name)
    return matches


def resolve_base_candidate(
    candidate_name: str,
    specs: Dict[str, dict],
) -> Optional[dict]:
    for profile, spec in specs.items():
        if spec.get("name") == candidate_name:
            return spec
    for profile, spec in specs.items():
        if profile == candidate_name:
            return spec
    return None


# ── Hill-climb loop ─────────────────────────────────────────────────────

def run_hill_climb(
    candidate_name: str,
    base_spec: dict,
    signals: Signals,
    max_iterations: int = 5,
    dry_run: bool = False,
    stage: str = "chapter",
) -> OptimizationRun:
    parsed = parse_variant_name(candidate_name)
    base_root = parsed[0] if parsed else candidate_name
    perm_key = extract_permutation_key(candidate_name)

    opt_run = OptimizationRun(
        model_name=base_spec.get("chapter_stage", {}).get("model", ""),
        time_budget=candidate_name.split("_")[0] if "_" in candidate_name else "30m",
        thinking="thinking" if "_thinking" in candidate_name else "notthinking",
        base_profile=base_spec.get("profile", ""),
        base_variant_name=candidate_name,
        stage=stage,
    )

    # Evaluate baseline (v1, already in candidate_spec)
    print(f"\n{'='*60}")
    print(f"Evaluating baseline: {candidate_name}")
    if not dry_run:
        baseline_result = evaluate_variant(candidate_name)
    else:
        baseline_result = _dummy_result(candidate_name, 0.70, 0.55, 0.40, 0.80)

    # Record baseline in permutation store
    base_components = make_components_from_spec(base_spec)
    permutation_store.add_history_entry(
        perm_key, stage, candidate_name, base_components, [], 0,
        baseline_result.avg_quality, baseline_result.avg_faithfulness,
        baseline_result.avg_concept_coverage, baseline_result.pass_rate,
        baseline_result.total_cost, baseline_result.samples_scored,
    )

    opt_run.best_result = baseline_result
    print(f"  Baseline composite: {baseline_result.composite_score:.4f}")
    if baseline_result.error:
        print(f"  [WARN] Baseline error: {baseline_result.error}")

    current_best = baseline_result
    current_name = candidate_name
    best_recorded_version = 1  # baseline is version 1

    for iteration in range(max_iterations):
        active_dims = get_active_dimensions(signals, current_name)
        if not active_dims:
            print(f"\n  No active dimensions with feedback. Stopping.")
            break

        print(f"\n--- Iteration {iteration + 1}/{max_iterations} ---")
        print(f"  Active dimensions: {active_dims}")

        variants = generate_variants_hill_climb(
            base_spec, signals, current_name, stage=stage,
        )
        if not variants:
            print("  No variants to try. Stopping.")
            break

        print(f"  Generated {len(variants)} variants to evaluate.")

        improved = False
        for variant in variants:
            print(f"\n  Testing variant: {variant.name}")
            print(f"    Changes: {variant.changed_dimensions}")
            for dim in variant.changed_dimensions:
                current_dims = components_to_dimensions(make_components_from_spec(base_spec))
                new_dims = components_to_dimensions({k: v for k, v in variant.components.items()})
                old_opt = current_dims.get(dim, "?")
                new_opt = new_dims.get(dim, "?")
                print(f"      {dim}: {old_opt} -> {new_opt}")

            if not dry_run:
                result = evaluate_variant_tempfile(variant.name, variant.components)
            else:
                result = _dummy_result(
                    variant.name,
                    baseline_result.avg_quality + 0.02 * (iteration + 1),
                    baseline_result.avg_faithfulness + 0.03,
                    baseline_result.avg_concept_coverage + 0.01,
                    baseline_result.pass_rate + 0.05,
                )

            # Record in permutation store
            recorded_v = permutation_store.add_history_entry(
                perm_key, stage, variant.profile,
                variant.components, variant.changed_dimensions,
                result.composite_score,
                result.avg_quality, result.avg_faithfulness,
                result.avg_concept_coverage, result.pass_rate,
                result.total_cost, result.samples_scored,
            )

            step = OptimizationStep(variant=variant, result=result)
            opt_run.steps.append(step)

            status = "PASS" if result.success and not result.error else "FAIL"
            print(f"    Result: {status} composite={result.composite_score:.4f} "
                  f"(Q={result.avg_quality:.2f} F={result.avg_faithfulness:.2f} "
                  f"C={result.avg_concept_coverage:.2f} PR={result.pass_rate:.2f})")

            if result.success and result.composite_score > current_best.composite_score:
                print(f"    >> IMPROVED! New best.")
                current_best = result
                opt_run.best_result = result
                opt_run.best_variant = variant
                current_name = variant.name
                best_recorded_version = recorded_v
                improved = True

        if not improved:
            print(f"\n  No improvement this iteration. Converged.")
            break

    # Mark best version in permutation store
    permutation_store.set_current_best(perm_key, stage, best_recorded_version)

    opt_run.best_result = current_best
    return opt_run


# ── Grid search loop ────────────────────────────────────────────────────

def run_grid_search(
    candidate_name: str,
    base_spec: dict,
    signals: Signals,
    max_variants: int = 12,
    dry_run: bool = False,
    stage: str = "chapter",
) -> OptimizationRun:
    perm_key = extract_permutation_key(candidate_name)

    opt_run = OptimizationRun(
        model_name=base_spec.get("chapter_stage", {}).get("model", ""),
        time_budget=candidate_name.split("_")[0] if "_" in candidate_name else "30m",
        thinking="thinking" if "_thinking" in candidate_name else "notthinking",
        base_profile=base_spec.get("profile", ""),
        base_variant_name=candidate_name,
        stage=stage,
    )

    # Evaluate baseline
    print(f"\n{'='*60}")
    print(f"Evaluating baseline: {candidate_name}")
    if not dry_run:
        baseline_result = evaluate_variant(candidate_name)
    else:
        baseline_result = _dummy_result(candidate_name, 0.70, 0.55, 0.40, 0.80)

    base_components = make_components_from_spec(base_spec)
    permutation_store.add_history_entry(
        perm_key, stage, candidate_name, base_components, [], 0,
        baseline_result.avg_quality, baseline_result.avg_faithfulness,
        baseline_result.avg_concept_coverage, baseline_result.pass_rate,
        baseline_result.total_cost, baseline_result.samples_scored,
    )

    opt_run.best_result = baseline_result
    print(f"  Baseline composite: {baseline_result.composite_score:.4f}")

    variants = generate_variants_grid_search(base_spec, signals, candidate_name, max_variants, stage=stage)
    if not variants:
        print("  No variants generated. Exiting.")
        return opt_run

    print(f"\nGenerated {len(variants)} grid-search variants.")
    best = baseline_result
    best_variant = None

    for i, variant in enumerate(variants):
        print(f"\n[{i+1}/{len(variants)}] Testing: {variant.name} (changes: {variant.changed_dimensions})")

        if not dry_run:
            result = evaluate_variant_tempfile(variant.name, variant.components)
        else:
            result = _dummy_result(
                variant.name,
                baseline_result.avg_quality + 0.01 * (i + 1),
                baseline_result.avg_faithfulness + 0.02,
                baseline_result.avg_concept_coverage + 0.01,
                baseline_result.pass_rate + 0.03,
            )

        permutation_store.add_history_entry(
            perm_key, stage, variant.profile,
            variant.components, variant.changed_dimensions,
            result.composite_score,
            result.avg_quality, result.avg_faithfulness,
            result.avg_concept_coverage, result.pass_rate,
            result.total_cost, result.samples_scored,
        )

        step = OptimizationStep(variant=variant, result=result)
        opt_run.steps.append(step)

        print(f"    composite={result.composite_score:.4f} "
              f"(Q={result.avg_quality:.2f} F={result.avg_faithfulness:.2f} "
              f"C={result.avg_concept_coverage:.2f} PR={result.pass_rate:.2f})")

        if result.success and result.composite_score > best.composite_score:
            best = result
            best_variant = variant
            print(f"    >> NEW BEST!")

    if best_variant:
        best_version = [i+2 for i, e in enumerate(permutation_store.load_permutation(perm_key).get(stage, {}).get("history", [])) if e.get("profile") == best_variant.profile]
        if best_version:
            permutation_store.set_current_best(perm_key, stage, best_version[0])

    opt_run.best_result = best
    opt_run.best_variant = best_variant
    return opt_run


# ── Main entry point ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Autoresearch — optimize prompt components using human notes.",
    )
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--budget", type=str, choices=["30m", "60m"], default=None)
    parser.add_argument("--thinking", type=str, choices=["thinking", "notthinking"], default=None)
    parser.add_argument("--candidate", type=str, default=None)
    parser.add_argument("--mode", type=str, choices=["hill_climb", "grid_search", "auto"], default="auto")
    parser.add_argument("--max-iter", type=int, default=5)
    parser.add_argument("--max-variants", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--stage", type=str, choices=["chapter", "composer"], default="chapter",
                        help="Which pipeline stage to optimize (default: chapter)")
    args = parser.parse_args()

    if not os.path.exists(NOTES_FILE):
        print(f"No notes file found at {NOTES_FILE}. Add notes first.", file=sys.stderr)
        print("Use the Run Explorer UI to annotate chapter summaries.", file=sys.stderr)
        sys.exit(1)

    signals = parse_notes_file(NOTES_FILE)
    total_signals = sum(c.total_signals for c in signals.candidates.values())
    if total_signals == 0:
        print("No valid notes with candidate_name tags found.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {total_signals} signals across {len(signals.candidates)} candidates.")
    for cname, csig in sorted(signals.candidates.items()):
        dims = list(csig.dimensions.keys())
        print(f"  {cname}: {csig.total_signals} notes, dimensions={dims}")

    specs = read_spec_file()

    if args.candidate:
        candidate_names = [args.candidate]
    else:
        if args.model:
            candidate_names = find_candidates_for_model(
                specs, args.model, args.budget, args.thinking,
            )
        else:
            candidate_names = sorted(signals.candidates.keys())

    candidate_names = [n for n in candidate_names if n in signals.candidates]
    if not candidate_names:
        print("No candidates match the given filters with notes.", file=sys.stderr)
        sys.exit(1)

    all_runs: List[OptimizationRun] = []

    for cname in candidate_names:
        base_spec = resolve_base_candidate(cname, specs)
        if base_spec is None:
            print(f"  [WARN] Candidate {cname} not found in spec file. Skipping.")
            continue

        cand_signals = signals.candidates.get(cname)
        n_signals = cand_signals.total_signals if cand_signals else 0

        mode = args.mode
        if mode == "auto":
            mode = "grid_search" if n_signals >= 5 else "hill_climb"

        print(f"\n{'#'*60}")
        print(f"# Running {mode} for {cname} ({n_signals} signals, stage={args.stage})")
        print(f"{'#'*60}")

        if mode == "hill_climb":
            opt_run = run_hill_climb(cname, base_spec, signals,
                                     max_iterations=args.max_iter, dry_run=args.dry_run,
                                     stage=args.stage)
        else:
            opt_run = run_grid_search(cname, base_spec, signals,
                                      max_variants=args.max_variants, dry_run=args.dry_run,
                                      stage=args.stage)
        all_runs.append(opt_run)

    # Summary
    print(f"\n{'='*60}")
    print("OPTIMIZATION SUMMARY")
    print(f"{'='*60}")
    for opt_run in all_runs:
        best = opt_run.best_result
        print(f"\n{opt_run.base_variant_name} (stage={opt_run.stage}):")
        print(f"  Best composite: {best.composite_score:.4f}" if best else "  No result")
        if best and best.success:
            print(f"  Quality: {best.avg_quality:.2f}  Faith: {best.avg_faithfulness:.2f}  "
                  f"Concept: {best.avg_concept_coverage:.2f}  Pass: {best.pass_rate:.2f}")
        print(f"  Steps evaluated: {len(opt_run.steps)}")
        if opt_run.best_variant:
            print(f"  Best variant: {opt_run.best_variant.name}  "
                  f"Changed: {opt_run.best_variant.changed_dimensions}")
        print(f"  Permutation key: {extract_permutation_key(opt_run.base_variant_name)}")

    if args.output:
        output = {
            "runs": [
                {
                    "base_variant": r.base_variant_name,
                    "model": r.model_name,
                    "time_budget": r.time_budget,
                    "stage": r.stage,
                    "best_composite": r.best_result.composite_score if r.best_result else 0,
                    "best_quality": r.best_result.avg_quality if r.best_result else 0,
                    "best_faithfulness": r.best_result.avg_faithfulness if r.best_result else 0,
                    "best_concept_coverage": r.best_result.avg_concept_coverage if r.best_result else 0,
                    "best_pass_rate": r.best_result.pass_rate if r.best_result else 0,
                    "steps": len(r.steps),
                    "best_variant_name": r.best_variant.name if r.best_variant else None,
                    "best_changes": r.best_variant.changed_dimensions if r.best_variant else [],
                    "permutation_key": extract_permutation_key(r.base_variant_name),
                }
                for r in all_runs
            ],
            "notes_file": NOTES_FILE,
            "total_signals": total_signals,
        }
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nWrote optimization report to {args.output}")


def _dummy_result(name: str, q: float, f: float, c: float, pr: float) -> EvaluationResult:
    return EvaluationResult(
        variant_name=name,
        success=True,
        avg_quality=q,
        avg_faithfulness=f,
        avg_concept_coverage=c,
        pass_rate=pr,
        samples_scored=10,
        total_cost=0.05,
    )


if __name__ == "__main__":
    main()
