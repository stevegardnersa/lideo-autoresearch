"""Offline tests for the placeholder template engine.

Run with: python3 tests/test_prompt_template.py

Covers spec `specs/prompts-ui-model-scoped-editing.md` §5.3 / §13.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prompt_template as pt


def test_extract_order_and_dedupe():
    text = "A {{b}} B {{a}} C {{b}} D{{ c }}"
    assert pt.extract_placeholders(text) == ["b", "a", "c"]
    assert pt.extract_placeholders("") == []
    assert pt.extract_placeholders(None) == []
    print("test_extract_order_and_dedupe: OK")


def test_blank_is_valid():
    assert pt.validate("", pt.CHAPTER_USER_ALLOWLIST) == []
    assert pt.validate("   \n ", pt.CHAPTER_USER_ALLOWLIST) == []
    assert pt.render("", {"x": "y"}) == ""
    print("test_blank_is_valid: OK")


def test_validate_allowlist():
    names = pt.validate("{{target_words}} {{source_md}}", pt.CHAPTER_USER_ALLOWLIST)
    assert names == ["target_words", "source_md"]
    try:
        pt.validate("{{nope}}", pt.CHAPTER_USER_ALLOWLIST)
    except pt.UnknownPlaceholder as exc:
        assert exc.names == ["nope"]
        assert "source_md" in exc.allowlist
    else:
        raise AssertionError("expected UnknownPlaceholder")
    print("test_validate_allowlist: OK")


def test_unbalanced_rejected():
    for bad in ("{{target_words", "target_words}}", "{{a {{b}}", "}}x"):
        try:
            pt.validate(bad, pt.CHAPTER_USER_ALLOWLIST)
        except pt.UnbalancedTemplate:
            pass
        else:
            raise AssertionError(f"expected UnbalancedTemplate for {bad!r}")
    print("test_unbalanced_rejected: OK")


def test_render_substitution_and_missing():
    out = pt.render("Target {{target_words}} ({{low}}-{{high}})\n{{source_md}}", {
        "target_words": 1200, "low": 1140, "high": 1260, "source_md": "SRC",
    })
    assert out == "Target 1200 (1140-1260)\nSRC"
    try:
        pt.render("{{missing}}", {"other": 1})
    except pt.MissingValue as exc:
        assert exc.names == ["missing"]
    else:
        raise AssertionError("expected MissingValue")
    print("test_render_substitution_and_missing: OK")


def test_single_braces_untouched():
    out = pt.render("keep {this} {{target_words}}", {"target_words": 5})
    assert out == "keep {this} 5"
    print("test_single_braces_untouched: OK")


def test_components_expansion_is_plain_value():
    out = pt.render("HEAD\n{{components}}\nTAIL", {"components": "comp-a\ncomp-b"})
    assert out == "HEAD\ncomp-a\ncomp-b\nTAIL"
    print("test_components_expansion_is_plain_value: OK")


def main():
    tests = [
        test_extract_order_and_dedupe,
        test_blank_is_valid,
        test_validate_allowlist,
        test_unbalanced_rejected,
        test_render_substitution_and_missing,
        test_single_braces_untouched,
        test_components_expansion_is_plain_value,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} PROMPT-TEMPLATE TESTS PASSED")


if __name__ == "__main__":
    main()
