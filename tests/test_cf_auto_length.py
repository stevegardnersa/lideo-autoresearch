"""Offline tests for the Cloud Function `auto_length` loop.

Run with: python3 tests/test_cf_auto_length.py

Covers:
  * `core.repair_prompt` templates render byte-identically to
    `candidate_spec.render_repair_user` (the registry bake must not drift).
  * registry version records round-trip `length_control` + `repair_templates`.
  * the shipped bundle carries baked length control + repair templates.
  * `handler.run_summarize(auto_length=True)` runs the loop, returns the best
    pass, aggregates cost, and reports pass metadata — no network.
"""

import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import prompt_registry, repair_prompt
from core.length_control import LengthControlConfig
from core.openrouter_client import GenerationResult, UsageRecord

import cloud_function.handler as handler

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "cloud_function" / "prompts" / "registry.json"
PROFILE = "30m_gpt-5.6-luna_effort-minimal"


class FakeClient:
    """Returns a summary whose length depends on the call index."""

    def __init__(self, word_counts: List[int]):
        self.word_counts = word_counts
        self.calls = 0
        self.requests: List[dict] = []

    def chat_completion(self, payload):
        self.requests.append(payload)
        count = self.word_counts[min(self.calls, len(self.word_counts) - 1)]
        self.calls += 1
        summary = ("word " * count).strip()
        parsed = {"summary_md": summary, "estimated_visible_words": count}
        return GenerationResult(
            summary_md=summary,
            estimated_visible_words=count,
            raw_content=json.dumps(parsed),
            usage=UsageRecord(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                generation_cost=0.001,
                uncached_generation_cost=0.001,
                generation_id=f"gen-{self.calls}",
                provider_name="fake",
                model_id="fake/model",
            ),
            raw_response={"usage": {"cost": 0.001}},
            parsed_json=parsed,
            finish_reason="stop",
            json_recovery="exact",
        )


def _tmp_registry() -> Path:
    return Path(tempfile.mkdtemp(prefix="cf-auto-length-")) / "registry.json"


# --------------------------------------------------------------------------- #
# Template parity
# --------------------------------------------------------------------------- #


def _chapter_sentence_note(spec, target_words):
    stage = spec.chapter_stage
    if not stage.prompt_components.get("length_policy"):
        return None
    budget = candidate_spec.sentence_budget(
        target_words,
        words_per_sentence=candidate_spec._sentence_target_words(stage),
        scale=candidate_spec._sentence_budget_scale(stage),
    )
    return f" The sentence budget is {budget}; adjust sentence count as well as word count."


def test_repair_templates_match_candidate_spec():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    lc = spec.length_control
    fmt = candidate_spec.FORMAT_INSTRUCTIONS[spec.chapter_stage.format_mode]
    for direction, policy_table, policy_id in (
        ("more", candidate_spec.REPAIR_MORE_POLICIES, lc.repair_more_prompt_id),
        ("less", candidate_spec.REPAIR_LESS_POLICIES, lc.repair_less_prompt_id),
    ):
        template = repair_prompt.chapter_repair_template(
            policy=policy_table[policy_id], format_instructions=fmt
        )
        for current in (None, 2267):
            rendered = repair_prompt.render_repair_prompt(
                template,
                direction=direction,
                target_words=1288,
                current_word_count=current,
                current_summary_md="word " * 10,
                source_md="src",
                book_title="B",
                chapter_title="C",
                tolerance_pct=lc.tolerance_pct,
                repair_target_mode=lc.repair_target_mode,
                repair_min_reduction_pct=lc.repair_min_reduction_pct,
                repair_sentence_delete=lc.repair_sentence_delete,
                sentence_budget_note=_chapter_sentence_note(spec, 1288),
            )
            expected = candidate_spec.render_repair_user(
                spec,
                source_md="src",
                current_summary_md="word " * 10,
                target_words=1288,
                direction=direction,
                book_title="B",
                chapter_title="C",
                current_word_count=current,
            )
            assert rendered == expected, (direction, current, rendered, expected)
    print("test_repair_templates_match_candidate_spec: OK")


def test_composer_template_matches_candidate_spec():
    from core import run_candidate as rc

    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    expected = rc.render_composer_repair_user(
        candidate_spec,
        spec,
        chapter_summaries_md="## ch1\n\nbody",
        current_summary_md="word " * 10,
        target_words=1288,
        direction="less",
        book_title="Book",
        toc_md="# toc",
        book_metadata="meta",
        retrieved_source_excerpts="",
        current_word_count=2267,
    )
    lc = spec.length_control
    template = repair_prompt.composer_repair_template(
        policy=candidate_spec.REPAIR_LESS_POLICIES[lc.repair_less_prompt_id],
        format_instructions=candidate_spec.FORMAT_INSTRUCTIONS[spec.composer_stage.format_mode],
    )
    note = None
    if spec.composer_stage.prompt_components.get("length_policy"):
        budget = candidate_spec.sentence_budget(
            1288,
            words_per_sentence=candidate_spec._sentence_target_words(spec.composer_stage),
            scale=candidate_spec._sentence_budget_scale(spec.composer_stage),
        )
        note = f" The sentence budget is {budget}; adjust sentence count as well as word count."
    rendered = repair_prompt.render_repair_prompt(
        template,
        direction="less",
        target_words=1288,
        current_word_count=2267,
        current_summary_md="word " * 10,
        book_title="Book",
        tolerance_pct=lc.tolerance_pct,
        repair_target_mode=lc.repair_target_mode,
        repair_min_reduction_pct=lc.repair_min_reduction_pct,
        repair_sentence_delete=lc.repair_sentence_delete,
        sentence_budget_note=note,
        extra_values={
            "chapter_summaries_md": "## ch1\n\nbody",
            "toc_line": "Table of contents:\n# toc",
            "metadata_line": "Book metadata:\nmeta",
            "excerpts_line": "",
        },
    )
    assert rendered == expected, (rendered, expected)
    print("test_composer_template_matches_candidate_spec: OK")


# --------------------------------------------------------------------------- #
# Registry schema
# --------------------------------------------------------------------------- #


def test_registry_round_trips_length_control_and_templates():
    path = _tmp_registry()
    prompt_registry.load_registry(path)
    lc = dataclasses.asdict(LengthControlConfig(max_passes=3, tolerance_pct=0.08))
    templates = {"more": "M {{source_md}}", "less": "L {{source_md}}"}
    version = prompt_registry.create_version(
        stage="chapter",
        label="baked",
        components={},
        length_control=lc,
        repair_templates=templates,
        path=path,
    )
    loaded = prompt_registry.get_version(version["id"], path)
    assert loaded["length_control"]["max_passes"] == 3
    assert loaded["length_control"]["tolerance_pct"] == 0.08
    assert loaded["repair_templates"] == templates

    revised = prompt_registry.revise_version(version["id"], label="copy", path=path)
    assert revised["length_control"]["max_passes"] == 3
    assert revised["repair_templates"] == templates
    print("test_registry_round_trips_length_control_and_templates: OK")


def test_bundle_production_version_is_baked():
    registry = prompt_registry.load_registry(BUNDLE, seed=False)
    prod_id = ((registry.get("slots") or {}).get("chapter", {}).get("general") or {}).get("production")
    assert prod_id, "chapter production slot must be assigned"
    version = prompt_registry.get_version(prod_id, BUNDLE)
    assert version is not None, prod_id
    assert isinstance(version.get("length_control"), dict) and version["length_control"]
    templates = version.get("repair_templates") or {}
    assert "more" in templates and "less" in templates
    assert "{{target_words}}" in templates["less"]
    assert "{{current_summary_md}}" in templates["less"]
    # Baked templates must render with the CF's runtime values.
    rendered = repair_prompt.render_repair_prompt(
        templates["less"],
        direction="less",
        target_words=400,
        current_word_count=500,
        current_summary_md="body",
        source_md="src",
    )
    assert "Delete at least" in rendered
    assert "body" in rendered and "src" in rendered
    print("test_bundle_production_version_is_baked: OK")


# --------------------------------------------------------------------------- #
# handler.run_summarize(auto_length=True)
# --------------------------------------------------------------------------- #


def _patch_client(client):
    original = handler._build_client
    handler._build_client = lambda api_key="", base_url="", timeout=600: client
    return original


def test_auto_length_loop_returns_best_pass_and_aggregates_cost():
    client = FakeClient([500, 350])
    original = _patch_client(client)
    try:
        result = handler.run_summarize(
            {
                "source_md": "## Ch\n\nSome source text.",
                "target_words": 353,
                "auto_length": True,
                "prompt_version": "chapter-p18",
            }
        )
    finally:
        handler._build_client = original

    assert result["success"] is True
    assert client.calls == 2, client.calls
    assert result["summary"]["estimated_visible_words"] == 350
    assert result["meta"]["auto_length"] is True
    assert result["meta"]["passes_used"] == 2
    assert result["meta"]["first_pass_visible_words"] == 500
    assert result["meta"]["final_visible_words"] == 350
    assert abs(result["usage"]["generation_cost"] - 0.002) < 1e-9
    assert abs(result["usage"]["total_cost"] - 0.002) < 1e-9
    assert result["usage"]["iterations"] == 2
    # The repair pass carried the measured delta.
    repair_user = client.requests[1]["messages"][1]["content"]
    assert "Current summary is 500 visible words" in repair_user
    assert "Delete at least" in repair_user
    print("test_auto_length_loop_returns_best_pass_and_aggregates_cost: OK")


def test_auto_length_body_override_max_passes():
    client = FakeClient([900, 350])
    original = _patch_client(client)
    try:
        result = handler.run_summarize(
            {
                "source_md": "src",
                "target_words": 353,
                "auto_length": True,
                "length_control": {"max_passes": 1},
                "prompt_version": "chapter-p18",
            }
        )
    finally:
        handler._build_client = original
    assert client.calls == 1
    assert result["meta"]["passes_used"] == 1
    print("test_auto_length_body_override_max_passes: OK")


def test_auto_length_validation():
    for body in (
        {"source_md": "src", "target_words": 353, "auto_length": True, "summary_md": "s", "rubric": {"key_terms": ["x"]}},
        {"source_md": "src", "auto_length": True, "prompt_version": "chapter-p18"},
    ):
        try:
            handler.run_summarize(body)
        except handler.PromptVersionError as exc:
            assert exc.status_code == 400, exc.status_code
            assert exc.error == "invalid_auto_length"
        else:
            raise AssertionError(f"expected PromptVersionError for {body}")
    print("test_auto_length_validation: OK")


def test_single_pass_reports_iterations_and_total_cost():
    client = FakeClient([350])
    original = _patch_client(client)
    try:
        result = handler.run_summarize(
            {"source_md": "src", "target_words": 353, "prompt_version": "chapter-p18"}
        )
    finally:
        handler._build_client = original
    assert result["usage"]["iterations"] == 1
    assert abs(result["usage"]["generation_cost"] - 0.001) < 1e-9
    assert abs(result["usage"]["total_cost"] - 0.001) < 1e-9
    print("test_single_pass_reports_iterations_and_total_cost: OK")


class JudgeFakeClient(FakeClient):
    """Returns a valid absolute-judge payload with a known raw usage cost."""

    def chat_completion(self, payload):
        self.requests.append(payload)
        self.calls += 1
        parsed = {
            "faithfulness": 0.9,
            "concept_coverage": 0.8,
            "qualifier_preservation": 0.7,
            "no_fluff": 0.6,
            "structure_quality": 0.5,
            "rationale": "ok",
        }
        return GenerationResult(
            summary_md="",
            estimated_visible_words=0,
            raw_content=json.dumps(parsed),
            usage=UsageRecord(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                generation_cost=0.0,
                uncached_generation_cost=0.0,
                generation_id="judge-1",
                provider_name="fake",
                model_id="fake/judge",
            ),
            raw_response={"usage": {"cost": 0.001}},
            parsed_json=parsed,
            finish_reason="stop",
            json_recovery="exact",
        )


def test_judge_only_total_cost_includes_judge():
    client = JudgeFakeClient([350])
    original = _patch_client(client)
    try:
        result = handler.run_summarize(
            {
                "source_md": "src",
                "summary_md": "word " * 100,
                "judge": True,
                "rubric": {"key_terms": ["word"]},
                "prompt_version": "chapter-p18",
            }
        )
    finally:
        handler._build_client = original
    assert result["usage"]["iterations"] == 1
    assert abs(result["usage"]["judge_generation_cost"] - 0.001) < 1e-9
    assert abs(result["usage"]["total_cost"] - 0.001) < 1e-9
    print("test_judge_only_total_cost_includes_judge: OK")


def main() -> None:
    tests = [
        test_repair_templates_match_candidate_spec,
        test_composer_template_matches_candidate_spec,
        test_registry_round_trips_length_control_and_templates,
        test_bundle_production_version_is_baked,
        test_auto_length_loop_returns_best_pass_and_aggregates_cost,
        test_auto_length_body_override_max_passes,
        test_auto_length_validation,
        test_single_pass_reports_iterations_and_total_cost,
        test_judge_only_total_cost_includes_judge,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} CF AUTO-LENGTH TESTS PASSED")


if __name__ == "__main__":
    main()
