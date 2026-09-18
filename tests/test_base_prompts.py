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


# ── comparison runs (dashboard "Base prompt comparisons") ───────────────

def test_default_artifact_paths_timestamped():
    original = sweep.ARTIFACT_DIR
    tmpdir = Path(tempfile.mkdtemp(prefix="base-sweep-artifacts-"))
    sweep.ARTIFACT_DIR = tmpdir
    try:
        json_path, md_path = sweep._default_artifact_paths(
            "30m_model/odd", "bench:v1", stamp="20260101T000000Z"
        )
        assert json_path.name == "30m_model_odd_bench_v1_20260101T000000Z.json", json_path
        assert md_path == json_path.with_suffix(".md"), md_path

        # Collision within the same second gets a numeric suffix.
        json_path.write_text("{}", encoding="utf-8")
        again, again_md = sweep._default_artifact_paths(
            "30m_model/odd", "bench:v1", stamp="20260101T000000Z"
        )
        assert again.name.endswith("-2.json"), again
        assert again_md.name.endswith("-2.md"), again_md

        # Without a stamp the legacy fixed path is used (explicit --json-out case).
        plain, _ = sweep._default_artifact_paths("30m_model", "chapter_fast-v3")
        assert plain.name == "30m_model_chapter_fast-v3.json", plain
    finally:
        sweep.ARTIFACT_DIR = original
    print("test_default_artifact_paths_timestamped: OK")


def test_run_sweep_records_job_id_and_timestamp():
    tmpdir = Path(tempfile.mkdtemp(prefix="base-sweep-job-"))
    runner = lambda base, repeat: None  # noqa: E731 - no manifests is fine
    args = argparse.Namespace(
        profile=PROFILE, bench="chapter_fast-v3", bases=["sentence"], repeat=1, mock=True,
        max_samples=0, adopt=False, dry_run=False, tolerance_pct=0.05,
        python=sys.executable, run_candidate="core/run_candidate.py",
        runs_dir=str(tmpdir), json_out="", md_out="",
        job_id="job-abc123", generated_at="2026-01-01T00:00:00Z",
    )
    result = sweep.run_sweep(args, runner=runner)
    assert result["job_id"] == "job-abc123", result["job_id"]
    assert result["generated_at"] == "2026-01-01T00:00:00Z", result["generated_at"]
    print("test_run_sweep_records_job_id_and_timestamp: OK")


def _write_comparison(dir_path: Path, stem: str, *, profile: str, bench: str,
                      generated_at: str, job_id: str = "") -> None:
    payload = {
        "profile": profile,
        "bench": bench,
        "generated_at": generated_at,
        "job_id": job_id,
        "winner": "sentence",
        "rows": [{"slug": "sentence", "length_score": 0.9}],
    }
    (dir_path / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")
    (dir_path / f"{stem}.md").write_text(f"# Summary for {profile}\n", encoding="utf-8")


def _write_sweep_job(jobs_dir: Path, job_id: str, *, profile: str, bench: str,
                     created_at: str) -> None:
    (jobs_dir / f"{job_id}.log").write_text(
        f'[job] meta: {{"kind":"meta","jobId":"{job_id}","toolId":"sweep_base_prompts",'
        f'"createdAt":"{created_at}"}}\n'
        f"  $ python core/run_candidate.py --bench {bench} --profile {profile}\n",
        encoding="utf-8",
    )


def test_list_and_get_comparisons():
    tmpdir = Path(tempfile.mkdtemp(prefix="base-sweep-comparisons-"))
    jobs_dir = Path(tempfile.mkdtemp(prefix="base-sweep-jobs-"))
    _write_comparison(tmpdir, "old_run", profile="30m_a", bench="bench-v1",
                      generated_at="2026-01-01T00:00:00Z")
    _write_comparison(tmpdir, "new_run", profile="30m_b", bench="bench-v2",
                      generated_at="2026-02-01T00:00:00Z", job_id="job-xyz")
    # Non-sweep JSON files are ignored.
    (tmpdir / "not_a_sweep.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    comparisons = prompts_api.list_comparisons(artifact_dir=tmpdir, jobs_dir=jobs_dir)
    assert [c["id"] for c in comparisons] == ["new_run", "old_run"], comparisons
    assert comparisons[0]["job_id"] == "job-xyz" and comparisons[0]["has_log"] is True
    assert comparisons[1]["has_log"] is False

    detail = prompts_api.get_comparison("old_run", artifact_dir=tmpdir, jobs_dir=jobs_dir)
    assert detail["markdown"].startswith("# Summary for 30m_a"), detail["markdown"]
    assert detail["data"]["winner"] == "sentence"

    # Path traversal and unknown ids resolve to nothing.
    assert prompts_api.get_comparison("../registry", artifact_dir=tmpdir, jobs_dir=jobs_dir) is None
    assert prompts_api.get_comparison("does-not-exist", artifact_dir=tmpdir, jobs_dir=jobs_dir) is None

    # The list op embeds the same summaries.
    listed = prompts_api.handle({"op": "list_comparisons", "artifact_dir": str(tmpdir),
                                 "jobs_dir": str(jobs_dir)})
    assert listed["ok"] and [c["id"] for c in listed["comparisons"]] == ["new_run", "old_run"]
    fetched = prompts_api.handle({"op": "get_comparison", "id": "new_run",
                                  "artifact_dir": str(tmpdir), "jobs_dir": str(jobs_dir)})
    assert fetched["ok"] and fetched["id"] == "new_run"
    missing = prompts_api.handle({"op": "get_comparison", "id": "nope",
                                  "artifact_dir": str(tmpdir), "jobs_dir": str(jobs_dir)})
    assert missing["ok"] is False and missing["code"] == 404
    print("test_list_and_get_comparisons: OK")


def test_resolve_comparison_job_from_logs():
    sweep_dir = Path(tempfile.mkdtemp(prefix="base-sweep-match-"))
    jobs_dir = Path(tempfile.mkdtemp(prefix="base-sweep-match-jobs-"))
    resolved_id = "abc12345-0000-0000-0000-000000000000"
    _write_sweep_job(jobs_dir, resolved_id, profile="30m_b", bench="bench-v2",
                     created_at="2026-02-01T00:00:00.000Z")
    _write_sweep_job(jobs_dir, "recorded-id", profile="30m_r", bench="bench-v3",
                     created_at="2026-02-02T00:00:00.000Z")
    # A non-sweep log must never be matched.
    (jobs_dir / "other.log").write_text(
        '[job] meta: {"kind":"meta","jobId":"other","toolId":"run_candidate",'
        '"createdAt":"2026-02-01T00:00:00.000Z"}\n'
        "  $ python core/run_candidate.py --bench bench-v2 --profile 30m_b\n",
        encoding="utf-8",
    )

    _write_comparison(sweep_dir, "resolved", profile="30m_b", bench="bench-v2",
                      generated_at="2026-02-01T00:03:00Z")
    _write_comparison(sweep_dir, "unmatched", profile="30m_c", bench="bench-v9",
                      generated_at="2026-02-01T00:03:00Z")
    _write_comparison(sweep_dir, "recorded", profile="30m_r", bench="bench-v3",
                      generated_at="2026-02-02T00:03:00Z", job_id="recorded-id")

    comps = {c["id"]: c for c in prompts_api.list_comparisons(
        artifact_dir=sweep_dir, jobs_dir=jobs_dir)}
    assert comps["resolved"]["job_id"] == resolved_id, comps["resolved"]
    assert comps["resolved"]["log_resolved"] is True
    assert comps["unmatched"]["job_id"] == "" and comps["unmatched"]["has_log"] is False

    # A recorded job id is trusted even when a matching log exists.
    assert comps["recorded"]["job_id"] == "recorded-id"
    assert comps["recorded"]["log_resolved"] is False

    # Detail view exposes the same resolved job id.
    detail = prompts_api.get_comparison("resolved", artifact_dir=sweep_dir, jobs_dir=jobs_dir)
    assert detail["summary"]["job_id"] == resolved_id
    print("test_resolve_comparison_job_from_logs: OK")


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
        test_default_artifact_paths_timestamped,
        test_run_sweep_records_job_id_and_timestamp,
        test_list_and_get_comparisons,
        test_resolve_comparison_job_from_logs,
        test_base_resolver_order,
        test_seed_components_overlays_spec,
        test_candidate_cmd_shape,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} BASE-PROMPT TESTS PASSED")


if __name__ == "__main__":
    main()
