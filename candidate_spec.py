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

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Literal, Optional, Sequence, Tuple

Profile = Literal[
    "30m_deepseek-v4-flash_notthinking", "30m_deepseek-v4-flash_thinking", "30m_deepseek-v4-pro_notthinking",
    "30m_deepseek-v4-pro_thinking", "30m_mimo-v2-flash_notthinking", "30m_mimo-v2-flash_thinking",
    "30m_mimo-v2.5-pro_notthinking", "30m_mimo-v2.5-pro_thinking", "60m_deepseek-v4-flash_notthinking",
    "60m_deepseek-v4-flash_thinking", "60m_deepseek-v4-pro_notthinking", "60m_deepseek-v4-pro_thinking",
    "60m_mimo-v2-flash_notthinking", "60m_mimo-v2-flash_thinking", "60m_mimo-v2.5-pro_notthinking",
    "60m_mimo-v2.5-pro_thinking"
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


@dataclass
class StageConfig:
    model: str
    temperature: float = 0.2
    seed: Optional[int] = 42
    max_tokens: int = 8192
    format_mode: FormatMode = "markdown_sections"
    context_mode: ContextMode = "chapter_plus_toc_and_meta"
    prompt_components: Dict[str, str] = field(default_factory=dict)
    provider_order: Tuple[str, ...] = ()
    allow_fallbacks: bool = False
    use_json_schema: Optional[bool] = None
    extra_body: Optional[Dict[str, Any]] = None


@dataclass
class LengthControlConfig:
    max_passes: int = 5
    tolerance_pct: float = 0.05
    hard_tolerance_pct: float = 0.10
    repair_strategy: RepairStrategy = "edit_existing"
    repair_more_prompt_id: str = "expand_missing_detail"
    repair_less_prompt_id: str = "shrink_dedup_first"


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
    "strict": True,
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


def _component_text(components: Dict[str, str], lookup_tables: Dict[str, Dict[str, str]]) -> str:
    parts: List[str] = []
    for key, table in lookup_tables.items():
        component_id = components.get(key)
        if component_id:
            parts.append(table[component_id])
    return "\n".join(parts)


def render_chapter_system(spec: CandidateSpec) -> str:
    stage = spec.chapter_stage
    return _component_text(
        stage.prompt_components,
        {
            "system_style": CHAPTER_SYSTEM_STYLES,
            "detail_policy": DETAIL_POLICIES,
            "qualifier_policy": QUALIFIER_POLICIES,
            "structure_policy": STRUCTURE_POLICIES,
            "example_policy": EXAMPLE_POLICIES,
            "terminology_policy": TERMINOLOGY_POLICIES,
            "anti_fluff_policy": ANTI_FLUFF_POLICIES,
        },
    )


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
    blocks = [
        "Write a faithful chapter summary.",
        _context_block(
            context_mode=stage.context_mode,
            book_title=book_title,
            chapter_title=chapter_title,
            toc_md=toc_md,
            book_metadata=book_metadata,
        ),
        (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            "Try to land inside the range on the first pass."
        ),
        FORMAT_INSTRUCTIONS[stage.format_mode],
        (
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
        ),
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
) -> str:
    policy = (
        REPAIR_MORE_POLICIES[spec.length_control.repair_more_prompt_id]
        if direction == "more"
        else REPAIR_LESS_POLICIES[spec.length_control.repair_less_prompt_id]
    )
    low, high = visible_word_range(target_words, spec.length_control.tolerance_pct)
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
            policy,
            FORMAT_INSTRUCTIONS[spec.chapter_stage.format_mode],
            "Keep the result faithful to the source.",
            "Current summary:\n" + current_summary_md.strip(),
            "Source chapter markdown:\n" + source_md.strip(),
        ]
        if block
    )


def render_composer_system(spec: CandidateSpec) -> str:
    stage = spec.composer_stage
    return _component_text(
        stage.prompt_components,
        {
            "system_style": COMPOSER_SYSTEM_STYLES,
            "synthesis_policy": COMPOSER_STRATEGIES,
            "detail_policy": DETAIL_POLICIES,
            "qualifier_policy": QUALIFIER_POLICIES,
            "structure_policy": STRUCTURE_POLICIES,
            "terminology_policy": TERMINOLOGY_POLICIES,
            "anti_fluff_policy": ANTI_FLUFF_POLICIES,
        },
    )


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
    blocks = [
        "Compose a faithful whole-book summary from the provided chapter summaries.",
        _context_block(
            context_mode=stage.context_mode,
            book_title=book_title,
            chapter_title="",
            toc_md=toc_md,
            book_metadata=book_metadata,
        ),
        (
            f"Target visible words: {target_words}. Acceptable range: {low}-{high}. "
            "Remove cross-chapter redundancy and keep the result dense."
        ),
        FORMAT_INSTRUCTIONS[stage.format_mode],
        (
            "Required behavior:\n"
            "- preserve the book's main thesis, frameworks, mechanisms, and caveats\n"
            "- synthesize repeated ideas across chapters instead of repeating them\n"
            "- keep the final result coherent as a standalone whole-book summary\n"
            "- do not mention the instructions, target length, or chapter-by-chapter omissions"
        ),
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
) -> Dict[str, object]:
    """Return a single-model OpenRouter chat request body.

    The benchmark should keep one model per stage and avoid model arrays or
    benchmark-time fallbacks. Provider routing can still be pinned externally.
    """
    request: Dict[str, object] = {
        "model": stage.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": stage.temperature,
        "max_tokens": stage.max_tokens,
    }
    if stage.seed is not None:
        request["seed"] = stage.seed
    if stage.provider_order:
        request["provider"] = {"order": list(stage.provider_order)}
    if use_json_schema:
        schema = dict(SUMMARY_JSON_SCHEMA)
        schema["name"] = schema_name
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": schema,
        }
    if stage.extra_body:
        request["extra_body"] = stage.extra_body
    return request


PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {
    "30m_deepseek-v4-flash_notthinking": CandidateSpec(
        name="30m_deepseek-v4-flash_notthinking_v1",
        profile="30m_deepseek-v4-flash_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 30m, notthinking, schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-flash_thinking": CandidateSpec(
        name="30m_deepseek-v4-flash_thinking_v1",
        profile="30m_deepseek-v4-flash_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 30m, thinking, schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-pro_notthinking": CandidateSpec(
        name="30m_deepseek-v4-pro_notthinking_v1",
        profile="30m_deepseek-v4-pro_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 30m, notthinking, schema=True",
        disable_composer=False
    ),
    "30m_deepseek-v4-pro_thinking": CandidateSpec(
        name="30m_deepseek-v4-pro_thinking_v1",
        profile="30m_deepseek-v4-pro_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 30m, thinking, schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2-flash_notthinking": CandidateSpec(
        name="30m_mimo-v2-flash_notthinking_v1",
        profile="30m_mimo-v2-flash_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 30m, notthinking, schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2-flash_thinking": CandidateSpec(
        name="30m_mimo-v2-flash_thinking_v1",
        profile="30m_mimo-v2-flash_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 30m, thinking, schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2.5-pro_notthinking": CandidateSpec(
        name="30m_mimo-v2.5-pro_notthinking_v1",
        profile="30m_mimo-v2.5-pro_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2.5-pro chapter+composer, 30m, notthinking, schema=True",
        disable_composer=False
    ),
    "30m_mimo-v2.5-pro_thinking": CandidateSpec(
        name="30m_mimo-v2.5-pro_thinking_v1",
        profile="30m_mimo-v2.5-pro_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2.5-pro chapter+composer, 30m, thinking, schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-flash_notthinking": CandidateSpec(
        name="60m_deepseek-v4-flash_notthinking_v1",
        profile="60m_deepseek-v4-flash_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 60m, notthinking, schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-flash_thinking": CandidateSpec(
        name="60m_deepseek-v4-flash_thinking_v1",
        profile="60m_deepseek-v4-flash_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-flash chapter+composer, 60m, thinking, schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-pro_notthinking": CandidateSpec(
        name="60m_deepseek-v4-pro_notthinking_v1",
        profile="60m_deepseek-v4-pro_notthinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 60m, notthinking, schema=True",
        disable_composer=False
    ),
    "60m_deepseek-v4-pro_thinking": CandidateSpec(
        name="60m_deepseek-v4-pro_thinking_v1",
        profile="60m_deepseek-v4-pro_thinking",
        chapter_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="deepseek/deepseek-v4-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: deepseek/deepseek-v4-pro chapter+composer, 60m, thinking, schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2-flash_notthinking": CandidateSpec(
        name="60m_mimo-v2-flash_notthinking_v1",
        profile="60m_mimo-v2-flash_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 60m, notthinking, schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2-flash_thinking": CandidateSpec(
        name="60m_mimo-v2-flash_thinking_v1",
        profile="60m_mimo-v2-flash_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2-flash", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2-flash chapter+composer, 60m, thinking, schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2.5-pro_notthinking": CandidateSpec(
        name="60m_mimo-v2.5-pro_notthinking_v1",
        profile="60m_mimo-v2.5-pro_notthinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "disabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2.5-pro chapter+composer, 60m, notthinking, schema=True",
        disable_composer=False
    ),
    "60m_mimo-v2.5-pro_thinking": CandidateSpec(
        name="60m_mimo-v2.5-pro_thinking_v1",
        profile="60m_mimo-v2.5-pro_thinking",
        chapter_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "dense_faithful", "detail_policy": "mechanisms_first", "qualifier_policy": "strict", "structure_policy": "heading_aware", "example_policy": "explanatory_only", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        composer_stage=StageConfig(model="xiaomi/mimo-v2.5-pro", temperature=0.2, seed=42, max_tokens=8192, format_mode="markdown_sections", context_mode="chapter_plus_toc_and_meta", prompt_components={"system_style": "architectural_synthesizer", "synthesis_policy": "thesis_then_frameworks", "detail_policy": "balanced_dense", "qualifier_policy": "strict", "structure_policy": "theme_clustered", "terminology_policy": "keep_source_terms", "anti_fluff_policy": "hard"}, extra_body={"thinking": {"type": "enabled"}}),
        length_control=LengthControlConfig(
            max_passes=5, tolerance_pct=0.08, hard_tolerance_pct=0.15, repair_strategy="edit_existing"
        ),
        budget_allocator=BudgetAllocatorConfig(
            words_per_minute=200, allocation_alpha=0.9, min_chapter_share=0.03, max_chapter_share=0.18, chapter_stage_multiplier_30m=1.2, chapter_stage_multiplier_60m=1.0, max_summary_to_source_ratio=0.9
        ),
        use_json_schema=True,
        json_schema_name="summary_response",
        notes="Auto-generated: xiaomi/mimo-v2.5-pro chapter+composer, 60m, thinking, schema=True",
        disable_composer=False
    )

}
def get_candidate(profile: Profile) -> CandidateSpec:
    if profile not in PROFILE_CANDIDATES:
        raise KeyError(f"Unknown profile: {profile}")
    return PROFILE_CANDIDATES[profile]

def get_all_profiles() -> list[Profile]:
    return list(PROFILE_CANDIDATES.keys())

def get_profiles_by_time(time_budget: str) -> list[Profile]:
    """Return profiles matching the given time budget ('30m', '60m', or 'all')."""
    if time_budget == "all":
        return get_all_profiles()
    return [p for p in PROFILE_CANDIDATES if p.startswith(f"{time_budget}_")]
