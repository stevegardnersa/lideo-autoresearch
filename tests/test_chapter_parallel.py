"""Offline tests for chapter-level parallel Cloud Function execution + resume.

Run with: python3 tests/test_chapter_parallel.py
No pytest required. Uses fake Cloud Function clients (no network / no LLM calls).

Covers:
  * cf_concurrency_slot bounding in-flight Cloud Function invocations
  * run_book_sample running chapters concurrently, in book order, with resume
    of both checkpointed chapters and mid-flight chapter stage state
  * run_candidate._run_single parallelizing chapter bench items, checkpointing
    each item, recovering on resume, and pausing on insufficient credits
"""

import dataclasses
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import run_candidate as rc
from core.openrouter_client import (
    GenerationResult,
    OpenRouterInsufficientCreditsError,
    UsageRecord,
)
from core.versioning import load_benchmark_manifest

ROOT = Path(__file__).resolve().parent.parent
PROFILE = "30m_deepseek-v4-flash_notthinking"

_ORIG_IS_CLOUD_FUNCTION = rc._is_cloud_function
_ORIG_MAKE_CLIENT = rc.make_client


# --------------------------------------------------------------------------- #
# Fakes and helpers
# --------------------------------------------------------------------------- #


class FakeCloudFunctionClient:
    """Thread-safe fake Cloud Function client.

    Tracks how many invocations are in flight at once (to assert the concurrency
    cap) and can raise an insufficient-credits error after N calls.
    """

    def __init__(self, *, delay: float = 0.0, summary_words: int = 80, fail_after: Optional[int] = None):
        self.delay = delay
        self.summary_words = summary_words
        self.fail_after = fail_after
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        self.function_url = "http://localhost/fake-cf"
        self.auth_mode = "none"
        self.last_meta = {}
        self.scoring_version_mismatch = False

    def chat_completion(self, request_body, **kwargs):
        with self._lock:
            self.calls += 1
            call_index = self.calls
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.fail_after is not None and call_index > self.fail_after:
                raise OpenRouterInsufficientCreditsError(402, "/chat/completions", "no credits")
            if self.delay:
                time.sleep(self.delay)
            payload = json.dumps(
                {
                    "summary_md": "word " * self.summary_words,
                    "estimated_visible_words": self.summary_words,
                }
            )
            return GenerationResult(
                summary_md="word " * self.summary_words,
                estimated_visible_words=self.summary_words,
                raw_content=payload,
                usage=UsageRecord(),
                raw_response={"mock": True},
            )
        finally:
            with self._lock:
                self.in_flight -= 1


def install_fake_cf(client) -> None:
    rc._is_cloud_function = lambda candidate: isinstance(candidate, FakeCloudFunctionClient)
    rc.make_client = lambda args: client


def uninstall_fake_cf() -> None:
    rc._is_cloud_function = _ORIG_IS_CLOUD_FUNCTION
    rc.make_client = _ORIG_MAKE_CLIENT
    rc.set_cf_concurrency(None)


def make_synthetic_book(root: Path, *, book_id: str = "synthetic-book", n_chapters: int = 6, source_words: int = 120):
    """Write a minimal data/books-style corpus so tests don't depend on real books."""
    data_dir = root / "books"
    book_dir = data_dir / book_id
    book_dir.mkdir(parents=True)
    chapters = []
    for index in range(n_chapters):
        chapter_id = f"{index:03d}"
        (book_dir / f"{chapter_id}.md").write_text(
            "# Chapter\n\n" + ("source word " * source_words) + "\n",
            encoding="utf-8",
        )
        chapters.append({"chapter_id": chapter_id, "title": f"Chapter {index}", "source_path": f"{chapter_id}.md"})
    (book_dir / "book.json").write_text(
        json.dumps({"book_title": "Synthetic Book", "chapters": chapters}),
        encoding="utf-8",
    )
    return data_dir, [chapter["chapter_id"] for chapter in chapters]


def chapter_parallel_spec(*, max_passes: int = 1, disable_composer: bool = True):
    base = candidate_spec.PROFILE_CANDIDATES[PROFILE]
    return dataclasses.replace(
        base,
        disable_composer=disable_composer,
        length_control=dataclasses.replace(base.length_control, max_passes=max_passes),
    )


def book_item(book_id: str):
    return {"book_id": book_id, "level": "book", "sample_id": book_id}


def make_stage_run(summary_words: int, *, passes_used: int = 1, cost: float = 0.0) -> "rc.StageRun":
    summary = "word " * summary_words
    return rc.StageRun(
        summary_md=summary,
        first_pass_summary_md=summary,
        passes_used=passes_used,
        generation_cost=cost,
        uncached_generation_cost=cost,
        raw_responses=(),
    )


def partial_book_progress(book, *, completed: int, midflight_id: Optional[str], targets):
    """Build a run_book_sample resume payload with `completed` finished chapters
    and (optionally) one chapter carrying mid-flight stage state."""
    progress = {
        "kind": "book",
        "phase": "chapters",
        "item_key": book.book_id,
        "item": book_item(book.book_id),
        "book_id": book.book_id,
        "sample_id": book.book_id,
        "chapter_targets": list(targets),
        "chapter_outputs": [],
        "chapter_passes": [],
        "total_generation_cost": 0.0,
        "total_uncached_cost": 0.0,
        "current_stage": None,
    }
    for chapter in book.chapters[:completed]:
        stage = make_stage_run(50, passes_used=1, cost=0.001)
        progress["chapter_outputs"].append(rc.serialize_completed_chapter_runs([(chapter, stage)])[0])
        progress["chapter_passes"].append(1)
        progress["total_generation_cost"] += 0.001
        progress["total_uncached_cost"] += 0.001
    if midflight_id is not None:
        progress["chapter_stage_states"] = {
            midflight_id: {
                "passes_used": 1,
                "summary_md": "word " * 40,
                "generation_cost": 0.0005,
                "first_pass_summary_md": "word " * 40,
            }
        }
    return progress


def build_run_args(tmp: Path, *, run_id: str, bench: str = "chapter_small", concurrency: int = 3, resume: Optional[str] = None, max_samples: int = 0):
    argv = [
        "run_candidate.py",
        "--bench",
        bench,
        "--profile",
        PROFILE,
        "--runs-dir",
        str(tmp / "runs"),
        "--catalog-snapshots-dir",
        str(tmp / "snapshots" / "catalog"),
        "--price-snapshots-dir",
        str(tmp / "snapshots" / "pricing"),
        "--chapter-concurrency",
        str(concurrency),
        "--run-id",
        run_id,
    ]
    if max_samples:
        argv += ["--max-samples", str(max_samples)]
    if resume:
        argv += ["--resume", resume]
    saved_argv = sys.argv
    sys.argv = argv
    try:
        return rc.parse_args()
    finally:
        sys.argv = saved_argv


def run_dir_for(benchmark_manifest) -> Path:
    return Path(benchmark_manifest["benchmark_version"])


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_cf_concurrency_slot_caps_inflight():
    """cf_concurrency_slot never lets more than `concurrency` workers through."""
    rc.set_cf_concurrency(2)
    try:
        state = {"in_flight": 0, "max": 0}
        state_lock = threading.Lock()

        def worker():
            with rc.cf_concurrency_slot():
                with state_lock:
                    state["in_flight"] += 1
                    state["max"] = max(state["max"], state["in_flight"])
                time.sleep(0.02)
                with state_lock:
                    state["in_flight"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert state["max"] == 2, f"expected cap of 2, observed {state['max']}"
    finally:
        rc.set_cf_concurrency(None)
    print("test_cf_concurrency_slot_caps_inflight: OK")


def test_run_book_sample_runs_chapters_concurrently_and_orders_outputs():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        data_dir, chapter_ids = make_synthetic_book(tmp, n_chapters=6)
        spec = chapter_parallel_spec()
        client = FakeCloudFunctionClient(delay=0.03)
        install_fake_cf(client)
        rc.set_cf_concurrency(3)
        try:
            events = []
            sample, trace = rc.run_book_sample(
                book_item("synthetic-book"),
                candidate_module=candidate_spec,
                spec=spec,
                client=client,
                data_dir=data_dir,
                judge_model="",
                judge_source_char_limit=32000,
                resume_progress=None,
                progress_callback=lambda payload: events.append(payload),
                run_dir=tmp / "runs",
                chapter_concurrency=3,
            )
        finally:
            uninstall_fake_cf()

        assert client.calls == len(chapter_ids), client.calls
        assert 2 <= client.max_in_flight <= 3, client.max_in_flight
        final = [event for event in events if event.get("phase") == "completed"][-1]
        assert [entry["chapter_id"] for entry in final["chapter_outputs"]] == chapter_ids
        assert trace["mean_chapter_passes_used"] == 1.0
        assert sample.summary_md.strip()
    print("test_run_book_sample_runs_chapters_concurrently_and_orders_outputs: OK")


def test_run_book_sample_resume_keeps_completed_and_resumes_midflight():
    """Resume must carry checkpointed chapters AND skip regenerating a chapter
    that already has mid-flight stage state."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        data_dir, chapter_ids = make_synthetic_book(tmp, n_chapters=6)
        spec = chapter_parallel_spec()
        book = rc.load_book_context("synthetic-book", data_dir)
        targets = candidate_spec.allocate_chapter_targets(
            [chapter.visible_words for chapter in book.chapters],
            book.total_visible_words,
            spec.profile,
            spec.budget_allocator,
        )
        resume_progress = partial_book_progress(book, completed=2, midflight_id=chapter_ids[2], targets=targets)

        client = FakeCloudFunctionClient()
        install_fake_cf(client)
        rc.set_cf_concurrency(3)
        try:
            events = []
            sample, trace = rc.run_book_sample(
                book_item("synthetic-book"),
                candidate_module=candidate_spec,
                spec=spec,
                client=client,
                data_dir=data_dir,
                judge_model="",
                judge_source_char_limit=32000,
                resume_progress=resume_progress,
                progress_callback=lambda payload: events.append(payload),
                run_dir=tmp / "runs",
                chapter_concurrency=3,
            )
        finally:
            uninstall_fake_cf()

        # 2 chapters came from checkpoints, 1 resumed from mid-flight stage state.
        assert client.calls == len(chapter_ids) - 3, client.calls
        final = [event for event in events if event.get("phase") == "completed"][-1]
        output_ids = [entry["chapter_id"] for entry in final["chapter_outputs"]]
        assert output_ids == chapter_ids, output_ids
        assert len(chapter_ids) == len(set(output_ids)), "duplicate chapters in resumed output"
        assert trace["mean_chapter_passes_used"] == 1.0
        assert sample.summary_md.strip()
        # checkpointed 2 * 0.001 + resumed mid-flight 0.0005; no double counting.
        assert abs(trace["generation_cost"] - 0.0025) < 1e-9, trace["generation_cost"]
    print("test_run_book_sample_resume_keeps_completed_and_resumes_midflight: OK")


def test_run_candidate_parallelizes_chapter_bench_items():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")
        client = FakeCloudFunctionClient(delay=0.02)
        install_fake_cf(client)
        try:
            args = build_run_args(tmp, run_id="parallel-smoke", concurrency=3)
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        state = json.loads((run_dir / "parallel-smoke.state.json").read_text())
        assert state["status"] == "finished", state["status"]
        assert state["completed_count"] == 4, state["completed_count"]
        assert state["current_items"] == {}, state["current_items"]
        assert state["current_item"] is None
        assert 2 <= client.max_in_flight <= 3, client.max_in_flight
        sample_rows = [json.loads(line) for line in (run_dir / "parallel-smoke.samples.jsonl").read_text().splitlines() if line.strip()]
        assert len(sample_rows) == 4, len(sample_rows)
        assert sorted(row["sample_index"] for row in sample_rows) == [0, 1, 2, 3]
        assert (run_dir / "parallel-smoke.json").exists()
    print("test_run_candidate_parallelizes_chapter_bench_items: OK")


def test_run_candidate_resume_runs_only_remaining_items():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")

        # Phase 1: full parallel run.
        first_client = FakeCloudFunctionClient(delay=0.02)
        install_fake_cf(first_client)
        try:
            args = build_run_args(tmp, run_id="resume-smoke", concurrency=3)
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        samples_path = run_dir / "resume-smoke.samples.jsonl"
        state_path = run_dir / "resume-smoke.state.json"
        out_path = run_dir / "resume-smoke.json"

        # Simulate a crash after the first two items checkpointed.
        rows = [json.loads(line) for line in samples_path.read_text().splitlines() if line.strip()]
        rows.sort(key=lambda row: row["sample_index"])
        kept = rows[:2]
        samples_path.write_text("\n".join(json.dumps(row) for row in kept) + "\n")
        state = json.loads(state_path.read_text())
        state["status"] = "running"
        state["current_item"] = None
        state["current_items"] = {}
        state["completed_count"] = len(kept)
        state["completed_sample_ids"] = [row["item_key"] for row in kept]
        state_path.write_text(json.dumps(state))
        out_path.unlink()

        # Phase 2: resume with a fresh client; only the two missing items run.
        resume_client = FakeCloudFunctionClient()
        install_fake_cf(resume_client)
        try:
            args = build_run_args(tmp, run_id="resume-smoke", concurrency=3, resume="resume-smoke")
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        assert resume_client.calls > 0
        assert resume_client.calls < first_client.calls, "resume should not regenerate checkpointed items"
        assert first_client.max_in_flight >= 2
        final_state = json.loads(state_path.read_text())
        assert final_state["status"] == "finished", final_state["status"]
        assert final_state["completed_count"] == 4, final_state["completed_count"]
        assert final_state["current_items"] == {}
        final_rows = [json.loads(line) for line in samples_path.read_text().splitlines() if line.strip()]
        assert len(final_rows) == 4, len(final_rows)
        assert sorted(row["sample_index"] for row in final_rows) == [0, 1, 2, 3]
        final_keys = {row["item_key"] for row in final_rows}
        assert len(final_keys) == 4
        assert out_path.exists()
    print("test_run_candidate_resume_runs_only_remaining_items: OK")


def test_run_candidate_parallel_pauses_on_insufficient_credits():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")
        client = FakeCloudFunctionClient(delay=0.01, fail_after=3)
        install_fake_cf(client)
        try:
            args = build_run_args(tmp, run_id="credits-smoke", concurrency=2)
            try:
                rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
                raise AssertionError("expected SystemExit(2) on insufficient credits")
            except SystemExit as exit_signal:
                assert exit_signal.code == 2, exit_signal.code
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        state = json.loads((run_dir / "credits-smoke.state.json").read_text())
        assert state["status"] == "paused_insufficient_credits", state["status"]
        assert state["latest_error"] is not None
        assert not (run_dir / "credits-smoke.json").exists(), "no artifact should be written while paused"
        samples_path = run_dir / "credits-smoke.samples.jsonl"
        checkpointed = [line for line in samples_path.read_text().splitlines() if line.strip()] if samples_path.exists() else []
        assert state["completed_count"] == len(checkpointed)
        assert state["current_items"], "unfinished items must keep resume progress"
    print("test_run_candidate_parallel_pauses_on_insufficient_credits: OK")


def test_run_candidate_sequential_when_concurrency_one():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")
        client = FakeCloudFunctionClient(delay=0.005)
        install_fake_cf(client)
        try:
            args = build_run_args(tmp, run_id="sequential-smoke", concurrency=1)
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        state = json.loads((run_dir / "sequential-smoke.state.json").read_text())
        assert state["status"] == "finished", state["status"]
        assert state["completed_count"] == 4, state["completed_count"]
        assert client.max_in_flight == 1, client.max_in_flight
    print("test_run_candidate_sequential_when_concurrency_one: OK")


def main():
    tests = [
        test_cf_concurrency_slot_caps_inflight,
        test_run_book_sample_runs_chapters_concurrently_and_orders_outputs,
        test_run_book_sample_resume_keeps_completed_and_resumes_midflight,
        test_run_candidate_parallelizes_chapter_bench_items,
        test_run_candidate_resume_runs_only_remaining_items,
        test_run_candidate_parallel_pauses_on_insufficient_credits,
        test_run_candidate_sequential_when_concurrency_one,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()
