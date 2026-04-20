"""LLM judge helpers for source-grounded nonfiction summary evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Dict, Mapping, Optional

from core.openrouter_client import GenerationResult, OpenRouterClient
from scoring import JudgeScores, PAIRWISE_JUDGE_JSON_SCHEMA, PairwiseJudgeResult, Rubric

ABSOLUTE_JUDGE_JSON_SCHEMA: Dict[str, object] = {
    "name": "absolute_summary_judge",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "faithfulness": {"type": "number", "minimum": 0, "maximum": 1},
            "concept_coverage": {"type": "number", "minimum": 0, "maximum": 1},
            "qualifier_preservation": {"type": "number", "minimum": 0, "maximum": 1},
            "no_fluff": {"type": "number", "minimum": 0, "maximum": 1},
            "structure_quality": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
        "required": [
            "faithfulness",
            "concept_coverage",
            "qualifier_preservation",
            "no_fluff",
            "structure_quality",
            "rationale",
        ],
        "additionalProperties": False,
    },
}

DEFAULT_ABSOLUTE_JUDGE_SYSTEM = (
    "You are a strict evaluator of source-grounded nonfiction summaries. "
    "Score only what is supported by the provided source and rubric. "
    "Do not reward elegance if it hides omissions or changes the author's meaning. "
    "Use the full 0 to 1 range."
)

DEFAULT_PAIRWISE_JUDGE_SYSTEM = (
    "You compare two source-grounded nonfiction summaries. "
    "Prefer the summary that better preserves concepts, explanations, qualifiers, and "
    "information density without adding unsupported content. "
    "Ignore style preferences unless they affect clarity or fidelity."
)


@dataclass(frozen=True)
class AbsoluteJudgeResult:
    scores: JudgeScores
    rationale: str
    raw_response: Mapping[str, object]


@dataclass(frozen=True)
class PairwiseResultWithRationale:
    result: PairwiseJudgeResult
    rationale: str
    raw_response: Mapping[str, object]


def rubric_to_markdown(rubric: Rubric) -> str:
    def render_section(title: str, items: tuple[str, ...]) -> str:
        if not items:
            return f"### {title}\n- none provided"
        bullets = "\n".join(f"- {item}" for item in items)
        return f"### {title}\n{bullets}"

    return "\n\n".join(
        [
            render_section("Headings", rubric.headings),
            render_section("Core concepts", rubric.core_concepts),
            render_section("Mechanisms or explanations", rubric.mechanisms_or_explanations),
            render_section("Critical qualifiers", rubric.critical_qualifiers),
            render_section("Important examples", rubric.important_examples),
            render_section("Key entities or numbers", rubric.key_entities_or_numbers),
            render_section("Key terms", rubric.key_terms),
        ]
    )


def build_absolute_judge_request(
    *,
    judge_model: str,
    summary_md: str,
    rubric: Rubric,
    source_md: str = "",
    system_prompt: str = DEFAULT_ABSOLUTE_JUDGE_SYSTEM,
    max_source_chars: int = 24000,
    max_summary_chars: int = 24000,
    seed: Optional[int] = 42,
) -> Dict[str, object]:
    source_block = source_md.strip()
    if len(source_block) > max_source_chars:
        source_block = source_block[:max_source_chars] + "\n\n[truncated]"
    summary_block = summary_md.strip()
    if len(summary_block) > max_summary_chars:
        summary_block = summary_block[:max_summary_chars] + "\n\n[truncated]"

    user_prompt = "\n\n".join(
        [
            "Evaluate this nonfiction summary against the source-derived rubric.",
            "Scoring rubric:\n"
            "- faithfulness: factual and interpretive accuracy relative to source or rubric\n"
            "- concept_coverage: whether the important concepts and explanations are preserved\n"
            "- qualifier_preservation: whether caveats, exceptions, and scope conditions survive\n"
            "- no_fluff: whether the summary avoids filler and low-value repetition\n"
            "- structure_quality: whether the summary is clear, coherent, and easy to scan",
            "Return JSON only.",
            "Source-derived rubric:\n" + rubric_to_markdown(rubric),
            f"Source excerpt or source text:\n{source_block}" if source_block else "",
            "Candidate summary:\n" + summary_block,
        ]
    )
    request: Dict[str, object] = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": ABSOLUTE_JUDGE_JSON_SCHEMA,
        },
    }
    if seed is not None:
        request["seed"] = seed
    return request


def parse_absolute_judge_result(result: GenerationResult) -> AbsoluteJudgeResult:
    payload = json.loads(result.raw_content)
    scores = JudgeScores(
        faithfulness=float(payload["faithfulness"]),
        concept_coverage=float(payload["concept_coverage"]),
        qualifier_preservation=float(payload["qualifier_preservation"]),
        no_fluff=float(payload["no_fluff"]),
        structure_quality=float(payload["structure_quality"]),
    ).clamped()
    rationale = str(payload.get("rationale", "")).strip()
    return AbsoluteJudgeResult(scores=scores, rationale=rationale, raw_response=result.raw_response)


def judge_summary_absolute(
    client: OpenRouterClient,
    *,
    judge_model: str,
    summary_md: str,
    rubric: Rubric,
    source_md: str = "",
    seed: Optional[int] = 42,
) -> AbsoluteJudgeResult:
    request = build_absolute_judge_request(
        judge_model=judge_model,
        summary_md=summary_md,
        rubric=rubric,
        source_md=source_md,
        seed=seed,
    )
    response = client.chat_completion(request)
    return parse_absolute_judge_result(response)


def build_pairwise_judge_request(
    *,
    judge_model: str,
    summary_a_md: str,
    summary_b_md: str,
    rubric: Rubric,
    source_md: str = "",
    system_prompt: str = DEFAULT_PAIRWISE_JUDGE_SYSTEM,
    swap_order: bool = False,
    max_source_chars: int = 24000,
    max_summary_chars: int = 20000,
    seed: Optional[int] = 42,
) -> Dict[str, object]:
    left_label, right_label = ("A", "B") if not swap_order else ("B", "A")
    summary_left = summary_a_md if not swap_order else summary_b_md
    summary_right = summary_b_md if not swap_order else summary_a_md

    source_block = source_md.strip()
    if len(source_block) > max_source_chars:
        source_block = source_block[:max_source_chars] + "\n\n[truncated]"

    def clamp_summary(text: str) -> str:
        text = text.strip()
        if len(text) > max_summary_chars:
            return text[:max_summary_chars] + "\n\n[truncated]"
        return text

    user_prompt = "\n\n".join(
        [
            "Compare two source-grounded nonfiction summaries.",
            "Judge them on concept coverage, explanation fidelity, qualifier preservation, no fluff, and structure.",
            "Prefer the one that better preserves the source's actual meaning under the same target budget.",
            "Return JSON only.",
            "Source-derived rubric:\n" + rubric_to_markdown(rubric),
            f"Source excerpt or source text:\n{source_block}" if source_block else "",
            f"Summary {left_label}:\n{clamp_summary(summary_left)}",
            f"Summary {right_label}:\n{clamp_summary(summary_right)}",
        ]
    )
    request: Dict[str, object] = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": PAIRWISE_JUDGE_JSON_SCHEMA,
        },
    }
    if seed is not None:
        request["seed"] = seed
    return request


def parse_pairwise_judge_result(result: GenerationResult, *, swap_order: bool = False) -> PairwiseResultWithRationale:
    payload = json.loads(result.raw_content)
    winner = str(payload["winner"])
    if swap_order:
        if winner == "A":
            winner = "B"
        elif winner == "B":
            winner = "A"

    a_scores = payload["a_scores"]
    b_scores = payload["b_scores"]
    if swap_order:
        a_scores, b_scores = b_scores, a_scores

    pairwise = PairwiseJudgeResult(
        winner=winner,
        a_scores=JudgeScores(
            faithfulness=float(a_scores["faithfulness"]),
            concept_coverage=float(a_scores["concept_coverage"]),
            qualifier_preservation=float(a_scores["qualifier_preservation"]),
            no_fluff=float(a_scores["no_fluff"]),
            structure_quality=float(a_scores["structure_quality"]),
        ).clamped(),
        b_scores=JudgeScores(
            faithfulness=float(b_scores["faithfulness"]),
            concept_coverage=float(b_scores["concept_coverage"]),
            qualifier_preservation=float(b_scores["qualifier_preservation"]),
            no_fluff=float(b_scores["no_fluff"]),
            structure_quality=float(b_scores["structure_quality"]),
        ).clamped(),
    )
    return PairwiseResultWithRationale(
        result=pairwise,
        rationale=str(payload.get("rationale", "")).strip(),
        raw_response=result.raw_response,
    )
