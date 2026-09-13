#!/usr/bin/env python3
"""Run the frozen benchmark harness against an editable candidate spec.

This script supports:
- chapter-level fast benchmarking
- full-book gate and holdout benchmarking
- optional OpenRouter-based rubric judging
- deterministic mock generation for smoke tests
- versioned benchmark manifests and run artifacts
- catalog and pricing snapshots for future model comparisons

The only file autoresearch should edit is ``candidate_spec.py``.
"""

from __future__ import annotations

import argparse
import copy
import csv
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait as futures_wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.book_data import BookTaxonomy, taxonomy_from_manifest
from core.dotenv import load_dotenv
from core.judge import AbsoluteJudgeResult, judge_summary_absolute
from core.openrouter_client import (
    GenerationResult,
    ModelUnavailableError,
    OpenRouterAPIError,
    OpenRouterHTTPError,
    OpenRouterClient,
    OpenRouterInsufficientCreditsError,
    UsageRecord,
)
from core.cf_client import CloudFunctionAuthError
from core.reasoning import DEFAULT_MAX_TOKEN_CAP, effort_enables_thinking, manifest_effort_label, stage_reasoning_effort, visible_max_tokens
from core import prompt_registry
from core.versioning import (
    build_prompt_hashes,
    build_run_id,
    derive_price_snapshot_from_catalog,
    ensure_default_benchmark_manifest,
    load_benchmark_manifest,
    save_json,
    sha256_file,
)
from scoring import JudgeScores, Rubric, SummarySample, readability_metrics, score_dataset, visible_word_count, DEFAULT_SCORING_CONFIG, apply_gates_override


PROGRESS_PRINT_INTERVAL_S = 15.0
_progress_print_lock = threading.Lock()
_last_progress_print: Dict[str, Any] = {
    "ts": 0.0,
    "phase": "",
    "item_key": "",
    "chapter_id": "",
    "passes_used": -1,
}

# Cloud Function concurrency gate. When a --chapter-concurrency > 1 is active,
# chapter generation is dispatched from a thread pool; the gate bounds how many
# Cloud Function invocations are in flight at once. Unset => no cap.
_cf_concurrency_gate: Optional[threading.Semaphore] = None
_cf_concurrency_gate_lock = threading.Lock()


def set_cf_concurrency(concurrency: Optional[int]) -> None:
    """Configure the module-level Cloud Function concurrency gate.

    ``None`` or ``< 1`` disables the gate (unlimited in-flight invocations).
    """
    global _cf_concurrency_gate
    with _cf_concurrency_gate_lock:
        if concurrency is None or concurrency < 1:
            _cf_concurrency_gate = None
        else:
            _cf_concurrency_gate = threading.Semaphore(int(concurrency))


@contextmanager
def cf_concurrency_slot():
    """Bound concurrent Cloud Function invocations when a gate is configured.

    No-op when the gate is disabled. Safe to enter from any worker thread.
    """
    gate = _cf_concurrency_gate
    if gate is None:
        yield
        return
    gate.acquire()
    try:
        yield
    finally:
        gate.release()


def maybe_print_progress(run_id: str, payload: Mapping[str, Any]) -> None:
    global _last_progress_print
    now = time.time()
    phase = str(payload.get("phase") or "")
    item_key = str(payload.get("item_key") or "")
    chapter_id = str(payload.get("chapter_id") or "")
    stage_state = payload.get("stage_state")
    passes_used = -1
    if isinstance(stage_state, Mapping):
        passes_used = int(stage_state.get("passes_used") or 0)
    with _progress_print_lock:
        changed = (
            phase != _last_progress_print["phase"]
            or item_key != _last_progress_print["item_key"]
            or chapter_id != _last_progress_print["chapter_id"]
            or passes_used != _last_progress_print["passes_used"]
        )
        if not changed and (now - float(_last_progress_print["ts"])) < PROGRESS_PRINT_INTERVAL_S:
            return
        _last_progress_print = {
            "ts": now,
            "phase": phase,
            "item_key": item_key,
            "chapter_id": chapter_id,
            "passes_used": passes_used,
        }
    pieces = [f"[{run_id}]", item_key, phase]
    if chapter_id and phase in ("stage", "chapters", "judge"):
        pieces.append(f"chapter={chapter_id}")
    target = payload.get("target_words")
    if target is not None:
        pieces.append(f"target={target}w")
    stage_state = payload.get("stage_state")
    if isinstance(stage_state, Mapping):
        pieces.append(f"pass={int(stage_state.get('passes_used') or 0)}")
        pieces.append(f"words={visible_word_count(str(stage_state.get('summary_md') or ''))}")
        pieces.append(f"cost=${float(stage_state.get('generation_cost') or 0.0):.4f}")
    print(" ".join(pieces), flush=True)


@dataclass(frozen=True)
class ChapterContext:
    chapter_id: str
    chapter_title: str
    source_path: Path
    source_md: str
    rubric_path: Path
    rubric: Rubric
    visible_words: int


@dataclass(frozen=True)
class BookContext:
    book_id: str
    book_title: str
    book_dir: Path
    toc_md: str
    metadata_md: str
    chapters: Tuple[ChapterContext, ...]
    book_rubric_path: Path
    book_rubric: Rubric
    total_visible_words: int
    taxonomy: BookTaxonomy


@dataclass(frozen=True)
class StageRun:
    summary_md: str
    first_pass_summary_md: str
    passes_used: int
    generation_cost: float
    uncached_generation_cost: float
    raw_responses: Tuple[Mapping[str, Any], ...]
    pass_latency_seconds: Tuple[float, ...] = ()


SLICE_FIELDS: Tuple[str, ...] = (
    "genre_macro",
    "genre_micro",
    "narrative_vs_expository",
    "prescriptive_vs_analytical",
    "quantitative_density",
    "chapter_length_profile",
    "benchmark_pool",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _is_thinking_enabled(spec) -> bool:
    effort = stage_reasoning_effort(spec.chapter_stage)
    if effort is not None:
        return effort_enables_thinking(effort)
    chapter_extra = spec.chapter_stage.extra_body or {}
    thinking_cfg = chapter_extra.get("thinking")
    if thinking_cfg and isinstance(thinking_cfg, dict):
        if thinking_cfg.get("type") == "disabled":
            return False
    return True


def _reasoning_effort_label(spec) -> str:
    return manifest_effort_label(spec.chapter_stage)


def error_to_dict(exc: BaseException) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    for attr in ("status_code", "path", "response_text"):
        if hasattr(exc, attr):
            value = getattr(exc, attr)
            if value not in (None, ""):
                payload[attr] = str(value)
    return payload


class ChapterRunError(RuntimeError):
    """Raised when every chapter in a book sample failed.

    Individual chapter failures are isolated and recorded on the sample trace
    (``chapter_errors``); this aggregate error is only used when there is no
    chapter output left to compose.
    """

    def __init__(self, book_id: str, chapter_errors: Mapping[str, Mapping[str, Any]]):
        super().__init__(f"All {len(chapter_errors)} chapter(s) failed for {book_id}")
        self.book_id = book_id
        self.chapter_errors = {str(key): dict(value) for key, value in chapter_errors.items()}


def resume_command_hint(*, bench_name: str, profile: str, run_id: str, args: argparse.Namespace) -> str:
    hint = f"python core/run_candidate.py --bench {bench_name} --profile {profile} --resume {run_id}"
    judge_model = getattr(args, "judge_model", "")
    if judge_model:
        hint += f" --judge-model {judge_model}"
    if getattr(args, "write_results", False):
        hint += " --write-results"
    return hint


def print_model_unavailable_banner(
    exc: BaseException,
    *,
    bench_name: str,
    profile: str,
    run_id: str,
    args: argparse.Namespace,
) -> None:
    model = str(getattr(exc, "model", "") or "<unknown>")
    print()
    print("=" * 72)
    print(f"MODEL UNAVAILABLE: {model}")
    print("The provider/gateway reported no healthy deployment for this model.")
    print("These attempts never reached a model, so they were not billed.")
    print("Restore the deployment/fallback upstream, then resume this run with:")
    print("  " + resume_command_hint(bench_name=bench_name, profile=profile, run_id=run_id, args=args))
    print("=" * 72)
    print()


def print_cf_auth_error_banner(
    exc: BaseException,
    *,
    bench_name: str,
    profile: str,
    run_id: str,
    args: argparse.Namespace,
) -> None:
    print()
    print("=" * 72)
    print("CLOUD FUNCTION AUTH FAILED")
    print("The OIDC identity token could not be minted, so no requests were sent and")
    print("nothing was billed. Re-authenticate, then resume this run with:")
    print()
    print("  gcloud auth login")
    print("  " + resume_command_hint(bench_name=bench_name, profile=profile, run_id=run_id, args=args))
    print()
    print("Or run with AUTH_MODE=env and GCP_IDENTITY_TOKEN set (CI/service credentials).")
    print("=" * 72)
    print()
    print(f"details: {exc}", file=sys.stderr)


def sample_key_from_item(item: Mapping[str, Any], *, bench_name: str) -> str:
    level = str(item.get("level", ""))
    if level == "chapter" or bench_name == "chapter_fast":
        return str(item.get("sample_id") or f"{item['book_id']}:{item['chapter_id']}")
    return str(item.get("sample_id") or item["book_id"])


def judge_scores_to_dict(scores: Optional[JudgeScores]) -> Optional[Dict[str, float]]:
    if scores is None:
        return None
    return {
        "faithfulness": float(scores.faithfulness),
        "concept_coverage": float(scores.concept_coverage),
        "qualifier_preservation": float(scores.qualifier_preservation),
        "no_fluff": float(scores.no_fluff),
        "structure_quality": float(scores.structure_quality),
    }


def judge_scores_from_dict(payload: Any) -> Optional[JudgeScores]:
    if not isinstance(payload, Mapping):
        return None
    return JudgeScores(
        faithfulness=float(payload.get("faithfulness", 0.0) or 0.0),
        concept_coverage=float(payload.get("concept_coverage", 0.0) or 0.0),
        qualifier_preservation=float(payload.get("qualifier_preservation", 0.0) or 0.0),
        no_fluff=float(payload.get("no_fluff", 0.0) or 0.0),
        structure_quality=float(payload.get("structure_quality", 0.0) or 0.0),
    )


def serialize_stage_run(stage_run: StageRun) -> Dict[str, Any]:
    return {
        "summary_md": stage_run.summary_md,
        "first_pass_summary_md": stage_run.first_pass_summary_md,
        "passes_used": int(stage_run.passes_used),
        "generation_cost": float(stage_run.generation_cost),
        "uncached_generation_cost": float(stage_run.uncached_generation_cost),
        "raw_responses": _json_safe(list(stage_run.raw_responses)),
        "pass_latency_seconds": [float(v) for v in (stage_run.pass_latency_seconds or ())],
    }


def deserialize_stage_run(payload: Mapping[str, Any]) -> StageRun:
    return StageRun(
        summary_md=str(payload.get("summary_md") or ""),
        first_pass_summary_md=str(payload.get("first_pass_summary_md") or ""),
        passes_used=int(payload.get("passes_used") or 0),
        generation_cost=float(payload.get("generation_cost") or 0.0),
        uncached_generation_cost=float(payload.get("uncached_generation_cost") or 0.0),
        raw_responses=tuple(payload.get("raw_responses") or []),
        pass_latency_seconds=tuple(float(v) for v in (payload.get("pass_latency_seconds") or [])),
    )


def serialize_completed_chapter_runs(chapter_outputs: Sequence[Tuple[ChapterContext, StageRun]]) -> List[Dict[str, Any]]:
    return [
        {
            "chapter_id": chapter.chapter_id,
            "chapter_title": chapter.chapter_title,
            "stage_run": serialize_stage_run(stage_run),
        }
        for chapter, stage_run in chapter_outputs
    ]


def build_sample_record(sample: SummarySample, trace: Mapping[str, Any], *, item_key: str) -> Dict[str, Any]:
    trace_payload = _json_safe(dict(trace))
    return {
        "item_key": item_key,
        "sample_id": sample.sample_id,
        "level": sample.level,
        "group_id": sample.group_id,
        "target_words": int(sample.target_words),
        "summary_md": sample.summary_md,
        "first_pass_summary_md": sample.first_pass_summary_md,
        "passes_used": int(sample.passes_used),
        "generation_cost": float(sample.generation_cost),
        "uncached_generation_cost": float(sample.uncached_generation_cost),
        "malformed": bool(sample.malformed),
        "judge_scores": judge_scores_to_dict(sample.judge_scores),
        "book_id": str(trace_payload.get("book_id") or sample.group_id),
        "chapter_id": str(trace_payload.get("chapter_id") or ""),
        "trace": trace_payload,
    }


def deserialize_sample_record(record: Mapping[str, Any], data_dir: Path) -> Tuple[SummarySample, Dict[str, Any], str]:
    level = str(record.get("level") or "chapter")
    book_id = str(record.get("book_id") or record.get("group_id") or "")
    if not book_id:
        raise ValueError(f"Checkpoint record is missing book_id: {record}")
    book = load_book_context(book_id, data_dir)
    if level == "chapter":
        chapter_id = str(record.get("chapter_id") or "")
        chapter = next((ch for ch in book.chapters if ch.chapter_id == chapter_id), None)
        if chapter is None:
            raise KeyError(f"Checkpoint references missing chapter {chapter_id!r} in book {book_id!r}")
        source_md = chapter.source_md
        rubric = chapter.rubric
    else:
        source_md = join_book_source(book)
        rubric = book.book_rubric

    sample = SummarySample(
        sample_id=str(record.get("sample_id") or ""),
        level=level,
        target_words=int(record.get("target_words") or 0),
        summary_md=str(record.get("summary_md") or ""),
        source_md=source_md,
        group_id=str(record.get("group_id") or book_id),
        first_pass_summary_md=str(record.get("first_pass_summary_md") or ""),
        passes_used=int(record.get("passes_used") or 0),
        generation_cost=float(record.get("generation_cost") or 0.0),
        uncached_generation_cost=float(record.get("uncached_generation_cost") or 0.0),
        malformed=bool(record.get("malformed")),
        rubric=rubric,
        judge_scores=judge_scores_from_dict(record.get("judge_scores")),
    )
    return sample, dict(record.get("trace") or {}), str(record.get("item_key") or sample.sample_id)


def append_sample_checkpoint(
    *,
    path: Path,
    run_id: str,
    sample_index: int,
    item_key: str,
    sample: SummarySample,
    trace: Mapping[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = build_sample_record(sample, trace, item_key=item_key)
    record["run_id"] = run_id
    record["sample_index"] = int(sample_index)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_sample_checkpoints(path: Path, data_dir: Path) -> Tuple[List[SummarySample], List[Mapping[str, Any]], List[str]]:
    if not path.exists():
        return [], [], []

    latest_by_key: Dict[str, Dict[str, Any]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        record = json.loads(line)
        item_key = str(record.get("item_key") or record.get("sample_id") or "")
        if not item_key:
            continue
        latest_by_key[item_key] = record

    ordered_records = sorted(
        latest_by_key.values(),
        key=lambda row: (int(row.get("sample_index") or 0), str(row.get("item_key") or row.get("sample_id") or "")),
    )
    samples: List[SummarySample] = []
    traces: List[Mapping[str, Any]] = []
    completed_item_keys: List[str] = []
    for record in ordered_records:
        sample, trace, item_key = deserialize_sample_record(record, data_dir)
        samples.append(sample)
        traces.append(trace)
        completed_item_keys.append(item_key)
    return samples, traces, completed_item_keys


def initial_progress_for_item(item: Mapping[str, Any], *, bench_name: str) -> Dict[str, Any]:
    item_key = sample_key_from_item(item, bench_name=bench_name)
    level = str(item.get("level", ""))
    if level == "chapter" or bench_name == "chapter_fast":
        return {
            "kind": "chapter",
            "phase": "stage",
            "item_key": item_key,
            "item": _json_safe(dict(item)),
            "book_id": str(item["book_id"]),
            "chapter_id": str(item["chapter_id"]),
            "sample_id": item_key,
        }
    return {
        "kind": "book",
        "phase": "chapters",
        "item_key": item_key,
        "item": _json_safe(dict(item)),
        "book_id": str(item["book_id"]),
        "sample_id": item_key,
    }


def save_run_state(path: Path, state: Mapping[str, Any]) -> None:
    payload = dict(state)
    payload["updated_at_utc"] = utc_now_iso()
    save_json(path, payload)


def validate_resume_state(
    state: Mapping[str, Any],
    *,
    run_id: str,
    bench_name: str,
    profile: str,
    spec_path: Path,
    benchmark_manifest: Mapping[str, Any],
    judge_model: str,
) -> None:
    if str(state.get("run_id") or "") != run_id:
        raise ValueError(f"Run state run_id mismatch: expected {run_id!r}, found {state.get('run_id')!r}")
    run_manifest = state.get("run_manifest") or {}
    if str(run_manifest.get("bench") or "") != bench_name:
        raise ValueError(f"Resume bench mismatch: expected {bench_name!r}, found {run_manifest.get('bench')!r}")
    if str(run_manifest.get("profile") or "") != profile:
        raise ValueError(f"Resume profile mismatch: expected {profile!r}, found {run_manifest.get('profile')!r}")
    expected_candidate_sha = sha256_file(spec_path)
    actual_candidate_sha = str(run_manifest.get("candidate_spec_sha256") or "")
    if actual_candidate_sha != expected_candidate_sha:
        raise ValueError(
            "Candidate spec changed since the run was created. Restore the original candidate_spec.py or start a new run."
        )
    expected_benchmark_version = str(benchmark_manifest.get("benchmark_version") or "")
    actual_benchmark_version = str(run_manifest.get("benchmark_version") or "")
    if actual_benchmark_version != expected_benchmark_version:
        raise ValueError(
            f"Benchmark version mismatch: expected {expected_benchmark_version!r}, found {actual_benchmark_version!r}"
        )
    actual_judge_model = str(run_manifest.get("judge_model") or "")
    if actual_judge_model != str(judge_model or ""):
        raise ValueError(
            f"Judge model mismatch on resume: expected {actual_judge_model!r} from the run state, got {judge_model!r}"
        )


def wait_for_credits(*, client: Optional[OpenRouterClient], args: argparse.Namespace, run_id: str) -> bool:
    poll_seconds = max(1, int(args.credit_poll_seconds))
    deadline = (time.time() + int(args.max_credit_wait_seconds)) if int(args.max_credit_wait_seconds) > 0 else None
    management_key = os.getenv(args.management_key_env, "")

    if _is_cloud_function(client):
        print("[credits] management-key polling unavailable in Cloud Function mode.")
    elif management_key and client is not None:
        while True:
            try:
                credits = client.get_credits(api_key_override=management_key)
                if credits.remaining_credits > 0:
                    print(
                        f"[credits] {run_id}: credits restored "
                        f"(remaining={credits.remaining_credits:.4f}). Retrying the paused sample."
                    )
                    return True
                print(
                    f"[credits] {run_id}: remaining={credits.remaining_credits:.4f}. "
                    f"Checking again in {poll_seconds}s."
                )
            except Exception as exc:
                print(
                    f"[credits] {run_id}: could not poll /credits via {args.management_key_env}: {exc}. "
                    "Falling back to timed retries."
                )
                break
            if deadline is not None and (time.time() + poll_seconds) > deadline:
                return False
            time.sleep(poll_seconds)

    print(f"[credits] {run_id}: waiting {poll_seconds}s before retrying the same sample.")
    if deadline is not None and (time.time() + poll_seconds) > deadline:
        return False
    time.sleep(poll_seconds)
    return True
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", default=str(ROOT / "candidate_spec.py"))
    parser.add_argument("--bench", required=True, help="Benchmark name (chapter_fast, book_gate, book_holdout) or path to JSONL")
    parser.add_argument("--time", default="all", choices=["all", "30m", "60m"],
                        help="Filter candidates by time budget: '30m', '60m', or 'all' (default: all)")
    parser.add_argument("--profile", required=True,
                        help="Profile name to run (e.g. '30m_deepseek-v4-flash_notthinking'). Use 'all' with --time to run all matching profiles.")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "books"))
    parser.add_argument("--results-tsv", default=str(ROOT / "results.tsv"))
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    parser.add_argument("--benchmark-manifest", default=str(ROOT / "benchmark_version.json"))
    parser.add_argument("--catalog-snapshots-dir", default=str(ROOT / "snapshots" / "catalog"))
    parser.add_argument("--price-snapshots-dir", default=str(ROOT / "snapshots" / "pricing"))
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--pricing-snapshot", default="")
    parser.add_argument("--referer", default="")
    parser.add_argument("--title", default="autoresearch-book-summary-benchmark")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-source-char-limit", type=int, default=32000)
    parser.add_argument(
        "--scoring-profile",
        default="auto",
        help="Named scoring gate profile (e.g. 'strict-v1' for the 0.88/0.80 accuracy bar) "
        "or 'auto' (default) to derive gates from the time budget. Deterministic runs use the "
        "profile's '<name>_deterministic' variant when present.",
    )
    parser.add_argument("--hypothesis", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--mock", action="store_true", help="Use a deterministic mock summarizer instead of OpenRouter")
    parser.add_argument(
        "--prompt-version",
        default=os.getenv("PROMPT_VERSION", ""),
        help="Force a prompt-registry version for this run (applies to both stages unless "
        "--prompt-stage is set). Defaults to the PROMPT_VERSION env var.",
    )
    parser.add_argument(
        "--prompt-stage",
        default="both",
        choices=["both", "chapter", "composer"],
        help="Which stage the --prompt-version override applies to (default: both).",
    )
    parser.add_argument(
        "--prompt-slot",
        default=os.getenv("PROMPT_SLOT", ""),
        choices=["", "local", "production"],
        help="Prompt-registry slot to resolve when no explicit version is given. Defaults to "
        "'local' (or 'production' in Cloud Function transport mode).",
    )
    parser.add_argument(
        "--allow-prompt-override",
        action="store_true",
        help="Forward allow_prompt_override=true to the Cloud Function, enabling inline prompt "
        "experiments not present in the function's bundled registry.",
    )
    parser.add_argument(
        "--prompt-registry",
        default=os.getenv("PROMPT_REGISTRY_PATH", ""),
        help="Path to the prompt registry JSON (defaults to data/prompts/registry.json).",
    )
    parser.add_argument(
        "--function-url",
        default=os.getenv("FUNCTION_URL", ""),
        help="Cloud Function URL. When non-empty, run in Cloud Function transport mode "
        "(no local provider API key required). Defaults to the FUNCTION_URL env var.",
    )
    parser.add_argument(
        "--auth-mode",
        default=os.getenv("AUTH_MODE", "auto"),
        choices=["auto", "oidc", "env", "sa_key", "none"],
        help="Cloud Function auth mode. auto resolves to 'none' for http (local dev) and "
        "'oidc' for https. 'sa_key' mints a self-signed OIDC ID token from a service-account "
        "JSON key (GCP_SA_KEY_FILE), no gcloud needed. Defaults to the AUTH_MODE env var.",
    )
    parser.add_argument(
        "--sa-key-file",
        default=os.getenv("GCP_SA_KEY_FILE", ""),
        help="Path to a service-account JSON key for AUTH_MODE=sa_key. "
        "Defaults to the GCP_SA_KEY_FILE env var.",
    )
    parser.add_argument(
        "--identity-audience",
        default=os.getenv("CF_IDENTITY_AUDIENCE", ""),
        help="Audience for the minted ID token (AUTH_MODE=sa_key). Defaults to "
        "CF_IDENTITY_AUDIENCE, else the Function URL.",
    )
    parser.add_argument(
        "--function-timeout",
        type=int,
        default=int(os.getenv("CF_TIMEOUT", "600")),
        help="HTTP timeout in seconds for Cloud Function calls (CF max 3600). "
        "Defaults to the CF_TIMEOUT env var or 600.",
    )
    parser.add_argument(
        "--chapter-concurrency",
        type=int,
        default=int(os.getenv("CF_CHAPTER_CONCURRENCY", "1")),
        help="Run chapters in parallel via the Cloud Function. 1 (default) runs "
        "sequentially. >1 dispatches chapter generation from a thread pool, bounded "
        "by a shared semaphore on in-flight Cloud Function invocations. Only "
        "effective in Cloud Function transport mode. Defaults to the "
        "CF_CHAPTER_CONCURRENCY env var or 1.",
    )
    parser.add_argument("--run-id", default="", help="Optional explicit run id for a new run.")
    parser.add_argument("--resume", default="", help="Resume an existing run id from its checkpoint/state files.")
    parser.add_argument(
        "--wait-for-credits",
        action="store_true",
        help="On HTTP 402 insufficient credits, pause and keep retrying the same sample until credits return.",
    )
    parser.add_argument(
        "--management-key-env",
        default="OPENROUTER_MANAGEMENT_KEY",
        help="Environment variable containing an OpenRouter management key for polling /credits.",
    )
    parser.add_argument(
        "--credit-poll-seconds",
        type=int,
        default=60,
        help="Seconds between /credits checks or timed retry attempts while paused on insufficient credits.",
    )
    parser.add_argument(
        "--max-credit-wait-seconds",
        type=int,
        default=0,
        help="Maximum total wait time for restored credits. 0 means wait indefinitely.",
    )
    return parser.parse_args()


def load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"Benchmark manifest not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_rubric(path: Path) -> Rubric:
    if not path.exists():
        return Rubric()
    data = load_json(path)
    return Rubric(
        headings=tuple(data.get("headings") or []),
        core_concepts=tuple(data.get("core_concepts") or []),
        mechanisms_or_explanations=tuple(data.get("mechanisms_or_explanations") or []),
        critical_qualifiers=tuple(data.get("critical_qualifiers") or []),
        important_examples=tuple(data.get("important_examples") or []),
        key_entities_or_numbers=tuple(data.get("key_entities_or_numbers") or []),
        key_terms=tuple(data.get("key_terms") or []),
    )


def _read_optional(path: Path) -> str:
    if not str(path) or str(path) == ".":
        return ""
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def load_book_context(book_id: str, data_dir: Path) -> BookContext:
    book_dir = data_dir / book_id
    manifest_path = book_dir / "book.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing book manifest for {book_id}: {manifest_path}")
    manifest = load_json(manifest_path)
    book_title = str(manifest.get("book_title") or manifest.get("title") or book_id)
    toc_md = _read_optional(book_dir / str(manifest.get("toc_path", "")))
    metadata_md = _read_optional(book_dir / str(manifest.get("metadata_path", "")))
    taxonomy = taxonomy_from_manifest(manifest)

    chapters: List[ChapterContext] = []
    for chapter in manifest.get("chapters") or []:
        chapter_id = str(chapter["chapter_id"])
        chapter_title = str(chapter.get("title", chapter_id))
        source_path = book_dir / str(chapter["source_path"])
        source_md = source_path.read_text(encoding="utf-8")
        rubric_path = ROOT / "artifacts" / "rubrics" / book_id / f"{chapter_id}.json"
        rubric = load_rubric(rubric_path)
        chapters.append(
            ChapterContext(
                chapter_id=chapter_id,
                chapter_title=chapter_title,
                source_path=source_path,
                source_md=source_md,
                rubric_path=rubric_path,
                rubric=rubric,
                visible_words=visible_word_count(source_md),
            )
        )

    book_rubric_path = ROOT / "artifacts" / "book_rubrics" / f"{book_id}.json"
    total_visible_words = sum(ch.visible_words for ch in chapters)
    return BookContext(
        book_id=book_id,
        book_title=book_title,
        book_dir=book_dir,
        toc_md=toc_md,
        metadata_md=metadata_md,
        chapters=tuple(chapters),
        book_rubric_path=book_rubric_path,
        book_rubric=load_rubric(book_rubric_path),
        total_visible_words=total_visible_words,
        taxonomy=taxonomy,
    )


def make_client(args: argparse.Namespace) -> Optional[OpenRouterClient]:
    if args.mock:
        return None
    function_url = (args.function_url or os.getenv("FUNCTION_URL", "")).strip()
    if function_url:
        from core.cf_client import CloudFunctionClient

        return CloudFunctionClient(
            function_url=function_url,
            auth_mode=args.auth_mode,
            timeout=args.function_timeout,
            sa_key_file=args.sa_key_file,
            identity_audience=args.identity_audience,
        )
    return OpenRouterClient.from_env(
        api_key_env=args.api_key_env,
        pricing_snapshot_path=args.pricing_snapshot,
        referer=args.referer,
        title=args.title,
        timeout=600,
    )


def _is_cloud_function(client) -> bool:
    if client is None:
        return False
    try:
        from core.cf_client import CloudFunctionClient
    except ImportError:
        return False
    return isinstance(client, CloudFunctionClient)


# ── Prompt-version routing ──────────────────────────────────────────────
# Process-global prompt selection, read by run_length_controlled_stage when it
# attaches `_prompt_meta` to a request. Runs are sequential per process and the
# value is read-only during a run, so no lock is required.
_PROMPT_CONTEXT: Dict[str, Any] = {"chapter": {}, "composer": {}, "allow_override": False, "registry_sha": ""}
# Wall-clock latency (seconds) of every generation pass in the current run.
_LATENCY_ACCUMULATOR: List[float] = []


def set_prompt_context(*, chapter: Mapping[str, Any], composer: Mapping[str, Any], allow_override: bool, registry_sha: str) -> None:
    _PROMPT_CONTEXT["chapter"] = dict(chapter)
    _PROMPT_CONTEXT["composer"] = dict(composer)
    _PROMPT_CONTEXT["allow_override"] = bool(allow_override)
    _PROMPT_CONTEXT["registry_sha"] = str(registry_sha or "")


def _prompt_meta_for(stage_kind: str) -> Optional[Dict[str, Any]]:
    meta = dict(_PROMPT_CONTEXT.get(stage_kind) or {})
    if not meta.get("has_overlay") and not _PROMPT_CONTEXT.get("allow_override"):
        return None
    if not meta:
        return None
    meta["allow_override"] = bool(_PROMPT_CONTEXT.get("allow_override"))
    return meta


def _merge_prompt_meta(request: Mapping[str, Any], stage_kind: str) -> Dict[str, Any]:
    meta = _prompt_meta_for(stage_kind)
    merged = dict(request)
    if meta is not None:
        merged["_prompt_meta"] = meta
    return merged


def _effective_slot(args: argparse.Namespace, client) -> str:
    configured = str(getattr(args, "prompt_slot", "") or "").strip()
    if configured:
        return configured
    return "production" if _is_cloud_function(client) else "local"


def _resolve_prompt_versions(args: argparse.Namespace, spec, candidate_module, client) -> Dict[str, Any]:
    """Resolve per-stage prompt versions and apply overlays to ``spec``.

    Fails fast (before any API spend) on an unknown explicit version id.
    """
    registry_arg = str(getattr(args, "prompt_registry", "") or "").strip()
    registry_file = Path(registry_arg) if registry_arg else None
    registry = prompt_registry.load_registry(registry_file)
    registry_sha = str(registry.get("registry_sha") or "")

    slot = _effective_slot(args, client)
    stage_override = str(getattr(args, "prompt_stage", "both") or "both")
    explicit = str(getattr(args, "prompt_version", "") or "").strip()

    selection: Dict[str, Any] = {}
    explicit_version = prompt_registry.get_version(explicit, registry_file) if explicit else None
    if explicit and explicit_version is None:
        known = ", ".join(v["id"] for v in prompt_registry.list_versions(None, registry_file)) or "(none)"
        raise SystemExit(
            f"Unknown prompt version {explicit!r} (registry {_display_registry_path(registry_file)}). "
            f"Known versions: {known}"
        )
    for stage_kind in ("chapter", "composer"):
        requested = ""
        if explicit:
            if stage_override == "both":
                # A version id belongs to one stage; apply it to that stage and
                # leave the other stage on its slot default.
                requested = explicit if explicit_version.get("stage") == stage_kind else ""
            elif stage_override == stage_kind:
                requested = explicit
        try:
            version_id = prompt_registry.resolve_version_id(
                stage=stage_kind, prompt_version=requested or None, slot=slot, path=registry_file
            )
        except prompt_registry.UnknownVersionError as exc:
            allowed = ", ".join(exc.allowed) or "(none)"
            raise SystemExit(
                f"Unknown prompt version {requested!r} for stage {stage_kind!r} "
                f"(registry {_display_registry_path(registry_file)}). Known versions: {allowed}"
            )
        stage_obj = getattr(spec, f"{stage_kind}_stage", None)
        components = prompt_registry.apply_overlay(
            spec, stage_kind=stage_kind, prompt_version=version_id, path=registry_file
        )
        version = prompt_registry.get_version(version_id, registry_file) if version_id else None
        has_overlay = bool(version and (version.get("components") or {}))
        render = getattr(candidate_module, f"render_{stage_kind}_system")
        system_text = render(spec)
        selection[stage_kind] = {
            "version": version_id or "",
            "slot": slot,
            "has_overlay": has_overlay,
            "fingerprint": prompt_registry.fingerprint(system_text),
            "components": components,
        }
    selection["registry_sha"] = registry_sha
    selection["slot"] = slot
    return selection


def _display_registry_path(registry_file: Optional[Path]) -> str:
    path = prompt_registry.registry_path(registry_file)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def render_composer_repair_user(
    candidate_module,
    spec,
    *,
    chapter_summaries_md: str,
    current_summary_md: str,
    target_words: int,
    direction: str,
    book_title: str,
    toc_md: str,
    book_metadata: str,
    retrieved_source_excerpts: str,
    current_word_count: Optional[int] = None,
) -> str:
    repair_more_policies = getattr(candidate_module, "REPAIR_MORE_POLICIES", {})
    repair_less_policies = getattr(candidate_module, "REPAIR_LESS_POLICIES", {})
    visible_word_range = getattr(candidate_module, "visible_word_range")
    low, high = visible_word_range(target_words, spec.length_control.tolerance_pct)
    if direction == "more":
        policy = repair_more_policies.get(spec.length_control.repair_more_prompt_id, "Expand missing detail.")
    else:
        policy = repair_less_policies.get(spec.length_control.repair_less_prompt_id, "Shorten by removing repetition.")
    format_instructions = getattr(candidate_module, "FORMAT_INSTRUCTIONS", {})[spec.composer_stage.format_mode]
    numeric_delta = ""
    if current_word_count is not None:
        if direction == "less":
            numeric_delta = (
                f"Current summary is {current_word_count} visible words; target is {target_words} "
                f"(range {low}-{high}). Cut approximately {max(0, current_word_count - high)} words so "
                f"the final markdown lands inside [{low}, {high}]. Count the visible words of your output "
                "before finishing and adjust if needed."
            )
        else:
            numeric_delta = (
                f"Current summary is {current_word_count} visible words; target is {target_words} "
                f"(range {low}-{high}). Add approximately {max(0, low - current_word_count)} words so "
                f"the final markdown lands inside [{low}, {high}]. Count the visible words of your output "
                "before finishing and adjust if needed."
            )
        if (spec.composer_stage.prompt_components or {}).get("length_policy"):
            target_sentences = max(1, int(round(max(1, target_words) / 18)))
            numeric_delta += (
                f" The sentence budget is {target_sentences}; adjust sentence count as well as word count."
            )
    blocks = [
        "Revise the whole-book summary to hit the target length more accurately.",
        f"Book title: {book_title}" if book_title else "",
        f"Target visible words: {target_words}. Acceptable range: {low}-{high}. This repair direction is: {direction}.",
        numeric_delta if numeric_delta else "",
        policy,
        format_instructions,
        "Keep the result faithful to the chapter summaries and any retrieved source excerpts.",
        "Current whole-book summary:\n" + current_summary_md.strip(),
        f"Table of contents:\n{toc_md.strip()}" if toc_md.strip() else "",
        f"Book metadata:\n{book_metadata.strip()}" if book_metadata.strip() else "",
        "Chapter summaries:\n" + chapter_summaries_md.strip(),
    ]
    if retrieved_source_excerpts.strip():
        blocks.append("Retrieved source excerpts:\n" + retrieved_source_excerpts.strip())
    return "\n\n".join(block for block in blocks if block)


def extractive_mock_summary(markdown_text: str, target_words: int) -> str:
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    headings: List[str] = []
    paragraphs: List[str] = []
    current: List[str] = []
    for line in lines:
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current).strip())
                current = []
            continue
        if re.match(r"^\s{0,3}#{1,6}\s+", line):
            if current:
                paragraphs.append(" ".join(current).strip())
                current = []
            headings.append(line.strip())
            continue
        current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current).strip())

    chunks: List[str] = []
    for heading in headings[:8]:
        chunks.append(heading)
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if sentence:
                chunks.append(sentence)

    out: List[str] = []
    for chunk in chunks:
        candidate = "\n\n".join(out + [chunk]).strip()
        if visible_word_count(candidate) > target_words and out:
            break
        out.append(chunk)
        if visible_word_count("\n\n".join(out)) >= target_words:
            break
    summary = "\n\n".join(out).strip()
    if not summary:
        words = markdown_text.split()
        summary = " ".join(words[: max(1, target_words)])
    return summary


def invoke_generation(
    client: Optional[OpenRouterClient],
    request_body: Mapping[str, Any],
    *,
    mock_source_md: str,
    target_words: int,
    current_summary_md: str = "",
) -> GenerationResult:
    if client is None:
        source = current_summary_md if current_summary_md and visible_word_count(current_summary_md) > target_words else mock_source_md
        if not source.strip():
            source = "placeholder content"
        summary_md = extractive_mock_summary(source, target_words=max(1, target_words))
        usage = UsageRecord()
        payload = json.dumps({"summary_md": summary_md, "estimated_visible_words": visible_word_count(summary_md)})
        return GenerationResult(
            summary_md=summary_md,
            estimated_visible_words=visible_word_count(summary_md),
            raw_content=payload,
            usage=usage,
            raw_response={"mock": True},
        )
    prompt_meta = request_body.get("_prompt_meta") if isinstance(request_body, Mapping) else None
    clean_request = {k: v for k, v in request_body.items() if k != "_prompt_meta"} if isinstance(request_body, Mapping) else request_body
    if _is_cloud_function(client):
        use_json = bool(clean_request.get("response_format")) if isinstance(clean_request, dict) else True
        with cf_concurrency_slot():
            return client.chat_completion(
                clean_request,
                source_md=mock_source_md,
                score=False,
                target_words=0,
                use_json_schema=use_json,
                prompt_meta=prompt_meta,
            )
    try:
        return client.chat_completion(clean_request)
    except (OpenRouterAPIError, OpenRouterHTTPError) as e:
        if isinstance(e, OpenRouterHTTPError):
            model_in_request = clean_request.get("model", "unknown") if isinstance(clean_request, dict) else "unknown"
            print(f"OpenRouter HTTP error: status_code={e.status_code}, path={e.path}, model={model_in_request}")
            if e.error_payload:
                error_info = e.error_payload.get('error', {})
                print(f"  error_payload.message: {error_info.get('message', 'unknown')}")
                print(f"  error_payload.type: {error_info.get('type', 'unknown')}")
                print(f"  error_payload.code: {error_info.get('code', 'unknown')}")
                print(f"  error_payload keys: {list(error_info.keys()) if isinstance(error_info, dict) else 'not a dict'}")
                print(f"  full error_payload: {e.error_payload}")
            else:
                print(f"  error_payload: None")
            print(f"  response_text (first 1000 chars): {e.response_text[:1000] if e.response_text else 'empty'}")
        else:
            print(f"OpenRouter API error: {e}")
        raise


def run_length_controlled_stage(
    *,
    candidate_module,
    spec,
    stage_kind: str,
    stage_config,
    system_prompt: str,
    initial_user_prompt: str,
    target_words: int,
    mock_source_md: str,
    client: Optional[OpenRouterClient],
    chapter_summaries_md: str = "",
    current_book_title: str = "",
    current_chapter_title: str = "",
    toc_md: str = "",
    book_metadata: str = "",
    retrieved_source_excerpts: str = "",
    resume_state: Optional[Mapping[str, Any]] = None,
    checkpoint_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> StageRun:
    build_openrouter_request = getattr(candidate_module, "build_openrouter_request")
    low, high = getattr(candidate_module, "visible_word_range")(target_words, spec.length_control.tolerance_pct)

    restored = dict(resume_state or {})
    responses: List[Mapping[str, Any]] = list(restored.get("raw_responses") or [])
    total_cost = float(restored.get("generation_cost") or 0.0)
    total_uncached_cost = float(restored.get("uncached_generation_cost") or 0.0)
    passes_used = int(restored.get("passes_used") or 0)
    summary_md = str(restored.get("summary_md") or "").strip()
    first_pass_summary_md = str(restored.get("first_pass_summary_md") or "").strip()
    total_truncation_retries = int(restored.get("truncation_retries") or 0)
    pass_latencies: List[float] = [float(v) for v in (restored.get("pass_latency_seconds") or [])]

    # Best-pass selection: among all passes executed (including passes restored
    # from a checkpoint), track the one whose visible word count is closest to
    # target_words. Returned summary_md is this pass, not the last one.
    best_summary_md = str(restored.get("best_summary_md") or "").strip()
    if not best_summary_md and summary_md:
        # Old-format checkpoint (no best pass recorded): seed from latest pass.
        best_summary_md = summary_md
    if best_summary_md:
        best_distance = abs(visible_word_count(best_summary_md) - target_words)
    else:
        best_distance = None

    def update_best(candidate_md: str) -> None:
        nonlocal best_summary_md, best_distance
        if not best_summary_md:
            best_summary_md = candidate_md
            best_distance = abs(visible_word_count(candidate_md) - target_words)
            return
        distance = abs(visible_word_count(candidate_md) - target_words)
        # Strict < keeps the earliest pass on ties.
        assert best_distance is not None
        if distance < best_distance:
            best_summary_md = candidate_md
            best_distance = distance

    def emit_checkpoint() -> None:
        if checkpoint_callback is None:
            return
        checkpoint_callback(
            {
                "summary_md": summary_md,
                "best_summary_md": best_summary_md,
                "best_distance": best_distance,
                "first_pass_summary_md": first_pass_summary_md or summary_md,
                "passes_used": passes_used,
                "generation_cost": total_cost,
                "uncached_generation_cost": total_uncached_cost,
                "truncation_retries": total_truncation_retries,
                "raw_responses": _json_safe(list(responses)),
                "pass_latency_seconds": [float(v) for v in pass_latencies],
            }
        )

    max_truncation_retries = int(getattr(spec.length_control, "max_truncation_retries", 1) or 0)

    def generate_pass(user_prompt: str, *, current_for_mock: str, pass_target: int) -> GenerationResult:
        nonlocal total_cost, total_uncached_cost, total_truncation_retries
        use_json_schema = stage_config.use_json_schema if stage_config.use_json_schema is not None else spec.use_json_schema
        attempt_target = max(1, pass_target)
        retry_count = 0
        retry_prompt = user_prompt
        while True:
            # Target-aware generation-time cap: derive max_tokens from the
            # visible-word target (plus reasoning reservation) so the model cannot
            # free-run to multiples of the target. Re-derived per truncation retry
            # from the lowered pass_target, so retries automatically tighten.
            effective_max_tokens = visible_max_tokens(
                attempt_target,
                effort=stage_reasoning_effort(stage_config),
                cap=int(stage_config.max_tokens or DEFAULT_MAX_TOKEN_CAP),
            )
            # Reconciliation with the pipeline brief's truncation floor: a short
            # sentence budget must not starve the JSON payload. Never exceeds the
            # profile budget.
            t_max_floor = max(1, int(round(attempt_target * 1.35 * 1.15)))
            effective_max_tokens = min(
                int(stage_config.max_tokens or DEFAULT_MAX_TOKEN_CAP),
                max(effective_max_tokens, t_max_floor),
            )
            request = build_openrouter_request(
                stage=stage_config,
                system_prompt=system_prompt,
                user_prompt=retry_prompt,
                schema_name=spec.json_schema_name,
                use_json_schema=use_json_schema,
                max_tokens=effective_max_tokens,
            )
            request = _merge_prompt_meta(request, stage_kind)
            pass_started = time.perf_counter()
            result = invoke_generation(
                client,
                request,
                mock_source_md=mock_source_md,
                target_words=attempt_target,
                current_summary_md=current_for_mock,
            )
            pass_latencies.append(time.perf_counter() - pass_started)
            _LATENCY_ACCUMULATOR.append(pass_latencies[-1])
            responses.append(_json_safe(dict(result.raw_response)))
            total_cost += result.usage.generation_cost
            total_uncached_cost += result.usage.uncached_generation_cost or result.usage.generation_cost
            if not result.truncated or retry_count >= max_truncation_retries:
                return result
            retry_count += 1
            total_truncation_retries += 1
            attempt_target = max(1, int(round(pass_target * (0.7 ** retry_count))))
            responses.append(
                {
                    "kind": "truncation_retry",
                    "retry_index": retry_count,
                    "pass_target_words": attempt_target,
                    "finish_reason": result.finish_reason,
                    "json_recovery": result.json_recovery,
                }
            )
            retry_prompt = (
                user_prompt
                + f"\n\nIMPORTANT: The previous attempt was cut off because it exceeded the output length limit "
                f"(finish_reason={result.finish_reason}). Rewrite a COMPLETE summary of at most {attempt_target} "
                "visible words so the JSON payload is not truncated. Do not leave the summary or the payload unfinished."
            )

    if passes_used <= 0 or not summary_md:
        result = generate_pass(initial_user_prompt, current_for_mock="", pass_target=target_words)
        passes_used = 1
        summary_md = result.summary_md.strip()
        first_pass_summary_md = summary_md
        update_best(summary_md)
        emit_checkpoint()

    while passes_used < spec.length_control.max_passes:
        words = visible_word_count(summary_md)
        if low <= words <= high:
            break
        direction = "more" if words < low else "less"
        if spec.length_control.repair_strategy == "regenerate_from_source":
            repair_user_prompt = initial_user_prompt
            current_for_mock = ""
        else:
            if stage_kind == "chapter":
                repair_user_prompt = candidate_module.render_repair_user(
                    spec,
                    source_md=mock_source_md,
                    current_summary_md=summary_md,
                    target_words=target_words,
                    direction=direction,
                    book_title=current_book_title,
                    chapter_title=current_chapter_title,
                    current_word_count=words,
                )
            else:
                repair_user_prompt = render_composer_repair_user(
                    candidate_module,
                    spec,
                    chapter_summaries_md=chapter_summaries_md,
                    current_summary_md=summary_md,
                    target_words=target_words,
                    direction=direction,
                    book_title=current_book_title,
                    toc_md=toc_md,
                    book_metadata=book_metadata,
                    retrieved_source_excerpts=retrieved_source_excerpts,
                    current_word_count=words,
                )
            current_for_mock = summary_md
        result = generate_pass(repair_user_prompt, current_for_mock=current_for_mock, pass_target=target_words)
        passes_used += 1
        summary_md = result.summary_md.strip()
        if not first_pass_summary_md:
            first_pass_summary_md = summary_md
        update_best(summary_md)
        emit_checkpoint()

    return StageRun(
        summary_md=best_summary_md,
        first_pass_summary_md=first_pass_summary_md or summary_md,
        passes_used=passes_used,
        generation_cost=total_cost,
        uncached_generation_cost=total_uncached_cost,
        raw_responses=tuple(responses),
        pass_latency_seconds=tuple(pass_latencies),
    )


def build_retrieved_source_excerpts(book: BookContext, chapter_targets: Sequence[int], max_total_words: int = 1800) -> str:
    if not chapter_targets:
        return ""
    total = sum(max(1, value) for value in chapter_targets)
    blocks: List[str] = []
    for chapter, target in zip(book.chapters, chapter_targets):
        per_chapter_target = max(80, int(round(max_total_words * (max(1, target) / total))))
        excerpt = extractive_mock_summary(chapter.source_md, per_chapter_target)
        blocks.append(f"## {chapter.chapter_title}\n\n{excerpt}")
    return "\n\n".join(blocks).strip()


def chapter_target_map(candidate_module, spec, book: BookContext) -> Dict[str, int]:
    targets = candidate_module.allocate_chapter_targets(
        [chapter.visible_words for chapter in book.chapters],
        book.total_visible_words,
        spec.profile,
        spec.budget_allocator,
    )
    return {chapter.chapter_id: target for chapter, target in zip(book.chapters, targets)}


def judge_if_requested(
    client: Optional[OpenRouterClient],
    judge_model: str,
    *,
    summary_md: str,
    rubric: Rubric,
    source_md: str,
    source_char_limit: int,
) -> Optional[AbsoluteJudgeResult]:
    if client is None or not judge_model:
        return None
    if _is_cloud_function(client):
        from core.cf_client import parse_cf_judge_scores

        with cf_concurrency_slot():
            result = client.chat_completion(
                {"model": judge_model},
                source_md=source_md[:source_char_limit],
                summary_md=summary_md,
                judge=True,
                judge_model=judge_model,
                rubric=rubric,
                score=False,
                thinking=False,
                use_json_schema=True,
                judge_source_char_limit=source_char_limit or 32000,
            )
        error = str((result.raw_response or {}).get("judge_error") or "").strip()
        if error:
            print(f"Judge error: {error}")
            return None
        parsed = parse_cf_judge_scores(result.raw_response)
        if parsed is None:
            return None
        scores, rationale = parsed
        return AbsoluteJudgeResult(
            scores=scores,
            rationale=rationale,
            raw_response=result.raw_response,
        )
    return judge_summary_absolute(
        client,
        judge_model=judge_model,
        summary_md=summary_md,
        rubric=rubric,
        source_md=source_md[:source_char_limit],
    )


def run_chapter_sample(
    item: Mapping[str, Any],
    *,
    candidate_module,
    spec,
    client: Optional[OpenRouterClient],
    data_dir: Path,
    judge_model: str,
    judge_source_char_limit: int,
    resume_progress: Optional[Mapping[str, Any]] = None,
    progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
    run_dir: Optional[Path] = None,
) -> Tuple[SummarySample, Dict[str, Any]]:
    book_id = str(item["book_id"])
    chapter_id = str(item["chapter_id"])
    sample_id = str(item.get("sample_id", f"{book_id}:{chapter_id}"))
    book = load_book_context(book_id, data_dir)
    target_map = chapter_target_map(candidate_module, spec, book)
    chapter = next((ch for ch in book.chapters if ch.chapter_id == chapter_id), None)
    if chapter is None:
        raise KeyError(f"Chapter {chapter_id} not found in book manifest for {book_id}")
    target_words = target_map[chapter_id]

    base_progress: Dict[str, Any] = {
        "kind": "chapter",
        "phase": "stage",
        "item_key": sample_id,
        "item": _json_safe(dict(item)),
        "book_id": book_id,
        "chapter_id": chapter_id,
        "sample_id": sample_id,
        "target_words": target_words,
    }
    progress = dict(resume_progress or {})
    for key, value in base_progress.items():
        progress.setdefault(key, value)

    def emit() -> None:
        if progress_callback is not None:
            progress_callback(_json_safe(progress))

    if str(progress.get("phase") or "") == "completed" and isinstance(progress.get("sample_record"), Mapping):
        sample, trace, _ = deserialize_sample_record(progress["sample_record"], data_dir)
        return sample, trace

    system_prompt = candidate_module.render_chapter_system(spec)
    user_prompt = candidate_module.render_chapter_user(
        spec,
        source_md=chapter.source_md,
        target_words=target_words,
        book_title=book.book_title,
        chapter_title=chapter.chapter_title,
        toc_md=book.toc_md,
        book_metadata=book.metadata_md,
    )

    stage_run_payload = progress.get("stage_run") if isinstance(progress.get("stage_run"), Mapping) else None
    stage_run = deserialize_stage_run(stage_run_payload) if stage_run_payload else None
    if stage_run is None:
        resume_stage_state = progress.get("stage_state") if isinstance(progress.get("stage_state"), Mapping) else None

        def stage_checkpoint(stage_state: Mapping[str, Any]) -> None:
            progress.clear()
            progress.update(base_progress)
            progress["phase"] = "stage"
            progress["stage_state"] = _json_safe(stage_state)
            emit()

        stage_run = run_length_controlled_stage(
            candidate_module=candidate_module,
            spec=spec,
            stage_kind="chapter",
            stage_config=spec.chapter_stage,
            system_prompt=system_prompt,
            initial_user_prompt=user_prompt,
            target_words=target_words,
            mock_source_md=chapter.source_md,
            client=client,
            current_book_title=book.book_title,
            current_chapter_title=chapter.chapter_title,
            resume_state=resume_stage_state,
            checkpoint_callback=stage_checkpoint,
        )
        progress.clear()
        progress.update(base_progress)
        progress["phase"] = "judge"
        progress["stage_run"] = serialize_stage_run(stage_run)
        emit()

    judge_result = judge_if_requested(
        client,
        judge_model,
        summary_md=stage_run.summary_md,
        rubric=chapter.rubric,
        source_md=chapter.source_md,
        source_char_limit=judge_source_char_limit,
    )
    sample = SummarySample(
        sample_id=sample_id,
        level="chapter",
        target_words=target_words,
        summary_md=stage_run.summary_md,
        source_md=chapter.source_md,
        group_id=book_id,
        first_pass_summary_md=stage_run.first_pass_summary_md,
        passes_used=stage_run.passes_used,
        generation_cost=stage_run.generation_cost,
        uncached_generation_cost=stage_run.uncached_generation_cost,
        malformed=not bool(stage_run.summary_md.strip()),
        rubric=chapter.rubric,
        judge_scores=judge_result.scores if judge_result else None,
    )
    trace = {
        "sample_id": sample.sample_id,
        "book_id": book_id,
        "chapter_id": chapter_id,
        "target_words": target_words,
        "output_words": visible_word_count(stage_run.summary_md),
        "passes_used": stage_run.passes_used,
        "generation_cost": stage_run.generation_cost,
        "uncached_generation_cost": stage_run.uncached_generation_cost,
        "judge_rationale": judge_result.rationale if judge_result else "",
    }
    trace.update(taxonomy_trace_payload(book.taxonomy))
    if spec.disable_composer and judge_result and judge_result.scores and run_dir:
        chapter_runs_dir = run_dir / "chapter_runs"
        chapter_runs_dir.mkdir(parents=True, exist_ok=True)
        chapter_csv_path = chapter_runs_dir / f"{sample_id}.csv"
        rm = readability_metrics(stage_run.summary_md) if stage_run.summary_md else None
        fieldnames = [
            "sample_id", "chapter_id", "chapter_title", "target_words", "output_words",
            "passes_used", "generation_cost", "uncached_generation_cost",
            "judge_faithfulness", "judge_concept_coverage", "judge_qualifier_preservation",
            "judge_no_fluff", "judge_structure_quality",
            "flesch_reading_ease", "flesch_kincaid_grade", "sentence_count",
        ]
        row = {
            "sample_id": sample_id,
            "chapter_id": chapter_id,
            "chapter_title": chapter.chapter_title,
            "target_words": target_words,
            "output_words": visible_word_count(stage_run.summary_md),
            "passes_used": stage_run.passes_used,
            "generation_cost": stage_run.generation_cost,
            "uncached_generation_cost": stage_run.uncached_generation_cost,
            "judge_faithfulness": judge_result.scores.faithfulness,
            "judge_concept_coverage": judge_result.scores.concept_coverage,
            "judge_qualifier_preservation": judge_result.scores.qualifier_preservation,
            "judge_no_fluff": judge_result.scores.no_fluff,
            "judge_structure_quality": judge_result.scores.structure_quality,
        }
        if rm:
            row["flesch_reading_ease"] = rm.flesch_reading_ease
            row["flesch_kincaid_grade"] = rm.flesch_kincaid_grade
            row["sentence_count"] = rm.sentence_count
        if chapter_csv_path.exists():
            existing_rows: List[Dict[str, Any]] = []
            with open(chapter_csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    existing_rows.append(r)
            existing_keys = {(r.get("sample_id", ""), r.get("chapter_id", "")) for r in existing_rows}
            if (sample_id, chapter_id) in existing_keys:
                chapter_csv_path = chapter_runs_dir / f"{sample_id}_{utc_now_ts()}.csv"
        with open(chapter_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)
        trace["chapter_runs_csv"] = str(chapter_csv_path)
    progress.clear()
    progress.update(base_progress)
    progress["phase"] = "completed"
    progress["stage_run"] = serialize_stage_run(stage_run)
    progress["sample_record"] = build_sample_record(sample, trace, item_key=sample_id)
    emit()
    return sample, trace


def render_chapter_summaries_for_composer(chapter_outputs: Sequence[Tuple[ChapterContext, StageRun]]) -> str:
    blocks = []
    for chapter, stage_run in chapter_outputs:
        blocks.append(f"## {chapter.chapter_title}\n\n{stage_run.summary_md.strip()}")
    return "\n\n".join(blocks).strip()


def join_book_source(book: BookContext) -> str:
    return "\n\n".join(f"# {chapter.chapter_title}\n\n{chapter.source_md.strip()}" for chapter in book.chapters).strip()


def _run_one_chapter(
    *,
    candidate_module,
    spec,
    client: Optional[OpenRouterClient],
    book: BookContext,
    chapter: ChapterContext,
    target_words: int,
    chapter_system_prompt: str,
    resume_stage_state: Optional[Mapping[str, Any]],
    checkpoint_callback: Callable[[Mapping[str, Any]], None],
    judge_model: str,
    judge_source_char_limit: int,
) -> Tuple[StageRun, Optional[JudgeScores], Dict[str, Any]]:
    """Run a single chapter's stage (plus optional judge) and build its trace row.

    Safe to call from multiple worker threads: all mutable book-level state lives
    in ``run_book_sample``; this function only mutates local state.
    """
    chapter_user_prompt = candidate_module.render_chapter_user(
        spec,
        source_md=chapter.source_md,
        target_words=target_words,
        book_title=book.book_title,
        chapter_title=chapter.chapter_title,
        toc_md=book.toc_md,
        book_metadata=book.metadata_md,
    )
    chapter_run = run_length_controlled_stage(
        candidate_module=candidate_module,
        spec=spec,
        stage_kind="chapter",
        stage_config=spec.chapter_stage,
        system_prompt=chapter_system_prompt,
        initial_user_prompt=chapter_user_prompt,
        target_words=target_words,
        mock_source_md=chapter.source_md,
        client=client,
        current_book_title=book.book_title,
        current_chapter_title=chapter.chapter_title,
        resume_state=resume_stage_state,
        checkpoint_callback=checkpoint_callback,
    )
    chapter_judge_scores: Optional[JudgeScores] = None
    if spec.disable_composer and client is not None and judge_model:
        chapter_judge_result = judge_if_requested(
            client,
            judge_model,
            summary_md=chapter_run.summary_md,
            rubric=chapter.rubric,
            source_md=chapter.source_md,
            source_char_limit=judge_source_char_limit,
        )
        if chapter_judge_result is not None and chapter_judge_result.scores is not None:
            chapter_judge_scores = chapter_judge_result.scores

    chapter_row: Dict[str, Any] = {
        "chapter_id": chapter.chapter_id,
        "chapter_title": chapter.chapter_title,
        "target_words": target_words,
        "output_words": visible_word_count(chapter_run.summary_md),
        "passes_used": chapter_run.passes_used,
        "generation_cost": chapter_run.generation_cost,
        "uncached_generation_cost": chapter_run.uncached_generation_cost,
        "judge_faithfulness": chapter_judge_scores.faithfulness if chapter_judge_scores else None,
        "judge_concept_coverage": chapter_judge_scores.concept_coverage if chapter_judge_scores else None,
        "judge_qualifier_preservation": chapter_judge_scores.qualifier_preservation if chapter_judge_scores else None,
        "judge_no_fluff": chapter_judge_scores.no_fluff if chapter_judge_scores else None,
        "judge_structure_quality": chapter_judge_scores.structure_quality if chapter_judge_scores else None,
    }
    if chapter_run.summary_md:
        rm = readability_metrics(chapter_run.summary_md)
        chapter_row["flesch_reading_ease"] = rm.flesch_reading_ease
        chapter_row["flesch_kincaid_grade"] = rm.flesch_kincaid_grade
        chapter_row["sentence_count"] = rm.sentence_count
    return chapter_run, chapter_judge_scores, chapter_row


def run_book_sample(
    item: Mapping[str, Any],
    *,
    candidate_module,
    spec,
    client: Optional[OpenRouterClient],
    data_dir: Path,
    judge_model: str,
    judge_source_char_limit: int,
    resume_progress: Optional[Mapping[str, Any]] = None,
    progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
    run_dir: Optional[Path] = None,
    chapter_concurrency: int = 1,
) -> Tuple[SummarySample, Dict[str, Any]]:
    book_id = str(item["book_id"])
    sample_id = str(item.get("sample_id", book_id))
    book = load_book_context(book_id, data_dir)

    base_progress: Dict[str, Any] = {
        "kind": "book",
        "phase": "chapters",
        "item_key": sample_id,
        "item": _json_safe(dict(item)),
        "book_id": book_id,
        "sample_id": sample_id,
    }
    progress = dict(resume_progress or {})
    for key, value in base_progress.items():
        progress.setdefault(key, value)

    def emit() -> None:
        if progress_callback is not None:
            progress_callback(_json_safe(progress))

    if str(progress.get("phase") or "") == "completed" and isinstance(progress.get("sample_record"), Mapping):
        sample, trace, _ = deserialize_sample_record(progress["sample_record"], data_dir)
        return sample, trace

    chapter_targets = [int(value) for value in (progress.get("chapter_targets") or [])]
    if len(chapter_targets) != len(book.chapters):
        chapter_targets = candidate_module.allocate_chapter_targets(
            [chapter.visible_words for chapter in book.chapters],
            book.total_visible_words,
            spec.profile,
            spec.budget_allocator,
        )

    completed_entries = progress.get("chapter_outputs") if isinstance(progress.get("chapter_outputs"), list) else []
    completed_map: Dict[str, StageRun] = {}
    for entry in completed_entries:
        if not isinstance(entry, Mapping):
            continue
        chapter_key = str(entry.get("chapter_id") or "")
        stage_payload = entry.get("stage_run")
        if chapter_key and isinstance(stage_payload, Mapping):
            completed_map[chapter_key] = deserialize_stage_run(stage_payload)

    chapter_judge_scores: Dict[str, JudgeScores] = {}
    chapter_judge_payload = progress.get("chapter_judge_scores")
    if isinstance(chapter_judge_payload, Mapping):
        for ch_id, scores_payload in chapter_judge_payload.items():
            scores = judge_scores_from_dict(scores_payload)
            if scores is not None:
                chapter_judge_scores[str(ch_id)] = scores

    chapter_runs_rows: List[Dict[str, Any]] = []
    saved_rows = progress.get("chapter_runs_rows")
    if isinstance(saved_rows, list):
        for row in saved_rows:
            if isinstance(row, Mapping):
                chapter_runs_rows.append(dict(row))

    chapter_outputs: List[Tuple[ChapterContext, StageRun]] = []
    chapter_passes: List[int] = []
    chapter_errors: Dict[str, Dict[str, Any]] = {}
    for chapter in book.chapters:
        completed_stage = completed_map.get(chapter.chapter_id)
        if completed_stage is not None:
            chapter_outputs.append((chapter, completed_stage))
            chapter_passes.append(completed_stage.passes_used)
    completed_generation_cost = sum(stage.generation_cost for stage in completed_map.values())
    completed_uncached_cost = sum(stage.uncached_generation_cost for stage in completed_map.values())
    if "total_generation_cost" in progress:
        total_generation_cost = float(progress.get("total_generation_cost") or 0.0)
    else:
        total_generation_cost = completed_generation_cost
    if "total_uncached_cost" in progress:
        total_uncached_cost = float(progress.get("total_uncached_cost") or 0.0)
    else:
        total_uncached_cost = completed_uncached_cost

    chapter_system_prompt = candidate_module.render_chapter_system(spec)
    current_stage = progress.get("current_stage") if isinstance(progress.get("current_stage"), Mapping) else {}
    chapter_stage_states = progress.get("chapter_stage_states")
    if not isinstance(chapter_stage_states, Mapping):
        chapter_stage_states = {}

    pending = [
        (chapter, target_words)
        for chapter, target_words in zip(book.chapters, chapter_targets)
        if chapter.chapter_id not in completed_map
    ]

    def resume_state_for(chapter_id: str) -> Optional[Mapping[str, Any]]:
        stored = chapter_stage_states.get(chapter_id)
        if isinstance(stored, Mapping):
            return stored
        if (
            str(current_stage.get("stage_kind") or "") == "chapter"
            and str(current_stage.get("chapter_id") or "") == chapter_id
            and isinstance(current_stage.get("stage_state"), Mapping)
        ):
            return current_stage.get("stage_state")
        return None

    progress_lock = threading.Lock()

    def _snapshot_progress(*, current_stage_payload: Optional[Mapping[str, Any]]) -> None:
        progress.clear()
        progress.update(base_progress)
        progress["phase"] = "chapters"
        progress["chapter_targets"] = list(chapter_targets)
        progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
        progress["chapter_passes"] = list(chapter_passes)
        progress["total_generation_cost"] = total_generation_cost
        progress["total_uncached_cost"] = total_uncached_cost
        progress["chapter_runs_rows"] = list(chapter_runs_rows)
        if chapter_errors:
            progress["chapter_errors"] = dict(chapter_errors)
        if spec.disable_composer and chapter_judge_scores:
            progress["chapter_judge_scores"] = {
                ch_id: judge_scores_to_dict(scores) for ch_id, scores in chapter_judge_scores.items()
            }
        if current_stage_payload is not None:
            progress["chapter_stage_states"] = dict(chapter_stage_states)
            progress["current_stage"] = current_stage_payload
        else:
            progress["current_stage"] = None
        emit()

    def _make_checkpoint(chapter_id: str) -> Callable[[Mapping[str, Any]], None]:
        def chapter_checkpoint(stage_state: Mapping[str, Any]) -> None:
            with progress_lock:
                chapter_stage_states[chapter_id] = _json_safe(stage_state)
                _snapshot_progress(
                    current_stage_payload={
                        "stage_kind": "chapter",
                        "chapter_id": chapter_id,
                        "stage_state": _json_safe(stage_state),
                    }
                )

        return chapter_checkpoint

    def _emit_completed_snapshot() -> None:
        with progress_lock:
            _snapshot_progress(current_stage_payload=None)

    def _run_pending_chapter(chapter: ChapterContext, target_words: int) -> Tuple[StageRun, Optional[JudgeScores], Dict[str, Any]]:
        return _run_one_chapter(
            candidate_module=candidate_module,
            spec=spec,
            client=client,
            book=book,
            chapter=chapter,
            target_words=target_words,
            chapter_system_prompt=chapter_system_prompt,
            resume_stage_state=resume_state_for(chapter.chapter_id),
            checkpoint_callback=_make_checkpoint(chapter.chapter_id),
            judge_model=judge_model,
            judge_source_char_limit=judge_source_char_limit,
        )

    def _record_chapter_result(
        chapter: ChapterContext,
        target_words: int,
        chapter_run: StageRun,
        chapter_scores: Optional[JudgeScores],
        chapter_row: Dict[str, Any],
    ) -> None:
        nonlocal total_generation_cost, total_uncached_cost
        with progress_lock:
            chapter_outputs.append((chapter, chapter_run))
            chapter_passes.append(chapter_run.passes_used)
            total_generation_cost += chapter_run.generation_cost
            total_uncached_cost += chapter_run.uncached_generation_cost
            if chapter_scores is not None:
                chapter_judge_scores[chapter.chapter_id] = chapter_scores
            chapter_runs_rows.append(chapter_row)

    def _record_chapter_error(chapter: ChapterContext, exc: BaseException) -> None:
        error_payload = error_to_dict(exc)
        with progress_lock:
            chapter_errors[chapter.chapter_id] = error_payload
            _snapshot_progress(current_stage_payload=None)
        print(
            f"WARN: chapter {chapter.chapter_id} of {book_id} failed; continuing with remaining chapters. "
            f"{error_payload['type']}: {error_payload['message']}",
            file=sys.stderr,
        )

    if chapter_concurrency is not None and chapter_concurrency > 1 and pending and _is_cloud_function(client):
        workers = min(int(chapter_concurrency), len(pending))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_run_pending_chapter, chapter, target_words): (chapter, target_words)
                for chapter, target_words in pending
            }
            for future in as_completed(futures):
                chapter, target_words = futures[future]
                try:
                    chapter_run, chapter_scores, chapter_row = future.result()
                except (OpenRouterInsufficientCreditsError, ModelUnavailableError):
                    raise
                except Exception as exc:
                    _record_chapter_error(chapter, exc)
                    continue
                _record_chapter_result(chapter, target_words, chapter_run, chapter_scores, chapter_row)
                _emit_completed_snapshot()
        book_order = {chapter.chapter_id: index for index, chapter in enumerate(book.chapters)}
        chapter_outputs.sort(key=lambda pair: book_order[pair[0].chapter_id])
        chapter_passes = [run.passes_used for _, run in chapter_outputs]
        chapter_runs_rows.sort(key=lambda row: book_order.get(str(row.get("chapter_id")), 0))
        _emit_completed_snapshot()
    else:
        for chapter, target_words in zip(book.chapters, chapter_targets):
            if chapter.chapter_id in completed_map:
                chapter_run = completed_map[chapter.chapter_id]
                existing_row = next((r for r in chapter_runs_rows if r.get("chapter_id") == chapter.chapter_id), None)
                if existing_row is None:
                    chapter_row: Dict[str, Any] = {
                        "chapter_id": chapter.chapter_id,
                        "chapter_title": chapter.chapter_title,
                        "target_words": target_words,
                        "output_words": visible_word_count(chapter_run.summary_md),
                        "passes_used": chapter_run.passes_used,
                        "generation_cost": chapter_run.generation_cost,
                        "uncached_generation_cost": chapter_run.uncached_generation_cost,
                    }
                    if chapter_run.summary_md:
                        rm = readability_metrics(chapter_run.summary_md)
                        chapter_row["flesch_reading_ease"] = rm.flesch_reading_ease
                        chapter_row["flesch_kincaid_grade"] = rm.flesch_kincaid_grade
                        chapter_row["sentence_count"] = rm.sentence_count
                    chapter_runs_rows.append(chapter_row)
                continue
            try:
                chapter_run, chapter_scores, chapter_row = _run_pending_chapter(chapter, target_words)
            except (OpenRouterInsufficientCreditsError, ModelUnavailableError):
                raise
            except Exception as exc:
                _record_chapter_error(chapter, exc)
                continue
            _record_chapter_result(chapter, target_words, chapter_run, chapter_scores, chapter_row)
            _emit_completed_snapshot()

    if chapter_errors and not chapter_outputs:
        raise ChapterRunError(book_id, chapter_errors)

    chapter_summaries_md = render_chapter_summaries_for_composer(chapter_outputs)
    retrieved_source_excerpts = ""
    if spec.composer_mode in {"hybrid_retrieve", "source_aware"}:
        retrieved_source_excerpts = build_retrieved_source_excerpts(book, chapter_targets)
    final_target_words = int(
        progress.get("final_target_words")
        or candidate_module.final_book_target_words(
            book.total_visible_words,
            spec.profile,
            spec.budget_allocator,
        )
    )

    composer_run_payload = progress.get("composer_stage_run") if isinstance(progress.get("composer_stage_run"), Mapping) else None
    composer_run = deserialize_stage_run(composer_run_payload) if composer_run_payload else None
    current_stage = progress.get("current_stage") if isinstance(progress.get("current_stage"), Mapping) else {}
    if composer_run is None and not spec.disable_composer:
        composer_system_prompt = candidate_module.render_composer_system(spec)
        composer_user_prompt = candidate_module.render_composer_user(
            spec,
            chapter_summaries_md=chapter_summaries_md,
            target_words=final_target_words,
            book_title=book.book_title,
            toc_md=book.toc_md,
            book_metadata=book.metadata_md,
            retrieved_source_excerpts=retrieved_source_excerpts,
        )
        composer_source = (
            chapter_summaries_md
            if spec.composer_mode == "summaries_only"
            else chapter_summaries_md + "\n\n" + retrieved_source_excerpts
        )
        resume_stage_state = None
        if str(current_stage.get("stage_kind") or "") == "composer" and isinstance(current_stage.get("stage_state"), Mapping):
            resume_stage_state = current_stage.get("stage_state")

        def composer_checkpoint(stage_state: Mapping[str, Any]) -> None:
            progress.clear()
            progress.update(base_progress)
            progress["phase"] = "composer"
            progress["chapter_targets"] = list(chapter_targets)
            progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
            progress["chapter_passes"] = list(chapter_passes)
            progress["final_target_words"] = final_target_words
            progress["total_generation_cost"] = total_generation_cost
            progress["total_uncached_cost"] = total_uncached_cost
            if chapter_errors:
                progress["chapter_errors"] = dict(chapter_errors)
            progress["current_stage"] = {
                "stage_kind": "composer",
                "stage_state": _json_safe(stage_state),
            }
            emit()

        composer_run = run_length_controlled_stage(
            candidate_module=candidate_module,
            spec=spec,
            stage_kind="composer",
            stage_config=spec.composer_stage,
            system_prompt=composer_system_prompt,
            initial_user_prompt=composer_user_prompt,
            target_words=final_target_words,
            mock_source_md=composer_source,
            client=client,
            chapter_summaries_md=chapter_summaries_md,
            current_book_title=book.book_title,
            toc_md=book.toc_md,
            book_metadata=book.metadata_md,
            retrieved_source_excerpts=retrieved_source_excerpts,
            resume_state=resume_stage_state,
            checkpoint_callback=composer_checkpoint,
        )
        total_generation_cost += composer_run.generation_cost
        total_uncached_cost += composer_run.uncached_generation_cost
        progress.clear()
        progress.update(base_progress)
        progress["phase"] = "judge"
        progress["chapter_targets"] = list(chapter_targets)
        progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
        progress["chapter_passes"] = list(chapter_passes)
        progress["final_target_words"] = final_target_words
        progress["composer_stage_run"] = serialize_stage_run(composer_run)
        progress["total_generation_cost"] = total_generation_cost
        progress["total_uncached_cost"] = total_uncached_cost
        if chapter_errors:
            progress["chapter_errors"] = dict(chapter_errors)
        progress["current_stage"] = None
        emit()
    else:
        if composer_run is not None:
            if "total_generation_cost" not in progress:
                total_generation_cost += composer_run.generation_cost
            if "total_uncached_cost" not in progress:
                total_uncached_cost += composer_run.uncached_generation_cost

    if spec.disable_composer:
        composer_run = StageRun(
            summary_md=chapter_summaries_md,
            first_pass_summary_md=chapter_summaries_md,
            passes_used=0,
            generation_cost=0.0,
            uncached_generation_cost=0.0,
            raw_responses=(),
        )
        progress["composer_stage_run"] = serialize_stage_run(composer_run)

    if spec.disable_composer and chapter_judge_scores:
        all_scores = list(chapter_judge_scores.values())
        if all_scores:
            aggregated_scores = JudgeScores(
                faithfulness=sum(s.faithfulness for s in all_scores) / len(all_scores),
                concept_coverage=sum(s.concept_coverage for s in all_scores) / len(all_scores),
                qualifier_preservation=sum(s.qualifier_preservation for s in all_scores) / len(all_scores),
                no_fluff=sum(s.no_fluff for s in all_scores) / len(all_scores),
                structure_quality=sum(s.structure_quality for s in all_scores) / len(all_scores),
            )
            judge_result = AbsoluteJudgeResult(
                scores=aggregated_scores,
                rationale="Aggregated from chapter-level judges (composer disabled)",
                raw_response={},
            )
        else:
            judge_result = None
        source_md = join_book_source(book)
    else:
        source_md = join_book_source(book)
        judge_result = judge_if_requested(
            client,
            judge_model,
            summary_md=composer_run.summary_md,
            rubric=book.book_rubric,
            source_md=source_md,
            source_char_limit=judge_source_char_limit,
        )
    sample = SummarySample(
        sample_id=sample_id,
        level="book",
        target_words=final_target_words,
        summary_md=composer_run.summary_md,
        source_md=source_md,
        group_id=book_id,
        first_pass_summary_md=composer_run.first_pass_summary_md,
        passes_used=composer_run.passes_used,
        generation_cost=total_generation_cost,
        uncached_generation_cost=total_uncached_cost,
        malformed=not bool(composer_run.summary_md.strip()),
        rubric=book.book_rubric,
        judge_scores=judge_result.scores if judge_result else None,
    )
    trace = {
        "sample_id": sample.sample_id,
        "book_id": book_id,
        "target_words": final_target_words,
        "output_words": visible_word_count(composer_run.summary_md),
        "composer_passes_used": composer_run.passes_used,
        "mean_chapter_passes_used": (sum(chapter_passes) / len(chapter_passes)) if chapter_passes else 0.0,
        "generation_cost": total_generation_cost,
        "uncached_generation_cost": total_uncached_cost,
        "chapter_targets": {chapter.chapter_id: target for chapter, target in zip(book.chapters, chapter_targets)},
        "judge_rationale": judge_result.rationale if judge_result else "",
    }
    if spec.disable_composer and chapter_judge_scores:
        all_scores = list(chapter_judge_scores.values())
        sorted_faith = sorted(s.faithfulness for s in all_scores)
        sorted_cov = sorted(s.concept_coverage for s in all_scores)
        n = len(all_scores)
        trace["chapter_judge_scores"] = {
            ch_id: judge_scores_to_dict(scores)
            for ch_id, scores in chapter_judge_scores.items()
        }
        trace["agg_faithfulness"] = sum(s.faithfulness for s in all_scores) / n
        trace["agg_concept_coverage"] = sum(s.concept_coverage for s in all_scores) / n
        trace["worst_chapter_faithfulness"] = sorted_faith[0]
        trace["worst_chapter_id_faithfulness"] = [
            ch_id for ch_id, s in chapter_judge_scores.items()
            if s.faithfulness == sorted_faith[0]
        ][0]
        trace["best_chapter_faithfulness"] = sorted_faith[-1]
        trace["faithfulness_p10"] = sorted_faith[n // 10] if n >= 10 else sorted_faith[0]
        trace["faithfulness_p90"] = sorted_faith[(n * 9) // 10] if n >= 10 else sorted_faith[-1]
        trace["faithfulness_std"] = (
            (sum((s.faithfulness - trace["agg_faithfulness"]) ** 2 for s in all_scores) / n) ** 0.5
        )
    trace.update(taxonomy_trace_payload(book.taxonomy))
    if chapter_errors:
        trace["chapter_errors"] = dict(chapter_errors)
        trace["n_failed_chapters"] = len(chapter_errors)
    progress.clear()
    progress.update(base_progress)
    progress["phase"] = "completed"
    progress["chapter_targets"] = list(chapter_targets)
    progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
    progress["chapter_passes"] = list(chapter_passes)
    progress["final_target_words"] = final_target_words
    progress["composer_stage_run"] = serialize_stage_run(composer_run)
    progress["total_generation_cost"] = total_generation_cost
    progress["total_uncached_cost"] = total_uncached_cost
    if chapter_errors:
        progress["chapter_errors"] = dict(chapter_errors)
    progress["current_stage"] = None
    progress["sample_record"] = build_sample_record(sample, trace, item_key=sample_id)
    emit()

    if spec.disable_composer and chapter_runs_rows and run_dir:
        chapter_runs_dir = run_dir / "chapter_runs"
        chapter_runs_dir.mkdir(parents=True, exist_ok=True)
        chapter_csv_path = chapter_runs_dir / f"{sample_id}.csv"
        if chapter_csv_path.exists():
            existing_rows: List[Dict[str, Any]] = []
            with open(chapter_csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_rows.append(row)
            existing_ids = {r.get("sample_id", "") for r in existing_rows}
            if sample_id in existing_ids:
                chapter_csv_path = chapter_runs_dir / f"{sample_id}_{utc_now_ts()}.csv"
        fieldnames = [
            "sample_id", "chapter_id", "chapter_title", "target_words", "output_words",
            "passes_used", "generation_cost", "uncached_generation_cost",
            "judge_faithfulness", "judge_concept_coverage", "judge_qualifier_preservation",
            "judge_no_fluff", "judge_structure_quality",
            "flesch_reading_ease", "flesch_kincaid_grade", "sentence_count",
        ]
        with open(chapter_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in chapter_runs_rows:
                writer.writerow({**row, "sample_id": sample_id})
        progress["chapter_runs_csv"] = str(chapter_csv_path)

    return sample, trace


def _mean(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def taxonomy_trace_payload(taxonomy: BookTaxonomy) -> Dict[str, str]:
    return taxonomy.to_dict()


def summarize_slice_scores(
    sample_scores: Sequence[Any],
    *,
    trace_lookup: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    slices: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for field_name in SLICE_FIELDS:
        groups: Dict[str, List[Any]] = {}
        for sample_score in sample_scores:
            trace = trace_lookup.get(str(sample_score.sample_id), {})
            field_value = str(trace.get(field_name) or "unknown")
            groups.setdefault(field_value, []).append(sample_score)

        field_summary: Dict[str, Dict[str, Any]] = {}
        for field_value, group_scores in sorted(groups.items(), key=lambda item: item[0]):
            books = {str(trace_lookup.get(str(score.sample_id), {}).get("book_id") or score.group_id) for score in group_scores}
            field_summary[field_value] = {
                "n_samples": len(group_scores),
                "n_books": len([book_id for book_id in books if book_id]),
                "hard_fail_rate": _mean([1.0 if score.hard_fail else 0.0 for score in group_scores]),
                "mean_quality": _mean([float(score.quality) for score in group_scores]),
                "mean_utility": _mean([float(score.utility) for score in group_scores]),
                "mean_faithfulness": _mean([float(score.resolved_faithfulness) for score in group_scores]),
                "mean_concept_coverage": _mean([float(score.resolved_concept_coverage) for score in group_scores]),
                "mean_passes_used": 0.0,
            }
        slices[field_name] = field_summary
    return slices


def _replace_slice_pass_cost_metrics(
    slices: Dict[str, Dict[str, Dict[str, Any]]],
    *,
    samples: Sequence[SummarySample],
    trace_lookup: Mapping[str, Mapping[str, Any]],
) -> None:
    sample_lookup = {str(sample.sample_id): sample for sample in samples}
    for field_name, field_summary in slices.items():
        for field_value, payload in field_summary.items():
            matching_samples = [
                sample_lookup[sample_id]
                for sample_id, trace in trace_lookup.items()
                if str(trace.get(field_name) or "unknown") == field_value and sample_id in sample_lookup
            ]
            payload["mean_passes_used"] = _mean([float(sample.passes_used) for sample in matching_samples])
            payload["mean_uncached_generation_cost"] = _mean([float(sample.uncached_generation_cost) for sample in matching_samples])
            payload["mean_generation_cost"] = _mean([float(sample.generation_cost) for sample in matching_samples])


def build_slice_summary_payload(
    dataset_score,
    *,
    samples: Sequence[SummarySample],
    traces: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    trace_lookup = {str(trace.get("sample_id") or ""): dict(trace) for trace in traces if str(trace.get("sample_id") or "")}
    slices = summarize_slice_scores(dataset_score.sample_scores, trace_lookup=trace_lookup)
    _replace_slice_pass_cost_metrics(slices, samples=samples, trace_lookup=trace_lookup)

    genre_macro_summary = slices.get("genre_macro", {})
    worst_genre_macro: Dict[str, Any] = {}
    if genre_macro_summary:
        worst_label, worst_payload = min(
            genre_macro_summary.items(),
            key=lambda item: (
                float(item[1].get("mean_utility", 0.0)),
                float(item[1].get("mean_quality", 0.0)),
                item[0],
            ),
        )
        worst_genre_macro = {"slice_value": worst_label, **dict(worst_payload)}
    return slices, worst_genre_macro


def capture_openrouter_snapshots(
    *,
    client: Optional[OpenRouterClient],
    pricing_snapshot_arg: str,
    catalog_dir: Path,
    price_dir: Path,
    benchmark_version: str,
    timestamp: str,
) -> Tuple[Optional[Path], Optional[Path]]:
    catalog_path: Optional[Path] = None
    price_path: Optional[Path] = None

    if _is_cloud_function(client):
        print("Cloud Function mode: skipping catalog/pricing snapshot capture.")
        return catalog_path, price_path

    if client is not None:
        catalog = client.fetch_models(refresh=True)
        catalog_path = catalog_dir / f"{timestamp}__{benchmark_version}.json"
        save_json(
            catalog_path,
            {
                "captured_at_utc": timestamp,
                "benchmark_version": benchmark_version,
                "models": {model_id: info.raw for model_id, info in sorted(catalog.items())},
            },
        )
        if pricing_snapshot_arg:
            snapshot_payload = load_json(resolve_path(pricing_snapshot_arg))
        elif client.pricing_snapshot:
            snapshot_payload = dict(client.pricing_snapshot)
        else:
            snapshot_payload = derive_price_snapshot_from_catalog(catalog)
        price_path = price_dir / f"{timestamp}__{benchmark_version}.json"
        save_json(price_path, snapshot_payload)
    elif pricing_snapshot_arg:
        snapshot_payload = load_json(resolve_path(pricing_snapshot_arg))
        price_path = price_dir / f"{timestamp}__{benchmark_version}.json"
        save_json(price_path, snapshot_payload)

    return catalog_path, price_path


def append_results_tsv(
    *,
    path: Path,
    run_manifest: Mapping[str, Any],
    benchmark_manifest: Mapping[str, Any],
    dataset_score,
    mean_generation_cost: float,
    hypothesis: str,
    notes: str,
    run_artifact_path: Path,
    artifact_payload: Mapping[str, Any],
) -> None:
    dataset_payload = dict(artifact_payload.get("dataset_score") or {})
    worst_genre_macro = dict(dataset_payload.get("worst_genre_macro") or {})
    header = (
        "timestamp\trun_id\tbenchmark_version\tcorpus_version\trubric_version\tscoring_version\tjudge_version\t"
        "profile\tbench\tcandidate_name\tcandidate_sha256\thypothesis\tchapter_model\tcomposer_model\tjudge_model\t"
        "use_json_schema\treasoning_effort\tthinking\tmean_quality\tmean_utility\tmean_faithfulness\tmean_concept_coverage\tmean_final_length_error_pct\t"
        "mean_first_pass_length_error_pct\tmean_passes_used\tmean_uncached_generation_cost\tmean_generation_cost\t"
        "hard_fail_rate\tworst_genre_macro\tworst_genre_macro_utility\tworst_genre_macro_quality\t"
        "genre_macro_spread_utility\tn_genre_macros\trun_artifact\tcatalog_snapshot\tprice_snapshot\tnotes\ttransport\t"
        "prompt_version\tprompt_fingerprint\n"
    )
    if not path.exists() or path.read_text(encoding="utf-8") == "":
        path.write_text(header, encoding="utf-8")
    else:
        existing = path.read_text(encoding="utf-8")
        header_line = existing.split("\n", 1)[0]
        if header_line.strip() != header.strip():
            parts = existing.split("\n", 1)
            migrated = header + (parts[1] if len(parts) > 1 else "")
            path.write_text(migrated, encoding="utf-8")

    row = [
        str(run_manifest.get("created_at_utc", "")),
        str(run_manifest.get("run_id", "")),
        str(benchmark_manifest.get("benchmark_version", "")),
        str(benchmark_manifest.get("corpus_version", "")),
        str(benchmark_manifest.get("rubric_version", "")),
        str(benchmark_manifest.get("scoring_version", "")),
        str(run_manifest.get("judge_version_resolved", "")),
        str(run_manifest.get("profile", "")),
        str(run_manifest.get("bench", "")),
        str(run_manifest.get("candidate_name", "")),
        str(run_manifest.get("candidate_spec_sha256", "")),
        hypothesis,
        str(run_manifest.get("chapter_model", "")),
        str(run_manifest.get("composer_model", "")),
        str(run_manifest.get("judge_model", "")),
        str(run_manifest.get("use_json_schema", "")),
        str(run_manifest.get("reasoning_effort", "")),
        str(run_manifest.get("thinking_enabled", "")),
        f"{dataset_score.mean_quality:.6f}",
        f"{dataset_score.mean_utility:.6f}",
        f"{dataset_score.mean_faithfulness:.6f}",
        f"{dataset_score.mean_concept_coverage:.6f}",
        f"{dataset_score.mean_final_length_error_pct:.6f}",
        f"{dataset_score.mean_first_pass_length_error_pct:.6f}",
        f"{dataset_score.mean_passes_used:.6f}",
        f"{dataset_score.mean_uncached_cost:.6f}",
        f"{mean_generation_cost:.6f}",
        f"{dataset_score.hard_fail_rate:.6f}",
        str(worst_genre_macro.get("slice_value", "")),
        f"{float(worst_genre_macro.get('mean_utility', 0.0)):.6f}",
        f"{float(worst_genre_macro.get('mean_quality', 0.0)):.6f}",
        f"{float(dataset_payload.get('genre_macro_spread_utility', 0.0)):.6f}",
        str(int(dataset_payload.get("n_genre_macros", 0) or 0)),
        display_path(run_artifact_path),
        str(run_manifest.get("catalog_snapshot", "")),
        str(run_manifest.get("price_snapshot", "")),
        notes,
        str(run_manifest.get("generation_transport", "")),
        str(run_manifest.get("prompt_version", "")),
        str(run_manifest.get("prompt_fingerprint", "")),
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\t".join(row) + "\n")


def summarize_trace(
    dataset_score,
    traces: Sequence[Mapping[str, Any]],
    *,
    samples: Sequence[SummarySample],
    run_manifest: Mapping[str, Any],
    benchmark_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    slice_summaries, worst_genre_macro = build_slice_summary_payload(dataset_score, samples=samples, traces=traces)
    genre_macro_values = list((slice_summaries.get("genre_macro") or {}).values())
    genre_macro_spread_utility = 0.0
    if genre_macro_values:
        utilities = [float(payload.get("mean_utility", 0.0)) for payload in genre_macro_values]
        genre_macro_spread_utility = max(utilities) - min(utilities) if utilities else 0.0
    return {
        "run_manifest": dict(run_manifest),
        "benchmark_manifest": dict(benchmark_manifest),
        "dataset_score": {
            "n_samples": dataset_score.n_samples,
            "hard_fail_rate": dataset_score.hard_fail_rate,
            "mean_quality": dataset_score.mean_quality,
            "mean_utility": dataset_score.mean_utility,
            "mean_faithfulness": dataset_score.mean_faithfulness,
            "mean_concept_coverage": dataset_score.mean_concept_coverage,
            "mean_final_length_error_pct": dataset_score.mean_final_length_error_pct,
            "mean_first_pass_length_error_pct": dataset_score.mean_first_pass_length_error_pct,
            "mean_passes_used": dataset_score.mean_passes_used,
            "mean_uncached_cost": dataset_score.mean_uncached_cost,
            "mean_generation_cost": _mean([sample.generation_cost for sample in samples]),
            "by_group_quality": dataset_score.by_group_quality,
            "by_group_utility": dataset_score.by_group_utility,
            "slice_summaries": slice_summaries,
            "worst_genre_macro": worst_genre_macro,
            "n_genre_macros": len(slice_summaries.get("genre_macro") or {}),
            "genre_macro_spread_utility": genre_macro_spread_utility,
        },
        "sample_scores": [
            {
                "sample_id": sample_score.sample_id,
                "group_id": sample_score.group_id,
                "level": sample_score.level,
                "hard_fail": sample_score.hard_fail,
                "hard_fail_reasons": list(sample_score.hard_fail_reasons),
                "quality": sample_score.quality,
                "utility": sample_score.utility,
                "resolved_faithfulness": sample_score.resolved_faithfulness,
                "resolved_concept_coverage": sample_score.resolved_concept_coverage,
                "resolved_qualifier_preservation": sample_score.resolved_qualifier_preservation,
                "resolved_no_fluff": sample_score.resolved_no_fluff,
                "resolved_structure_quality": sample_score.resolved_structure_quality,
                "deterministic": asdict(sample_score.deterministic),
            }
            for sample_score in dataset_score.sample_scores
        ],
        "samples": [
            {
                "sample_id": sample.sample_id,
                "level": sample.level,
                "group_id": sample.group_id,
                "target_words": sample.target_words,
                "summary_visible_words": visible_word_count(sample.summary_md),
                "passes_used": sample.passes_used,
                "generation_cost": sample.generation_cost,
                "uncached_generation_cost": sample.uncached_generation_cost,
            }
            for sample in samples
        ],
        "traces": list(traces),
    }


def main() -> None:
    load_dotenv()
    args = parse_args()
    benchmark_manifest_path = resolve_path(args.benchmark_manifest)
    ensure_default_benchmark_manifest(benchmark_manifest_path)
    benchmark_manifest = load_benchmark_manifest(benchmark_manifest_path)

    # Fail fast on an unknown named scoring profile, before any API spend.
    requested_scoring_profile = str(getattr(args, "scoring_profile", "auto") or "auto")
    if requested_scoring_profile not in ("auto", ""):
        available = sorted((benchmark_manifest.get("scoring_gates") or {}).keys())
        if requested_scoring_profile not in available:
            raise SystemExit(
                f"Unknown scoring profile {requested_scoring_profile!r}. "
                f"Available: {', '.join(available) or '(none)'}"
            )

    spec_path = resolve_path(args.spec)
    candidate_module = load_module_from_path("candidate_spec_runtime", spec_path)

    if args.profile == "all":
        if args.resume:
            print("Error: --resume is not supported with --profile all. Resume a specific run instead.")
            raise SystemExit(1)
        profiles = candidate_module.get_profiles_by_time(args.time)
        if not profiles:
            print(f"No profiles found for time budget: {args.time}")
            raise SystemExit(1)
        print(f"Running {len(profiles)} profile(s) for time={args.time}: {profiles}")
        run_all_profiles(profiles, args, benchmark_manifest, spec_path)
    else:
        _run_single(args, benchmark_manifest, spec_path, inherited_run_id=None)


def run_all_profiles(
    profiles: Sequence[str],
    args: argparse.Namespace,
    benchmark_manifest: Dict[str, Any],
    spec_path: Path,
) -> None:
    """Run benchmark for multiple profiles sequentially."""
    for profile in profiles:
        print(f"\n{'='*60}")
        print(f"Running profile: {profile}")
        print(f"{'='*60}\n")
        args.profile = profile
        runs_root = resolve_path(args.runs_dir)
        benchmark_version = str(benchmark_manifest.get("benchmark_version", "benchmark"))
        check_dir = runs_root / benchmark_version
        candidates = sorted(check_dir.glob(f"*__{profile}_v*.state.json"), reverse=True)
        if candidates:
            state_path = candidates[0]
            state = load_json(state_path)
            completed = state.get("completed_count", 0)
            total = state.get("n_total_samples", 0)
            if completed >= total and total > 0:
                print(f"Skipping {profile}: already completed ({completed}/{total}). Use --resume to inspect.")
                continue
        _run_single(args, benchmark_manifest, spec_path, inherited_run_id=None)


class UnknownScoringProfile(ValueError):
    def __init__(self, profile: str, allowed: list):
        self.profile = profile
        self.allowed = list(allowed)
        super().__init__(
            f"Unknown scoring profile {profile!r}. Available: {', '.join(self.allowed) or '(none)'}"
        )


def select_scoring_config(
    benchmark_manifest: Mapping[str, Any],
    *,
    profile_name: str,
    judge_model: str,
    scoring_profile: str = "auto",
    base_config=DEFAULT_SCORING_CONFIG,
) -> Tuple[Any, str, str]:
    """Resolve the scoring config for a run.

    Judge-less ("deterministic") runs use ``<budget>_deterministic`` gates, whose
    semantic thresholds are ``null`` (disabled) because the deterministic proxies
    are not calibrated to the judged thresholds. Manifests without those keys
    fall back to the judged budget gate.

    ``scoring_profile="auto"`` derives the gate key from the budget as before.
    A named profile (e.g. ``"strict-v1"``) selects that gate explicitly; in
    deterministic mode its ``<name>_deterministic`` variant is preferred.

    Returns ``(scoring_config, scoring_mode, gate_key)``.
    """
    if profile_name.startswith("30m_"):
        budget_key = "30m"
    elif profile_name.startswith("60m_"):
        budget_key = "60m"
    else:
        budget_key = "default"
    scoring_mode = "judged" if judge_model else "deterministic"
    scoring_gates = benchmark_manifest.get("scoring_gates", {}) or {}

    named = str(scoring_profile or "auto").strip() or "auto"
    if named != "auto":
        if named not in scoring_gates:
            raise UnknownScoringProfile(named, sorted(scoring_gates.keys()))
        if scoring_mode == "deterministic":
            deterministic_key = f"{named}_deterministic"
            gate_key = deterministic_key if deterministic_key in scoring_gates else named
        else:
            gate_key = named
        gates = scoring_gates.get(gate_key)
    else:
        gate_key = budget_key if scoring_mode == "judged" else f"{budget_key}_deterministic"
        gates = scoring_gates.get(gate_key)
        if not gates and gate_key != budget_key:
            # Old manifests lack *_deterministic keys; fall back to the judged gate
            # for this budget so behavior is unchanged for them.
            gate_key = budget_key
            gates = scoring_gates.get(budget_key)

    scoring_config = base_config
    if gates:
        override_kwargs: Dict[str, Any] = {}
        # Explicit keys (including null => disable) are honored; absent keys keep defaults.
        if "min_faithfulness" in gates:
            override_kwargs["min_faithfulness"] = gates["min_faithfulness"]
        if "min_concept_coverage" in gates:
            override_kwargs["min_concept_coverage"] = gates["min_concept_coverage"]
        if gates.get("max_final_length_error_pct") is not None:
            override_kwargs["max_final_length_error_pct"] = gates["max_final_length_error_pct"]
        if gates.get("max_passes") is not None:
            override_kwargs["max_passes"] = gates["max_passes"]
        if override_kwargs:
            scoring_config = apply_gates_override(scoring_config, **override_kwargs)
    return scoring_config, scoring_mode, gate_key


def _run_single(
    args: argparse.Namespace,
    benchmark_manifest: Dict[str, Any],
    spec_path: Path,
    inherited_run_id: Optional[str],
) -> str:
    if inherited_run_id:
        args.run_id = inherited_run_id
    spec_path = resolve_path(args.spec)
    candidate_module = load_module_from_path("candidate_spec_runtime", spec_path)
    spec = copy.deepcopy(candidate_module.get_candidate(args.profile))
    client = make_client(args)
    prompt_selection = _resolve_prompt_versions(args, spec, candidate_module, client)
    _LATENCY_ACCUMULATOR.clear()
    set_prompt_context(
        chapter=prompt_selection.get("chapter") or {},
        composer=prompt_selection.get("composer") or {},
        allow_override=bool(getattr(args, "allow_prompt_override", False)),
        registry_sha=str(prompt_selection.get("registry_sha") or ""),
    )
    if _is_cloud_function(client) and args.chapter_concurrency and args.chapter_concurrency > 1:
        set_cf_concurrency(args.chapter_concurrency)
    else:
        set_cf_concurrency(None)
    if _is_cloud_function(client):
        _concurrency = int(args.chapter_concurrency or 1)
        if _concurrency > 1:
            print(
                f"Cloud Function chapter concurrency: {_concurrency} "
                f"(chapters dispatched in parallel)."
            )
        else:
            print(
                "WARN: Cloud Function transport is active but chapter concurrency is 1; "
                "chapters will run sequentially. Pass --chapter-concurrency N or set "
                "CF_CHAPTER_CONCURRENCY=N (>1) to parallelize.",
                file=sys.stderr,
            )
        try:
            auth_headers = getattr(client, "auth_headers", None)
            if callable(auth_headers):
                auth_headers()
        except CloudFunctionAuthError as exc:
            print()
            print("=" * 72)
            print("CLOUD FUNCTION AUTH FAILED")
            print("Could not obtain an OIDC identity token; no requests were sent and")
            print("nothing was billed. Re-authenticate, then retry:")
            print()
            print("  gcloud auth login")
            print()
            print("Or run with AUTH_MODE=env and GCP_IDENTITY_TOKEN set (CI/service credentials).")
            print("=" * 72)
            print(f"details: {exc}", file=sys.stderr)
            raise SystemExit(4)

    bench_name = Path(args.bench).stem if str(args.bench).endswith(".jsonl") else args.bench
    bench_path = resolve_path(args.bench) if str(args.bench).endswith(".jsonl") else (ROOT / "bench" / f"{args.bench}.jsonl")
    bench_rows_from_file = load_jsonl(bench_path)
    if args.max_samples > 0:
        bench_rows_from_file = bench_rows_from_file[: args.max_samples]

    data_dir = resolve_path(args.data_dir)
    runs_root = resolve_path(args.runs_dir)
    benchmark_version = str(benchmark_manifest.get("benchmark_version", "benchmark"))
    if args.mock:
        run_dir = runs_root / "mock" / benchmark_version
    else:
        run_dir = runs_root / benchmark_version
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        run_id = args.resume.strip()
        if not run_id:
            raise ValueError("--resume requires a non-empty run id")
        state_path = run_dir / f"{run_id}.state.json"
        if not state_path.exists():
            raise FileNotFoundError(f"Run state not found for resume: {state_path}")
        state = load_json(state_path)
        if not args.judge_model:
            args.judge_model = str((state.get("run_manifest") or {}).get("judge_model") or "")
        validate_resume_state(
            state,
            run_id=run_id,
            bench_name=bench_name,
            profile=spec.profile,
            spec_path=spec_path,
            benchmark_manifest=benchmark_manifest,
            judge_model=args.judge_model,
        )
        run_manifest = dict(state.get("run_manifest") or {})
        stored_benchmark_manifest = state.get("benchmark_manifest")
        if isinstance(stored_benchmark_manifest, Mapping):
            benchmark_manifest = dict(stored_benchmark_manifest)
        stored_rows = state.get("bench_rows")
        bench_rows = list(stored_rows) if isinstance(stored_rows, list) and stored_rows else bench_rows_from_file
        samples_path = resolve_path(str(state.get("samples_path") or (run_dir / f"{run_id}.samples.jsonl")))
        out_path = resolve_path(str(state.get("out_path") or (run_dir / f"{run_id}.json")))
        resume_events = list(state.get("resume_events_utc") or [])
        resume_events.append(utc_now_iso())
        state["resume_events_utc"] = resume_events
        state["status"] = "running"
        state["latest_error"] = None
        save_run_state(state_path, state)
        print(f"Resuming run ID: {run_id}")
    else:
        bench_rows = bench_rows_from_file
        timestamp_dt = datetime.now(timezone.utc)
        timestamp_id = timestamp_dt.strftime("%Y%m%dT%H%M%SZ")
        timestamp_iso = timestamp_dt.isoformat()
        catalog_snapshot_path, price_snapshot_path = capture_openrouter_snapshots(
            client=client,
            pricing_snapshot_arg=args.pricing_snapshot,
            catalog_dir=resolve_path(args.catalog_snapshots_dir),
            price_dir=resolve_path(args.price_snapshots_dir),
            benchmark_version=benchmark_version,
            timestamp=timestamp_id,
        )
        run_id = args.run_id.strip() or build_run_id(
            timestamp=timestamp_id,
            benchmark_version=benchmark_version,
            bench_name=bench_name,
            profile=spec.profile,
            candidate_name=spec.name,
        )
        state_path = run_dir / f"{run_id}.state.json"
        samples_path = run_dir / f"{run_id}.samples.jsonl"
        out_path = run_dir / f"{run_id}.json"
        if state_path.exists() or samples_path.exists() or out_path.exists():
            raise FileExistsError(
                f"Run artifacts already exist for {run_id!r}. Use --resume {run_id!r} or choose a different --run-id."
            )

        prompt_hashes = build_prompt_hashes(
            candidate_module,
            spec,
            prompt_version=str((prompt_selection.get("chapter") or {}).get("version") or ""),
            prompt_fingerprint=str((prompt_selection.get("chapter") or {}).get("fingerprint") or ""),
            prompt_source="registry" if (prompt_selection.get("chapter") or {}).get("has_overlay") else "request",
            prompt_registry_sha=str(prompt_selection.get("registry_sha") or ""),
        )
        judge_version_resolved = (
            f"{benchmark_manifest.get('judge_version', 'judge-v1')}::{args.judge_model}"
            if args.judge_model
            else f"{benchmark_manifest.get('judge_version', 'judge-v1')}::deterministic"
        )
        run_manifest = {
            "run_id": run_id,
            "created_at_utc": timestamp_iso,
            "benchmark_manifest_path": display_path(resolve_path(args.benchmark_manifest)),
            "benchmark_version": benchmark_manifest.get("benchmark_version", ""),
            "corpus_version": benchmark_manifest.get("corpus_version", ""),
            "rubric_version": benchmark_manifest.get("rubric_version", ""),
            "scoring_version": benchmark_manifest.get("scoring_version", ""),
            "judge_version_resolved": judge_version_resolved,
            "profile": spec.profile,
            "bench": bench_name,
            "bench_path": display_path(bench_path),
            "data_dir": display_path(data_dir),
            "candidate_name": spec.name,
            "candidate_spec_path": display_path(spec_path),
            "candidate_spec_sha256": sha256_file(spec_path),
            "chapter_model": spec.chapter_stage.model,
            "composer_model": spec.composer_stage.model,
            "judge_model": args.judge_model,
            "use_json_schema": spec.use_json_schema,
            "thinking_enabled": _is_thinking_enabled(spec),
            "reasoning_effort": _reasoning_effort_label(spec),
            "chapter_provider": spec.chapter_stage.provider,
            "composer_provider": spec.composer_stage.provider,
            "prompt_hashes": prompt_hashes,
            "catalog_snapshot": display_path(catalog_snapshot_path) if catalog_snapshot_path else "",
            "price_snapshot": display_path(price_snapshot_path) if price_snapshot_path else "",
            "hypothesis": args.hypothesis,
            "notes": args.notes,
        }
        state = {
            "run_id": run_id,
            "created_at_utc": timestamp_iso,
            "status": "running",
            "run_manifest": run_manifest,
            "benchmark_manifest": dict(benchmark_manifest),
            "bench_rows": _json_safe(list(bench_rows)),
            "completed_sample_ids": [],
            "completed_count": 0,
            "n_total_samples": len(bench_rows),
            "current_item": None,
            "latest_error": None,
            "resume_events_utc": [],
            "state_path": display_path(state_path),
            "samples_path": display_path(samples_path),
            "out_path": display_path(out_path),
        }
        save_run_state(state_path, state)
        print(f"Run ID: {run_id}")

    run_manifest = dict(state.get("run_manifest") or run_manifest)
    chapter_prompt = prompt_selection.get("chapter") or {}
    composer_prompt = prompt_selection.get("composer") or {}
    previous_chapter_version = str(run_manifest.get("prompt_version") or "")
    run_manifest["prompt_version"] = str(chapter_prompt.get("version") or "")
    run_manifest["composer_prompt_version"] = str(composer_prompt.get("version") or "")
    run_manifest["prompt_fingerprint"] = str(chapter_prompt.get("fingerprint") or "")
    run_manifest["prompt_source"] = "registry" if chapter_prompt.get("has_overlay") else "request"
    run_manifest["prompt_registry_sha"] = str(prompt_selection.get("registry_sha") or "")
    run_manifest["prompt_slot"] = str(prompt_selection.get("slot") or "")
    run_manifest["prompt_selection"] = _json_safe(prompt_selection)
    if args.resume and previous_chapter_version and previous_chapter_version != run_manifest["prompt_version"]:
        run_manifest["prompt_version_changed_on_resume"] = True
    if _is_cloud_function(client):
        run_manifest["generation_transport"] = "cloud_function"
        run_manifest["function_url"] = client.function_url
        run_manifest["auth_mode"] = client.auth_mode
        run_manifest["chapter_concurrency"] = int(args.chapter_concurrency or 1)
    elif args.mock:
        run_manifest["generation_transport"] = "mock"
    else:
        run_manifest["generation_transport"] = "direct"
    state["run_manifest"] = run_manifest

    samples, traces, completed_item_keys = load_sample_checkpoints(samples_path, data_dir)
    completed_set = set(completed_item_keys)
    merged_completed_ids: List[str] = []
    for item_key in list(state.get("completed_sample_ids") or []) + completed_item_keys:
        if item_key not in merged_completed_ids:
            merged_completed_ids.append(item_key)
    state["completed_sample_ids"] = merged_completed_ids
    state["completed_count"] = len(completed_set)
    current_item = state.get("current_item")
    if isinstance(current_item, Mapping) and str(current_item.get("item_key") or "") in completed_set:
        state["current_item"] = None
    save_run_state(state_path, state)

    if completed_set:
        print(f"Loaded {len(completed_set)} completed checkpointed samples.")

    state_lock = threading.Lock()
    if not isinstance(state.get("current_items"), dict):
        state["current_items"] = {}
    failed_items: Dict[str, Dict[str, Any]] = {}
    attempted_failed: set = set()
    state["failed_items"] = failed_items

    def _item_uses_chapter_path(item: Mapping[str, Any]) -> bool:
        return str(item.get("level", "")) == "chapter" or bench_name == "chapter_fast"

    def _resume_progress_for(item_key: str) -> Optional[Mapping[str, Any]]:
        current_items = state.get("current_items")
        if isinstance(current_items, Mapping):
            stored = current_items.get(item_key)
            if isinstance(stored, Mapping):
                return stored
        legacy = state.get("current_item")
        if isinstance(legacy, Mapping) and str(legacy.get("item_key") or "") == item_key:
            return legacy
        return None

    def _process_parallel_chapter_item(index: int, item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        item_key = sample_key_from_item(item, bench_name=bench_name)

        def _set_progress(progress_payload: Mapping[str, Any]) -> None:
            maybe_print_progress(run_id, progress_payload)
            with state_lock:
                state["current_items"][item_key] = _json_safe(progress_payload)
                state["current_item"] = _json_safe(progress_payload)
                state["status"] = "running"
                save_run_state(state_path, state)

        with state_lock:
            current_progress = _resume_progress_for(item_key)
            if not isinstance(current_progress, Mapping):
                current_progress = initial_progress_for_item(item, bench_name=bench_name)
            state["current_items"][item_key] = _json_safe(current_progress)
            state["current_item"] = _json_safe(current_progress)
            state["status"] = "running"
            state["latest_error"] = None
            save_run_state(state_path, state)

        try:
            sample, trace = run_chapter_sample(
                item,
                candidate_module=candidate_module,
                spec=spec,
                client=client,
                data_dir=data_dir,
                judge_model=args.judge_model,
                judge_source_char_limit=args.judge_source_char_limit,
                resume_progress=current_progress,
                progress_callback=_set_progress,
                run_dir=run_dir,
            )
        except OpenRouterInsufficientCreditsError:
            with state_lock:
                state["status"] = "paused_insufficient_credits"
                save_run_state(state_path, state)
            raise
        except ModelUnavailableError as exc:
            with state_lock:
                state["status"] = "paused_model_unavailable"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
            raise
        except CloudFunctionAuthError as exc:
            with state_lock:
                state["status"] = "paused_auth_error"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
            raise
        except Exception as exc:
            error_payload = error_to_dict(exc)
            with state_lock:
                state["failed_items"][item_key] = error_payload
                # Keep current_items[item_key] so a later resume can continue this
                # sample from its last checkpoint instead of regenerating it.
                state["latest_error"] = error_payload
                save_run_state(state_path, state)
            print(
                f"WARN: sample {item_key} failed; continuing with remaining items. "
                f"{error_payload['type']}: {error_payload['message']}",
                file=sys.stderr,
            )
            return error_payload

        with state_lock:
            append_sample_checkpoint(
                path=samples_path,
                run_id=run_id,
                sample_index=index,
                item_key=item_key,
                sample=sample,
                trace=trace,
            )
            completed_set.add(item_key)
            if item_key not in state["completed_sample_ids"]:
                state["completed_sample_ids"].append(item_key)
            state["completed_count"] = len(completed_set)
            state["current_items"].pop(item_key, None)
            if str((state.get("current_item") or {}).get("item_key") or "") == item_key:
                state["current_item"] = None
            state["latest_error"] = None
            state["status"] = "running"
            save_run_state(state_path, state)
            completed_now = len(completed_set)
        print(
            f"[{completed_now}/{len(bench_rows)}] {sample.sample_id}: "
            f"words={visible_word_count(sample.summary_md)} target={sample.target_words} "
            f"passes={sample.passes_used} cost={sample.uncached_generation_cost:.6f}"
        )
        return None

    pending_entries = [
        (index, item)
        for index, item in enumerate(bench_rows)
        if sample_key_from_item(item, bench_name=bench_name) not in completed_set
    ]
    use_parallel = (
        _is_cloud_function(client)
        and int(args.chapter_concurrency or 1) > 1
        and len(pending_entries) > 1
        and all(_item_uses_chapter_path(item) for _, item in pending_entries)
    )

    if use_parallel:
        max_workers = min(int(args.chapter_concurrency), len(pending_entries))
        print(
            f"Running {len(pending_entries)} chapter items concurrently via Cloud Function "
            f"(concurrency={max_workers})."
        )
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            while True:
                pending_entries = [
                    (index, item)
                    for index, item in enumerate(bench_rows)
                    if sample_key_from_item(item, bench_name=bench_name) not in completed_set
                    and sample_key_from_item(item, bench_name=bench_name) not in attempted_failed
                ]
                if not pending_entries:
                    break
                pending_iter = iter(pending_entries)
                in_flight: Dict[Any, Tuple[int, Mapping[str, Any]]] = {}
                abort_exc: Optional[BaseException] = None

                def _submit_next() -> bool:
                    try:
                        index, item = next(pending_iter)
                    except StopIteration:
                        return False
                    future = executor.submit(_process_parallel_chapter_item, index, item)
                    in_flight[future] = (index, item)
                    return True

                for _ in range(max_workers):
                    if not _submit_next():
                        break

                while in_flight:
                    done, _ = futures_wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
                    for future in done:
                        _index, item = in_flight.pop(future, (None, None))
                        try:
                            item_error = future.result()
                        except (OpenRouterInsufficientCreditsError, ModelUnavailableError, CloudFunctionAuthError) as exc:
                            abort_exc = exc
                            item_error = None
                        if item_error is not None and item is not None:
                            attempted_failed.add(sample_key_from_item(item, bench_name=bench_name))
                        if abort_exc is None:
                            _submit_next()
                    if abort_exc is not None:
                        break

                if abort_exc is None:
                    break

                for future in list(in_flight.keys()):
                    future.cancel()
                executor.shutdown(wait=True)

                if isinstance(abort_exc, CloudFunctionAuthError):
                    state["status"] = "paused_auth_error"
                    state["latest_error"] = error_to_dict(abort_exc)
                    save_run_state(state_path, state)
                    print_cf_auth_error_banner(
                        abort_exc, bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                    )
                    raise SystemExit(4)

                if isinstance(abort_exc, ModelUnavailableError):
                    state["status"] = "paused_model_unavailable"
                    state["latest_error"] = error_to_dict(abort_exc)
                    save_run_state(state_path, state)
                    print_model_unavailable_banner(
                        abort_exc, bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                    )
                    raise SystemExit(3)

                state["status"] = "paused_insufficient_credits"
                state["latest_error"] = error_to_dict(abort_exc)
                save_run_state(state_path, state)
                print(
                    f"Paused run {run_id} due to insufficient OpenRouter credits while running chapter items."
                )
                if not args.wait_for_credits:
                    print(
                        "Add credits, then resume with: "
                        + resume_command_hint(
                            bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                        )
                    )
                    raise SystemExit(2)
                if not wait_for_credits(client=client, args=args, run_id=run_id):
                    print(f"Stopped waiting for credits. Resume later with --resume {run_id}.")
                    raise SystemExit(2)
                state["status"] = "running"
                state["latest_error"] = None
                save_run_state(state_path, state)
                executor = ThreadPoolExecutor(max_workers=max_workers)
        finally:
            executor.shutdown(wait=True)

        samples, traces, completed_item_keys = load_sample_checkpoints(samples_path, data_dir)
        completed_set = set(completed_item_keys)

    for index, item in enumerate(bench_rows):
        item_key = sample_key_from_item(item, bench_name=bench_name)
        if item_key in completed_set or item_key in attempted_failed:
            continue

        while True:
            current_progress = state.get("current_item") if isinstance(state.get("current_item"), Mapping) else None
            if not current_progress or str(current_progress.get("item_key") or "") != item_key:
                current_progress = initial_progress_for_item(item, bench_name=bench_name)
                state["current_item"] = current_progress
                state["status"] = "running"
                state["latest_error"] = None
                save_run_state(state_path, state)

            def progress_callback(progress_payload: Mapping[str, Any]) -> None:
                maybe_print_progress(run_id, progress_payload)
                state["current_item"] = _json_safe(progress_payload)
                state["status"] = "running"
                save_run_state(state_path, state)

            try:
                level = str(item.get("level", ""))
                if level == "chapter" or bench_name == "chapter_fast":
                    sample, trace = run_chapter_sample(
                        item,
                        candidate_module=candidate_module,
                        spec=spec,
                        client=client,
                        data_dir=data_dir,
                        judge_model=args.judge_model,
                        judge_source_char_limit=args.judge_source_char_limit,
                        resume_progress=current_progress,
                        progress_callback=progress_callback,
                        run_dir=run_dir,
                    )
                else:
                    sample, trace = run_book_sample(
                        item,
                        candidate_module=candidate_module,
                        spec=spec,
                        client=client,
                        data_dir=data_dir,
                        judge_model=args.judge_model,
                        judge_source_char_limit=args.judge_source_char_limit,
                        resume_progress=current_progress,
                        progress_callback=progress_callback,
                        run_dir=run_dir,
                        chapter_concurrency=args.chapter_concurrency,
                    )

                append_sample_checkpoint(
                    path=samples_path,
                    run_id=run_id,
                    sample_index=index,
                    item_key=item_key,
                    sample=sample,
                    trace=trace,
                )
                samples.append(sample)
                traces.append(trace)
                completed_set.add(item_key)
                if item_key not in state["completed_sample_ids"]:
                    state["completed_sample_ids"].append(item_key)
                state["completed_count"] = len(completed_set)
                state["current_item"] = None
                state["latest_error"] = None
                state["status"] = "running"
                save_run_state(state_path, state)
                print(
                    f"[{len(completed_set)}/{len(bench_rows)}] {sample.sample_id}: "
                    f"words={visible_word_count(sample.summary_md)} target={sample.target_words} "
                    f"passes={sample.passes_used} cost={sample.uncached_generation_cost:.6f}"
                )
                break
            except OpenRouterInsufficientCreditsError as exc:
                state["status"] = "paused_insufficient_credits"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
                print(
                    f"Paused run {run_id} due to insufficient OpenRouter credits while processing {item_key}."
                )
                if not args.wait_for_credits:
                    print(
                        "Add credits, then resume with: "
                        + resume_command_hint(
                            bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                        )
                    )
                    raise SystemExit(2)
                if not wait_for_credits(client=client, args=args, run_id=run_id):
                    print(f"Stopped waiting for credits. Resume later with --resume {run_id}.")
                    raise SystemExit(2)
                state["status"] = "running"
                state["latest_error"] = None
                save_run_state(state_path, state)
                continue
            except ModelUnavailableError as exc:
                state["status"] = "paused_model_unavailable"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
                print_model_unavailable_banner(
                    exc, bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                )
                raise SystemExit(3)
            except CloudFunctionAuthError as exc:
                state["status"] = "paused_auth_error"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
                print_cf_auth_error_banner(
                    exc, bench_name=bench_name, profile=spec.profile, run_id=run_id, args=args
                )
                raise SystemExit(4)
            except Exception as exc:
                state["status"] = "failed"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
                raise

    if not samples:
        all_failed_error = RuntimeError(
            f"All {len(failed_items)} sample(s) failed for run {run_id}; nothing to score."
        )
        state["status"] = "failed"
        state["latest_error"] = error_to_dict(all_failed_error)
        state["failed_items"] = failed_items
        save_run_state(state_path, state)
        raise all_failed_error

    try:
        scoring_config, scoring_mode, gate_key = select_scoring_config(
            benchmark_manifest,
            profile_name=spec.profile,
            judge_model=args.judge_model,
            scoring_profile=getattr(args, "scoring_profile", "auto"),
        )
    except UnknownScoringProfile as exc:
        raise SystemExit(str(exc))

    dataset_score = score_dataset(samples, config=scoring_config)
    mean_generation_cost = _mean([sample.generation_cost for sample in samples])
    run_manifest = dict(state.get("run_manifest") or run_manifest)
    if _is_cloud_function(client):
        if getattr(client, "scoring_version_mismatch", False):
            run_manifest["scoring_version_mismatch"] = True
            print(
                "WARN: local scoring.py sha256 does not match the Cloud Function's "
                "deployed scoring_version (see run_manifest.cloud_function_meta).",
                file=sys.stderr,
            )
        cf_meta = getattr(client, "last_meta", {}) or {}
        if cf_meta:
            run_manifest["cloud_function_meta"] = dict(cf_meta)
    resume_events = list(state.get("resume_events_utc") or [])
    run_manifest["resume_count"] = len(resume_events)
    run_manifest["scoring_mode"] = scoring_mode
    run_manifest["scoring_gate_key"] = gate_key
    requested_profile = str(getattr(args, "scoring_profile", "auto") or "auto")
    run_manifest["scoring_profile"] = requested_profile
    if requested_profile not in ("auto", "") and scoring_mode == "deterministic":
        print(
            f"WARN: --scoring-profile {requested_profile} requested without --judge-model; "
            "semantic gates are unavailable for deterministic proxies and stay disabled.",
            file=sys.stderr,
        )
    mean_latency_seconds = _mean(_LATENCY_ACCUMULATOR) if _LATENCY_ACCUMULATOR else 0.0
    run_manifest["mean_latency_seconds"] = mean_latency_seconds
    run_manifest["n_latency_samples"] = len(_LATENCY_ACCUMULATOR)
    if _is_cloud_function(client):
        cf_meta = getattr(client, "last_meta", {}) or {}
        if cf_meta:
            try:
                prompt_registry.write_cf_report({
                    "prompt_registry_sha": str(cf_meta.get("prompt_registry_sha") or ""),
                    "prompt_version": str(cf_meta.get("prompt_version") or ""),
                    "prompt_fingerprint": str(cf_meta.get("prompt_fingerprint") or ""),
                    "prompt_source": str(cf_meta.get("prompt_source") or ""),
                    "run_id": run_id,
                })
            except OSError:
                pass
    if resume_events:
        run_manifest["resume_events_utc"] = resume_events
    if failed_items:
        run_manifest["failed_items"] = dict(failed_items)
        run_manifest["n_failed_items"] = len(failed_items)

    artifact_payload = summarize_trace(
        dataset_score,
        traces,
        samples=samples,
        run_manifest=run_manifest,
        benchmark_manifest=benchmark_manifest,
    )
    dataset_payload = dict(artifact_payload.get("dataset_score") or {})
    worst_genre_macro = dict(dataset_payload.get("worst_genre_macro") or {})
    scoring_gates_override_dict = asdict(scoring_config) if gate_key != "default" else None
    if scoring_gates_override_dict is not None:
        artifact_payload["scoring_gates_override"] = scoring_gates_override_dict

    summary_block = {
        "run_id": run_id,
        "benchmark_version": benchmark_manifest.get("benchmark_version", ""),
        "bench": bench_name,
        "profile": spec.profile,
        "candidate_name": spec.name,
        "n_samples": dataset_score.n_samples,
        "scoring_mode": scoring_mode,
        "scoring_gates_override": scoring_gates_override_dict,
        "hard_fail_rate": dataset_score.hard_fail_rate,
        "mean_quality": dataset_score.mean_quality,
        "mean_utility": dataset_score.mean_utility,
        "mean_faithfulness": dataset_score.mean_faithfulness,
        "mean_concept_coverage": dataset_score.mean_concept_coverage,
        "mean_final_length_error_pct": dataset_score.mean_final_length_error_pct,
        "mean_first_pass_length_error_pct": dataset_score.mean_first_pass_length_error_pct,
        "mean_passes_used": dataset_score.mean_passes_used,
        "mean_uncached_cost": dataset_score.mean_uncached_cost,
        "mean_generation_cost": mean_generation_cost,
        "mean_latency_seconds": mean_latency_seconds,
        "prompt_version": run_manifest.get("prompt_version", ""),
        "prompt_fingerprint": run_manifest.get("prompt_fingerprint", ""),
        "worst_genre_macro": worst_genre_macro.get("slice_value", ""),
        "worst_genre_macro_utility": float(worst_genre_macro.get("mean_utility", 0.0) or 0.0),
        "genre_macro_spread_utility": float(dataset_payload.get("genre_macro_spread_utility", 0.0) or 0.0),
        "n_genre_macros": int(dataset_payload.get("n_genre_macros", 0) or 0),
        "n_failed_items": len(failed_items),
    }
    print(json.dumps(summary_block, ensure_ascii=False, indent=2))

    save_json(out_path, artifact_payload)
    print(f"Wrote run artifact: {display_path(out_path)}")

    if args.write_results:
        results_path = resolve_path(args.results_tsv)
        append_results_tsv(
            path=results_path,
            run_manifest=run_manifest,
            benchmark_manifest=benchmark_manifest,
            dataset_score=dataset_score,
            mean_generation_cost=mean_generation_cost,
            hypothesis=args.hypothesis or str(run_manifest.get("hypothesis") or ""),
            notes=args.notes or str(run_manifest.get("notes") or ""),
            run_artifact_path=out_path,
            artifact_payload=artifact_payload,
        )
        print(f"Updated results table: {display_path(results_path)}")

    state["status"] = "finished_with_errors" if failed_items else "finished"
    state["current_item"] = None
    state["latest_error"] = None
    state["completed_count"] = len(completed_set)
    state["completed_sample_ids"] = list(state.get("completed_sample_ids") or [])
    state["failed_items"] = failed_items
    state["run_manifest"] = run_manifest
    state["out_path"] = display_path(out_path)
    save_run_state(state_path, state)


if __name__ == "__main__":
    main()
