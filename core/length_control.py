"""Transport-agnostic length-control loop.

Extracted from ``core.run_candidate.run_length_controlled_stage`` so the same
generate -> measure -> repair loop can run inside the Cloud Function without
importing the candidate module or the autoresearch harness.

The loop owns pass accounting and every termination rule:

* soft tolerance band acceptance (target +/- ``tolerance_pct``)
* hard tolerance acceptance (target +/- ``hard_tolerance_pct``) when enabled
* direction-flip stop
* diminishing-returns early stop (``early_stop_patience`` / ``early_stop_epsilon``)
* truncation retry (reissue a shorter target; does not consume a pass)
* best-pass selection (closest visible word count wins; earliest on ties)

Callers inject the provider-specific pieces: ``generate`` performs one
completion, ``render_repair`` builds the direction-specific repair prompt, and
``word_range`` computes the acceptance band.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Tuple

from scoring import visible_word_count


@dataclass
class LengthControlConfig:
    """Loop config with the same fields/defaults as ``candidate_spec``.

    ``core.run_candidate`` passes the live ``candidate_spec`` config object
    (duck-typed); the Cloud Function can build one of these directly from a
    version's baked ``length_control`` mapping without importing the tuned
    candidate file.
    """

    max_passes: int = 5
    tolerance_pct: float = 0.05
    hard_tolerance_pct: float = 0.10
    repair_strategy: str = "edit_existing"
    repair_more_prompt_id: str = "expand_missing_detail"
    repair_less_prompt_id: str = "shrink_dedup_first"
    max_truncation_retries: int = 1
    repair_target_mode: str = "target"
    repair_sentence_delete: bool = False
    repair_min_reduction_pct: float = 0.0
    early_stop_patience: int = 0
    early_stop_epsilon: int = 0
    stop_on_direction_flip: bool = False
    accept_within_hard_tolerance: bool = False

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "LengthControlConfig":
        """Build a config from a (possibly partial) baked mapping.

        Unknown keys are ignored and malformed values fall back to the field
        default, so a registry record can never crash the loop.
        """
        config = cls()
        if not isinstance(raw, Mapping):
            return config
        for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if name not in raw:
                continue
            default = getattr(config, name)
            value = raw[name]
            try:
                if isinstance(default, bool):
                    setattr(config, name, bool(value))
                elif isinstance(default, int):
                    setattr(config, name, int(value))
                elif isinstance(default, float):
                    setattr(config, name, float(value))
                elif isinstance(default, str):
                    text = str(value).strip()
                    if text:
                        setattr(config, name, text)
            except (TypeError, ValueError):
                continue
        return config


@dataclass(frozen=True)
class LengthControlResult:
    summary_md: str
    first_pass_summary_md: str
    passes_used: int
    generation_cost: float
    uncached_generation_cost: float
    raw_responses: Tuple[Mapping[str, Any], ...]
    pass_latency_seconds: Tuple[float, ...] = ()


def visible_word_range(target_words: int, tolerance_pct: float) -> Tuple[int, int]:
    delta = max(1, int(round(target_words * tolerance_pct)))
    return max(1, target_words - delta), target_words + delta


def _json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def run_length_control(
    *,
    generate: Callable[[str, int, str], Any],
    render_repair: Callable[[str, str, int, int], str],
    word_range: Callable[[int, float], Tuple[int, int]],
    initial_user_prompt: str,
    target_words: int,
    length_control: Any,
    resume_state: Optional[Mapping[str, Any]] = None,
    checkpoint_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
    latency_accumulator: Optional[List[float]] = None,
    visible_words: Callable[[str], int] = visible_word_count,
) -> LengthControlResult:
    """Run the generate/repair loop until in range, exhausted, or stopped.

    ``generate(user_prompt, attempt_target, current_for_mock)`` performs exactly
    one completion and returns a ``GenerationResult``-like object exposing
    ``summary_md``, ``raw_response``, ``usage.generation_cost``,
    ``usage.uncached_generation_cost``, ``truncated``, ``finish_reason`` and
    ``json_recovery``.
    """
    low, high = word_range(target_words, length_control.tolerance_pct)

    restored = dict(resume_state or {})
    responses: List[Mapping[str, Any]] = list(restored.get("raw_responses") or [])
    total_cost = float(restored.get("generation_cost") or 0.0)
    total_uncached_cost = float(restored.get("uncached_generation_cost") or 0.0)
    passes_used = int(restored.get("passes_used") or 0)
    summary_md = str(restored.get("summary_md") or "").strip()
    first_pass_summary_md = str(restored.get("first_pass_summary_md") or "").strip()
    total_truncation_retries = int(restored.get("truncation_retries") or 0)
    pass_latencies: List[float] = [float(v) for v in (restored.get("pass_latency_seconds") or [])]

    # Best-pass selection: among all passes executed (including passes restored
    # from a checkpoint), track the one whose visible word count is closest to
    # target_words. Returned summary_md is this pass, not the last one.
    best_summary_md = str(restored.get("best_summary_md") or "").strip()
    if not best_summary_md and summary_md:
        # Old-format checkpoint (no best pass recorded): seed from latest pass.
        best_summary_md = summary_md
    if best_summary_md:
        best_distance: Optional[int] = abs(visible_words(best_summary_md) - target_words)
    else:
        best_distance = None

    def update_best(candidate_md: str) -> None:
        nonlocal best_summary_md, best_distance
        if not best_summary_md:
            best_summary_md = candidate_md
            best_distance = abs(visible_words(candidate_md) - target_words)
            return
        distance = abs(visible_words(candidate_md) - target_words)
        # Strict < keeps the earliest pass on ties.
        assert best_distance is not None
        if distance < best_distance:
            best_summary_md = candidate_md
            best_distance = distance

    def emit_checkpoint() -> None:
        if checkpoint_callback is None:
            return
        checkpoint_callback(
            {
                "summary_md": summary_md,
                "best_summary_md": best_summary_md,
                "best_distance": best_distance,
                "first_pass_summary_md": first_pass_summary_md or summary_md,
                "passes_used": passes_used,
                "generation_cost": total_cost,
                "uncached_generation_cost": total_uncached_cost,
                "truncation_retries": total_truncation_retries,
                "raw_responses": _json_safe(list(responses)),
                "pass_latency_seconds": [float(v) for v in pass_latencies],
            }
        )

    max_truncation_retries = int(getattr(length_control, "max_truncation_retries", 1) or 0)

    def generate_pass(user_prompt: str, *, current_for_mock: str, pass_target: int) -> Any:
        nonlocal total_cost, total_uncached_cost, total_truncation_retries
        attempt_target = max(1, pass_target)
        retry_count = 0
        retry_prompt = user_prompt
        while True:
            pass_started = time.perf_counter()
            result = generate(retry_prompt, attempt_target, current_for_mock)
            pass_latencies.append(time.perf_counter() - pass_started)
            if latency_accumulator is not None:
                latency_accumulator.append(pass_latencies[-1])
            responses.append(_json_safe(dict(result.raw_response)))
            total_cost += result.usage.generation_cost
            total_uncached_cost += result.usage.uncached_generation_cost or result.usage.generation_cost
            if not result.truncated or retry_count >= max_truncation_retries:
                return result
            retry_count += 1
            total_truncation_retries += 1
            attempt_target = max(1, int(round(pass_target * (0.7 ** retry_count))))
            responses.append(
                {
                    "kind": "truncation_retry",
                    "retry_index": retry_count,
                    "pass_target_words": attempt_target,
                    "finish_reason": result.finish_reason,
                    "json_recovery": result.json_recovery,
                }
            )
            retry_prompt = (
                user_prompt
                + f"\n\nIMPORTANT: The previous attempt was cut off because it exceeded the output length limit "
                f"(finish_reason={result.finish_reason}). Rewrite a COMPLETE summary of at most {attempt_target} "
                "visible words so the JSON payload is not truncated. Do not leave the summary or the payload unfinished."
            )

    if passes_used <= 0 or not summary_md:
        result = generate_pass(initial_user_prompt, current_for_mock="", pass_target=target_words)
        passes_used = 1
        summary_md = result.summary_md.strip()
        first_pass_summary_md = summary_md
        update_best(summary_md)
        emit_checkpoint()

    lc = length_control
    hard_low, hard_high = word_range(target_words, lc.hard_tolerance_pct)
    last_words: Optional[int] = None
    last_direction: Optional[str] = None
    stalled_passes = 0
    while passes_used < lc.max_passes:
        words = visible_words(summary_md)
        if low <= words <= high:
            break
        if getattr(lc, "accept_within_hard_tolerance", False) and hard_low <= words <= hard_high:
            break
        direction = "more" if words < low else "less"
        # Direction flip: we overshot past the target into the other repair mode.
        if getattr(lc, "stop_on_direction_flip", False) and last_direction is not None and direction != last_direction:
            break
        # Diminishing returns: the last pass barely (or did not) reduce error.
        early_stop_patience = int(getattr(lc, "early_stop_patience", 0) or 0)
        if last_words is not None and early_stop_patience > 0:
            epsilon = int(getattr(lc, "early_stop_epsilon", 0) or 0)
            if abs(words - target_words) <= abs(last_words - target_words) - epsilon:
                stalled_passes = 0
            else:
                stalled_passes += 1
                if stalled_passes >= early_stop_patience:
                    break
        if getattr(lc, "repair_strategy", "edit_existing") == "regenerate_from_source":
            repair_user_prompt = initial_user_prompt
            current_for_mock = ""
        else:
            repair_user_prompt = render_repair(direction, summary_md, target_words, words)
            current_for_mock = summary_md
        result = generate_pass(repair_user_prompt, current_for_mock=current_for_mock, pass_target=target_words)
        passes_used += 1
        summary_md = result.summary_md.strip()
        if not first_pass_summary_md:
            first_pass_summary_md = summary_md
        update_best(summary_md)
        last_words = words
        last_direction = direction
        emit_checkpoint()

    return LengthControlResult(
        summary_md=best_summary_md,
        first_pass_summary_md=first_pass_summary_md or summary_md,
        passes_used=passes_used,
        generation_cost=total_cost,
        uncached_generation_cost=total_uncached_cost,
        raw_responses=tuple(responses),
        pass_latency_seconds=tuple(pass_latencies),
    )
