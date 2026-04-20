from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

from scoring import visible_word_count


UNKNOWN_LABEL = "unknown"
DEFAULT_BENCHMARK_POOL = "balanced"
_VALID_BENCHMARK_POOLS = {"balanced", "dev_only", "exclude"}


@dataclass(frozen=True)
class BookTaxonomy:
    genre_macro: str = UNKNOWN_LABEL
    genre_micro: str = UNKNOWN_LABEL
    narrative_vs_expository: str = "mixed"
    prescriptive_vs_analytical: str = "mixed"
    quantitative_density: str = UNKNOWN_LABEL
    chapter_length_profile: str = UNKNOWN_LABEL
    benchmark_pool: str = DEFAULT_BENCHMARK_POOL

    def to_dict(self) -> Dict[str, str]:
        return {
            "genre_macro": self.genre_macro,
            "genre_micro": self.genre_micro,
            "narrative_vs_expository": self.narrative_vs_expository,
            "prescriptive_vs_analytical": self.prescriptive_vs_analytical,
            "quantitative_density": self.quantitative_density,
            "chapter_length_profile": self.chapter_length_profile,
            "benchmark_pool": self.benchmark_pool,
        }


@dataclass(frozen=True)
class ChapterDoc:
    chapter_id: str
    title: str
    source_path: Path
    source_md: str
    visible_words: int


@dataclass(frozen=True)
class BookDoc:
    book_id: str
    display_title: str
    book_dir: Path
    manifest_path: Path
    toc_md: str
    metadata_md: str
    chapters: Tuple[ChapterDoc, ...]
    total_visible_words: int
    taxonomy: BookTaxonomy


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_optional_text(path: Path) -> str:
    if not str(path) or str(path) == ".":
        return ""
    return _read_text(path) if path.exists() and path.is_file() else ""


def _clean_label(value: object, *, default: str) -> str:
    text = str(value or "").strip()
    return text if text else default


def taxonomy_from_manifest(manifest: Mapping[str, object]) -> BookTaxonomy:
    benchmark_pool = _clean_label(manifest.get("benchmark_pool"), default=DEFAULT_BENCHMARK_POOL)
    if benchmark_pool not in _VALID_BENCHMARK_POOLS:
        benchmark_pool = DEFAULT_BENCHMARK_POOL
    return BookTaxonomy(
        genre_macro=_clean_label(manifest.get("genre_macro"), default=UNKNOWN_LABEL),
        genre_micro=_clean_label(manifest.get("genre_micro"), default=UNKNOWN_LABEL),
        narrative_vs_expository=_clean_label(manifest.get("narrative_vs_expository"), default="mixed"),
        prescriptive_vs_analytical=_clean_label(manifest.get("prescriptive_vs_analytical"), default="mixed"),
        quantitative_density=_clean_label(manifest.get("quantitative_density"), default=UNKNOWN_LABEL),
        chapter_length_profile=_clean_label(manifest.get("chapter_length_profile"), default=UNKNOWN_LABEL),
        benchmark_pool=benchmark_pool,
    )


def iter_book_manifest_paths(books_root: Path) -> Iterable[Path]:
    if not books_root.exists():
        return []
    manifests = []
    for child in sorted(books_root.iterdir()):
        manifest = child / "book.json"
        if manifest.exists():
            manifests.append(manifest)
    return manifests


def load_book(manifest_path: Path) -> BookDoc:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    book_dir = manifest_path.parent
    book_id = str(manifest.get("book_id") or book_dir.name)
    display_title = str(manifest.get("book_title") or manifest.get("title") or book_id)
    toc_md = _read_optional_text(book_dir / str(manifest.get("toc_path", "")))
    metadata_md = _read_optional_text(book_dir / str(manifest.get("metadata_path", "")))
    taxonomy = taxonomy_from_manifest(manifest)

    chapters: List[ChapterDoc] = []
    for chapter in manifest.get("chapters") or []:
        chapter_id = str(chapter["chapter_id"])
        title = str(chapter.get("title") or chapter_id)
        source_path = book_dir / str(chapter["source_path"])
        source_md = _read_text(source_path)
        chapters.append(
            ChapterDoc(
                chapter_id=chapter_id,
                title=title,
                source_path=source_path,
                source_md=source_md,
                visible_words=visible_word_count(source_md),
            )
        )

    return BookDoc(
        book_id=book_id,
        display_title=display_title,
        book_dir=book_dir,
        manifest_path=manifest_path,
        toc_md=toc_md,
        metadata_md=metadata_md,
        chapters=tuple(chapters),
        total_visible_words=sum(chapter.visible_words for chapter in chapters),
        taxonomy=taxonomy,
    )


def load_books(books_root: Path) -> Dict[str, BookDoc]:
    books: Dict[str, BookDoc] = {}
    for manifest_path in iter_book_manifest_paths(books_root):
        book = load_book(manifest_path)
        books[book.book_id] = book
    return books


def resolve_chapter_rubric_path(book: BookDoc, chapter: ChapterDoc, artifacts_root: Path) -> Path:
    return artifacts_root / "rubrics" / book.book_id / f"{chapter.chapter_id}.json"


def resolve_book_rubric_path(book: BookDoc, artifacts_root: Path) -> Path:
    return artifacts_root / "book_rubrics" / f"{book.book_id}.json"
