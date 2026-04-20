#!/usr/bin/env python3
"""Build benchmark splits from a book corpus.

Outputs:
- bench/chapter_fast.jsonl
- bench/book_gate.jsonl
- bench/book_holdout.jsonl
- bench/splits.json

The default split mode is genre-aware and works best when your 18-book corpus is:
- 4 macro-genres x 4 books each marked as benchmark_pool=balanced
- 2 wildcard development books marked as benchmark_pool=dev_only
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import random
import sys
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.book_data import BookDoc, BookTaxonomy, ChapterDoc, load_books
from scoring import extract_markdown_headings, extract_numbers, visible_word_count

SPLIT_NAMES = ("development", "gate", "holdout")


def chapter_density_score(markdown_text: str) -> float:
    words = max(1, visible_word_count(markdown_text))
    number_density = len(extract_numbers(markdown_text)) / words
    heading_density = len(extract_markdown_headings(markdown_text)) / max(1.0, words / 500.0)
    return number_density + (0.05 * heading_density)


def choose_dev_chapters(chapters: Sequence[ChapterDoc], chapters_per_book: int) -> List[ChapterDoc]:
    if len(chapters) <= chapters_per_book:
        return list(chapters)

    enriched = [
        {
            "chapter": chapter,
            "words": chapter.visible_words,
            "density": chapter_density_score(chapter.source_md),
        }
        for chapter in chapters
    ]
    by_length = sorted(enriched, key=lambda item: (item["words"], item["chapter"].chapter_id))
    short = by_length[0]["chapter"]
    median_words = by_length[len(by_length) // 2]["words"]
    medium = min(by_length, key=lambda item: abs(item["words"] - median_words))["chapter"]
    long = by_length[-1]["chapter"]
    dense = max(enriched, key=lambda item: (item["density"], item["words"]))["chapter"]

    chosen: List[ChapterDoc] = []
    seen = set()
    for chapter in [short, medium, long, dense]:
        if chapter.chapter_id not in seen:
            chosen.append(chapter)
            seen.add(chapter.chapter_id)
        if len(chosen) >= chapters_per_book:
            return chosen[:chapters_per_book]

    for item in by_length:
        chapter = item["chapter"]
        if chapter.chapter_id not in seen:
            chosen.append(chapter)
            seen.add(chapter.chapter_id)
        if len(chosen) >= chapters_per_book:
            break
    return chosen[:chapters_per_book]


def taxonomy_payload(taxonomy: BookTaxonomy) -> Dict[str, str]:
    return taxonomy.to_dict()


def chapter_record(book: BookDoc, chapter: ChapterDoc) -> Dict[str, Any]:
    payload = {
        "sample_id": f"{book.book_id}:{chapter.chapter_id}",
        "level": "chapter",
        "book_id": book.book_id,
        "group_id": book.book_id,
        "chapter_id": chapter.chapter_id,
        "chapter_title": chapter.title,
        "book_title": book.display_title,
    }
    payload.update(taxonomy_payload(book.taxonomy))
    return payload


def book_record(book: BookDoc) -> Dict[str, Any]:
    payload = {
        "sample_id": book.book_id,
        "level": "book",
        "book_id": book.book_id,
        "group_id": book.book_id,
        "book_title": book.display_title,
    }
    payload.update(taxonomy_payload(book.taxonomy))
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-root", type=Path, default=PROJECT_ROOT / "data" / "books")
    parser.add_argument("--bench-dir", type=Path, default=PROJECT_ROOT / "bench")
    parser.add_argument("--dev-books", type=int, default=10)
    parser.add_argument("--gate-books", type=int, default=4)
    parser.add_argument("--holdout-books", type=int, default=4)
    parser.add_argument("--chapters-per-dev-book", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--split-mode",
        choices=["balanced_genre", "random"],
        default="balanced_genre",
        help="Use genre-aware stratification or simple random assignment.",
    )
    parser.add_argument(
        "--stratify-field",
        default="genre_macro",
        help="Book taxonomy field used for balanced selection. Default: genre_macro",
    )
    return parser.parse_args()


def _shuffled(items: Sequence[BookDoc], rng: random.Random) -> List[BookDoc]:
    out = list(items)
    rng.shuffle(out)
    return out


def apportion_counts_with_limits(
    *,
    weights: Mapping[str, int],
    total: int,
    limits: Mapping[str, int],
) -> Dict[str, int]:
    if total < 0:
        raise ValueError("Total must be non-negative")
    if total == 0:
        return {key: 0 for key in weights}
    if total > sum(max(0, int(limits.get(key, 0))) for key in weights):
        raise ValueError("Requested total exceeds the available per-key limits")

    total_weight = sum(max(0, int(value)) for value in weights.values())
    if total_weight <= 0:
        raise ValueError("Cannot apportion counts with zero total weight")

    exact: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for key, weight in weights.items():
        exact_value = total * (max(0, int(weight)) / total_weight)
        exact[key] = exact_value
        counts[key] = min(int(math.floor(exact_value)), int(limits.get(key, 0)))

    remaining = total - sum(counts.values())
    while remaining > 0:
        candidates = [
            key
            for key in weights
            if counts[key] < int(limits.get(key, 0))
        ]
        if not candidates:
            raise ValueError("Could not complete apportionment within the provided limits")
        best_key = max(
            candidates,
            key=lambda key: (
                exact[key] - counts[key],
                int(weights.get(key, 0)),
                -len(key),
                key,
            ),
        )
        counts[best_key] += 1
        remaining -= 1
    return counts


def _book_bucket_value(book: BookDoc, field_name: str) -> str:
    value = getattr(book.taxonomy, field_name, None)
    text = str(value or "").strip()
    return text or "unknown"


def _book_counter(books: Sequence[BookDoc], field_name: str) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for book in books:
        counter[_book_bucket_value(book, field_name)] += 1
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def select_balanced_books(
    books: Sequence[BookDoc],
    *,
    target_total: int,
    field_name: str,
    rng: random.Random,
) -> Dict[str, List[BookDoc]]:
    by_bucket: MutableMapping[str, List[BookDoc]] = defaultdict(list)
    for book in books:
        by_bucket[_book_bucket_value(book, field_name)].append(book)

    bucket_weights = {bucket: len(items) for bucket, items in by_bucket.items()}
    bucket_limits = dict(bucket_weights)
    selected_per_bucket = apportion_counts_with_limits(weights=bucket_weights, total=target_total, limits=bucket_limits)

    selected: Dict[str, List[BookDoc]] = {}
    for bucket, items in sorted(by_bucket.items(), key=lambda item: item[0]):
        shuffled = _shuffled(sorted(items, key=lambda book: book.book_id), rng)
        keep = int(selected_per_bucket.get(bucket, 0))
        if keep > 0:
            selected[bucket] = shuffled[:keep]
    return selected


def assign_selected_books_to_splits(
    selected_by_bucket: Mapping[str, Sequence[BookDoc]],
    *,
    dev_books: int,
    gate_books: int,
    holdout_books: int,
) -> Dict[str, List[BookDoc]]:
    targets = {
        "development": int(dev_books),
        "gate": int(gate_books),
        "holdout": int(holdout_books),
    }
    total_selected = sum(len(items) for items in selected_by_bucket.values())
    if total_selected != sum(targets.values()):
        raise ValueError(
            f"Selected {total_selected} books but split targets require {sum(targets.values())}."
        )

    queues: Dict[str, List[BookDoc]] = {
        bucket: list(sorted(items, key=lambda book: book.book_id))
        for bucket, items in selected_by_bucket.items()
    }
    ideals: Dict[str, Dict[str, float]] = {
        bucket: {
            split_name: (len(items) * targets[split_name] / total_selected) if total_selected else 0.0
            for split_name in SPLIT_NAMES
        }
        for bucket, items in queues.items()
    }
    current: Dict[str, Dict[str, int]] = {
        bucket: {split_name: 0 for split_name in SPLIT_NAMES}
        for bucket in queues
    }
    remaining = dict(targets)
    assigned = {split_name: [] for split_name in SPLIT_NAMES}
    bucket_order = sorted(queues.keys(), key=lambda bucket: (-len(queues[bucket]), bucket))

    while any(queues[bucket] for bucket in bucket_order):
        made_progress = False
        for bucket in bucket_order:
            if not queues[bucket]:
                continue
            choices = [split_name for split_name in SPLIT_NAMES if remaining[split_name] > 0]
            if not choices:
                raise ValueError("No split capacity remains while books are still unassigned")
            best_split = max(
                choices,
                key=lambda split_name: (
                    ideals[bucket][split_name] - current[bucket][split_name],
                    (remaining[split_name] / targets[split_name]) if targets[split_name] > 0 else -1.0,
                    -SPLIT_NAMES.index(split_name),
                ),
            )
            book = queues[bucket].pop(0)
            assigned[best_split].append(book)
            current[bucket][best_split] += 1
            remaining[best_split] -= 1
            made_progress = True
        if not made_progress:
            break

    if any(remaining.values()):
        raise ValueError(f"Could not fill all split targets: {remaining}")
    return assigned


def build_random_splits(
    books: Sequence[BookDoc],
    *,
    dev_books: int,
    gate_books: int,
    holdout_books: int,
    rng: random.Random,
) -> Dict[str, List[BookDoc]]:
    shuffled = _shuffled(sorted(books, key=lambda book: book.book_id), rng)
    return {
        "development": shuffled[:dev_books],
        "gate": shuffled[dev_books : dev_books + gate_books],
        "holdout": shuffled[dev_books + gate_books : dev_books + gate_books + holdout_books],
    }


def build_balanced_genre_splits(
    books: Sequence[BookDoc],
    *,
    dev_books: int,
    gate_books: int,
    holdout_books: int,
    stratify_field: str,
    rng: random.Random,
) -> Dict[str, List[BookDoc]]:
    if not all(hasattr(book.taxonomy, stratify_field) for book in books):
        raise ValueError(f"Unknown taxonomy field for stratification: {stratify_field!r}")

    eligible = [book for book in books if book.taxonomy.benchmark_pool != "exclude"]
    total_requested = dev_books + gate_books + holdout_books
    if total_requested > len(eligible):
        raise SystemExit(
            f"Requested {total_requested} books across splits but only found {len(eligible)} eligible books under the corpus"
        )

    dev_only_books = [book for book in eligible if book.taxonomy.benchmark_pool == "dev_only"]
    balanced_books = [book for book in eligible if book.taxonomy.benchmark_pool != "dev_only"]

    dev_seed = _shuffled(sorted(dev_only_books, key=lambda book: book.book_id), rng)
    forced_dev = dev_seed[: min(dev_books, len(dev_seed))]
    remaining_dev = dev_books - len(forced_dev)
    remaining_total = remaining_dev + gate_books + holdout_books

    selected_by_bucket = select_balanced_books(
        balanced_books,
        target_total=remaining_total,
        field_name=stratify_field,
        rng=rng,
    ) if remaining_total > 0 else {}
    assigned = assign_selected_books_to_splits(
        selected_by_bucket,
        dev_books=remaining_dev,
        gate_books=gate_books,
        holdout_books=holdout_books,
    ) if remaining_total > 0 else {split_name: [] for split_name in SPLIT_NAMES}
    assigned["development"] = list(forced_dev) + list(assigned["development"])

    if len(assigned["development"]) != dev_books:
        raise ValueError("Development split size mismatch after applying dev_only books")
    return assigned


def split_payload(assigned: Mapping[str, Sequence[BookDoc]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    all_books = [book for split_books in assigned.values() for book in split_books]
    summary["overall_counts"] = {
        "n_books": len(all_books),
        "genre_macro": _book_counter(all_books, "genre_macro"),
        "benchmark_pool": _book_counter(all_books, "benchmark_pool"),
    }
    by_split: Dict[str, Any] = {}
    for split_name in SPLIT_NAMES:
        split_books = list(assigned.get(split_name) or [])
        by_split[split_name] = {
            "book_ids": [book.book_id for book in sorted(split_books, key=lambda book: book.book_id)],
            "n_books": len(split_books),
            "genre_macro": _book_counter(split_books, "genre_macro"),
            "genre_micro": _book_counter(split_books, "genre_micro"),
            "benchmark_pool": _book_counter(split_books, "benchmark_pool"),
            "narrative_vs_expository": _book_counter(split_books, "narrative_vs_expository"),
            "prescriptive_vs_analytical": _book_counter(split_books, "prescriptive_vs_analytical"),
            "quantitative_density": _book_counter(split_books, "quantitative_density"),
            "chapter_length_profile": _book_counter(split_books, "chapter_length_profile"),
        }
    summary["by_split"] = by_split
    return summary


def main() -> None:
    args = parse_args()
    books = list(load_books(args.books_root).values())
    if not books:
        raise SystemExit(f"No books found under {args.books_root}")

    total_requested = args.dev_books + args.gate_books + args.holdout_books
    eligible_books = [book for book in books if book.taxonomy.benchmark_pool != "exclude"]
    if total_requested > len(eligible_books):
        raise SystemExit(
            f"Requested {total_requested} books across splits but only found {len(eligible_books)} eligible books under {args.books_root}"
        )

    rng = random.Random(args.seed)
    if args.split_mode == "random":
        assigned = build_random_splits(
            eligible_books,
            dev_books=args.dev_books,
            gate_books=args.gate_books,
            holdout_books=args.holdout_books,
            rng=rng,
        )
    else:
        assigned = build_balanced_genre_splits(
            eligible_books,
            dev_books=args.dev_books,
            gate_books=args.gate_books,
            holdout_books=args.holdout_books,
            stratify_field=args.stratify_field,
            rng=rng,
        )

    dev = sorted(assigned["development"], key=lambda item: item.book_id)
    gate = sorted(assigned["gate"], key=lambda item: item.book_id)
    holdout = sorted(assigned["holdout"], key=lambda item: item.book_id)

    chapter_fast_rows: List[Dict[str, Any]] = []
    for book in dev:
        for chapter in choose_dev_chapters(book.chapters, args.chapters_per_dev_book):
            chapter_fast_rows.append(chapter_record(book, chapter))

    book_gate_rows = [book_record(book) for book in gate]
    book_holdout_rows = [book_record(book) for book in holdout]

    args.bench_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.bench_dir / "chapter_fast.jsonl", chapter_fast_rows)
    write_jsonl(args.bench_dir / "book_gate.jsonl", book_gate_rows)
    write_jsonl(args.bench_dir / "book_holdout.jsonl", book_holdout_rows)
    write_json(
        args.bench_dir / "splits.json",
        {
            "seed": args.seed,
            "split_mode": args.split_mode,
            "stratify_field": args.stratify_field,
            "development": [book.book_id for book in dev],
            "gate": [book.book_id for book in gate],
            "holdout": [book.book_id for book in holdout],
            "coverage": split_payload({"development": dev, "gate": gate, "holdout": holdout}),
        },
    )

    print(f"Wrote chapter_fast.jsonl with {len(chapter_fast_rows)} samples")
    print(f"Wrote book_gate.jsonl with {len(book_gate_rows)} samples")
    print(f"Wrote book_holdout.jsonl with {len(book_holdout_rows)} samples")
    coverage = split_payload({"development": dev, "gate": gate, "holdout": holdout})
    print(json.dumps(coverage["by_split"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
