#!/usr/bin/env python3
"""Show overall and slice-based leaderboards from results.tsv and run artifacts."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


NUMERIC_COLUMNS = {
    "mean_quality",
    "mean_utility",
    "mean_faithfulness",
    "mean_concept_coverage",
    "mean_final_length_error_pct",
    "mean_first_pass_length_error_pct",
    "mean_passes_used",
    "mean_uncached_generation_cost",
    "mean_generation_cost",
    "hard_fail_rate",
    "worst_genre_macro_utility",
    "worst_genre_macro_quality",
    "genre_macro_spread_utility",
    "n_genre_macros",
    "n_samples",
    "n_books",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=PROJECT_ROOT / "results.tsv")
    parser.add_argument("--profile", default="")
    parser.add_argument("--bench", default="")
    parser.add_argument("--benchmark-version", default="")
    parser.add_argument("--model-contains", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--sort-by", default="mean_utility")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--slice-field",
        default="",
        help="Load run artifacts and show per-slice metrics for this field, e.g. genre_macro.",
    )
    parser.add_argument("--slice-value", default="")
    return parser.parse_args()


def load_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader)


def numeric_value(row: Dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "") or 0.0)
    except ValueError:
        return 0.0


def resolve_artifact_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def filter_rows(rows: List[Dict[str, str]], args: argparse.Namespace) -> List[Dict[str, str]]:
    filtered = []
    needle = args.model_contains.lower().strip()
    for row in rows:
        if args.profile and row.get("profile") != args.profile:
            continue
        if args.bench and row.get("bench") != args.bench:
            continue
        if args.benchmark_version and row.get("benchmark_version") != args.benchmark_version:
            continue
        if args.run_id and row.get("run_id") != args.run_id:
            continue
        if needle:
            haystack = " ".join(
                [row.get("chapter_model", ""), row.get("composer_model", ""), row.get("candidate_name", "")]
            ).lower()
            if needle not in haystack:
                continue
        filtered.append(row)
    return filtered


def render_table(rows: List[Dict[str, Any]], *, columns: List[str], sort_by: str, top: int) -> str:
    rows = sorted(rows, key=lambda row: numeric_value(row, sort_by), reverse=True)
    if top > 0:
        rows = rows[:top]
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column], len(str(row.get(column, ""))))

    lines = []
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    divider = "  ".join("-" * widths[column] for column in columns)
    lines.append(header)
    lines.append(divider)
    for row in rows:
        lines.append("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))
    return "\n".join(lines)


def load_artifact(row: Mapping[str, str]) -> Dict[str, Any]:
    artifact_ref = str(row.get("run_artifact") or "").strip()
    if not artifact_ref:
        raise FileNotFoundError(f"Row has no run_artifact field: {row.get('run_id', '')}")
    path = resolve_artifact_path(artifact_ref)
    if not path.exists():
        raise FileNotFoundError(f"Run artifact not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_slice_rows(rows: List[Dict[str, str]], *, field_name: str, slice_value_filter: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        artifact = load_artifact(row)
        dataset_score = dict(artifact.get("dataset_score") or {})
        field_payload = dict((dataset_score.get("slice_summaries") or {}).get(field_name) or {})
        for slice_value, metrics in sorted(field_payload.items(), key=lambda item: item[0]):
            if slice_value_filter and slice_value != slice_value_filter:
                continue
            out.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "benchmark_version": row.get("benchmark_version", ""),
                    "profile": row.get("profile", ""),
                    "bench": row.get("bench", ""),
                    "candidate_name": row.get("candidate_name", ""),
                    "chapter_model": row.get("chapter_model", ""),
                    "composer_model": row.get("composer_model", ""),
                    "slice_field": field_name,
                    "slice_value": slice_value,
                    "n_samples": metrics.get("n_samples", 0),
                    "n_books": metrics.get("n_books", 0),
                    "mean_utility": metrics.get("mean_utility", 0.0),
                    "mean_quality": metrics.get("mean_quality", 0.0),
                    "mean_faithfulness": metrics.get("mean_faithfulness", 0.0),
                    "mean_concept_coverage": metrics.get("mean_concept_coverage", 0.0),
                    "mean_passes_used": metrics.get("mean_passes_used", 0.0),
                    "mean_uncached_generation_cost": metrics.get("mean_uncached_generation_cost", 0.0),
                    "mean_generation_cost": metrics.get("mean_generation_cost", 0.0),
                    "hard_fail_rate": metrics.get("hard_fail_rate", 0.0),
                    "run_id": row.get("run_id", ""),
                }
            )
    return out


def main() -> None:
    args = parse_args()
    rows = load_rows(args.results)
    filtered = filter_rows(rows, args)
    if not filtered:
        raise SystemExit("No matching rows found.")

    if args.slice_field:
        slice_rows = build_slice_rows(filtered, field_name=args.slice_field, slice_value_filter=args.slice_value)
        if not slice_rows:
            raise SystemExit(f"No slice rows found for field {args.slice_field!r}.")
        columns = [
            "timestamp",
            "benchmark_version",
            "profile",
            "bench",
            "candidate_name",
            "slice_field",
            "slice_value",
            "n_samples",
            "n_books",
            "mean_utility",
            "mean_quality",
            "mean_uncached_generation_cost",
            "hard_fail_rate",
            "run_id",
        ]
        print(render_table(slice_rows, columns=columns, sort_by=args.sort_by, top=args.top))
        return

    columns = [
        "timestamp",
        "benchmark_version",
        "profile",
        "bench",
        "candidate_name",
        "chapter_model",
        "composer_model",
        "reasoning_effort",
        "mean_utility",
        "mean_quality",
        "worst_genre_macro",
        "worst_genre_macro_utility",
        "genre_macro_spread_utility",
        "mean_uncached_generation_cost",
        "hard_fail_rate",
        "run_id",
    ]
    print(render_table(filtered, columns=columns, sort_by=args.sort_by, top=args.top))


if __name__ == "__main__":
    main()
