"""OpenRouter reasoning-effort configuration (recommended API).

Centralizes the effort tiers, token-budget fractions, request-param mapping, and
profile naming. Two supported parameter styles, per the OpenRouter reasoning
guide:

- ``reasoning: {"effort": X}``  (Option A, recommended, structured)
- ``reasoning_effort: X``       (Option B, top-level scalar)

New-style stage configs use the ``reasoning`` / ``reasoning_effort`` fields.
Legacy ``extra_body={"thinking": ...}`` configs keep byte-identical requests
and are only translated to the new API on explicit migration.
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
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        return str(reasoning["effort"])
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
    wrapper untouched. ``reasoning_effort="none"`` maps to a plain request (no
    reasoning param) for maximal compatibility with non-reasoning models.
    """
    if stage is None:
        return {}, True
    reasoning = getattr(stage, "reasoning", None)
    if isinstance(reasoning, dict) and reasoning:
        return {"reasoning": reasoning}, False
    effort = getattr(stage, "reasoning_effort", None)
    if effort and str(effort).strip().lower() != "none":
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