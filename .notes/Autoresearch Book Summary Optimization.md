# Autoresearch — Book Chapter Summary Optimization System

**What:** End-to-end system for optimizing LLM-generated book chapter summaries. Human annotators review summaries in a side-by-side explorer, leave structured notes, and an automated optimization engine cycles through prompt variants to improve quality scores.

**Stack:** Python 3 (benchmark engine, optimization loop) + Vanilla JS dashboard (Vite dev server with middleware API) + JSONL storage.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    candidate_spec.py                              │
│  Defines every "candidate" — a model × prompt-component          │
│  combination. Each candidate is a 2-stage pipeline:              │
│    Chapter stage → Composer stage → Length control               │
│  Prompt behavior is controlled by 7 orthogonal "components"      │
│  (policy directives injected into the system prompt).            │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    core/run_candidate.py                          │
│  Runs a single candidate against benchmark books.                │
│  Produces: per-chapter summaries, run manifests, sample scores.  │
│  Writes everything into runs/<timestamp>_<candidate>/           │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    scoring.py                                     │
│  Evaluates each chapter summary against ground truth.            │
│  Metrics:                                                         │
│    • faithfulness (0-1) — LLM-as-judge: how faithful to source   │
│    • concept_coverage (0-1) — LLM-as-judge: concepts captured    │
│    • quality (0-1) — deterministic readability score             │
│    • utility (0-1) — information density proxy                   │
│    • pass_rate — fraction of samples without hard failures       │
│  Composite: 35% faith + 25% quality + 25% concept + 15% pass     │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Dashboard (Vite)                               │
│  Web UI at localhost:3000. Side-by-side run explorer shows:      │
│    • Left pane: candidate summaries per chapter                  │
│    • Right pane: another candidate OR original chapter text      │
│    • Scoring tables, pass/fail per sample                        │
│    • Chapter Notes panel (NEW) — annotate and tag summaries      │
│  Notes stored in data/chapter_notes.jsonl (append-only JSONL)   │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│              data/optimized_prompts/<perm>.json                   │
│  Per-permutation (model × budget × thinking) JSON history files  │
│  Each file records: all evaluated variants with scores,          │
│  current_best version per stage (chapter, composer).             │
│  candidate_spec.get_candidate() auto-resolves v2+ profiles from  │
│  here — candidate_spec.py is never mutated by the optimizer.     │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Autoresearch Engine                            │
│  autoresearch/permutation_store.py — I/O for permutation files   │
│  autoresearch/notes_reader.py — parse notes → structured signals │
│  autoresearch/optimizer.py — generate variants, temp-file eval   │
│  autoresearch/agent.py — main loop: read notes → optimize → run  │
│  autoresearch/reporter.py — generate per-model markdown reports  │
└──────────────────────────────────────────────────────────────────┘
```

---

## The Prompt Component System

LLM behavior is controlled by **7 independent policy dimensions**, each with 2-3 preset options. These are injected into the chapter-stage system prompt.

| Dimension        | Policy Key (in code)      | Options                                             | What It Controls                        |
|------------------|---------------------------|-----------------------------------------------------|-----------------------------------------|
| **style**        | `system_style`            | `dense_faithful`, `teacherly_precise`               | Overall tone and compression philosophy |
| **detail**       | `detail_policy`           | `balanced_dense`, `mechanisms_first`, `concepts_first` | What gets prioritized when compressing  |
| **qualifier**    | `qualifier_policy`        | `strict`, `moderate`                                | How caveats, exceptions, limits are handled |
| **structure**    | `structure_policy`        | `heading_aware`, `theme_clustered`, `bullets_only`  | Section organization and heading style  |
| **example**      | `example_policy`          | `explanatory_only`, `sparse_examples`               | When and how source examples are kept   |
| **terminology**  | `terminology_policy`      | `keep_source_terms`, `gloss_more`                   | How technical terms are preserved/explained |
| **anti_fluff**   | `anti_fluff_policy`       | `hard`, `medium`                                    | Aggressiveness of filler and repetition removal |

Each component maps to an exact text block in `candidate_spec.py`. For example:

```python
CHAPTER_SYSTEM_STYLES: Dict[str, str] = {
    "dense_faithful": (
        "You write dense, source-faithful summaries of nonfiction books. "
        "Your task is compression, not simplification..."
    ),
}
```

These maps are assembled into `StageConfig.prompt_components` per candidate, then inserted into the system prompt template at render time.

### Full Policy Options Reference

**CHAPTER_SYSTEM_STYLES** (system_style)
- `dense_faithful` — Maximum compression. Preserve concepts, explanations, terminology, distinctions, caveats. Never invent. Include specific examples, names, numbers, quotes from source. Strive for brevity within word budget.
- `teacherly_precise` — Expert editor of serious nonfiction. Explain clearly but preserve nuance, causal logic, definitions, exceptions, limits. Include source-specific examples.

**DETAIL_POLICIES** (detail_policy)
- `balanced_dense` — Keep as much explanatory detail as budget allows. Prioritize definitions, frameworks, mechanisms, causal relationships, reasoning steps, important examples.
- `mechanisms_first` — Prioritize how things work: mechanisms, sequences, cause-and-effect, operational logic, why concepts matter. Compress rhetorical framing first.
- `concepts_first` — Prioritize major concepts and relationships. Keep conceptual scaffold explicit and scannable.

**QUALIFIER_POLICIES** (qualifier_policy)
- `strict` — Preserve all scope conditions, caveats, exceptions, uncertainty, trade-offs, limits. Never turn qualified claims absolute.
- `moderate` — Preserve important caveats and exceptions, especially when they change meaning.

**STRUCTURE_POLICIES** (structure_policy)
- `heading_aware` — Short markdown headings following chapter structure. Merge minor headings but keep scannable.
- `theme_clustered` — Few conceptual sections, following source order. Cluster related ideas.
- `bullets_only` — Markdown bullets with nested sub-bullets. Keep each bullet dense.

**EXAMPLE_POLICIES** (example_policy)
- `explanatory_only` — Include examples only when clarifying a concept, mechanism, or distinction. Drop decorative anecdotes.
- `sparse_examples` — At most a few representative examples. Prefer abstraction over repeated illustration.

**TERMINOLOGY_POLICIES** (terminology_policy)
- `keep_source_terms` — Preserve author's technical terms and named concepts. Gloss unfamiliar terms once in plain language.
- `gloss_more` — Preserve technical terms but add brief plain-language glosses for readability.

**ANTI_FLUFF_POLICIES** (anti_fluff_policy)
- `hard` — Avoid motivational framing, praise, repetition, scene-setting, meta commentary. Every paragraph adds source-grounded information.
- `medium` — Favor information density. Remove filler and repetition before removing core ideas.

---

## Scoring System

Each chapter summary run produces per-sample scores in `scoring.py`:

```
SampleScore:
  sample_id: str               ← e.g. "frankenstein:000"
  group_id: str                ← e.g. "frankenstein" (book group)
  hard_fail: bool              ← structural failure (empty, truncated, etc.)
  hard_fail_reasons: Tuple     ← why it failed
  resolved_faithfulness: float  ← LLM-as-judge faithfulness [0-1]
  resolved_concept_coverage: float ← LLM-as-judge concept coverage [0-1]
  resolved_qualifier_preservation: float
  resolved_no_fluff: float
  resolved_structure_quality: float
  quality: float               ← deterministic readability score
  utility: float               ← information density proxy
```

**Composite Score** (used for hill-climb comparisons):
```
composite = 0.35 × faithfulness + 0.25 × quality + 0.25 × concept_coverage + 0.15 × pass_rate
```

**DatasetScore** aggregates all SampleScores for a run:
```
DatasetScore:
  n_samples, hard_fail_rate
  mean_quality, mean_utility, mean_faithfulness, mean_concept_coverage
  mean_final_length_error_pct, mean_first_pass_length_error_pct
  mean_passes_used, mean_uncached_cost
  by_group_quality, by_group_utility
  sample_scores: Tuple[SampleScore]
```

---

## Run Explorer Dashboard

Located in `dashboard/`. Served via Vite dev server at `http://localhost:3000`.

### Navigation
- **Runs List** (leftmost panel): All completed runs, sorted by date. Each run shows model, time budget, thinking mode.
- **Book/Chapter Selection**: Click a book → chapters listed. Click a chapter → summaries loaded.
- **Composer Section**: If the run includes a composer stage, the composed book-level summary appears at the top.

### Side-by-Side Comparison
- **Left pane**: Selected candidate's chapter summary.
- **Right pane**: Another candidate's summary OR the original source text (`ORIGINAL_CHAPTER`).
- Swap candidates via dropdown selectors above each pane.
- Switch between summary view and raw scoring data via tabs.

### Scoring View
- Per-metric scores with color coding (green=good, red=poor).
- Hard failure indicators.
- Per-sample cost breakdown.
- Best/worst sample sorting.

### Time Budget Toggle
- "30m" and "60m" pills at the top filter runs by audio-length budget.
- The budget determines word allocation and chapter-stage multipliers.

---

## Chapter Notes System (NEW)

### Purpose
Capture structured human feedback on specific chapter summaries during review. Notes drive the automatic prompt optimization engine.

### Where
The notes panel appears at the bottom of the right pane in the run explorer. Click the header to collapse/expand.

### How It Works

1. **Select a chapter** in the left pane — the notes panel loads existing notes for that chapter.

2. **Look at the summary** you want to comment on. The note is tagged with whatever candidate is currently selected in the **left** pane (so swap left to the candidate you're judging).

3. **Write your note** in the textarea. Be specific: mention what's wrong, what's good, which aspect of the summary you're evaluating.

4. **Tag the dimension(s)** affected by your note. Click one or more tag chips:
   - `style` — overall tone, compression philosophy
   - `detail` — what details are kept vs dropped
   - `qualifier` — caveat/exception handling
   - `structure` — section organization, headings
   - `example` — example usage
   - `terminology` — technical term handling
   - `anti_fluff` — filler/wordiness

   Active tags turn dark (filled).

5. **Click "Add Note"** — it saves to `data/chapter_notes.jsonl` and refreshes the list.

### Data Format
```jsonl
{"book_id":"frankenstein","chapter_id":"000","item_key":"frankenstein:000","candidate_name":"30m_deepseek-v4-flash_notthinking_v1","tags":["style","detail"],"text":"Summary too wordy, needs denser style. Good structure though.","sentiment":-0.6,"auto_tag_source":"llm","timestamp":"2026-06-16T12:00:00Z"}
```

### LLM Auto-Tagging

When a note is submitted **without** manually selected tags (empty `tags` array), the server automatically classifies it:

1. **Primary: OpenCode Go LLM call** — sends the note text to `opencode-go/deepseek-v4-flash` via the OpenCode API (`OPENCODE_BASE_URL`, `OPENCODE_API_KEY`). The LLM returns:
   - `tags`: array of dimension slugs from `["style","detail","qualifier","structure","example","terminology","anti_fluff"]`
   - `sentiment`: float in [-1, 1] indicating whether the note says the current prompt works well (+) or poorly (-)

2. **Fallback: Keyword matching** — if the API is unavailable (no key, timeout, parse error), `inferTags()` scans the note text against per-dimension keyword lists:
   - `style`: tone, voice, compression, dense, wordy, concise, verbose, readable, pace, pacing, write, writing, feels, sounds
   - `detail`: detail, mechanism, concept, balance, depth, deep, surface, coverage, covered, missing detail
   - `qualifier`: qualifier, caveat, exception, nuance, hedging, certainty, uncertain, qualified, absolute, limit, limitation, scope, tradeoff
   - `structure`: structure, heading, section, bullet, organization, cluster, theme, outline, scan, subhead, subsection, layout, flow
   - `example`: example, anecdote, illustration, instance, case, sparse, few examples, too many examples, explanatory, decorative
   - `terminology`: term, terminology, glossary, gloss, jargon, technical, vocabulary, word choice, source terms, defined, definition
   - `anti_fluff`: fluff, fluffy, filler, repetition, repeats, padding, waste, unnecessary, bland, generic, surface-level, shallow

The response body includes `auto_tag_source` ("llm", "keyword", "keyword_fallback_timeout", etc.) so the UI can show whether classification was AI-driven or fallback.

### Manual Tagging

If the user **does** select tag chips before submitting, the server preserves those manual tags verbatim — no auto-tagging occurs. Manual tags always take precedence.

### Sentiment

The `sentiment` field (LLM-assigned or 0 from keyword fallback) is stored on each note. `notes_reader.py` reads it directly — there is no client-side keyword scanning. Mean sentiment per candidate × dimension drives the optimizer's strategy selection (positive sentiment reinforces current options, negative sentiment triggers variants).

### API Routes
| Method | Path         | Description                              |
|--------|-------------|------------------------------------------|
| GET    | `/notes`    | Return all notes with tags, sentiment, auto_tag_source |
| GET    | `/notes/all`| Same as /notes                                         |
| POST   | `/notes`    | Append note; if tags empty, auto-classify via LLM       |

---

## Autoresearch Optimization Engine

Located in `autoresearch/`. Module-based Python package.

### Design Principle: candidate_spec.py is NEVER Mutated

The optimizer never edits `candidate_spec.py`. Instead:
- **Read-only import** of v1 baselines via subprocess
- **Temp-file evaluation**: `_create_temp_spec_file()` copies candidate_spec.py to a temp dir, appends a `get_candidate()` patch that resolves v2+ profiles from the permutation store, and runs the benchmark in that temp environment
- **Permutation store**: all results written to `data/optimized_prompts/<permutation_key>.json` — one JSON file per fixed model × budget × thinking combination
- **Runtime resolution**: `candidate_spec.get_candidate()` checks permutation files for prompt component overrides before falling back to the static `PROFILE_CANDIDATES` dict

### Module: `autoresearch/permutation_store.py`

Core I/O for `data/optimized_prompts/<perm>.json` files. Each file tracks one "permutation" (a fixed model × budget × thinking combination, e.g. `30m_deepseek-v4-flash_notthinking`).

**Schema per file:**
```json
{
  "permutation_key": "30m_deepseek-v4-flash_notthinking",
  "created_at": "2026-06-16T12:00:00Z",
  "updated_at": "2026-06-16T12:05:00Z",
  "chapter": {
    "current_best_version": 3,
    "history": [
      {
        "version": 1,
        "profile": "30m_deepseek-v4-flash_notthinking_v1",
        "components": {"system_style": "dense_faithful", "detail_policy": "balanced_dense", ...},
        "changed_dimensions": [],
        "composite": 0.7123,
        "quality": 0.75,
        "faithfulness": 0.68,
        "concept_coverage": 0.72,
        "pass_rate": 0.85,
        "cost": 0.05,
        "samples_scored": 10,
        "run_id": "",
        "timestamp": "2026-06-16T12:00:00Z"
      }
    ]
  },
  "composer": { "current_best_version": 0, "history": [] }
}
```

**Key Functions:**
- `load_permutation(key) → dict` — read or create empty permutation file
- `save_permutation(key, data)` — atomic write (tmp + rename)
- `add_history_entry(key, stage, profile, components, ...) → int` — append entry, return version number
- `set_current_best(key, stage, version)` — mark best version per stage
- `get_current_best_components(key, stage) → dict | None` — prompt components for current best
- `get_prompt_override(profile, stage) → dict | None` — resolve override for a specific profile
- `resolve_variant(profile) → CandidateSpec | None` — build full spec from v1 baseline + permutation overrides
- `extract_permutation_key(name) → str` — strip version suffix (e.g. `_v3` → base key)
- `list_all_permutations() → List[dict]` — summary of all permutation files

### Module: `autoresearch/optimizer.py`

Generates variant candidates by cycling/swapping prompt components. No longer writes to candidate_spec.py.

**Variant Evaluation:**
- `evaluate_variant(name)` — run benchmark for a v1 baseline (already in PROFILE_CANDIDATES)
- `evaluate_variant_tempfile(name, components)` — create temp patched spec → run benchmark → parse manifest → record in permutation store → cleanup temp dir

**Two Strategies:**

1. **Hill-Climb** (`generate_variants_hill_climb`):
   - For each dimension with human notes, create one variant cycling to the next option
   - Evaluate each via temp-file, keep best if composite improves
   - Records all results to permutation store with version numbers
   - Calls `set_current_best()` on the winning version

2. **Grid Search** (`generate_variants_grid_search`):
   - For dimensions with negative LLM sentiment, try ALL available options
   - Sorted by most-negative sentiment first
   - Up to `max_variants` (default 12) distinct combinations
   - Records all to permutation store, marks best

**Key Functions:**
- `read_spec_file() → Dict[str, dict]` — import candidate_spec.py PROFILE_CANDIDATES via subprocess (read-only)
- `evaluate_variant(name) → EvaluationResult` — run benchmark for existing v1 candidate
- `evaluate_variant_tempfile(name, components) → EvaluationResult` — benchmark via temp patched spec
- `generate_variants_hill_climb(base_spec, signals, name, stage) → List[Variant]`
- `generate_variants_grid_search(base_spec, signals, name, max_variants, stage) → List[Variant]`
- `components_to_dimensions(components) → Dict[str, str]` — convert policy_key:value to dimension:value
- `dimensions_to_components(dims) → Dict[str, str]` — reverse conversion

### Module: `autoresearch/notes_reader.py`

Parses `data/chapter_notes.jsonl` into structured `Signals` objects. Reads LLM-assigned `sentiment` directly from note data (no keyword scanning).

**Key Types:**
- `NoteSignal` — one note × one tag dimension, with candidate, chapter, text, LLM sentiment
- `DimensionFeedback` — aggregated per-dimension (count, chapters, text samples, mean LLM sentiment)
- `CandidateSignals` — all dimensions for one candidate
- `Signals` — all candidates indexed by name

**Key Functions:**
- `parse_notes_file(filepath) → Signals` — read JSONL, parse all notes
- `get_active_dimensions(signals, candidate_name) → List[str]` — which dims have feedback
- `get_dimension_sentiment(feedback) → float` — mean LLM sentiment in [-1, 1]
- `get_current_option(candidate_spec, dimension) → str` — current prompt component value

### Module: `autoresearch/agent.py`

Main coordinator loop. Wires notes → signals → variants → benchmarks → reports.

**CLI Usage:**
```bash
# Optimize a specific candidate with hill-climb (2 iterations)
python -m autoresearch.agent --candidate "30m_deepseek-v4-flash_notthinking_v1" \
  --mode hill_climb --max-iter 2 --output /tmp/opt.json

# Grid search on a candidate with notes (dry-run uses dummy scores)
python -m autoresearch.agent --candidate "30m_deepseek-v4-pro_notthinking_v1" \
  --mode grid_search --max-variants 8 --output /tmp/opt.json

# Auto mode (picks hill-climb or grid-search based on signal count)
python -m autoresearch.agent --model deepseek-v4-flash --budget 30m --thinking notthinking \
  --mode auto --output /tmp/opt.json

# Dry run (no actual benchmarks, uses synthetic scores)
python -m autoresearch.agent --candidate "..." --dry-run --output /dev/null
```

**Arguments:**
| Flag | Choices | Default | Description |
|------|---------|---------|-------------|
| `--model` | any model string | `None` | Filter candidates by model name substring |
| `--budget` | `30m`, `60m` | `None` | Filter by time budget |
| `--thinking` | `thinking`, `notthinking` | `None` | Filter by thinking mode |
| `--candidate` | any candidate name | `None` | Specific candidate to optimize (bypasses filters) |
| `--stage` | `chapter`, `composer` | `chapter` | Which pipeline stage to optimize |
| `--mode` | `hill_climb`, `grid_search`, `auto` | `auto` | Optimization strategy |
| `--max-iter` | integer | `5` | Max hill-climb iterations |
| `--max-variants` | integer | `12` | Max grid search variants |
| `--dry-run` | flag | `False` | Use dummy scores, skip actual benchmarks |
| `--output` | file path | `None` | Write optimization result JSON |

**Mode Selection (--mode auto):**
- ≥ 5 notes for a candidate → grid_search
- < 5 notes → hill_climb

**Output JSON Format:**
```json
{
  "runs": [
    {
      "base_variant": "30m_deepseek-v4-flash_notthinking_v1",
      "model": "deepseek/deepseek-chat",
      "time_budget": "30m",
      "stage": "chapter",
      "best_composite": 0.7123,
      "best_quality": 0.75,
      "best_faithfulness": 0.68,
      "best_concept_coverage": 0.72,
      "best_pass_rate": 0.85,
      "steps": 6,
      "best_variant_name": "30m_deepseek-v4-flash_notthinking_v3",
      "best_changes": ["style", "detail"],
      "permutation_key": "30m_deepseek-v4-flash_notthinking"
    }
  ],
  "notes_file": "data/chapter_notes.jsonl",
  "total_signals": 12
}
```

**Where Results Live:**
- Optimization records: `data/optimized_prompts/<permutation_key>.json`
- Run artifacts: `runs/<timestamp>_<variant_name>/`
- The `current_best_version` per stage in each permutation file is what `candidate_spec.get_candidate()` reads at runtime

### Module: `autoresearch/reporter.py`

Generates per-model markdown optimization reports.

**Two APIs:**
1. `report_from_agent_output(agent_json_path, output_path=None) → str` — from agent JSON output
2. `report_from_optimization_runs(runs, signals, output_path=None) → str` — from in-memory objects

**Report Sections:**
- Overall summary table (candidate, baseline composite, best composite, delta, improved?)
- Per-candidate detail:
  - Model, budget, notes received, affected dimensions
  - Baseline metrics table
  - Best variant name, changed dimensions
  - Best vs baseline comparison with deltas
  - Improvement status (✅ / ❌)
  - Variants tested count

---

## Data Flow: End-to-End Optimization Cycle

```
1. HUMAN REVIEW
   └─ Open Run Explorer at localhost:3000
   └─ Compare candidate summaries side-by-side
   └─ Leave tagged notes on poor-performing chapters
   └─ Notes saved to data/chapter_notes.jsonl

2. SIGNAL PARSING
   └─ notes_reader.py parses JSONL → Signals
   └─ Each note → per-dimension signal with sentiment
   └─ Aggregates: which dimensions need attention, for which candidates

3. STRATEGY SELECTION
   └─ agent.py: if ≥5 notes for candidate → grid_search
   └─ Otherwise → hill_climb
   └─ Can override with --mode flag

4. VARIANT GENERATION
   └─ optimizer.py cycles or sweeps through prompt component options
   └─ NEVER mutates candidate_spec.py
   └─ Uses v1 baseline (read-only via subprocess import)

5. BENCHMARK EXECUTION
   └─ optimizer.py creates temp copy of candidate_spec.py in tmpdir
   └─ Patches get_candidate() to resolve v2+ profiles from permutation store
   └─ Calls core/run_candidate.py <variant_name> in temp environment
   └─ Produces scores in runs/<timestamp>_<variant>/
   └─ Records results in data/optimized_prompts/<perm>.json
   └─ Cleans up temp directory after evaluation

6. SCORE EXTRACTION
   └─ Parses run manifest JSON for SampleScore array
   └─ Computes composite: 0.35 × faith + 0.25 × quality + 0.25 × concept + 0.15 × pass

7. DECISION
   └─ Hill-climb: keep variant if composite > current best
   └─ Grid search: test all, pick highest composite
   └─ Iterate until convergence or iteration budget exhausted

8. REPORTING
   └─ agent.py outputs JSON summary
   └─ reporter.py renders markdown with baseline/best comparisons
   └─ Human reviews report, decides whether to keep changes
```

---

## File Map

```
tool/
├── candidate_spec.py          ← THE source of truth: all candidates, prompt components, policies
├── scoring.py                 ← Quality scoring engine (SampleScore, DatasetScore)
├── core/
│   ├── run_candidate.py       ← Entry point to run a single candidate through benchmark
│   ├── render_system.py       ← Assembles prompt components into final prompt strings
│   └── ...
├── data/
│   ├── chapter_notes.jsonl        ← Append-only JSONL of human notes (with LLM sentiment)
│   ├── optimized_prompts/          ← Per-permutation history files (model × budget × thinking)
│   │   └── <perm_key>.json         ← All evaluated variants + current_best pointer
│   ├── candidates.json             ← OpenRouter model capabilities cache
│   └── benchmark_version.json      ← Current benchmark snapshot ID
├── dashboard/
│   ├── vite.config.js              ← Vite dev server + notes API routes + LLM auto-tagger
│   ├── explorer.html               ← Run explorer UI with notes panel
│   ├── explorer.js                 ← Frontend logic: comparison, notes CRUD, sentiment badges
│   └── explorer.css                ← Styling including notes panel, sentiment/auto-tag badges
├── autoresearch/                   ← Automated prompt optimization engine
│   ├── __init__.py
│   ├── permutation_store.py        ← Read/write data/optimized_prompts/ files (NEW)
│   ├── notes_reader.py             ← Parse chapter_notes.jsonl → Signals (LLM sentiment)
│   ├── optimizer.py                ← Variant generation, temp-file benchmark evaluation
│   ├── agent.py                    ← Main CLI coordinator loop
│   └── reporter.py                 ← Per-model markdown report generation
├── runs/                           ← Benchmark output (run manifests, summaries, scores)
└── tools/
    ├── leaderboard.py              ← Aggregate scoring leaderboard
    └── ...
```

---

## Quickstart: First Optimization Run

```bash
# 1. Start the dashboard
cd tool
npm run dev

# 2. Open http://localhost:3000
#    - Compare candidate summaries
#    - Leave tagged notes on chapters you want to improve

# 3. Check how many notes you've left
wc -l data/chapter_notes.jsonl

# 4. Run optimization (dry-run first to verify)
python -m autoresearch.agent \
  --candidate "30m_deepseek-v4-flash_notthinking_v1" \
  --mode auto --dry-run --output /tmp/opt_preview.json

# 5. Check what variants would be generated
cat /tmp/opt_preview.json | python -m json.tool

# 6. Generate a markdown preview report
python -c "
from autoresearch.reporter import report_from_agent_output
print(report_from_agent_output('/tmp/opt_preview.json'))
"

# 7. When ready, run for real (removes -dry-run)
python -m autoresearch.agent \
  --candidate "30m_deepseek-v4-flash_notthinking_v1" \
  --mode auto --output /tmp/opt_result.json

# 8. Review the report
python -c "
from autoresearch.reporter import report_from_agent_output
report = report_from_agent_output('/tmp/opt_result.json', 'optimization_report.md')
print(f'Report written to optimization_report.md')
"

# 9. Check the permutation store to see what changed
cat data/optimized_prompts/30m_deepseek-v4-flash_notthinking.json | python -m json.tool

# 10. The best variant's prompt components are already registered in the
#     permutation file. candidate_spec.get_candidate() auto-resolves v2+
#     profiles from there. No need to edit candidate_spec.py manually.
#     To run the optimized variant directly:
python core/run_candidate.py 30m_deepseek-v4-flash_notthinking_v3
```

---

## Adding a New Model Candidate

1. Add a new entry to `PROFILE_CANDIDATES` in `candidate_spec.py`:
   ```python
   "30m_your-model_notthinking_v1": CandidateSpec(
       name="30m_your-model_notthinking_v1",
       profile="30m_your-model_notthinking_v1",
       chapter_stage=StageConfig(
           model="your-provider/your-model",
           prompt_components={
               "system_style": "dense_faithful",
               "detail_policy": "balanced_dense",
               "qualifier_policy": "strict",
               "structure_policy": "heading_aware",
               "example_policy": "explanatory_only",
               "terminology_policy": "keep_source_terms",
               "anti_fluff_policy": "hard",
           },
       ),
       # ... rest of configuration
   ),
   ```
2. Run it: `python core/run_candidate.py 30m_your-model_notthinking_v1`
3. Review in dashboard → leave notes → optimize.

## Best Practices for Chapter Notes

- **Be specific**: "The summary drops Walpole's 1764 definition of the sublime" is better than "missing detail".
- **Tag the right dimension**: If the issue is dropped caveats, tag `qualifier`, not `detail`.
- **One dimension per note**: Multiple tags are fine if the issue spans dimensions, but prefer focused notes.
- **Compare before noting**: Use the right pane to view the original chapter text before judging.
- **Leave manual tags when you can**: The LLM auto-tagger is good but your domain knowledge is better. Click tag chips to classify manually when you know what's wrong.
- **If you skip tags, let the LLM auto-tag**: Submit without selecting any tag chips — the server runs the note through `opencode-go/deepseek-v4-flash` to assign dimensions and a sentiment score. Falls back to keyword matching if the API is unavailable.
- **Positive notes help too**: Notes with positive sentiment reinforce current option choices. The LLM auto-tagger scores sentiment on [-1, 1] based on the note text.
- **Minimum for grid search**: 5+ notes on a candidate trigger exhaustive grid search. Fewer notes trigger incremental hill-climb.
- **Notes persist**: The JSONL file is append-only. Old notes remain available for future optimization runs.
- **Check permutation history**: `data/optimized_prompts/` tracks every evaluated variant. Review before making manual changes to candidate_spec.py.
