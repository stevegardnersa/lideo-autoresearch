#!/usr/bin/env python3
"""Audit the corpus composition for genre-aware benchmarking."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Dict, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.book_data import load_books

FIELDS = (
    "genre_macro",
    "genre_micro",
    "narrative_vs_expository",
    "prescriptive_vs_analytical",
    "quantitative_density",
    "chapter_length_profile",
    "benchmark_pool",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-root", type=Path, default=PROJECT_ROOT / "data" / "books")
    parser.add_argument(
        "--recommended-core-books-per-genre",
        type=int,
        default=4,
        help="Used only for warnings. Default matches the 4x4+2 recommendation.",
    )
    return parser.parse_args()


def count_values(books, field_name: str) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for book in books:
        value = str(getattr(book.taxonomy, field_name, "unknown") or "unknown")
        counter[value] += 1
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def main() -> None:
    args = parse_args()
    books = list(load_books(args.books_root).values())
    if not books:
        raise SystemExit(f"No books found under {args.books_root}")

    eligible = [book for book in books if book.taxonomy.benchmark_pool != "exclude"]
    by_field = {field_name: count_values(eligible, field_name) for field_name in FIELDS}
    warnings = []
    genre_counts = by_field.get("genre_macro", {})
    balanced_counts = {
        genre: count
        for genre, count in genre_counts.items()
        if genre != "unknown"
    }
    weak_genres = [genre for genre, count in balanced_counts.items() if count < args.recommended_core_books_per_genre]
    if weak_genres:
        warnings.append(
            "Some genre_macro buckets have fewer books than the recommended 4-book minimum: "
            + ", ".join(f"{genre}={balanced_counts[genre]}" for genre in sorted(weak_genres))
        )
    if len(balanced_counts) < 4:
        warnings.append(
            f"Only {len(balanced_counts)} non-unknown genre_macro buckets found. The recommended starting point is 4 core macro-genres."
        )
    dev_only_count = by_field.get("benchmark_pool", {}).get("dev_only", 0)
    if dev_only_count == 0:
        warnings.append(
            "No dev_only wildcard books detected. The recommended 18-book setup keeps 2 wildcard books in development only."
        )

    payload = {
        "books_root": str(args.books_root),
        "n_books_total": len(books),
        "n_books_eligible": len(eligible),
        "fields": by_field,
        "warnings": warnings,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
