from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import shutil
import statistics
import sys
import unicodedata
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


EXCLUDED_TOC_PATTERNS = [
    r"^index$",
    r"^bibliography$",
    r"^references$",
    r"^reference(s)? and notes$",
    r"^notes$",
    r"^endnotes$",
    r"^appendix(?:es)?$",
    r"^glossary$",
    r"^acknowledg(?:e)?ments$",
    r"^about the author$",
    r"^copyright$",
    r"^contents$",
    r"^table of contents$",
    r"^title page$",
    r"^cover$",
]

TITLE_KEYS = ("title", "label", "text", "name")
CONTAINER_KEYS = (
    "toc",
    "entries",
    "items",
    "children",
    "contents",
    "sections",
    "subsections",
    "subitems",
    "navpoint",
    "navpoints",
    "navmap",
)

WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[’'\-][A-Za-z0-9]+)*")
NUMERIC_TOKEN_RE = re.compile(
    r"(?<!\w)(?:[$£€¥]?\d[\d,]*(?:\.\d+)?%?|\d+/\d+|\d+:\d+)(?!\w)",
    flags=re.UNICODE,
)
TABLE_ROW_RE = re.compile(r"\|.*\|")
EQUATIONISH_RE = re.compile(r"(?:\b[A-Za-z]\s*=\s*\S|\b\d+\s*[+\-*/=]\s*\d+)")


@dataclass(frozen=True)
class TocEntry:
    title: str
    level: int
    href: str | None = None
    excluded: bool = False


@dataclass(frozen=True)
class BootstrapResult:
    book_dir: Path
    manifest_path: Path
    metadata_path: Path
    toc_path: Path
    toc_json_path: Path
    chapter_titles_method: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class EpubMetadata:
    title: str | None
    subtitle: str | None
    authors: tuple[str, ...]
    publisher: str | None
    published_date: str | None
    language: str | None
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class MetricSuggestion:
    selected: str
    suggested: str
    details: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create book.json, metadata.md, toc.md, and toc.json for a book directory that already contains chapter markdown files."
        )
    )
    parser.add_argument("--book-dir", required=True, help="Directory containing the chapter markdown files.")
    parser.add_argument(
        "--chapter-glob",
        default="*.md",
        help=(
            "Glob used to find chapter files relative to --book-dir. "
            "Examples: '*.md' for numbered files in the book root, or 'chapters/*.md' for a chapters subdirectory."
        ),
    )
    parser.add_argument(
        "--env-file",
        help=(
            "Optional .env file to load before looking for GOOGLE_BOOKS_API_KEY. "
            "If omitted, the script searches upward from the current working directory and --book-dir."
        ),
    )
    parser.add_argument(
        "--epub",
        help=(
            "Optional explicit EPUB path. If omitted, the script auto-discovers a single .epub file inside --book-dir."
        ),
    )
    parser.add_argument(
        "--volume-json",
        "--google-json",
        dest="volume_json",
        help=(
            "Path to a Google Books JSON file. Supported forms: a raw Volume resource, "
            "an 'items[0]' wrapper from volumes.list, or a list whose first element is the volume."
        ),
    )
    parser.add_argument(
        "--toc-json",
        help=(
            "Path to a parsed EPUB TOC file. Supported forms: JSON or a plain text/markdown list of TOC lines. "
            "If omitted, the script tries to parse the EPUB directly."
        ),
    )
    parser.add_argument(
        "--google-books-api-key",
        help="Optional Google Books API key override. When omitted, GOOGLE_BOOKS_API_KEY is loaded from the environment or .env.",
    )
    parser.add_argument(
        "--google-volume-id",
        help="Optional Google Books volume ID. When provided, the script fetches that exact volume if no --volume-json file is supplied.",
    )
    parser.add_argument(
        "--google-query",
        help=(
            "Optional Google Books search query. When omitted, the script prefers isbn:... and otherwise builds a title/author query."
        ),
    )
    parser.add_argument(
        "--isbn",
        help="Optional ISBN override used for Google Books lookup when --volume-json is not supplied.",
    )
    parser.add_argument("--book-id", help="Optional explicit book_id. Default is derived from title/author/year.")
    parser.add_argument("--book-title", help="Optional explicit book title. Default comes from Google Books, EPUB metadata, or the folder name.")
    parser.add_argument("--subtitle", help="Optional explicit subtitle override.")
    parser.add_argument(
        "--genre-macro",
        default="unknown",
        help="Coarse benchmark bucket, for example business_economics_productivity.",
    )
    parser.add_argument("--genre-micro", default="unknown", help="Finer benchmark subtype.")
    parser.add_argument(
        "--narrative-vs-expository",
        default="unknown",
        choices=("narrative", "expository", "mixed", "unknown"),
        help="Coarse structural style label. Leave as 'unknown' to review manually after bootstrap.",
    )
    parser.add_argument(
        "--prescriptive-vs-analytical",
        default="unknown",
        choices=("prescriptive", "analytical", "mixed", "unknown"),
        help="Coarse instructional style label. Leave as 'unknown' to review manually after bootstrap.",
    )
    parser.add_argument(
        "--quantitative-density",
        default="unknown",
        choices=("low", "medium", "high", "unknown"),
        help="Rough amount of formulas, numbers, or data discussion. Auto-suggested when left as 'unknown'.",
    )
    parser.add_argument(
        "--chapter-length-profile",
        default="unknown",
        choices=("short", "medium", "long", "mixed", "unknown"),
        help="Rough chapter length bucket. Auto-suggested when left as 'unknown'.",
    )
    parser.add_argument(
        "--benchmark-pool",
        default="balanced",
        choices=("balanced", "dev_only", "exclude"),
        help="Benchmark split pool for this book.",
    )
    parser.add_argument(
        "--language",
        help="Optional language override used in metadata.md when Google Books and EPUB metadata are missing or wrong.",
    )
    parser.add_argument(
        "--publisher",
        help="Optional publisher override used in metadata.md when Google Books and EPUB metadata are missing or wrong.",
    )
    parser.add_argument(
        "--published-date",
        help="Optional published date override, for example 2018 or 2018-10-16.",
    )
    parser.add_argument(
        "--description",
        help="Optional description override. When omitted, the script uses a lightly cleaned Google Books description if present.",
    )
    parser.add_argument(
        "--toc-offset",
        type=int,
        help=(
            "Optional manual offset into the non-excluded TOC entries when chapter file count and TOC count differ. "
            "For example, use 1 if the TOC begins with a preface that is not present in your extracted markdown."
        ),
    )
    parser.add_argument(
        "--skip-google-books",
        action="store_true",
        help="Do not query the Google Books API automatically, even if GOOGLE_BOOKS_API_KEY is available.",
    )
    parser.add_argument(
        "--no-write-toc-json",
        action="store_true",
        help="Do not write the normalized toc.json sidecar.",
    )
    parser.add_argument(
        "--copy-raw-json",
        action="store_true",
        help=(
            "Copy the source Google Books and TOC inputs into the book directory as raw_* files for provenance. "
            "When the TOC is parsed from the EPUB, the normalized TOC is also copied to raw_epub_toc.json."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing book.json, metadata.md, toc.md, and toc.json if they already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned outputs without writing files.",
    )
    return parser.parse_args()


def natural_sort_key(value: str) -> list[Any]:
    parts = re.split(r"(\d+)", value)
    key: list[Any] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def discover_chapter_paths(book_dir: Path, chapter_glob: str) -> list[Path]:
    candidates = [path for path in book_dir.glob(chapter_glob) if path.is_file()]
    excluded_names = {"book.json", "metadata.md", "toc.md", "toc.json", "README.md"}
    chapter_paths = [path for path in candidates if path.name not in excluded_names]
    chapter_paths.sort(key=lambda path: natural_sort_key(str(path.relative_to(book_dir))))
    return chapter_paths


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_text_flexible(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def extract_first_markdown_heading(text: str) -> str | None:
    atx_match = re.search(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.MULTILINE)
    if atx_match:
        heading = cleanup_inline_markdown(atx_match.group(1))
        return heading or None

    lines = text.splitlines()
    for index in range(len(lines) - 1):
        line = lines[index].strip()
        underline = lines[index + 1].strip()
        if line and re.fullmatch(r"[=-]{3,}", underline):
            heading = cleanup_inline_markdown(line)
            return heading or None
    return None


def cleanup_inline_markdown(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -#\t\n\r")


def load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def select_google_volume(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        if "volumeInfo" in raw:
            return raw
        items = raw.get("items")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, Mapping):
                return first
        volume = raw.get("volume")
        if isinstance(volume, Mapping):
            return volume
    if isinstance(raw, list) and raw and isinstance(raw[0], Mapping):
        return raw[0]
    raise ValueError("Could not locate a Google Books volume in the provided JSON file.")


def strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def shorten_description(text: str, *, max_sentences: int = 4, max_chars: int = 700) -> str:
    cleaned = strip_html(text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected = " ".join(sentence.strip() for sentence in sentences[:max_sentences] if sentence.strip())
    selected = selected.strip()
    if len(selected) > max_chars:
        selected = selected[: max_chars - 1].rstrip() + "…"
    return selected


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "book"


def extract_year(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4})", value)
    return match.group(1) if match else None


def author_surname(authors: Sequence[str]) -> str | None:
    if not authors:
        return None
    first_author = authors[0].strip()
    if not first_author:
        return None
    parts = re.split(r"\s+", first_author)
    return parts[-1] if parts else None


def extract_isbn(volume_info: Mapping[str, Any], kind: str) -> str | None:
    identifiers = volume_info.get("industryIdentifiers")
    if not isinstance(identifiers, list):
        return None
    for item in identifiers:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("type")) == kind:
            identifier = str(item.get("identifier") or "").strip()
            if identifier:
                return identifier
    return None


def normalize_isbn(candidate: str | None) -> str | None:
    if not candidate:
        return None
    normalized = re.sub(r"[^0-9Xx]", "", candidate)
    if len(normalized) in (10, 13):
        return normalized.upper()
    return None


def collect_scalar_title(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = cleanup_inline_markdown(value)
        return cleaned or None
    return None


def get_mapping_title(node: Mapping[str, Any]) -> str | None:
    for key in TITLE_KEYS:
        title = collect_scalar_title(node.get(key))
        if title:
            return title
    nav_label = node.get("navLabel")
    if isinstance(nav_label, Mapping):
        title = collect_scalar_title(nav_label.get("text"))
        if title:
            return title
    return None


def get_children(node: Mapping[str, Any]) -> list[Any]:
    children: list[Any] = []
    for key in CONTAINER_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            children.extend(value)
        elif isinstance(value, Mapping):
            children.append(value)
    return children


def is_excluded_toc_title(title: str) -> bool:
    simplified = simplify_title(title)
    return any(re.fullmatch(pattern, simplified) for pattern in EXCLUDED_TOC_PATTERNS)


def flatten_toc_json(node: Any, level: int = 1) -> list[TocEntry]:
    entries: list[TocEntry] = []
    if isinstance(node, list):
        for item in node:
            entries.extend(flatten_toc_json(item, level=level))
        return entries

    if isinstance(node, Mapping):
        title = get_mapping_title(node)
        entry_level = node.get("level")
        if isinstance(entry_level, int) and entry_level > 0:
            effective_level = entry_level
        elif isinstance(entry_level, str) and entry_level.isdigit() and int(entry_level) > 0:
            effective_level = int(entry_level)
        else:
            effective_level = level

        if title:
            href = None
            for key in ("href", "src", "path", "id"):
                raw = node.get(key)
                if raw is not None:
                    href = str(raw)
                    break
            entries.append(
                TocEntry(
                    title=title,
                    level=max(1, effective_level),
                    href=href,
                    excluded=is_excluded_toc_title(title),
                )
            )
            children = get_children(node)
            for child in children:
                entries.extend(flatten_toc_json(child, level=max(1, effective_level + 1)))
            return entries

        for key in CONTAINER_KEYS:
            if key in node:
                entries.extend(flatten_toc_json(node[key], level=level))
        return entries

    title = collect_scalar_title(node)
    if title:
        entries.append(TocEntry(title=title, level=max(1, level), excluded=is_excluded_toc_title(title)))
    return entries


def flatten_toc_text(text: str) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = cleanup_inline_markdown(stripped.lstrip("#"))
            level = max(1, len(stripped) - len(stripped.lstrip("#")))
        else:
            indent = len(line) - len(line.lstrip(" \t"))
            level = 1 + indent // 2
            title = cleanup_inline_markdown(re.sub(r"^[\-+*]\s+", "", stripped))
            title = cleanup_inline_markdown(re.sub(r"^\d+[.)]\s+", "", title))
        if not title:
            continue
        entries.append(TocEntry(title=title, level=level, excluded=is_excluded_toc_title(title)))
    return entries


def load_toc_entries(path: Path | None) -> list[TocEntry]:
    if path is None:
        return []
    suffix = path.suffix.lower()
    if suffix == ".json":
        return flatten_toc_json(load_json_file(path))
    return flatten_toc_text(read_text_flexible(path))


def simplify_title(text: str) -> str:
    text = cleanup_inline_markdown(text)
    text = text.lower()
    text = html.unescape(text)
    text = re.sub(r"^[^a-z0-9]+", "", text)
    text = re.sub(r"\bchapter\s+\d+\b", "", text)
    text = re.sub(r"\bpart\s+[ivxlcdm0-9]+\b", "", text)
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_similarity(a: str, b: str) -> float:
    a_simple = simplify_title(a)
    b_simple = simplify_title(b)
    if not a_simple or not b_simple:
        return 0.0
    return difflib.SequenceMatcher(None, a_simple, b_simple).ratio()


def normalize_match_text(text: str | None) -> str:
    if text is None:
        return ""
    cleaned = cleanup_inline_markdown(strip_html(str(text)))
    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\b(?:a|an|the)\b", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def text_similarity(a: str | None, b: str | None) -> float:
    a_simple = normalize_match_text(a)
    b_simple = normalize_match_text(b)
    if not a_simple or not b_simple:
        return 0.0
    return difflib.SequenceMatcher(None, a_simple, b_simple).ratio()


def exactish_text_match(a: str | None, b: str | None) -> bool:
    a_simple = normalize_match_text(a)
    b_simple = normalize_match_text(b)
    return bool(a_simple and b_simple and a_simple == b_simple)


def score_year_match(desired_published_date: str | None, candidate_published_date: str | None) -> float:
    desired_year = extract_year(desired_published_date)
    candidate_year = extract_year(candidate_published_date)
    if not desired_year or not candidate_year:
        return 0.0
    if desired_year == candidate_year:
        return 1.0
    try:
        diff = abs(int(desired_year) - int(candidate_year))
    except ValueError:
        return 0.0
    if diff == 1:
        return 0.6
    if diff == 2:
        return 0.25
    return 0.0


def quote_google_query_term(term: str | None) -> str | None:
    if term is None:
        return None
    cleaned = cleanup_inline_markdown(strip_html(str(term))).strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace('"', '')
    return f'"{cleaned}"'


def build_google_books_queries(
    *,
    explicit_query: str | None,
    isbn: str | None,
    title: str | None,
    subtitle: str | None,
    authors: Sequence[str],
    publisher: str | None,
) -> list[str]:
    if explicit_query and explicit_query.strip():
        return [explicit_query.strip()]

    normalized_isbn = normalize_isbn(isbn)
    if normalized_isbn:
        return [f"isbn:{normalized_isbn}"]

    title_phrase = quote_google_query_term(title)
    subtitle_phrase = quote_google_query_term(subtitle)
    author_phrase = quote_google_query_term(authors[0] if authors else None)
    publisher_phrase = quote_google_query_term(publisher)

    queries: list[str] = []
    if title_phrase and author_phrase and publisher_phrase:
        queries.append(f"intitle:{title_phrase} inauthor:{author_phrase} inpublisher:{publisher_phrase}")
    if title_phrase and author_phrase:
        queries.append(f"intitle:{title_phrase} inauthor:{author_phrase}")
    if title_phrase and publisher_phrase:
        queries.append(f"intitle:{title_phrase} inpublisher:{publisher_phrase}")
    if title_phrase and subtitle_phrase and author_phrase:
        queries.append(f"intitle:{title_phrase} {subtitle_phrase} inauthor:{author_phrase}")
    if title_phrase and subtitle_phrase:
        queries.append(f"intitle:{title_phrase} {subtitle_phrase}")
    if title_phrase:
        queries.append(f"intitle:{title_phrase}")
    if title and subtitle:
        combined = quote_google_query_term(f"{title} {subtitle}")
        if combined:
            queries.append(combined)
    if title:
        raw_title = quote_google_query_term(title)
        if raw_title:
            queries.append(raw_title)

    unique_queries: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = query.strip()
        if normalized and normalized not in seen:
            unique_queries.append(normalized)
            seen.add(normalized)
    return unique_queries


def summarize_google_volume(item: Mapping[str, Any], *, score: float | None = None, components: Mapping[str, Any] | None = None) -> dict[str, Any]:
    volume_info = item.get("volumeInfo") if isinstance(item, Mapping) else None
    if not isinstance(volume_info, Mapping):
        volume_info = {}
    summary = {
        "volume_id": str(item.get("id")) if isinstance(item.get("id"), str) else None,
        "title": str(volume_info.get("title") or "").strip() or None,
        "subtitle": str(volume_info.get("subtitle") or "").strip() or None,
        "authors": [str(author) for author in (volume_info.get("authors") or []) if str(author).strip()],
        "publisher": str(volume_info.get("publisher") or "").strip() or None,
        "published_date": str(volume_info.get("publishedDate") or "").strip() or None,
        "isbn_13": extract_isbn(volume_info, "ISBN_13"),
        "isbn_10": extract_isbn(volume_info, "ISBN_10"),
    }
    if score is not None:
        summary["score"] = round(float(score), 4)
    if components:
        summary["score_components"] = dict(components)
    return summary


def score_google_volume_candidate(
    item: Mapping[str, Any],
    *,
    desired_title: str | None,
    desired_subtitle: str | None,
    desired_authors: Sequence[str],
    desired_isbn: str | None,
    desired_publisher: str | None,
    desired_published_date: str | None,
) -> tuple[float, dict[str, Any]]:
    volume_info = item.get("volumeInfo")
    if not isinstance(volume_info, Mapping):
        return -1.0, {"invalid": True}

    score = 0.0
    components: dict[str, Any] = {}
    candidate_title = str(volume_info.get("title") or "")
    candidate_subtitle = str(volume_info.get("subtitle") or "")
    candidate_authors = [str(author) for author in (volume_info.get("authors") or []) if str(author).strip()]
    candidate_publisher = str(volume_info.get("publisher") or "")
    candidate_published_date = str(volume_info.get("publishedDate") or "")

    if desired_title and candidate_title:
        similarity = title_similarity(desired_title, candidate_title)
        components["title_similarity"] = round(similarity, 4)
        score += 3.5 * similarity
        if exactish_text_match(desired_title, candidate_title):
            components["title_exactish_bonus"] = 0.75
            score += 0.75

    if desired_subtitle and candidate_subtitle:
        similarity = text_similarity(desired_subtitle, candidate_subtitle)
        components["subtitle_similarity"] = round(similarity, 4)
        score += 1.5 * similarity
        if exactish_text_match(desired_subtitle, candidate_subtitle):
            components["subtitle_exactish_bonus"] = 0.35
            score += 0.35

    if desired_title and desired_subtitle and candidate_title:
        desired_combined = f"{desired_title}: {desired_subtitle}" if desired_subtitle else desired_title
        candidate_combined = f"{candidate_title}: {candidate_subtitle}" if candidate_subtitle else candidate_title
        similarity = text_similarity(desired_combined, candidate_combined)
        components["combined_title_similarity"] = round(similarity, 4)
        score += 1.0 * similarity

    if desired_authors and candidate_authors:
        author_scores = [
            text_similarity(desired_author, candidate_author)
            for desired_author in desired_authors
            for candidate_author in candidate_authors
        ]
        if author_scores:
            best_author = max(author_scores)
            components["author_similarity"] = round(best_author, 4)
            score += 1.5 * best_author

    normalized_wanted_isbn = normalize_isbn(desired_isbn)
    if normalized_wanted_isbn:
        isbn_13 = extract_isbn(volume_info, "ISBN_13")
        isbn_10 = extract_isbn(volume_info, "ISBN_10")
        if normalize_isbn(isbn_13) == normalized_wanted_isbn:
            components["isbn_exact_bonus"] = 6.0
            components["isbn_match_type"] = "ISBN_13"
            score += 6.0
        elif normalize_isbn(isbn_10) == normalized_wanted_isbn:
            components["isbn_exact_bonus"] = 6.0
            components["isbn_match_type"] = "ISBN_10"
            score += 6.0

    if desired_publisher and candidate_publisher:
        similarity = text_similarity(desired_publisher, candidate_publisher)
        components["publisher_similarity"] = round(similarity, 4)
        score += 1.0 * similarity
        if exactish_text_match(desired_publisher, candidate_publisher):
            components["publisher_exactish_bonus"] = 0.35
            score += 0.35

    year_bonus = score_year_match(desired_published_date, candidate_published_date)
    if year_bonus:
        components["published_year_bonus"] = year_bonus
        components["desired_year"] = extract_year(desired_published_date)
        components["candidate_year"] = extract_year(candidate_published_date)
        score += year_bonus

    components["total_score"] = round(score, 4)
    return score, components


def choose_toc_slice(
    toc_titles: Sequence[str],
    chapter_heading_titles: Sequence[str | None],
    n_chapters: int,
    manual_offset: int | None,
) -> tuple[list[str] | None, str, list[str]]:
    warnings: list[str] = []
    if not toc_titles:
        return None, "chapter_headings_or_fallback", warnings

    if manual_offset is not None:
        start = max(0, manual_offset)
        if start + n_chapters <= len(toc_titles):
            return list(toc_titles[start : start + n_chapters]), f"toc_manual_offset_{start}", warnings
        warnings.append(
            f"Manual TOC offset {manual_offset} is out of range for {len(toc_titles)} non-excluded TOC entries."
        )

    if len(toc_titles) == n_chapters:
        return list(toc_titles), "toc_exact", warnings

    comparable_indices = [index for index, title in enumerate(chapter_heading_titles) if title]
    if len(comparable_indices) >= 2 and len(toc_titles) >= n_chapters:
        best_start: int | None = None
        best_score = -1.0
        for start in range(len(toc_titles) - n_chapters + 1):
            scores: list[float] = []
            for index in comparable_indices:
                chapter_title = chapter_heading_titles[index]
                if not chapter_title:
                    continue
                scores.append(title_similarity(chapter_title, toc_titles[start + index]))
            if scores:
                score = sum(scores) / len(scores)
                if score > best_score:
                    best_score = score
                    best_start = start
        if best_start is not None and best_score >= 0.55:
            return list(toc_titles[best_start : best_start + n_chapters]), f"toc_aligned_offset_{best_start}", warnings

    if len(toc_titles) >= n_chapters:
        warnings.append(
            "TOC and chapter counts differ. Falling back to chapter headings where possible because the TOC could not be aligned confidently."
        )
    else:
        warnings.append(
            f"Only {len(toc_titles)} non-excluded TOC entries were found for {n_chapters} chapter files. Missing titles will fall back to chapter headings."
        )
    return None, "chapter_headings_or_fallback", warnings


def assign_chapter_titles(
    chapter_paths: Sequence[Path], toc_entries: Sequence[TocEntry], manual_offset: int | None
) -> tuple[list[str], str, list[str]]:
    chapter_heading_titles = [extract_first_markdown_heading(read_text_flexible(path)) for path in chapter_paths]
    toc_titles = [entry.title for entry in toc_entries if not entry.excluded]
    chosen_slice, method, warnings = choose_toc_slice(
        toc_titles=toc_titles,
        chapter_heading_titles=chapter_heading_titles,
        n_chapters=len(chapter_paths),
        manual_offset=manual_offset,
    )

    titles: list[str] = []
    for index, path in enumerate(chapter_paths):
        title: str | None = None
        if chosen_slice is not None:
            title = chosen_slice[index]
        if not title:
            title = chapter_heading_titles[index]
        if not title:
            title = f"Chapter {index + 1}"
            warnings.append(f"No TOC title or markdown heading found for {path.name}; used fallback title '{title}'.")
        titles.append(title)
    return titles, method, warnings


def format_toc_markdown(toc_entries: Sequence[TocEntry], fallback_titles: Sequence[str]) -> str:
    lines = ["# Table of contents", ""]
    if toc_entries:
        included = [entry for entry in toc_entries if not entry.excluded]
        excluded = [entry for entry in toc_entries if entry.excluded]
        for entry in included:
            indent = "  " * (max(1, entry.level) - 1)
            lines.append(f"{indent}- {entry.title}")
        if excluded:
            lines.extend(["", "## Excluded from benchmark corpus", ""])
            for entry in excluded:
                lines.append(f"- {entry.title}")
    else:
        for title in fallback_titles:
            lines.append(f"- {title}")
    lines.append("")
    return "\n".join(lines)


def normalize_toc_json(toc_entries: Sequence[TocEntry]) -> list[dict[str, Any]]:
    return [
        {
            "title": entry.title,
            "level": entry.level,
            "href": entry.href,
            "excluded": entry.excluded,
        }
        for entry in toc_entries
    ]


def infer_book_id(
    explicit_book_id: str | None,
    title: str,
    authors: Sequence[str],
    published_date: str | None,
    fallback_dir_name: str,
) -> str:
    if explicit_book_id:
        return slugify(explicit_book_id)
    pieces: list[str] = []
    if title:
        pieces.append(slugify(title))
    surname = author_surname(authors)
    if surname:
        pieces.append(slugify(surname))
    year = extract_year(published_date)
    if year:
        pieces.append(year)
    candidate = "-".join(piece for piece in pieces if piece)
    return candidate or slugify(fallback_dir_name)


def infer_book_title(
    explicit_title: str | None,
    volume_info: Mapping[str, Any] | None,
    epub_metadata: EpubMetadata | None,
    fallback: str,
) -> str:
    if explicit_title:
        return explicit_title.strip()
    if volume_info:
        title = str(volume_info.get("title") or "").strip()
        if title:
            return title
    if epub_metadata and epub_metadata.title:
        return epub_metadata.title
    return fallback


def build_metadata_markdown(
    *,
    title: str,
    subtitle: str | None,
    authors: Sequence[str],
    publisher: str | None,
    published_date: str | None,
    language: str | None,
    page_count: int | str | None,
    categories: Sequence[str],
    isbn_13: str | None,
    isbn_10: str | None,
    google_volume_id: str | None,
    description: str | None,
) -> str:
    lines = ["# Book metadata", ""]
    metadata_items = [
        ("Title", title),
        ("Subtitle", subtitle),
        ("Authors", ", ".join(authors) if authors else None),
        ("Publisher", publisher),
        ("Published date", published_date),
        ("Language", language),
        ("Page count", str(page_count) if page_count else None),
        ("Categories", ", ".join(categories) if categories else None),
        ("ISBN-13", isbn_13),
        ("ISBN-10", isbn_10),
        ("Google Books volume ID", google_volume_id),
    ]
    for label, value in metadata_items:
        if value:
            lines.append(f"- {label}: {value}")

    if description:
        lines.extend(["", "## Description", "", description])
    lines.append("")
    return "\n".join(lines)


def build_manifest(
    *,
    book_id: str,
    book_title: str,
    google_volume_id: str | None,
    isbn_13: str | None,
    genre_macro: str,
    genre_micro: str,
    narrative_vs_expository: str,
    prescriptive_vs_analytical: str,
    quantitative_density: str,
    chapter_length_profile: str,
    benchmark_pool: str,
    toc_path: str,
    metadata_path: str,
    toc_json_path: str | None,
    chapter_paths: Sequence[Path],
    chapter_titles: Sequence[str],
    book_dir: Path,
) -> dict[str, Any]:
    width = max(3, len(str(len(chapter_paths) - 1)))
    chapters: list[dict[str, str]] = []
    for index, path in enumerate(chapter_paths):
        chapters.append(
            {
                "chapter_id": f"{index:0{width}d}",
                "title": chapter_titles[index],
                "source_path": str(path.relative_to(book_dir)).replace("\\", "/"),
            }
        )

    manifest: dict[str, Any] = {
        "book_id": book_id,
        "book_title": book_title,
        "genre_macro": genre_macro,
        "genre_micro": genre_micro,
        "narrative_vs_expository": narrative_vs_expository,
        "prescriptive_vs_analytical": prescriptive_vs_analytical,
        "quantitative_density": quantitative_density,
        "chapter_length_profile": chapter_length_profile,
        "benchmark_pool": benchmark_pool,
        "toc_path": toc_path,
        "metadata_path": metadata_path,
        "chapters": chapters,
    }
    if toc_json_path:
        manifest["toc_json_path"] = toc_json_path
    if google_volume_id:
        manifest["google_books_volume_id"] = google_volume_id
    if isbn_13:
        manifest["isbn_13"] = isbn_13
    return manifest


def ensure_writable(path: Path, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file without --overwrite: {path}")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_raw_json(source: Path | None, destination: Path, *, overwrite: bool) -> None:
    if source is None:
        return
    if destination.exists() and not overwrite:
        return
    shutil.copyfile(source, destination)


def write_json(path: Path, payload: Any, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file without --overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def markdown_to_visible_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[(.*?)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?\s*:?[-]{3,}:?\s*(?:\|\s*:?[-]{3,}:?\s*)+$", " ", text, flags=re.MULTILINE)
    text = strip_html(text)
    text = re.sub(r"[_*~]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def count_visible_words(text: str) -> int:
    return len(WORD_RE.findall(markdown_to_visible_text(text)))


def count_numeric_tokens(text: str) -> int:
    visible = markdown_to_visible_text(text)
    return len(NUMERIC_TOKEN_RE.findall(visible))


def count_table_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if TABLE_ROW_RE.search(line))


def count_equationish_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if EQUATIONISH_RE.search(line))


def suggest_quantitative_density(chapter_texts: Sequence[str], selected: str) -> MetricSuggestion:
    total_words = sum(count_visible_words(text) for text in chapter_texts)
    numeric_tokens = sum(count_numeric_tokens(text) for text in chapter_texts)
    table_lines = sum(count_table_lines(text) for text in chapter_texts)
    equationish_lines = sum(count_equationish_lines(text) for text in chapter_texts)
    rate = 1000.0 * numeric_tokens / max(total_words, 1)

    if rate < 5.0:
        suggested = "low"
    elif rate <= 15.0:
        suggested = "medium"
    else:
        suggested = "high"

    if suggested == "low" and rate >= 4.0 and (table_lines >= 6 or equationish_lines >= 3):
        suggested = "medium"
    elif suggested == "medium" and rate >= 12.0 and (table_lines >= 12 or equationish_lines >= 6):
        suggested = "high"

    details = {
        "total_visible_words": total_words,
        "numeric_token_count": numeric_tokens,
        "numeric_tokens_per_1000_words": round(rate, 2),
        "table_line_count": table_lines,
        "equationish_line_count": equationish_lines,
        "heuristic_thresholds": {
            "low_lt": 5.0,
            "medium_lte": 15.0,
        },
    }
    return MetricSuggestion(selected=suggested if selected == "unknown" else selected, suggested=suggested, details=details)


def suggest_chapter_length_profile(chapter_texts: Sequence[str], selected: str) -> tuple[MetricSuggestion, list[int]]:
    chapter_word_counts = [count_visible_words(text) for text in chapter_texts]
    if not chapter_word_counts:
        details = {"chapter_word_counts": [], "median_chapter_words": 0}
        return MetricSuggestion(selected=selected, suggested="unknown", details=details), chapter_word_counts

    median_words = float(statistics.median(chapter_word_counts))
    min_words = min(chapter_word_counts)
    max_words = max(chapter_word_counts)
    has_short = any(count < 1500 for count in chapter_word_counts)
    has_long = any(count > 5000 for count in chapter_word_counts)
    looks_mixed = (min_words < 0.5 * median_words and max_words > 1.8 * median_words) or (has_short and has_long)

    if looks_mixed:
        suggested = "mixed"
    elif median_words < 1800:
        suggested = "short"
    elif median_words <= 4500:
        suggested = "medium"
    else:
        suggested = "long"

    details = {
        "chapter_word_counts": chapter_word_counts,
        "median_chapter_words": round(median_words, 1),
        "min_chapter_words": min_words,
        "max_chapter_words": max_words,
        "mixed_heuristic_triggered": looks_mixed,
        "heuristic_thresholds": {
            "short_lt": 1800,
            "medium_lte": 4500,
            "mixed_min_ratio_lt": 0.5,
            "mixed_max_ratio_gt": 1.8,
            "mixed_short_any_lt": 1500,
            "mixed_long_any_gt": 5000,
        },
    }
    return MetricSuggestion(selected=suggested if selected == "unknown" else selected, suggested=suggested, details=details), chapter_word_counts


def find_dotenv_candidates(book_dir: Path, explicit_path: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser().resolve())
        return candidates

    seen: set[str] = set()
    for start in [Path.cwd().resolve(), book_dir.resolve()]:
        current = start
        while True:
            candidate = current / ".env"
            key = str(candidate)
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)
            if current.parent == current:
                break
            current = current.parent
    return candidates


def load_env_file(path: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return loaded
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


def prepare_environment(book_dir: Path, explicit_env_file: str | None) -> dict[str, Any]:
    env_files = find_dotenv_candidates(book_dir, explicit_env_file)
    loaded_from: list[str] = []
    for path in env_files:
        loaded = load_env_file(path)
        if loaded:
            loaded_from.append(str(path))
            break
    return {"env_files_considered": [str(path) for path in env_files], "env_loaded_from": loaded_from}


def discover_epub_path(book_dir: Path, explicit_epub: str | None) -> Path | None:
    if explicit_epub:
        epub_path = Path(explicit_epub).expanduser().resolve()
        if not epub_path.exists():
            raise FileNotFoundError(f"EPUB file does not exist: {epub_path}")
        return epub_path
    epubs = sorted(book_dir.glob("*.epub"), key=lambda path: natural_sort_key(path.name))
    if not epubs:
        return None
    if len(epubs) > 1:
        raise FileExistsError(
            f"Found multiple EPUB files inside {book_dir}. Please specify one explicitly with --epub."
        )
    return epubs[0].resolve()


def ns_tag(namespace: str | None, local: str) -> str:
    return f"{{{namespace}}}{local}" if namespace else local


def element_localname(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_xml_bytes(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse XML content: {exc}") from exc


def read_zip_member(zf: zipfile.ZipFile, member: str) -> bytes:
    with zf.open(member) as handle:
        return handle.read()


def resolve_zip_path(base: str, href: str) -> str:
    base_parent = PurePosixPath(base).parent
    resolved = (base_parent / href).as_posix()
    parts: list[str] = []
    for part in PurePosixPath(resolved).parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def parse_opf_package(zf: zipfile.ZipFile, opf_path: str) -> tuple[EpubMetadata, dict[str, dict[str, str]], str | None]:
    root = parse_xml_bytes(read_zip_member(zf, opf_path))
    package_ns = "http://www.idpf.org/2007/opf"
    dc_ns = "http://purl.org/dc/elements/1.1/"

    metadata_node = root.find(ns_tag(package_ns, "metadata"))
    manifest_node = root.find(ns_tag(package_ns, "manifest"))
    spine_node = root.find(ns_tag(package_ns, "spine"))

    def collect_dc(local: str) -> list[str]:
        if metadata_node is None:
            return []
        values: list[str] = []
        for element in metadata_node.findall(ns_tag(dc_ns, local)):
            if element.text and element.text.strip():
                values.append(element.text.strip())
        return values

    titles = collect_dc("title")
    creators = collect_dc("creator")
    languages = collect_dc("language")
    publishers = collect_dc("publisher")
    dates = collect_dc("date")
    identifiers = collect_dc("identifier")

    title = titles[0] if titles else None
    subtitle = titles[1] if len(titles) > 1 else None

    manifest: dict[str, dict[str, str]] = {}
    if manifest_node is not None:
        for item in manifest_node.findall(ns_tag(package_ns, "item")):
            item_id = item.attrib.get("id")
            href = item.attrib.get("href")
            if not item_id or not href:
                continue
            manifest[item_id] = {
                "href": resolve_zip_path(opf_path, href),
                "media-type": item.attrib.get("media-type", ""),
                "properties": item.attrib.get("properties", ""),
            }

    spine_toc_id = spine_node.attrib.get("toc") if spine_node is not None else None
    metadata = EpubMetadata(
        title=title,
        subtitle=subtitle,
        authors=tuple(creators),
        publisher=publishers[0] if publishers else None,
        published_date=dates[0] if dates else None,
        language=languages[0] if languages else None,
        identifiers=tuple(identifiers),
    )
    return metadata, manifest, spine_toc_id


def collect_text_content(element: ET.Element) -> str:
    parts = [text.strip() for text in element.itertext() if text and text.strip()]
    return cleanup_inline_markdown(" ".join(parts))


def parse_nav_ol_list(ol_node: ET.Element, level: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for li in list(ol_node):
        if element_localname(li.tag) != "li":
            continue
        title: str | None = None
        href: str | None = None
        child_ol: ET.Element | None = None
        for child in list(li):
            local = element_localname(child.tag)
            if local in {"a", "span"} and title is None:
                title = collect_text_content(child)
                if local == "a":
                    href = child.attrib.get("href")
            elif local == "ol":
                child_ol = child
            elif title is None:
                maybe_title = collect_text_content(child)
                if maybe_title:
                    title = maybe_title
        if title is None:
            title = collect_text_content(li)
        if not title:
            continue
        entry: dict[str, Any] = {
            "title": title,
            "level": level,
            "href": href,
        }
        if child_ol is not None:
            children = parse_nav_ol_list(child_ol, level + 1)
            if children:
                entry["children"] = children
        entries.append(entry)
    return entries


def parse_epub_nav_toc(zf: zipfile.ZipFile, nav_path: str) -> list[dict[str, Any]]:
    root = parse_xml_bytes(read_zip_member(zf, nav_path))
    nav_candidates = [element for element in root.iter() if element_localname(element.tag) == "nav"]
    chosen: ET.Element | None = None
    for nav in nav_candidates:
        attributes = {element_localname(key): value for key, value in nav.attrib.items()}
        raw_type = " ".join(attributes.get(key, "") for key in ("type", "epub:type", "role"))
        raw_id = attributes.get("id", "")
        if any(token in raw_type.lower() for token in ("toc", "doc-toc")) or raw_id.lower() == "toc":
            chosen = nav
            break
    if chosen is None and nav_candidates:
        chosen = nav_candidates[0]
    if chosen is None:
        return []
    for child in list(chosen):
        if element_localname(child.tag) == "ol":
            return parse_nav_ol_list(child, level=1)
    return []


def parse_ncx_navpoints(navpoint_nodes: Iterable[ET.Element], level: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for navpoint in navpoint_nodes:
        if element_localname(navpoint.tag) != "navPoint":
            continue
        title: str | None = None
        href: str | None = None
        for child in list(navpoint):
            local = element_localname(child.tag)
            if local == "navLabel":
                title = collect_text_content(child)
            elif local == "content":
                href = child.attrib.get("src")
        if not title:
            continue
        entry: dict[str, Any] = {
            "title": title,
            "level": level,
            "href": href,
        }
        children = parse_ncx_navpoints([child for child in list(navpoint) if element_localname(child.tag) == "navPoint"], level + 1)
        if children:
            entry["children"] = children
        entries.append(entry)
    return entries


def parse_epub_ncx_toc(zf: zipfile.ZipFile, ncx_path: str) -> list[dict[str, Any]]:
    root = parse_xml_bytes(read_zip_member(zf, ncx_path))
    navmap: ET.Element | None = None
    for element in root.iter():
        if element_localname(element.tag) == "navMap":
            navmap = element
            break
    if navmap is None:
        return []
    return parse_ncx_navpoints(list(navmap), level=1)


def flatten_nested_toc_items(items: Sequence[Mapping[str, Any]], level: int = 1) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for item in items:
        title = collect_scalar_title(item.get("title") or item.get("label") or item.get("text"))
        if not title:
            continue
        item_level_raw = item.get("level")
        item_level = level
        if isinstance(item_level_raw, int) and item_level_raw > 0:
            item_level = item_level_raw
        href = str(item.get("href")) if item.get("href") is not None else None
        entries.append(
            TocEntry(
                title=title,
                level=max(1, item_level),
                href=href,
                excluded=is_excluded_toc_title(title),
            )
        )
        children = item.get("children")
        if isinstance(children, list):
            entries.extend(flatten_nested_toc_items(children, level=max(1, item_level + 1)))
    return entries


def extract_epub_metadata_and_toc(epub_path: Path) -> tuple[EpubMetadata | None, list[TocEntry], list[dict[str, Any]] | None]:
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            container_root = parse_xml_bytes(read_zip_member(zf, "META-INF/container.xml"))
            rootfile_path: str | None = None
            for element in container_root.iter():
                if element_localname(element.tag) == "rootfile":
                    candidate = element.attrib.get("full-path")
                    if candidate:
                        rootfile_path = candidate
                        break
            if not rootfile_path:
                raise ValueError("Could not locate the OPF package path inside META-INF/container.xml")

            metadata, manifest, spine_toc_id = parse_opf_package(zf, rootfile_path)

            nav_path: str | None = None
            for item in manifest.values():
                if "nav" in item.get("properties", "").split():
                    nav_path = item["href"]
                    break

            raw_toc: list[dict[str, Any]] | None = None
            if nav_path:
                raw_toc = parse_epub_nav_toc(zf, nav_path)
            if not raw_toc:
                ncx_path: str | None = None
                if spine_toc_id and spine_toc_id in manifest:
                    ncx_path = manifest[spine_toc_id]["href"]
                else:
                    for item in manifest.values():
                        if item.get("media-type") == "application/x-dtbncx+xml":
                            ncx_path = item["href"]
                            break
                if ncx_path:
                    raw_toc = parse_epub_ncx_toc(zf, ncx_path)

            toc_entries = flatten_nested_toc_items(raw_toc or [])
            return metadata, toc_entries, raw_toc
    except KeyError as exc:
        raise ValueError(f"EPUB is missing a required file: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid EPUB archive: {exc}") from exc


def extract_epub_isbn(epub_metadata: EpubMetadata | None) -> str | None:
    if epub_metadata is None:
        return None
    for identifier in epub_metadata.identifiers:
        isbn = normalize_isbn(identifier)
        if isbn:
            return isbn
        match = re.search(r"(97[89]\d{10}|\d{9}[\dXx])", identifier)
        if match:
            isbn = normalize_isbn(match.group(1))
            if isbn:
                return isbn
    return None


def google_books_request(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "autoresearch-bootstrap/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def choose_best_google_volume(
    items: Sequence[Mapping[str, Any]],
    *,
    desired_title: str | None,
    desired_subtitle: str | None,
    desired_authors: Sequence[str],
    desired_isbn: str | None,
    desired_publisher: str | None,
    desired_published_date: str | None,
) -> tuple[Mapping[str, Any] | None, list[dict[str, Any]]]:
    if not items:
        return None, []

    ranked: list[tuple[float, dict[str, Any], Mapping[str, Any]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        score, components = score_google_volume_candidate(
            item,
            desired_title=desired_title,
            desired_subtitle=desired_subtitle,
            desired_authors=desired_authors,
            desired_isbn=desired_isbn,
            desired_publisher=desired_publisher,
            desired_published_date=desired_published_date,
        )
        ranked.append((score, components, item))

    if not ranked:
        return None, []

    ranked.sort(key=lambda entry: entry[0], reverse=True)
    ranking_details = [summarize_google_volume(item, score=score, components=components) for score, components, item in ranked]
    return ranked[0][2], ranking_details


def fetch_google_books_volume(
    *,
    api_key: str,
    volume_id: str | None,
    query: str | None,
    isbn: str | None,
    title: str | None,
    subtitle: str | None,
    authors: Sequence[str],
    publisher: str | None,
    published_date: str | None,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    trace: dict[str, Any] = {
        "source": "google_volume_id" if volume_id else ("google_query" if query else "automatic_lookup"),
        "desired": {
            "title": title,
            "subtitle": subtitle,
            "authors": list(authors),
            "isbn": isbn,
            "publisher": publisher,
            "published_date": published_date,
            "published_year": extract_year(published_date),
        },
        "queries": [],
    }

    if volume_id:
        url = (
            "https://www.googleapis.com/books/v1/volumes/"
            + urllib.parse.quote(volume_id)
            + "?key="
            + urllib.parse.quote(api_key)
        )
        response = google_books_request(url)
        selected = select_google_volume(response)
        trace["selected"] = summarize_google_volume(selected)
        return selected, trace

    effective_queries = build_google_books_queries(
        explicit_query=query,
        isbn=isbn,
        title=title,
        subtitle=subtitle,
        authors=authors,
        publisher=publisher,
    )
    if not effective_queries:
        trace["status"] = "no_query"
        return None, trace

    deduped_items: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for effective_query in effective_queries:
        params = {
            "q": effective_query,
            "key": api_key,
            "maxResults": "10",
            "printType": "books",
        }
        url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
        response = google_books_request(url)
        items = response.get("items") if isinstance(response, Mapping) else None
        typed_items = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
        trace["queries"].append({"query": effective_query, "returned_items": len(typed_items)})
        for item in typed_items:
            item_id = str(item.get("id")) if isinstance(item.get("id"), str) else None
            dedupe_key = item_id or json.dumps(item, sort_keys=True, ensure_ascii=False)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            deduped_items.append(item)

    if not deduped_items:
        trace["status"] = "no_results"
        return None, trace

    chosen, ranking_details = choose_best_google_volume(
        deduped_items,
        desired_title=title,
        desired_subtitle=subtitle,
        desired_authors=authors,
        desired_isbn=isbn,
        desired_publisher=publisher,
        desired_published_date=published_date,
    )
    trace["candidate_count"] = len(deduped_items)
    trace["ranking"] = ranking_details[:5]
    trace["selected"] = ranking_details[0] if ranking_details else None
    trace["status"] = "matched" if chosen is not None else "no_match"
    return select_google_volume(chosen) if chosen else None, trace


def resolve_google_volume(
    *,
    args: argparse.Namespace,
    epub_metadata: EpubMetadata | None,
) -> tuple[Mapping[str, Any] | None, Path | None, str | None, list[str], dict[str, Any]]:
    warnings: list[str] = []
    volume_json_path = Path(args.volume_json).expanduser().resolve() if args.volume_json else None
    if volume_json_path is not None:
        volume_raw = select_google_volume(load_json_file(volume_json_path))
        google_volume_id = str(volume_raw.get("id")) if isinstance(volume_raw.get("id"), str) else None
        trace = {
            "source": "volume_json",
            "status": "matched",
            "selected": summarize_google_volume(volume_raw),
        }
        return volume_raw, volume_json_path, google_volume_id, warnings, trace

    if args.skip_google_books:
        return None, None, None, warnings, {"source": "skipped", "status": "skipped_by_flag"}

    api_key = args.google_books_api_key or os.environ.get("GOOGLE_BOOKS_API_KEY")
    if not api_key:
        warnings.append("No Google Books API key found. Skipping automatic Google Books lookup.")
        return None, None, None, warnings, {"source": "automatic_lookup", "status": "missing_api_key"}

    isbn = args.isbn or extract_epub_isbn(epub_metadata)
    title = args.book_title or (epub_metadata.title if epub_metadata else None)
    subtitle = args.subtitle or (epub_metadata.subtitle if epub_metadata else None)
    authors = list(epub_metadata.authors if epub_metadata else ())
    publisher = args.publisher or (epub_metadata.publisher if epub_metadata else None)
    published_date = args.published_date or (epub_metadata.published_date if epub_metadata else None)
    try:
        volume_raw, trace = fetch_google_books_volume(
            api_key=api_key,
            volume_id=args.google_volume_id,
            query=args.google_query,
            isbn=isbn,
            title=title,
            subtitle=subtitle,
            authors=authors,
            publisher=publisher,
            published_date=published_date,
        )
    except Exception as exc:
        warnings.append(f"Automatic Google Books lookup failed: {exc}")
        return None, None, None, warnings, {"source": "automatic_lookup", "status": "error", "error": str(exc)}

    if volume_raw is None:
        warnings.append("Google Books lookup returned no matching volume.")
        return None, None, None, warnings, trace

    google_volume_id = str(volume_raw.get("id")) if isinstance(volume_raw.get("id"), str) else None
    return volume_raw, None, google_volume_id, warnings, trace


def choose_scalar(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        stripped = str(value).strip()
        if stripped:
            return stripped
    return None


def choose_list(*values: Sequence[str]) -> list[str]:
    for value in values:
        if value:
            return [item for item in value if str(item).strip()]
    return []


def bootstrap_book(args: argparse.Namespace) -> BootstrapResult:
    book_dir = Path(args.book_dir).resolve()
    if not book_dir.exists() or not book_dir.is_dir():
        raise FileNotFoundError(f"Book directory does not exist: {book_dir}")

    env_info = prepare_environment(book_dir, args.env_file)

    chapter_paths = discover_chapter_paths(book_dir, args.chapter_glob)
    if not chapter_paths:
        raise FileNotFoundError(
            f"No chapter markdown files matched --chapter-glob {args.chapter_glob!r} inside {book_dir}"
        )
    chapter_texts = [read_text_flexible(path) for path in chapter_paths]

    epub_path = discover_epub_path(book_dir, args.epub)
    epub_metadata: EpubMetadata | None = None
    epub_toc_entries: list[TocEntry] = []
    epub_toc_payload: list[dict[str, Any]] | None = None
    toc_entries: list[TocEntry] = []
    toc_json_payload: list[dict[str, Any]] | None = None
    warnings: list[str] = []

    if epub_path is not None:
        try:
            epub_metadata, epub_toc_entries, raw_toc = extract_epub_metadata_and_toc(epub_path)
            epub_toc_payload = raw_toc if raw_toc else normalize_toc_json(epub_toc_entries)
        except Exception as exc:
            warnings.append(f"EPUB parsing failed: {exc}")

    toc_json_path = Path(args.toc_json).expanduser().resolve() if args.toc_json else None
    if toc_json_path is not None:
        toc_entries = load_toc_entries(toc_json_path)
        toc_json_payload = normalize_toc_json(toc_entries)
    elif epub_toc_entries:
        toc_entries = epub_toc_entries
        toc_json_payload = epub_toc_payload
    else:
        if epub_path is not None:
            warnings.append("EPUB was found, but no navigable TOC entries could be extracted. Falling back to chapter headings.")
        else:
            warnings.append("No --toc-json file and no EPUB found. TOC generation will fall back to chapter headings.")

    volume_raw, volume_json_path, google_volume_id, google_warnings, google_lookup_trace = resolve_google_volume(args=args, epub_metadata=epub_metadata)
    warnings.extend(google_warnings)
    volume_info = volume_raw.get("volumeInfo") if isinstance(volume_raw, Mapping) else None

    title = infer_book_title(args.book_title, volume_info, epub_metadata, fallback=book_dir.name)
    subtitle = choose_scalar(
        args.subtitle,
        str(volume_info.get("subtitle") or "") if volume_info else None,
        epub_metadata.subtitle if epub_metadata else None,
    )
    authors = choose_list(
        list(volume_info.get("authors") or []) if volume_info else [],
        list(epub_metadata.authors if epub_metadata else ()),
    )
    publisher = choose_scalar(
        args.publisher,
        str(volume_info.get("publisher") or "") if volume_info else None,
        epub_metadata.publisher if epub_metadata else None,
    )
    published_date = choose_scalar(
        args.published_date,
        str(volume_info.get("publishedDate") or "") if volume_info else None,
        epub_metadata.published_date if epub_metadata else None,
    )
    language = choose_scalar(
        args.language,
        str(volume_info.get("language") or "") if volume_info else None,
        epub_metadata.language if epub_metadata else None,
    )
    page_count = volume_info.get("pageCount") if volume_info else None
    categories = list(volume_info.get("categories") or []) if volume_info else []
    description = choose_scalar(
        args.description,
        shorten_description(str(volume_info.get("description") or "")) if volume_info else None,
    )
    isbn_13 = extract_isbn(volume_info or {}, "ISBN_13")
    isbn_10 = extract_isbn(volume_info or {}, "ISBN_10")
    if not isbn_13:
        epub_isbn = extract_epub_isbn(epub_metadata)
        if epub_isbn and len(epub_isbn) == 13:
            isbn_13 = epub_isbn
        elif epub_isbn and len(epub_isbn) == 10:
            isbn_10 = isbn_10 or epub_isbn

    book_id = infer_book_id(args.book_id, title, authors, published_date, book_dir.name)
    chapter_titles, chapter_titles_method, title_warnings = assign_chapter_titles(
        chapter_paths=chapter_paths,
        toc_entries=toc_entries,
        manual_offset=args.toc_offset,
    )
    warnings.extend(title_warnings)

    quantitative = suggest_quantitative_density(chapter_texts, args.quantitative_density)
    length_profile, chapter_word_counts = suggest_chapter_length_profile(chapter_texts, args.chapter_length_profile)

    manifest = build_manifest(
        book_id=book_id,
        book_title=title,
        google_volume_id=google_volume_id,
        isbn_13=isbn_13,
        genre_macro=args.genre_macro,
        genre_micro=args.genre_micro,
        narrative_vs_expository=args.narrative_vs_expository,
        prescriptive_vs_analytical=args.prescriptive_vs_analytical,
        quantitative_density=quantitative.selected,
        chapter_length_profile=length_profile.selected,
        benchmark_pool=args.benchmark_pool,
        toc_path="toc.md",
        metadata_path="metadata.md",
        toc_json_path=None if args.no_write_toc_json else "toc.json",
        chapter_paths=chapter_paths,
        chapter_titles=chapter_titles,
        book_dir=book_dir,
    )

    metadata_md = build_metadata_markdown(
        title=title,
        subtitle=subtitle,
        authors=authors,
        publisher=publisher,
        published_date=published_date,
        language=language,
        page_count=page_count,
        categories=categories,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        google_volume_id=google_volume_id,
        description=description,
    )
    toc_md = format_toc_markdown(toc_entries, chapter_titles)

    manifest_path = book_dir / "book.json"
    metadata_path = book_dir / "metadata.md"
    toc_path = book_dir / "toc.md"
    toc_json_output_path = book_dir / "toc.json"

    summary = {
        "book_dir": str(book_dir),
        "book_id": book_id,
        "book_title": title,
        "epub_path": str(epub_path) if epub_path else None,
        "chapter_count": len(chapter_paths),
        "chapter_title_method": chapter_titles_method,
        "chapter_files": [str(path.relative_to(book_dir)) for path in chapter_paths],
        "chapter_titles": chapter_titles,
        "chapter_word_counts": chapter_word_counts,
        "env_loaded_from": env_info.get("env_loaded_from", []),
        "google_books_lookup": google_lookup_trace,
        "quantitative_density": {
            "selected": quantitative.selected,
            "suggested": quantitative.suggested,
            "details": quantitative.details,
        },
        "chapter_length_profile": {
            "selected": length_profile.selected,
            "suggested": length_profile.suggested,
            "details": length_profile.details,
        },
        "manual_review_fields": {
            "genre_macro": args.genre_macro,
            "genre_micro": args.genre_micro,
            "narrative_vs_expository": args.narrative_vs_expository,
            "prescriptive_vs_analytical": args.prescriptive_vs_analytical,
        },
        "warnings": warnings,
    }

    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return BootstrapResult(
            book_dir=book_dir,
            manifest_path=manifest_path,
            metadata_path=metadata_path,
            toc_path=toc_path,
            toc_json_path=toc_json_output_path,
            chapter_titles_method=chapter_titles_method,
            warnings=tuple(warnings),
        )

    for path in (manifest_path, metadata_path, toc_path):
        ensure_writable(path, overwrite=args.overwrite)
    if not args.no_write_toc_json:
        ensure_writable(toc_json_output_path, overwrite=args.overwrite)

    write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    write_text(metadata_path, metadata_md)
    write_text(toc_path, toc_md)
    if not args.no_write_toc_json:
        write_json(toc_json_output_path, toc_json_payload or normalize_toc_json(toc_entries), overwrite=args.overwrite)

    if args.copy_raw_json:
        copy_raw_json(volume_json_path, book_dir / "raw_google_books_volume.json", overwrite=args.overwrite)
        if volume_raw is not None and volume_json_path is None:
            write_json(book_dir / "raw_google_books_volume.json", volume_raw, overwrite=args.overwrite)
        if toc_json_path is not None and toc_json_path.suffix.lower() == ".json":
            copy_raw_json(toc_json_path, book_dir / "raw_epub_toc.json", overwrite=args.overwrite)
        elif toc_json_payload is not None:
            write_json(book_dir / "raw_epub_toc.json", toc_json_payload, overwrite=args.overwrite)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return BootstrapResult(
        book_dir=book_dir,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        toc_path=toc_path,
        toc_json_path=toc_json_output_path,
        chapter_titles_method=chapter_titles_method,
        warnings=tuple(warnings),
    )


def main() -> int:
    args = parse_args()
    try:
        bootstrap_book(args)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
