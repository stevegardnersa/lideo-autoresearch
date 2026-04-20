#!/usr/bin/env python3
"""Build frozen source-derived rubrics for every book and chapter.

The default mode is deterministic and inexpensive:
- chapter rubrics come from heuristic extraction over the source markdown
- book rubrics are aggregated from the chapter rubrics plus the table of contents
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.book_data import load_books, resolve_book_rubric_path, resolve_chapter_rubric_path
from core.rubrics import aggregate_book_rubric, heuristic_rubric_from_source, write_rubric


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-root", type=Path, default=PROJECT_ROOT / "data" / "books")
    parser.add_argument("--artifacts-root", type=Path, default=PROJECT_ROOT / "artifacts")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    books = load_books(args.books_root)
    if not books:
        raise SystemExit(f"No books found under {args.books_root}")

    n_books = 0
    n_chapters = 0
    for book in books.values():
        chapter_rubrics = []
        for chapter in book.chapters:
            rubric = heuristic_rubric_from_source(chapter.source_md)
            chapter_rubrics.append(rubric)
            chapter_path = resolve_chapter_rubric_path(book, chapter, args.artifacts_root)
            if args.overwrite or not chapter_path.exists():
                write_rubric(chapter_path, rubric)
            n_chapters += 1
        book_rubric = aggregate_book_rubric(chapter_rubrics, toc_md=book.toc_md)
        book_path = resolve_book_rubric_path(book, args.artifacts_root)
        if args.overwrite or not book_path.exists():
            write_rubric(book_path, book_rubric)
        n_books += 1

    print(f"Wrote rubrics for {n_books} books and {n_chapters} chapters under {args.artifacts_root}")


if __name__ == "__main__":
    main()
