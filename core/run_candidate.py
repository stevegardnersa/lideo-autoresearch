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
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.book_data import BookTaxonomy, taxonomy_from_manifest
from core.judge import AbsoluteJudgeResult, judge_summary_absolute
from core.openrouter_client import (
    GenerationResult,
    OpenRouterAPIError,
    OpenRouterHTTPError,
    OpenRouterClient,
    OpenRouterInsufficientCreditsError,
    UsageRecord,
)
from core.reasoning import effort_enables_thinking, manifest_effort_label, stage_reasoning_effort
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
_last_progress_print: Dict[str, Any] = {"ts": 0.0, "phase": "", "item_key": ""}


def maybe_print_progress(run_id: str, payload: Mapping[str, Any]) -> None:
    global _last_progress_print
    now = time.time()
    phase = str(payload.get("phase") or "")
    item_key = str(payload.get("item_key") or "")
    changed = phase != _last_progress_print["phase"] or item_key != _last_progress_print["item_key"]
    if not changed and (now - float(_last_progress_print["ts"])) < PROGRESS_PRINT_INTERVAL_S:
        return
    _last_progress_print = {"ts": now, "phase": phase, "item_key": item_key}
    pieces = [f"[{run_id}]", item_key, phase]
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
    }


def deserialize_stage_run(payload: Mapping[str, Any]) -> StageRun:
    return StageRun(
        summary_md=str(payload.get("summary_md") or ""),
        first_pass_summary_md=str(payload.get("first_pass_summary_md") or ""),
        passes_used=int(payload.get("passes_used") or 0),
        generation_cost=float(payload.get("generation_cost") or 0.0),
        uncached_generation_cost=float(payload.get("uncached_generation_cost") or 0.0),
        raw_responses=tuple(payload.get("raw_responses") or []),
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

    if management_key and client is not None:
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
    parser.add_argument("--hypothesis", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--mock", action="store_true", help="Use a deterministic mock summarizer instead of OpenRouter")
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
    return OpenRouterClient.from_env(
        api_key_env=args.api_key_env,
        pricing_snapshot_path=args.pricing_snapshot,
        referer=args.referer,
        title=args.title,
        timeout=600,
    )


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
    blocks = [
        "Revise the whole-book summary to hit the target length more accurately.",
        f"Book title: {book_title}" if book_title else "",
        f"Target visible words: {target_words}. Acceptable range: {low}-{high}. This repair direction is: {direction}.",
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
    try:
        return client.chat_completion(request_body)
    except (OpenRouterAPIError, OpenRouterHTTPError) as e:
        if isinstance(e, OpenRouterHTTPError):
            model_in_request = request_body.get("model", "unknown") if isinstance(request_body, dict) else "unknown"
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
            request = build_openrouter_request(
                stage=stage_config,
                system_prompt=system_prompt,
                user_prompt=retry_prompt,
                schema_name=spec.json_schema_name,
                use_json_schema=use_json_schema,
            )
            result = invoke_generation(
                client,
                request,
                mock_source_md=mock_source_md,
                target_words=attempt_target,
                current_summary_md=current_for_mock,
            )
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

    for chapter, target_words in zip(book.chapters, chapter_targets):
        if chapter.chapter_id in completed_map:
            chapter_run = completed_map[chapter.chapter_id]
            chapter_outputs.append((chapter, chapter_run))
            chapter_passes.append(chapter_run.passes_used)
            total_generation_cost += chapter_run.generation_cost
            total_uncached_cost += chapter_run.uncached_generation_cost
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

        chapter_user_prompt = candidate_module.render_chapter_user(
            spec,
            source_md=chapter.source_md,
            target_words=target_words,
            book_title=book.book_title,
            chapter_title=chapter.chapter_title,
            toc_md=book.toc_md,
            book_metadata=book.metadata_md,
        )
        resume_stage_state = None
        if (
            str(current_stage.get("stage_kind") or "") == "chapter"
            and str(current_stage.get("chapter_id") or "") == chapter.chapter_id
            and isinstance(current_stage.get("stage_state"), Mapping)
        ):
            resume_stage_state = current_stage.get("stage_state")

        def chapter_checkpoint(stage_state: Mapping[str, Any], *, chapter_key: str = chapter.chapter_id) -> None:
            progress.clear()
            progress.update(base_progress)
            progress["phase"] = "chapters"
            progress["chapter_targets"] = list(chapter_targets)
            progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
            progress["chapter_passes"] = list(chapter_passes)
            progress["total_generation_cost"] = total_generation_cost
            progress["total_uncached_cost"] = total_uncached_cost
            progress["current_stage"] = {
                "stage_kind": "chapter",
                "chapter_id": chapter_key,
                "stage_state": _json_safe(stage_state),
            }
            emit()

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
            checkpoint_callback=chapter_checkpoint,
        )
        chapter_outputs.append((chapter, chapter_run))
        chapter_passes.append(chapter_run.passes_used)
        total_generation_cost += chapter_run.generation_cost
        total_uncached_cost += chapter_run.uncached_generation_cost

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
                chapter_judge_scores[chapter.chapter_id] = chapter_judge_result.scores

        chapter_row = {
            "chapter_id": chapter.chapter_id,
            "chapter_title": chapter.chapter_title,
            "target_words": target_words,
            "output_words": visible_word_count(chapter_run.summary_md),
            "passes_used": chapter_run.passes_used,
            "generation_cost": chapter_run.generation_cost,
            "uncached_generation_cost": chapter_run.uncached_generation_cost,
            "judge_faithfulness": chapter_judge_scores.get(chapter.chapter_id, {}).get("faithfulness"),
            "judge_concept_coverage": chapter_judge_scores.get(chapter.chapter_id, {}).get("concept_coverage"),
            "judge_qualifier_preservation": chapter_judge_scores.get(chapter.chapter_id, {}).get("qualifier_preservation"),
            "judge_no_fluff": chapter_judge_scores.get(chapter.chapter_id, {}).get("no_fluff"),
            "judge_structure_quality": chapter_judge_scores.get(chapter.chapter_id, {}).get("structure_quality"),
            "judge_rationale": chapter_judge_scores.get(chapter.chapter_id, ""),
        }
        if chapter_run.summary_md:
            rm = readability_metrics(chapter_run.summary_md)
            chapter_row["flesch_reading_ease"] = rm.flesch_reading_ease
            chapter_row["flesch_kincaid_grade"] = rm.flesch_kincaid_grade
            chapter_row["sentence_count"] = rm.sentence_count
        chapter_runs_rows.append(chapter_row)

        progress.clear()
        progress.update(base_progress)
        progress["phase"] = "chapters"
        progress["chapter_targets"] = list(chapter_targets)
        progress["chapter_outputs"] = serialize_completed_chapter_runs(chapter_outputs)
        progress["chapter_passes"] = list(chapter_passes)
        progress["total_generation_cost"] = total_generation_cost
        progress["total_uncached_cost"] = total_uncached_cost
        progress["chapter_runs_rows"] = chapter_runs_rows
        if spec.disable_composer and chapter_judge_scores:
            progress["chapter_judge_scores"] = {
                ch_id: judge_scores_to_dict(scores) for ch_id, scores in chapter_judge_scores.items()
            }
        progress["current_stage"] = None
        emit()

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
        "genre_macro_spread_utility\tn_genre_macros\trun_artifact\tcatalog_snapshot\tprice_snapshot\tnotes\n"
    )
    if not path.exists() or path.read_text(encoding="utf-8") == "":
        path.write_text(header, encoding="utf-8")

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
    args = parse_args()
    benchmark_manifest_path = resolve_path(args.benchmark_manifest)
    ensure_default_benchmark_manifest(benchmark_manifest_path)
    benchmark_manifest = load_benchmark_manifest(benchmark_manifest_path)

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
    spec = candidate_module.get_candidate(args.profile)
    client = make_client(args)

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

        prompt_hashes = build_prompt_hashes(candidate_module, spec)
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

    for index, item in enumerate(bench_rows):
        item_key = sample_key_from_item(item, bench_name=bench_name)
        if item_key in completed_set:
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
                        f"python core/run_candidate.py --bench {bench_name} --profile {spec.profile} --resume {run_id}"
                        + (f" --judge-model {args.judge_model}" if args.judge_model else "")
                        + (" --write-results" if args.write_results else "")
                    )
                    raise SystemExit(2)
                if not wait_for_credits(client=client, args=args, run_id=run_id):
                    print(f"Stopped waiting for credits. Resume later with --resume {run_id}.")
                    raise SystemExit(2)
                state["status"] = "running"
                state["latest_error"] = None
                save_run_state(state_path, state)
                continue
            except Exception as exc:
                state["status"] = "failed"
                state["latest_error"] = error_to_dict(exc)
                save_run_state(state_path, state)
                raise

    scoring_config = DEFAULT_SCORING_CONFIG
    profile_name = spec.profile
    if profile_name.startswith("30m_"):
        gate_key = "30m"
    elif profile_name.startswith("60m_"):
        gate_key = "60m"
    else:
        gate_key = "default"
    gates = benchmark_manifest.get("scoring_gates", {}).get(gate_key)
    if gates:
        scoring_config = apply_gates_override(
            scoring_config,
            min_faithfulness=gates.get("min_faithfulness"),
            min_concept_coverage=gates.get("min_concept_coverage"),
            max_final_length_error_pct=gates.get("max_final_length_error_pct"),
            max_passes=gates.get("max_passes"),
        )

    dataset_score = score_dataset(samples, config=scoring_config)
    mean_generation_cost = _mean([sample.generation_cost for sample in samples])
    run_manifest = dict(state.get("run_manifest") or run_manifest)
    resume_events = list(state.get("resume_events_utc") or [])
    run_manifest["resume_count"] = len(resume_events)
    if resume_events:
        run_manifest["resume_events_utc"] = resume_events

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
        "worst_genre_macro": worst_genre_macro.get("slice_value", ""),
        "worst_genre_macro_utility": float(worst_genre_macro.get("mean_utility", 0.0) or 0.0),
        "genre_macro_spread_utility": float(dataset_payload.get("genre_macro_spread_utility", 0.0) or 0.0),
        "n_genre_macros": int(dataset_payload.get("n_genre_macros", 0) or 0),
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

    state["status"] = "finished"
    state["current_item"] = None
    state["latest_error"] = None
    state["completed_count"] = len(completed_set)
    state["completed_sample_ids"] = list(state.get("completed_sample_ids") or [])
    state["run_manifest"] = run_manifest
    state["out_path"] = display_path(out_path)
    save_run_state(state_path, state)


if __name__ == "__main__":
    main()
