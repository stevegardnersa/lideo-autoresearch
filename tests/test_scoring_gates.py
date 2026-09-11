"""Offline tests for deterministic-vs-judged scoring gates.

Run with: python3 tests/test_scoring_gates.py

Covers the gate schema/selection added for judge-less runs:
  * ``<budget>_deterministic`` gates disable the semantic thresholds (null)
  * judged runs keep the v4 gates (30m 0.50/0.15, 60m 0.60/0.50)
  * legacy manifests without ``*_deterministic`` keys fall back to the budget gate
  * an explicit ``None`` to ``apply_gates_override`` disables a gate; an absent
    argument leaves the default in place
  * ``score_sample`` still hard-fails on objective length overshoot but not on
    low deterministic proxy scores when semantic gates are disabled
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.run_candidate import select_scoring_config
from core.versioning import load_benchmark_manifest
from scoring import (
    DEFAULT_SCORING_CONFIG,
    Rubric,
    SummarySample,
    apply_gates_override,
    score_sample,
)

ROOT = Path(__file__).resolve().parent.parent
PROFILE_30M = "30m_deepseek-v4-flash_notthinking"
PROFILE_60M = "60m_deepseek-v4-flash_notthinking"


def _manifest():
    return load_benchmark_manifest(ROOT / "benchmark_version.json")


def test_judged_runs_keep_v4_gates():
    manifest = _manifest()
    config, mode, gate_key = select_scoring_config(manifest, profile_name=PROFILE_30M, judge_model="openai/gpt-5.4-mini")
    assert mode == "judged"
    assert gate_key == "30m", gate_key
    assert config.gates.min_faithfulness == 0.50, config.gates
    assert config.gates.min_concept_coverage == 0.15, config.gates

    config60, mode60, gate60 = select_scoring_config(manifest, profile_name=PROFILE_60M, judge_model="openai/gpt-5.4-mini")
    assert mode60 == "judged" and gate60 == "60m"
    assert config60.gates.min_faithfulness == 0.60
    assert config60.gates.min_concept_coverage == 0.50
    print("test_judged_runs_keep_v4_gates: OK")


def test_deterministic_runs_disable_semantic_gates():
    manifest = _manifest()
    for profile, budget in ((PROFILE_30M, "30m"), (PROFILE_60M, "60m"), ("default_profile", "default")):
        config, mode, gate_key = select_scoring_config(manifest, profile_name=profile, judge_model="")
        assert mode == "deterministic"
        assert gate_key == f"{budget}_deterministic", (profile, gate_key)
        assert config.gates.min_faithfulness is None, config.gates
        assert config.gates.min_concept_coverage is None, config.gates
        # Objective gates are untouched.
        assert config.gates.max_passes == 5
        assert config.gates.max_final_length_error_pct == 0.10
    print("test_deterministic_runs_disable_semantic_gates: OK")


def test_legacy_manifest_falls_back_to_budget_gate():
    manifest = _manifest()
    legacy = dict(manifest)
    legacy["scoring_gates"] = {
        "default": {"min_faithfulness": 0.70, "min_concept_coverage": 0.60},
        "30m": {"min_faithfulness": 0.50, "min_concept_coverage": 0.15},
    }
    config, mode, gate_key = select_scoring_config(legacy, profile_name=PROFILE_30M, judge_model="")
    assert mode == "deterministic"
    assert gate_key == "30m", gate_key
    assert config.gates.min_faithfulness == 0.50
    assert config.gates.min_concept_coverage == 0.15
    print("test_legacy_manifest_falls_back_to_budget_gate: OK")


def test_apply_gates_override_null_disables_and_absent_keeps_default():
    # No args: identity (same object).
    assert apply_gates_override(DEFAULT_SCORING_CONFIG) is DEFAULT_SCORING_CONFIG

    # Explicit None disables the semantic gates.
    disabled = apply_gates_override(DEFAULT_SCORING_CONFIG, min_faithfulness=None, min_concept_coverage=None)
    assert disabled.gates.min_faithfulness is None
    assert disabled.gates.min_concept_coverage is None
    # Objective gates unchanged.
    assert disabled.gates.max_passes == DEFAULT_SCORING_CONFIG.gates.max_passes
    assert disabled.gates.max_final_length_error_pct == DEFAULT_SCORING_CONFIG.gates.max_final_length_error_pct

    # Explicit value overrides; omitted keys keep defaults.
    partial = apply_gates_override(DEFAULT_SCORING_CONFIG, min_faithfulness=0.42)
    assert partial.gates.min_faithfulness == 0.42
    assert partial.gates.min_concept_coverage == DEFAULT_SCORING_CONFIG.gates.min_concept_coverage
    print("test_apply_gates_override_null_disables_and_absent_keeps_default: OK")


def _sample(target_words, summary_words, rubric=None):
    return SummarySample(
        sample_id="s0",
        level="chapter",
        target_words=target_words,
        summary_md=("word " * summary_words).strip(),
        source_md=("source " * target_words).strip(),
        passes_used=1,
        generation_cost=0.0,
        uncached_generation_cost=0.0,
        rubric=rubric
        or Rubric(
            headings=("zzabsentzz",),
            core_concepts=("zzabsentzz",),
            mechanisms_or_explanations=("zzabsentzz",),
            critical_qualifiers=("zzabsentzz",),
            important_examples=("zzabsentzz",),
            key_entities_or_numbers=("999999",),
            key_terms=("zzabsentzz",),
        ),
    )


def test_score_sample_objective_only_when_semantic_gates_disabled():
    disabled = apply_gates_override(DEFAULT_SCORING_CONFIG, min_faithfulness=None, min_concept_coverage=None)

    # On-length, single pass, low deterministic proxies -> passes (only objective gates apply).
    on_length = _sample(target_words=100, summary_words=100)
    score = score_sample(on_length, config=disabled)
    assert score.hard_fail is False, score.hard_fail_reasons
    assert "faithfulness_below_threshold" not in score.hard_fail_reasons
    assert "concept_coverage_below_threshold" not in score.hard_fail_reasons

    # Length overshoot still hard-fails under the objective length gate.
    overshoot = _sample(target_words=100, summary_words=500)
    score_over = score_sample(overshoot, config=disabled)
    assert score_over.hard_fail is True
    assert "length_outside_hard_tolerance" in score_over.hard_fail_reasons

    # Sanity: with judged gates restored, the same on-length sample fails on semantics.
    judged = apply_gates_override(DEFAULT_SCORING_CONFIG, min_faithfulness=0.50, min_concept_coverage=0.15)
    score_judged = score_sample(on_length, config=judged)
    assert score_judged.hard_fail is True, "expected low proxy to fail judged gates"
    assert "faithfulness_below_threshold" in score_judged.hard_fail_reasons or "concept_coverage_below_threshold" in score_judged.hard_fail_reasons
    print("test_score_sample_objective_only_when_semantic_gates_disabled: OK")


def main():
    tests = [
        test_judged_runs_keep_v4_gates,
        test_deterministic_runs_disable_semantic_gates,
        test_legacy_manifest_falls_back_to_budget_gate,
        test_apply_gates_override_null_disables_and_absent_keeps_default,
        test_score_sample_objective_only_when_semantic_gates_disabled,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} SCORING-GATE TESTS PASSED")


if __name__ == "__main__":
    main()
