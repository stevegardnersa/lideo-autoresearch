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
    assert registry["slots"]["chapter"]["general"] == {"local": "chapter-p1", "production": "chapter-p1"}
    assert registry["slots"]["chapter"]["models"] == {}
    assert registry["slots"]["composer"]["general"] == {"local": "composer-p1", "production": "composer-p1"}
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


def _spec_with_version(version_id, path):
    spec = dataclasses.replace(candidate_spec.PROFILE_CANDIDATES[PROFILE])
    stage_kind = str((pr.get_version(version_id, path) or {}).get("stage") or "chapter")
    stage_obj = getattr(spec, f"{stage_kind}_stage")
    setattr(spec, f"{stage_kind}_stage", dataclasses.replace(stage_obj, prompt_components=dict(stage_obj.prompt_components)))
    pr.apply_overlay(spec, stage_kind=stage_kind, prompt_version=version_id, path=path)
    return spec


def test_component_text_override():
    path = _tmp_registry()
    pr.load_registry(path)
    version = pr.create_version(
        stage="chapter", label="custom text",
        components={"system_style": "dense_faithful"},
        component_text={"system_style": "CUSTOM STYLE FOR {{book_title}}"},
        path=path,
    )
    spec = _spec_with_version(version["id"], path)
    rendered = candidate_spec.render_chapter_system(spec)
    assert rendered.startswith("CUSTOM STYLE FOR")
    assert "{{book_title}}" not in rendered
    assert "CUSTOM STYLE FOR" in rendered
    print("test_component_text_override: OK")


def test_system_and_user_templates():
    path = _tmp_registry()
    pr.load_registry(path)
    version = pr.create_version(
        stage="chapter", label="templated",
        components={"length_policy": "explicit_budget_first"},
        system_template="SYS {{components}} END",
        user_template="Book {{book_title}} | {{target_words}} {{low}}-{{high}} | {{source_md}}",
        path=path,
    )
    assert version["system_template"] == "SYS {{components}} END"
    assert version["user_template"].startswith("Book")
    assert version["user_template_fingerprint"]
    spec = _spec_with_version(version["id"], path)
    system = candidate_spec.render_chapter_system(spec)
    assert system.startswith("SYS ") and system.endswith(" END")
    user = candidate_spec.render_chapter_user(spec, source_md="SRC", target_words=900, book_title="B")
    assert user == "Book B | 900 828-972 | SRC"
    print("test_system_and_user_templates: OK")


def test_byte_identical_baseline():
    """Seeded versions must render byte-identically after the template changes."""
    expected = {
        "chapter-p1": (
            "2906d9c5015f7f2119361da0251930f92f6cfa699028740d40b5e31357e5fe91",
            "535fe199de0639ba472a7e4601d417a186e23e800c288d76d7ae5e1b5cfa0cf2",
        ),
        "chapter-p2": (
            "ce79c4d35f92de9a1f7dadd70ce4d26141a45cbe306d7f05ee47da2fed0eca40",
            "3f3b408caf126ad9e9040dc11d0a76bf7bd865732d795a1be719c5b206e74fa4",
        ),
        "composer-p1": (
            "95516b89af43c2752730793b3a21986b09fd7a87a7f09899ba037c53805e13b1",
            "65bcd6f3338459a7770b6950e48f9a3664a0a62a6a000b1cb8e55d90bc0be73b",
        ),
    }
    path = _tmp_registry()
    pr.load_registry(path)
    for vid, (sys_sha, usr_sha) in expected.items():
        version = pr.get_version(vid, path)
        spec = _spec_with_version(vid, path)
        system = candidate_spec.render_chapter_system(spec) if version["stage"] == "chapter" \
            else candidate_spec.render_composer_system(spec)
        user = candidate_spec.render_chapter_user(spec, source_md="src", target_words=1200, book_title="B") \
            if version["stage"] == "chapter" \
            else candidate_spec.render_composer_user(spec, chapter_summaries_md="cs", target_words=1200, book_title="B")
        assert pr.sha256_text(system) == sys_sha, vid
        assert pr.sha256_text(user) == usr_sha, vid
    print("test_byte_identical_baseline: OK")


def test_legacy_template_alias_and_normalization():
    import json

    path = _tmp_registry()
    legacy = {
        "schema_version": 1,
        "versions": [{
            "id": "chapter-p1", "stage": "chapter", "label": "legacy",
            "components": {"length_policy": "explicit_budget_first"},
            "template": "LEGACY TEMPLATE", "system_prompt_fingerprint": "abc",
        }],
        "slots": {"chapter": {"local": "chapter-p1", "production": "chapter-p1"}, "composer": {"local": None, "production": None}},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    registry = pr.load_registry(path)
    version = registry["versions"][0]
    assert version["scope"] == {"kind": "general"}
    assert version["component_text"] == {}
    assert registry["slots"]["chapter"]["general"]["local"] == "chapter-p1"
    assert registry["slots"]["chapter"]["models"] == {}
    text, fp = pr.version_text(version)
    assert text == "LEGACY TEMPLATE"
    assert fp == pr.fingerprint("LEGACY TEMPLATE")
    print("test_legacy_template_alias_and_normalization: OK")


def test_revise_copy_on_write():
    path = _tmp_registry()
    pr.load_registry(path)
    source = pr.get_version("chapter-p2", path)
    revised = pr.revise_version(
        "chapter-p2",
        label="edited",
        component_text={"system_style": "EDITED {{book_title}}"},
        scope={"kind": "general"},
        path=path,
    )
    assert revised["id"] != "chapter-p2"
    assert revised["derived_from"] == "chapter-p2"
    assert revised["component_text"]["system_style"].startswith("EDITED")
    still = pr.get_version("chapter-p2", path)
    assert still["component_text"] == {}
    assert still["label"] == source["label"]
    print("test_revise_copy_on_write: OK")


def test_scope_validation_and_slots():
    import tools.prompts_api as api

    path = _tmp_registry()
    pr.load_registry(path)
    model = "deepseek/deepseek-v4-flash"
    scoped = pr.create_version(
        stage="chapter", label="flash", components={},
        scope={"kind": "model", "model": model}, path=path,
    )
    assert scoped["scope"] == {"kind": "model", "model": model}
    # Model-scoped version cannot be assigned to the general slot.
    try:
        pr.set_slot(stage="chapter", slot="local", version_id=scoped["id"], path=path)
    except pr.ScopeMismatchError:
        pass
    else:
        raise AssertionError("expected ScopeMismatchError")
    # General version may pin to a model slot.
    pr.set_slot(stage="chapter", slot="local", version_id="chapter-p1", model=model, path=path)
    assert pr.get_slot(stage="chapter", slot="local", model=model, path=path) == "chapter-p1"
    # Unknown model/profile are rejected by the API layer.
    bad = api.handle({"op": "create", "stage": "chapter", "label": "x", "scope": {"kind": "model", "model": "nope/nope"}})
    assert bad["ok"] is False and bad["error"] == "unknown_model"
    bad2 = api.handle({"op": "create", "stage": "chapter", "label": "x",
                       "scope": {"kind": "model", "model": model, "profiles": ["nope"]}})
    assert bad2["ok"] is False and bad2["error"] == "unknown_profile"
    # Resolution precedence: model slot then general fallback.
    vid, source = pr.resolve_version(stage="chapter", slot="local", model=model, path=path)
    assert (vid, source) == ("chapter-p1", "registry_model")
    vid2, source2 = pr.resolve_version(stage="chapter", slot="production", model=model, path=path)
    assert source2 == "registry_general"
    print("test_scope_validation_and_slots: OK")


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
        test_component_text_override,
        test_system_and_user_templates,
        test_byte_identical_baseline,
        test_legacy_template_alias_and_normalization,
        test_revise_copy_on_write,
        test_scope_validation_and_slots,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} PROMPT-REGISTRY TESTS PASSED")


if __name__ == "__main__":
    main()
