"""Offline tests for curated base prompts + the base-prompt sweep.

Run with: python3 tests/test_base_prompts.py

Covers specs/base-prompts-and-model-strategy-tuning.md §5.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import prompt_registry as pr
from tools import prompts_api
from tools import sweep_base_prompts as sweep
from autoresearch import base_resolver
from autoresearch import optimizer

PROFILE = "30m_gpt-5.6-luna_notthinking"
BASE_SLUGS = ["sentence", "readable", "readable-low"]
BASE_IDS = {"sentence": "chapter-p15", "readable": "chapter-p16", "readable-low": "chapter-p17"}


def _tmp_registry() -> Path:
    d = tempfile.mkdtemp(prefix="base-prompts-")
    return Path(d) / "registry.json"


# ── fixtures ────────────────────────────────────────────────────────────

def _manifest(*, target=1000, final_words=1010, first_words=1300, passes=1,
              hard_fail=False, faith=0.8, concept=0.7, judge=False, sample_id="book:001"):
    final_err = abs(final_words - target) / target
    first_err = abs(first_words - target) / target
    tol = 0.05
    return {
        "run_manifest": {
            "run_id": "fixture",
            "judge_version_resolved": "judge-absolute-v1" if judge else "judge-absolute-v1::deterministic",
        },
        "samples": [{
            "sample_id": sample_id,
            "target_words": target,
            "summary_visible_words": final_words,
            "passes_used": passes,
            "generation_cost": 0.01,
        }],
        "sample_scores": [{
            "sample_id": sample_id,
            "hard_fail": hard_fail,
            "quality": 0.5,
            "resolved_faithfulness": faith,
            "resolved_concept_coverage": concept,
            "deterministic": {
                "final_length_error_pct": final_err,
                "first_pass_length_error_pct": first_err,
                "final_length_accuracy": 1.0 if final_err <= tol else 0.4,
                "first_pass_length_accuracy": 1.0 if first_err <= tol else 0.0,
                "visible_words": final_words,
                "first_pass_visible_words": first_words,
            },
        }],
    }


# ── §4.3 base resolution ────────────────────────────────────────────────

def test_bases_resolve_from_real_registry():
    bases = prompts_api.list_base_prompts()
    assert [b["slug"] for b in bases] == BASE_SLUGS, bases
    ids = [b["version_id"] for b in bases]
    assert len(set(ids)) == 3, ids
    assert {b["slug"]: b["version_id"] for b in bases} == BASE_IDS
    print("test_bases_resolve_from_real_registry: OK")


def test_sentence_base_components():
    bases = {b["slug"]: b for b in prompts_api.list_base_prompts()}
    assert bases["sentence"]["components"] == {
        "length_policy": "sentence_primary",
        "system_style": "plain_readable",
        "target_sentence_words": "13",
        "sentence_target_words": "11",
    }
    print("test_sentence_base_components: OK")


def test_label_prefix_edge_cases():
    path = _tmp_registry()
    pr.load_registry(path)  # seed

    # An 'experiment: base: <slug>' label must NOT satisfy the base match.
    pr.create_version(
        stage="chapter",
        label="experiment: base: sentence",
        components={"length_policy": "explicit_budget_first"},
        path=path,
    )
    try:
        prompts_api.list_base_prompts(path=path)
        raise AssertionError("expected missing-base error")
    except pr.PromptRegistryError as exc:
        assert "sentence" in str(exc), exc

    # A real base label resolves.
    pr.create_version(
        stage="chapter",
        label="base: sentence",
        components={"length_policy": "sentence_primary"},
        path=path,
    )
    try:
        prompts_api.list_base_prompts(path=path)
        raise AssertionError("expected missing readable/readable-low error")
    except pr.PromptRegistryError:
        pass

    # Ambiguity is an error, not a silent pick.
    pr.create_version(
        stage="chapter",
        label="base: sentence",
        components={"length_policy": "sentence_primary"},
        path=path,
    )
    try:
        prompts_api.list_base_prompts(path=path)
        raise AssertionError("expected ambiguous-base error")
    except pr.PromptRegistryError as exc:
        assert "sentence" in str(exc), exc
    print("test_label_prefix_edge_cases: OK")


# ── drift guard (§4.1/§4.2) ─────────────────────────────────────────────

def test_rederived_bases_match_fresh_render():
    for vid in BASE_IDS.values():
        version = pr.require_version(vid)
        fresh = pr._render_template(
            version["stage"], version["components"],
            version.get("component_text"), version.get("system_template"),
        )
        assert fresh, f"{vid} rendered empty"
        assert version["template"] == fresh, f"{vid} stored template is stale"
        assert version["system_prompt_sha256"] == pr.sha256_text(fresh), f"{vid} sha is stale"
    # The aligned plain_readable text must be in the readable bases.
    readable = pr.require_version(BASE_IDS["readable"])
    assert "words-per-sentence the request asks for" in readable["template"]
    assert "14 to 16 words per sentence" not in readable["template"]
    print("test_rederived_bases_match_fresh_render: OK")


def test_experiments_demoted():
    for vid in ["chapter-p3", "chapter-p14"]:
        version = pr.require_version(vid)
        assert str(version["label"]).startswith("experiment: "), version["label"]
    # ...but still resolvable by id for reproducing past runs.
    assert pr.require_version("chapter-p3")["id"] == "chapter-p3"
    print("test_experiments_demoted: OK")


# ── §4.4 metrics / eligibility / ranking ────────────────────────────────

def test_metrics_from_manifest():
    m = sweep.metrics_from_manifest(_manifest(final_words=1000, passes=3))
    assert m["final_hit_rate"] == 1.0, m
    assert m["mean_final_error_pct"] == 0.0, m
    assert m["mean_extra_passes"] == 2.0, m
    assert m["judge_backed"] is False, m
    judged = sweep.metrics_from_manifest(_manifest(judge=True))
    assert judged["judge_backed"] is True
    print("test_metrics_from_manifest: OK")


def test_length_score_monotonic():
    good = sweep.metrics_from_manifest(_manifest(final_words=1000, passes=1))
    bad = sweep.metrics_from_manifest(_manifest(final_words=1800, passes=5))
    assert sweep.length_score(good) > sweep.length_score(bad)
    print("test_length_score_monotonic: OK")


def _row(slug, score, extra=0.0, faith=0.8, hard_fail=0.0, judge=True):
    row = sweep.aggregate_runs([sweep.metrics_from_manifest(
        _manifest(faith=faith, hard_fail=bool(hard_fail), judge=judge)
    )])
    row.update({"slug": slug, "version_id": BASE_IDS[slug], "label": f"base: {slug}",
                "components": {}, "length_score": score, "mean_extra_passes": extra})
    return row


def test_rank_bases_eligibility_and_tiebreak():
    rows = [
        _row("sentence", 0.80, extra=1.0),
        _row("readable", 0.80, extra=0.5),
        _row("readable-low", 0.60, extra=0.0, faith=0.40),  # below the 0.70 floor
        _row("sentence", 0.10, extra=9.9),  # dropped by dict key below
    ]
    rows = rows[:3]
    ranked, winner = sweep.rank_bases(rows)
    assert winner == "readable", (winner, [(r["slug"], r["eligible"]) for r in ranked])
    ineligible = [r for r in ranked if not r["eligible"]]
    assert ineligible and ineligible[0]["slug"] == "readable-low"
    assert any("faithfulness" in reason for reason in ineligible[0]["ineligible_reasons"])

    # Tie on score -> lower mean_extra_passes wins.
    tied, tied_winner = sweep.rank_bases([
        _row("sentence", 0.70, extra=2.0),
        _row("readable", 0.70, extra=0.5),
    ])
    assert tied_winner == "readable", tied_winner

    # Tie on score and passes -> base order (sentence first).
    ordered, ordered_winner = sweep.rank_bases([
        _row("readable-low", 0.70, extra=1.0),
        _row("sentence", 0.70, extra=1.0),
    ])
    assert ordered_winner == "sentence", ordered_winner

    # No successful runs -> no winner.
    empty = _row("sentence", 0.0)
    empty["runs"] = 0
    _, none_winner = sweep.rank_bases([empty])
    assert none_winner is None
    print("test_rank_bases_eligibility_and_tiebreak: OK")


def test_sweep_end_to_end_mocked():
    tmpdir = Path(tempfile.mkdtemp(prefix="base-sweep-"))
    out_json = tmpdir / "sweep.json"
    out_md = tmpdir / "sweep.md"
    calls = []

    # Per-base fixtures: sentence wins, readable close, readable-low judge-backed + low faith.
    spec = {
        "sentence": _manifest(final_words=1000, passes=1),
        "readable": _manifest(final_words=1060, passes=2),
        "readable-low": _manifest(final_words=1200, passes=4, faith=0.30, concept=0.30, judge=True),
    }

    def runner(base, repeat):
        calls.append((base["slug"], repeat))
        path = tmpdir / f"{base['slug']}-r{repeat}.json"
        path.write_text(json.dumps(spec[base["slug"]]), encoding="utf-8")
        return path

    args = argparse.Namespace(
        profile=PROFILE, bench="chapter_fast-v3", bases=[], repeat=3, mock=True,
        max_samples=0, adopt=False, dry_run=False, tolerance_pct=0.05,
        python=sys.executable, run_candidate="core/run_candidate.py",
        runs_dir=str(tmpdir), json_out=str(out_json), md_out=str(out_md),
    )
    result = sweep.run_sweep(args, runner=runner)

    # --repeat is honored for every base.
    assert len(calls) == 9, calls
    assert all(sum(1 for slug, _ in calls if slug == base) == 3 for base in spec), calls

    assert out_json.exists() and out_md.exists()
    data = json.loads(out_json.read_text())
    assert data["repeat"] == 3
    assert len(data["rows"]) == 3
    assert data["winner"] == "sentence", data["winner"]
    assert {r["slug"] for r in data["rows"]} == set(spec)
    assert all("length_score" in r and "eligible" in r for r in data["rows"])
    ineligible = [r for r in data["rows"] if not r["eligible"]]
    assert [r["slug"] for r in ineligible] == ["readable-low"], ineligible
    assert "Recommendation" in out_md.read_text()
    print("test_sweep_end_to_end_mocked: OK")


# ── §4.6 base-aware tuning plumbing ─────────────────────────────────────

def test_base_resolver_order():
    assert base_resolver.resolve_base(PROFILE, "sentence")["version_id"] == "chapter-p15"
    assert base_resolver.resolve_base(PROFILE, "none") is None
    default = base_resolver.resolve_base(PROFILE)
    assert default["slug"] in BASE_SLUGS
    try:
        base_resolver.resolve_base(PROFILE, "bogus")
        raise AssertionError("expected unknown-base error")
    except ValueError:
        pass
    print("test_base_resolver_order: OK")


def test_seed_components_overlays_spec():
    spec = {"chapter_stage": {"prompt_components": {"length_policy": "explicit_budget_first"}}}
    seeded = base_resolver.seed_components(spec, base_resolver.resolve_base(PROFILE, "sentence"))
    assert seeded["length_policy"] == "sentence_primary", seeded
    assert seeded["sentence_target_words"] == "11", seeded
    assert spec["chapter_stage"]["prompt_components"]["length_policy"] == "sentence_primary"
    print("test_seed_components_overlays_spec: OK")


def test_candidate_cmd_shape():
    cmd = optimizer.build_candidate_cmd(
        run_candidate_script="core/run_candidate.py",
        profile=PROFILE,
        bench="chapter_fast-v3",
        spec_path="/tmp/x/candidate_spec.py",
        prompt_version="chapter-p16",
    )
    joined = " ".join(cmd)
    assert "--bench chapter_fast-v3" in joined, joined
    assert f"--profile {PROFILE}" in joined, joined
    assert "--spec /tmp/x/candidate_spec.py" in joined, joined
    assert "--prompt-version chapter-p16 --prompt-stage chapter" in joined, joined
    print("test_candidate_cmd_shape: OK")


def main():
    tests = [
        test_bases_resolve_from_real_registry,
        test_sentence_base_components,
        test_label_prefix_edge_cases,
        test_rederived_bases_match_fresh_render,
        test_experiments_demoted,
        test_metrics_from_manifest,
        test_length_score_monotonic,
        test_rank_bases_eligibility_and_tiebreak,
        test_sweep_end_to_end_mocked,
        test_base_resolver_order,
        test_seed_components_overlays_spec,
        test_candidate_cmd_shape,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} BASE-PROMPT TESTS PASSED")


if __name__ == "__main__":
    main()
