"""Offline tests for chapter-level parallel Cloud Function execution + resume.

Run with: python3 tests/test_chapter_parallel.py
No pytest required. Uses fake Cloud Function clients (no network / no LLM calls).

Covers:
  * cf_concurrency_slot bounding in-flight Cloud Function invocations
  * run_book_sample running chapters concurrently, in book order, with resume
    of both checkpointed chapters and mid-flight chapter stage state
  * run_candidate._run_single parallelizing chapter bench items, checkpointing
    each item, recovering on resume, and pausing on insufficient credits
  * ModelUnavailableError detection/backoff at the CF client, the runner banner,
    and a run pausing (not failing items) when the gateway has no deployment
"""

import dataclasses
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core import cf_client
from core import run_candidate as rc
from core.openrouter_client import (
    GenerationResult,
    ModelUnavailableError,
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
    cap). Can raise an insufficient-credits error after N calls, or inject a
    generic failure for a specific chapter identified by its source text.
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        summary_words: int = 80,
        fail_after: Optional[int] = None,
        fail_source_exact: Optional[str] = None,
        fail_source_marker: Optional[str] = None,
        fail_exception_factory=None,
        model_unavailable: bool = False,
        model_unavailable_model: str = "gpt-5.6-luna",
    ):
        self.delay = delay
        self.summary_words = summary_words
        self.fail_after = fail_after
        self.fail_source_exact = fail_source_exact
        self.fail_source_marker = fail_source_marker
        self.fail_exception_factory = fail_exception_factory
        self.model_unavailable = model_unavailable
        self.model_unavailable_model = model_unavailable_model
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        self.function_url = "http://localhost/fake-cf"
        self.auth_mode = "none"
        self.last_meta = {}
        self.scoring_version_mismatch = False

    def _should_fail_source(self, source_md: str) -> bool:
        if self.fail_source_exact is not None and source_md == self.fail_source_exact:
            return True
        return bool(self.fail_source_marker and self.fail_source_marker in source_md)

    def chat_completion(self, request_body, **kwargs):
        with self._lock:
            self.calls += 1
            call_index = self.calls
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.fail_after is not None and call_index > self.fail_after:
                raise OpenRouterInsufficientCreditsError(402, "/chat/completions", "no credits")
            if self.model_unavailable:
                raise ModelUnavailableError(
                    500,
                    "http://localhost/fake-cf",
                    f"Internal error: no healthy deployments for model group {self.model_unavailable_model}",
                    model=self.model_unavailable_model,
                )
            source_md = str(kwargs.get("source_md") or "")
            if self._should_fail_source(source_md):
                factory = self.fail_exception_factory or (
                    lambda: RuntimeError("injected chapter failure")
                )
                raise factory()
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
            "# Chapter\n\n" + f"marker-chapter-{chapter_id} " + ("source word " * source_words) + "\n",
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


def test_run_book_sample_isolates_chapter_exception_and_continues():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        data_dir, chapter_ids = make_synthetic_book(tmp, n_chapters=6)
        spec = chapter_parallel_spec()
        client = FakeCloudFunctionClient(delay=0.01, fail_source_marker="marker-chapter-002")
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

        final = [event for event in events if event.get("phase") == "completed"][-1]
        output_ids = [entry["chapter_id"] for entry in final["chapter_outputs"]]
        assert "002" not in output_ids, output_ids
        assert output_ids == [chapter_id for chapter_id in chapter_ids if chapter_id != "002"], output_ids
        assert final["chapter_errors"]["002"]["type"] == "RuntimeError"
        assert trace["n_failed_chapters"] == 1
        assert trace["chapter_errors"]["002"]["message"] == "injected chapter failure"
        # The failure is isolated: siblings still generated (and are not cancelled).
        assert client.calls >= len(chapter_ids) - 1
        # A partial book sample is still produced from the surviving chapters.
        assert sample.summary_md.strip()
    print("test_run_book_sample_isolates_chapter_exception_and_continues: OK")


def test_run_book_sample_all_chapters_failing_raises():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        data_dir, chapter_ids = make_synthetic_book(tmp, n_chapters=3)
        spec = chapter_parallel_spec()
        # Marker appears in every synthetic chapter's source.
        client = FakeCloudFunctionClient(fail_source_marker="marker-chapter-")
        install_fake_cf(client)
        rc.set_cf_concurrency(2)
        try:
            try:
                rc.run_book_sample(
                    book_item("synthetic-book"),
                    candidate_module=candidate_spec,
                    spec=spec,
                    client=client,
                    data_dir=data_dir,
                    judge_model="",
                    judge_source_char_limit=32000,
                    resume_progress=None,
                    progress_callback=lambda payload: None,
                    run_dir=tmp / "runs",
                    chapter_concurrency=2,
                )
                raise AssertionError("expected ChapterRunError when every chapter fails")
            except rc.ChapterRunError as exc:
                assert exc.book_id == "synthetic-book"
                assert set(exc.chapter_errors) == set(chapter_ids)
        finally:
            uninstall_fake_cf()
    print("test_run_book_sample_all_chapters_failing_raises: OK")


def test_run_candidate_parallel_isolates_item_exception_and_continues():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")
        target_book, target_chapter = "atomic-habits-clear-2018", "032"
        target_key = f"{target_book}:{target_chapter}"
        book = rc.load_book_context(target_book, ROOT / "data" / "books")
        target_source = next(ch.source_md for ch in book.chapters if ch.chapter_id == target_chapter)

        failing_client = FakeCloudFunctionClient(delay=0.01, fail_source_exact=target_source)
        install_fake_cf(failing_client)
        try:
            args = build_run_args(tmp, run_id="item-error-smoke", concurrency=3)
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        state = json.loads((run_dir / "item-error-smoke.state.json").read_text())
        assert state["status"] == "finished_with_errors", state["status"]
        assert target_key in state["failed_items"], state["failed_items"]
        assert state["failed_items"][target_key]["message"] == "injected chapter failure"
        assert state["completed_count"] == 3, state["completed_count"]
        rows = [
            json.loads(line)
            for line in (run_dir / "item-error-smoke.samples.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert len(rows) == 3, len(rows)
        assert target_key not in {row["item_key"] for row in rows}
        assert (run_dir / "item-error-smoke.json").exists(), "partial artifact should still be written"

        # Resume with a healthy client: only the previously failed item runs.
        healthy_client = FakeCloudFunctionClient(delay=0.005)
        install_fake_cf(healthy_client)
        try:
            args = build_run_args(tmp, run_id="item-error-smoke", concurrency=3, resume="item-error-smoke")
            rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
        finally:
            uninstall_fake_cf()

        resumed_state = json.loads((run_dir / "item-error-smoke.state.json").read_text())
        assert resumed_state["status"] == "finished", resumed_state["status"]
        assert resumed_state["completed_count"] == 4, resumed_state["completed_count"]
        assert resumed_state["failed_items"] == {}, resumed_state["failed_items"]
        rows = [
            json.loads(line)
            for line in (run_dir / "item-error-smoke.samples.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert len(rows) == 4, len(rows)
        assert target_key in {row["item_key"] for row in rows}
        assert healthy_client.calls > 0
    print("test_run_candidate_parallel_isolates_item_exception_and_continues: OK")


def test_run_book_sample_propagates_insufficient_credits():
    """Credits errors must escape chapter isolation so the run can pause/resume."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        data_dir, _chapter_ids = make_synthetic_book(tmp, n_chapters=4)
        spec = chapter_parallel_spec()
        client = FakeCloudFunctionClient(fail_after=1)
        install_fake_cf(client)
        rc.set_cf_concurrency(2)
        try:
            try:
                rc.run_book_sample(
                    book_item("synthetic-book"),
                    candidate_module=candidate_spec,
                    spec=spec,
                    client=client,
                    data_dir=data_dir,
                    judge_model="",
                    judge_source_char_limit=32000,
                    resume_progress=None,
                    progress_callback=lambda payload: None,
                    run_dir=tmp / "runs",
                    chapter_concurrency=2,
                )
                raise AssertionError("expected OpenRouterInsufficientCreditsError to propagate")
            except OpenRouterInsufficientCreditsError:
                pass
        finally:
            uninstall_fake_cf()
    print("test_run_book_sample_propagates_insufficient_credits: OK")


# --------------------------------------------------------------------------- #
# Model-unavailable (gateway has no healthy deployment) handling
# --------------------------------------------------------------------------- #


class _FakeHTTPResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = self

    def get_content_charset(self):
        return "utf-8"

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _model_unavailable_http_error(url: str, model: str = "gpt-5.6-luna"):
    body = json.dumps(
        {"success": False, "error": f"Internal error: no healthy deployments for model group {model}"}
    )
    return urllib.error.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(body.encode("utf-8")))


class _PatchUrlopen:
    def __init__(self, handler):
        self.handler = handler
        self._original = None

    def __enter__(self):
        self._original = cf_client.urllib.request.urlopen
        cf_client.urllib.request.urlopen = self.handler
        return self

    def __exit__(self, *exc_info):
        cf_client.urllib.request.urlopen = self._original
        return False


def test_cf_client_model_unavailable_retries_then_raises():
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise _model_unavailable_http_error(request.full_url)

    original_backoff = cf_client.MODEL_UNAVAILABLE_BACKOFF
    cf_client.MODEL_UNAVAILABLE_BACKOFF = (0.0, 0.0, 0.0)
    try:
        client = cf_client.CloudFunctionClient(
            function_url="http://localhost/fake-cf",
            auth_mode="none",
            max_retries=1,
            model_unavailable_retries=2,
        )
        with _PatchUrlopen(fake_urlopen):
            try:
                client._request_json({"model": "gpt-5.6-luna"})
                raise AssertionError("expected ModelUnavailableError")
            except ModelUnavailableError as exc:
                assert exc.model == "gpt-5.6-luna", exc.model
                assert exc.status_code == 500, exc.status_code
                assert "no healthy deployments" in str(exc)
        assert calls["n"] == 3, calls["n"]  # 1 initial + 2 bounded model-unavailable retries
    finally:
        cf_client.MODEL_UNAVAILABLE_BACKOFF = original_backoff
    print("test_cf_client_model_unavailable_retries_then_raises: OK")


def test_cf_client_model_unavailable_recovers_on_retry():
    calls = {"n": 0}
    good_body = json.dumps({"summary": {"summary_md": "ok", "estimated_visible_words": 1}})

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _model_unavailable_http_error(request.full_url)
        return _FakeHTTPResponse(good_body)

    original_backoff = cf_client.MODEL_UNAVAILABLE_BACKOFF
    cf_client.MODEL_UNAVAILABLE_BACKOFF = (0.0, 0.0, 0.0)
    try:
        client = cf_client.CloudFunctionClient(
            function_url="http://localhost/fake-cf",
            auth_mode="none",
            max_retries=1,
            model_unavailable_retries=3,
        )
        with _PatchUrlopen(fake_urlopen):
            response = client._request_json({"model": "gpt-5.6-luna"})
        assert response["summary"]["summary_md"] == "ok"
        assert calls["n"] == 2, calls["n"]
    finally:
        cf_client.MODEL_UNAVAILABLE_BACKOFF = original_backoff
    print("test_cf_client_model_unavailable_recovers_on_retry: OK")


def test_model_unavailable_banner_names_model_and_resume_command():
    with tempfile.TemporaryDirectory() as tmp_name:
        args = build_run_args(Path(tmp_name), run_id="banner-smoke", concurrency=1)
    exc = ModelUnavailableError(500, "http://localhost/fake-cf", "no healthy deployments", model="gpt-5.6-luna")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc.print_model_unavailable_banner(
            exc, bench_name="chapter_fast", profile=PROFILE, run_id="banner-smoke", args=args
        )
    text = buffer.getvalue()
    assert "MODEL UNAVAILABLE: gpt-5.6-luna" in text, text
    assert "--resume banner-smoke" in text, text
    assert "--profile 30m_deepseek-v4-flash_notthinking" in text, text
    print("test_model_unavailable_banner_names_model_and_resume_command: OK")


def test_run_candidate_pauses_on_model_unavailable():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        manifest = load_benchmark_manifest(ROOT / "benchmark_version.json")
        client = FakeCloudFunctionClient(model_unavailable=True)
        install_fake_cf(client)
        try:
            args = build_run_args(tmp, run_id="model-unavailable-smoke", concurrency=2)
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    rc._run_single(args, manifest, ROOT / "candidate_spec.py", inherited_run_id=None)
                raise AssertionError("expected SystemExit(3) on model unavailable")
            except SystemExit as exit_signal:
                assert exit_signal.code == 3, exit_signal.code
        finally:
            uninstall_fake_cf()

        run_dir = tmp / "runs" / run_dir_for(manifest)
        state = json.loads((run_dir / "model-unavailable-smoke.state.json").read_text())
        assert state["status"] == "paused_model_unavailable", state["status"]
        assert state["latest_error"]["type"] == "ModelUnavailableError", state["latest_error"]
        assert state["latest_error"]["message"].find("no healthy deployments") >= 0
        assert not (run_dir / "model-unavailable-smoke.json").exists(), "no artifact while paused"
        assert "MODEL UNAVAILABLE: gpt-5.6-luna" in buffer.getvalue()
    print("test_run_candidate_pauses_on_model_unavailable: OK")


def main():
    tests = [
        test_cf_concurrency_slot_caps_inflight,
        test_run_book_sample_runs_chapters_concurrently_and_orders_outputs,
        test_run_book_sample_resume_keeps_completed_and_resumes_midflight,
        test_run_book_sample_isolates_chapter_exception_and_continues,
        test_run_book_sample_all_chapters_failing_raises,
        test_run_book_sample_propagates_insufficient_credits,
        test_run_candidate_parallelizes_chapter_bench_items,
        test_run_candidate_resume_runs_only_remaining_items,
        test_run_candidate_parallel_pauses_on_insufficient_credits,
        test_run_candidate_parallel_isolates_item_exception_and_continues,
        test_run_candidate_sequential_when_concurrency_one,
        test_cf_client_model_unavailable_retries_then_raises,
        test_cf_client_model_unavailable_recovers_on_retry,
        test_model_unavailable_banner_names_model_and_resume_command,
        test_run_candidate_pauses_on_model_unavailable,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()
