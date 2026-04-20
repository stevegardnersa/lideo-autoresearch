"""Frozen scoring rules for the nonfiction book-summary benchmark.

This module is designed to stay fixed while autoresearch edits only
``candidate_spec.py``. It implements:
- deterministic length-control metrics
- deterministic readability metrics
- deterministic source-coverage proxies from frozen rubrics
- hard gates
- a quality score
- a utility score that penalizes cost and extra repair passes
- helper structures for an order-swapped pairwise LLM judge

The evaluator should provide source-derived rubrics and judge outputs.
When judge outputs are missing, the scoring functions fall back to deterministic
proxies so the code remains usable for smoke tests.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import mean
import math
import re
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Tuple

SampleLevel = Literal["chapter", "book"]
PairwiseWinner = Literal["A", "B", "tie"]

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_NUMBER_RE = re.compile(r"(?<!\w)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?!\w)")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^\)]+\)")
_HTML_RE = re.compile(r"<[^>]+>")
_LIST_MARKER_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_ORDERED_LIST_RE = re.compile(r"^\s*\d+[\.)]\s+", re.MULTILINE)
_TABLE_PIPE_RE = re.compile(r"\|")
_MULTISPACE_RE = re.compile(r"\s+")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


@dataclass(frozen=True)
class Rubric:
    headings: Tuple[str, ...] = ()
    core_concepts: Tuple[str, ...] = ()
    mechanisms_or_explanations: Tuple[str, ...] = ()
    critical_qualifiers: Tuple[str, ...] = ()
    important_examples: Tuple[str, ...] = ()
    key_entities_or_numbers: Tuple[str, ...] = ()
    key_terms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeScores:
    faithfulness: float
    concept_coverage: float
    qualifier_preservation: float
    no_fluff: float
    structure_quality: float

    def clamped(self) -> "JudgeScores":
        return JudgeScores(
            faithfulness=_clamp01(self.faithfulness),
            concept_coverage=_clamp01(self.concept_coverage),
            qualifier_preservation=_clamp01(self.qualifier_preservation),
            no_fluff=_clamp01(self.no_fluff),
            structure_quality=_clamp01(self.structure_quality),
        )


@dataclass(frozen=True)
class PairwiseJudgeResult:
    winner: PairwiseWinner
    a_scores: JudgeScores
    b_scores: JudgeScores


@dataclass(frozen=True)
class SummarySample:
    sample_id: str
    level: SampleLevel
    target_words: int
    summary_md: str
    source_md: str
    group_id: str = ""
    first_pass_summary_md: str = ""
    passes_used: int = 1
    generation_cost: float = 0.0
    uncached_generation_cost: float = 0.0
    malformed: bool = False
    rubric: Rubric = field(default_factory=Rubric)
    judge_scores: Optional[JudgeScores] = None


@dataclass(frozen=True)
class ReadabilityMetrics:
    visible_words: int
    sentence_count: int
    syllable_count: int
    average_sentence_length: float
    flesch_reading_ease: float
    flesch_kincaid_grade: float


@dataclass(frozen=True)
class DeterministicMetrics:
    visible_words: int
    first_pass_visible_words: int
    final_length_error_pct: float
    first_pass_length_error_pct: float
    final_length_accuracy: float
    first_pass_length_accuracy: float
    readability_band: float
    heading_coverage: float
    concept_phrase_coverage: float
    mechanism_phrase_coverage: float
    qualifier_phrase_coverage: float
    key_term_coverage: float
    number_coverage: float
    redundancy_score: float
    structure_proxy: float
    faithfulness_proxy: float
    concept_proxy: float


@dataclass(frozen=True)
class ScoringWeights:
    faithfulness: float = 0.33
    concept_coverage: float = 0.20
    qualifier_preservation: float = 0.10
    no_fluff: float = 0.10
    readability_band: float = 0.07
    structure_quality: float = 0.05
    final_length_accuracy: float = 0.10
    first_pass_accuracy: float = 0.05


@dataclass(frozen=True)
class GateConfig:
    max_final_length_error_pct: float = 0.10
    max_passes: int = 5
    min_faithfulness: float = 0.70
    min_concept_coverage: float = 0.60


@dataclass(frozen=True)
class PenaltyConfig:
    cost_penalty_per_cost_unit: float = 0.02
    extra_pass_penalty: float = 0.01


@dataclass(frozen=True)
class ScoringConfig:
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    gates: GateConfig = field(default_factory=GateConfig)
    penalties: PenaltyConfig = field(default_factory=PenaltyConfig)
    target_tolerance_pct: float = 0.05
    zero_accuracy_at_error_pct: float = 0.25


DEFAULT_SCORING_CONFIG = ScoringConfig()


PAIRWISE_JUDGE_JSON_SCHEMA: Dict[str, object] = {
    "name": "pairwise_summary_judge",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "a_scores": {
                "type": "object",
                "properties": {
                    "faithfulness": {"type": "number", "minimum": 0, "maximum": 1},
                    "concept_coverage": {"type": "number", "minimum": 0, "maximum": 1},
                    "qualifier_preservation": {"type": "number", "minimum": 0, "maximum": 1},
                    "no_fluff": {"type": "number", "minimum": 0, "maximum": 1},
                    "structure_quality": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "faithfulness",
                    "concept_coverage",
                    "qualifier_preservation",
                    "no_fluff",
                    "structure_quality",
                ],
                "additionalProperties": False,
            },
            "b_scores": {
                "type": "object",
                "properties": {
                    "faithfulness": {"type": "number", "minimum": 0, "maximum": 1},
                    "concept_coverage": {"type": "number", "minimum": 0, "maximum": 1},
                    "qualifier_preservation": {"type": "number", "minimum": 0, "maximum": 1},
                    "no_fluff": {"type": "number", "minimum": 0, "maximum": 1},
                    "structure_quality": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "faithfulness",
                    "concept_coverage",
                    "qualifier_preservation",
                    "no_fluff",
                    "structure_quality",
                ],
                "additionalProperties": False,
            },
            "rationale": {
                "type": "string",
                "description": "A brief rationale. Keep short to reduce evaluation cost.",
            },
        },
        "required": ["winner", "a_scores", "b_scores", "rationale"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class SampleScore:
    sample_id: str
    group_id: str
    level: SampleLevel
    hard_fail: bool
    hard_fail_reasons: Tuple[str, ...]
    deterministic: DeterministicMetrics
    resolved_faithfulness: float
    resolved_concept_coverage: float
    resolved_qualifier_preservation: float
    resolved_no_fluff: float
    resolved_structure_quality: float
    quality: float
    utility: float


@dataclass(frozen=True)
class DatasetScore:
    n_samples: int
    hard_fail_rate: float
    mean_quality: float
    mean_utility: float
    mean_faithfulness: float
    mean_concept_coverage: float
    mean_final_length_error_pct: float
    mean_first_pass_length_error_pct: float
    mean_passes_used: float
    mean_uncached_cost: float
    by_group_quality: Dict[str, float]
    by_group_utility: Dict[str, float]
    sample_scores: Tuple[SampleScore, ...]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def strip_markdown(markdown_text: str) -> str:
    text = markdown_text or ""
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(lambda m: f" {m.group(1)} ", text)
    text = _LINK_RE.sub(lambda m: f" {m.group(1)} ", text)
    text = _INLINE_CODE_RE.sub(lambda m: f" {m.group(1)} ", text)
    text = _HTML_RE.sub(" ", text)
    text = _HEADING_RE.sub(lambda m: f"\n{m.group(1)}\n", text)
    text = _LIST_MARKER_RE.sub("", text)
    text = _ORDERED_LIST_RE.sub("", text)
    text = _TABLE_PIPE_RE.sub(" ", text)
    text = text.replace("*", " ").replace("_", " ").replace("~", " ")
    text = text.replace(">", " ")
    text = _MULTISPACE_RE.sub(" ", text)
    return text.strip()


def visible_words(text: str) -> List[str]:
    return _WORD_RE.findall(strip_markdown(text))


def visible_word_count(text: str) -> int:
    return len(visible_words(text))


def extract_markdown_headings(markdown_text: str) -> List[str]:
    return [match.group(1).strip() for match in _HEADING_RE.finditer(markdown_text or "")]


def normalize_phrase(text: str) -> str:
    text = strip_markdown(text).lower()
    text = re.sub(r"[^a-z0-9% ]+", " ", text)
    text = _MULTISPACE_RE.sub(" ", text)
    return text.strip()


def extract_numbers(text: str) -> List[str]:
    numbers = []
    for token in _NUMBER_RE.findall(strip_markdown(text)):
        normalized = token.lower().replace(",", "")
        numbers.append(normalized)
    return numbers


def phrase_recall(summary_text: str, phrases: Sequence[str]) -> float:
    normalized_summary = f" {normalize_phrase(summary_text)} "
    cleaned = [normalize_phrase(phrase) for phrase in phrases]
    cleaned = [phrase for phrase in cleaned if phrase]
    if not cleaned:
        return 1.0
    hits = 0
    for phrase in cleaned:
        if f" {phrase} " in normalized_summary:
            hits += 1
    return hits / len(cleaned)


def number_recall(summary_text: str, reference_strings: Sequence[str]) -> float:
    reference_numbers: List[str] = []
    for item in reference_strings:
        reference_numbers.extend(extract_numbers(item))
    unique_reference_numbers = sorted(set(reference_numbers))
    if not unique_reference_numbers:
        return 1.0
    summary_numbers = set(extract_numbers(summary_text))
    hits = sum(1 for token in unique_reference_numbers if token in summary_numbers)
    return hits / len(unique_reference_numbers)


def repeated_ngram_ratio(text: str, n: int = 6) -> float:
    words = [word.lower() for word in visible_words(text)]
    if len(words) < n:
        return 0.0
    grams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    counts = Counter(grams)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / max(1, len(grams))


def _count_syllables(word: str) -> int:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    matches = _VOWEL_GROUP_RE.findall(word)
    count = len(matches)
    if word.endswith("e") and not word.endswith(("le", "ye")) and count > 1:
        count -= 1
    if word.endswith("ed") and count > 1 and not word.endswith(("ted", "ded")):
        count -= 1
    return max(1, count)


def readability_metrics(text: str) -> ReadabilityMetrics:
    clean = strip_markdown(text)
    words = _WORD_RE.findall(clean)
    sentences = [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(clean) if segment.strip()]
    word_count = max(1, len(words))
    sentence_count = max(1, len(sentences))
    syllable_count = sum(_count_syllables(word) for word in words)
    average_sentence_length = word_count / sentence_count
    flesch_reading_ease = 206.835 - 1.015 * average_sentence_length - 84.6 * (syllable_count / word_count)
    flesch_kincaid_grade = 0.39 * average_sentence_length + 11.8 * (syllable_count / word_count) - 15.59
    return ReadabilityMetrics(
        visible_words=word_count,
        sentence_count=sentence_count,
        syllable_count=syllable_count,
        average_sentence_length=average_sentence_length,
        flesch_reading_ease=flesch_reading_ease,
        flesch_kincaid_grade=flesch_kincaid_grade,
    )


def band_score(value: float, ideal_low: float, ideal_high: float, soft_low: float, soft_high: float) -> float:
    if value < soft_low or value > soft_high:
        return 0.0
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        return (value - soft_low) / (ideal_low - soft_low)
    return (soft_high - value) / (soft_high - ideal_high)


def readability_band(text: str) -> float:
    metrics = readability_metrics(text)
    ease_score = band_score(
        metrics.flesch_reading_ease,
        ideal_low=30.0,
        ideal_high=60.0,
        soft_low=15.0,
        soft_high=75.0,
    )
    grade_score = band_score(
        metrics.flesch_kincaid_grade,
        ideal_low=9.0,
        ideal_high=14.0,
        soft_low=7.0,
        soft_high=18.0,
    )
    sentence_score = band_score(
        metrics.average_sentence_length,
        ideal_low=12.0,
        ideal_high=24.0,
        soft_low=8.0,
        soft_high=32.0,
    )
    return _clamp01((ease_score + grade_score + sentence_score) / 3.0)


def length_error_pct(actual_words: int, target_words: int) -> float:
    if target_words <= 0:
        raise ValueError("target_words must be positive")
    return abs(actual_words - target_words) / target_words


def length_accuracy(error_pct: float, tolerance_pct: float, zero_at_error_pct: float) -> float:
    if error_pct <= tolerance_pct:
        return 1.0
    if error_pct >= zero_at_error_pct:
        return 0.0
    span = zero_at_error_pct - tolerance_pct
    if span <= 0:
        return 0.0
    return 1.0 - ((error_pct - tolerance_pct) / span)


def structure_proxy(summary_md: str, rubric: Rubric) -> float:
    headings = extract_markdown_headings(summary_md)
    has_structure = 1.0 if headings else 0.5
    heading_recall = phrase_recall(summary_md, rubric.headings) if rubric.headings else 1.0
    return _clamp01((has_structure + heading_recall) / 2.0)


def deterministic_metrics(sample: SummarySample, config: ScoringConfig = DEFAULT_SCORING_CONFIG) -> DeterministicMetrics:
    final_visible_words = visible_word_count(sample.summary_md)
    first_pass_text = sample.first_pass_summary_md or sample.summary_md
    first_pass_visible_words = visible_word_count(first_pass_text)

    final_error = length_error_pct(final_visible_words, sample.target_words)
    first_pass_error = length_error_pct(first_pass_visible_words, sample.target_words)

    headings = sample.rubric.headings or tuple(extract_markdown_headings(sample.source_md))
    heading_cov = phrase_recall(sample.summary_md, headings)
    concept_cov = phrase_recall(sample.summary_md, sample.rubric.core_concepts)
    mechanism_cov = phrase_recall(sample.summary_md, sample.rubric.mechanisms_or_explanations)
    qualifier_cov = phrase_recall(sample.summary_md, sample.rubric.critical_qualifiers)
    key_term_cov = phrase_recall(sample.summary_md, sample.rubric.key_terms)
    number_cov = number_recall(sample.summary_md, sample.rubric.key_entities_or_numbers)

    redundancy = 1.0 - repeated_ngram_ratio(sample.summary_md, n=6)
    readability = readability_band(sample.summary_md)
    structure = structure_proxy(sample.summary_md, sample.rubric)

    faithfulness_proxy = _clamp01(
        (0.30 * heading_cov)
        + (0.20 * key_term_cov)
        + (0.20 * qualifier_cov)
        + (0.15 * mechanism_cov)
        + (0.15 * number_cov)
    )
    concept_proxy = _clamp01((0.50 * concept_cov) + (0.35 * mechanism_cov) + (0.15 * heading_cov))

    return DeterministicMetrics(
        visible_words=final_visible_words,
        first_pass_visible_words=first_pass_visible_words,
        final_length_error_pct=final_error,
        first_pass_length_error_pct=first_pass_error,
        final_length_accuracy=length_accuracy(
            final_error,
            tolerance_pct=config.target_tolerance_pct,
            zero_at_error_pct=config.zero_accuracy_at_error_pct,
        ),
        first_pass_length_accuracy=length_accuracy(
            first_pass_error,
            tolerance_pct=config.target_tolerance_pct,
            zero_at_error_pct=config.zero_accuracy_at_error_pct,
        ),
        readability_band=readability,
        heading_coverage=heading_cov,
        concept_phrase_coverage=concept_cov,
        mechanism_phrase_coverage=mechanism_cov,
        qualifier_phrase_coverage=qualifier_cov,
        key_term_coverage=key_term_cov,
        number_coverage=number_cov,
        redundancy_score=redundancy,
        structure_proxy=structure,
        faithfulness_proxy=faithfulness_proxy,
        concept_proxy=concept_proxy,
    )


def resolve_scores(sample: SummarySample, metrics: DeterministicMetrics) -> JudgeScores:
    if sample.judge_scores is not None:
        return sample.judge_scores.clamped()
    return JudgeScores(
        faithfulness=metrics.faithfulness_proxy,
        concept_coverage=metrics.concept_proxy,
        qualifier_preservation=metrics.qualifier_phrase_coverage,
        no_fluff=metrics.redundancy_score,
        structure_quality=metrics.structure_proxy,
    )


def hard_fail_reasons(
    sample: SummarySample,
    metrics: DeterministicMetrics,
    resolved: JudgeScores,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> Tuple[str, ...]:
    reasons: List[str] = []
    if sample.malformed:
        reasons.append("malformed_output")
    if metrics.final_length_error_pct > config.gates.max_final_length_error_pct:
        reasons.append("length_outside_hard_tolerance")
    if sample.passes_used > config.gates.max_passes:
        reasons.append("too_many_passes")
    if resolved.faithfulness < config.gates.min_faithfulness:
        reasons.append("faithfulness_below_threshold")
    if resolved.concept_coverage < config.gates.min_concept_coverage:
        reasons.append("concept_coverage_below_threshold")
    return tuple(reasons)


def quality_score(
    metrics: DeterministicMetrics,
    resolved: JudgeScores,
    weights: ScoringWeights,
) -> float:
    score = 0.0
    score += weights.faithfulness * resolved.faithfulness
    score += weights.concept_coverage * resolved.concept_coverage
    score += weights.qualifier_preservation * resolved.qualifier_preservation
    score += weights.no_fluff * resolved.no_fluff
    score += weights.readability_band * metrics.readability_band
    score += weights.structure_quality * resolved.structure_quality
    score += weights.final_length_accuracy * metrics.final_length_accuracy
    score += weights.first_pass_accuracy * metrics.first_pass_length_accuracy
    return _clamp01(score)


def utility_score(
    quality: float,
    sample: SummarySample,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> float:
    uncached_cost = sample.uncached_generation_cost or sample.generation_cost
    extra_passes = max(0, sample.passes_used - 1)
    utility = quality
    utility -= config.penalties.cost_penalty_per_cost_unit * uncached_cost
    utility -= config.penalties.extra_pass_penalty * extra_passes
    return utility


def score_sample(sample: SummarySample, config: ScoringConfig = DEFAULT_SCORING_CONFIG) -> SampleScore:
    metrics = deterministic_metrics(sample, config=config)
    resolved = resolve_scores(sample, metrics)
    reasons = hard_fail_reasons(sample, metrics, resolved, config=config)
    hard_fail = bool(reasons)
    quality = quality_score(metrics, resolved, config.weights)
    utility = utility_score(quality, sample, config=config)
    return SampleScore(
        sample_id=sample.sample_id,
        group_id=sample.group_id,
        level=sample.level,
        hard_fail=hard_fail,
        hard_fail_reasons=reasons,
        deterministic=metrics,
        resolved_faithfulness=resolved.faithfulness,
        resolved_concept_coverage=resolved.concept_coverage,
        resolved_qualifier_preservation=resolved.qualifier_preservation,
        resolved_no_fluff=resolved.no_fluff,
        resolved_structure_quality=resolved.structure_quality,
        quality=quality,
        utility=utility,
    )


def _macro_means(sample_scores: Sequence[SampleScore], metric_name: str) -> Tuple[float, Dict[str, float]]:
    if not sample_scores:
        return 0.0, {}
    group_to_values: Dict[str, List[float]] = defaultdict(list)
    for score in sample_scores:
        group_key = score.group_id or score.sample_id
        group_to_values[group_key].append(float(getattr(score, metric_name)))
    by_group = {group: mean(values) for group, values in group_to_values.items()}
    return mean(by_group.values()), by_group


def score_dataset(
    samples: Sequence[SummarySample],
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> DatasetScore:
    sample_scores = tuple(score_sample(sample, config=config) for sample in samples)
    if not sample_scores:
        return DatasetScore(
            n_samples=0,
            hard_fail_rate=0.0,
            mean_quality=0.0,
            mean_utility=0.0,
            mean_faithfulness=0.0,
            mean_concept_coverage=0.0,
            mean_final_length_error_pct=0.0,
            mean_first_pass_length_error_pct=0.0,
            mean_passes_used=0.0,
            mean_uncached_cost=0.0,
            by_group_quality={},
            by_group_utility={},
            sample_scores=(),
        )

    mean_quality, by_group_quality = _macro_means(sample_scores, "quality")
    mean_utility, by_group_utility = _macro_means(sample_scores, "utility")

    hard_fail_rate = mean(1.0 if score.hard_fail else 0.0 for score in sample_scores)
    mean_faithfulness = mean(score.resolved_faithfulness for score in sample_scores)
    mean_concept_coverage = mean(score.resolved_concept_coverage for score in sample_scores)
    mean_final_length_error_pct = mean(score.deterministic.final_length_error_pct for score in sample_scores)
    mean_first_pass_length_error_pct = mean(
        score.deterministic.first_pass_length_error_pct for score in sample_scores
    )
    mean_passes_used = mean(sample.passes_used for sample in samples)
    mean_uncached_cost = mean(sample.uncached_generation_cost or sample.generation_cost for sample in samples)

    return DatasetScore(
        n_samples=len(sample_scores),
        hard_fail_rate=hard_fail_rate,
        mean_quality=mean_quality,
        mean_utility=mean_utility,
        mean_faithfulness=mean_faithfulness,
        mean_concept_coverage=mean_concept_coverage,
        mean_final_length_error_pct=mean_final_length_error_pct,
        mean_first_pass_length_error_pct=mean_first_pass_length_error_pct,
        mean_passes_used=mean_passes_used,
        mean_uncached_cost=mean_uncached_cost,
        by_group_quality=by_group_quality,
        by_group_utility=by_group_utility,
        sample_scores=sample_scores,
    )


def build_pairwise_judge_payload(
    *,
    source_md: str,
    rubric: Rubric,
    target_words: int,
    summary_a_md: str,
    summary_b_md: str,
) -> Dict[str, object]:
    """Build a judge request payload for an order-swapped pairwise comparison.

    Recommended usage:
    1. call once with candidate as A and incumbent as B
    2. call again with the order swapped
    3. average the focal candidate's scores across the two calls
    """
    rubric_block = "\n".join(
        [
            "Frozen rubric:",
            f"- core concepts: {list(rubric.core_concepts)}",
            f"- mechanisms or explanations: {list(rubric.mechanisms_or_explanations)}",
            f"- critical qualifiers: {list(rubric.critical_qualifiers)}",
            f"- important examples: {list(rubric.important_examples)}",
            f"- headings: {list(rubric.headings)}",
            f"- key terms: {list(rubric.key_terms)}",
            f"- key entities or numbers: {list(rubric.key_entities_or_numbers)}",
        ]
    )
    system_prompt = (
        "You are grading two nonfiction summaries against the same source chapter or source book. "
        "Evaluate only faithfulness to the source, concept coverage, preservation of qualifiers, "
        "absence of fluff, and structural clarity. Do not reward style over fidelity. "
        "Be strict about unsupported claims and missing caveats."
    )
    user_prompt = "\n\n".join(
        [
            f"Target words: {target_words}",
            rubric_block,
            "Source markdown:",
            source_md.strip(),
            "Summary A:",
            summary_a_md.strip(),
            "Summary B:",
            summary_b_md.strip(),
            (
                "Return JSON with a winner and per-summary scores from 0 to 1 for: "
                "faithfulness, concept_coverage, qualifier_preservation, no_fluff, structure_quality."
            ),
        ]
    )
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": PAIRWISE_JUDGE_JSON_SCHEMA,
        },
    }


def order_swapped_focal_scores(
    ab: PairwiseJudgeResult,
    ba: PairwiseJudgeResult,
) -> JudgeScores:
    """Return the focal candidate's order-averaged scores.

    Convention:
    - ``ab`` is the call where the focal candidate was placed in slot A.
    - ``ba`` is the call where the focal candidate was placed in slot B.
    """
    ab_a = ab.a_scores.clamped()
    ba_b = ba.b_scores.clamped()
    return JudgeScores(
        faithfulness=(ab_a.faithfulness + ba_b.faithfulness) / 2.0,
        concept_coverage=(ab_a.concept_coverage + ba_b.concept_coverage) / 2.0,
        qualifier_preservation=(ab_a.qualifier_preservation + ba_b.qualifier_preservation) / 2.0,
        no_fluff=(ab_a.no_fluff + ba_b.no_fluff) / 2.0,
        structure_quality=(ab_a.structure_quality + ba_b.structure_quality) / 2.0,
    )


def order_swapped_win_rate(ab: PairwiseJudgeResult, ba: PairwiseJudgeResult) -> float:
    """Return the focal candidate's win rate across order-swapped comparisons.

    Convention is the same as ``order_swapped_focal_scores``.
    """
    wins = 0.0
    for winner, focal_label in ((ab.winner, "A"), (ba.winner, "B")):
        if winner == "tie":
            wins += 0.5
        elif winner == focal_label:
            wins += 1.0
    return wins / 2.0
