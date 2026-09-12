"""OpenRouter reasoning-effort configuration (recommended API).

Centralizes the effort tiers, token-budget fractions, request-param mapping, and
profile naming. Two supported parameter styles, per the OpenRouter reasoning
guide:

- ``reasoning: {"effort": X}``  (Option A, recommended, structured)
- ``reasoning_effort: X``       (Option B, top-level scalar)

New-style stage configs use the ``reasoning`` / ``reasoning_effort`` fields.
The ``effort="none"`` tier is canonicalised to ``reasoning={"enabled": false}``
(forced-off) at request-build time — absent reasoning is the provider default,
which may still reason. Models with mandatory reasoning (probe
``none_style="mandatory"``, e.g. stepfun/step-3.7-flash) reject the disabled
form and reason on plain requests, so requesting ``enabled: false`` on them
raises immediately instead of silently running at default effort.
``extra_body={"thinking": ...}`` legacy configs are no longer produced by the
profile set; support is retained only for back-compat of in-flight stages and
kept byte-identical until explicit migration.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Fraction of max_output_tokens that reasoning is expected to consume.
# A high effort eats most of the budget, so the runtime must raise max_tokens
# or the final visible response is starved.
EFFORT_TOKEN_FRACTION: Dict[str, float] = {
    "none": 0.0,
    "minimal": 0.10,
    "low": 0.20,
    "medium": 0.50,
    "high": 0.80,
    "xhigh": 0.95,
    "max": 0.95,
}

DEFAULT_THINKING_EFFORT = "high"  # legacy thinking:enabled migrates to this effort

DEFAULT_MAX_TOKEN_CAP = 163_840

# Generation-time length control (target-aware max_tokens cap).
TOKENS_PER_WORD = 1.4  # ~0.7 words/token for English markdown
JSON_OVERHEAD_TOKENS = 200  # response_format wrapper + markdown framing
DEFAULT_VISIBLE_HEADROOM = 1.6  # visible-output headroom over the target words
DEFAULT_MIN_OUTPUT_TOKENS = 512  # degenerate tiny targets still get a usable budget

# Models whose providers reject ``reasoning={"enabled": false}`` and still reason
# on a plain request (no reasoning field). A ``_notthinking`` profile is
# impossible for them — the request builder must fail loudly instead of silently
# running at default effort. Populated by the generated candidate_spec via
# ``register_mandatory_reasoning_models`` (see tools/gen_profile_literal.py).
_MANDATORY_REASONING_MODELS: frozenset = frozenset()


def register_mandatory_reasoning_models(models: Any) -> None:
    """Set the set of model ids whose reasoning cannot be disabled.

    Called by the generated ``candidate_spec`` module at import time so the
    guard below fires on the direct path, the Cloud Function path, and any
    offline consumer (tests). Idempotent; last call wins.
    """
    global _MANDATORY_REASONING_MODELS
    _MANDATORY_REASONING_MODELS = frozenset(models or ())


def mandatory_reasoning_models() -> frozenset:
    return _MANDATORY_REASONING_MODELS


def assert_disabling_reasoning_allowed(model: Any) -> None:
    """Raise when a request would disable reasoning on a mandatory-reasoning model.

    Without this, ``_notthinking`` on such a model silently reasons at default
    effort (the provider ignores ``enabled: false`` and a plain request still
    reasons). Failing loudly surfaces the impossible profile immediately.
    """
    model = str(model or "")
    if model and model in _MANDATORY_REASONING_MODELS:
        raise ValueError(
            f"Model {model} has mandatory reasoning and cannot disable it: "
            "reasoning.enabled=false is rejected by the provider, and a plain "
            "request still reasons at default effort. Use an effort-tier profile "
            f"(e.g. an {model} _effort-* profile) instead of a _notthinking profile."
        )


def effort_token_fraction(effort: Optional[str]) -> float:
    """Fraction of the max_output_tokens budget reasoning is expected to consume."""
    if not effort:
        return 0.0
    return EFFORT_TOKEN_FRACTION.get(str(effort).strip().lower(), 0.0)


def scaled_max_tokens(base: int = 8192, effort: Optional[str] = None, cap: int = DEFAULT_MAX_TOKEN_CAP) -> int:
    """Raise a token budget so the visible output survives a high reasoning effort."""
    fraction = effort_token_fraction(effort)
    if fraction <= 0.0:
        return int(base)
    scaled = math.ceil((float(base) / (1.0 - fraction)) - 1e-9)
    return min(int(scaled), int(cap))


def visible_max_tokens(
    target_words: int,
    *,
    effort: Optional[str] = None,
    headroom: float = DEFAULT_VISIBLE_HEADROOM,
    cap: int = DEFAULT_MAX_TOKEN_CAP,
) -> int:
    """Per-request ``max_tokens`` derived from a visible-word target.

    The visible-output budget is ``ceil(target_words * headroom)`` words converted
    at ``TOKENS_PER_WORD`` plus ``JSON_OVERHEAD_TOKENS``, then inflated by the
    reasoning effort's token reservation (the same ``1 / (1 - fraction)`` divisor
    as ``scaled_max_tokens``, so the two stay consistent for the same effort).
    Clamped to ``[DEFAULT_MIN_OUTPUT_TOKENS, cap]``.

    Monotonic non-decreasing in ``target_words`` for fixed effort/headroom; never
    exceeds ``cap``; never drops below the floor. ``effort=None`` and
    ``"none"`` apply no inflation.
    """
    fraction = effort_token_fraction(effort)
    visible_base_words = max(1, math.ceil(float(target_words) * float(headroom)))
    tokens = float(visible_base_words) * TOKENS_PER_WORD + JSON_OVERHEAD_TOKENS
    if fraction > 0.0:
        tokens /= (1.0 - fraction)
    return int(min(int(cap), max(DEFAULT_MIN_OUTPUT_TOKENS, math.ceil(tokens))))


def stage_reasoning_effort(stage: Any) -> Optional[str]:
    """Resolve the effective reasoning effort for a stage config.

    Precedence: ``stage.reasoning["effort"]`` -> ``stage.reasoning_effort`` ->
    legacy ``extra_body["thinking"]``. Returns ``None`` when reasoning is left
    unconfigured or uses legacy ``enabled`` semantics (unknown effort),
    ``"none"`` when reasoning is disabled, and the effort string otherwise.
    """
    if stage is None:
        return None
    reasoning = getattr(stage, "reasoning", None)
    if isinstance(reasoning, dict) and reasoning:
        if reasoning.get("enabled") is False:
            return "none"
        effort = reasoning.get("effort")
        if effort:
            return str(effort)
    effort = getattr(stage, "reasoning_effort", None)
    if effort:
        return str(effort)
    thinking_cfg = (getattr(stage, "extra_body", None) or {}).get("thinking")
    if isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "disabled":
        return "none"
    return None


def reasoning_request_params(stage: Any) -> Tuple[Dict[str, Any], bool]:
    """Return (top_level_params, is_legacy_extra_body) for the request builder.

    New-style configs yield the params to merge at the top level of the request
    body. Legacy/plain configs yield empty params and keep the ``extra_body``
    wrapper untouched. ``reasoning_effort="none"`` maps to an explicit
    ``reasoning: {"enabled": false}`` so trimming a reasoning model's effort is
    always requested rather than left to provider defaults.
    """
    if stage is None:
        return {}, True
    reasoning = getattr(stage, "reasoning", None)
    if isinstance(reasoning, dict) and reasoning:
        if str(reasoning.get("effort") or "").strip().lower() == "none":
            assert_disabling_reasoning_allowed(getattr(stage, "model", ""))
            return {"reasoning": {"enabled": False}}, False
        if reasoning.get("enabled") is False:
            assert_disabling_reasoning_allowed(getattr(stage, "model", ""))
        return {"reasoning": reasoning}, False
    effort = getattr(stage, "reasoning_effort", None)
    if effort:
        if str(effort).strip().lower() == "none":
            assert_disabling_reasoning_allowed(getattr(stage, "model", ""))
            return {"reasoning": {"enabled": False}}, False
        return {"reasoning_effort": str(effort)}, False
    return {}, True


def effort_enables_thinking(effort: Optional[str]) -> bool:
    """True when a resolved effort corresponds to reasoning enabled (legacy default)."""
    if effort is None:
        return True  # legacy "plain" and legacy "thinking: enabled" semantics
    return str(effort).strip().lower() != "none"


def effort_style_label(effort: Optional[str]) -> str:
    """Profile suffix for a reasoning effort.

    ``none`` maps to ``notthinking`` for backward compatibility with existing
    run patterns and the autoresearch agent's ``_thinking``/``_notthinking``
    filters. Other efforts get explicit ``effort-<name>`` suffixes and never
    collide with the legacy ``_thinking`` names.
    """
    value = str(effort).strip().lower() if effort else "none"
    if value == "none":
        return "notthinking"
    return f"effort-{value}"


def profile_name_for(time_budget: str, slug: str, effort: Optional[str]) -> str:
    return f"{time_budget}_{slug}_{effort_style_label(effort)}"


def manifest_effort_label(stage: Any) -> str:
    """Human/CLI label for a stage's effective reasoning configuration.

    ``notthinking`` for effort ``none``, ``effort-<name>`` for new-style
    efforts, and ``kept`` when reasoning is unconfigured or uses the legacy
    thinking param untouched.
    """
    effort = stage_reasoning_effort(stage)
    if effort is None:
        return "kept"
    return effort_style_label(effort)


def resolve_effort(requested: Optional[str], supported: Any) -> str:
    """Pick the closest supported effort to an unsupported request."""
    value = str(requested).strip().lower() if requested else "none"
    supported_set = set(supported or ())
    if value in supported_set:
        return value
    non_none = [e for e in REASONING_EFFORTS if e in supported_set and e != "none"]
    if non_none:
        return non_none[-1]  # highest supported effort
    return "none"