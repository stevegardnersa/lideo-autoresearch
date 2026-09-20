"""Offline tests for the Cloud Function prompt-version contract.

Run with: python3 tests/test_cf_prompt_version.py

Covers spec `specs/system-prompt-v2-versioned-routing.md` §16 tests 9-14.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud_function import handler
from core import prompt_registry as pr
from core.openrouter_client import GenerationResult, UsageRecord


class FakeOpenRouter:
    def __init__(self):
        self.payloads = []

    def chat_completion(self, payload, **kwargs):
        self.payloads.append(payload)
        return GenerationResult(
            summary_md="## Summary\n\nSome faithful text.",
            estimated_visible_words=6,
            raw_content='{"summary_md": "## Summary\\n\\nSome faithful text.", "estimated_visible_words": 6}',
            usage=UsageRecord(model_id=str(payload.get("model") or "")),
            raw_response={"mock": True, "usage": {}},
        )

    def system_text(self):
        return self.payloads[-1]["messages"][0]["content"]


def _temp_registry() -> Path:
    d = tempfile.mkdtemp(prefix="cf-prompt-")
    path = Path(d) / "registry.json"
    pr.load_registry(path)  # seed
    return path


def _install(registry_path: Path):
    os.environ["PROMPT_REGISTRY_PATH"] = str(registry_path)
    fake = FakeOpenRouter()
    handler._build_client = lambda **kwargs: fake  # type: ignore[assignment]
    return fake

def _base_body(**overrides):
    body = {
        "source_md": "source text",
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "system_prompt": "LEGACY SYSTEM PROMPT",
        "user_prompt": "write the summary",
    }
    body.update(overrides)
    return body


def _restore():
    os.environ.pop("PROMPT_REGISTRY_PATH", None)


# 9 — known version resolves from registry
def test_known_version():
    path = _temp_registry()
    fake = _install(path)
    try:
        result = handler.run_summarize(_base_body(prompt_version="chapter-p2", prompt_stage="chapter"))
    finally:
        _restore()
    assert result["meta"]["prompt_source"] == "registry"
    assert result["meta"]["prompt_version"] == "chapter-p2"
    assert "Plan the entire summary against the stated word and sentence budgets" in fake.system_text()
    print("test_known_version: OK")


# 10 — unknown version + opt-in inline override
def test_unknown_version_inline_override():
    path = _temp_registry()
    fake = _install(path)
    try:
        result = handler.run_summarize(_base_body(
            prompt_version="chapter-p99", allow_prompt_override=True,
        ))
    finally:
        _restore()
    assert result["meta"]["prompt_source"] == "inline_override"
    assert result["meta"]["prompt_warning"] == "unknown_version"
    assert fake.system_text() == "LEGACY SYSTEM PROMPT"
    print("test_unknown_version_inline_override: OK")


# 11 — unknown version without opt-in -> 422
def test_unknown_version_rejected():
    path = _temp_registry()
    _install(path)
    try:
        try:
            handler.run_summarize(_base_body(prompt_version="chapter-p99"))
        except handler.PromptVersionError as exc:
            assert exc.status_code == 422
            assert exc.error == "unknown_prompt_version"
            assert "chapter-p1" in exc.allowed
        else:
            raise AssertionError("expected PromptVersionError")
    finally:
        _restore()
    print("test_unknown_version_rejected: OK")


# 12 — no version fields -> legacy byte-compatible
def test_legacy_request():
    path = _temp_registry()
    fake = _install(path)
    try:
        result = handler.run_summarize(_base_body())
    finally:
        _restore()
    assert result["meta"]["prompt_source"] == "request"
    assert result["meta"]["prompt_version"] == ""
    assert fake.system_text() == "LEGACY SYSTEM PROMPT"
    print("test_legacy_request: OK")


# 13 — fingerprint mismatch proceeds with a warning
def test_fingerprint_mismatch():
    path = _temp_registry()
    fake = _install(path)
    try:
        result = handler.run_summarize(_base_body(
            prompt_version="chapter-p2", prompt_fingerprint="deadbeefdeadbeef",
        ))
    finally:
        _restore()
    assert result["meta"]["prompt_source"] == "registry"
    assert result["meta"]["prompt_warning"] == "fingerprint_mismatch"
    assert "Plan the entire summary against the stated word and sentence budgets" in fake.system_text()
    print("test_fingerprint_mismatch: OK")


# 14 — empty system prompt + no version -> production default
def test_production_default():
    path = _temp_registry()
    pr.set_slot(stage="chapter", slot="production", version_id="chapter-p2", path=path)
    fake = _install(path)
    try:
        body = _base_body()
        body.pop("system_prompt")
        result = handler.run_summarize(body)
    finally:
        _restore()
    assert result["meta"]["prompt_source"] == "production_default"
    assert result["meta"]["prompt_version"] == "chapter-p2"
    assert "Plan the entire summary against the stated word and sentence budgets" in fake.system_text()
    print("test_production_default: OK")


# 15 — model-scoped production default resolves from the bundled registry
def test_scoped_production_default():
    path = _temp_registry()
    model = "deepseek/deepseek-v4-flash"
    scoped = pr.create_version(
        stage="chapter", label="flash tuned", components={},
        system_template="FLASH SYS {{components}}", scope={"kind": "model", "model": model}, path=path,
    )
    pr.set_slot(stage="chapter", slot="production", version_id=scoped["id"], model=model, path=path)
    fake = _install(path)
    try:
        body = _base_body()
        body.pop("system_prompt")
        body["model"] = model
        result = handler.run_summarize(body)
        scoped_text = fake.system_text()
        other = _base_body()
        other.pop("system_prompt")
        other["model"] = "openai/gpt-5.6-luna"
        result_other = handler.run_summarize(other)
    finally:
        _restore()
    assert result["meta"]["prompt_version"] == scoped["id"]
    assert result["meta"]["prompt_source"] == "production_default"
    assert "FLASH SYS" in scoped_text
    assert result_other["meta"]["prompt_version"] == "chapter-p1"
    print("test_scoped_production_default: OK")


# 16 — sync-shaped bundle round-trips scoped versions + nested slots
def test_sync_round_trip_copies_scoped_versions():
    path = _temp_registry()
    model = "deepseek/deepseek-v4-flash"
    scoped = pr.create_version(
        stage="chapter", label="flash tuned", components={},
        scope={"kind": "model", "model": model}, path=path,
    )
    pr.set_slot(stage="chapter", slot="production", version_id=scoped["id"], model=model, path=path)
    import json

    registry = pr.load_registry(path)
    bundle = Path(path).parent / "cf_bundle_registry.json"
    bundle.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.environ["PROMPT_REGISTRY_PATH"] = str(bundle)
    try:
        loaded = json.loads(bundle.read_text(encoding="utf-8"))
        ids = {v["id"] for v in loaded["versions"]}
        assert scoped["id"] in ids
        assert loaded["slots"]["chapter"]["models"][model]["production"] == scoped["id"]
        resolved = handler._resolve_prompt(
            {"model": model, "prompt_stage": "chapter"},
            pr.load_registry(bundle, seed=False),
        )
        assert resolved[1]["prompt_version"] == scoped["id"]
    finally:
        _restore()
    print("test_sync_round_trip_copies_scoped_versions: OK")


# 17 — production version may bake model + reasoning + a bundled user_template
def test_baked_model_reasoning_and_user_template():
    path = _temp_registry()
    tpl = ("Rewrite the chapter in about {{target_words}} words (between {{low}} and "
           "{{high}}). Keep it short.\n\nTitle: {{chapter_title}}\n\nChapter:\n{{source_md}}\n")
    v = pr.create_version(
        stage="chapter", label="baked", components={"target_sentence_words": "13"},
        user_template=tpl, model="openai/gpt-5.6-luna",
        reasoning={"enabled": False}, path=path,
    )
    pr.set_slot(stage="chapter", slot="production", version_id=v["id"], path=path)
    fake = _install(path)
    try:
        result = handler.run_summarize({"source_md": "the chapter body.", "title": "The Chapter"})
    finally:
        _restore()
    payload = fake.payloads[-1]
    assert payload["model"] == "openai/gpt-5.6-luna", payload["model"]
    assert payload["reasoning"] == {"enabled": False}, payload.get("reasoning")
    assert result["meta"]["prompt_version"] == v["id"]
    assert result["meta"]["prompt_source"] == "production_default"
    assert result["meta"]["model"] == "openai/gpt-5.6-luna"
    user = payload["messages"][1]["content"]
    assert "the chapter body" in user
    assert "The Chapter" in user
    assert "between" in user
    print("test_baked_model_reasoning_and_user_template: OK")


# 18 — explicit request model wins over the baked one
def test_request_model_overrides_baked():
    path = _temp_registry()
    v = pr.create_version(
        stage="chapter", label="baked", components={},
        user_template="Summarize {{source_md}}.",
        model="openai/gpt-5.6-luna", reasoning={"enabled": False}, path=path,
    )
    pr.set_slot(stage="chapter", slot="production", version_id=v["id"], path=path)
    fake = _install(path)
    try:
        handler.run_summarize({"source_md": "s", "model": "deepseek/deepseek-v4-flash"})
    finally:
        _restore()
    payload = fake.payloads[-1]
    assert payload["model"] == "deepseek/deepseek-v4-flash"
    # reasoning is still baked when no reasoning controls are given
    assert payload["reasoning"] == {"enabled": False}
    print("test_request_model_overrides_baked: OK")


# 19 — legacy field aliases (chapter/title/words) feed the bundled template
def test_field_aliases_feed_user_template():
    path = _temp_registry()
    tpl = ("Rewrite in about {{target_words}} words (between {{low}} and {{high}}).\n\n"
           "Title: {{chapter_title}}\n\nChapter:\n{{source_md}}")
    v = pr.create_version(
        stage="chapter", label="aliases", components={"target_sentence_words": "10"},
        user_template=tpl, model="openai/gpt-5.6-luna", reasoning={"enabled": False}, path=path,
    )
    pr.set_slot(stage="chapter", slot="production", version_id=v["id"], path=path)
    fake = _install(path)
    try:
        handler.run_summarize({
            "chapter": "the chapter body", "title": "T", "words": 200, "model": "m/x",
        })
    finally:
        _restore()
    user = fake.payloads[-1]["messages"][1]["content"]
    assert "about 200 words" in user
    assert "between 190 and 210" in user
    assert "the chapter body" in user
    print("test_field_aliases_feed_user_template: OK")


# 20 — production version without a user_template still requires a user prompt
def test_missing_user_prompt_rejected():
    path = _temp_registry()
    v = pr.create_version(stage="chapter", label="no tpl", components={}, path=path)
    pr.set_slot(stage="chapter", slot="production", version_id=v["id"], path=path)
    _install(path)
    try:
        try:
            handler.run_summarize({"source_md": "x"})
        except handler.PromptVersionError as exc:
            assert exc.status_code == 422
            assert exc.error == "missing_user_prompt"
        else:
            raise AssertionError("expected missing_user_prompt 422")
    finally:
        _restore()
    print("test_missing_user_prompt_rejected: OK")


def main():
    tests = [
        test_known_version,
        test_unknown_version_inline_override,
        test_unknown_version_rejected,
        test_legacy_request,
        test_fingerprint_mismatch,
        test_production_default,
        test_scoped_production_default,
        test_sync_round_trip_copies_scoped_versions,
        test_baked_model_reasoning_and_user_template,
        test_request_model_overrides_baked,
        test_field_aliases_feed_user_template,
        test_missing_user_prompt_rejected,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} CF PROMPT-VERSION TESTS PASSED")


if __name__ == "__main__":
    main()
