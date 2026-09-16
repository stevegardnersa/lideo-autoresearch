"""Offline tests for the sentence-budget and length-aim prompt components.

Covers the length/readability tuning work: separate words-per-sentence for the
sentence budget, sentence-budget scaling, and the first-pass length aim.

Run with: python3 tests/test_sentence_budget.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from candidate_spec import (
    get_candidate,
    render_chapter_user,
    sentence_budget,
    _words_per_sentence,
    _sentence_target_words,
    _sentence_budget_scale,
    _length_aim,
)
from core import prompt_registry as pr


class _Stage:
    def __init__(self, components):
        self.prompt_components = components


def test_sentence_budget_defaults():
    assert sentence_budget(360) == 20
    assert sentence_budget(1288) == 72
    assert sentence_budget(1288, words_per_sentence=13) == 99
    assert sentence_budget(1288, words_per_sentence=12) == 107
    print("test_sentence_budget_defaults: OK")


def test_sentence_budget_scale():
    assert sentence_budget(1288, words_per_sentence=11, scale=1.0) == 117
    assert sentence_budget(1288, words_per_sentence=11, scale=0.9) == 105
    assert sentence_budget(10, words_per_sentence=11, scale=0.9) >= 1
    print("test_sentence_budget_scale: OK")


def test_component_readers():
    stage = _Stage(
        {
            "target_sentence_words": "13",
            "sentence_target_words": "11",
            "sentence_budget_scale": "0.9",
            "length_aim": "0.90",
        }
    )
    assert _words_per_sentence(stage) == 13
    assert _sentence_target_words(stage) == 11
    assert abs(_sentence_budget_scale(stage) - 0.9) < 1e-9
    assert _length_aim(stage) == "0.90"

    # sentence_target_words falls back to target_sentence_words.
    fallback = _Stage({"target_sentence_words": "12"})
    assert _sentence_target_words(fallback) == 12
    assert _sentence_budget_scale(fallback) == 1.0
    assert _length_aim(fallback) == "target"

    # invalid values fall back to defaults rather than raising.
    bad = _Stage({"target_sentence_words": "x", "sentence_budget_scale": "y", "length_aim": ""})
    assert _words_per_sentence(bad) == candidate_spec.SENTENCE_WORDS
    assert _sentence_budget_scale(bad) == 1.0
    assert _length_aim(bad) == "target"
    print("test_component_readers: OK")


def test_aim_targets_rendered():
    spec = get_candidate("30m_gpt-5.6-luna_notthinking")
    spec.chapter_stage.prompt_components = {
        "length_policy": "explicit_budget_first",
        "target_sentence_words": "12",
        "length_aim": "0.90",
    }
    text = render_chapter_user(spec, source_md="src", target_words=1288, book_title="B")
    assert "Aim for about 1159 words" in text  # round(1288 * 0.90)
    assert "Target sentences: 107" in text  # round(1288 / 12)

    spec.chapter_stage.prompt_components["length_aim"] = "low"
    low_text = render_chapter_user(spec, source_md="src", target_words=1288, book_title="B")
    assert "Aim for about 1185 words" in low_text  # low edge at 8% tolerance
    print("test_aim_targets_rendered: OK")


def test_sentence_primary_policy_rendered():
    spec = get_candidate("30m_gpt-5.6-luna_notthinking")
    spec.chapter_stage.prompt_components = {
        "length_policy": "sentence_primary",
        "sentence_target_words": "11",
    }
    text = render_chapter_user(spec, source_md="src", target_words=1288, book_title="B")
    assert "write exactly 117 sentences" in text
    assert "hard constraint" in text
    print("test_sentence_primary_policy_rendered: OK")


def test_sentence_base_rendered_from_registry():
    """The curated `sentence` base renders the exact S instruction (spec §5)."""
    from tools.prompts_api import list_base_prompts

    bases = {b["slug"]: b for b in list_base_prompts()}
    base = bases["sentence"]
    spec = get_candidate("30m_gpt-5.6-luna_notthinking")
    pr.apply_overlay(spec, stage_kind="chapter", prompt_version=base["version_id"])
    text = render_chapter_user(spec, source_md="src", target_words=1288, book_title="B")
    assert "write exactly 117 sentences" in text, text
    assert "hard constraint" in text, text
    assert base["version_id"] == "chapter-p15", base
    print("test_sentence_base_rendered_from_registry: OK")


def main():
    tests = [
        test_sentence_budget_defaults,
        test_sentence_budget_scale,
        test_component_readers,
        test_aim_targets_rendered,
        test_sentence_primary_policy_rendered,
        test_sentence_base_rendered_from_registry,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} SENTENCE-BUDGET TESTS PASSED")


if __name__ == "__main__":
    main()
