"""Offline tests for the prompt-version registry.

Run with: python3 tests/test_prompt_registry.py

Covers spec `specs/system-prompt-v2-versioned-routing.md` §16 tests 1-8.
"""

import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import prompt_registry as pr
from core import run_candidate as rc
from core.openrouter_client import GenerationResult, UsageRecord
from core.reasoning import stage_reasoning_effort, visible_max_tokens

PROFILE = "30m_gpt-5.6-luna_effort-minimal"


def _tmp_registry() -> Path:
    d = tempfile.mkdtemp(prefix="prompt-registry-")
    return Path(d) / "registry.json"


# 1 — seeding
def test_seeding():
    path = _tmp_registry()
    registry = pr.load_registry(path)
    ids = [v["id"] for v in registry["versions"]]
    assert ids == ["chapter-p1", "chapter-p2", "composer-p1"], ids
    assert registry["slots"]["chapter"] == {"local": "chapter-p1", "production": "chapter-p1"}
    assert registry["slots"]["composer"] == {"local": "composer-p1", "production": "composer-p1"}
    p2 = pr.get_version("chapter-p2", path)
    assert p2 is not None
    assert p2["components"].get("length_policy") == "explicit_budget_first"
    assert path.exists()
    print("test_seeding: OK")


# 2 — fingerprint determinism
def test_fingerprint_determinism():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    t1, f1 = pr.render_system(spec=spec, stage_kind="chapter", prompt_version=None)
    t2, f2 = pr.render_system(spec=spec, stage_kind="chapter", prompt_version=None)
    assert (t1, f1) == (t2, f2)
    path = _tmp_registry()
    pr.load_registry(path)
    tp1, fp1 = pr.render_system(spec=spec, stage_kind="chapter", prompt_version="chapter-p1", path=path)
    tp2, fp2 = pr.render_system(spec=spec, stage_kind="chapter", prompt_version="chapter-p2", path=path)
    assert fp1 != fp2
    # p1 is a pure passthrough: byte-identical to the unversioned baseline.
    assert tp1 == t1 and fp1 == f1
    assert "explicit_budget_first" not in tp1
    assert "Plan the entire summary against the stated word and sentence budgets" in tp2
    print("test_fingerprint_determinism: OK")


# 3 — immutability (label/notes only; content is never mutated)
def test_immutability():
    path = _tmp_registry()
    pr.load_registry(path)
    before = pr.get_version("chapter-p2", path)
    after = pr.update_version("chapter-p2", label="renamed", notes="note", path=path)
    assert after["label"] == "renamed"
    assert after["notes"] == "note"
    assert after["components"] == before["components"]
    assert after["template"] == before["template"]
    assert after["immutable"] is True
    print("test_immutability: OK")


# 4 — precedence + byte-identical baseline
def test_precedence_and_baseline():
    spec = dataclasses.replace(candidate_spec.PROFILE_CANDIDATES[PROFILE])
    spec.chapter_stage = dataclasses.replace(
        spec.chapter_stage, prompt_components=dict(spec.chapter_stage.prompt_components)
    )
    # Simulate a permutation override layer.
    spec.chapter_stage.prompt_components["anti_fluff_policy"] = "medium"
    baseline = candidate_spec.render_chapter_system(spec)
    assert "Favor information density" in baseline

    path = _tmp_registry()
    pr.load_registry(path)
    # No version -> unchanged profile+permutation render.
    assert candidate_spec.render_chapter_system(spec) == baseline
    # Version overlay beats permutation for the keys it defines.
    components = pr.resolve_components(
        stage="chapter",
        profile_components=spec.chapter_stage.prompt_components,
        prompt_version="chapter-p2",
        path=path,
    )
    assert components["anti_fluff_policy"] == "medium"  # falls back to profile
    assert components["length_policy"] == "explicit_budget_first"
    overlaid = candidate_spec.render_chapter_system(spec, components=components)
    assert "Favor information density" in overlaid
    assert "Plan the entire summary against the stated word and sentence budgets" in overlaid
    print("test_precedence_and_baseline: OK")


# 5 — slot resolution
def test_slot_resolution():
    path = _tmp_registry()
    pr.load_registry(path)
    pr.set_slot(stage="chapter", slot="local", version_id="chapter-p2", path=path)
    assert pr.resolve_version_id(stage="chapter", slot="local", path=path) == "chapter-p2"
    assert pr.resolve_version_id(stage="chapter", prompt_version="chapter-p2", path=path) == "chapter-p2"
    assert pr.resolve_version_id(stage="composer", slot="local", path=path) == "composer-p1"
    try:
        pr.resolve_version_id(stage="chapter", prompt_version="chapter-nope", path=path)
    except pr.UnknownVersionError as exc:
        assert "chapter-p1" in exc.allowed
    else:
        raise AssertionError("expected UnknownVersionError")
    print("test_slot_resolution: OK")


# 6 — v2 prompt content + sentence line gating
def test_v2_prompt_content():
    spec = dataclasses.replace(candidate_spec.PROFILE_CANDIDATES[PROFILE])
    spec.chapter_stage = dataclasses.replace(
        spec.chapter_stage, prompt_components=dict(spec.chapter_stage.prompt_components)
    )
    legacy_user = candidate_spec.render_chapter_user(
        spec, source_md="src", target_words=360, book_title="B"
    )
    assert "Target sentences:" not in legacy_user

    spec.chapter_stage.prompt_components["length_policy"] = "explicit_budget_first"
    v2_user = candidate_spec.render_chapter_user(spec, source_md="src", target_words=360, book_title="B")
    s_expected = candidate_spec.sentence_budget(360)
    assert f"Target sentences: {s_expected}" in v2_user
    assert f"do not exceed {s_expected} sentences" in v2_user
    v2_system = candidate_spec.render_chapter_system(spec)
    assert "Plan the entire summary against the stated word and sentence budgets" in v2_system
    print("test_v2_prompt_content: OK")


# 7 — effective cap reconciliation
def test_effective_cap_reconciliation():
    spec = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    stage = spec.chapter_stage
    for target in (150, 353, 1288, 4000):
        base = visible_max_tokens(target, effort=stage_reasoning_effort(stage), cap=int(stage.max_tokens))
        floor = max(1, int(round(target * 1.35 * 1.15)))
        effective = min(int(stage.max_tokens), max(base, floor))
        assert effective <= stage.max_tokens
        assert effective >= base
        assert effective >= floor
    # Tight profile budget: the floor is clamped by max_tokens.
    tight = dataclasses.replace(stage, max_tokens=600)
    base = visible_max_tokens(2000, effort=stage_reasoning_effort(tight), cap=600)
    floor = max(1, int(round(2000 * 1.35 * 1.15)))
    assert min(600, max(base, floor)) == 600
    print("test_effective_cap_reconciliation: OK")


# 8 — latency per pass + aggregation
class FakeClient:
    def __init__(self):
        self.function_url = "http://localhost/fake"
        self.auth_mode = "none"
        self.last_meta = {}
        self.scoring_version_mismatch = False

    def chat_completion(self, request_body, **kwargs):
        return GenerationResult(
            summary_md="word " * 300,
            estimated_visible_words=300,
            raw_content=json.dumps({"summary_md": "word " * 300, "estimated_visible_words": 300}),
            usage=UsageRecord(),
            raw_response={"mock": True},
        )


def test_latency_fields():
    spec = dataclasses.replace(
        candidate_spec.PROFILE_CANDIDATES[PROFILE],
        length_control=dataclasses.replace(
            candidate_spec.PROFILE_CANDIDATES[PROFILE].length_control, max_passes=2
        ),
    )
    module = SimpleNamespace(
        build_openrouter_request=lambda **kw: {"model": kw["stage"].model, "messages": []},
        render_repair_user=candidate_spec.render_repair_user,
        render_chapter_user=candidate_spec.render_chapter_user,
        render_chapter_system=candidate_spec.render_chapter_system,
        visible_word_range=candidate_spec.visible_word_range,
        REPAIR_MORE_POLICIES=candidate_spec.REPAIR_MORE_POLICIES,
        REPAIR_LESS_POLICIES=candidate_spec.REPAIR_LESS_POLICIES,
        FORMAT_INSTRUCTIONS=candidate_spec.FORMAT_INSTRUCTIONS,
    )
    rc._LATENCY_ACCUMULATOR.clear()
    stage_run = rc.run_length_controlled_stage(
        candidate_module=module,
        spec=spec,
        stage_kind="chapter",
        stage_config=spec.chapter_stage,
        system_prompt="sys",
        initial_user_prompt="write",
        target_words=300,
        mock_source_md="source " * 60,
        client=FakeClient(),
    )
    assert len(stage_run.pass_latency_seconds) == stage_run.passes_used
    assert all(v >= 0 for v in stage_run.pass_latency_seconds)
    assert len(rc._LATENCY_ACCUMULATOR) == stage_run.passes_used
    # Serialize/deserialize round-trip preserves latencies.
    payload = rc.serialize_stage_run(stage_run)
    restored = rc.deserialize_stage_run(payload)
    assert restored.pass_latency_seconds == stage_run.pass_latency_seconds
    print("test_latency_fields: OK")


def main():
    tests = [
        test_seeding,
        test_fingerprint_determinism,
        test_immutability,
        test_precedence_and_baseline,
        test_slot_resolution,
        test_v2_prompt_content,
        test_effective_cap_reconciliation,
        test_latency_fields,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} PROMPT-REGISTRY TESTS PASSED")


if __name__ == "__main__":
    main()
