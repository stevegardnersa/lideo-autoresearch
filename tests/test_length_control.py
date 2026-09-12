"""Offline tests for target-aware generation-time length control.

Run with: python3 tests/test_length_control.py

Covers spec `specs/target-aware-length-control.md` (fed4164):
  * Fix A: `visible_max_tokens` derives a per-pass `max_tokens` from the
    visible-word target (plus reasoning reservation), capped and floored;
    `build_openrouter_request` honors the per-request override; every generated
    pass request carries the target-aware cap.
  * Fix B: chapter and composer repair prompts carry the measured current word
    count and an exact cut/add delta when `current_word_count` is provided, and
    are byte-identical to the legacy prompts when it is not.

Expected token values below are the §4.1 formula applied literally: ceil words,
x1.4, +200 overhead, /(1-effort_fraction), ceil, clamp. The §4.3 table in
`specs/target-aware-length-control.md` is aligned to these values.
"""

import dataclasses
import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import run_candidate as rc
from core.openrouter_client import GenerationResult, UsageRecord
from core.reasoning import (
    DEFAULT_MIN_OUTPUT_TOKENS,
    stage_reasoning_effort,
    visible_max_tokens,
)

ROOT = Path(__file__).resolve().parent.parent
PROFILE = "30m_gpt-5.6-luna_effort-minimal"
LENGTH_CONTROL_SPEC = ROOT / "specs" / "target-aware-length-control.md"


class FakeClient:
    """Minimal fake: returns a summary whose length depends on the call index."""

    def __init__(self, word_counts: List[int]):
        self.word_counts = word_counts
        self.calls = 0
        self.max_in_flight = 0
        self.in_flight = 0
        self._lock = threading.Lock()
        self.function_url = "http://localhost/fake"
        self.auth_mode = "none"
        self.last_meta = {}
        self.scoring_version_mismatch = False

    def chat_completion(self, request_body, **kwargs):
        with self._lock:
            self.calls += 1
            index = min(self.calls - 1, len(self.word_counts) - 1)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            count = self.word_counts[index]
            summary = ("word " * count).strip()
            payload = json.dumps({"summary_md": summary, "estimated_visible_words": count})
            return GenerationResult(
                summary_md=summary,
                estimated_visible_words=count,
                raw_content=payload,
                usage=UsageRecord(),
                raw_response={"mock": True},
            )
        finally:
            with self._lock:
                self.in_flight -= 1


def _stub_module(request_log: list):
    """Candidate module view for run_length_controlled_stage: real prompts, recording request builder."""
    log = request_log

    def build_openrouter_request(*, stage, system_prompt, user_prompt, schema_name, use_json_schema=True, max_tokens=None):
        log.append(
            {
                "max_tokens": max_tokens,
                "stage_max_tokens": stage.max_tokens,
                "user_prompt": user_prompt,
            }
        )
        return {"model": stage.model, "max_tokens": max_tokens, "messages": []}

    module = SimpleNamespace(
        build_openrouter_request=build_openrouter_request,
        render_chapter_system=candidate_spec.render_chapter_system,
        render_chapter_user=candidate_spec.render_chapter_user,
        render_repair_user=candidate_spec.render_repair_user,
        render_composer_user=candidate_spec.render_composer_user,
        render_composer_system=candidate_spec.render_composer_system,
        visible_word_range=candidate_spec.visible_word_range,
        REPAIR_MORE_POLICIES=candidate_spec.REPAIR_MORE_POLICIES,
        REPAIR_LESS_POLICIES=candidate_spec.REPAIR_LESS_POLICIES,
        FORMAT_INSTRUCTIONS=candidate_spec.FORMAT_INSTRUCTIONS,
    )
    return module


# --------------------------------------------------------------------------- #
# Fix A — visible_max_tokens
# --------------------------------------------------------------------------- #


def test_visible_max_tokens_table_and_contract():
    # Code-block formula values (normative; see module docstring re prose table).
    assert visible_max_tokens(353, effort="minimal", cap=9103) == 1102
    assert visible_max_tokens(353, effort="medium", cap=16384) == 1982
    assert visible_max_tokens(1288, effort="minimal", cap=9103) == 3429
    assert visible_max_tokens(1288, effort="medium", cap=16384) == 6171

    # Contract: reasoning inflation keeps the request under the stage budget
    # (luna stage.max_tokens 9103 minimal / 16384 medium).
    assert visible_max_tokens(353, effort="minimal", cap=9103) < 9103
    assert visible_max_tokens(353, effort="medium", cap=16384) < 16384
    assert visible_max_tokens(1288, effort="minimal", cap=9103) < 9103
    assert visible_max_tokens(1288, effort="medium", cap=16384) < 16384

    # Reasoning inflation: effort raises the budget; none/None do not.
    base = visible_max_tokens(353, effort="none", cap=16384)
    assert visible_max_tokens(353, effort=None, cap=16384) == base == 991
    assert visible_max_tokens(353, effort="medium", cap=16384) > base
    assert visible_max_tokens(353, effort="minimal", cap=9103) > base

    # Monotonic non-decreasing in target_words.
    for effort in ("none", "minimal", "medium"):
        prev = 0
        for t in range(100, 2001):
            value = visible_max_tokens(t, effort=effort, cap=16384)
            assert value >= prev, (effort, t, value, prev)
            prev = value

    # Cap and floor.
    assert visible_max_tokens(10_000, cap=16384) == 16384
    assert visible_max_tokens(10_000_000, cap=16384) == 16384
    assert visible_max_tokens(10, effort="none") == DEFAULT_MIN_OUTPUT_TOKENS == 512
    assert visible_max_tokens(0, effort="none") >= DEFAULT_MIN_OUTPUT_TOKENS
    print("test_visible_max_tokens_table_and_contract: OK")


_SPEC_CALL_RE = re.compile(
    r'visible_max_tokens\(\s*(?P<args>[^()]*?)\s*\)\s*==\s*(?P<expected>\d+)'
)


def _eval_spec_call(args: str) -> int:
    """Evaluate one ``visible_max_tokens(...)`` call written in the spec table."""
    target = None
    effort = None
    cap = None
    for part in args.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = (piece.strip() for piece in part.split("=", 1))
            value = value.strip().strip('"').strip("'")
            if key == "effort":
                effort = value
            elif key == "cap":
                cap = int(value)
            else:
                raise AssertionError(f"unexpected visible_max_tokens kwarg in spec: {key!r}")
        else:
            target = int(part)
    assert target is not None, f"visible_max_tokens call missing target: {args!r}"
    kwargs = {}
    if effort is not None:
        kwargs["effort"] = effort
    if cap is not None:
        kwargs["cap"] = cap
    return visible_max_tokens(target, **kwargs)


def test_spec_4_3_table_matches_formula():
    """§4.3's table must equal what the formula computes, so spec/runtime drift fails CI.

    Parses every ``visible_max_tokens(...) == N`` in the §4.3 subsection and
    evaluates each call through the real function. If the spec's numbers are
    edited without the code (or vice versa), this fails.
    """
    text = LENGTH_CONTROL_SPEC.read_text(encoding="utf-8")
    marker = "### 4.3"
    assert marker in text, f"spec section {marker!r} not found in {LENGTH_CONTROL_SPEC}"
    tail = text.split(marker, 1)[1]
    next_heading = re.search(r"\n#{2,3} ", tail)
    section = tail[: next_heading.start()] if next_heading else tail

    matches = list(_SPEC_CALL_RE.finditer(section))
    assert len(matches) >= 6, (
        f"expected >=6 visible_max_tokens assertions in §4.3, found {len(matches)}: "
        f"{[m.group(0) for m in matches]}"
    )

    checked = {}
    for match in matches:
        label = match.group(0)
        expected = int(match.group("expected"))
        actual = _eval_spec_call(match.group("args"))
        assert actual == expected, f"spec §4.3 drift: `{label}` but formula gives {actual}"
        checked[label] = actual

    # The four effort/cap cases must remain present (not silently dropped on rewrite).
    for target, effort, cap in (
        (353, "minimal", 9103),
        (353, "medium", 16384),
        (1288, "minimal", 9103),
        (1288, "medium", 16384),
    ):
        assert any(line.startswith(f"visible_max_tokens({target},") for line in checked), (
            target,
            effort,
            cap,
        )
    print(f"test_spec_4_3_table_matches_formula: OK ({len(checked)} assertions)")


def test_build_openrouter_request_override():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    stage = spec.chapter_stage
    request_default = candidate_spec.build_openrouter_request(
        stage=stage,
        system_prompt="sys",
        user_prompt="user",
        schema_name=spec.json_schema_name,
        use_json_schema=False,
    )
    assert request_default["max_tokens"] == stage.max_tokens

    request_capped = candidate_spec.build_openrouter_request(
        stage=stage,
        system_prompt="sys",
        user_prompt="user",
        schema_name=spec.json_schema_name,
        use_json_schema=False,
        max_tokens=1102,
    )
    assert request_capped["max_tokens"] == 1102
    # Override must not perturb the rest of the body.
    assert request_capped["model"] == request_default["model"]
    assert request_capped["messages"] == request_default["messages"]
    assert request_capped["temperature"] == request_default["temperature"]
    print("test_build_openrouter_request_override: OK")


# --------------------------------------------------------------------------- #
# Fix B — numeric repair prompts
# --------------------------------------------------------------------------- #


def test_render_repair_user_delta_block():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    target = 1288
    low, high = candidate_spec.visible_word_range(target, spec.length_control.tolerance_pct)

    # direction="less": current 2267 (over), cut = 2267 - high.
    less_prompt = candidate_spec.render_repair_user(
        spec,
        source_md="src",
        current_summary_md="word " * 10,
        target_words=target,
        direction="less",
        current_word_count=2267,
    )
    assert "Current summary is 2267 visible words" in less_prompt, less_prompt
    cut = max(0, 2267 - high)
    assert f"Cut approximately {cut} words" in less_prompt, less_prompt
    assert f"[{low}, {high}]" in less_prompt, less_prompt
    assert "Count the visible words of your output before finishing" in less_prompt

    # direction="more": current 900 (under), add = low - 900.
    more_prompt = candidate_spec.render_repair_user(
        spec,
        source_md="src",
        current_summary_md="word " * 10,
        target_words=target,
        direction="more",
        current_word_count=900,
    )
    assert "Current summary is 900 visible words" in more_prompt
    add = max(0, low - 900)
    assert f"Add approximately {add} words" in more_prompt, more_prompt

    # None -> legacy byte-identical output (no delta block).
    legacy = candidate_spec.render_repair_user(
        spec,
        source_md="src",
        current_summary_md="word " * 10,
        target_words=target,
        direction="less",
    )
    assert "Current summary is" not in legacy
    assert "Cut approximately" not in legacy
    assert "Target visible words: 1288." in legacy
    print("test_render_repair_user_delta_block: OK")


def test_render_composer_repair_user_delta_block():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    target = 1288
    low, high = candidate_spec.visible_word_range(target, spec.length_control.tolerance_pct)

    less_prompt = rc.render_composer_repair_user(
        candidate_spec,
        spec,
        chapter_summaries_md="## ch1\n\nbody",
        current_summary_md="word " * 10,
        target_words=target,
        direction="less",
        book_title="Book",
        toc_md="# toc",
        book_metadata="meta",
        retrieved_source_excerpts="",
        current_word_count=2267,
    )
    assert "Current summary is 2267 visible words" in less_prompt
    assert f"Cut approximately {max(0, 2267 - high)} words" in less_prompt
    assert f"[{low}, {high}]" in less_prompt

    more_prompt = rc.render_composer_repair_user(
        candidate_spec,
        spec,
        chapter_summaries_md="## ch1\n\nbody",
        current_summary_md="word " * 10,
        target_words=target,
        direction="more",
        book_title="Book",
        toc_md="# toc",
        book_metadata="meta",
        retrieved_source_excerpts="",
        current_word_count=900,
    )
    assert f"Add approximately {max(0, low - 900)} words" in more_prompt

    legacy = rc.render_composer_repair_user(
        candidate_spec,
        spec,
        chapter_summaries_md="## ch1\n\nbody",
        current_summary_md="word " * 10,
        target_words=target,
        direction="less",
        book_title="Book",
        toc_md="# toc",
        book_metadata="meta",
        retrieved_source_excerpts="",
    )
    assert "Current summary is" not in legacy
    assert "Target visible words: 1288." in legacy
    print("test_render_composer_repair_user_delta_block: OK")


# --------------------------------------------------------------------------- #
# Integration — every pass request carries the cap; repair carries the delta
# --------------------------------------------------------------------------- #


def test_stage_passes_carry_target_aware_cap_and_delta():
    spec = dataclasses.replace(
        candidate_spec.PROFILE_CANDIDATES[PROFILE],
        length_control=dataclasses.replace(
            candidate_spec.PROFILE_CANDIDATES[PROFILE].length_control,
            max_passes=2,
        ),
    )
    target = 353
    stage = spec.chapter_stage
    expected_cap = visible_max_tokens(
        target,
        effort=stage_reasoning_effort(stage),
        cap=int(stage.max_tokens),
    )
    # First pass overshoots (500 words > high), repair lands in-range (350 words).
    client = FakeClient(word_counts=[500, 350])

    request_log: list = []
    stage_run = rc.run_length_controlled_stage(
        candidate_module=_stub_module(request_log),
        spec=spec,
        stage_kind="chapter",
        stage_config=stage,
        system_prompt="system",
        initial_user_prompt="Write the summary.",
        target_words=target,
        mock_source_md="source " * 60,
        client=client,
        current_book_title="Book",
        current_chapter_title="Ch1",
    )

    assert client.calls == 2, client.calls
    assert len(request_log) == 2, request_log
    # Every generated pass request carries the target-aware cap (not the 9103 stage budget).
    for entry in request_log:
        assert entry["max_tokens"] == expected_cap, (entry, expected_cap)
        assert entry["max_tokens"] < entry["stage_max_tokens"], entry
    # The repair pass prompt carries the measured word count and the cut delta.
    low, high = candidate_spec.visible_word_range(target, spec.length_control.tolerance_pct)
    repair_prompt = request_log[1]["user_prompt"]
    assert "Current summary is 500 visible words" in repair_prompt, repair_prompt
    assert f"Cut approximately {500 - high} words" in repair_prompt, repair_prompt
    # Final stage run used the in-range summary.
    assert rc.visible_word_count(stage_run.summary_md) == 350
    assert stage_run.passes_used == 2
    print("test_stage_passes_carry_target_aware_cap_and_delta: OK")


def main():
    tests = [
        test_visible_max_tokens_table_and_contract,
        test_spec_4_3_table_matches_formula,
        test_build_openrouter_request_override,
        test_render_repair_user_delta_block,
        test_render_composer_repair_user_delta_block,
        test_stage_passes_carry_target_aware_cap_and_delta,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} LENGTH-CONTROL TESTS PASSED")


if __name__ == "__main__":
    main()