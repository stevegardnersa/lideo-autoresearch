"""Editable candidate specification for the nonfiction book-summary benchmark.

Design intent
-------------
This file is the only file that the autoresearch agent should edit.
The frozen harness imports the profile-specific candidate via ``get_candidate`` and
uses the render helpers below to build requests.

The benchmark has two separate products:
- 30-minute whole-book summary
- 60-minute whole-book summary

Each profile uses a two-stage pipeline:
1. chapter summarization
2. whole-book composition from chapter summaries

The evaluator owns scoring, judging, and benchmark splits.
This file owns the production system: model choice, prompt components, repair
policy, and chapter budget allocation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Literal, Optional, Sequence, Tuple

Profile = Literal[
    "30m_deepseek-v4-flash_notthinking", "30m_deepseek-v4-flash_thinking", "30m_deepseek-v4-pro_notthinking",
    "30m_deepseek-v4-pro_thinking", "30m_gemini-3-flash-preview_notthinking", "30m_gemini-3-flash-preview_thinking",
    "30m_gemini-3.1-flash-lite-preview_notthinking", "30m_gemini-3.1-flash-lite-preview_thinking",
    "30m_gemini-3.1-pro-preview_notthinking", "30m_gemini-3.1-pro-preview_thinking", "30m_gemini-3.5-flash_notthinking",
    "30m_gemini-3.5-flash_thinking", "30m_glm-5.3-flash_effort-minimal", "30m_glm-5.3-flash_notthinking",
    "30m_gpt-5.4-mini_notthinking", "30m_gpt-5.4-mini_thinking", "30m_gpt-5.4-nano_notthinking", "30m_gpt-5.4-nano_thinking",
    "30m_gpt-5.6-luna_effort-medium", "30m_gpt-5.6-luna_effort-minimal", "30m_gpt-5.6-luna_notthinking",
    "30m_gpt-oss-120b_notthinking", "30m_gpt-oss-120b_thinking", "30m_grok-4.3_notthinking", "30m_grok-4.3_thinking",
    "30m_mimo-v2-flash_notthinking", "30m_mimo-v2-flash_thinking", "30m_minimax-m2.5_notthinking",
    "30m_minimax-m2.5_thinking", "30m_minimax-m2.7_notthinking", "30m_minimax-m2.7_thinking",
    "30m_muse-glimmer-30b_effort-medium", "30m_muse-glimmer-30b_notthinking", "30m_nemotron-3-super-120b-a12b_notthinking",
    "30m_nemotron-3-super-120b-a12b_thinking", "30m_nemotron-3.5-lightning_effort-medium",
    "30m_nemotron-3.5-lightning_effort-minimal", "30m_nemotron-3.5-lightning_notthinking", "30m_qwen3.6-35b-a3b_notthinking",
    "30m_qwen3.6-35b-a3b_thinking", "30m_qwen3.6-plus_notthinking", "30m_qwen3.6-plus_thinking",
    "30m_qwen3.7-max_notthinking", "30m_qwen3.7-max_thinking", "30m_step-3.5-flash_notthinking",
    "30m_step-3.5-flash_thinking", "30m_step-3.7-flash_effort-medium", "60m_deepseek-v4-flash_notthinking",
    "60m_deepseek-v4-flash_thinking", "60m_deepseek-v4-pro_notthinking", "60m_deepseek-v4-pro_thinking",
    "60m_gemini-3-flash-preview_notthinking", "60m_gemini-3-flash-preview_thinking",
    "60m_gemini-3.1-flash-lite-preview_notthinking", "60m_gemini-3.1-flash-lite-preview_thinking",
    "60m_gemini-3.1-pro-preview_notthinking", "60m_gemini-3.1-pro-preview_thinking", "60m_glm-5.3-flash_notthinking",
    "60m_gpt-5.4-mini_notthinking", "60m_gpt-5.4-mini_thinking", "60m_gpt-5.4-nano_notthinking", "60m_gpt-5.4-nano_thinking",
    "60m_gpt-5.6-luna_effort-medium", "60m_gpt-5.6-luna_effort-minimal", "60m_gpt-5.6-luna_notthinking",
    "60m_gpt-oss-120b_notthinking", "60m_gpt-oss-120b_thinking", "60m_grok-4.3_notthinking", "60m_grok-4.3_thinking",
    "60m_mimo-v2-flash_notthinking", "60m_mimo-v2-flash_thinking", "60m_minimax-m2.5_notthinking",
    "60m_minimax-m2.5_thinking", "60m_minimax-m2.7_notthinking", "60m_minimax-m2.7_thinking",
    "60m_muse-glimmer-30b_effort-medium", "60m_muse-glimmer-30b_notthinking", "60m_nemotron-3-super-120b-a12b_notthinking",
    "60m_nemotron-3-super-120b-a12b_thinking", "60m_qwen3.6-35b-a3b_notthinking", "60m_qwen3.6-35b-a3b_thinking",
    "60m_qwen3.6-plus_notthinking", "60m_qwen3.6-plus_thinking", "60m_step-3.5-flash_notthinking",
    "60m_step-3.5-flash_thinking", "60m_step-3.7-flash_effort-medium"
]
FormatMode = Literal["markdown_sections", "markdown_bullets", "prose"]
ContextMode = Literal[
    "chapter_only",
    "chapter_plus_toc",
    "chapter_plus_book_meta",
    "chapter_plus_toc_and_meta",
]
RepairStrategy = Literal["edit_existing", "regenerate_from_source"]
ComposerMode = Literal["summaries_only", "hybrid_retrieve", "source_aware"]

try:
    from core.reasoning import reasoning_request_params, register_mandatory_reasoning_models
except ImportError:  # pragma: no cover - hosts without core on sys.path
    reasoning_request_params = None
    register_mandatory_reasoning_models = None

try:
    from core import prompt_template as _prompt_template
except ImportError:  # pragma: no cover - hosts without core on sys.path
    _prompt_template = None


# Models whose providers reject ``reasoning={"enabled": false}`` and still reason
# on a plain request. A ``_notthinking`` profile is impossible for them; the
# request builder raises when one is attempted. Regenerated from
# data/candidates.json capabilities by tools/gen_profile_literal.py.
MANDATORY_REASONING_MODELS: frozenset = frozenset({"stepfun/step-3.7-flash"})
if register_mandatory_reasoning_models is not None:
    register_mandatory_reasoning_models(MANDATORY_REASONING_MODELS)


@dataclass
class StageConfig:
    model: str
    temperature: float = 0.2
    seed: Optional[int] = 42
    max_tokens: int = 8192
    format_mode: FormatMode = "markdown_sections"
    context_mode: ContextMode = "chapter_plus_toc_and_meta"
    prompt_components: Dict[str, str] = field(default_factory=dict)
    provider: Optional[Dict[str, Any]] = None
    use_json_schema: Optional[bool] = None
    reasoning: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    extra_body: Optional[Dict[str, Any]] = None
    system_template: Optional[str] = None
    user_template: Optional[str] = None
    component_text: Dict[str, str] = field(default_factory=dict)


@dataclass
class LengthControlConfig:
    max_passes: int = 5
    tolerance_pct: float = 0.05
    hard_tolerance_pct: float = 0.10
    repair_strategy: RepairStrategy = "edit_existing"
    repair_more_prompt_id: str = "expand_missing_detail"
    repair_less_prompt_id: str = "shrink_dedup_first"
    max_truncation_retries: int = 1
    # Repair targeting: "target" aims at the target word count, "low" aims at the
    # low edge of the accepted range so model under-editing still lands in range.
    repair_target_mode: str = "target"
    # Delete whole sentences (lowest-value first) instead of only rewording.
    repair_sentence_delete: bool = False
    # Demand a proportional cut so the model does not make a token 1-word edit.
    repair_min_reduction_pct: float = 0.0
    # Stop after this many passes with no improvement >= early_stop_epsilon words.
    early_stop_patience: int = 0
    early_stop_epsilon: int = 0
    # Stop when the repair direction flips (over -> under or vice versa).
    stop_on_direction_flip: bool = False
    # Accept a pass outside the soft range if inside hard_tolerance_pct.
    accept_within_hard_tolerance: bool = False


@dataclass
class BudgetAllocatorConfig:
    words_per_minute: int = 200
    allocation_alpha: float = 0.90
    min_chapter_share: float = 0.03
    max_chapter_share: float = 0.18
    chapter_stage_multiplier_30m: float = 1.20
    chapter_stage_multiplier_60m: float = 1.00
    max_summary_to_source_ratio: float = 0.90


@dataclass
class CandidateSpec:
    name: str
    profile: Profile
    chapter_stage: StageConfig
    composer_stage: StageConfig
    composer_mode: ComposerMode = "summaries_only"
    length_control: LengthControlConfig = field(default_factory=LengthControlConfig)
    budget_allocator: BudgetAllocatorConfig = field(default_factory=BudgetAllocatorConfig)
    use_json_schema: bool = True
    json_schema_name: str = "summary_response"
    notes: str = ""
    disable_composer: bool = False

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


SUMMARY_JSON_SCHEMA: Dict[str, object] = {
    "name": "summary_response",
    "schema": {
        "type": "object",
        "properties": {
            "summary_md": {
                "type": "string",
                "description": "The requested summary in markdown.",
            },
            "estimated_visible_words": {
                "type": "integer",
                "description": "The model's estimate of visible words in summary_md.",
                "minimum": 0,
            },
        },
        "required": ["summary_md", "estimated_visible_words"],
        "additionalProperties": False,
    },
}


CHAPTER_SYSTEM_STYLES: Dict[str, str] = {
    "dense_faithful": (
        "You write dense, source-faithful summaries of nonfiction books. "
        "Your task is compression, not simplification. Preserve concepts, explanations, "
        "terminology, distinctions, and caveats. Never invent claims, examples, or "
        "interpretations not supported by the source text. "
        "CRITICAL: If the source text includes specific examples, case studies, names, "
        "numbers, or quotes, you MUST include them in the summary. Do NOT substitute "
        "your own knowledge or generic versions of those examples. The source's specific "
        "content must remain in the summary. "
        "Strive for brevity within the word budget - avoid wordy or elaborate framing."
    ),
    "teacherly_precise": (
        "You are an expert editor of serious nonfiction. Explain ideas clearly, but do not "
        "flatten nuance. Preserve the author's causal logic, definitions, exceptions, and limits. "
        "CRITICAL: Include the source's specific examples, names, and quotes - do not replace "
        "them with generic alternatives."
    ),
    "plain_readable": (
        "You write faithful nonfiction summaries at an approachable reading level. "
        "Precision must survive, but the prose must be plain. "
        "SENTENCE RULE: A sentence is one idea. Split any sentence with more than one clause "
        "joined by and/but/so/because/while or by commas into separate sentences. "
        "Target an average of 14 to 16 words per sentence. "
        "WORD RULE: Prefer short, common words (use, get, keep, make) over longer formal ones "
        "(utilize, obtain, maintain, facilitate). When a technical term is required, keep the "
        "term but follow it with a short plain gloss. "
        "FIDELITY: Keep the author's concepts, mechanisms, examples, names, numbers, quotes, "
        "and caveats. Compression and simplification of STYLE, not of content. "
        "Never invent facts the source does not state."
    ),
}


DETAIL_POLICIES: Dict[str, str] = {
    "balanced_dense": (
        "Keep as much explanatory detail as the target budget allows. Prioritize definitions, "
        "frameworks, mechanisms, causal relationships, reasoning steps, and important examples."
    ),
    "mechanisms_first": (
        "Prioritize how things work: mechanisms, sequences, cause-and-effect, operational logic, "
        "and why each concept matters. Compress rhetorical framing before explanatory material."
    ),
    "concepts_first": (
        "Prioritize the chapter's major concepts and how they relate. Include supporting "
        "explanations, but keep the conceptual scaffold explicit and easy to scan."
    ),
}


QUALIFIER_POLICIES: Dict[str, str] = {
    "strict": (
        "Preserve scope conditions, caveats, exceptions, uncertainty, trade-offs, and limits. "
        "Do not turn a qualified claim into an absolute one."
    ),
    "moderate": (
        "Preserve important caveats and exceptions, especially when they change the meaning of a claim."
    ),
}


STRUCTURE_POLICIES: Dict[str, str] = {
    "heading_aware": (
        "Use short markdown section headings that broadly follow the chapter's conceptual structure. "
        "You may merge minor headings, but keep the summary easy to scan."
    ),
    "theme_clustered": (
        "Organize the summary into a small number of conceptual sections, even if the source uses "
        "many headings. Preserve the source order unless there is a strong reason not to."
    ),
    "bullets_only": (
        "Present the summary as markdown bullets with nested sub-bullets when needed. Keep each bullet dense."
    ),
}


EXAMPLE_POLICIES: Dict[str, str] = {
    "explanatory_only": (
        "Include examples only when they clarify a concept, mechanism, or distinction. "
        "Drop decorative anecdotes."
    ),
    "sparse_examples": (
        "Include at most a few representative examples. Prefer abstraction over repeated illustration."
    ),
}


TERMINOLOGY_POLICIES: Dict[str, str] = {
    "keep_source_terms": (
        "Preserve the author's technical terms and named concepts when they carry meaning. "
        "If a term may be unfamiliar, gloss it once in plain language rather than replacing it."
    ),
    "gloss_more": (
        "Preserve technical terms, but add brief plain-language glosses when helpful for readability."
    ),
}


ANTI_FLUFF_POLICIES: Dict[str, str] = {
    "hard": (
        "Avoid motivational framing, praise, repetition, scene-setting, and meta commentary. "
        "Every paragraph should add source-grounded information."
    ),
    "medium": (
        "Favor information density. Remove filler and repetition before removing core ideas."
    ),
}


LENGTH_POLICIES: Dict[str, str] = {
    "explicit_budget_first": (
        "Plan the entire summary against the stated word and sentence budgets before writing a "
        "single sentence. The request gives an exact target word count, an acceptable range, and a "
        "sentence budget. Obey the word range. Treat the sentence budget as the structural limit: "
        "never write more sentences than the budget. Before finishing, count your visible words and "
        "sentences and trim or expand to land inside the stated range."
    ),
    "outline_then_fill": (
        "Before writing, size each section so its words sum to the stated budget, then fill them in "
        "order while keeping a running word count."
    ),
    "verbatim_target_echo": (
        "Immediately before writing, restate the numeric target and the remaining word budget, then "
        "write to it."
    ),
    "sentence_budget_strict": (
        "You will be given an exact sentence budget S. Write EXACTLY S sentences total. Do not "
        "exceed this sentence count under any circumstance. Prefer the target word count; the "
        "sentence count is the hard constraint."
    ),
    "sentence_split_hard": (
        "Sentence mechanics are a hard constraint, as strict as the word budget. The request gives "
        "a sentence budget S computed at the target words-per-sentence. Write close to S sentences, "
        "and never average more than the target words per sentence. Split every sentence that joins "
        "two or more independent clauses with and/but/so/because/while or a comma. A sentence is one "
        "idea: subject + verb + one clause. If a sentence exceeds the target length, cut it at the "
        "next clause boundary and start a new sentence. Do the same split when a sentence reaches "
        "three comma-separated parts. The result must read naturally as short, plain, complete "
        "sentences, not truncated fragments."
    ),
}


COMPOSER_SYSTEM_STYLES: Dict[str, str] = {
    "dedupe_synthesizer": (
        "You synthesize chapter summaries into a faithful whole-book summary. Remove cross-chapter "
        "redundancy while preserving the book's main concepts, explanations, and caveats."
    ),
    "architectural_synthesizer": (
        "You compose a faithful, coherent account of a nonfiction book from chapter summaries. "
        "Preserve the book's argument structure and conceptual dependencies, not just isolated facts."
    ),
}


COMPOSER_STRATEGIES: Dict[str, str] = {
    "thesis_then_frameworks": (
        "Start with the book's overall thesis and organizing logic, then cover the main frameworks, "
        "mechanisms, distinctions, and applications. Deduplicate recurring claims across chapters."
    ),
    "progressive_argument": (
        "Reflect how the book's argument develops across chapters. Merge repeated setup material and "
        "keep the exposition cumulative rather than chapter-by-chapter."
    ),
}


REPAIR_MORE_POLICIES: Dict[str, str] = {
    "expand_missing_detail": (
        "The summary is too short. Expand by restoring omitted mechanisms, key definitions, caveats, "
        "and explanatory examples from the source. Do not pad with generic prose."
    ),
    "expand_mechanisms_first": (
        "The summary is too short. Add missing explanatory detail in this order: mechanisms, "
        "conceptual distinctions, caveats, then examples."
    ),
}


REPAIR_LESS_POLICIES: Dict[str, str] = {
    "shrink_dedup_first": (
        "The summary is too long. Shorten it by removing repetition, rhetorical setup, and low-value "
        "examples before cutting core concepts or caveats."
    ),
    "shrink_merge_sections": (
        "The summary is too long. Merge overlapping sections, compress repeated definitions, and keep "
        "only the most explanatory examples."
    ),
    "shrink_sentence_delete": (
        "The summary is too long. DELETE whole sentences, lowest-value first, until it is at or below "
        "the low end of the acceptable range. Work in this order: (1) full sentences that only restate "
        "a point already made, (2) sentences that set up or frame rather than inform, (3) sentences "
        "whose example is decorative, then (4) any remaining sentence you can compress without losing a "
        "distinct concept, mechanism, or caveat. Remove sentence by sentence; do not merely reword. A "
        "revision that is not strictly shorter than the current summary is rejected."
    ),
}


FORMAT_INSTRUCTIONS: Dict[FormatMode, str] = {
    "markdown_sections": (
        "Return markdown only. Use short section headings and dense paragraphs or bullets as needed."
    ),
    "markdown_bullets": (
        "Return markdown only. Use bullets and nested bullets. Keep each bullet information-dense."
    ),
    "prose": "Return markdown only. Use compact prose paragraphs and minimal headings.",
}

CHAPTER_REQUIRED_BEHAVIOR = (
    "Required behavior:\n"
    "- preserve the chapter's core concepts, explanatory logic, and caveats\n"
    "- preserve specific examples, names, numbers, and quotes from the source verbatim\n"
    "- NEVER substitute your own knowledge or generic examples for the source's specific content\n"
    "- if the source discusses a specific case study (e.g., a company, historical event, "
    "research finding), include that specific case study in the summary\n"
    "- include the source's specific data points, statistics, and quantifiable claims\n"
    "- prioritize information over rhetoric\n"
    "- include examples only when they help explain a concept\n"
    "- do not mention the instructions, target length, or what was omitted\n"
    "- do not add information that is not supported by the source"
)

COMPOSER_REQUIRED_BEHAVIOR = (
    "Required behavior:\n"
    "- preserve the book's main thesis, frameworks, mechanisms, and caveats\n"
    "- synthesize repeated ideas across chapters instead of repeating them\n"
    "- keep the final result coherent as a standalone whole-book summary\n"
    "- do not mention the instructions, target length, or chapter-by-chapter omissions"
)


def final_book_target_words(
    total_book_visible_words: int,
    profile: Profile,
    cfg: BudgetAllocatorConfig,
) -> int:
    """Return the target visible word count for the final product.

    The user's original factor-based method reduces to a constant reading-time budget
    when the same words-per-minute rate is used for both the source and the summary.
    Therefore:
    - 30m -> 30 * words_per_minute
    - 60m -> 60 * words_per_minute

    A safeguard caps the summary so it never exceeds a large fraction of the source.
    """
    minutes = 30 if profile == "30m" else 60
    nominal_target = minutes * cfg.words_per_minute
    source_cap = max(1, int(round(total_book_visible_words * cfg.max_summary_to_source_ratio)))
    return min(nominal_target, source_cap)


def chapter_stage_total_target_words(
    total_book_visible_words: int,
    profile: Profile,
    cfg: BudgetAllocatorConfig,
) -> int:
    final_target = final_book_target_words(total_book_visible_words, profile, cfg)
    multiplier = (
        cfg.chapter_stage_multiplier_30m if profile == "30m" else cfg.chapter_stage_multiplier_60m
    )
    return max(1, int(round(final_target * multiplier)))


def _bounded_normalized_shares(
    raw_weights: Sequence[float],
    min_share: float,
    max_share: float,
) -> List[float]:
    """Project raw positive weights onto a simplex with lower and upper bounds.

    This keeps chapter allocation proportional in spirit while preventing giant
    chapters from swallowing the book budget and tiny chapters from being starved.
    """
    if not raw_weights:
        return []
    n_items = len(raw_weights)
    if min_share < 0 or max_share <= 0:
        raise ValueError("Invalid share bounds.")

    # Relax impossible bounds automatically for very short books or smoke tests.
    # Example: with 3 chapters, max_share must be at least 1/3 or the simplex has no solution.
    min_share = min(min_share, 1.0 / n_items)
    max_share = max(max_share, 1.0 / n_items)
    if min_share > max_share:
        min_share = max_share = 1.0 / n_items

    raw = [max(float(weight), 1e-9) for weight in raw_weights]
    shares = [0.0 for _ in raw]
    free = set(range(n_items))
    remaining_total = 1.0

    while free:
        remaining_weight = sum(raw[i] for i in free)
        if remaining_weight <= 0:
            even_share = remaining_total / len(free)
            for i in free:
                shares[i] = even_share
            break

        changed = False
        for i in list(free):
            proposed = remaining_total * raw[i] / remaining_weight
            if proposed < min_share:
                shares[i] = min_share
                remaining_total -= min_share
                free.remove(i)
                changed = True
            elif proposed > max_share:
                shares[i] = max_share
                remaining_total -= max_share
                free.remove(i)
                changed = True

        if not changed:
            remaining_weight = sum(raw[i] for i in free)
            for i in free:
                shares[i] = remaining_total * raw[i] / remaining_weight
            break

    total = sum(shares)
    if total <= 0:
        return [1.0 / n_items for _ in raw]
    return [share / total for share in shares]


def allocate_chapter_targets(
    chapter_visible_words: Sequence[int],
    total_book_visible_words: int,
    profile: Profile,
    cfg: BudgetAllocatorConfig,
) -> List[int]:
    """Allocate chapter-stage targets from the fixed whole-book budget.

    The target total for chapter summaries can be slightly larger than the final
    whole-book target, especially for the 30-minute product, to preserve recall
    before the composer compresses and deduplicates across chapters.
    """
    if not chapter_visible_words:
        return []

    total_stage_target = chapter_stage_total_target_words(total_book_visible_words, profile, cfg)
    raw_weights = [max(words, 1) ** cfg.allocation_alpha for words in chapter_visible_words]
    shares = _bounded_normalized_shares(
        raw_weights,
        min_share=cfg.min_chapter_share,
        max_share=cfg.max_chapter_share,
    )

    float_targets = [total_stage_target * share for share in shares]
    base_targets = [max(1, int(value)) for value in float_targets]
    shortfall = total_stage_target - sum(base_targets)

    if shortfall > 0:
        order = sorted(
            range(len(float_targets)),
            key=lambda idx: float_targets[idx] - base_targets[idx],
            reverse=True,
        )
        for idx in order[:shortfall]:
            base_targets[idx] += 1
    elif shortfall < 0:
        order = sorted(
            range(len(float_targets)),
            key=lambda idx: base_targets[idx] - float_targets[idx],
            reverse=True,
        )
        to_remove = -shortfall
        for idx in order:
            if to_remove <= 0:
                break
            removable = max(0, base_targets[idx] - 1)
            if removable <= 0:
                continue
            step = min(removable, to_remove)
            base_targets[idx] -= step
            to_remove -= step

    return base_targets


def visible_word_range(target_words: int, tolerance_pct: float) -> Tuple[int, int]:
    delta = max(1, int(round(target_words * tolerance_pct)))
    return max(1, target_words - delta), target_words + delta


def _context_block(
    *,
    context_mode: ContextMode,
    book_title: str,
    chapter_title: str,
    toc_md: str,
    book_metadata: str,
) -> str:
    blocks: List[str] = []
    if book_title:
        blocks.append(f"Book title: {book_title}")
    if chapter_title:
        blocks.append(f"Chapter title: {chapter_title}")
    if context_mode in {"chapter_plus_toc", "chapter_plus_toc_and_meta"} and toc_md.strip():
        blocks.append("Table of contents:\n" + toc_md.strip())
    if context_mode in {"chapter_plus_book_meta", "chapter_plus_toc_and_meta"} and book_metadata.strip():
        blocks.append("Book metadata:\n" + book_metadata.strip())
    return "\n\n".join(blocks)


def _component_text(components: Dict[str, str], lookup_tables: Dict[str, Dict[str, str]],
                    component_text: Optional[Dict[str, str]] = None) -> str:
    parts: List[str] = []
    overrides = component_text or {}
    for key, table in lookup_tables.items():
        override = overrides.get(key)
        if isinstance(override, str) and override.strip():
            parts.append(override)
            continue
        component_id = components.get(key)
        if component_id:
            parts.append(table[component_id])
    return "\n".join(parts)


# Standard nonfiction sentence length used to derive the sentence budget S_target
# from a visible-word target (see system-prompt-v2 spec §3.2).
SENTENCE_WORDS = 18

# Prompt-component key that overrides the per-sentence word budget (defaults to
# SENTENCE_WORDS when absent). E.g. {"target_sentence_words": "14"} forces a
# tighter sentence budget, which lowers average sentence length and the
# Flesch-Kincaid reading grade of the output.
TARGET_SENTENCE_WORDS_KEY = "target_sentence_words"


def _words_per_sentence(stage: Any) -> int:
    raw = str(((getattr(stage, "prompt_components", None) or {}).get(TARGET_SENTENCE_WORDS_KEY) or "")).strip()
    if not raw:
        return SENTENCE_WORDS
    try:
        value = int(raw)
    except ValueError:
        return SENTENCE_WORDS
    return value if value >= 3 else SENTENCE_WORDS


def sentence_budget(target_words: int, *, words_per_sentence: Optional[int] = None) -> int:
    wps = words_per_sentence if words_per_sentence is not None else SENTENCE_WORDS
    return max(1, int(round(max(1, target_words) / wps)))


def chapter_component_tables() -> Dict[str, Dict[str, str]]:
    return {
        "system_style": CHAPTER_SYSTEM_STYLES,
        "detail_policy": DETAIL_POLICIES,
        "qualifier_policy": QUALIFIER_POLICIES,
        "structure_policy": STRUCTURE_POLICIES,
        "example_policy": EXAMPLE_POLICIES,
        "terminology_policy": TERMINOLOGY_POLICIES,
        "anti_fluff_policy": ANTI_FLUFF_POLICIES,
        "length_policy": LENGTH_POLICIES,
    }


def composer_component_tables() -> Dict[str, Dict[str, str]]:
    return {
        "system_style": COMPOSER_SYSTEM_STYLES,
        "synthesis_policy": COMPOSER_STRATEGIES,
        "detail_policy": DETAIL_POLICIES,
        "qualifier_policy": QUALIFIER_POLICIES,
        "structure_policy": STRUCTURE_POLICIES,
        "terminology_policy": TERMINOLOGY_POLICIES,
        "anti_fluff_policy": ANTI_FLUFF_POLICIES,
        "length_policy": LENGTH_POLICIES,
    }


COMPONENT_HELP: Dict[str, str] = {
    "system_style": "Overall voice and identity of the summarizer: how dense, faithful, and explanatory the writing should feel. Sets the framing every other component builds on.",
    "detail_policy": "How much explanatory depth to keep per concept (definitions, mechanisms, causal reasoning, examples) within the target word budget.",
    "qualifier_policy": "How strictly to preserve scope conditions, caveats, uncertainty, and limits so a qualified claim is never overstated as absolute.",
    "structure_policy": "How the summary is organized: heading-following sections, a few thematic clusters, or dense bullets.",
    "example_policy": "How many source examples to carry over and whether anecdotes are kept or dropped as non-essential.",
    "terminology_policy": "Whether to keep the source's own terms verbatim or add glosses and plain-language explanations for jargon.",
    "anti_fluff_policy": "How aggressively to strip filler, hedging, rhetorical framing, and repetition that adds no information.",
    "length_policy": "How the word/sentence budget is communicated to the model: explicit first-pass budget, outline-then-fill, or a strict sentence target.",
    "target_sentence_words": "Integer override for the assumed words-per-sentence used to derive the sentence budget (default 18). Lower values force shorter sentences and a lower reading grade.",
    "synthesis_policy": "Composer-only: whether to lead with the thesis then frameworks, or build a progressive argument that accumulates across chapters.",
}

CHAPTER_COMPONENT_KEYS: Tuple[str, ...] = (
    "system_style", "detail_policy", "qualifier_policy", "structure_policy",
    "example_policy", "terminology_policy", "anti_fluff_policy", "length_policy",
)

COMPOSER_COMPONENT_KEYS: Tuple[str, ...] = (
    "system_style", "synthesis_policy", "detail_policy", "qualifier_policy",
    "structure_policy", "terminology_policy", "anti_fluff_policy", "length_policy",
)


def component_help() -> Dict[str, str]:
    """Human-readable tooltip text for each prompt component dimension."""
    return dict(COMPONENT_HELP)


def _render_system_values(composed: str) -> Dict[str, str]:
    return {
        "components": composed,
        "book_title": "",
        "chapter_title": "",
        "toc_md": "",
        "book_metadata": "",
    }


def _substitute_system_text(text: str) -> str:
    if _prompt_template is None or not text or "{{" not in text:
        return text
    try:
        return _prompt_template.render(text, _render_system_values(text))
    except _prompt_template.TemplateError:
        return text


def render_chapter_system(spec: Optional[CandidateSpec] = None, *, components: Optional[Dict[str, str]] = None) -> str:
    stage = getattr(spec, "chapter_stage", None) if spec is not None else None
    if components is None:
        components = (stage.prompt_components if stage is not None else {}) or {}
    composed = _component_text(components, chapter_component_tables(), getattr(stage, "component_text", None))
    composed = _substitute_system_text(composed)
    template = (getattr(stage, "system_template", None) or "") if stage is not None else ""
    if template.strip() and _prompt_template is not None:
        try:
            return _prompt_template.render(template, _render_system_values(composed))
        except _prompt_template.TemplateError:
            return composed
    return composed



def render_chapter_user(
    spec: CandidateSpec,
    *,
    source_md: str,
    target_words: int,
    book_title: str = "",
    chapter_title: str = "",
    toc_md: str = "",
    book_metadata: str = "",
) -> str:
    stage = spec.chapter_stage
    low, high = visible_word_range(target_words, spec.length_control.tolerance_pct)
    wps = _words_per_sentence(stage)
    if stage.prompt_components.get("length_policy"):
        s_target = sentence_budget(target_words, words_per_sentence=wps)
        target_block = (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            f"Target sentences: {s_target}\n(about {wps} words each). "
            f"Try to land inside the word range on the first pass; do not exceed {s_target} sentences."
        )
    else:
        s_target = sentence_budget(target_words, words_per_sentence=wps)
        target_block = (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            "Try to land inside the range on the first pass."
        )
    context = _context_block(
        context_mode=stage.context_mode,
        book_title=book_title,
        chapter_title=chapter_title,
        toc_md=toc_md,
        book_metadata=book_metadata,
    )
    format_instructions = FORMAT_INSTRUCTIONS[stage.format_mode]
    required_behavior = CHAPTER_REQUIRED_BEHAVIOR
    template = getattr(stage, "user_template", None) or ""
    if template.strip() and _prompt_template is not None:
        values = {
            "book_title": book_title,
            "chapter_title": chapter_title,
            "toc_md": toc_md,
            "book_metadata": book_metadata,
            "target_words": target_words,
            "low": low,
            "high": high,
            "s_target": s_target,
            "source_md": source_md.strip(),
            "context_block": context,
            "format_instructions": format_instructions,
            "required_behavior": required_behavior,
        }
        try:
            return _prompt_template.render(template, values)
        except _prompt_template.TemplateError:
            pass
    blocks = [
        "Write a faithful chapter summary.",
        context,
        target_block,
        format_instructions,
        required_behavior,
        "Source chapter markdown:\n" + source_md.strip(),
    ]
    return "\n\n".join(block for block in blocks if block)


def render_repair_user(
    spec: CandidateSpec,
    *,
    source_md: str,
    current_summary_md: str,
    target_words: int,
    direction: Literal["more", "less"],
    book_title: str = "",
    chapter_title: str = "",
    current_word_count: Optional[int] = None,
) -> str:
    policy = (
        REPAIR_MORE_POLICIES[spec.length_control.repair_more_prompt_id]
        if direction == "more"
        else REPAIR_LESS_POLICIES[spec.length_control.repair_less_prompt_id]
    )
    low, high = visible_word_range(target_words, spec.length_control.tolerance_pct)
    lc = spec.length_control
    numeric_delta = ""
    if current_word_count is not None:
        if direction == "less":
            ceiling = low if lc.repair_target_mode == "low" else high
            gap = max(0, current_word_count - ceiling)
            min_cut = math.ceil(current_word_count * lc.repair_min_reduction_pct) if lc.repair_min_reduction_pct > 0 else 0
            cut = max(gap, min_cut)
            landing = (
                f"land at or below {low}" if lc.repair_target_mode == "low"
                else f"land inside [{low}, {high}]"
            )
            numeric_delta = (
                f"Current summary is {current_word_count} visible words; target is {target_words} "
                f"(range {low}-{high}). Delete at least {cut} words so the final markdown does {landing}. "
                f"Your output MUST be strictly shorter than {current_word_count} visible words; a revision "
                "that is not shorter is rejected. Count the visible words of your output before finishing "
                "and delete more if needed."
            )
        else:
            numeric_delta = (
                f"Current summary is {current_word_count} visible words; target is {target_words} "
                f"(range {low}-{high}). Add approximately {max(0, low - current_word_count)} words so "
                f"the final markdown lands inside [{low}, {high}]. Count the visible words of your output "
                "before finishing and adjust if needed."
            )
        if spec.chapter_stage.prompt_components.get("length_policy"):
            numeric_delta += (
                f" The sentence budget is {sentence_budget(target_words, words_per_sentence=_words_per_sentence(spec.chapter_stage))}; adjust sentence count as well "
                "as word count."
            )
        if direction == "less" and lc.repair_sentence_delete:
            numeric_delta += (
                " Prefer deleting whole sentences, lowest-value first, over rewording; do not add new "
                "sentences."
            )
    return "\n\n".join(
        block
        for block in [
            "Revise the existing chapter summary to hit the target length more accurately.",
            f"Book title: {book_title}" if book_title else "",
            f"Chapter title: {chapter_title}" if chapter_title else "",
            (
                f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
                f"This repair direction is: {direction}."
            ),
            numeric_delta if numeric_delta else "",
            policy,
            FORMAT_INSTRUCTIONS[spec.chapter_stage.format_mode],
            "Keep the result faithful to the source.",
            "Current summary:\n" + current_summary_md.strip(),
            "Source chapter markdown:\n" + source_md.strip(),
        ]
        if block
    )


def render_composer_system(spec: Optional[CandidateSpec] = None, *, components: Optional[Dict[str, str]] = None) -> str:
    stage = getattr(spec, "composer_stage", None) if spec is not None else None
    if components is None:
        components = (stage.prompt_components if stage is not None else {}) or {}
    composed = _component_text(components, composer_component_tables(), getattr(stage, "component_text", None))
    composed = _substitute_system_text(composed)
    template = (getattr(stage, "system_template", None) or "") if stage is not None else ""
    if template.strip() and _prompt_template is not None:
        try:
            return _prompt_template.render(template, _render_system_values(composed))
        except _prompt_template.TemplateError:
            return composed
    return composed



def render_composer_user(
    spec: CandidateSpec,
    *,
    chapter_summaries_md: str,
    target_words: int,
    book_title: str = "",
    toc_md: str = "",
    book_metadata: str = "",
    retrieved_source_excerpts: str = "",
) -> str:
    stage = spec.composer_stage
    low, high = visible_word_range(target_words, spec.length_control.tolerance_pct)
    wps = _words_per_sentence(stage)
    s_target = sentence_budget(target_words, words_per_sentence=wps)
    if stage.prompt_components.get("length_policy"):
        target_block = (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            f"Target sentences: {s_target}\n(about {wps} words each). "
            "Remove cross-chapter redundancy and keep the result dense."
        )
    else:
        target_block = (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            "Remove cross-chapter redundancy and keep the result dense."
        )
    context = _context_block(
        context_mode=stage.context_mode,
        book_title=book_title,
        chapter_title="",
        toc_md=toc_md,
        book_metadata=book_metadata,
    )
    format_instructions = FORMAT_INSTRUCTIONS[stage.format_mode]
    required_behavior = COMPOSER_REQUIRED_BEHAVIOR
    template = getattr(stage, "user_template", None) or ""
    if template.strip() and _prompt_template is not None:
        values = {
            "book_title": book_title,
            "chapter_summaries": chapter_summaries_md.strip(),
            "target_words": target_words,
            "low": low,
            "high": high,
            "s_target": s_target,
            "format_instructions": format_instructions,
        }
        try:
            return _prompt_template.render(template, values)
        except _prompt_template.TemplateError:
            pass
    blocks = [
        "Compose a faithful whole-book summary from the provided chapter summaries.",
        context,
        target_block,
        format_instructions,
        required_behavior,
        "Chapter summaries:\n" + chapter_summaries_md.strip(),
    ]
    if spec.composer_mode in {"hybrid_retrieve", "source_aware"} and retrieved_source_excerpts.strip():
        blocks.append("Retrieved source excerpts:\n" + retrieved_source_excerpts.strip())
    return "\n\n".join(block for block in blocks if block)


def build_openrouter_request(
    *,
    stage: StageConfig,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    use_json_schema: bool = True,
    max_tokens: Optional[int] = None,
) -> Dict[str, object]:
    """Return a single-model OpenRouter chat request body.

    The benchmark should keep one model per stage and avoid model arrays or
    benchmark-time fallbacks. Provider routing can still be pinned externally.

    ``max_tokens`` overrides ``stage.max_tokens`` per request (the
    target-aware generation-time cap); when omitted the stage's effort-scaled
    budget is used.
    """
    request: Dict[str, object] = {
        "model": stage.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": stage.temperature,
        "max_tokens": stage.max_tokens if max_tokens is None else int(max_tokens),
    }
    if stage.seed is not None:
        request["seed"] = stage.seed
    if stage.provider is not None:
        request["provider"] = stage.provider
    if use_json_schema:
        schema = dict(SUMMARY_JSON_SCHEMA)
        schema["name"] = schema_name
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": schema,
        }
        json_hint = "Respond using JSON format exactly matching the provided schema.\n\n"
        if request["messages"][0]["role"] == "system":
            request["messages"][0]["content"] = json_hint + request["messages"][0]["content"]
    if reasoning_request_params is not None:
        params, _ = reasoning_request_params(stage)
        if params:
            request.update(params)
    if stage.extra_body:
        request["extra_body"] = stage.extra_body
    return request


PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {
    "30m_deepseek-v4-flash_notthinking": CandidateSpec(
        name="30m_deepseek-v4-flash_notthinking_v1",
        profile="30m_deepseek-v4-flash_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-flash_thinking": CandidateSpec(
        name="30m_deepseek-v4-flash_thinking_v1",
        profile="30m_deepseek-v4-flash_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-pro_notthinking": CandidateSpec(
        name="30m_deepseek-v4-pro_notthinking_v1",
        profile="30m_deepseek-v4-pro_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-pro_thinking": CandidateSpec(
        name="30m_deepseek-v4-pro_thinking_v1",
        profile="30m_deepseek-v4-pro_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3-flash-preview_notthinking": CandidateSpec(
        name="30m_gemini-3-flash-preview_notthinking_v1",
        profile="30m_gemini-3-flash-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3-flash-preview chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3-flash-preview_thinking": CandidateSpec(
        name="30m_gemini-3-flash-preview_thinking_v1",
        profile="30m_gemini-3-flash-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3-flash-preview chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3.1-flash-lite-preview_notthinking": CandidateSpec(
        name="30m_gemini-3.1-flash-lite-preview_notthinking_v1",
        profile="30m_gemini-3.1-flash-lite-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-flash-lite-preview chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3.1-flash-lite-preview_thinking": CandidateSpec(
        name="30m_gemini-3.1-flash-lite-preview_thinking_v1",
        profile="30m_gemini-3.1-flash-lite-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-flash-lite-preview chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3.1-pro-preview_notthinking": CandidateSpec(
        name="30m_gemini-3.1-pro-preview_notthinking_v1",
        profile="30m_gemini-3.1-pro-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-pro-preview chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3.1-pro-preview_thinking": CandidateSpec(
        name="30m_gemini-3.1-pro-preview_thinking_v1",
        profile="30m_gemini-3.1-pro-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-pro-preview chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gemini-3.5-flash_notthinking": CandidateSpec(
        name="30m_gemini-3.5-flash_notthinking_v1",
        profile="30m_gemini-3.5-flash_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.5-flash chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_gemini-3.5-flash_thinking": CandidateSpec(
        name="30m_gemini-3.5-flash_thinking_v1",
        profile="30m_gemini-3.5-flash_thinking",
        chapter_stage=StageConfig(model="google/gemini-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.5-flash chapter+composer, 30m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "30m_glm-5.3-flash_effort-minimal": CandidateSpec(
        name="30m_glm-5.3-flash_effort-minimal_v1",
        profile="30m_glm-5.3-flash_effort-minimal",
        chapter_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        composer_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: z-ai/glm-5.3-flash chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_glm-5.3-flash_notthinking": CandidateSpec(
        name="30m_glm-5.3-flash_notthinking_v1",
        profile="30m_glm-5.3-flash_notthinking",
        chapter_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: z-ai/glm-5.3-flash chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gpt-5.4-mini_notthinking": CandidateSpec(
        name="30m_gpt-5.4-mini_notthinking_v1",
        profile="30m_gpt-5.4-mini_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-mini chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gpt-5.4-mini_thinking": CandidateSpec(
        name="30m_gpt-5.4-mini_thinking_v1",
        profile="30m_gpt-5.4-mini_thinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-mini chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gpt-5.4-nano_notthinking": CandidateSpec(
        name="30m_gpt-5.4-nano_notthinking_v1",
        profile="30m_gpt-5.4-nano_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-nano chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_gpt-5.4-nano_thinking": CandidateSpec(
        name="30m_gpt-5.4-nano_thinking_v1",
        profile="30m_gpt-5.4-nano_thinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-nano chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_gpt-5.6-luna_effort-medium": CandidateSpec(
        name="30m_gpt-5.6-luna_effort-medium_v1",
        profile="30m_gpt-5.6-luna_effort-medium",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 30m, effort-medium (effort=medium), schema=False",
        disable_composer=False
    ),
    "30m_gpt-5.6-luna_effort-minimal": CandidateSpec(
        name="30m_gpt-5.6-luna_effort-minimal_v1",
        profile="30m_gpt-5.6-luna_effort-minimal",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 30m, effort-minimal (effort=minimal), schema=False",
        disable_composer=False
    ),
    "30m_gpt-5.6-luna_notthinking": CandidateSpec(
        name="30m_gpt-5.6-luna_notthinking_v1",
        profile="30m_gpt-5.6-luna_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_gpt-oss-120b_notthinking": CandidateSpec(
        name="30m_gpt-oss-120b_notthinking_v1",
        profile="30m_gpt-oss-120b_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-oss-120b chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_gpt-oss-120b_thinking": CandidateSpec(
        name="30m_gpt-oss-120b_thinking_v1",
        profile="30m_gpt-oss-120b_thinking",
        chapter_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-oss-120b chapter+composer, 30m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "30m_grok-4.3_notthinking": CandidateSpec(
        name="30m_grok-4.3_notthinking_v1",
        profile="30m_grok-4.3_notthinking",
        chapter_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: x-ai/grok-4.3 chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_grok-4.3_thinking": CandidateSpec(
        name="30m_grok-4.3_thinking_v1",
        profile="30m_grok-4.3_thinking",
        chapter_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: x-ai/grok-4.3 chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2-flash_notthinking": CandidateSpec(
        name="30m_mimo-v2-flash_notthinking_v1",
        profile="30m_mimo-v2-flash_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2-flash_thinking": CandidateSpec(
        name="30m_mimo-v2-flash_thinking_v1",
        profile="30m_mimo-v2-flash_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_minimax-m2.5_notthinking": CandidateSpec(
        name="30m_minimax-m2.5_notthinking_v1",
        profile="30m_minimax-m2.5_notthinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.5 chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_minimax-m2.5_thinking": CandidateSpec(
        name="30m_minimax-m2.5_thinking_v1",
        profile="30m_minimax-m2.5_thinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.5 chapter+composer, 30m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "30m_minimax-m2.7_notthinking": CandidateSpec(
        name="30m_minimax-m2.7_notthinking_v1",
        profile="30m_minimax-m2.7_notthinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.7 chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_minimax-m2.7_thinking": CandidateSpec(
        name="30m_minimax-m2.7_thinking_v1",
        profile="30m_minimax-m2.7_thinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.7 chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_muse-glimmer-30b_effort-medium": CandidateSpec(
        name="30m_muse-glimmer-30b_effort-medium_v1",
        profile="30m_muse-glimmer-30b_effort-medium",
        chapter_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: meta/muse-glimmer-30b chapter+composer, 30m, effort-medium (effort=medium), schema=False",
        disable_composer=False
    ),
    "30m_muse-glimmer-30b_notthinking": CandidateSpec(
        name="30m_muse-glimmer-30b_notthinking_v1",
        profile="30m_muse-glimmer-30b_notthinking",
        chapter_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: meta/muse-glimmer-30b chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_nemotron-3-super-120b-a12b_notthinking": CandidateSpec(
        name="30m_nemotron-3-super-120b-a12b_notthinking_v1",
        profile="30m_nemotron-3-super-120b-a12b_notthinking",
        chapter_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3-super-120b-a12b chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_nemotron-3-super-120b-a12b_thinking": CandidateSpec(
        name="30m_nemotron-3-super-120b-a12b_thinking_v1",
        profile="30m_nemotron-3-super-120b-a12b_thinking",
        chapter_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3-super-120b-a12b chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_nemotron-3.5-lightning_effort-medium": CandidateSpec(
        name="30m_nemotron-3.5-lightning_effort-medium_v1",
        profile="30m_nemotron-3.5-lightning_effort-medium",
        chapter_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3.5-lightning chapter+composer, 30m, effort-medium, schema=True",
        disable_composer=False
    ),
    "30m_nemotron-3.5-lightning_effort-minimal": CandidateSpec(
        name="30m_nemotron-3.5-lightning_effort-minimal_v1",
        profile="30m_nemotron-3.5-lightning_effort-minimal",
        chapter_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3.5-lightning chapter+composer, 30m, effort-minimal, schema=True",
        disable_composer=False
    ),
    "30m_nemotron-3.5-lightning_notthinking": CandidateSpec(
        name="30m_nemotron-3.5-lightning_notthinking_v1",
        profile="30m_nemotron-3.5-lightning_notthinking",
        chapter_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3.5-lightning", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3.5-lightning chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_qwen3.6-35b-a3b_notthinking": CandidateSpec(
        name="30m_qwen3.6-35b-a3b_notthinking_v1",
        profile="30m_qwen3.6-35b-a3b_notthinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-35b-a3b chapter+composer, 30m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "30m_qwen3.6-35b-a3b_thinking": CandidateSpec(
        name="30m_qwen3.6-35b-a3b_thinking_v1",
        profile="30m_qwen3.6-35b-a3b_thinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-35b-a3b chapter+composer, 30m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "30m_qwen3.6-plus_notthinking": CandidateSpec(
        name="30m_qwen3.6-plus_notthinking_v1",
        profile="30m_qwen3.6-plus_notthinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-plus chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_qwen3.6-plus_thinking": CandidateSpec(
        name="30m_qwen3.6-plus_thinking_v1",
        profile="30m_qwen3.6-plus_thinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-plus chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_qwen3.7-max_notthinking": CandidateSpec(
        name="30m_qwen3.7-max_notthinking_v1",
        profile="30m_qwen3.7-max_notthinking",
        chapter_stage=StageConfig(model="qwen/qwen3.7-max", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.7-max", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.7-max chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_qwen3.7-max_thinking": CandidateSpec(
        name="30m_qwen3.7-max_thinking_v1",
        profile="30m_qwen3.7-max_thinking",
        chapter_stage=StageConfig(model="qwen/qwen3.7-max", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.7-max", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.7-max chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_step-3.5-flash_notthinking": CandidateSpec(
        name="30m_step-3.5-flash_notthinking_v1",
        profile="30m_step-3.5-flash_notthinking",
        chapter_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.5-flash chapter+composer, 30m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "30m_step-3.5-flash_thinking": CandidateSpec(
        name="30m_step-3.5-flash_thinking_v1",
        profile="30m_step-3.5-flash_thinking",
        chapter_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.5-flash chapter+composer, 30m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "30m_step-3.7-flash_effort-medium": CandidateSpec(
        name="30m_step-3.7-flash_effort-medium_v1",
        profile="30m_step-3.7-flash_effort-medium",
        chapter_stage=StageConfig(model="stepfun/step-3.7-flash", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.7-flash", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.7-flash chapter+composer, 30m, effort-medium, schema=False",
        disable_composer=False
    ),
    "60m_deepseek-v4-flash_notthinking": CandidateSpec(
        name="60m_deepseek-v4-flash_notthinking_v1",
        profile="60m_deepseek-v4-flash_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-flash_thinking": CandidateSpec(
        name="60m_deepseek-v4-flash_thinking_v1",
        profile="60m_deepseek-v4-flash_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-pro_notthinking": CandidateSpec(
        name="60m_deepseek-v4-pro_notthinking_v1",
        profile="60m_deepseek-v4-pro_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-pro_thinking": CandidateSpec(
        name="60m_deepseek-v4-pro_thinking_v1",
        profile="60m_deepseek-v4-pro_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3-flash-preview_notthinking": CandidateSpec(
        name="60m_gemini-3-flash-preview_notthinking_v1",
        profile="60m_gemini-3-flash-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3-flash-preview chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3-flash-preview_thinking": CandidateSpec(
        name="60m_gemini-3-flash-preview_thinking_v1",
        profile="60m_gemini-3-flash-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3-flash-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3-flash-preview chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3.1-flash-lite-preview_notthinking": CandidateSpec(
        name="60m_gemini-3.1-flash-lite-preview_notthinking_v1",
        profile="60m_gemini-3.1-flash-lite-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-flash-lite-preview chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3.1-flash-lite-preview_thinking": CandidateSpec(
        name="60m_gemini-3.1-flash-lite-preview_thinking_v1",
        profile="60m_gemini-3.1-flash-lite-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-flash-lite-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-flash-lite-preview chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3.1-pro-preview_notthinking": CandidateSpec(
        name="60m_gemini-3.1-pro-preview_notthinking_v1",
        profile="60m_gemini-3.1-pro-preview_notthinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-pro-preview chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gemini-3.1-pro-preview_thinking": CandidateSpec(
        name="60m_gemini-3.1-pro-preview_thinking_v1",
        profile="60m_gemini-3.1-pro-preview_thinking",
        chapter_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="google/gemini-3.1-pro-preview", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: google/gemini-3.1-pro-preview chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_glm-5.3-flash_notthinking": CandidateSpec(
        name="60m_glm-5.3-flash_notthinking_v1",
        profile="60m_glm-5.3-flash_notthinking",
        chapter_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="z-ai/glm-5.3-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: z-ai/glm-5.3-flash chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gpt-5.4-mini_notthinking": CandidateSpec(
        name="60m_gpt-5.4-mini_notthinking_v1",
        profile="60m_gpt-5.4-mini_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-mini chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gpt-5.4-mini_thinking": CandidateSpec(
        name="60m_gpt-5.4-mini_thinking_v1",
        profile="60m_gpt-5.4-mini_thinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-mini", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-mini chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_gpt-5.4-nano_notthinking": CandidateSpec(
        name="60m_gpt-5.4-nano_notthinking_v1",
        profile="60m_gpt-5.4-nano_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-nano chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_gpt-5.4-nano_thinking": CandidateSpec(
        name="60m_gpt-5.4-nano_thinking_v1",
        profile="60m_gpt-5.4-nano_thinking",
        chapter_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.4-nano", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.4-nano chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_gpt-5.6-luna_effort-medium": CandidateSpec(
        name="60m_gpt-5.6-luna_effort-medium_v1",
        profile="60m_gpt-5.6-luna_effort-medium",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 60m, effort-medium (effort=medium), schema=False",
        disable_composer=False
    ),
    "60m_gpt-5.6-luna_effort-minimal": CandidateSpec(
        name="60m_gpt-5.6-luna_effort-minimal_v1",
        profile="60m_gpt-5.6-luna_effort-minimal",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=9103, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "minimal"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 60m, effort-minimal (effort=minimal), schema=False",
        disable_composer=False
    ),
    "60m_gpt-5.6-luna_notthinking": CandidateSpec(
        name="60m_gpt-5.6-luna_notthinking_v1",
        profile="60m_gpt-5.6-luna_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-5.6-luna", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-5.6-luna chapter+composer, 60m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "60m_gpt-oss-120b_notthinking": CandidateSpec(
        name="60m_gpt-oss-120b_notthinking_v1",
        profile="60m_gpt-oss-120b_notthinking",
        chapter_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-oss-120b chapter+composer, 60m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "60m_gpt-oss-120b_thinking": CandidateSpec(
        name="60m_gpt-oss-120b_thinking_v1",
        profile="60m_gpt-oss-120b_thinking",
        chapter_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="openai/gpt-oss-120b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: openai/gpt-oss-120b chapter+composer, 60m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "60m_grok-4.3_notthinking": CandidateSpec(
        name="60m_grok-4.3_notthinking_v1",
        profile="60m_grok-4.3_notthinking",
        chapter_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: x-ai/grok-4.3 chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_grok-4.3_thinking": CandidateSpec(
        name="60m_grok-4.3_thinking_v1",
        profile="60m_grok-4.3_thinking",
        chapter_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="x-ai/grok-4.3", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: x-ai/grok-4.3 chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2-flash_notthinking": CandidateSpec(
        name="60m_mimo-v2-flash_notthinking_v1",
        profile="60m_mimo-v2-flash_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2-flash_thinking": CandidateSpec(
        name="60m_mimo-v2-flash_thinking_v1",
        profile="60m_mimo-v2-flash_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_minimax-m2.5_notthinking": CandidateSpec(
        name="60m_minimax-m2.5_notthinking_v1",
        profile="60m_minimax-m2.5_notthinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.5 chapter+composer, 60m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "60m_minimax-m2.5_thinking": CandidateSpec(
        name="60m_minimax-m2.5_thinking_v1",
        profile="60m_minimax-m2.5_thinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.5", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.5 chapter+composer, 60m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "60m_minimax-m2.7_notthinking": CandidateSpec(
        name="60m_minimax-m2.7_notthinking_v1",
        profile="60m_minimax-m2.7_notthinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.7 chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_minimax-m2.7_thinking": CandidateSpec(
        name="60m_minimax-m2.7_thinking_v1",
        profile="60m_minimax-m2.7_thinking",
        chapter_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="minimax/minimax-m2.7", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: minimax/minimax-m2.7 chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_muse-glimmer-30b_effort-medium": CandidateSpec(
        name="60m_muse-glimmer-30b_effort-medium_v1",
        profile="60m_muse-glimmer-30b_effort-medium",
        chapter_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: meta/muse-glimmer-30b chapter+composer, 60m, effort-medium (effort=medium), schema=False",
        disable_composer=False
    ),
    "60m_muse-glimmer-30b_notthinking": CandidateSpec(
        name="60m_muse-glimmer-30b_notthinking_v1",
        profile="60m_muse-glimmer-30b_notthinking",
        chapter_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="meta/muse-glimmer-30b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: meta/muse-glimmer-30b chapter+composer, 60m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "60m_nemotron-3-super-120b-a12b_notthinking": CandidateSpec(
        name="60m_nemotron-3-super-120b-a12b_notthinking_v1",
        profile="60m_nemotron-3-super-120b-a12b_notthinking",
        chapter_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3-super-120b-a12b chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_nemotron-3-super-120b-a12b_thinking": CandidateSpec(
        name="60m_nemotron-3-super-120b-a12b_thinking_v1",
        profile="60m_nemotron-3-super-120b-a12b_thinking",
        chapter_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="nvidia/nemotron-3-super-120b-a12b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: nvidia/nemotron-3-super-120b-a12b chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_qwen3.6-35b-a3b_notthinking": CandidateSpec(
        name="60m_qwen3.6-35b-a3b_notthinking_v1",
        profile="60m_qwen3.6-35b-a3b_notthinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-35b-a3b chapter+composer, 60m, notthinking (effort=none), schema=False",
        disable_composer=False
    ),
    "60m_qwen3.6-35b-a3b_thinking": CandidateSpec(
        name="60m_qwen3.6-35b-a3b_thinking_v1",
        profile="60m_qwen3.6-35b-a3b_thinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-35b-a3b", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-35b-a3b chapter+composer, 60m, thinking (enabled=true), schema=False",
        disable_composer=False
    ),
    "60m_qwen3.6-plus_notthinking": CandidateSpec(
        name="60m_qwen3.6-plus_notthinking_v1",
        profile="60m_qwen3.6-plus_notthinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-plus chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_qwen3.6-plus_thinking": CandidateSpec(
        name="60m_qwen3.6-plus_thinking_v1",
        profile="60m_qwen3.6-plus_thinking",
        chapter_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="qwen/qwen3.6-plus", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: qwen/qwen3.6-plus chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_step-3.5-flash_notthinking": CandidateSpec(
        name="60m_step-3.5-flash_notthinking_v1",
        profile="60m_step-3.5-flash_notthinking",
        chapter_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": False}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.5-flash chapter+composer, 60m, notthinking (effort=none), schema=True",
        disable_composer=False
    ),
    "60m_step-3.5-flash_thinking": CandidateSpec(
        name="60m_step-3.5-flash_thinking_v1",
        profile="60m_step-3.5-flash_thinking",
        chapter_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.5-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"enabled": True}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.5-flash chapter+composer, 60m, thinking (enabled=true), schema=True",
        disable_composer=False
    ),
    "60m_step-3.7-flash_effort-medium": CandidateSpec(
        name="60m_step-3.7-flash_effort-medium_v1",
        profile="60m_step-3.7-flash_effort-medium",
        chapter_stage=StageConfig(model="stepfun/step-3.7-flash", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        composer_stage=StageConfig(model="stepfun/step-3.7-flash", temperature=0.2, seed=42, max_tokens=16384, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, provider=None, reasoning={"effort": "medium"}, extra_body=None),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.1, repair_strategy="edit_existing",
            repair_more_prompt_id="expand_mechanisms_first", repair_less_prompt_id="shrink_sentence_delete", max_truncation_retries=1,
            repair_target_mode="low", repair_sentence_delete=True, repair_min_reduction_pct=0.08,
            early_stop_patience=1, early_stop_epsilon=3, stop_on_direction_flip=True, accept_within_hard_tolerance=True
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: stepfun/step-3.7-flash chapter+composer, 60m, effort-medium, schema=False",
        disable_composer=False
    )

}
def get_candidate(profile: Profile) -> CandidateSpec:
    if profile in PROFILE_CANDIDATES:
        spec = PROFILE_CANDIDATES[profile]
        # Check for optimized prompt component overrides from permutation files
        _apply_permutation_overrides(profile, spec)
        return spec

    # Try resolving v2+ variants from permutation history
    from autoresearch.permutation_store import resolve_variant
    spec = resolve_variant(profile)
    if spec is not None:
        return spec
    raise KeyError(f"Unknown profile: {profile}")


def _apply_permutation_overrides(profile: Profile, spec: CandidateSpec):
    """Apply optimized prompt components from permutation files if they exist."""
    try:
        from autoresearch.permutation_store import get_prompt_override
    except ImportError:
        return
    for stage_name, attr_name in [("chapter", "chapter_stage"), ("composer", "composer_stage")]:
        stage = getattr(spec, attr_name, None)
        if stage is None:
            continue
        override = get_prompt_override(profile, stage_name)
        if override:
            stage.prompt_components.update(override)

def get_all_profiles() -> list[Profile]:
    return list(PROFILE_CANDIDATES.keys())

def get_profiles_by_time(time_budget: str) -> list[Profile]:
    """Return profiles matching the given time budget ('30m', '60m', or 'all')."""
    if time_budget == "all":
        return get_all_profiles()
    return [p for p in PROFILE_CANDIDATES if p.startswith(f"{time_budget}_")]
