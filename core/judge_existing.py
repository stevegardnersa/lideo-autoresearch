#!/usr/bin/env python3
"""Re-run LLM judge on existing runs, writing results to separate .llmj.* files.

Original run files are never modified.

Usage:
    python core/judge_existing.py --bench booksum-v4 --judge-model openai/gpt-4o --dry-run
    python core/judge_existing.py --bench booksum-v4 --judge-model openai/gpt-4o --profile nemotron-3 --dry-run
    python core/judge_existing.py --bench booksum-v4 --judge-model openai/gpt-4o --profile thinking
    python core/judge_existing.py --bench booksum-v4 --judge-model openai/gpt-4o --run-id 20260512t075446z__booksum-v4__chapter_fast-v3__30m_nemotron-3-s__30m_nemotron-3-super-120b-a12b_thinking_v1
    python core/judge_existing.py --bench booksum-v4 --judge-model openai/gpt-4o --profile nemotron-3 --max-samples 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.judge import AbsoluteJudgeResult, judge_summary_absolute
from core.openrouter_client import OpenRouterClient
from scoring import JudgeScores, Rubric, SummarySample, score_dataset


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_rubric(path: Path) -> Rubric:
    if not path.exists():
        return Rubric()
    data = load_json(path)
    return Rubric(
        headings=tuple(data.get("headings") or []),
        core_concepts=tuple(data.get("core_concepts") or []),
        mechanisms_or_explanations=tuple(data.get("mechanisms_or_explanations") or []),
        critical_qualifiers=tuple(data.get("critical_qualifiers") or []),
        important_examples=tuple(data.get("important_examples") or []),
        key_entities_or_numbers=tuple(data.get("key_entities_or_numbers") or []),
        key_terms=tuple(data.get("key_terms") or []),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir",
        default="runs/",
        help="Base directory containing benchmark subdirectories (default: runs/)",
    )
    parser.add_argument(
        "--bench",
        required=True,
        help="Benchmark subdirectory to scan (e.g. booksum-v4)",
    )
    parser.add_argument(
        "--judge-model",
        required=True,
        help="OpenRouter model ID to use for judging (e.g. openai/gpt-4o)",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Operate on a single run by exact run-id.",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Filter runs to those with this profile (substring match).",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=0,
        help="Limit number of samples to judge per run (0 = all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate matching runs and sample counts without calling the judge API or writing files.",
    )
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Overwrite existing llmj output files (default: skip runs where llmj files already exist).",
    )
    parser.add_argument(
        "--api-key-env",
        default="LLM_API_KEY",
        help="Environment variable holding the API key for the provider.",
    )
    return parser.parse_args()


def llmj_paths(run_json_path: Path, judge_model: str) -> tuple[Path, Path, Path]:
    model_slug = judge_model.split("/")[-1]
    llj_suffix = f"__llmj_{model_slug}"
    return (
        run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.json",
        run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.state.json",
        run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.samples.jsonl",
    )


def runs_needing_judge(run_dir: Path) -> list[Path]:
    """Return .json paths for runs where judge_model is empty AND judge_version_resolved contains 'deterministic'."""
    results = []
    if not run_dir.exists():
        return results
    for json_path in run_dir.glob("*.json"):
        if json_path.suffix == ".state":
            continue
        state_path = json_path.with_suffix(".state.json")
        if not state_path.exists():
            continue
        try:
            record = load_json(json_path)
        except Exception:
            continue
        manifest = record.get("run_manifest", {})
        judge_model = manifest.get("judge_model", "")
        judge_version_resolved = manifest.get("judge_version_resolved", "")
        if judge_model == "" and "deterministic" in judge_version_resolved:
            results.append(json_path)
    return sorted(results, key=lambda p: p.name)


def build_sample(line: dict, rubric_dir: Path, judge_scores_map: dict | None = None) -> SummarySample:
    sample_id = line["sample_id"]
    book_id = line.get("book_id", sample_id.split(":")[0] if ":" in sample_id else "")
    chapter_id = line.get("chapter_id", sample_id.split(":")[1] if ":" in sample_id else "")
    rubric_path = rubric_dir / book_id / chapter_id / "rubric.json"
    rubric = load_rubric(rubric_path)
    raw_summary = line.get("summary_md") or ""
    summary_md = raw_summary.strip()
    if summary_md.startswith("{") and '"summary_md":' in raw_summary:
        try:
            inner = json.loads(raw_summary)
            summary_md = inner.get("summary_md", "") or raw_summary
        except Exception:
            pass
    judge_scores = None
    if judge_scores_map is not None and sample_id in judge_scores_map:
        raw_js = judge_scores_map[sample_id]
        if raw_js is not None:
            judge_scores = JudgeScores(
                faithfulness=raw_js.get("faithfulness", 0.0),
                concept_coverage=raw_js.get("concept_coverage", 0.0),
                qualifier_preservation=raw_js.get("qualifier_preservation", 0.0),
                no_fluff=raw_js.get("no_fluff", 0.0),
                structure_quality=raw_js.get("structure_quality", 0.0),
            )
    elif line.get("judge_scores") is not None:
        raw_js = line["judge_scores"]
        judge_scores = JudgeScores(
            faithfulness=raw_js.get("faithfulness", 0.0),
            concept_coverage=raw_js.get("concept_coverage", 0.0),
            qualifier_preservation=raw_js.get("qualifier_preservation", 0.0),
            no_fluff=raw_js.get("no_fluff", 0.0),
            structure_quality=raw_js.get("structure_quality", 0.0),
        )
    return SummarySample(
        sample_id=sample_id,
        level="chapter",
        target_words=line.get("target_words", 0),
        summary_md=summary_md,
        source_md="",
        group_id=line.get("group_id", ""),
        first_pass_summary_md=line.get("first_pass_summary_md") or "",
        passes_used=line.get("passes_used", 1),
        generation_cost=line.get("generation_cost", 0.0),
        uncached_generation_cost=line.get("uncached_generation_cost", 0.0),
        malformed=line.get("malformed", False),
        rubric=rubric,
        judge_scores=judge_scores,
    )


def judge_run(
    run_json_path: Path,
    judge_model: str,
    max_samples: int,
    client: OpenRouterClient,
) -> tuple[dict, int]:
    record = load_json(run_json_path)
    state = load_json(run_json_path.with_suffix(".state.json"))
    samples_path = run_json_path.with_suffix(".samples.jsonl")
    data_dir = Path(record["run_manifest"].get("data_dir", "data/books"))
    rubric_dir = data_dir if data_dir.exists() else ROOT / "data" / "books"

    with open(samples_path, encoding="utf-8") as f:
        all_lines = [json.loads(l) for l in f]

    judge_candidates = [
        (i, line)
        for i, line in enumerate(all_lines)
        if not line.get("malformed") and line.get("summary_md")
    ]
    if max_samples > 0:
        judge_candidates = judge_candidates[:max_samples]

    judge_scores_map = {line["sample_id"]: line["judge_scores"] for _, line in judge_candidates}

    total_judge_cost = 0.0
    total_judge_uncached = 0.0

    for sample_idx, line in judge_candidates:
        sample = build_sample(line, rubric_dir, judge_scores_map)
        result: AbsoluteJudgeResult = judge_summary_absolute(
            client,
            judge_model=judge_model,
            summary_md=sample.summary_md,
            rubric=sample.rubric,
            source_md=sample.source_md,
            seed=42,
        )
        raw_usage = result.raw_response.get("usage", {})
        cost = raw_usage.get("cost") or 0.0
        total_judge_cost += cost
        total_judge_uncached += cost
        line["judge_scores"] = {
            "faithfulness": result.scores.faithfulness,
            "concept_coverage": result.scores.concept_coverage,
            "qualifier_preservation": result.scores.qualifier_preservation,
            "no_fluff": result.scores.no_fluff,
            "structure_quality": result.scores.structure_quality,
        }
        if "trace" in line and isinstance(line["trace"], dict):
            line["trace"]["judge_rationale"] = result.rationale

    run_id = record["run_manifest"]["run_id"]
    model_slug = judge_model.split("/")[-1]
    llj_suffix = f"__llmj_{model_slug}"
    manifest = dict(record["run_manifest"])
    manifest["judge_model"] = judge_model
    manifest["judge_version_resolved"] = f"judge-absolute-v1::{judge_model}"

    n_judged = len(judge_candidates)
    mean_judge_cost = (total_judge_cost / n_judged) if n_judged > 0 else 0.0
    mean_judge_uncached = (total_judge_uncached / n_judged) if n_judged > 0 else 0.0
    orig_mean_gen_cost = record.get("dataset_score", {}).get("mean_generation_cost", 0.0)
    manifest["mean_generation_cost"] = orig_mean_gen_cost
    manifest["mean_llm_judge_cost"] = mean_judge_cost
    manifest["mean_llm_judge_uncached"] = mean_judge_uncached
    manifest["mean_total_cost"] = orig_mean_gen_cost + mean_judge_cost

    judged_record = dict(record)
    judged_record["run_manifest"] = manifest

    updated_samples = [build_sample(l, rubric_dir) for l in all_lines]
    dataset_score = score_dataset(updated_samples)
    judged_record["dataset_score"] = asdict_dataset_score(dataset_score, mean_judge_cost, orig_mean_gen_cost)
    judged_record["sample_scores"] = compute_sample_scores(updated_samples)

    judged_state = dict(state)
    judged_state["judge_model"] = judge_model
    judged_state["judge_version_resolved"] = f"judge-absolute-v1::{judge_model}"

    llmj_json_path = run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.json"
    llmj_state_path = run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.state.json"
    llmj_samples_path = run_json_path.parent / f"{run_json_path.stem}{llj_suffix}.samples.jsonl"

    save_json(llmj_json_path, judged_record)
    save_json(llmj_state_path, judged_state)

    with open(llmj_samples_path, "w", encoding="utf-8") as f:
        for line in all_lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    return judged_record, n_judged, str(llmj_json_path), str(llmj_state_path), str(llmj_samples_path)


def asdict_dataset_score(ds, mean_judge_cost=0.0, orig_mean_gen_cost=0.0):
    return {
        "n_samples": ds.n_samples,
        "hard_fail_rate": ds.hard_fail_rate,
        "mean_quality": ds.mean_quality,
        "mean_utility": ds.mean_utility,
        "mean_faithfulness": ds.mean_faithfulness,
        "mean_concept_coverage": ds.mean_concept_coverage,
        "mean_final_length_error_pct": ds.mean_final_length_error_pct,
        "mean_first_pass_length_error_pct": ds.mean_first_pass_length_error_pct,
        "mean_passes_used": ds.mean_passes_used,
        "mean_uncached_cost": ds.mean_uncached_cost,
        "mean_llm_judge_cost": mean_judge_cost,
        "mean_total_cost": orig_mean_gen_cost + mean_judge_cost,
        "by_group_quality": ds.by_group_quality,
        "by_group_utility": ds.by_group_utility,
        "sample_scores": [
            {"sample_id": str(ss.sample_id), "quality": ss.quality, "utility": ss.utility}
            for ss in ds.sample_scores
        ],
    }


def compute_sample_scores(samples):
    scores = []
    for s in samples:
        js = s.judge_scores
        if js is None:
            scores.append({"sample_id": s.sample_id, "malformed": s.malformed})
        else:
            scores.append({
                "sample_id": s.sample_id,
                "malformed": s.malformed,
                "faithfulness": js.faithfulness,
                "concept_coverage": js.concept_coverage,
                "qualifier_preservation": js.qualifier_preservation,
                "no_fluff": js.no_fluff,
                "structure_quality": js.structure_quality,
            })
    return scores


def llmj_exists(run_json_path: Path, judge_model: str) -> bool:
    json_path, state_path, samples_path = llmj_paths(run_json_path, judge_model)
    return json_path.exists() and state_path.exists() and samples_path.exists()


def filter_runs(paths: list[Path], profile: str, bench: str) -> list[Path]:
    result = []
    for p in paths:
        try:
            record = load_json(p)
        except Exception:
            continue
        manifest = record.get("run_manifest", {})
        p_val = manifest.get("profile", "")
        b_val = manifest.get("bench", "")
        if profile and profile not in p_val:
            continue
        if bench and bench not in b_val:
            continue
        result.append(p)
    return result


def dry_run_report(run_json_path: Path) -> tuple[str, str, str, int, int]:
    record = load_json(run_json_path)
    manifest = record.get("run_manifest", {})
    run_id = manifest.get("run_id", run_json_path.stem)
    profile = manifest.get("profile", "")
    chapter_model = manifest.get("chapter_model", "")

    samples_path = run_json_path.with_suffix(".samples.jsonl")
    with open(samples_path, encoding="utf-8") as f:
        all_lines = [json.loads(l) for l in f]

    total = len(all_lines)
    judgeable = sum(1 for l in all_lines if not l.get("malformed") and l.get("summary_md"))

    return run_id, profile, chapter_model, judgeable, total


def main() -> None:
    args = parse_args()
    run_dir = Path(args.runs_dir).resolve() / args.bench

    if args.run_id:
        run_json_path = run_dir / f"{args.run_id}.json"
        if not run_json_path.exists():
            sys.exit(f"Run not found: {run_json_path}")
        run_paths = [run_json_path]
    else:
        all_runs = runs_needing_judge(run_dir)
        run_paths = [p for p in all_runs if not args.profile or args.profile in p.stem]
        if not run_paths:
            print("No runs found needing judge.")
            return

    if args.dry_run:
        skipped = sum(1 for p in run_paths if llmj_exists(p, args.judge_model))
        print(f"Scanning: {run_dir}")
        print(f"Judging with: {args.judge_model}")
        if args.profile:
            print(f"Profile filter: {args.profile}")
        print(f"\nFound {len(run_paths)} run(s) needing judge ({skipped} skipped — already have llmj sidecar):\n")
        for p in run_paths:
            if not args.force_overwrite and llmj_exists(p, args.judge_model):
                status = "SKIPPED (already exists)"
            else:
                status = ""
            run_id, profile, chapter_model, judgeable, total = dry_run_report(p)
            print(f"  {run_id}")
            print(f"    profile       : {profile}")
            print(f"    chapter_model: {chapter_model}")
            print(f"    samples       : {judgeable} of {total} would be judged")
            if status:
                print(f"    {status}")
            print()
        return

    client = OpenRouterClient(api_key=os.environ.get(args.api_key_env, ""))

    for run_json_path in run_paths:
        run_id = run_json_path.stem
        if not args.force_overwrite and llmj_exists(run_json_path, args.judge_model):
            print(f"\nSkipping {run_id}: llmj output already exists for {args.judge_model}")
            continue
        print(f"\nJudging run: {run_id}")
        try:
            judged_record, n_judged, llmj_json, llmj_state, llmj_samples = judge_run(
                run_json_path,
                args.judge_model,
                args.max_samples,
                client,
            )
            ds = judged_record.get("dataset_score", {})
            print(f"  Judged {n_judged} sample(s)")
            print(f"  quality={ds.get('mean_quality', 0):.4f}  utility={ds.get('mean_utility', 0):.4f}")
            print(f"  Written: {llmj_json}")
            print(f"  Written: {llmj_state}")
            print(f"  Written: {llmj_samples}")
        except Exception as ex:
            print(f"  ERROR: {ex}")
            continue

    print("\nDone.")


if __name__ == "__main__":
    main()