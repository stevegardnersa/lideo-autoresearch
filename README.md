# Autoresearch scaffold for nonfiction book summarization

This scaffold gives you a fixed benchmark harness plus one editable candidate file for optimizing a chapter-by-chapter nonfiction summarization pipeline.

It is now **genre-aware** in two ways:
- the corpus manifest supports coarse and fine nonfiction genre metadata
- run artifacts and the leaderboard report **overall**, **per-genre**, and **worst-genre** performance

That lets you start with a strong **general nonfiction system** and later add genre-specific optimizations without losing a reliable fallback.

## Frozen files

- `program.md` — research brief for the autoresearch loop
- `scoring.py` — deterministic metrics, hard gates, and utility scoring
- `results.tsv` — experiment log
- `core/run_candidate.py` — frozen runtime harness
- `core/judge.py` — frozen rubric-based LLM judge helpers
- `tools/build_rubrics.py` — deterministic rubric builder
- `tools/build_bench.py` — benchmark split builder

## Editable file

- `candidate_spec.py` — the only file autoresearch should edit

## Recommended starting corpus

Use **18 books** in the first real benchmark:
- **16 core books** across **4 macro-genres** with **4 books per genre**
- **2 wildcard books** kept in development only as broader stress tests

Recommended split:
- **10 development books**
- **4 gate books**
- **4 holdout books**

Recommended corpus pattern:
- each core macro-genre contributes **2 dev, 1 gate, 1 holdout**
- wildcard books are marked `benchmark_pool: "dev_only"`

Suggested macro-genres:
- `explanatory_science_technology_environment`
- `business_economics_productivity`
- `psychology_health_self_development`
- `history_biography_politics_social_analysis`

## Expected corpus layout

A small sample corpus is included under `data/books/` so the scaffold is runnable immediately. Rebuild rubrics and benchmarks after you replace it with your real books.

Both chapter layouts below are supported:

```text
data/books/<book_id>/
  book.json
  toc.md                 # optional
  toc.json               # optional normalized TOC sidecar
  metadata.md            # optional
  0.md
  1.md
  2.md
```

or

```text
data/books/<book_id>/
  book.json
  toc.md                 # optional
  toc.json               # optional normalized TOC sidecar
  metadata.md            # optional
  chapters/
    01-intro.md
    02-core-idea.md
```

Example `book.json`:

```json
{
  "book_id": "example-book",
  "book_title": "Example Book",
  "genre_macro": "business_economics_productivity",
  "genre_micro": "decision_making",
  "narrative_vs_expository": "unknown",
  "prescriptive_vs_analytical": "unknown",
  "quantitative_density": "medium",
  "chapter_length_profile": "medium",
  "benchmark_pool": "balanced",
  "toc_path": "toc.md",
  "toc_json_path": "toc.json",
  "metadata_path": "metadata.md",
  "chapters": [
    {"chapter_id": "01-intro", "title": "Intro", "source_path": "chapters/01-intro.md"},
    {"chapter_id": "02-core-idea", "title": "Core Idea", "source_path": "chapters/02-core-idea.md"}
  ]
}
```

### Meaning of the genre fields

- `genre_macro`: the main benchmarking bucket you want to report against
- `genre_micro`: a finer subtype for later specialization
- `narrative_vs_expository`: for example `narrative`, `expository`, `mixed`, or `unknown` while you are still reviewing it manually
- `prescriptive_vs_analytical`: for example `prescriptive`, `analytical`, `mixed`, or `unknown` while you are still reviewing it manually
- `quantitative_density`: for example `low`, `medium`, or `high`; the bootstrap tool can suggest this automatically
- `chapter_length_profile`: for example `short`, `medium`, `long`, or `mixed`; the bootstrap tool can suggest this automatically from chapter word counts
- `benchmark_pool`:
  - `balanced` — eligible for stratified dev/gate/holdout assignment
  - `dev_only` — extra wildcard books used only in development
  - `exclude` — ignore this book when building benchmark splits

## Bootstrapping manifests from numbered chapter files

If your book directory already contains chapter markdown like `0.md`, `1.md`, `2.md`, the bootstrap tool can now build `book.json`, `metadata.md`, `toc.md`, and `toc.json` in three ways:

1. from a local Google Books JSON file plus a local TOC file
2. from the original `.epub` already sitting in the book folder
3. from the EPUB plus a `GOOGLE_BOOKS_API_KEY` loaded from `.env`

The simplest case is now:

```bash
python3 tools/bootstrap_book.py \
  --book-dir data/books/my-book \
  --chapter-glob '*.md' \
  --copy-raw-json
```

If there is exactly one `.epub` inside the book folder, the script will try to:
- extract EPUB metadata
- extract the EPUB TOC and write both `toc.md` and `toc.json`
- use the TOC titles for chapter mapping
- auto-suggest `quantitative_density` and `chapter_length_profile`

If `.env` contains `GOOGLE_BOOKS_API_KEY`, the script will also try to look up the book automatically by ISBN first and otherwise by a ranked search that uses title, subtitle, first author, publisher, and publication year where available.

You can still override everything manually:

```bash
python3 tools/bootstrap_book.py \
  --book-dir data/books/my-book \
  --chapter-glob '*.md' \
  --volume-json /path/to/google_books_volume.json \
  --toc-json /path/to/epub_toc.json \
  --genre-macro business_economics_productivity \
  --genre-micro decision_making \
  --narrative-vs-expository expository \
  --prescriptive-vs-analytical prescriptive \
  --copy-raw-json
```

Useful notes:
- use `--chapter-glob 'chapters/*.md'` if your files live under a `chapters/` subdirectory
- the script prefers EPUB TOC titles, but falls back to the first markdown heading in each chapter if TOC alignment is uncertain
- `--toc-offset 1` is useful when the EPUB TOC starts with something like a preface that is not present in your extracted markdown
- `--google-volume-id ...` forces a specific Google Books match
- `--google-query 'intitle:... inauthor:...'` lets you override the automatic Google Books search query completely
- automatic Google Books search now tries progressively broader queries and then reranks candidates using ISBN, title, subtitle, author, publisher, and publication year
- `--dry-run` now shows the Google Books lookup trace, including the queries that were tried and the top-ranked candidate matches
- `--overwrite` replaces an existing `book.json`, `metadata.md`, `toc.md`, or `toc.json`

The bootstrap summary now reports:
- per-chapter visible word counts
- the suggested and selected `quantitative_density`
- the suggested and selected `chapter_length_profile`
- the current manual-review values for `genre_macro`, `genre_micro`, `narrative_vs_expository`, and `prescriptive_vs_analytical`

A good workflow is to let the tool fill the deterministic fields automatically, then manually review the genre and style fields for the final `book.json`.

## Rubric and benchmark outputs

```text
artifacts/
  rubrics/<book_id>/<chapter_id>.json
  book_rubrics/<book_id>.json

bench/
  chapter_fast.jsonl
  book_gate.jsonl
  book_holdout.jsonl
  splits.json
```

## Workflow

Build frozen rubrics from the source chapters:

```bash
python3 tools/build_rubrics.py --books-root data/books --artifacts-root artifacts
```

Check that the corpus has reasonable genre coverage:

```bash
python3 tools/corpus_report.py --books-root data/books
```

Build the benchmark splits. The default mode is **genre-aware** and will balance the selected books across `genre_macro` while respecting `benchmark_pool`:

```bash
python3 tools/build_bench.py \
  --books-root data/books \
  --bench-dir bench \
  --dev-books 10 \
  --gate-books 4 \
  --holdout-books 4 \
  --seed 42
```

For the included sample corpus, rebuild with:

```bash
python3 tools/build_bench.py \
  --books-root data/books \
  --bench-dir bench \
  --dev-books 1 \
  --gate-books 1 \
  --holdout-books 1 \
  --seed 42
```

### Adding a New Candidate

**Step 1 — Auto-generate profiles from a model name:**

```bash
# Both 30m and 60m profiles (default)
python3 tools/add_candidate.py --model-full "minimax/minimax-1.5-flash"
python3 tools/add_candidate.py --model-full "deepseek/deepseek-v4-flash"

# 30m profiles only
python3 tools/add_candidate.py --model-full "openai/gpt-5-mini" --time-budget 30m

# Preview what would be created (no API calls, no file writes)
python3 tools/add_candidate.py --model-full "openai/gpt-5-mini" --dry-run

# List all profiles in the candidates JSON
python3 tools/add_candidate.py --list
```

The script probes the model for (a) JSON schema support, (b) thinking mode, (c) non-thinking mode — and creates one profile per supported mode. If the model supports both thinking and non-thinking, two profiles are created (e.g. `30m_deepseek-v4-flash_thinking` and `30m_deepseek-v4-flash_notthinking`).

Key `--time-budget` values:
- `30m` — only 30-minute whole-book summary profiles
- `60m` — only 60-minute whole-book summary profiles
- `30m 60m` — both (default)

Profiles are written to `data/candidates.json`.

**Step 2 — Compile profiles into the active harness:**

```bash
python3 tools/gen_profile_literal.py
```

This regenerates `Profile` union type and `_CANDIDATES` dict in `candidate_spec.py` from `data/candidates.json`. Run this whenever you add or update profiles.

**Step 3 — Smoke test:**

```bash
python3 core/run_candidate.py --bench chapter_fast --profile <name> --mock --write-results
```

### Run a smoke test without API calls:

```bash
# Single profile smoke test
python3 core/run_candidate.py --bench chapter_fast --profile 30m_minimax_notthinking --mock

# All 30m profiles smoke test (sequential, no LLM calls)
python3 core/run_candidate.py --bench chapter_fast --profile all --time 30m --mock

# All 60m profiles smoke test
python3 core/run_candidate.py --bench chapter_fast --profile all --time 60m --mock
```

### Run a real benchmark with OpenRouter:

```bash
export OPENROUTER_API_KEY=...
# Single profile
python3 core/run_candidate.py --bench chapter_fast --profile 30m_minimax_notthinking --judge-model openai/gpt-5-mini --write-results

# All 30m profiles (sequential)
python3 core/run_candidate.py --bench chapter_fast --profile all --time 30m --judge-model openai/gpt-5-mini --write-results
```

**Profile selection:**
- `--profile <name>` — run a single named profile (e.g. `30m_minimax_notthinking`, `60m_deepseek-v4-flash_thinking`)
- `--profile all --time 30m` — run all 30m-prefixed profiles sequentially
- `--profile all --time 60m` — run all 60m-prefixed profiles sequentially
- `--profile all --time all` — run all profiles sequentially (default with `--profile all`)

**Scoring gates by profile:**
- `30m_*` profiles → min_faithfulness=0.50, min_concept_coverage=0.15 (permissive)
- `60m_*` profiles → min_faithfulness=0.60, min_concept_coverage=0.50 (moderate)
- bare `30m`/`60m` → min_faithfulness=0.70, min_concept_coverage=0.60 (strict default)

**Judge behavior:**
- With `--judge-model openai/gpt-5-mini` — LLM judge evaluates summaries; results include `judge_no_fluff`, `judge_structure_quality`, `judge_concept_coverage`, `judge_concept_coverage_faithfulness`, `judge_overall_quality`
- Without `--judge-model` — scoring uses only deterministic metrics (faithfulness proxy from keyword overlap, concept coverage proxy, length error). All judge fields are absent from results. Useful for fast iteration without LLM cost.

**Common flag combinations:**
- `--mock` — run without any LLM API calls; uses fake responses to test pipeline logic end-to-end
- `--write-results` — persist results JSON and samples JSONL to `runs/` (without this flag results are only printed to stdout)
- `--mock --write-results` — smoke test that saves artifacts locally; useful to verify the full pipeline before real runs

Promote only winning candidates to `book_gate`, then evaluate finalists once on `book_holdout`.

**Step 4 — Promote through benchmark splits:**

| Split | Purpose | Gate |
|-------|---------|------|
| `chapter_fast` | Rapid iteration on chapter summarization | min_faithfulness=0.50, min_concept_coverage=0.15 (30m) |
| `book_gate` | Full-book evaluation, used to select finalists | Same gates as fast |
| `book_holdout` | Final evaluation of winning candidates on unseen books | Same gates |

Promote a candidate from `chapter_fast` → `book_gate` only if its leaderboard scores are competitive. Finalists on `book_holdout` determine which candidates are production-ready.

## Leaderboards

Overall leaderboard:

```bash
python3 tools/leaderboard.py --bench chapter_fast --profile 30m
```

Per-genre leaderboard:

```bash
python3 tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field genre_macro
```

Other useful slices:

```bash
python3 tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field narrative_vs_expository
python3 tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field prescriptive_vs_analytical
python3 tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field quantitative_density
```

The overall results table now includes:
- `worst_genre_macro`
- `worst_genre_macro_utility`
- `genre_macro_spread_utility`
- `n_genre_macros`

That makes it easier to reject systems that win overall but collapse on one important nonfiction genre.

## Make targets

```bash
make rubrics
make corpus-report
make bench
make smoke
make leaderboard
```

## Notes

- The harness optimizes **uncached generation cost**, not evaluation cost.
- The judge is optional. If you omit `--judge-model`, the scorer falls back to deterministic proxies.
- `candidate_spec.py` contains two separate task profiles: `30m` and `60m`.
- The benchmark is designed so you can start with one **general nonfiction system** and later branch into **genre-specific systems** while keeping the general winner as the fallback.

## Resetting the Benchmark

To clear all runs, results, and snapshot data and start fresh:

```bash
python3 reset_benchmark.py
```

This interactive script asks for confirmation before deleting:
- `bench/book_gate.jsonl` — gate set (books selected for benchmark evaluation)
- `artifacts/runs/` — all run outputs
- `results.tsv` — experiment log
- `data/candidates.json` — saved candidates snapshot
- `snapshots/catalog/*.json` — catalog snapshots
- `snapshots/pricing/*.json` — pricing snapshots
- **`Profile` Literal[] in `candidate_spec.py`** — all profile type aliases
- **`PROFILE_CANDIDATES` in `candidate_spec.py`** — all candidate definitions

After resetting, you have a blank slate. Add profiles to `data/candidates.json` via `python tools/add_candidate.py`, then run `python tools/gen_profile_literal.py` to update `candidate_spec.py`.
