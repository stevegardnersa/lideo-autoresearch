from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Dict, Optional

from core.judge import judge_summary_absolute
from core.openrouter_client import GenerationResult, OpenRouterClient
from scoring import (
    DEFAULT_SCORING_CONFIG,
    Rubric,
    JudgeScores,
    SummarySample,
    deterministic_metrics,
    score_sample,
    visible_word_count,
)


def _build_openrouter_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    use_json_schema: bool = True,
    thinking: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    seed: int = 42,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "seed": seed,
        "extra_body": {
            "thinking": {"type": "enabled" if thinking else "disabled"},
        },
    }
    if use_json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "summary_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "summary_md": {"type": "string"},
                        "estimated_visible_words": {
                            "type": "integer",
                            "description": "visible word count estimate",
                            "minimum": 0,
                        },
                    },
                    "required": ["summary_md", "estimated_visible_words"],
                    "additionalProperties": False,
                },
            },
        }
    return payload


def _build_client(
    api_key: str = "",
    base_url: str = "",
    timeout: int = 600,
) -> OpenRouterClient:
    resolved_base = base_url or "https://openrouter.ai/api/v1"
    if api_key:
        return OpenRouterClient(
            api_key=api_key,
            base_url=resolved_base,
            timeout=timeout,
            max_retries=3,
        )
    return OpenRouterClient.from_env(
        base_url=resolved_base,
        timeout=timeout,
        max_retries=3,
    )


def _parse_rubric(raw: Optional[Dict[str, Any]]) -> Optional[Rubric]:
    if not raw:
        return None
    keys = [
        "headings", "core_concepts", "mechanisms_or_explanations",
        "critical_qualifiers", "important_examples",
        "key_entities_or_numbers", "key_terms",
    ]
    return Rubric(
        **{k: tuple(raw.get(k, ()) or ()) for k in keys},
    )


def _usage_dict(gen: GenerationResult) -> Dict[str, Any]:
    u = gen.usage
    return {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.total_tokens,
        "reasoning_tokens": u.reasoning_tokens,
        "cached_prompt_tokens": u.cached_prompt_tokens,
        "generation_cost": u.generation_cost,
        "uncached_generation_cost": u.uncached_generation_cost,
        "generation_id": u.generation_id,
        "provider_name": u.provider_name,
        "model_id": u.model_id,
    }


def _scoring_dict(sample: SummarySample) -> Dict[str, Any]:
    scored = score_sample(sample, config=DEFAULT_SCORING_CONFIG)
    return {
        "hard_fail": scored.hard_fail,
        "hard_fail_reasons": list(scored.hard_fail_reasons),
        "deterministic": asdict(scored.deterministic),
        "resolved_faithfulness": scored.resolved_faithfulness,
        "resolved_concept_coverage": scored.resolved_concept_coverage,
        "resolved_qualifier_preservation": scored.resolved_qualifier_preservation,
        "resolved_no_fluff": scored.resolved_no_fluff,
        "resolved_structure_quality": scored.resolved_structure_quality,
        "quality": scored.quality,
        "utility": scored.utility,
    }


def _first_pass_summary_md(gen: GenerationResult, use_json: bool) -> str:
    if use_json and gen.parsed_json:
        return str(gen.parsed_json.get("summary_md") or gen.summary_md)
    return gen.summary_md


def run_summarize(body: Dict[str, Any]) -> Dict[str, Any]:
    source_md = str(body["source_md"]).strip()
    model = str(body["model"]).strip()
    system_prompt = str(body["system_prompt"]).strip()
    user_prompt = str(body["user_prompt"]).strip()
    api_key = str(body.get("api_key", "")).strip()
    base_url = str(body.get("base_url", "")).strip()
    thinking = bool(body.get("thinking", False))
    use_json = bool(body.get("use_json_schema", True))
    target_words = int(body["target_words"]) if body.get("target_words") else 0
    do_judge = bool(body.get("judge", False))
    judge_model = str(body.get("judge_model", "openai/gpt-4o-mini")).strip()
    rubric_raw = body.get("rubric")

    rubric = _parse_rubric(rubric_raw)

    client = _build_client(api_key=api_key, base_url=base_url)

    payload = _build_openrouter_payload(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        use_json_schema=use_json,
        thinking=thinking,
    )

    gen: GenerationResult = client.chat_completion(payload)

    summary_md = gen.summary_md.strip()
    estimated_words = gen.estimated_visible_words
    if use_json and gen.parsed_json:
        summary_md = str(gen.parsed_json.get("summary_md") or summary_md).strip()
        estimated_words = int(gen.parsed_json.get("estimated_visible_words") or estimated_words or 0)

    if not estimated_words:
        estimated_words = visible_word_count(summary_md)

    result: Dict[str, Any] = {
        "success": True,
        "summary": {
            "summary_md": summary_md,
            "estimated_visible_words": estimated_words,
        },
        "usage": _usage_dict(gen),
    }

    scoring_result: Optional[Dict[str, Any]] = None
    judge_scores: Optional[Dict[str, Any]] = None
    judge_error: Optional[str] = None

    if rubric is not None and target_words > 0:
        first_pass = _first_pass_summary_md(gen, use_json)
        sample = SummarySample(
            sample_id="cf_chapter",
            level="chapter",
            target_words=target_words,
            summary_md=summary_md,
            source_md=source_md,
            group_id="cloud_function",
            first_pass_summary_md=first_pass,
            passes_used=1,
            generation_cost=gen.usage.generation_cost,
            uncached_generation_cost=gen.usage.uncached_generation_cost,
            malformed=False,
            rubric=rubric,
            judge_scores=None,
        )

        if do_judge:
            try:
                judge_result = judge_summary_absolute(
                    client,
                    judge_model=judge_model,
                    summary_md=summary_md,
                    rubric=rubric,
                    source_md=source_md,
                )
                judge_scores = {
                    "faithfulness": judge_result.scores.faithfulness,
                    "concept_coverage": judge_result.scores.concept_coverage,
                    "qualifier_preservation": judge_result.scores.qualifier_preservation,
                    "no_fluff": judge_result.scores.no_fluff,
                    "structure_quality": judge_result.scores.structure_quality,
                    "rationale": judge_result.rationale,
                }
                sample = SummarySample(
                    sample_id=sample.sample_id,
                    level=sample.level,
                    target_words=sample.target_words,
                    summary_md=sample.summary_md,
                    source_md=sample.source_md,
                    group_id=sample.group_id,
                    first_pass_summary_md=sample.first_pass_summary_md,
                    passes_used=sample.passes_used,
                    generation_cost=sample.generation_cost,
                    uncached_generation_cost=sample.uncached_generation_cost,
                    malformed=sample.malformed,
                    rubric=sample.rubric,
                    judge_scores=judge_result.scores,
                )
            except Exception as exc:
                judge_error = str(exc)
                print(f"Judge error: {exc}", flush=True)

        scoring_result = _scoring_dict(sample)

    result["scoring"] = scoring_result
    if judge_scores is not None:
        result["judge_scores"] = judge_scores
    if judge_error is not None:
        result["judge_error"] = judge_error

    return result


def build_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return result
