#!/usr/bin/env python3
"""Batch chapter summarization via Cloud Function or direct OpenAI-compatible API.

Reads a bench JSONL, renders prompts from ``candidate_spec.py``, sends each
chapter to the Cloud Function (or directly to any OpenAI-compatible provider), collects results,
scores them, and writes run artifacts (state.json + samples.jsonl).

This is the *first pass only* — no length-controlled repair. The CF handles
deterministic scoring and optional LLM judging server-side.

Usage
-----
  # Via Cloud Function (recommended for production)
  uv run python tools/batch_summarize.py \\
    --bench chapter_fast \\
    --profile 30m_deepseek-v4-flash_notthinking \\
    --function-url https://us-central1-PROJECT.cloudfunctions.net/summarize

  # Direct API call (OpenRouter, OpenAI, or any OpenAI-compatible provider)
  uv run python tools/batch_summarize.py \\
    --bench chapter_fast \\
    --profile 30m_deepseek-v4-flash_notthinking \\
    --base-url https://openrouter.ai/api/v1 \\
    --api-key-env LLM_API_KEY

  # Custom concurrency and timeout
  uv run python tools/batch_summarize.py \\
    --bench chapter_fast \\
    --profile 30m_deepseek-v4-flash_thinking \\
    --function-url $FUNC_URL \\
    --concurrency 8 \\
    --function-timeout 600
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from candidate_spec import CandidateSpec, get_candidate, render_chapter_system, render_chapter_user
from core.openrouter_client import OpenRouterClient
from scoring import (
    DEFAULT_SCORING_CONFIG,
    Rubric,
    SummarySample,
    score_sample,
    score_dataset,
    visible_word_count,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"Bench file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _parse_rubric(raw: Optional[Dict[str, Any]]) -> Optional[Rubric]:
    if not raw:
        return None
    keys = [
        "headings", "core_concepts", "mechanisms_or_explanations",
        "critical_qualifiers", "important_examples",
        "key_entities_or_numbers", "key_terms",
    ]
    return Rubric(**{k: tuple(raw.get(k, ()) or ()) for k in keys})


def load_rubric(path: Path) -> Rubric:
    if not path.exists():
        return Rubric()
    return _parse_rubric(load_json(path)) or Rubric()


def load_book_data(
    book_id: str,
    data_dir: Path,
    chapter_id: str,
) -> Tuple[str, Optional[Rubric]]:
    book_dir = data_dir / book_id
    manifest_path = book_dir / "book.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing book manifest for {book_id}: {manifest_path}")
    manifest = load_json(manifest_path)
    book_title = str(manifest.get("book_title") or manifest.get("title") or book_id)
    toc_path = book_dir / str(manifest.get("toc_path", ""))
    meta_path = book_dir / str(manifest.get("metadata_path", ""))
    toc_md = toc_path.read_text(encoding="utf-8") if toc_path.exists() and str(toc_path) != "." else ""
    metadata_md = meta_path.read_text(encoding="utf-8") if meta_path.exists() and str(meta_path) != "." else ""

    chapter_manifest = None
    for ch in manifest.get("chapters") or []:
        if str(ch.get("chapter_id") or "") == chapter_id:
            chapter_manifest = ch
            break
    if chapter_manifest is None:
        raise KeyError(f"Chapter {chapter_id} not found in book {book_id} manifest")

    source_path = book_dir / str(chapter_manifest["source_path"])
    source_md = source_path.read_text(encoding="utf-8")
    chapter_title = str(chapter_manifest.get("title", chapter_id))

    rubric_path = ROOT / "artifacts" / "rubrics" / book_id / f"{chapter_id}.json"
    rubric = load_rubric(rubric_path)

    return source_md, rubric, toc_md, metadata_md, book_title, chapter_title


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", required=True, help="Benchmark name (chapter_fast) or path to JSONL")
    parser.add_argument("--profile", required=True, help="Profile name from candidate_spec.py")
    parser.add_argument("--spec", default=str(ROOT / "candidate_spec.py"))
    parser.add_argument("--function-url", default="", help="Cloud Function URL (omit for direct API call)")
    parser.add_argument("--api-key", default="", help="API key (omit to use --api-key-env)")
    parser.add_argument("--api-key-env", default="LLM_API_KEY", help="Env var for API key (direct mode only)")
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1", help="OpenAI-compatible API base URL")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "books"))
    parser.add_argument("--runs-dir", default=str(ROOT / "runs" / "batch"))
    parser.add_argument("--concurrency", type=int, default=4, help="Max parallel HTTP requests")
    parser.add_argument("--function-timeout", type=int, default=600, help="HTTP client timeout in seconds")
    parser.add_argument("--judge", action="store_true", help="Request LLM judge scores from CF")
    parser.add_argument("--judge-model", default="openai/gpt-4o-mini", help="Judge model (CF mode)")
    parser.add_argument("--max-samples", type=int, default=0, help="Limit samples processed")
    parser.add_argument("--resume", default="", help="Resume a previous run ID")
    parser.add_argument("--run-id", default="", help="Explicit run ID for state management")
    return parser.parse_args()


def _render_prompt(
    spec: CandidateSpec,
    source_md: str,
    target_words: int,
    book_title: str,
    chapter_title: str,
    toc_md: str,
    metadata_md: str,
) -> Tuple[str, str]:
    system = render_chapter_system(spec)
    user = render_chapter_user(
        spec,
        source_md=source_md,
        target_words=target_words,
        book_title=book_title,
        chapter_title=chapter_title,
        toc_md=toc_md,
        book_metadata=metadata_md,
    )
    return system, user


def _cf_payload(
    source_md: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    target_words: int,
    thinking: bool,
    use_json: bool,
    judge: bool,
    judge_model: str,
    rubric: Optional[Rubric],
    api_key: str = "",
    base_url: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "source_md": source_md,
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "target_words": target_words,
        "thinking": thinking,
        "use_json_schema": use_json,
        "judge": judge,
        "judge_model": judge_model,
    }
    if api_key:
        payload["api_key"] = api_key
    if base_url and base_url != "https://openrouter.ai/api/v1":
        payload["base_url"] = base_url
    if rubric is not None:
        payload["rubric"] = asdict(rubric)
    return payload


def _parse_cf_response(
    item: Dict[str, Any],
    resp_json: Dict[str, Any],
    rubric: Optional[Rubric],
) -> SummarySample:
    summary_data = resp_json.get("summary", {})
    summary_md = str(summary_data.get("summary_md", "") or "")
    estimated_words = int(summary_data.get("estimated_visible_words", 0) or 0)
    usage = resp_json.get("usage", {})
    generation_cost = float(usage.get("generation_cost", 0.0) or 0.0)
    uncached_cost = float(usage.get("uncached_generation_cost", 0.0) or 0.0)
    generation_id = str(usage.get("generation_id", ""))

    if not estimated_words:
        estimated_words = visible_word_count(summary_md)

    judge_raw = resp_json.get("judge_scores")
    judge_scores = None
    if isinstance(judge_raw, dict) and judge_raw.get("faithfulness") is not None:
        from scoring import JudgeScores
        judge_scores = JudgeScores(
            faithfulness=float(judge_raw.get("faithfulness", 0.0)),
            concept_coverage=float(judge_raw.get("concept_coverage", 0.0)),
            qualifier_preservation=float(judge_raw.get("qualifier_preservation", 0.0)),
            no_fluff=float(judge_raw.get("no_fluff", 0.0)),
            structure_quality=float(judge_raw.get("structure_quality", 0.0)),
        )

    sample_id = str(item.get("sample_id", ""))
    book_id = str(item.get("book_id", ""))
    source_md = str(item.get("source_md", ""))
    target_words = int(item.get("target_words", 0))

    sample = SummarySample(
        sample_id=sample_id,
        level="chapter",
        target_words=target_words,
        summary_md=summary_md,
        source_md=source_md,
        group_id=book_id,
        first_pass_summary_md=summary_md,
        passes_used=1,
        generation_cost=generation_cost,
        uncached_generation_cost=uncached_cost,
        malformed=not bool(summary_md.strip()),
        rubric=rubric,
        judge_scores=judge_scores,
    )
    return sample


def _build_trace(
    item: Dict[str, Any],
    sample: SummarySample,
    resp_json: Dict[str, Any],
) -> Dict[str, Any]:
    usage = resp_json.get("usage", {})
    return {
        "sample_id": sample.sample_id,
        "book_id": str(item.get("book_id", "")),
        "chapter_id": str(item.get("chapter_id", "")),
        "target_words": sample.target_words,
        "output_words": visible_word_count(sample.summary_md),
        "passes_used": 1,
        "generation_cost": sample.generation_cost,
        "uncached_generation_cost": sample.uncached_generation_cost,
        "generation_id": str(usage.get("generation_id", "")),
        "provider_name": str(usage.get("provider_name", "")),
        "model_id": str(usage.get("model_id", "")),
        "error": resp_json.get("error", ""),
    }


def _build_sample_record(sample: SummarySample, trace: Dict[str, Any], item_key: str) -> Dict[str, Any]:
    from core.run_candidate import judge_scores_to_dict
    return {
        "item_key": item_key,
        "sample_id": sample.sample_id,
        "level": "chapter",
        "group_id": sample.group_id,
        "target_words": int(sample.target_words),
        "summary_md": sample.summary_md,
        "first_pass_summary_md": sample.first_pass_summary_md,
        "passes_used": int(sample.passes_used),
        "generation_cost": float(sample.generation_cost),
        "uncached_generation_cost": float(sample.uncached_generation_cost),
        "malformed": bool(sample.malformed),
        "judge_scores": judge_scores_to_dict(sample.judge_scores),
        "book_id": str(trace.get("book_id", "")),
        "chapter_id": str(trace.get("chapter_id", "")),
        "trace": _json_safe(trace),
    }


async def _call_cf(
    client: httpx.AsyncClient,
    url: str,
    payload: Dict[str, Any],
    timeout: int,
) -> Dict[str, Any]:
    resp = await client.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_item_key(item: Dict[str, Any]) -> str:
    return str(item.get("sample_id", "") or f"{item['book_id']}:{item['chapter_id']}")


async def run_batch(args: argparse.Namespace) -> None:
    data_dir = resolve_path(args.data_dir)
    runs_dir = resolve_path(args.runs_dir)
    spec_path = resolve_path(args.spec)

    bench_name_or_path = args.bench
    if str(bench_name_or_path).endswith(".jsonl"):
        bench_path = resolve_path(bench_name_or_path)
        bench_name = Path(bench_name_or_path).stem
    else:
        bench_name = bench_name_or_path
        bench_path = ROOT / "bench" / f"{bench_name}.jsonl"

    bench_rows = load_jsonl(bench_path)
    if args.max_samples > 0:
        bench_rows = bench_rows[: args.max_samples]

    _spec_loader = importlib.util.spec_from_file_location("candidate_spec_runtime", spec_path)
    if _spec_loader is None or _spec_loader.loader is None:
        raise ImportError(f"Could not load {spec_path}")
    candidate_module = importlib.util.module_from_spec(_spec_loader)
    sys.modules["candidate_spec_runtime"] = candidate_module
    _spec_loader.loader.exec_module(candidate_module)

    spec: CandidateSpec = candidate_module.get_candidate(args.profile)

    run_id = args.resume.strip() or args.run_id.strip() or f"batch_{bench_name}_{spec.profile}_{utc_now_ts()}"
    state_path = runs_dir / f"{run_id}.state.json"
    samples_path = runs_dir / f"{run_id}.samples.jsonl"
    out_path = runs_dir / f"{run_id}.json"

    completed_item_keys: set = set()
    if args.resume:
        if not state_path.exists():
            raise FileNotFoundError(f"Resume state not found: {state_path}")
        state = load_json(state_path)
        completed_from_samples: set = set()
        if samples_path.exists():
            for line in samples_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                ik = str(record.get("item_key", "") or record.get("sample_id", ""))
                if ik:
                    completed_from_samples.add(ik)
        completed_item_keys = completed_from_samples
        resume_events = list(state.get("resume_events_utc", []))
        resume_events.append(utc_now_iso())
        state["resume_events_utc"] = resume_events
        state["status"] = "running"
        state["latest_error"] = None
        save_json(state_path, state)
        print(f"Resumed run: {run_id} ({len(completed_item_keys)} already completed)")
    else:
        state = {
            "run_id": run_id,
            "created_at_utc": utc_now_iso(),
            "status": "running",
            "profile": spec.profile,
            "bench": bench_name,
            "model": spec.chapter_stage.model,
            "thinking": spec.chapter_stage.extra_body.get("thinking", {}).get("type", "disabled") == "enabled"
            if spec.chapter_stage.extra_body else False,
            "use_json_schema": spec.use_json_schema,
            "function_url": args.function_url or "(direct API)",
            "base_url": args.base_url,
            "judge": args.judge,
            "judge_model": args.judge_model,
            "n_total_samples": len(bench_rows),
            "completed_count": 0,
            "completed_item_keys": [],
            "latest_error": None,
            "resume_events_utc": [],
            "state_path": str(state_path),
            "samples_path": str(samples_path),
            "out_path": str(out_path),
        }
        save_json(state_path, state)
        print(f"Run ID: {run_id}")
        print(f"  Items: {len(bench_rows)} | Concurrency: {args.concurrency}")

    items = [item for item in bench_rows if _extract_item_key(item) not in completed_item_keys]
    if not items:
        print("All items already completed. Skipping.")
    else:
        print(f"  Pending: {len(items)}")

    client: Optional[OpenRouterClient] = None
    if not args.function_url:
        resolved_base = args.base_url or "https://openrouter.ai/api/v1"
        if args.api_key:
            client = OpenRouterClient(
                api_key=args.api_key,
                base_url=resolved_base,
                timeout=args.function_timeout,
            )
        else:
            client = OpenRouterClient.from_env(
                api_key_env=args.api_key_env,
                base_url=resolved_base,
                timeout=args.function_timeout,
            )

    samples: List[SummarySample] = []
    traces: List[Dict[str, Any]] = []

    if completed_item_keys and samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            ik = str(record.get("item_key", "") or record.get("sample_id", ""))
            if ik in completed_item_keys:
                from core.run_candidate import deserialize_sample_record
                sample, trace, _ = deserialize_sample_record(record, data_dir)
                samples.append(sample)
                traces.append(dict(trace))

    semaphore = asyncio.Semaphore(args.concurrency)

    async def process_one(item: Dict[str, Any], index: int) -> Tuple[Optional[SummarySample], Optional[Dict[str, Any]]]:
        async with semaphore:
            item_key = _extract_item_key(item)
            try:
                book_id = str(item["book_id"])
                chapter_id = str(item["chapter_id"])
                source_md, rubric, toc_md, metadata_md, book_title, chapter_title = load_book_data(
                    book_id, data_dir, chapter_id,
                )
                source_words = visible_word_count(source_md)
                profile_prefix = spec.profile[:3]
                minutes = 30 if profile_prefix == "30m" else 60
                wpm = spec.budget_allocator.words_per_minute
                multiplier = (
                    spec.budget_allocator.chapter_stage_multiplier_30m
                    if profile_prefix == "30m"
                    else spec.budget_allocator.chapter_stage_multiplier_60m
                )
                total_stage_budget = minutes * wpm * multiplier
                est_book_words = max(source_words, total_stage_budget * 3)
                target_words = max(
                    100,
                    min(
                        int(total_stage_budget * source_words / est_book_words),
                        int(source_words * spec.budget_allocator.max_summary_to_source_ratio),
                    ),
                )

                system_prompt, user_prompt = _render_prompt(
                    spec, source_md, target_words, book_title, chapter_title, toc_md, metadata_md,
                )

                if args.function_url:
                    payload = _cf_payload(
                        source_md=source_md,
                        model=spec.chapter_stage.model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        target_words=target_words,
                        thinking=spec.chapter_stage.extra_body.get("thinking", {}).get("type", "enabled") == "enabled"
                        if spec.chapter_stage.extra_body else False,
                        use_json=spec.use_json_schema,
                        judge=args.judge,
                        judge_model=args.judge_model,
                        rubric=rubric,
                        api_key=args.api_key,
                        base_url=args.base_url,
                    )
                    async with httpx.AsyncClient() as hc:
                        resp_json = await _call_cf(hc, args.function_url, payload, args.function_timeout)
                else:
                    or_payload = _build_chat_payload(
                        model=spec.chapter_stage.model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        use_json_schema=spec.use_json_schema,
                        thinking=spec.chapter_stage.extra_body.get("thinking", {}).get("type", "enabled") == "enabled"
                        if spec.chapter_stage.extra_body else False,
                    )
                    gen = client.chat_completion(or_payload) if client else _mock_gen(source_md, target_words)
                    resp_json = {
                        "success": True,
                        "summary": {"summary_md": gen.summary_md, "estimated_visible_words": gen.estimated_visible_words or visible_word_count(gen.summary_md)},
                        "usage": {
                            "prompt_tokens": gen.usage.prompt_tokens,
                            "completion_tokens": gen.usage.completion_tokens,
                            "total_tokens": gen.usage.total_tokens,
                            "reasoning_tokens": gen.usage.reasoning_tokens,
                            "generation_cost": gen.usage.generation_cost,
                            "uncached_generation_cost": gen.usage.uncached_generation_cost,
                            "generation_id": gen.usage.generation_id,
                            "provider_name": gen.usage.provider_name,
                            "model_id": gen.usage.model_id,
                        },
                    }

                if not resp_json.get("success", True):
                    raise RuntimeError(f"CF/API error: {resp_json.get('error', 'unknown')}")

                sample = _parse_cf_response(item, resp_json, rubric)
                trace = _build_trace(item, sample, resp_json)
                return sample, trace

            except Exception as exc:
                print(f"[{index + 1}/{len(bench_rows)}] FAIL {item_key}: {exc}", flush=True)
                return None, None

    if client and not args.function_url:
        print(f"Direct API: {args.base_url}")
    elif args.function_url:
        print(f"Connecting to Cloud Function: {args.function_url}")

    t0 = time.time()
    tasks = [process_one(item, i) for i, item in enumerate(items)]
    batch_size = args.concurrency * 2
    for chunk_start in range(0, len(tasks), batch_size):
        chunk = tasks[chunk_start: chunk_start + batch_size]
        results = await asyncio.gather(*chunk)
        for item, (sample, trace) in zip(items[chunk_start: chunk_start + batch_size], results):
            item_key = _extract_item_key(item)
            if sample is None or trace is None:
                continue

            samples.append(sample)
            traces.append(trace)
            completed_item_keys.add(item_key)

            record = _build_sample_record(sample, trace, item_key)
            record["run_id"] = run_id
            record["sample_index"] = len(samples) - 1
            samples_path.parent.mkdir(parents=True, exist_ok=True)
            with samples_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        state["completed_count"] = len(completed_item_keys)
        state["completed_item_keys"] = list(completed_item_keys)
        state["latest_error"] = None
        save_json(state_path, state)

        elapsed = time.time() - t0
        rate = len(completed_item_keys) / elapsed if elapsed > 0 else 0
        print(
            f"Progress: {len(completed_item_keys)}/{len(bench_rows)} "
            f"({rate:.1f} items/s)"
        )

    elapsed = time.time() - t0
    print(f"\nCompleted {len(completed_item_keys)} items in {elapsed:.0f}s")

    if samples:
        print("Scoring...")
        scoring_config = DEFAULT_SCORING_CONFIG
        dataset_score = score_dataset(samples, config=scoring_config)
        mean_cost = sum(s.uncached_generation_cost for s in samples) / len(samples)

        summary_block = {
            "run_id": run_id,
            "profile": spec.profile,
            "bench": bench_name,
            "n_samples": dataset_score.n_samples,
            "hard_fail_rate": dataset_score.hard_fail_rate,
            "mean_quality": dataset_score.mean_quality,
            "mean_utility": dataset_score.mean_utility,
            "mean_faithfulness": dataset_score.mean_faithfulness,
            "mean_concept_coverage": dataset_score.mean_concept_coverage,
            "mean_final_length_error_pct": dataset_score.mean_final_length_error_pct,
            "mean_passes_used": dataset_score.mean_passes_used,
            "mean_uncached_cost": dataset_score.mean_uncached_cost,
            "mean_generation_cost": mean_cost,
        }
        print(json.dumps(summary_block, ensure_ascii=False, indent=2))

        save_json(out_path, {
            "run_id": run_id,
            "summary": summary_block,
            "samples": [
                {
                    "sample_id": s.sample_id,
                    "target_words": s.target_words,
                    "summary_visible_words": visible_word_count(s.summary_md),
                    "passes_used": s.passes_used,
                    "generation_cost": s.generation_cost,
                    "uncached_generation_cost": s.uncached_generation_cost,
                }
                for s in samples
            ],
            "traces": _json_safe(traces),
        })
        print(f"Wrote: {out_path}")

    state["status"] = "finished"
    state["completed_count"] = len(completed_item_keys)
    state["completed_item_keys"] = list(completed_item_keys)
    save_json(state_path, state)
    print(f"State: {state_path}")


def _build_chat_payload(
    model: str,
    system_prompt: str,
    user_prompt: str,
    use_json_schema: bool = True,
    thinking: bool = False,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8192,
        "seed": 42,
        "extra_body": {
            "thinking": {"type": "enabled" if thinking else "disabled"},
        },
    }
    if use_json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "summary_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "summary_md": {"type": "string"},
                        "estimated_visible_words": {
                            "type": "integer",
                            "minimum": 0,
                        },
                    },
                    "required": ["summary_md", "estimated_visible_words"],
                    "additionalProperties": False,
                },
            },
        }
        json_hint = "Respond using JSON format exactly matching the provided schema.\n\n"
        msgs = payload["messages"]
        if msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] = json_hint + msgs[0]["content"]
    return payload


def _mock_gen(source_md: str, target_words: int) -> Any:
    from core.run_candidate import extractive_mock_summary
    from core.openrouter_client import GenerationResult, UsageRecord
    summary = extractive_mock_summary(source_md, target_words=target_words)
    return GenerationResult(
        summary_md=summary,
        estimated_visible_words=visible_word_count(summary),
        raw_content=summary,
        usage=UsageRecord(),
        raw_response={"mock": True},
    )


def main() -> None:
    args = parse_args()
    if args.function_url and httpx is None:
        print("Error: httpx is required for Cloud Function mode. Install: pip install httpx")
        sys.exit(1)
    asyncio.run(run_batch(args))


if __name__ == "__main__":
    main()
