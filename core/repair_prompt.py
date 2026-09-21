"""Transport-agnostic repair-prompt rendering.

Single source of truth for the length-control *repair* prompt text. Both the
autoresearch harness (``candidate_spec`` / ``core.run_candidate``) and the Cloud
Function render the same templates through this module, so a version baked into
the prompt registry produces byte-identical repair prompts to a local run.

The templates are plain ``{{placeholder}}`` strings rendered by
``core.prompt_template``. Blocks that render empty are dropped, which is how the
optional lines (book/chapter title, numeric delta, TOC, metadata, excerpts) stay
conditional without a template-language ``if``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Tuple

try:  # pragma: no cover - hosts without core on sys.path
    from core import prompt_template as _prompt_template
except ImportError:  # pragma: no cover
    _prompt_template = None

DEFAULT_TOLERANCE_PCT = 0.05

# Fallback policy text when a version carries no repair policy of its own.
DEFAULT_REPAIR_MORE_POLICY = "Expand missing detail."
DEFAULT_REPAIR_LESS_POLICY = "Shorten by removing repetition."


def visible_word_range(target_words: int, tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> Tuple[int, int]:
    """Return the accepted (low, high) visible-word band around a target."""
    delta = max(1, int(round(target_words * tolerance_pct)))
    return max(1, target_words - delta), target_words + delta


def sentence_budget(target_words: int, words_per_sentence: int, scale: float = 1.0) -> int:
    """Number of sentences that yields ``target_words`` at ``words_per_sentence``."""
    wps = words_per_sentence if words_per_sentence and words_per_sentence > 0 else 1
    budget = max(1, int(round(max(1, target_words) / wps)))
    if scale and scale != 1.0:
        budget = max(1, int(round(budget * scale)))
    return budget


def repair_numeric_delta(
    *,
    direction: str,
    target_words: int,
    current_word_count: int,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    repair_target_mode: str = "target",
    repair_min_reduction_pct: float = 0.0,
    repair_sentence_delete: bool = False,
    sentence_budget_note: Optional[str] = None,
) -> str:
    """The measured current count + exact cut/add instruction for a repair pass."""
    low, high = visible_word_range(target_words, tolerance_pct)
    if direction == "less":
        ceiling = low if repair_target_mode == "low" else high
        gap = max(0, current_word_count - ceiling)
        min_cut = (
            math.ceil(current_word_count * repair_min_reduction_pct)
            if repair_min_reduction_pct > 0
            else 0
        )
        cut = max(gap, min_cut)
        landing = (
            f"land at or below {low}"
            if repair_target_mode == "low"
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
    if sentence_budget_note:
        numeric_delta += sentence_budget_note
    if direction == "less" and repair_sentence_delete:
        numeric_delta += (
            " Prefer deleting whole sentences, lowest-value first, over rewording; do not add new "
            "sentences."
        )
    return numeric_delta


def render_blocks(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``values`` into ``template`` and drop empty blocks."""
    if _prompt_template is None:
        return template
    rendered = _prompt_template.render(template, values)
    blocks = [block.strip() for block in rendered.split("\n\n")]
    return "\n\n".join(block for block in blocks if block)


def render_repair_prompt(
    template: str,
    *,
    direction: str,
    target_words: int,
    current_summary_md: str,
    source_md: str = "",
    current_word_count: Optional[int] = None,
    book_title: str = "",
    chapter_title: str = "",
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    repair_target_mode: str = "target",
    repair_min_reduction_pct: float = 0.0,
    repair_sentence_delete: bool = False,
    sentence_budget_note: Optional[str] = None,
    extra_values: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render a repair template with the measured delta and request values."""
    low, high = visible_word_range(target_words, tolerance_pct)
    numeric_delta = ""
    if current_word_count is not None:
        numeric_delta = repair_numeric_delta(
            direction=direction,
            target_words=target_words,
            current_word_count=current_word_count,
            tolerance_pct=tolerance_pct,
            repair_target_mode=repair_target_mode,
            repair_min_reduction_pct=repair_min_reduction_pct,
            repair_sentence_delete=repair_sentence_delete,
            sentence_budget_note=sentence_budget_note,
        )
    values: dict = {
        "direction": direction,
        "target_words": target_words,
        "low": low,
        "high": high,
        "numeric_delta": numeric_delta,
        "current_summary_md": current_summary_md.strip(),
        "source_md": source_md.strip(),
        "book_title_line": f"Book title: {book_title}" if book_title else "",
        "chapter_title_line": f"Chapter title: {chapter_title}" if chapter_title else "",
    }
    if extra_values:
        values.update(dict(extra_values))
    return render_blocks(template, values)


def chapter_repair_template(*, policy: str, format_instructions: str) -> str:
    """Chapter repair template with policy + format baked in at build time."""
    return "\n\n".join(
        [
            "Revise the existing chapter summary to hit the target length more accurately.",
            "{{book_title_line}}",
            "{{chapter_title_line}}",
            (
                "Target visible words: {{target_words}}. Acceptable range: {{low}}-{{high}}. "
                "This repair direction is: {{direction}}."
            ),
            "{{numeric_delta}}",
            policy,
            format_instructions,
            "Keep the result faithful to the source.",
            "Current summary:\n{{current_summary_md}}",
            "Source chapter markdown:\n{{source_md}}",
        ]
    )


def composer_repair_template(*, policy: str, format_instructions: str) -> str:
    """Composer (whole-book) repair template with policy + format baked in."""
    return "\n\n".join(
        [
            "Revise the whole-book summary to hit the target length more accurately.",
            "{{book_title_line}}",
            (
                "Target visible words: {{target_words}}. Acceptable range: {{low}}-{{high}}. "
                "This repair direction is: {{direction}}."
            ),
            "{{numeric_delta}}",
            policy,
            format_instructions,
            "Keep the result faithful to the chapter summaries and any retrieved source excerpts.",
            "Current whole-book summary:\n{{current_summary_md}}",
            "{{toc_line}}",
            "{{metadata_line}}",
            "Chapter summaries:\n{{chapter_summaries_md}}",
            "{{excerpts_line}}",
        ]
    )
