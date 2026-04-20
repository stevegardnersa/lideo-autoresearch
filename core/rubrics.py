from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Sequence

from scoring import Rubric, extract_markdown_headings, extract_numbers, strip_markdown

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_EMPHASIS_RE = re.compile(r"\*\*([^*\n]{2,120})\*\*|__([^_\n]{2,120})__|`([^`\n]{2,120})`")
_TITLE_CASE_RE = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b")
_PERCENT_OR_NUMBER_SENTENCE_RE = re.compile(r"\d|%")

_CONCEPT_MARKERS = (
    " is ",
    " are ",
    " means ",
    " refers to ",
    " describes ",
    " defined as ",
    " framework",
    " principle",
    " model",
    " concept",
)
_MECHANISM_MARKERS = (
    " because ",
    " therefore ",
    " so that ",
    " leads to ",
    " results in ",
    " works by ",
    " process",
    " step",
    " sequence",
    " mechanism",
    " if ",
    " when ",
    " how ",
)
_QUALIFIER_MARKERS = (
    " however",
    " but ",
    " unless ",
    " except ",
    " only if ",
    " in general",
    " typically",
    " usually",
    " often",
    " may ",
    " might ",
    " can ",
    " under ",
    " depends on ",
)
_EXAMPLE_MARKERS = (
    "for example",
    "for instance",
    "e.g.",
    "example",
    "case study",
    "consider ",
)
_STOP_TERMS = {
    "Introduction",
    "Conclusion",
    "Summary",
    "Chapter",
    "Part",
    "Figure",
    "Table",
    "Notes",
}


def _unique_preserve(items: Iterable[str], *, limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        cleaned = _clean_item(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _clean_item(text: str) -> str:
    text = strip_markdown(text or "")
    text = re.sub(r"\s+", " ", text).strip(" -:;,.\n\t")
    if len(text) < 3:
        return ""
    if len(text) > 220:
        text = text[:217].rstrip() + "..."
    return text


def _sentences_from_source(source_md: str) -> List[str]:
    text = strip_markdown(source_md)
    raw_sentences = _SENTENCE_SPLIT_RE.split(text)
    sentences = []
    for sentence in raw_sentences:
        cleaned = _clean_item(sentence)
        if not cleaned:
            continue
        word_count = len(cleaned.split())
        if 4 <= word_count <= 45:
            sentences.append(cleaned)
    return sentences


def _first_sentences_by_section(source_md: str) -> List[str]:
    blocks = re.split(r"\n\s*\n", source_md)
    out: List[str] = []
    for block in blocks:
        cleaned = strip_markdown(block)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        sentence = _SENTENCE_SPLIT_RE.split(cleaned)[0].strip()
        sentence = _clean_item(sentence)
        if sentence:
            out.append(sentence)
    return out


def _extract_emphasized_terms(source_md: str) -> List[str]:
    terms: List[str] = []
    for match in _EMPHASIS_RE.finditer(source_md):
        term = next((group for group in match.groups() if group), "")
        term = _clean_item(term)
        if term and term not in _STOP_TERMS:
            terms.append(term)
    return terms


def _extract_title_case_terms(source_md: str) -> List[str]:
    clean = strip_markdown(source_md)
    terms = []
    for match in _TITLE_CASE_RE.finditer(clean):
        term = _clean_item(match.group(0))
        if not term or term in _STOP_TERMS:
            continue
        if len(term.split()) > 5:
            continue
        terms.append(term)
    return terms


def _extract_heading_terms(headings: Sequence[str]) -> List[str]:
    terms: List[str] = []
    for heading in headings:
        heading = _clean_item(heading)
        if not heading:
            continue
        terms.append(heading)
        for piece in re.split(r"[:\-–—]", heading):
            piece = _clean_item(piece)
            if piece and piece not in _STOP_TERMS:
                terms.append(piece)
    return terms


def _filter_sentences(sentences: Sequence[str], markers: Sequence[str], *, limit: int) -> List[str]:
    selected = []
    for sentence in sentences:
        lower = f" {sentence.lower()} "
        if any(marker in lower for marker in markers):
            selected.append(sentence)
    return _unique_preserve(selected, limit=limit)


def heuristic_rubric_from_source(source_md: str) -> Rubric:
    headings = _unique_preserve(extract_markdown_headings(source_md), limit=12)
    sentences = _sentences_from_source(source_md)
    section_openers = _first_sentences_by_section(source_md)

    concept_candidates = list(section_openers)
    concept_candidates.extend(_filter_sentences(sentences, _CONCEPT_MARKERS, limit=12))
    concept_candidates.extend(sentences[:6])

    mechanism_candidates = _filter_sentences(sentences, _MECHANISM_MARKERS, limit=12)
    qualifier_candidates = _filter_sentences(sentences, _QUALIFIER_MARKERS, limit=10)
    example_candidates = _filter_sentences(sentences, _EXAMPLE_MARKERS, limit=8)

    number_candidates = [sentence for sentence in sentences if _PERCENT_OR_NUMBER_SENTENCE_RE.search(sentence)]
    number_candidates.extend(extract_numbers(source_md))

    key_terms = []
    key_terms.extend(_extract_heading_terms(headings))
    key_terms.extend(_extract_emphasized_terms(source_md))
    key_terms.extend(_extract_title_case_terms(source_md))

    return Rubric(
        headings=tuple(_unique_preserve(headings, limit=12)),
        core_concepts=tuple(_unique_preserve(concept_candidates, limit=12)),
        mechanisms_or_explanations=tuple(_unique_preserve(mechanism_candidates, limit=12)),
        critical_qualifiers=tuple(_unique_preserve(qualifier_candidates, limit=10)),
        important_examples=tuple(_unique_preserve(example_candidates, limit=8)),
        key_entities_or_numbers=tuple(_unique_preserve(number_candidates, limit=12)),
        key_terms=tuple(_unique_preserve(key_terms, limit=20)),
    )


def aggregate_book_rubric(chapter_rubrics: Sequence[Rubric], *, toc_md: str = "") -> Rubric:
    toc_headings = extract_markdown_headings(toc_md)
    headings = list(toc_headings)
    core_concepts: List[str] = []
    mechanisms: List[str] = []
    qualifiers: List[str] = []
    examples: List[str] = []
    numbers: List[str] = []
    key_terms: List[str] = []
    for rubric in chapter_rubrics:
        headings.extend(rubric.headings)
        core_concepts.extend(rubric.core_concepts)
        mechanisms.extend(rubric.mechanisms_or_explanations)
        qualifiers.extend(rubric.critical_qualifiers)
        examples.extend(rubric.important_examples)
        numbers.extend(rubric.key_entities_or_numbers)
        key_terms.extend(rubric.key_terms)

    return Rubric(
        headings=tuple(_unique_preserve(headings, limit=24)),
        core_concepts=tuple(_unique_preserve(core_concepts, limit=24)),
        mechanisms_or_explanations=tuple(_unique_preserve(mechanisms, limit=24)),
        critical_qualifiers=tuple(_unique_preserve(qualifiers, limit=20)),
        important_examples=tuple(_unique_preserve(examples, limit=16)),
        key_entities_or_numbers=tuple(_unique_preserve(numbers, limit=24)),
        key_terms=tuple(_unique_preserve(key_terms, limit=40)),
    )


def rubric_to_dict(rubric: Rubric) -> dict:
    return {
        "headings": list(rubric.headings),
        "core_concepts": list(rubric.core_concepts),
        "mechanisms_or_explanations": list(rubric.mechanisms_or_explanations),
        "critical_qualifiers": list(rubric.critical_qualifiers),
        "important_examples": list(rubric.important_examples),
        "key_entities_or_numbers": list(rubric.key_entities_or_numbers),
        "key_terms": list(rubric.key_terms),
    }


def write_rubric(path: Path, rubric: Rubric) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rubric_to_dict(rubric), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
