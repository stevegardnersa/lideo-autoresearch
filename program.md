# AutoResearch program for nonfiction book summarization

You are running autonomous research on a source-grounded benchmark for summarizing nonfiction books.

Your job is to improve the candidate system in `candidate_spec.py` only.

Do not modify the evaluator, scoring rules, data, rubrics, benchmark splits, benchmark manifest, or run logging schema.

## Objective

Find the best summarization system for two separate products:

1. a **30-minute** whole-book summary
2. a **60-minute** whole-book summary

The winning system should maximize **quality per unit cost**.

Quality means:
- high faithfulness to the source chapters
- strong coverage of the chapter's concepts and explanations
- preservation of important qualifiers, caveats, and limits
- low fluff and low redundancy
- acceptable readability for dense nonfiction
- accurate final word count
- good first-pass length control

Cost means:
- total uncached generation cost for the full production pipeline
- including all length-repair passes

## Recommended corpus size and composition

Use **18 books**.

Build the first real benchmark as:
- **16 core books** across **4 macro-genres**, with **4 books per genre**
- **2 wildcard books** kept in development only

Split them like this:
- **10 development books**
- **4 gate books**
- **4 holdout books**

Recommended layout:
- each core macro-genre contributes **2 dev, 1 gate, 1 holdout**
- wildcard books are marked `benchmark_pool: "dev_only"`

Suggested core macro-genres:
1. `explanatory_science_technology_environment`
2. `business_economics_productivity`
3. `psychology_health_self_development`
4. `history_biography_politics_social_analysis`

## Why this benchmark is genre-aware

This benchmark is designed to find a strong **general nonfiction system first**.
Genre-specific optimization comes later.

Every candidate should therefore be evaluated on:
- **overall utility**
- **per-genre utility**
- **worst-genre utility**

Do not accept an apparent win that improves the global average while collapsing on one important macro-genre.
The best general system should remain the fallback for genres that do not yet have a dedicated optimization path.

## Fast benchmark construction

Build `chapter_fast.jsonl` from the 10 development books.
For each development book, sample 4 chapters:
- one short chapter
- one medium chapter
- one long chapter
- one dense or data-heavy chapter

This yields about **40 chapter-level samples**.

## Slow gate construction

Build `book_gate.jsonl` from the 4 gate books.
Run the full chapter-summary pipeline and then the final whole-book composer.

## Holdout construction

Build `book_holdout.jsonl` from the 4 holdout books.
Do not use these books to choose prompts, models, or hyperparameters.
Use them only once you already have finalists.

## Frozen contract

The following should be treated as frozen:
- `scoring.py`
- benchmark splits under `bench/`
- source markdown under `data/`
- source-derived rubrics under `artifacts/rubrics/`
- judge prompts and judge model settings in the evaluator
- benchmark manifest in `benchmark_version.json`
- logging schema in `results.tsv`

The only file you may edit is:
- `candidate_spec.py`

## Versioning rule

Historical comparisons are only valid inside a single benchmark version.

Increment `benchmark_version` in `benchmark_version.json` whenever any of these change:
- corpus contents
- split membership or split-building logic
- rubric builder behavior
- scoring rules
- judge prompt or judge model
- visible-word-count logic
- logging schema

When a new benchmark version is created, rerun a small anchor set of past models so you can compare the old and new eras.

## Pipeline assumptions

Each candidate defines a two-stage system:

1. **Chapter summarizer**
   - input: source chapter markdown
   - output: detailed chapter summary at a chapter-specific target length

2. **Book composer**
   - input: chapter summaries, plus optional book metadata or retrieved excerpts depending on mode
   - output: final 30-minute or 60-minute whole-book summary

Length control is part of the system under test.
The evaluator records:
- first-pass length error
- final length error
- passes used
- total generation cost across all passes

## Rules for OpenRouter benchmarking

During benchmarking:
- use exactly **one model per stage per experiment**
- do not use model arrays or fallbacks during evaluation
- keep provider settings fixed inside a run
- prefer structured JSON outputs from the model
- compare systems using **uncached** generation cost, not cache-assisted cost
- record a catalog snapshot and a price snapshot for every real run

Caching may still happen in practice, but cached wins are not considered true quality-per-cost improvements for a fresh book.

## Search priorities

Search in this order unless the current incumbent suggests otherwise:

1. model choice for chapter summarization
2. chapter prompt components
3. repair strategy for overshoot and undershoot
4. chapter budget allocation settings
5. composer mode and composer prompt
6. 30-minute chapter over-allocation multiplier before composition
7. format and context modes
8. temperature and token budget settings

## What good hypotheses look like

Prefer one sharp change at a time. Examples:
- "Switch chapter prompt from balanced detail to mechanisms-first because dense nonfiction chapters are explanation-heavy."
- "Use edit-in-place shortening instead of full regeneration because it should reduce cost without hurting faithfulness."
- "Lower allocation alpha so oversized chapters consume less of the final book budget."
- "Increase 30-minute chapter stage multiplier so the composer has more recall to compress from."
- "Use a stronger composer model while keeping a cheaper chapter model."
- "Keep the overall prompt general, but make qualifier preservation stricter because some macro-genres are losing caveats."

Avoid diffuse edits that change many variables without a clear hypothesis.

## Experiment loop

1. Read `results.tsv` and identify the current incumbent for the target profile and benchmark split.
2. Form one falsifiable hypothesis.
3. Edit `candidate_spec.py` only.
4. Run the fast benchmark for the relevant profile.
5. Compare against the incumbent.
6. Check **overall**, **per-genre**, and **worst-genre** results.
7. Only run the slow gate if the fast benchmark is meaningfully better and has no hard failures.
8. Log the outcome to `results.tsv`.
9. Keep the edit only if it improves the selected profile on the frozen metric **without causing a genre collapse**.

## Suggested run commands

```bash
python core/run_candidate.py --spec candidate_spec.py --bench chapter_fast --profile 30m --write-results
python core/run_candidate.py --spec candidate_spec.py --bench chapter_fast --profile 60m --write-results
python core/run_candidate.py --spec candidate_spec.py --bench book_gate --profile 30m --write-results
python core/run_candidate.py --spec candidate_spec.py --bench book_gate --profile 60m --write-results
python core/run_candidate.py --spec candidate_spec.py --bench book_holdout --profile 30m --write-results
python core/run_candidate.py --spec candidate_spec.py --bench book_holdout --profile 60m --write-results
python tools/leaderboard.py --bench chapter_fast --profile 30m --slice-field genre_macro
```

## Acceptance gates

A candidate should be rejected immediately if any of these occur on the fast benchmark:
- malformed output
- final length still outside the hard tolerance after the allowed repair passes
- faithfulness below the evaluator minimum
- catastrophic cost increase with no quality benefit
- severe degradation in one core macro-genre

A candidate should be promoted to slow gate only if:
- fast-benchmark utility beats the current incumbent
- hard-fail count is zero
- the gain is not caused only by cached requests or judge variance
- worst-genre utility does not fall below the current minimum acceptable floor

## Reporting standard

Every row in `results.tsv` should include:
- timestamp
- run id
- benchmark version
- profile
- benchmark split
- candidate name
- candidate hash
- chapter model
- composer model
- judge model
- mean quality
- mean utility
- mean faithfulness
- mean concept coverage
- mean final length error
- mean first-pass length error
- mean passes used
- mean uncached generation cost
- mean actual generation cost
- hard-fail rate
- worst genre macro
- worst genre macro utility
- genre utility spread
- number of genre macros represented in the run
- run artifact path
- catalog snapshot path
- price snapshot path
- notes

## Important strategic guidance

Do not over-optimize readability.
The summaries are for readers who want a faithful, information-dense account of a nonfiction book.
A readable summary is good, but a simpler summary that erases the author's distinctions is worse.

For the **60-minute** product, prefer chapter summaries that are close to their final budget.
For the **30-minute** product, it is acceptable to keep chapter summaries slightly over-complete before the final composer deduplicates and compresses across chapters.

Keep separate leaders for 30-minute and 60-minute summaries.
They are related tasks, but they should not be forced into one configuration unless a shared setup clearly wins on the frozen holdout.

Optimize for a **general nonfiction winner first**.
Only after that should you branch into genre-specific systems, and the general winner should remain the fallback for unseen or underrepresented genres.
