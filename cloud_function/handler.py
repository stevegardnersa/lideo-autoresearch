from __future__ import annotations

from dataclasses import asdict
import hashlib
from datetime import datetime, timezone
import math
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from core.judge import judge_summary_absolute
from core.length_control import LengthControlConfig, run_length_control
from core.openrouter_client import GenerationResult, OpenRouterClient
from core.reasoning import visible_max_tokens
from core import prompt_registry, prompt_template, repair_prompt
from cloud_function import storage
from scoring import (
    DEFAULT_SCORING_CONFIG,
    Rubric,
    JudgeScores,
    SummarySample,
    score_sample,
    visible_word_count,
)

ROOT = Path(__file__).resolve().parents[1]
CF_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "registry.json"

# Fallback model used when neither the request nor the resolved version bakes
# one. Mirrors the app-side default; the deployed registry bakes it.
FALLBACK_MODEL = os.environ.get("CF_DEFAULT_MODEL", "").strip() or "deepseek/deepseek-v4-flash"
FALLBACK_SENTENCE_WORDS = 13
TARGET_WORDS_TOLERANCE = 0.05


class PromptVersionError(RuntimeError):
    """Raised when a request names a prompt version the CF cannot resolve."""

    def __init__(self, message: str, *, error: str = "unknown_prompt_version",
                 status_code: int = 422, allowed: Optional[list] = None):
        super().__init__(message)
        self.error = error
        self.status_code = status_code
        self.allowed = list(allowed or [])


def _load_cf_registry() -> Dict[str, Any]:
    path = os.environ.get("PROMPT_REGISTRY_PATH", "").strip()
    registry_file = Path(path) if path else CF_REGISTRY_PATH
    return prompt_registry.load_registry(registry_file, seed=False)


def _resolve_prompt(body: Mapping[str, Any], registry: Mapping[str, Any]):
    """Resolve the system prompt for a request (spec §7.2). Returns (text, meta)."""
    stage = str(body.get("prompt_stage") or "chapter").strip() or "chapter"
    if stage not in ("chapter", "composer"):
        raise PromptVersionError(f"Unknown prompt_stage: {stage!r}", error="invalid_prompt_stage", status_code=400)

    incoming = str(body.get("system_prompt") or "").strip()
    override_text = str(body.get("system_prompt_override") or "").strip()
    allow = bool(body.get("allow_prompt_override", False))
    version_id = str(body.get("prompt_version") or "").strip()
    client_fp = str(body.get("prompt_fingerprint") or "").strip()
    registry_sha = str(registry.get("registry_sha") or "")
    versions = list(registry.get("versions") or [])

    def _meta(source: str, text: str, vid: str, warning: str = "") -> Dict[str, str]:
        return {
            "prompt_registry_sha": registry_sha,
            "prompt_version": vid,
            "prompt_fingerprint": prompt_registry.fingerprint(text) if text else "",
            "prompt_source": source,
            "prompt_warning": warning,
        }

    # 1. Explicit inline override.
    if override_text and allow:
        return override_text, _meta("inline_override", override_text, version_id)

    if version_id:
        version = next((v for v in versions if v.get("id") == version_id), None)
        # 2. Known version in the bundled registry.
        if version is not None:
            text, fp = prompt_registry.version_text(version)
            if not text:
                text = incoming
                fp = prompt_registry.fingerprint(text) if text else ""
            warning = "fingerprint_mismatch" if (client_fp and fp and client_fp != fp) else ""
            meta = _meta("registry", text, version_id, warning)
            meta["prompt_fingerprint"] = fp
            return text, meta
        # 3. Unknown version: inline override if permitted, else 422.
        if allow and incoming:
            meta = _meta("inline_override", incoming, version_id, "unknown_version")
            return incoming, meta
        allowed = [v.get("id") for v in versions if v.get("stage") == stage]
        raise PromptVersionError(
            f"unknown_prompt_version: {version_id!r}",
            error="unknown_prompt_version",
            status_code=422,
            allowed=allowed,
        )

    # 4. No version: incoming prompt, else the bundled production default.
    if incoming:
        return incoming, _meta("request", incoming, "")

    stage_slots = (registry.get("slots") or {}).get(stage) or {}
    model = str(body.get("model") or "").strip()
    production_id = None
    if model:
        production_id = ((stage_slots.get("models") or {}).get(model) or {}).get("production")
    if not production_id:
        production_id = (stage_slots.get("general") or {}).get("production")
    production = next((v for v in versions if v.get("id") == production_id), None)
    if production is not None:
        text, fp = prompt_registry.version_text(production)
        if text:
            meta = _meta("production_default", text, str(production_id))
            meta["prompt_fingerprint"] = fp
            return text, meta

    raise PromptVersionError(
        "No system prompt available for request",
        error="missing_system_prompt",
        status_code=422,
    )


def _resolve_bundle_record(body: Mapping[str, Any], registry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the effective version record for a request (None when none).

    Mirrors ``_resolve_prompt``'s routing so ``run_summarize`` can bake the
    version's model / reasoning / user_template without duplicating rules.
    """
    stage = str(body.get("prompt_stage") or "chapter").strip() or "chapter"
    version_id = str(body.get("prompt_version") or "").strip()
    versions = {v.get("id"): v for v in registry.get("versions") or []}
    if version_id:
        return versions.get(version_id) or None
    incoming = str(body.get("system_prompt") or "").strip()
    if incoming:
        return None
    stage_slots = (registry.get("slots") or {}).get(stage) or {}
    model = str(body.get("model") or "").strip()
    production_id = None
    if model:
        production_id = ((stage_slots.get("models") or {}).get(model) or {}).get("production")
    if not production_id:
        production_id = (stage_slots.get("general") or {}).get("production")
    return versions.get(production_id) or None


def _sentence_words(version: Optional[Mapping[str, Any]]) -> int:
    """Words per sentence the version's components target (default 13)."""
    if version is None:
        return FALLBACK_SENTENCE_WORDS
    components = version.get("components") or {}
    for key in ("target_sentence_words", "sentence_target_words"):
        try:
            value = int(str(components.get(key) or "").strip())
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return FALLBACK_SENTENCE_WORDS


def _word_range(target_words: int, tolerance: float = TARGET_WORDS_TOLERANCE) -> Tuple[int, int]:
    """Return (low, high) word-count band around the target."""
    if target_words <= 0:
        return 0, 0
    low = max(1, int(target_words * (1 - tolerance)))
    high = math.ceil(target_words * (1 + tolerance))
    return low, high


def _sentence_budget(target_words: int, words_per_sentence: int) -> int:
    """Number of sentences that yields the target word count."""
    if target_words <= 0 or words_per_sentence <= 0:
        return 0
    return max(1, round(target_words / words_per_sentence))


def _render_user_prompt(version: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    """Render a version's bundled user_template with request values.

    Falls back to the legacy inline ``user_prompt`` when the template is
    absent or empty.
    """
    template = str(version.get("user_template") or "").strip()
    if not template:
        return ""
    source_md = str(body.get("source_md") or body.get("chapter") or "").strip()
    chapter_title = str(body.get("chapter_title") or body.get("title") or "").strip()
    target_words_raw = body.get("target_words")
    if target_words_raw in (None, ""):
        target_words_raw = body.get("words")
    try:
        target_words = int(str(target_words_raw or "").strip() or 0)
    except (TypeError, ValueError):
        target_words = 0
    low, high = _word_range(target_words)
    values: Dict[str, Any] = {
        "chapter_title": chapter_title,
        "book_title": str(body.get("book_title") or ""),
        "toc_md": str(body.get("toc_md") or ""),
        "book_metadata": str(body.get("book_metadata") or ""),
        "source_md": source_md,
        "target_words": str(target_words),
        "low": str(low),
        "high": str(high),
        "s_target": str(_sentence_budget(target_words, _sentence_words(version))),
        "context_block": str(body.get("context_block") or ""),
        "format_instructions": str(body.get("format_instructions") or ""),
        "required_behavior": str(body.get("required_behavior") or ""),
    }
    try:
        return prompt_template.render(template, values)
    except prompt_template.MissingValue as exc:
        raise PromptVersionError(
            f"missing values for bundled user_template placeholders: {', '.join(exc.names)}",
            error="missing_user_prompt_values",
            status_code=422,
        )


def _build_openrouter_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    use_json_schema: bool = True,
    thinking: Optional[bool] = None,
    reasoning: Optional[Mapping[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
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
    }
    # Emit OpenRouter-native reasoning controls only. The legacy litellm
    # ``extra_body={"thinking": {...}}`` wrapper is silently ignored by
    # OpenRouter, which would leave reasoning at provider defaults; translate
    # it to the unified ``reasoning`` object instead. Mirrors
    # core/reasoning.py so the direct and Cloud Function paths agree.
    if reasoning:
        reasoning_norm = dict(reasoning)
        if str(reasoning_norm.get("effort") or "").strip().lower() == "none":
            reasoning_norm = {"enabled": False}
        payload["reasoning"] = reasoning_norm
    elif reasoning_effort is not None:
        effort = str(reasoning_effort).strip()
        if effort.lower() == "none":
            payload["reasoning"] = {"enabled": False}
        else:
            payload["reasoning"] = {"effort": effort}
    elif thinking is not None:
        # Legacy ``thinking`` flag, derived by cf_client from extra_body.thinking.
        payload["reasoning"] = {"enabled": bool(thinking)}
    # else: plain request — no reasoning field, provider default applies.
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


ENV_API_KEY_NAMES = ("LLM_API_KEY",)


def _resolve_api_key_env() -> str:
    for name in ENV_API_KEY_NAMES:
        if os.environ.get(name, "").strip():
            return name
    return ""


def _build_client(
    api_key: str = "",
    base_url: str = "",
    timeout: int = 600,
) -> OpenRouterClient:
    resolved_base = (
        base_url
        or os.environ.get("LLM_BASE_URL", "").strip()
        or "https://openrouter.ai/api/v1"
    )
    if api_key:
        return OpenRouterClient(
            api_key=api_key,
            base_url=resolved_base,
            timeout=timeout,
            max_retries=3,
        )
    env_name = _resolve_api_key_env()
    if not env_name:
        raise ValueError(
            "No API key available: provide 'api_key' in the request body or set "
            f"one of {', '.join(ENV_API_KEY_NAMES)}"
        )
    return OpenRouterClient.from_env(
        api_key_env=env_name,
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


def _zero_usage_dict(model: str) -> Dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_prompt_tokens": 0,
        "generation_cost": 0.0,
        "uncached_generation_cost": 0.0,
        "generation_id": "",
        "provider_name": "",
        "model_id": model,
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


def _raw_usage_cost(raw_response: Mapping[str, object]) -> float:
    try:
        usage = raw_response.get("usage") or {}
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _scoring_version() -> str:
    try:
        return hashlib.sha256((ROOT / "scoring.py").read_bytes()).hexdigest()
    except OSError:
        return ""


def _handler_version() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


def _deployed_at_utc() -> str:
    try:
        mtime = Path(__file__).stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        return datetime.now(timezone.utc).isoformat()


def _meta_block(prompt_meta: Optional[Mapping[str, str]] = None, registry: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    meta = {
        "scoring_version": _scoring_version(),
        "handler_version": _handler_version(),
        "deployed_at_utc": _deployed_at_utc(),
    }
    if registry is not None:
        meta["prompt_registry_sha"] = str(registry.get("registry_sha") or "")
    if prompt_meta:
        meta.update({
            "prompt_version": str(prompt_meta.get("prompt_version") or ""),
            "prompt_fingerprint": str(prompt_meta.get("prompt_fingerprint") or ""),
            "prompt_source": str(prompt_meta.get("prompt_source") or ""),
            "prompt_warning": str(prompt_meta.get("prompt_warning") or ""),
        })
        if prompt_meta.get("model"):
            meta["model"] = str(prompt_meta.get("model"))
    return meta


def _judge_scores_dict(judge_result) -> Dict[str, Any]:
    return {
        "faithfulness": judge_result.scores.faithfulness,
        "concept_coverage": judge_result.scores.concept_coverage,
        "qualifier_preservation": judge_result.scores.qualifier_preservation,
        "no_fluff": judge_result.scores.no_fluff,
        "structure_quality": judge_result.scores.structure_quality,
        "rationale": judge_result.rationale,
    }


def _positive_int(raw: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float(raw: Any, default: float = 1.0) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _effective_reasoning_effort(
    reasoning: Optional[Mapping[str, Any]], reasoning_effort: Optional[str]
) -> Optional[str]:
    """Best-effort reasoning-effort label for the target-aware token cap."""
    if isinstance(reasoning, Mapping):
        effort = str(reasoning.get("effort") or "").strip()
        if effort and effort.lower() != "none":
            return effort
        if reasoning.get("enabled") is False:
            return None
    if reasoning_effort is not None:
        effort = str(reasoning_effort).strip()
        if effort and effort.lower() != "none":
            return effort
    return None


def _target_aware_max_tokens(
    target_words: int,
    reasoning: Optional[Mapping[str, Any]],
    reasoning_effort: Optional[str],
) -> int:
    """Derive a per-pass max_tokens from the visible-word target.

    Mirrors `core.run_candidate`: the reasoning-aware cap, floored so a short
    sentence budget cannot starve the JSON payload, never above the CF budget.
    """
    cap = int(os.environ.get("CF_MAX_TOKENS", "").strip() or 8192)
    base = visible_max_tokens(
        target_words,
        effort=_effective_reasoning_effort(reasoning, reasoning_effort),
        cap=cap,
    )
    floor = max(1, int(round(target_words * 1.35 * 1.15)))
    return min(cap, max(base, floor))


def _resolve_length_control(
    version: Optional[Mapping[str, Any]], body: Mapping[str, Any]
) -> LengthControlConfig:
    """Loop config: request override > baked version > built-in defaults."""
    merged: Dict[str, Any] = dict((version or {}).get("length_control") or {})
    override = body.get("length_control")
    if isinstance(override, Mapping):
        merged.update(dict(override))
    return LengthControlConfig.from_mapping(merged)


def _resolve_repair_templates(
    version: Optional[Mapping[str, Any]], stage: str
) -> Dict[str, str]:
    """Baked repair templates for the version's stage (may be empty)."""
    templates = (version or {}).get("repair_templates")
    if not isinstance(templates, Mapping):
        return {}
    return {str(k): str(v) for k, v in templates.items() if str(v).strip()}


def _fallback_repair_template(stage: str, direction: str, length_control: LengthControlConfig) -> str:
    """Repair template when a version carries no baked one (older versions)."""
    policy = (
        repair_prompt.DEFAULT_REPAIR_MORE_POLICY
        if direction == "more"
        else repair_prompt.DEFAULT_REPAIR_LESS_POLICY
    )
    build = repair_prompt.composer_repair_template if stage == "composer" else repair_prompt.chapter_repair_template
    return build(policy=policy, format_instructions="Return markdown only.")


def _sentence_budget_note(
    version: Optional[Mapping[str, Any]], target_words: int
) -> Optional[str]:
    """Sentence-budget addendum for the repair delta (None when not applicable)."""
    components = (version or {}).get("components") or {}
    if not components.get("length_policy"):
        return None
    words_per_sentence = (
        _positive_int(components.get("sentence_target_words"))
        or _positive_int(components.get("target_sentence_words"))
        or FALLBACK_SENTENCE_WORDS
    )
    scale = _positive_float(components.get("sentence_budget_scale"), 1.0)
    budget = repair_prompt.sentence_budget(target_words, words_per_sentence, scale)
    return f" The sentence budget is {budget}; adjust sentence count as well as word count."


def _empty_token_totals() -> Dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_prompt_tokens": 0,
    }


def _run_auto_length(
    *,
    client: OpenRouterClient,
    model: str,
    system_prompt: str,
    user_prompt: str,
    source_md: str,
    target_words: int,
    use_json: bool,
    thinking: Optional[bool],
    reasoning: Optional[Mapping[str, Any]],
    reasoning_effort: Optional[str],
    resolved_version: Optional[Mapping[str, Any]],
    stage: str,
    body: Mapping[str, Any],
    book_title: str,
    chapter_title: str,
) -> Tuple[str, int, str, Dict[str, Any], float, float, int]:
    """Run the generate -> measure -> repair loop and return the summary.

    Returns ``(summary_md, estimated_words, first_pass_summary_md, usage,
    generation_cost, uncached_generation_cost, passes_used)``.
    """
    length_control = _resolve_length_control(resolved_version, body)
    repair_templates = _resolve_repair_templates(resolved_version, stage)
    sentence_budget_note = _sentence_budget_note(resolved_version, target_words)
    token_totals = _empty_token_totals()
    last_meta = {"generation_id": "", "provider_name": "", "model_id": model}

    def generate(prompt_text: str, attempt_target: int, current_for_mock: str) -> GenerationResult:
        payload = _build_openrouter_payload(
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt_text,
            use_json_schema=use_json,
            thinking=thinking,
            reasoning=reasoning,
            reasoning_effort=reasoning_effort,
            max_tokens=_target_aware_max_tokens(attempt_target, reasoning, reasoning_effort),
        )
        gen = client.chat_completion(payload)
        record = gen.usage
        token_totals["prompt_tokens"] += record.prompt_tokens or 0
        token_totals["completion_tokens"] += record.completion_tokens or 0
        token_totals["total_tokens"] += record.total_tokens or 0
        token_totals["reasoning_tokens"] += record.reasoning_tokens or 0
        token_totals["cached_prompt_tokens"] += record.cached_prompt_tokens or 0
        last_meta["generation_id"] = record.generation_id or last_meta["generation_id"]
        last_meta["provider_name"] = record.provider_name or last_meta["provider_name"]
        last_meta["model_id"] = record.model_id or last_meta["model_id"]
        return gen

    def render_repair(direction: str, current_summary_md: str, target: int, current_word_count: int) -> str:
        template = repair_templates.get(direction) or _fallback_repair_template(stage, direction, length_control)
        return repair_prompt.render_repair_prompt(
            template,
            direction=direction,
            target_words=target,
            current_word_count=current_word_count,
            current_summary_md=current_summary_md,
            source_md=source_md,
            book_title=book_title,
            chapter_title=chapter_title,
            tolerance_pct=length_control.tolerance_pct,
            repair_target_mode=length_control.repair_target_mode,
            repair_min_reduction_pct=length_control.repair_min_reduction_pct,
            repair_sentence_delete=length_control.repair_sentence_delete,
            sentence_budget_note=sentence_budget_note,
        )

    loop = run_length_control(
        generate=generate,
        render_repair=render_repair,
        word_range=repair_prompt.visible_word_range,
        initial_user_prompt=user_prompt,
        target_words=target_words,
        length_control=length_control,
        visible_words=visible_word_count,
    )
    summary_md = loop.summary_md
    estimated_words = visible_word_count(summary_md)
    usage: Dict[str, Any] = {
        "prompt_tokens": token_totals["prompt_tokens"],
        "completion_tokens": token_totals["completion_tokens"],
        "total_tokens": token_totals["total_tokens"],
        "reasoning_tokens": token_totals["reasoning_tokens"],
        "cached_prompt_tokens": token_totals["cached_prompt_tokens"],
        "generation_cost": loop.generation_cost,
        "uncached_generation_cost": loop.uncached_generation_cost,
        "generation_id": last_meta["generation_id"],
        "provider_name": last_meta["provider_name"],
        "model_id": last_meta["model_id"],
    }
    return (
        summary_md,
        estimated_words,
        loop.first_pass_summary_md,
        usage,
        loop.generation_cost,
        loop.uncached_generation_cost,
        loop.passes_used,
    )


def _resolve_source_md(body: Mapping[str, Any]) -> str:
    """Return the chapter source markdown for a request.

    Prefers inline ``source_md``/``chapter``; otherwise resolves a structured
    ``source_ref`` from storage (the CF downloads the file itself). Raises
    :class:`storage.SourceRefError` for a bad/missing object and
    :class:`storage.SourceFetchError` for a transient storage failure.
    """
    inline = str(body.get("source_md") or body.get("chapter") or "").strip()
    if inline:
        return inline
    ref = body.get("source_ref")
    if isinstance(ref, Mapping):
        return storage.resolve_source_ref(ref).strip()
    return ""


def run_summarize(body: Dict[str, Any]) -> Dict[str, Any]:
    source_md = _resolve_source_md(body)
    summary_md_input = str(body.get("summary_md") or "").strip()
    api_key = str(body.get("api_key", "")).strip()
    base_url = str(body.get("base_url", "")).strip()
    thinking_raw = body.get("thinking")
    thinking: Optional[bool] = bool(thinking_raw) if thinking_raw is not None else None
    reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), Mapping) else None
    reasoning_effort = body.get("reasoning_effort") if body.get("reasoning_effort") is not None else None
    use_json = bool(body.get("use_json_schema", True))
    do_score = bool(body.get("score", True))
    target_raw = body.get("target_words")
    if target_raw in (None, ""):
        target_raw = body.get("words")
    try:
        target_words = int(str(target_raw or "").strip() or 0)
    except (TypeError, ValueError):
        target_words = 0
    do_judge = bool(body.get("judge", False))
    judge_model = str(body.get("judge_model", "openai/gpt-4o-mini")).strip() or "openai/gpt-4o-mini"
    judge_source_char_limit = int(body.get("judge_source_char_limit", 32000) or 32000)
    rubric_raw = body.get("rubric")

    auto_length = bool(body.get("auto_length", False))
    if auto_length and summary_md_input:
        raise PromptVersionError(
            "auto_length is not supported with summary_md (judge/scoring-only) mode",
            error="invalid_auto_length",
            status_code=400,
        )
    if auto_length and target_words <= 0:
        raise PromptVersionError(
            "auto_length requires target_words > 0",
            error="invalid_auto_length",
            status_code=400,
        )

    rubric = _parse_rubric(rubric_raw)
    client = _build_client(api_key=api_key, base_url=base_url)
    judge_only = bool(summary_md_input)

    registry = _load_cf_registry()
    resolved_version = _resolve_bundle_record(body, registry)

    # Model resolution (spec §7.2): explicit request wins, then the version's
    # baked model, then the function default.
    body_model = str(body.get("model", "")).strip()
    model = body_model or str((resolved_version or {}).get("model") or "").strip() or FALLBACK_MODEL

    # Reasoning default: the version may bake it (e.g. reasoning disabled);
    # explicit request controls always win.
    if reasoning is None and reasoning_effort is None and thinking is None:
        reasoning = dict((resolved_version or {}).get("reasoning") or {}) or None

    prompt_meta: Optional[Dict[str, str]] = None
    if not judge_only:
        system_prompt_text, prompt_meta = _resolve_prompt(body, registry)
        system_prompt = system_prompt_text
        prompt_meta["model"] = model

    stage = str(body.get("prompt_stage") or "chapter").strip() or "chapter"
    book_title = str(body.get("book_title") or "").strip()
    chapter_title = str(body.get("chapter_title") or body.get("title") or "").strip()
    passes_used = 1

    if judge_only:
        summary_md = summary_md_input
        estimated_words = visible_word_count(summary_md)
        first_pass_summary_md = summary_md
        usage: Dict[str, Any] = _zero_usage_dict(model)
        generation_cost = 0.0
        uncached_generation_cost = 0.0
    else:
        user_prompt = str(body.get("user_prompt") or "").strip()
        if not user_prompt and resolved_version is not None:
            user_prompt = _render_user_prompt(resolved_version, body)
        if not user_prompt:
            raise PromptVersionError(
                "No user prompt available: provide 'user_prompt' or rely on a "
                "version with a bundled user_template",
                error="missing_user_prompt",
                status_code=422,
            )

        if auto_length:
            summary_md, estimated_words, first_pass_summary_md, usage, generation_cost, uncached_generation_cost, passes_used = _run_auto_length(
                client=client,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                source_md=source_md,
                target_words=target_words,
                use_json=use_json,
                thinking=thinking,
                reasoning=reasoning,
                reasoning_effort=reasoning_effort,
                resolved_version=resolved_version,
                stage=stage,
                body=body,
                book_title=book_title,
                chapter_title=chapter_title,
            )
        else:
            payload = _build_openrouter_payload(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_json_schema=use_json,
                thinking=thinking,
                reasoning=reasoning,
                reasoning_effort=reasoning_effort,
            )
            gen: GenerationResult = client.chat_completion(payload)
            summary_md = gen.summary_md.strip()
            estimated_words = gen.estimated_visible_words
            if use_json and gen.parsed_json:
                summary_md = str(gen.parsed_json.get("summary_md") or summary_md).strip()
                estimated_words = int(gen.parsed_json.get("estimated_visible_words") or estimated_words or 0)
            if not estimated_words:
                estimated_words = visible_word_count(summary_md)
            usage = _usage_dict(gen)
            first_pass_summary_md = _first_pass_summary_md(gen, use_json)
            generation_cost = gen.usage.generation_cost
            uncached_generation_cost = gen.usage.uncached_generation_cost

    result: Dict[str, Any] = {
        "success": True,
        "summary": {
            "summary_md": summary_md,
            "estimated_visible_words": estimated_words,
        },
    }

    judge_scores: Optional[Dict[str, Any]] = None
    judge_error: Optional[str] = None
    judge_cost = 0.0

    if do_judge and rubric is not None:
        try:
            judge_result = judge_summary_absolute(
                client,
                judge_model=judge_model,
                summary_md=summary_md,
                rubric=rubric,
                source_md=source_md[:judge_source_char_limit],
            )
            judge_scores = _judge_scores_dict(judge_result)
            judge_cost = _raw_usage_cost(judge_result.raw_response)
        except Exception as exc:
            judge_error = str(exc)
            print(f"Judge error: {exc}", flush=True)

    scoring_result: Optional[Dict[str, Any]] = None
    if rubric is not None and target_words > 0 and do_score:
        sample_judge_scores: Optional[JudgeScores] = None
        if judge_scores is not None:
            sample_judge_scores = JudgeScores(
                faithfulness=judge_scores["faithfulness"],
                concept_coverage=judge_scores["concept_coverage"],
                qualifier_preservation=judge_scores["qualifier_preservation"],
                no_fluff=judge_scores["no_fluff"],
                structure_quality=judge_scores["structure_quality"],
            ).clamped()
        sample = SummarySample(
            sample_id="cf_chapter",
            level="chapter",
            target_words=target_words,
            summary_md=summary_md,
            source_md=source_md,
            group_id="cloud_function",
            first_pass_summary_md=first_pass_summary_md,
            passes_used=passes_used,
            generation_cost=generation_cost,
            uncached_generation_cost=uncached_generation_cost,
            malformed=False,
            rubric=rubric,
            judge_scores=sample_judge_scores,
        )
        scoring_result = _scoring_dict(sample)

    usage["judge_generation_cost"] = judge_cost
    if judge_only:
        usage["generation_cost"] = judge_cost
    # total_cost = every billed completion (generation passes + judge call).
    # In judge-only mode `generation_cost` is 0.0 locally, so this is judge_cost.
    usage["total_cost"] = generation_cost + judge_cost
    usage["iterations"] = passes_used
    result["usage"] = usage
    if scoring_result is not None:
        result["scoring"] = scoring_result
    if judge_scores is not None:
        result["judge_scores"] = judge_scores
    if judge_error is not None:
        result["judge_error"] = judge_error
    result["meta"] = _meta_block(prompt_meta, registry)
    if auto_length:
        result["meta"]["auto_length"] = True
        result["meta"]["passes_used"] = passes_used
        result["meta"]["final_visible_words"] = estimated_words
        result["meta"]["first_pass_visible_words"] = visible_word_count(first_pass_summary_md)

    return result


def build_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return result
