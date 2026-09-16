"""Offline tests for the legacy-thinking -> native reasoning migration.

Run with: python3 tests/test_reasoning_migration.py

Covers spec `specs/legacy-thinking-reasoning-migration.md`:
  * R1/R5: every profile in `candidate_spec.PROFILE_CANDIDATES` has no legacy
    `extra_body` and carries a native reasoning signal per stage; the generated
    candidate_spec.py matches data/candidates.json for reasoning fields.
  * R3: every (time x model) family has a `_notthinking` profile whose stages
    carry `reasoning.enabled is False`.
  * R4: `reasoning={"effort": "none"}` collapses to `reasoning.enabled=False`
    in `reasoning_request_params`; `stage_reasoning_effort` resolves the
    canonical "none".
"""

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core.reasoning import (
    effort_style_label,
    profile_name_for,
    reasoning_request_params,
    stage_reasoning_effort,
)

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_JSON = ROOT / "data" / "candidates.json"


def _stage(stage: Any) -> Dict[str, Any]:
    return {
        "extra_body": getattr(stage, "extra_body", None),
        "reasoning": getattr(stage, "reasoning", None),
        "reasoning_effort": getattr(stage, "reasoning_effort", None),
    }


def test_profiles_have_native_reasoning_only():
    cands = candidate_spec.PROFILE_CANDIDATES
    assert len(cands) > 0
    for key, spec in cands.items():
        for stage in (spec.chapter_stage, spec.composer_stage):
            s = _stage(stage)
            assert s["extra_body"] is None, f"{key}: legacy extra_body still present: {s['extra_body']}"
            has_reasoning = s["reasoning"] is not None
            has_scalar = s["reasoning_effort"] is not None
            assert not (has_reasoning and has_scalar), f"{key}: both reasoning styles set"
            # _notthinking may be plain (absent reasoning) when the model cannot
            # disable reasoning (none_style="plain"); every other profile must
            # carry an explicit native reasoning signal.
            if key.endswith("_notthinking"):
                continue
            assert has_reasoning or has_scalar, f"{key}: no reasoning signal at all"
    print(f"test_profiles_have_native_reasoning_only: OK ({len(cands)} profiles)")


def test_generated_py_matches_json_reasoning_fields():
    with open(CANDIDATES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    profiles = data["profiles"]
    for key, spec in candidate_spec.PROFILE_CANDIDATES.items():
        json_profile = profiles[key]
        for field in ("chapter_stage", "composer_stage"):
            js = json_profile[field]
            py = getattr(spec, field)
            assert py.reasoning == js.get("reasoning"), f"{key}.{field}: reasoning mismatch"
            assert py.reasoning_effort == js.get("reasoning_effort"), f"{key}.{field}: reasoning_effort mismatch"
            assert py.extra_body == js.get("extra_body"), f"{key}.{field}: extra_body mismatch"
    print("test_generated_py_matches_json_reasoning_fields: OK")


def test_every_family_has_notthinking_profile():
    cands = candidate_spec.PROFILE_CANDIDATES
    mandatory = set(candidate_spec.MANDATORY_REASONING_MODELS)
    families: Dict[str, list] = {}
    for key in cands:
        families.setdefault(key.rsplit("_", 1)[0], []).append(key)
    assert len(families) > 0
    for family, keys in families.items():
        spec = cands[keys[0]]
        if spec.chapter_stage.model in mandatory:
            # Models with mandatory reasoning cannot have a _notthinking profile
            # at all (see test_mandatory_models_have_no_notthinking_profile).
            continue
        nt = [k for k in keys if k.endswith("_notthinking")]
        assert nt, f"family {family} lacks a _notthinking profile ({keys})"
        spec = cands[nt[0]]
        for stage in (spec.chapter_stage, spec.composer_stage):
            reasoning = stage.reasoning or {}
            # _notthinking must never enable reasoning.
            assert reasoning.get("enabled") is not True, (
                f"{nt[0]}: _notthinking stage reasoning is enabled=True: {reasoning}"
            )
            assert "effort" not in reasoning, (
                f"{nt[0]}: _notthinking stage carries an effort tier: {reasoning}"
            )
    print(f"test_every_family_has_notthinking_profile: OK ({len(families)} families, "
          f"{len(mandatory)} mandatory models excluded)")


def test_mandatory_models_have_no_notthinking_profile():
    mandatory = set(candidate_spec.MANDATORY_REASONING_MODELS)
    assert mandatory, "expected at least one mandatory-reasoning model (stepfun)"
    cands = candidate_spec.PROFILE_CANDIDATES
    for key, spec in cands.items():
        if spec.chapter_stage.model in mandatory:
            assert not key.endswith("_notthinking"), (
                f"{key}: a _notthinking profile must not exist for mandatory-reasoning "
                f"model {spec.chapter_stage.model}"
            )
            for stage in (spec.chapter_stage, spec.composer_stage):
                reasoning = stage.reasoning or {}
                assert reasoning.get("enabled") is not False, (
                    f"{key}: mandatory model must never carry reasoning.enabled=False"
                )
    print(f"test_mandatory_models_have_no_notthinking_profile: OK ({len(mandatory)} models)")


def test_raw_json_has_no_off_variant_for_mandatory_models():
    candidates_path = Path(__file__).resolve().parents[1] / "data" / "candidates.json"
    data = json.loads(candidates_path.read_text(encoding="utf-8"))
    caps = data.get("capabilities", {}) or {}
    mandatory_caps = {
        model
        for model, cap in caps.items()
        if isinstance(cap, dict) and cap.get("none_style") in ("mandatory", "plain")
    }
    assert mandatory_caps, "expected mandatory/plain capabilities in candidates.json"
    for key, profile in (data.get("profiles") or {}).items():
        model = ((profile or {}).get("chapter_stage") or {}).get("model")
        if model not in mandatory_caps:
            continue
        reasoning = ((profile.get("chapter_stage") or {}).get("reasoning")) or {}
        disabled = reasoning.get("enabled") is False or reasoning.get("effort") == "none"
        assert not disabled, (
            f"{key}: an effort-off variant must never be authored for mandatory-reasoning "
            f"model {model} (hidden or not)"
        )
    print("test_raw_json_has_no_off_variant_for_mandatory_models: OK")


def test_mandatory_off_variant_filtered_even_if_unhidden():
    import importlib.util

    spec_path = Path(__file__).resolve().parents[1] / "tools" / "gen_profile_literal.py"
    spec = importlib.util.spec_from_file_location("_gen_profile_literal", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mandatory = mod.load_mandatory_reasoning_models()
    assert "stepfun/step-3.5-flash" in mandatory
    assert mod._disables_reasoning(
        {"chapter_stage": {"model": "stepfun/step-3.5-flash", "reasoning": {"enabled": False}}}
    )
    assert mod._disables_reasoning(
        {"chapter_stage": {"model": "stepfun/step-3.5-flash", "reasoning": {"effort": "none"}}}
    )
    assert not mod._disables_reasoning(
        {"chapter_stage": {"model": "stepfun/step-3.5-flash", "reasoning": {"effort": "minimal"}}}
    )
    print("test_mandatory_off_variant_filtered_even_if_unhidden: OK")


def test_step_35_flash_is_registered_mandatory():
    model = "stepfun/step-3.5-flash"
    assert model in candidate_spec.MANDATORY_REASONING_MODELS, (
        f"{model} must be registered as mandatory-reasoning "
        "(provider rejects reasoning.enabled=false)"
    )
    step_keys = [
        key
        for key, spec in candidate_spec.PROFILE_CANDIDATES.items()
        if spec.chapter_stage.model == model
    ]
    assert step_keys, f"no {model} profiles present"
    for key in step_keys:
        assert not key.endswith("_notthinking"), f"{key}: notthinking is impossible for {model}"
    for key in ("30m_step-3.5-flash_effort-medium", "60m_step-3.5-flash_effort-medium"):
        spec = candidate_spec.PROFILE_CANDIDATES.get(key)
        assert spec is not None, f"{key} missing"
        assert spec.use_json_schema is False, f"{key}: provider rejects JSON schema"
        for stage in (spec.chapter_stage, spec.composer_stage):
            assert stage.reasoning == {"effort": "medium"}, (key, stage.reasoning)
            effective_schema = (
                stage.use_json_schema if stage.use_json_schema is not None else spec.use_json_schema
            )
            assert effective_schema is False, f"{key}: request would carry a JSON schema"
    print("test_step_35_flash_is_registered_mandatory: OK")


def test_reasoning_request_params_raises_on_mandatory_disable():
    from types import SimpleNamespace

    mandatory = set(candidate_spec.MANDATORY_REASONING_MODELS)
    model = sorted(mandatory)[0]
    for kwargs in (
        {"reasoning": {"enabled": False}},
        {"reasoning": {"effort": "none"}},
    ):
        stage = SimpleNamespace(model=model, reasoning=kwargs["reasoning"], reasoning_effort=None, extra_body=None)
        try:
            reasoning_request_params(stage)
        except ValueError as e:
            assert "mandatory reasoning" in str(e), e
        else:
            raise AssertionError(f"expected ValueError for {model} disable via {kwargs}")
    # Non-mandatory model still emits enabled=False params.
    stage_ok = SimpleNamespace(model="openai/gpt-5.6-luna", reasoning={"enabled": False}, reasoning_effort=None, extra_body=None)
    params, _ = reasoning_request_params(stage_ok)
    assert params == {"reasoning": {"enabled": False}}, params
    print("test_reasoning_request_params_raises_on_mandatory_disable: OK")


def test_build_openrouter_request_raises_on_mandatory_disable():
    mandatory = set(candidate_spec.MANDATORY_REASONING_MODELS)
    model = sorted(mandatory)[0]
    stage = candidate_spec.StageConfig(model=model, reasoning={"enabled": False})
    try:
        candidate_spec.build_openrouter_request(
            stage=stage, system_prompt="s", user_prompt="u", schema_name="summary_response"
        )
    except ValueError as e:
        assert "mandatory reasoning" in str(e), e
    else:
        raise AssertionError(f"expected ValueError building a disable request for {model}")
    # Non-mandatory stage builds fine and carries enabled=False.
    stage_ok = candidate_spec.StageConfig(model="openai/gpt-5.6-luna", reasoning={"enabled": False})
    request = candidate_spec.build_openrouter_request(
        stage=stage_ok, system_prompt="s", user_prompt="u", schema_name="summary_response"
    )
    assert request["reasoning"] == {"enabled": False}, request["reasoning"]
    print("test_build_openrouter_request_raises_on_mandatory_disable: OK")


def test_tool_prevents_mandatory_notthinking_creation():
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from add_candidate import _resolve_efforts

    caps = {"model": "stepfun/step-3.7-flash", "efforts": ["medium"], "none_style": "mandatory"}
    try:
        _resolve_efforts(caps, requested=["none"])
    except ValueError as e:
        assert "cannot disable reasoning" in str(e), e
    else:
        raise AssertionError("expected ValueError requesting effort=none on mandatory model")
    # Default (no explicit request) never yields a _notthinking effort.
    chosen = _resolve_efforts(caps, requested=None)
    assert "none" not in chosen, chosen
    print("test_tool_prevents_mandatory_notthinking_creation: OK")


def test_stage_reasoning_effort_table():
    cases = [
        ({"enabled": False}, "none"),
        ({"effort": "none"}, "none"),
        (None, None),
        ({"effort": "minimal"}, "minimal"),
    ]
    for reasoning, expected in cases:
        stage = SimpleNamespace(reasoning=reasoning, reasoning_effort=None, extra_body=None)
        got = stage_reasoning_effort(stage)
        assert got == expected, f"stage_reasoning_effort({reasoning}) = {got!r}, want {expected!r}"
    print("test_stage_reasoning_effort_table: OK")


def test_reasoning_request_params_collapses_effort_none():
    stage = SimpleNamespace(reasoning={"effort": "none"}, reasoning_effort=None, extra_body=None)
    params, is_legacy = reasoning_request_params(stage)
    assert params == {"reasoning": {"enabled": False}}, params
    assert is_legacy is False, is_legacy

    stage_disabled = SimpleNamespace(reasoning={"enabled": False}, reasoning_effort=None, extra_body=None)
    params2, _ = reasoning_request_params(stage_disabled)
    assert params2 == {"reasoning": {"enabled": False}}, params2
    print("test_reasoning_request_params_collapses_effort_none: OK")


def test_effort_naming_preserved():
    assert effort_style_label("none") == "notthinking"
    assert profile_name_for("30m", "gpt-5.6-luna", "none") == "30m_gpt-5.6-luna_notthinking"
    print("test_effort_naming_preserved: OK")


def main():
    tests = [
        test_profiles_have_native_reasoning_only,
        test_generated_py_matches_json_reasoning_fields,
        test_every_family_has_notthinking_profile,
        test_mandatory_models_have_no_notthinking_profile,
        test_raw_json_has_no_off_variant_for_mandatory_models,
        test_mandatory_off_variant_filtered_even_if_unhidden,
        test_step_35_flash_is_registered_mandatory,
        test_reasoning_request_params_raises_on_mandatory_disable,
        test_build_openrouter_request_raises_on_mandatory_disable,
        test_tool_prevents_mandatory_notthinking_creation,
        test_stage_reasoning_effort_table,
        test_reasoning_request_params_collapses_effort_none,
        test_effort_naming_preserved,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} REASONING-MIGRATION TESTS PASSED")


if __name__ == "__main__":
    main()