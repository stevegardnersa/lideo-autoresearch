"""Offline tests for named scoring gate profiles (strict-v1).

Run with: python3 tests/test_scoring_profiles.py

Covers:
  * ``--scoring-profile strict-v1`` applies the 0.88/0.80 accuracy bar when judged
  * deterministic runs prefer ``strict-v1_deterministic`` (semantic gates disabled)
  * ``auto`` (the default) reproduces the existing budget-derived gates exactly
  * an explicit ``default`` profile selects the default gate
  * unknown profile names fail fast with the available list
  * new named profiles are merged into manifests written before they existed,
    without overwriting operator tuning
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.run_candidate import UnknownScoringProfile, select_scoring_config
from core.versioning import DEFAULT_BENCHMARK_MANIFEST, load_benchmark_manifest
from scoring import DEFAULT_SCORING_CONFIG, Rubric, SummarySample, score_sample

ROOT = Path(__file__).resolve().parent.parent
PROFILE_30M = "30m_deepseek-v4-flash_notthinking"
PROFILE_60M = "60m_deepseek-v4-flash_notthinking"
JUDGE = "openai/gpt-5.4-mini"


def _manifest():
    return load_benchmark_manifest(ROOT / "benchmark_version.json")


def test_strict_v1_judged():
    manifest = _manifest()
    config, mode, gate_key = select_scoring_config(
        manifest, profile_name=PROFILE_30M, judge_model=JUDGE, scoring_profile="strict-v1"
    )
    assert mode == "judged"
    assert gate_key == "strict-v1", gate_key
    assert config.gates.min_faithfulness == 0.88, config.gates
    assert config.gates.min_concept_coverage == 0.80, config.gates
    # Objective gates keep their defaults (length gate is not weakened).
    assert config.gates.max_final_length_error_pct == DEFAULT_SCORING_CONFIG.gates.max_final_length_error_pct
    assert config.gates.max_passes == DEFAULT_SCORING_CONFIG.gates.max_passes
    print("test_strict_v1_judged: OK")


def test_strict_v1_deterministic_disables_semantics():
    manifest = _manifest()
    config, mode, gate_key = select_scoring_config(
        manifest, profile_name=PROFILE_60M, judge_model="", scoring_profile="strict-v1"
    )
    assert mode == "deterministic"
    assert gate_key == "strict-v1_deterministic", gate_key
    assert config.gates.min_faithfulness is None
    assert config.gates.min_concept_coverage is None
    assert config.gates.max_final_length_error_pct == 0.10
    assert config.gates.max_passes == 5
    print("test_strict_v1_deterministic_disables_semantics: OK")


def test_auto_is_unchanged_regression():
    manifest = _manifest()
    judged, mode, key = select_scoring_config(manifest, profile_name=PROFILE_30M, judge_model=JUDGE)
    assert mode == "judged" and key == "30m"
    assert judged.gates.min_faithfulness == 0.50
    assert judged.gates.min_concept_coverage == 0.15

    det, mode, key = select_scoring_config(manifest, profile_name=PROFILE_30M, judge_model="")
    assert mode == "deterministic" and key == "30m_deterministic"
    assert det.gates.min_faithfulness is None

    # Explicit "auto" is identical to the parameter default.
    explicit, mode, key = select_scoring_config(
        manifest, profile_name=PROFILE_30M, judge_model=JUDGE, scoring_profile="auto"
    )
    assert (explicit.gates.min_faithfulness, key) == (judged.gates.min_faithfulness, "30m")
    print("test_auto_is_unchanged_regression: OK")


def test_explicit_default_profile():
    manifest = _manifest()
    config, mode, gate_key = select_scoring_config(
        manifest, profile_name=PROFILE_30M, judge_model=JUDGE, scoring_profile="default"
    )
    assert gate_key == "default"
    assert config.gates.min_faithfulness == 0.70
    assert config.gates.min_concept_coverage == 0.60
    print("test_explicit_default_profile: OK")


def test_unknown_profile_fails_fast():
    manifest = _manifest()
    try:
        select_scoring_config(
            manifest, profile_name=PROFILE_30M, judge_model=JUDGE, scoring_profile="strict-v9"
        )
    except UnknownScoringProfile as exc:
        assert exc.profile == "strict-v9"
        assert "strict-v1" in exc.allowed
        assert "30m" in exc.allowed
    else:
        raise AssertionError("expected UnknownScoringProfile")
    print("test_unknown_profile_fails_fast: OK")


def test_new_profiles_merge_into_legacy_manifest():
    legacy = {
        "benchmark_version": "legacy",
        "corpus_version": "legacy",
        "rubric_version": "legacy",
        "scoring_version": "legacy",
        "judge_version": "legacy",
        "split_seed": 1,
        "scoring_gates": {
            "30m": {"min_faithfulness": 0.51, "min_concept_coverage": 0.16},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "benchmark_version.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        manifest = load_benchmark_manifest(path)

    gates = manifest["scoring_gates"]
    assert "strict-v1" in gates and "strict-v1_deterministic" in gates
    # Existing operator tuning is preserved.
    assert gates["30m"]["min_faithfulness"] == 0.51
    assert gates["60m"] == DEFAULT_BENCHMARK_MANIFEST["scoring_gates"]["60m"]
    print("test_new_profiles_merge_into_legacy_manifest: OK")


def _sample(target_words: int, summary_words: int) -> SummarySample:
    return SummarySample(
        sample_id="s0",
        level="chapter",
        target_words=target_words,
        summary_md=("word " * summary_words).strip(),
        source_md=("source " * target_words).strip(),
        passes_used=1,
        generation_cost=0.0,
        uncached_generation_cost=0.0,
        rubric=Rubric(
            headings=("zzabsentzz",),
            core_concepts=("zzabsentzz",),
            mechanisms_or_explanations=("zzabsentzz",),
            critical_qualifiers=("zzabsentzz",),
            important_examples=("zzabsentzz",),
            key_entities_or_numbers=("999999",),
            key_terms=("zzabsentzz",),
        ),
    )


def test_strict_v1_gate_is_stricter_than_budget_gate():
    manifest = _manifest()
    sample = _sample(target_words=100, summary_words=100)

    budget, _, _ = select_scoring_config(manifest, profile_name=PROFILE_30M, judge_model=JUDGE)
    strict, _, _ = select_scoring_config(
        manifest, profile_name=PROFILE_30M, judge_model=JUDGE, scoring_profile="strict-v1"
    )
    # The strict bar is strictly higher on both semantic metrics.
    assert strict.gates.min_faithfulness > budget.gates.min_faithfulness
    assert strict.gates.min_concept_coverage > budget.gates.min_concept_coverage
    # The proxy sample fails the strict gate and the failure is attributed to a
    # semantic threshold, not to length/passes.
    strict_score = score_sample(sample, config=strict)
    assert strict_score.hard_fail is True
    assert any(
        reason in strict_score.hard_fail_reasons
        for reason in ("faithfulness_below_threshold", "concept_coverage_below_threshold")
    ), strict_score.hard_fail_reasons
    print("test_strict_v1_gate_is_stricter_than_budget_gate: OK")


def main():
    tests = [
        test_strict_v1_judged,
        test_strict_v1_deterministic_disables_semantics,
        test_auto_is_unchanged_regression,
        test_explicit_default_profile,
        test_unknown_profile_fails_fast,
        test_new_profiles_merge_into_legacy_manifest,
        test_strict_v1_gate_is_stricter_than_budget_gate,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} SCORING-PROFILE TESTS PASSED")


if __name__ == "__main__":
    main()
