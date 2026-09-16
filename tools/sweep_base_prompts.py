#!/usr/bin/env python3
"""Sweep the curated base chapter prompts and rank them per model.

Runs each base prompt (see ``tools/prompts_api.py::BASE_PROMPTS``) on a benchmark,
repeats N times, and ranks the bases with the length objective from
``specs/per-model-prompt-tuning.md`` §5.

Example::

    python3 tools/sweep_base_prompts.py --profile 30m_gpt-5.6-luna_notthinking

Artifacts are written to ``artifacts/autoresearch/base_sweep/``:

* ``<profile>_<bench>.json`` — ranked rows with all aggregates
* ``<profile>_<bench>.md``   — human summary + recommendation

``--adopt`` (opt-in) writes the winning components into the permutation store
for the profile; it never touches registry slots.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.prompts_api import BASE_PROMPTS, list_base_prompts  # noqa: E402

# ── Objective constants (specs/per-model-prompt-tuning.md §5) ───────────

W_FINAL_HIT = 0.55
W_FIRST_HIT = 0.20
W_ACCURACY = 0.15
W_EXTRA_PASSES = 0.10
W_PASS_CAP = 0.10

MIN_FAITHFULNESS = 0.70
MIN_CONCEPT_COVERAGE = 0.60
HARD_FAIL_SLACK = 0.05

DEFAULT_TOLERANCE_PCT = 0.05
ZERO_ACCURACY_AT_ERROR_PCT = 0.25
DEFAULT_MAX_PASSES = 5

DEFAULT_BENCH = "chapter_fast-v3"
DEFAULT_REPEAT = 3
DEFAULT_RUN_CANDIDATE = "core/run_candidate.py"
BASE_ORDER = ["sentence", "readable", "readable-low"]

ARTIFACT_DIR = ROOT / "artifacts" / "autoresearch" / "base_sweep"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def metrics_from_manifest(manifest: Dict[str, Any], *,
                          tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
                          max_passes: int = DEFAULT_MAX_PASSES) -> Dict[str, Any]:
    """Compute the §5 length metrics from a run manifest.

    Uses ``sample_scores[].deterministic`` when present (it already carries the
    error percentages and hit accuracies) and falls back to joining
    ``samples[]`` for word counts and pass counts.
    """
    run_manifest = manifest.get("run_manifest") or {}
    judge_version = str(run_manifest.get("judge_version_resolved") or "")
    # Judge-less (deterministic-proxy) runs use uncalibrated faithfulness/concept
    # proxies, so the §5 semantic floors do not apply (specs/per-model-prompt-tuning.md §5).
    semantic_floors_apply = bool(judge_version) and "deterministic" not in judge_version.lower()

    samples = {s.get("sample_id"): s for s in (manifest.get("samples") or []) if isinstance(s, dict)}
    scores = [s for s in (manifest.get("sample_scores") or []) if isinstance(s, dict)]

    final_errs: List[float] = []
    first_errs: List[float] = []
    final_hits: List[float] = []
    first_hits: List[float] = []
    extras: List[float] = []
    capped: List[float] = []
    hard_fails: List[float] = []
    faithfulness: List[float] = []
    concept: List[float] = []
    qualities: List[float] = []
    costs: List[float] = []

    for score in scores:
        sid = score.get("sample_id")
        sample = samples.get(sid) or {}
        det = score.get("deterministic") or {}

        target = _num(det.get("chapter_target_words")) or _num(sample.get("target_words"))
        final_err = _num(det.get("final_length_error_pct"))
        if final_err is None:
            words = _num(det.get("visible_words")) or _num(sample.get("summary_visible_words"))
            if words is not None and target:
                final_err = abs(words - target) / target
        first_err = _num(det.get("first_pass_length_error_pct"))
        if first_err is None:
            first_words = _num(det.get("first_pass_visible_words"))
            if first_words is not None and target:
                first_err = abs(first_words - target) / target

        final_acc = _num(det.get("final_length_accuracy"))
        first_acc = _num(det.get("first_pass_length_accuracy"))
        final_hit = 1.0 if (final_acc is not None and final_acc >= 1.0 - 1e-9) else (
            1.0 if (final_err is not None and final_err <= tolerance_pct) else 0.0)
        first_hit = 1.0 if (first_acc is not None and first_acc >= 1.0 - 1e-9) else (
            1.0 if (first_err is not None and first_err <= tolerance_pct) else 0.0)

        passes = _num(sample.get("passes_used"))
        passes = int(passes) if passes else 1

        if final_err is not None:
            final_errs.append(final_err)
            final_hits.append(final_hit)
        if first_err is not None:
            first_errs.append(first_err)
            first_hits.append(first_hit)
        extras.append(max(0, passes - 1))
        capped.append(1.0 if passes >= max_passes else 0.0)
        hard_fails.append(1.0 if score.get("hard_fail") else 0.0)

        faith = _num(score.get("resolved_faithfulness"))
        conc = _num(score.get("resolved_concept_coverage"))
        faithfulness.append(faith or 0.0)
        concept.append(conc or 0.0)
        qualities.append(_num(score.get("quality")) or 0.0)
        costs.append(_num(sample.get("generation_cost")) or 0.0)

    n = len(scores)
    return {
        "samples_scored": n,
        "final_hit_rate": _mean(final_hits),
        "first_pass_hit_rate": _mean(first_hits),
        "mean_final_error_pct": _mean(final_errs),
        "mean_first_pass_error_pct": _mean(first_errs),
        "mean_extra_passes": _mean(extras),
        "pass_cap_rate": _mean(capped),
        "hard_fail_rate": _mean(hard_fails),
        "mean_faithfulness": _mean(faithfulness),
        "mean_concept_coverage": _mean(concept),
        "mean_quality": _mean(qualities),
        "mean_cost": _mean(costs),
        "judge_backed": semantic_floors_apply,
    }


def length_score(metrics: Dict[str, Any]) -> float:
    """The §5 length objective (maximize)."""
    return (
        W_FINAL_HIT * float(metrics.get("final_hit_rate") or 0.0)
        + W_FIRST_HIT * float(metrics.get("first_pass_hit_rate") or 0.0)
        + W_ACCURACY * (1.0 - _clamp01(float(metrics.get("mean_final_error_pct") or 0.0) / ZERO_ACCURACY_AT_ERROR_PCT))
        - W_EXTRA_PASSES * _clamp01(float(metrics.get("mean_extra_passes") or 0.0) / max(1, DEFAULT_MAX_PASSES - 1))
        - W_PASS_CAP * float(metrics.get("pass_cap_rate") or 0.0)
    )


def aggregate_runs(per_run: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Average the per-run metric dicts into one aggregate."""
    if not per_run:
        return {"runs": 0}
    keys = [
        "final_hit_rate", "first_pass_hit_rate", "mean_final_error_pct",
        "mean_first_pass_error_pct", "mean_extra_passes", "pass_cap_rate",
        "hard_fail_rate", "mean_faithfulness", "mean_concept_coverage",
        "mean_quality", "mean_cost",
    ]
    agg: Dict[str, Any] = {"runs": len(per_run)}
    for key in keys:
        agg[key] = _mean([float(r.get(key) or 0.0) for r in per_run])
    agg["samples_scored"] = sum(int(r.get("samples_scored") or 0) for r in per_run)
    agg["judge_backed"] = any(r.get("judge_backed") for r in per_run)
    agg["run_ids"] = [r.get("run_id", "") for r in per_run]
    agg["length_score"] = length_score(agg)
    return agg


def rank_bases(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Mark eligibility, sort by length score, and pick a winner.

    ``baseline_hard_fail_rate`` defaults to the best hard-fail rate among the
    compared bases: a base that is materially worse than the best available one
    is ineligible. Pass an explicit baseline row to override.
    """
    if not rows:
        return [], None
    baseline_hard_fail = min(float(r.get("hard_fail_rate") or 0.0) for r in rows)
    order_index = {slug: i for i, slug in enumerate(BASE_ORDER)}

    ranked: List[Dict[str, Any]] = []
    for row in rows:
        reasons: List[str] = []
        if not int(row.get("runs") or 0):
            reasons.append("no successful runs")
        if row.get("judge_backed"):
            if float(row.get("mean_faithfulness") or 0.0) < MIN_FAITHFULNESS:
                reasons.append(
                    f"mean_faithfulness {float(row.get('mean_faithfulness') or 0.0):.2f} < {MIN_FAITHFULNESS:.2f}"
                )
            if float(row.get("mean_concept_coverage") or 0.0) < MIN_CONCEPT_COVERAGE:
                reasons.append(
                    f"mean_concept_coverage {float(row.get('mean_concept_coverage') or 0.0):.2f} < {MIN_CONCEPT_COVERAGE:.2f}"
                )
        if float(row.get("hard_fail_rate") or 0.0) > baseline_hard_fail + HARD_FAIL_SLACK:
            reasons.append(
                f"hard_fail_rate {float(row.get('hard_fail_rate') or 0.0):.2f} > baseline {baseline_hard_fail:.2f} + {HARD_FAIL_SLACK:.2f}"
            )
        enriched = dict(row)
        enriched["eligible"] = not reasons
        enriched["ineligible_reasons"] = reasons
        ranked.append(enriched)

    ranked.sort(key=lambda r: (
        0 if r["eligible"] else 1,
        -float(r.get("length_score") or 0.0),
        float(r.get("mean_extra_passes") or 0.0),
        -float(r.get("mean_faithfulness") or 0.0),
        order_index.get(str(r.get("slug")), 99),
    ))
    winner = next((r["slug"] for r in ranked if r["eligible"]), None)
    return ranked, winner


# ── Running ─────────────────────────────────────────────────────────────

Runner = Callable[[Dict[str, Any], int], Optional[Path]]


def _find_manifest(runs_dir: Path, run_id: str) -> Optional[Path]:
    best: Optional[Path] = None
    for path in runs_dir.rglob("*.json"):
        name = path.name
        if name.endswith(".state.json") or name.endswith(".samples.jsonl") or "mock" in str(path):
            continue
        if run_id and run_id not in name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest = (data.get("run_manifest") or {}) if isinstance(data, dict) else {}
        if run_id and str(manifest.get("run_id") or "") != run_id:
            continue
        best = path
        break
    return best


def _default_runner(args: argparse.Namespace, base: Dict[str, Any], repeat: int) -> Optional[Path]:
    run_id = f"sweep-{base['slug']}-r{repeat}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    cmd = [
        args.python, args.run_candidate,
        "--bench", args.bench,
        "--profile", args.profile,
        "--prompt-version", base["version_id"],
        "--prompt-stage", "chapter",
        "--run-id", run_id,
        "--notes", f"base sweep: {base['slug']} repeat {repeat}",
    ]
    if args.max_samples:
        cmd += ["--max-samples", str(args.max_samples)]
    if args.mock:
        cmd += ["--mock"]
    if args.dry_run:
        print("  [dry-run] " + " ".join(cmd))
        return None
    print("  $ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print(f"  ! run failed (exit {proc.returncode})", file=sys.stderr)
        return None
    return _find_manifest(Path(args.runs_dir), run_id)


def _max_passes_for(profile: str) -> int:
    try:
        import candidate_spec
        spec = candidate_spec.get_candidate(profile)
        return int(getattr(spec.length_control, "max_passes", DEFAULT_MAX_PASSES) or DEFAULT_MAX_PASSES)
    except Exception:
        return DEFAULT_MAX_PASSES


def run_sweep(args: argparse.Namespace, *, runner: Optional[Runner] = None) -> Dict[str, Any]:
    bases = list_base_prompts()
    selected = [b for b in bases if not args.bases or b["slug"] in args.bases]
    missing = [s for s in (args.bases or []) if s not in {b["slug"] for b in bases}]
    if missing:
        raise SystemExit(f"unknown base(s): {', '.join(missing)}; known: {', '.join(BASE_PROMPTS)}")
    if not selected:
        raise SystemExit("no bases selected")

    max_passes = _max_passes_for(args.profile)
    rows: List[Dict[str, Any]] = []
    for base in selected:
        print(f"== base {base['slug']} ({base['version_id']}) ==", flush=True)
        per_run: List[Dict[str, Any]] = []
        for repeat in range(1, max(1, args.repeat) + 1):
            manifest_path = (runner or (lambda b, r: _default_runner(args, b, r)))(base, repeat)
            if manifest_path is None:
                print(f"  (no manifest for repeat {repeat})")
                continue
            try:
                manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  ! cannot read {manifest_path}: {exc}", file=sys.stderr)
                continue
            metrics = metrics_from_manifest(
                manifest,
                tolerance_pct=args.tolerance_pct,
                max_passes=max_passes,
            )
            metrics["run_id"] = str((manifest.get("run_manifest") or {}).get("run_id") or "")
            metrics["manifest"] = str(manifest_path)
            per_run.append(metrics)
            print(
                f"  repeat {repeat}: hits {metrics['final_hit_rate']:.2f}/{metrics['first_pass_hit_rate']:.2f} "
                f"err {metrics['mean_final_error_pct']*100:.1f}% extra {metrics['mean_extra_passes']:.2f}",
                flush=True,
            )
        agg = aggregate_runs(per_run)
        agg.update({
            "slug": base["slug"],
            "version_id": base["version_id"],
            "label": base["label"],
            "components": base["components"],
            "per_run": per_run,
        })
        rows.append(agg)

    ranked, winner = rank_bases(rows)
    result = {
        "profile": args.profile,
        "bench": args.bench,
        "repeat": args.repeat,
        "tolerance_pct": args.tolerance_pct,
        "max_passes": max_passes,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "winner": winner,
        "rows": ranked,
    }

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    if args.md_out:
        out = Path(args.md_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(result), encoding="utf-8")
        print(f"wrote {out}")

    if args.adopt and winner:
        adopt_winner(args.profile, ranked)
    return result


def adopt_winner(profile: str, ranked: Sequence[Dict[str, Any]]) -> int:
    """Write the winning base's components+metrics into the permutation store."""
    winner = next((r for r in ranked if r.get("slug") and r.get("eligible")), None)
    if winner is None:
        print("adopt: no eligible base; nothing written", file=sys.stderr)
        return 0
    from autoresearch import permutation_store

    perm_key = permutation_store.extract_permutation_key(profile)
    run_id = (winner.get("run_ids") or [""])[0]
    version = permutation_store.add_history_entry(
        permutation_key=perm_key,
        stage="chapter",
        profile=profile,
        components=dict(winner.get("components") or {}),
        changed_dimensions=[],
        composite=0.0,
        quality=float(winner.get("mean_quality") or 0.0),
        faithfulness=float(winner.get("mean_faithfulness") or 0.0),
        concept_coverage=float(winner.get("mean_concept_coverage") or 0.0),
        pass_rate=1.0 - float(winner.get("hard_fail_rate") or 0.0),
        cost=float(winner.get("mean_cost") or 0.0),
        samples_scored=int(winner.get("samples_scored") or 0),
        run_id=run_id,
        base_prompt=str(winner.get("slug") or ""),
        metrics={
            "final_hit_rate": winner.get("final_hit_rate"),
            "first_pass_hit_rate": winner.get("first_pass_hit_rate"),
            "mean_passes_used": 1.0 + float(winner.get("mean_extra_passes") or 0.0),
            "mean_final_length_error_pct": winner.get("mean_final_error_pct"),
            "length_score": winner.get("length_score"),
        },
    )
    permutation_store.set_current_best(perm_key, "chapter", version)
    print(f"adopt: {perm_key} chapter v{version} <- base {winner['slug']}")
    return version


def render_markdown(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Base prompt sweep — {result['profile']}")
    lines.append("")
    lines.append(f"- bench: `{result['bench']}`")
    lines.append(f"- repeats: {result['repeat']}")
    lines.append(f"- tolerance: {result['tolerance_pct']:.0%}; max passes: {result['max_passes']}")
    lines.append(f"- generated: {result['generated_at']}")
    lines.append(f"- winner: **{result['winner'] or 'none eligible'}**")
    lines.append("")
    lines.append("| rank | base | version | eligible | length score | final hit | first hit | mean err | extra passes | pass cap | faithfulness | concept |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, row in enumerate(result["rows"], 1):
        lines.append(
            f"| {i} | `{row.get('slug')}` | `{row.get('version_id')}` "
            f"| {'yes' if row.get('eligible') else 'no'} "
            f"| {float(row.get('length_score') or 0):.3f} "
            f"| {float(row.get('final_hit_rate') or 0):.2f} "
            f"| {float(row.get('first_pass_hit_rate') or 0):.2f} "
            f"| {float(row.get('mean_final_error_pct') or 0)*100:.1f}% "
            f"| {float(row.get('mean_extra_passes') or 0):.2f} "
            f"| {float(row.get('pass_cap_rate') or 0):.2f} "
            f"| {float(row.get('mean_faithfulness') or 0):.2f} "
            f"| {float(row.get('mean_concept_coverage') or 0):.2f} |"
        )
    ineligible = [r for r in result["rows"] if not r.get("eligible")]
    if ineligible:
        lines.append("")
        lines.append("Ineligible:")
        for row in ineligible:
            lines.append(f"- `{row.get('slug')}`: {'; '.join(row.get('ineligible_reasons') or [])}")
    lines.append("")
    if result.get("winner"):
        lines.append(f"Recommendation: adopt base `{result['winner']}`.")
    else:
        lines.append("Recommendation: no base cleared the eligibility floors; keep the current prompt.")
    return "\n".join(lines) + "\n"


def _default_artifact_paths(profile: str, bench: str) -> Tuple[Path, Path]:
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in profile)
    bench_slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in bench)
    stem = f"{slug}_{bench_slug}"
    return ARTIFACT_DIR / f"{stem}.json", ARTIFACT_DIR / f"{stem}.md"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="Candidate profile to sweep.")
    parser.add_argument("--bench", default=DEFAULT_BENCH, help=f"Benchmark name or JSONL path (default: {DEFAULT_BENCH}).")
    parser.add_argument("--bases", nargs="+", default=[], help=f"Base slugs (default: all of {','.join(BASE_PROMPTS)}); accepts a comma-separated list too.")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help=f"Runs per base (default: {DEFAULT_REPEAT}).")
    parser.add_argument("--json-out", default="", help="JSON artifact path (default: artifacts/autoresearch/base_sweep/...).")
    parser.add_argument("--md-out", default="", help="Markdown artifact path (default: artifacts/autoresearch/base_sweep/...).")
    parser.add_argument("--adopt", action="store_true", help="Write the winner's components to the permutation store (opt-in).")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands without running them.")
    parser.add_argument("--mock", action="store_true", help="Pass --mock to run_candidate (no API spend).")
    parser.add_argument("--max-samples", type=int, default=0, help="Cap samples per run (0 = all).")
    parser.add_argument("--tolerance-pct", type=float, default=DEFAULT_TOLERANCE_PCT, help="Hit tolerance for length accuracy.")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for run_candidate.")
    parser.add_argument("--run-candidate", default=DEFAULT_RUN_CANDIDATE, help="Path to run_candidate.py.")
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"), help="Where to look for run manifests.")
    args = parser.parse_args(argv)
    args.bases = [s.strip() for raw in (args.bases or []) for s in str(raw).split(",") if s.strip()]

    json_path, md_path = _default_artifact_paths(args.profile, args.bench)
    if not args.json_out:
        args.json_out = str(json_path)
    if not args.md_out:
        args.md_out = str(md_path)

    result = run_sweep(args)
    print()
    print(render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
