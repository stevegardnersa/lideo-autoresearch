"""Offline tests for prompt-version resolution precedence.

Run with: python3 tests/test_prompt_resolution.py

Covers spec `specs/prompts-ui-model-scoped-editing.md` §6.3 / §13.
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import prompt_registry as pr
from core import run_candidate as rc
import tools.prompts_api as api

PROFILE = "30m_gpt-5.6-luna_effort-minimal"
MODEL = "deepseek/deepseek-v4-flash"


def _tmp_registry():
    d = tempfile.mkdtemp(prefix="prompt-resolution-")
    return Path(d) / "registry.json"


class _LocalClient:
    function_url = ""
    auth_mode = "none"


def test_general_then_baseline():
    path = _tmp_registry()
    pr.load_registry(path)
    vid, source = pr.resolve_version(stage="chapter", slot="local", path=path)
    assert (vid, source) == ("chapter-p1", "registry_general")
    # Clear the general slot -> baseline.
    pr.set_slot(stage="chapter", slot="local", version_id="", path=path)
    vid2, source2 = pr.resolve_version(stage="chapter", slot="local", path=path)
    assert (vid2, source2) == (None, "baseline")
    print("test_general_then_baseline: OK")


def test_explicit_override_wins():
    path = _tmp_registry()
    pr.load_registry(path)
    vid, source = pr.resolve_version(stage="chapter", prompt_version="chapter-p2", slot="local", path=path)
    assert (vid, source) == ("chapter-p2", "override")
    print("test_explicit_override_wins: OK")


def test_model_slot_and_profile_qualified():
    path = _tmp_registry()
    pr.load_registry(path)
    # Model-wide scoped version assigned to the model slot.
    wide = pr.create_version(stage="chapter", label="wide", components={},
                             scope={"kind": "model", "model": MODEL}, path=path)
    pr.set_slot(stage="chapter", slot="local", version_id=wide["id"], model=MODEL, path=path)
    vid, source = pr.resolve_version(stage="chapter", slot="local", model=MODEL, profile=PROFILE, path=path)
    assert (vid, source) == (wide["id"], "registry_model")

    # Profile-qualified version wins over the model-wide assignment.
    profiled = pr.create_version(
        stage="chapter", label="profiled", components={},
        scope={"kind": "model", "model": MODEL, "profiles": [PROFILE]}, path=path,
    )
    vid2, source2 = pr.resolve_version(stage="chapter", slot="local", model=MODEL, profile=PROFILE, path=path)
    assert (vid2, source2) == (profiled["id"], "registry_model")

    # Unknown profile falls back to the model-wide slot version.
    vid3, source3 = pr.resolve_version(stage="chapter", slot="local", model=MODEL, profile="some/other", path=path)
    assert (vid3, source3) == (wide["id"], "registry_model")

    # A different model falls through to the general slot.
    vid4, source4 = pr.resolve_version(stage="chapter", slot="local", model="other/model", path=path)
    assert source4 == "registry_general"
    print("test_model_slot_and_profile_qualified: OK")


def test_permutation_outranks_registry():
    path = _tmp_registry()
    pr.load_registry(path)
    args = argparse.Namespace(
        prompt_registry=str(path),
        prompt_slot="local",
        prompt_stage="both",
        prompt_version="",
        allow_prompt_override=False,
    )
    spec = candidate_spec.get_candidate(PROFILE)
    original = rc._permutation_override
    rc._permutation_override = lambda profile, stage: ({"anti_fluff_policy": "medium"} if stage == "chapter" else {})
    try:
        selection = rc._resolve_prompt_versions(args, spec, candidate_spec, _LocalClient())
    finally:
        rc._permutation_override = original
    assert selection["chapter"]["source"] == "permutation"
    assert selection["chapter"]["version"] == ""
    assert selection["composer"]["source"] == "registry_general"
    assert rc._prompt_source_label(selection["chapter"]) == "permutation"
    assert rc._prompt_source_label(selection["composer"]) == "registry"
    print("test_permutation_outranks_registry: OK")


def test_resolve_endpoint_shape():
    path = _tmp_registry()
    pr.load_registry(path)
    reg = {"op": "resolve", "stage": "chapter", "slot": "local", "path": str(path)}
    out = api.handle(reg)
    assert out["ok"] is True
    assert out["version_id"] == "chapter-p1"
    assert out["source"] == "registry_general"
    assert out["fingerprint"]
    print("test_resolve_endpoint_shape: OK")


def main():
    tests = [
        test_general_then_baseline,
        test_explicit_override_wins,
        test_model_slot_and_profile_qualified,
        test_permutation_outranks_registry,
        test_resolve_endpoint_shape,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} PROMPT-RESOLUTION TESTS PASSED")


if __name__ == "__main__":
    main()
