"""Parse chapter_notes.jsonl into structured optimization signals.

Each note carries: item_key, candidate_name, tags (dimension labels),
text (freeform), timestamp, sentiment (LLM score or 0), auto_tag_source.

Sentiment comes from the LLM auto-tagger (or keyword fallback). No
client-side keyword scanning here — the server handles classification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ── Dimension registry (same slug names as the tag chips) ────────────────

DIMENSION_SLUGS: Set[str] = {
    "style",
    "detail",
    "qualifier",
    "structure",
    "example",
    "terminology",
    "anti_fluff",
}

# Maps each dimension to the candidate_spec policy table key + valid option IDs
DIMENSION_TO_POLICY_KEY: Dict[str, str] = {
    "style": "system_style",
    "detail": "detail_policy",
    "qualifier": "qualifier_policy",
    "structure": "structure_policy",
    "example": "example_policy",
    "terminology": "terminology_policy",
    "anti_fluff": "anti_fluff_policy",
}

# Valid option IDs per dimension (from candidate_spec.py policy dict keys)
DIMENSION_OPTIONS: Dict[str, List[str]] = {
    "style": ["dense_faithful", "teacherly_precise"],
    "detail": ["balanced_dense", "mechanisms_first", "concepts_first"],
    "qualifier": ["strict", "moderate"],
    "structure": ["heading_aware", "theme_clustered", "bullets_only"],
    "example": ["explanatory_only", "sparse_examples"],
    "terminology": ["keep_source_terms", "gloss_more"],
    "anti_fluff": ["hard", "medium"],
}

VALID_OPTIONS: Dict[str, Set[str]] = {d: set(o) for d, o in DIMENSION_OPTIONS.items()}


# ── Data types ──────────────────────────────────────────────────────────

@dataclass
class NoteSignal:
    """A single note parsed into a structured signal."""
    item_key: str
    book_id: str
    chapter_id: str
    candidate_name: Optional[str]
    dimension: str
    text: str
    timestamp: str
    sentiment: float = 0.0  # LLM-assigned sentiment [-1, 1]
    auto_tag_source: str = ""

    @staticmethod
    def from_note(note: dict) -> List["NoteSignal"]:
        """Explode a note with multiple tags into one signal per tag."""
        signals: List[NoteSignal] = []
        item_key = note.get("item_key", "")
        book_id = note.get("book_id", "")
        chapter_id = note.get("chapter_id", "")
        candidate_name = note.get("candidate_name")
        text = note.get("text", "")
        timestamp = note.get("timestamp", "")
        sentiment = float(note.get("sentiment", 0) or 0)
        auto_tag_source = note.get("auto_tag_source", "")
        for tag in note.get("tags", []):
            tag = tag.strip()
            if tag in DIMENSION_SLUGS:
                signals.append(NoteSignal(
                    item_key=item_key,
                    book_id=book_id,
                    chapter_id=chapter_id,
                    candidate_name=candidate_name,
                    dimension=tag,
                    text=text,
                    timestamp=timestamp,
                    sentiment=sentiment,
                    auto_tag_source=auto_tag_source,
                ))
        return signals


@dataclass
class DimensionFeedback:
    """Aggregated feedback for one dimension across chapters."""
    total_signals: int = 0
    chapter_keys: Set[str] = field(default_factory=set)
    text_samples: List[str] = field(default_factory=list)
    # LLM sentiment aggregation
    sentiment_sum: float = 0.0
    sentiment_count: int = 0


@dataclass
class CandidateSignals:
    """Per-candidate signal bundle the optimizer can read."""
    candidate_name: str
    total_signals: int = 0
    dimensions: Dict[str, DimensionFeedback] = field(default_factory=dict)

    def ensure_dimension(self, dim: str) -> DimensionFeedback:
        if dim not in self.dimensions:
            self.dimensions[dim] = DimensionFeedback()
        return self.dimensions[dim]


@dataclass
class Signals:
    """All parsed signals, indexed by candidate and dimension."""
    candidates: Dict[str, CandidateSignals] = field(default_factory=dict)

    def ensure_candidate(self, name: str) -> CandidateSignals:
        if name not in self.candidates:
            self.candidates[name] = CandidateSignals(candidate_name=name)
        return self.candidates[name]


# ── Main parser ─────────────────────────────────────────────────────────

def parse_notes_file(filepath: str) -> Signals:
    """Read chapter_notes.jsonl and return structured Signals.

    Sentiment comes from the LLM auto-tagger stored in the note's
    ``sentiment`` field (raw keyword scanning removed from this module).
    """
    signals = Signals()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return signals

    if not raw:
        return signals

    all_notes: List[dict] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            all_notes.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    for note in all_notes:
        note_signals = NoteSignal.from_note(note)
        for ns in note_signals:
            if ns.candidate_name is None:
                continue
            cand = signals.ensure_candidate(ns.candidate_name)
            fb = cand.ensure_dimension(ns.dimension)
            fb.total_signals += 1
            cand.total_signals += 1
            fb.chapter_keys.add(ns.item_key)
            fb.text_samples.append(ns.text[:200])
            fb.sentiment_sum += ns.sentiment
            fb.sentiment_count += 1

    return signals


def get_active_dimensions(signals: Signals, candidate_name: str) -> List[str]:
    """Return dimensions with feedback, sorted by signal count desc."""
    cand = signals.candidates.get(candidate_name)
    if cand is None:
        return []
    items = sorted(cand.dimensions.items(), key=lambda x: x[1].total_signals, reverse=True)
    return [dim for dim, _ in items if dim in DIMENSION_SLUGS]


def get_dimension_sentiment(fb: DimensionFeedback) -> float:
    """Return mean LLM sentiment in [-1, 1]. Positive = happy, negative = unhappy."""
    if fb.sentiment_count == 0:
        return 0.0
    return fb.sentiment_sum / fb.sentiment_count


def get_current_option(
    candidate_spec: dict,
    dimension: str,
) -> Optional[str]:
    """Extract the current chapter-stage prompt component value for a dimension."""
    key = DIMENSION_TO_POLICY_KEY.get(dimension)
    if key is None:
        return None
    chapter = candidate_spec.get("chapter_stage", {})
    components = chapter.get("prompt_components", {})
    return components.get(key)
