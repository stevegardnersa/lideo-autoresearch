# E2E Pipeline Test 3 — Full Run

**Spec ID:** `86d427nuq-e2e-pipeline-test-3-full-run`
**Status:** Draft
**Target system:** `autoresearch` — genre-aware nonfiction book summarization benchmark

---

## 1. Overview

This specification defines a **full end-to-end run** of the `autoresearch`
nonfiction book summarization benchmark pipeline. A "full run" means exercising
every frozen pipeline stage against the 18-book corpus, from rubric construction
through benchmark splits, candidate execution (chapter + composer stages),
judging, scoring, leaderboard generation, and results logging — using **real
OpenRouter generation and judging** (not `--mock`).

This is test number **3** of the E2E pipeline test series. Its purpose is to
verify that the complete pipeline executes without structural failure and
produces internally consistent, comparable artifacts across all three benchmark
splits for both product targets (30-minute and 60-minute summaries).

### Goals

1. Prove the pipeline runs end-to-end on the frozen `booksum-v4` benchmark.
2. Produce run artifacts, catalog/price snapshots, and `results.tsv` rows for
   `chapter_fast`, `book_gate`, and `book_holdout`.
3. Confirm scoring gates, genre-aware reporting, and leaderboards behave as
   specified.
4. Establish a clean baseline for subsequent candidate optimization work.

### Non-goals

- Optimizing prompt components or models (that is the autoresearch loop, not this test).
- Modifying any frozen file (see §3).
- Cached-cost wins — the harness must report **uncached** generation cost.

---

## 2. Scope

The full run covers the following stages, in order:

| # | Stage | Tool | Split / Output |
|---|-------|------|----------------|
| 0 | Preflight & environment | — | env vars, corpus presence |
| 1 | Rubric construction | `tools/build_rubrics.py` | `artifacts/rubrics/`, `artifacts/book_rubrics/` |
| 2 | Corpus report | `tools/corpus_report.py` | stdout genre coverage |
| 3 | Benchmark splits | `tools/build_bench.py` | `bench/chapter_fast.jsonl`, `book_gate.jsonl`, `book_holdout.jsonl`, `splits.json` |
| 4 | Smoke test gate | `core/run_candidate.py --mock` | `runs/mock/...` (no LLM cost) |
| 5 | Real runs — fast | `core/run_candidate.py` | `chapter_fast`, 30m + 60m |
| 6 | Real runs — gate | `core/run_candidate.py` | `book_gate`, 30m + 60m |
| 7 | Real runs — holdout | `core/run_candidate.py` | `book_holdout`, 30m + 60m |
| 8 | Leaderboards | `tools/leaderboard.py` | overall + per-genre slices |
| 9 | Results verification | — | `results.tsv` consistency check |

Each real run (stages 5–7) must record a **catalog snapshot** and a **price
snapshot** (see §8), and must use a judge model for full fidelity.

---

## 3. Frozen Contract & Preconditions

### 3.1 Frozen files (must not be modified by the run)

- `scoring.py` — deterministic metrics, hard gates, utility scoring
- `bench/` — all split JSONL files and `splits.json`
- `data/` — source markdown, `book.json` manifests, `candidates.json`
- `artifacts/rubrics/`, `artifacts/book_rubrics/` — source-derived rubrics
- judge prompts and judge model settings in the evaluator
- `benchmark_version.json` — benchmark manifest (`booksum-v4`)
- `results.tsv` — experiment log schema

The **only** editable file is `candidate_spec.py`. For this test it is left at
its committed baseline.

### 3.2 Preconditions

- 18-book corpus present under `data/books/` (16 core + 2 wildcard).
- `benchmark_version.json` reports `benchmark_version = "booksum-v4"`.
- `data/candidates.json` contains profiles for the models to be run.
- `candidate_spec.py` compiles and `get_candidate()` resolves every profile
  named in `Profile`.
- Required environment variables are set (§4).
- Python 3 environment with `requirements.txt` installed.

---

## 4. Environment, Credentials & Permissions

### 4.1 Required environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `OPENROUTER_API_KEY` | Yes (real runs) | OpenRouter generation for chapter + composer stages |
| `OPENROUTER_MANAGEMENT_KEY` | Optional | Credit polling when `--wait-for-credits` is used |
| `OPENROUTER_HTTP_REFERER` | Optional | `HTTP-Referer` header for OpenRouter |
| `OPENROUTER_APP_TITLE` | Optional | `X-Title` header (default `autoresearch-book-summary-benchmark`) |
| `GOOGLE_BOOKS_API_KEY` | Optional | Only for corpus bootstrapping, not the run itself |

Secret values must never be written into run artifacts, `results.tsv`, commit
messages, or this specification. The harness reads them from the environment at
runtime.

### 4.2 Permission model

- The pipeline has a single trust boundary: **network access to OpenRouter**.
- No multi-user roles; the local filesystem is trusted.
- The judge model is a **read-only** evaluation dependency — it never mutates
  candidate output, only scores it.

---

## 5. Pipeline Stages — Business Logic

### Stage 0 — Preflight

1. Confirm `benchmark_version.json` → `booksum-v4`.
2. Confirm `data/books/` contains exactly the 18 expected book directories.
3. Confirm `OPENROUTER_API_KEY` is present and non-empty.
4. Confirm `candidate_spec.py` imports without error.

**Abort the run** if any preflight check fails (see §10.1).

### Stage 1 — Rubric construction

```bash
python3 tools/build_rubrics.py --books-root data/books --artifacts-root artifacts
```

- Deterministic. Produces per-chapter rubrics and per-book rubric bundles.
- Must be re-run only if the corpus changed; otherwise it is idempotent.

### Stage 2 — Corpus report

```bash
python3 tools/corpus_report.py --books-root data/books
```

- Verifies genre coverage across the 4 macro-genres.
- Warning (not fatal) if a macro-genre is underrepresented.

### Stage 3 — Benchmark splits

```bash
python3 tools/build_bench.py \
  --books-root data/books \
  --bench-dir bench \
  --dev-books 10 --gate-books 4 --holdout-books 4 \
  --seed 42
```

- Genre-aware (`balanced_genre`) stratification across `genre_macro`.
- Respects `benchmark_pool`: `balanced` eligible for assignment, `dev_only`
  restricted to development, `exclude` ignored.
- `chapter_fast` samples 4 chapters per dev book (short, medium, long, dense).

### Stage 4 — Smoke test gate (mock, no API cost)

```bash
python3 core/run_candidate.py --bench chapter_fast --profile all --time 30m --mock --write-results
python3 core/run_candidate.py --bench chapter_fast --profile all --time 60m --mock --write-results
```

- Deterministic mock summarizer; no LLM calls.
- Proves pipeline logic (rubric lookup, split parsing, scoring, artifact
  writing) is intact before spending money.
- **Gate:** mock runs must complete with zero exceptions and produce run
  manifests under `runs/mock/`. If mock fails, do not proceed to real runs.

### Stage 5 — Real fast runs (`chapter_fast`)

```bash
python3 core/run_candidate.py --bench chapter_fast --profile all --time 30m \
  --judge-model openai/gpt-5-mini --write-results
python3 core/run_candidate.py --bench chapter_fast --profile all --time 60m \
  --judge-model openai/gpt-5-mini --write-results
```

### Stage 6 — Real gate runs (`book_gate`)

```bash
python3 core/run_candidate.py --bench book_gate --profile all --time 30m \
  --judge-model openai/gpt-5-mini --write-results
python3 core/run_candidate.py --bench book_gate --profile all --time 60m \
  --judge-model openai/gpt-5-mini --write-results
```

### Stage 7 — Real holdout runs (`book_holdout`)

```bash
python3 core/run_candidate.py --bench book_holdout --profile all --time 30m \
  --judge-model openai/gpt-5-mini --write-results
python3 core/run_candidate.py --bench book_holdout --profile all --time 60m \
  --judge-model openai/gpt-5-mini --write-results
```

### Stage 8 — Leaderboards

```bash
python3 tools/leaderboard.py --bench chapter_fast --profile 30m
python3 tools/leaderboard.py --bench chapter_fast --profile 60m
python3 tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field genre_macro
python3 tools/leaderboard.py --bench chapter_fast --profile 60m --slice-field genre_macro
```

### Stage 9 — Results verification

- Confirm every real run produced a `results.tsv` row with non-empty
  `run_artifact`, `catalog_snapshot`, and `price_snapshot` paths.
- Confirm `n_genre_macros >= 2` on fast runs (genre coverage sanity).

---

## 6. Validation Rules & Scoring Gates

### 6.1 Per-profile scoring gates

Resolved from `benchmark_version.json` → `scoring_gates`, applied per sample:

| Profile class | `min_faithfulness` | `min_concept_coverage` |
|---------------|--------------------|------------------------|
| `30m_*` | 0.50 | 0.15 |
| `60m_*` | 0.60 | 0.50 |
| default (bare) | 0.70 | 0.60 |

A sample is a **hard fail** if **any** of the following is true:

1. `length_outside_hard_tolerance` — final length exceeds hard tolerance after
   the allowed repair passes.
2. `resolved_faithfulness < gates.min_faithfulness`.
3. `resolved_concept_coverage < gates.min_concept_coverage`.
4. Malformed output (empty, truncated, unparseable JSON).

### 6.2 Length control rules

- `max_passes` default 5 (repair passes after first attempt).
- `tolerance_pct` (soft) and `hard_tolerance_pct` (hard) govern length error.
- `repair_strategy` ∈ {`edit_existing`, `regenerate_from_source`}.
- The evaluator records first-pass length error, final length error, passes
  used, and **total uncached generation cost across all passes**.

### 6.3 Composite score

```
composite = 0.35 × faithfulness + 0.25 × quality + 0.25 × concept_coverage + 0.15 × pass_rate
```

`utility` is the frozen primary metric for leaderboard comparisons and
promotion decisions.

### 6.4 Acceptance gates (candidate rejection on fast split)

A candidate must be rejected immediately if, on `chapter_fast`:

- malformed output occurs;
- final length remains outside hard tolerance after allowed repair passes;
- faithfulness below the evaluator minimum;
- catastrophic cost increase with no quality benefit;
- severe degradation in one core macro-genre.

### 6.5 Promotion rules

A candidate is eligible for `book_gate` only if:

- fast-benchmark utility beats the incumbent;
- hard-fail count is zero;
- the gain is not caused only by cached requests or judge variance;
- worst-genre utility does not fall below the current minimum acceptable floor.

Finalists advance to `book_holdout` (unseen books) before any production claim.

---

## 7. State Flows

### 7.1 Run lifecycle

```
preflight → rubrics → splits → mock-gate → [chapter_fast → book_gate → book_holdout] → leaderboard → verify
```

Each real run transitions through: `loading` (sample generation) →
`scoring` → `judging` → `writing artifacts` → `done`. A run interrupted
mid-flight may be resumed with `--resume <run-id>` using its checkpoint/state
files.

### 7.2 Loading / empty / error states

| State | Trigger | Required behavior |
|-------|---------|-------------------|
| **Loading** | API call in flight | No partial results written; progress via stdout |
| **Empty corpus** | `data/books/` missing/empty | Stage 0 aborts with clear message |
| **Empty split** | `bench/*.jsonl` empty or missing | `run_candidate` errors out before any API call |
| **Missing rubric** | sample can't locate rubric in `data_dir` | Judging fails for that sample; error recorded, not silently skipped |
| **Network error** | OpenRouter timeout / 5xx | Retry with backoff; sample marked failed after retry budget |
| **HTTP 402** | Insufficient credits | If `--wait-for-credits`: poll and retry same sample until credits return or timeout; otherwise fail fast |
| **Auth error (401/403)** | Bad/missing API key | Abort run; do not retry indefinitely |
| **Judge unavailable** | Judge model call fails | If judge omitted → fall back to deterministic proxies (judge fields absent); if judge configured but fails → run fails for that sample |

### 7.3 Offline behavior

- `--mock` mode must work fully offline (no network).
- Real runs require network; there is no offline fallback for real generation.
- Catalog and price snapshots must still be written for real runs even if the
  judge is omitted.

---

## 8. API Contracts

### 8.1 OpenRouter generation (chapter + composer stages)

- Endpoint: OpenRouter chat completions (provider-routed via `OPENROUTER_API_KEY`).
- Headers: `Authorization: Bearer <key>`, `HTTP-Referer`, `X-Title`.
- One model per stage per experiment; no model arrays or fallbacks during
  evaluation (per `program.md` rules).
- Prefer structured JSON output; `use_json_schema` with schema
  `summary_response` requires `summary_md` (string) and
  `estimated_visible_words` (integer ≥ 0), `additionalProperties: false`.
- Cost accounting is on **uncached** generation cost.

### 8.2 Judge contract

- `--judge-model openai/gpt-5-mini` (or equivalent) triggers LLM judging.
- Judge output fields per sample:
  - `judge_no_fluff`
  - `judge_structure_quality`
  - `judge_concept_coverage`
  - `judge_concept_coverage_faithfulness`
  - `judge_overall_quality`
- Without `--judge-model`, scoring uses deterministic proxies only; all judge
  fields are absent from results.

### 8.3 Snapshot contract

Every real run records:
- `catalog_snapshot` — model catalog at run time (for future comparisons).
- `price_snapshot` — pricing at run time (for cost re-computation).

---

## 9. Data Models & Entity Relationships

### 9.1 Candidate model (`candidate_spec.py`)

```
Profile            = Literal[70 profile names]   # 30m_*/60m_* × model × thinking
StageConfig        = model, temperature, seed, max_tokens, format_mode,
                     context_mode, prompt_components, provider,
                     use_json_schema, extra_body
LengthControlConfig= max_passes, tolerance_pct, hard_tolerance_pct,
                     repair_strategy, repair_more_prompt_id, repair_less_prompt_id
BudgetAllocatorConfig = words_per_minute, allocation_alpha, min_chapter_share,
                     max_chapter_share, chapter_stage_multiplier_30m/60m,
                     max_summary_to_source_ratio
CandidateSpec      = name, profile, chapter_stage, composer_stage, composer_mode,
                     length_control, budget_allocator, use_json_schema,
                     json_schema_name, notes, disable_composer
```

`get_candidate(profile)` resolves v2+ overrides from permutation history
(`autoresearch.permutation_store`) before falling back to `PROFILE_CANDIDATES`;
unknown profiles raise `KeyError`.

### 9.2 Scoring model (`scoring.py`)

```
SampleScore  = sample_id, group_id, level, hard_fail, hard_fail_reasons,
               deterministic, resolved_faithfulness, resolved_concept_coverage,
               resolved_qualifier_preservation, resolved_no_fluff,
               resolved_structure_quality, quality, utility
DatasetScore = n_samples, hard_fail_rate, mean_quality, mean_utility,
               mean_faithfulness, mean_concept_coverage,
               mean_final_length_error_pct, mean_first_pass_length_error_pct,
               mean_passes_used, mean_uncached_cost,
               by_group_quality, by_group_utility, sample_scores
```

### 9.3 `results.tsv` row schema (frozen)

`timestamp, run_id, benchmark_version, corpus_version, rubric_version,
scoring_version, judge_version, profile, bench, candidate_name,
candidate_sha256, hypothesis, chapter_model, composer_model, judge_model,
use_json_schema, thinking, mean_quality, mean_utility, mean_faithfulness,
mean_concept_coverage, mean_final_length_error_pct,
mean_first_pass_length_error_pct, mean_passes_used,
mean_uncached_generation_cost, mean_generation_cost, hard_fail_rate,
worst_genre_macro, worst_genre_macro_utility, worst_genre_macro_quality,
genre_macro_spread_utility, n_genre_macros, run_artifact, catalog_snapshot,
price_snapshot, notes`

### 9.4 Entity relationships

- **Book** (1) → **Chapter** (N): `book.json` `chapters[]` with source paths.
- **Book** (1) → **genre_macro** (1): stratification bucket.
- **Chapter** (1) → **rubric** (1): `artifacts/rubrics/<book_id>/<chapter_id>.json`.
- **Benchmark split** (1) → **samples** (N): JSONL records referencing books/chapters.
- **Candidate** (1) → **run** (N): one run per candidate × split × budget.
- **Run** (1) → **samples** (N) → **SampleScore** (N).
- **Run** (1) → **catalog_snapshot / price_snapshot** (0..1 each).
- **Run** (1) → **results.tsv row** (1).

---

## 10. Edge Cases & Failure Handling

### 10.1 Abort conditions (fail fast)

- Missing/invalid `OPENROUTER_API_KEY` on a real run.
- Missing corpus or missing benchmark splits.
- `benchmark_version` mismatch with frozen expectations.
- `candidate_spec.py` import failure.

### 10.2 Network & retry

- Transient 5xx/timeouts: retry with exponential backoff, bounded by a retry budget.
- HTTP 402: honor `--wait-for-credits` (poll `OPENROUTER_MANAGEMENT_KEY`) or fail.
- HTTP 401/403: abort immediately (key invalid).

### 10.3 Empty / degenerate data

- A book with zero chapters must be excluded by split builders, not crash.
- A sample with no rubric must fail explicitly (recorded), never produce a
  fabricated score.
- A split with zero samples aborts before any API spend.

### 10.4 Concurrent edits

- `results.tsv` and run artifacts are **append-only** during a run; the harness
  must serialize writes to avoid interleaved rows when multiple runs share the
  same `--results-tsv`.
- The frozen `candidate_spec.py` is read-only for the optimizer (temp-file
  evaluation); concurrent optimization must not mutate the committed baseline.

### 10.5 Cost safety

- The harness optimizes **uncached** cost; cached wins are not counted as
  quality-per-cost improvements.
- A run that exceeds a catastrophic cost threshold with no quality gain must be
  flagged in `notes`.

---

## 11. Performance Targets

| Metric | Target |
|--------|--------|
| Mock smoke run (full profile sweep) | completes in < 5 minutes |
| `chapter_fast` real run (40 samples, single profile) | completes within one API budget session |
| Repair passes per sample | ≤ `max_passes` (default 5) |
| Hard-fail rate on fast split (baseline profiles) | 0.00 (no structural failures) |
| Judge round-trip | adds LLM cost only; deterministic fallback stays free |
| Artifact integrity | every real run writes manifest + samples JSONL + catalog + price snapshot |

These are observability targets, not correctness gates — a slower run is
acceptable if it is not caused by a pipeline defect.

---

## 12. Accessibility (Run Explorer Dashboard)

The run explorer (`dashboard/`) is the human review surface for the artifacts
this run produces. Accessibility requirements:

- Color is never the sole indicator of score quality (pass/fail icons + text
  labels accompany color coding).
- All interactive controls (dropdowns, tag chips, tabs, collapse/expand)
  are keyboard-operable and reachable via tab order.
- Score tables expose row/column semantics to assistive technology.
- Text contrast meets WCAG AA for score labels and note text.
- Notes panel is operable with keyboard; submitting a note has a visible
  success/error state.

---

## 13. Test Acceptance Criteria (PASS/FAIL for this E2E test)

The E2E Pipeline Test 3 is **PASS** when **all** of the following hold:

1. Stages 0–9 complete with exit code 0 and no unhandled exceptions.
2. `artifacts/rubrics/` and `artifacts/book_rubrics/` are populated.
3. `bench/chapter_fast.jsonl`, `bench/book_gate.jsonl`,
   `bench/book_holdout.jsonl`, and `bench/splits.json` exist.
4. Mock runs (stage 4) produce manifests under `runs/mock/`.
5. Each real run (stages 5–7) writes a run manifest, `samples.jsonl`,
   `catalog_snapshot`, and `price_snapshot`.
6. `results.tsv` gains one row per real run with non-empty `run_artifact`,
   `catalog_snapshot`, and `price_snapshot` paths, and `n_genre_macros >= 2`
   on fast runs.
7. Leaderboards render for 30m and 60m, overall and `genre_macro` slice.
8. No frozen file is modified during the run.

Any single failure above is a **FAIL** and must be reported with the failing
stage, the exact command, and the captured error output.

---

## 14. Out of Scope

- Prompt/model optimization (the autoresearch search loop).
- Corpus expansion or re-bootstrapping (corpus is assumed present).
- Dashboard feature development.
- Re-judging historical runs (`core/judge_existing.py`) — not part of this test.
- Benchmark versioning changes (remains `booksum-v4`).
