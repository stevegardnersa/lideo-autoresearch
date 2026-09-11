# Spec: Per-Model Prompt Tuning (`tune_prompt` tool + Prompts tab)

**Feature:** A first-class tool that tunes the chapter-stage prompt for one selected model profile and adopts the best variant, optimizing for **landing inside the word-count target while using the fewest generation passes**. Exposed from a new **Prompts** tab on the Settings page, backed by a registry job tool (`tune_prompt`) that reuses the existing autoresearch loop.

**Input needed:** None — complete behavior contract. No `design_ref/` assets exist in this repo; UI behavior, not pixels, is specified.

**Status:** Draft for implementation

---

## 1. Goals

1. **Per-model tuning.** Given a model, resolve all of its candidate profiles (e.g. `30m_deepseek-v4-flash_thinking_v1`, `60m_…_thinking_v1`), and tune the chapter-stage prompt for the chosen profile.
2. **Default prompt preserved as the baseline.** Every model profile currently ships the same shared baseline `chapter_stage.prompt_components` (see §3). The tool evaluates that baseline first, then searches for per-model overrides. The shared default is never mutated.
3. **Length-first objective.** Score variants by how often the **final** summary lands inside the target tolerance, how often it lands there on the **first pass**, and how many extra repair passes were used. Maximize hit rate; minimize passes.
4. **Quality non-regression guard.** A length win must not silently destroy faithfulness/concept coverage; variants that breach configured floors are ineligible to become the best.
5. **Adopt on success.** The winning variant is written to the permutation store and marked `current_best`, so `candidate_spec.get_candidate()` applies it at runtime for that profile — no hand-editing of `candidate_spec.py`.
6. **Readable report.** Emit a markdown report (baseline vs best, length metrics, pass metrics, changed dimensions, adopted version).
7. **Notes are optional.** The optimizer runs from deterministic run-manifest metrics even when `data/chapter_notes.jsonl` is empty. Human notes may be supplied to steer qualitative dimensions (see §8).

## 2. Non-Goals

- No change to global scoring formulas, gates, or tolerance values (`scoring.py`).
- No change to the shared baseline `prompt_components` in `candidate_spec.py`.
- No tuning of `temperature`, `max_tokens`, provider routing, model selection, or benchmark definitions.
- No mutation of `candidate_spec.py` on disk (same invariant as the existing optimizer).
- No judge-model calls required; the objective is deterministic. LLM judging remains optional and off by default for this tool.
- No visual redesign of the Models page; the Prompts tab is additive.

## 3. Context: Existing System

- **Prompt construction** (`candidate_spec.py`):
  - `StageConfig.prompt_components: Dict[str, str]` maps policy keys to option IDs.
  - `render_chapter_system(spec)` assembles the system prompt from `CHAPTER_SYSTEM_STYLES`, `DETAIL_POLICIES`, `QUALIFIER_POLICIES`, `STRUCTURE_POLICIES`, `EXAMPLE_POLICIES`, `TERMINOLOGY_POLICIES`, `ANTI_FLUFF_POLICIES`.
  - `render_chapter_user(...)` already tells the model `Target visible words: N. Acceptable range: low-high. Try to land inside the range on the first pass.`
  - The **default/shared prompt** is the same component set on every profile: `{system_style: dense_faithful, detail_policy: mechanisms_first, qualifier_policy: strict, structure_policy: heading_aware, example_policy: explanatory_only, terminology_policy: keep_source_terms, anti_fluff_policy: hard}`. This sharedness is exactly what per-model tuning targets.
- **Autoresearch** (`autoresearch/`):
  - `agent.py` coordinates: parse notes → generate variants → benchmark → record in permutation store → mark best.
  - `optimizer.py` generates variants, evaluates via temp `candidate_spec.py` copy, and parses the latest run manifest (`_parse_latest_run_manifest`) into `EvaluationResult` (currently quality, faithfulness, concept coverage, pass_rate, cost — **no length or pass metrics**).
  - `notes_reader.py` parses `data/chapter_notes.jsonl` into per-candidate, per-dimension signals; `agent.py` exits when zero signals exist.
  - `permutation_store.py` persists `data/optimized_prompts/<perm_key>.json` with `{chapter: {current_best_version, history[]}, composer: {...}}`; `candidate_spec.get_candidate()` calls `_apply_permutation_overrides` (v1) and `resolve_variant` (v2+).
  - Permutation key = profile without `_vN`, e.g. `30m_deepseek-v4-flash_thinking`.
- **Length control** (`core/run_candidate.py` `run_length_controlled_stage`): first pass then up to `spec.length_control.max_passes` repair passes, stopping as soon as the current pass is inside `visible_word_range(target_words, tolerance_pct)`. Manifest rows carry `passes_used` and `output_words`.
- **Run manifest** (`core/run_candidate.py`, ~line 2030):
  - `sample_scores[].deterministic` includes `visible_words`, `final_length_error_pct`, `first_pass_length_error_pct`, `final_length_accuracy`, `first_pass_length_accuracy`.
  - `samples[]` includes `target_words`, `summary_visible_words`, `passes_used`.
  - `dataset_score` includes `mean_final_length_error_pct`, `mean_first_pass_length_error_pct`, `mean_passes_used`, `hard_fail_rate`.
- **Dashboard**:
  - Settings nav has a disabled placeholder `Prompts` item (`settings.js` line ~67, `data-section="upcoming"`).
  - Tools are declared in `SCRIPT_REGISTRY` (`vite.config.js` ~line 987) and launched through `POST /api/jobs` (job manager). An `agent` tool already exists with args `model/budget/thinking/candidate/mode/max-iter/max-variants/stage/dry-run`.
  - Models are listed at `GET /api/models`; model edit/run actions live in `settings.js` (`profileMenuAction`, `launchProfileJob`).
  - Notes API: `GET /notes`, `GET /notes/all`, `POST /notes` (auto-tags via LLM, falls back to keyword inference).
- **Relevant scoring constants** (`scoring.py`): `target_tolerance_pct=0.05`, `zero_accuracy_at_error_pct=0.25`, gates `max_final_length_error_pct=0.10`, `max_passes=5`, `min_faithfulness=0.70`, `min_concept_coverage=0.60`, penalty `extra_pass_penalty=0.01`.

## 4. User-Facing Behavior

### 4.1 Prompts tab (Settings)

Enable the existing disabled `Prompts` nav item. The section renders:

1. **Default prompt panel (read-only).** Shows the shared baseline component set and the rendered chapter system prompt text (from `render_chapter_system`). A note states this is the cross-model comparison baseline and is not edited here.
2. **Per-model tuning list.** One row per registered model (`GET /api/models`), each showing:
   - model id, profile count, and the profiles eligible for tuning (budget × thinking/effort);
   - tuning status: `Not tuned` / `Tuned v<N>` with last-tuned timestamp, read from a new `GET /api/optimized-prompts?model=<id>` endpoint;
   - a **Tune prompt** button (disabled while any `tune_prompt` job for that model is queued/running);
   - a **View report** affordance for the most recent tune run.
3. **Empty state.** No models → "Add a model on the Models tab first." No permutation files → rows show `Not tuned`.

### 4.2 Tune dialog

Opened by **Tune prompt**; mirrors the existing model dialog patterns (`launchProfileJob`, notice area, busy guard).

| Field | Control | Default | Notes |
|---|---|---|---|
| Model | select (prefilled, locked when opened from a row) | selected model | From `/api/models`. |
| Profile | select | first tunable profile for the model | Lists `profile` keys; budget/effort derived. Required. |
| Stage | select | `chapter` | `chapter` only in v1; `composer` deferred. |
| Benchmark | text (bench autocomplete) | `chapter_fast` | Existing `bench: true` arg type. |
| Objective | radio | `length` | `length` (default) or `balanced` (legacy composite). See §5. |
| Quality floor | toggle + number | on, `0.70` faithful / `0.60` concept | Guards adoption. Advanced. |
| Search mode | select | `auto` | `hill_climb` / `grid_search` / `auto`. |
| Max iterations / variants | int | `5` / `12` | Bounds 1–50. |
| Include notes | toggle | on if `chapter_notes.jsonl` non-empty, else off | If on and no signals for profile, fall back to metric-only; do not fail. |
| Dry run | bool | off | Prints variant plan, no benchmark spend. |

Submitting validates required fields, then `POST /api/jobs {toolId: "tune_prompt", args}`. A cost/time warning is shown when `dry-run` is off (search may run many benchmark passes).

### 4.3 Job progress & results

- The job streams into **Run data** like any other tool (`runtimeClass: "llm"`, grouped under `run`).
- On success the job marquee line reports: baseline vs adopted length hit rate, mean passes, and the adopted version.
- Report is written next to the permutation file (see §7) and rendered in the Prompts tab on demand.
- Result hints: `{ model, profile, permutation_key, adoptedVersion, lengthHitRate, firstPassHitRate, meanPassesUsed }`.

### 4.4 Applying / reverting

- **Adopt** is automatic on success (`set_current_best`). Because `get_candidate()` applies overrides, the next `run_candidate` for that profile uses the tuned prompt.
- **Revert** sets `current_best_version` back to the baseline version (version 1) via a future endpoint; v1 keeps the shared default. The spec requires the store to retain history so revert is non-destructive.

## 5. Objective Function (core contract)

All metrics are computed from the latest run manifest for the variant, matching by profile name (existing `_parse_latest_run_manifest` discovery).

For sample `i`: `t_i = target_words`, `w_i = summary_visible_words` (fallback `deterministic.visible_words`), `f_i = first_pass_visible_words`, `p_i = passes_used`.

Per-sample:
- `final_err_i = |w_i − t_i| / t_i`
- `first_err_i = |f_i − t_i| / t_i`
- `final_hit_i = 1` iff `final_err_i <= tolerance_pct` (default `0.05`) else `0` (equivalent to `final_length_accuracy == 1.0`)
- `first_hit_i = 1` iff `first_err_i <= tolerance_pct` else `0`
- `extra_i = max(0, p_i − 1)`
- `capped_i = 1` iff `p_i >= length_control.max_passes` (default 5)

Aggregate:
- `final_hit_rate = mean(final_hit_i)`
- `first_pass_hit_rate = mean(first_hit_i)`
- `mean_final_err = mean(final_err_i)`
- `mean_extra_passes = mean(extra_i)`
- `pass_cap_rate = mean(capped_i)`

**Length objective (default, maximize):**

```
length_score =
      0.55 * final_hit_rate
    + 0.20 * first_pass_hit_rate
    + 0.15 * (1 - clamp01(mean_final_err / zero_accuracy_at_error_pct))
    - 0.10 * clamp01(mean_extra_passes / (max_passes - 1))
    - 0.10 * pass_cap_rate
```

Range ≈ [−0.20, 1.00]; monotonic in hit rate and inverse in passes. Weights live in one constant so they are auditable, not scattered.

**Balanced objective (legacy):** existing composite `0.35·faithfulness + 0.25·quality + 0.25·concept_coverage + 0.15·pass_rate`, extended with the same length/pass terms at reduced weight (implementation detail: keep legacy behavior available unchanged when `--objective balanced`).

**Eligibility (quality guard applies to all objectives):**
A variant may become `current_best` only if all hold:
- `mean_faithfulness >= min_faithfulness` (default 0.70), and
- `mean_concept_coverage >= min_concept_coverage` (default 0.60), and
- `hard_fail_rate <= baseline_hard_fail_rate + 0.05`.

If no variant passes eligibility, nothing is adopted; the job finishes `succeeded` with `adoptedVersion = 0` and a clear report line.

**Selection / tie-break:**
Among eligible variants choose the highest `length_score`; ties broken by (1) lower `mean_extra_passes`, (2) higher `mean_faithfulness`, (3) earlier version number.

**Anti-gaming rule:** the objective's tolerance and gates are fixed from `DEFAULT_SCORING_CONFIG`; the tuner must never alter scoring config, gate thresholds, `length_control.tolerance_pct`, or the benchmark. Doing so would make variants incomparable. Tightening/loosening length tolerance is out of scope for this tool.

## 6. Tuning Surface (what variants may change)

Variants remain a dict overlay applied to the profile's `chapter_stage`. Supported keys:

1. **Existing prompt dimensions** (already enumerated in `notes_reader.DIMENSION_OPTIONS`): `style`, `detail`, `qualifier`, `structure`, `example`, `terminology`, `anti_fluff`.
2. **New `length` dimension** → policy key `length_policy`, rendered by a new `LENGTH_POLICIES` map and included in `render_chapter_system` and `render_chapter_user`. Proposed options:
   - `explicit_budget_first` — instructs drafting to the target word count in one pass before polishing.
   - `outline_then_fill` — size each section against the chapter's share of the budget before writing.
   - `verbatim_target_echo` — restates the numeric target and remaining-word budget immediately before generation.
   If `candidate_spec.py` lacks `length_policy`, the option is ignored (forward-compatible overlay), and the spec implementation must add the map to realize the gain.
3. **Length-control repair policies** (advanced, optional): `repair_more` ∈ `REPAIR_MORE_POLICIES` keys, `repair_less` ∈ `REPAIR_LESS_POLICIES` keys. These are applied to `length_control.repair_*_prompt_id`, not `prompt_components`; the permutation entry gains an optional `length_control` override object, and `_apply_permutation_overrides` / `resolve_variant` must apply it.
4. **Search space size:** hill-climb one dimension per step (existing); grid search tries all options for active dimensions capped at `max_variants`. Candidate generation for the length objective does not require notes; when notes are absent, hill-climb cycles the dimensions most correlated with length (`anti_fluff`, `length`, `structure`, `detail`, `style`) rather than an empty active set.

## 7. Backend Contract

### 7.1 New tool entry (`vite.config.js` `SCRIPT_REGISTRY`)

```js
{
  id: 'tune_prompt',
  group: 'run',
  title: 'Tune prompt for a model',
  description: 'Search prompt variants for one model profile; optimize word-count hit rate and minimise passes. Writes data/optimized_prompts/.',
  script: 'autoresearch/tune_prompt.py',
  runtimeClass: 'llm',
  outputs: ['data/optimized_prompts/*.json', 'artifacts/autoresearch/*.md'],
  args: [
    arg('model', 'Model', { type: 'text', required: true, pattern: '^[A-Za-z0-9_.\\-/]+$' }),
    arg('candidate', 'Profile', { type: 'text', required: true, pattern: '^[A-Za-z0-9_.\\-]+$' }),
    arg('bench', 'Benchmark', { type: 'text', bench: true, default: 'chapter_fast' }),
    arg('objective', 'Objective', { type: 'enum', default: 'length', choices: ['length', 'balanced'] }),
    arg('mode', 'Search mode', { type: 'enum', default: 'auto', choices: ['hill_climb', 'grid_search', 'auto'] }),
    arg('max-iter', 'Max iterations', { type: 'int', default: 5, min: 1, max: 50 }),
    arg('max-variants', 'Max variants', { type: 'int', default: 12, min: 1, max: 50 }),
    arg('min-faithfulness', 'Min faithfulness', { type: 'float', default: 0.70, advanced: true }),
    arg('min-concept', 'Min concept coverage', { type: 'float', default: 0.60, advanced: true }),
    arg('include-notes', 'Use chapter notes', { type: 'bool', default: true, advanced: true }),
    arg('dry-run', 'Dry run', { type: 'bool', default: false }),
    arg('json-out', 'JSON report path', { type: 'path', advanced: true, ui: false }),
  ],
  presets: [
    { id: 'length-default', label: 'Length-first (recommended)', args: { objective: 'length', mode: 'auto' } },
    { id: 'dry', label: 'Dry run', args: { objective: 'length', mode: 'auto', 'dry-run': true } },
  ],
}
```

`float` arg type may be new; if not already supported, add it (parse + range validation) or use `text` with pattern.

### 7.2 CLI contract (`autoresearch/tune_prompt.py`)

```
python -m autoresearch.tune_prompt \
  --model deepseek/deepseek-v4-flash \
  --candidate 30m_deepseek-v4-flash_thinking_v1 \
  --bench chapter_fast \
  --objective length \
  --mode auto \
  --max-iter 5 --max-variants 12 \
  --min-faithfulness 0.70 --min-concept 0.60 \
  [--include-notes] [--notes-file data/chapter_notes.jsonl] \
  [--dry-run] [--json-out PATH] \
  [--function-url URL --auth-mode auto]   # forwarded to evaluations like agent.py
```

Behavior:
1. `load_dotenv()`; resolve profile via `candidate_spec.get_candidate` / `read_spec_file`; derive `permutation_key = extract_permutation_key(candidate)`.
2. Evaluate baseline (v1 for that profile) with `evaluate_variant`, or `evaluate_variant_tempfile` for a v2+ starting point. Record baseline metrics (extends `EvaluationResult`, §7.3).
3. If `--include-notes`, parse notes; if zero signals for the candidate, print a notice and continue metric-only (do **not** exit as `agent.py` does).
4. Determine search mode: `auto` → `grid_search` when notes signals ≥ 5 else `hill_climb` (existing rule), but with a metric-only fallback dimension list when no notes.
5. Generate variants (`generate_variants_hill_climb` / `generate_variants_grid_search`, extended for `length` and repair dimensions), evaluate each via temp spec file, record every result in the permutation store as today.
6. Compute `length_score` per variant, apply eligibility, choose best, `set_current_best(perm_key, stage, version)`.
7. Write:
   - permutation store entry (existing schema + optional `length_control` and new metric fields),
   - JSON report to `--json-out` (default `artifacts/autoresearch/tune_<perm_key>_<utcstamp>.json`),
   - markdown report to the same stem `.md` via `reporter.py`.
8. Exit `0` on success even when no variant is adopted; exit non-zero only on unrecoverable errors (missing profile, benchmark failure for baseline, permission/write failure).

### 7.3 `EvaluationResult` extension (`autoresearch/optimizer.py`)

Add fields parsed from the manifest (`_parse_latest_run_manifest`):
`mean_final_length_error_pct`, `mean_first_pass_length_error_pct`, `final_hit_rate`, `first_pass_hit_rate`, `mean_passes_used`, `mean_extra_passes`, `pass_cap_rate`, `mean_uncached_cost`.

Parsing rules:
- Prefer `sample_scores[].deterministic.{final_length_error_pct, first_pass_length_error_pct, final_length_accuracy, first_pass_length_accuracy}` and `samples[].{target_words, summary_visible_words, passes_used}`.
- Join `sample_scores` and `samples` by `sample_id` when both present; if only one list is present, degrade gracefully (hit rate from `final_length_accuracy`; passes default to 1).
- `mean_uncached_cost = mean(uncached_generation_cost or generation_cost)` from `samples[]`.
- Existing composite stays for `objective: balanced`.

### 7.4 Permutation store extension (`autoresearch/permutation_store.py`)

History entries gain optional keys:
- `length_control`: `{repair_more_prompt_id, repair_less_prompt_id}` (present only when tuned),
- metrics: `final_hit_rate`, `first_pass_hit_rate`, `mean_passes_used`, `mean_final_length_error_pct`, `length_score`.

`_apply_permutation_overrides` / `resolve_variant` apply `length_control` overrides onto `spec.length_control` after prompt components. Older files without these keys remain readable.

### 7.5 API additions (`vite.config.js`)

| Method | Path | Request | Response |
|---|---|---|---|
| `GET` | `/api/optimized-prompts` | optional `?model=<id>` | `{ ok, permutations: [{ key, model, profile, chapter_best_version, adopted_metrics, updated_at, report_md? }] }` |
| `GET` | `/api/optimized-prompts/:key` | — | `{ ok, permutation, history, reports: [...] }` |
| `POST` | `/api/optimized-prompts/:key/revert` | `{ stage: "chapter", version: 1 }` | `{ ok, current_best_version }` (resets adoption; non-destructive) |

`GET /api/optimized-prompts` is read-only and safe to call on tab load. Revert is additive and must be confirmed in the UI (irreversible-looking action), though it only changes a pointer.

Job creation stays `POST /api/jobs` with `toolId: "tune_prompt"`. Existing duplicate-detection and LLM-tool serialization rules apply unchanged.

## 8. Notes: Do They Help Fine-Tuning? (decision)

**Short answer: yes, but only as a secondary steering signal — they are not the main lever, and the tool must not require them.**

Reasoning:
- The existing note pipeline maps free text to seven **qualitative** dimensions (`style`, `detail`, `qualifier`, `structure`, `example`, `terminology`, `anti_fluff`) with a sentiment score. Those decide *which* prompt dimensions the optimizer perturbs.
- This feature's target ("hit word count, minimise passes") is **directly measurable** from run manifests (`final_length_accuracy`, `first_pass_length_accuracy`, `passes_used`). The optimizer can hill-climb those numbers with zero human input, so gating on notes would only make the tool harder to use and slower to converge.
- Notes help in three concrete ways:
  1. **New length/pass dimensions.** Add `length` and `passes` to `DIMENSION_SLUGS`, the auto-tag prompt, and the keyword map, so reviewer comments like "first draft was 40% over" or "needed four repairs" become actionable signals mapped to the new `length` dim / repair policies.
  2. **Diagnosis.** A note explaining *why* length misses (padding, over-long examples, wordy framing) indicates which qualitative dimension to move alongside length, reducing wasted benchmark spend.
  3. **Quality protection.** Notes provide the rationale when a length-optimal variant drops faithfulness, informing whether the quality guard should block it.

Recommendation: ship the tool metric-first; add `length`/`passes` note dimensions and an optional free-text **tuning brief** per run. If notes exist and map to active dimensions, mix them into candidate generation; otherwise cycle the length-correlated dimensions. Never make notes a prerequisite.

## 9. State Flows

- **Prompts tab load:** skeleton → `GET /api/models` + `GET /api/optimized-prompts` → rows. Either request failing shows an inline error with retry; the other section still renders.
- **Job launch:** dialog validates → disable submit → `POST /api/jobs` → on 4xx show field/server error (stay open); on success show marquee notice and close/reset.
- **Job running:** guarded by existing `updateToolGuards` (one LLM job at a time); Tune buttons for the model disabled; status visible in Run data.
- **Job success:** result hints shown; Prompts tab refetches permutations; `View report` enabled.
- **Job failure:** job shows failed + stderr tail; no permutation mutation; user can re-run from Run data.
- **No adoption:** success with `adoptedVersion: 0`; UI copies "No variant met the quality floor — baseline retained."
- **Offline / missing API key:** `launchProfileJob` already blocks when `OPENROUTER_API_KEY` is absent; reuse that notice. Cloud Function mode is supported by forwarding `--function-url`/`--auth-mode` like `agent.py`.

## 10. Edge Cases

1. **Unknown profile / model not in spec** → validation error before job creation; CLI exits non-zero with a clear message.
2. **Zero notes** → metric-only search; must not exit like `agent.py`.
3. **Baseline benchmark fails** → abort before generating variants; job fails with manifest error; no store write.
4. **Variant benchmark times out / exits non-zero** → recorded as failed step (`EvaluationResult.error`), excluded from eligibility; search continues.
5. **All variants fail** → no adoption; report lists errors.
6. **No run manifest found** for a variant → error like today; not eligible.
7. **Tie in `length_score`** → deterministic tie-break (§5).
8. **Exact target hit on first pass** (`final_err = 0`, `p = 1`) → maximal practical score; no further iterations needed (loop can stop early when `final_hit_rate == 1.0` and `mean_extra_passes == 0` and budget allows).
9. **Profile already tuned** → baseline for the run is the current best (highest version) rather than v1, so tuning is incremental and monotonic; history is retained.
10. **Concurrent tune for same profile** → existing job manager duplicate/serialization rules prevent two LLM jobs; additionally reject with 409 semantics if a runner is active.
11. **Partial manifest / old manifest without `passes_used`** → default passes to 1 and note the degraded metrics in the report rather than crashing.
12. **`length_policy` unsupported by candidate_spec** → overlay ignored; variant still evaluated, gain may be zero; report flags it.
13. **Permutation file corrupt** → treat as empty (existing behavior) but do not overwrite until a valid baseline is recorded; log a warning.
14. **Revert target version missing** → 400, no change.
15. **`max_passes == 1`** → avoid divide-by-zero in the pass normalization; clamp denominator to ≥ 1.

## 11. Auth & Permissions

- No new auth surface: the dashboard is local, and all endpoints inherit the existing dev-server model. `POST /api/jobs` and the revert endpoint are mutating and must be same-origin only (no CORS widening).
- The tool spends provider credits; it must respect the existing missing-key guard and (if present) the LLM-tool concurrency guard.
- Cloud Function mode (`--function-url`, `--auth-mode`) must propagate to every child evaluation exactly as in `agent.py`.

## 12. Accessibility

- Prompts nav item is a real `<button>` in the existing nav; `aria-current`/active class on selection.
- Tune dialog: labelled controls with `for`/`id`; validation errors linked via `aria-describedby` and announced in the existing live region.
- Job status badges use text (not color alone); console area keeps `role="log"` and `aria-live="polite"`.
- Model list rows expose Tune as a button with accessible name including the model id (`aria-label="Tune prompt for <model>"`).
- Revert confirmation is a keyboard-reachable button with an explicit confirm state (mirror the two-step cancel pattern).
- Respect `prefers-reduced-motion` for any progress animation (existing console auto-scroll already checks it).
- Report table uses proper `<table>` semantics with headers.

## 13. Performance Targets

- Prompts tab first paint from cached `/api/models` < 300 ms; `/api/optimized-prompts` < 200 ms for ≤ 100 permutation files.
- No benchmark evaluation is triggered by page load; only explicit jobs.
- Tuning cost/time is bounded by `--max-iter` / `--max-variants`; dry-run prints the plan and performs zero LLM calls.
- Each evaluation is a full benchmark subprocess; the spec keeps the existing 3600 s per-evaluation timeout and does not parallelize evaluations (provider/concurrency safety).
- Manifest parsing is O(samples) and reuses the existing single-pass scan; no full run-tree re-read per sample.

## 14. Testing Requirements

1. **Objective math** (unit, offline): fixture manifests with known target/first-pass/final words and passes → assert `final_hit_rate`, `first_pass_hit_rate`, `mean_extra_passes`, `pass_cap_rate`, `length_score`.
2. **Eligibility guard**: a variant that improves length but drops faithfulness below 0.70 is not adopted; baseline retained.
3. **Adoption**: best eligible variant gets `current_best_version`; `candidate_spec.get_candidate(profile)` returns its components (and `length_control` override if present).
4. **Metric-only search**: empty `chapter_notes.jsonl` → search runs and can adopt; no sys.exit.
5. **Notes optional mix**: with notes present, active dimensions influence variant generation; with none, length-correlated fallback is used.
6. **Revert**: reverting to version 1 restores the shared default components; history intact.
7. **API**: `GET /api/optimized-prompts` shape; revert validation (missing version → 400).
8. **Tool registry**: `tune_prompt` appears in `GET /api/tools` with correct args/presets; job creates and streams.
9. **Edge cases** in §10: unknown profile, baseline failure, all-variant failure, old manifest.
10. All tests run offline against the mock generator; no live provider calls or judge calls unless explicitly gated.

---

## Appendix A — Permutation entry shape (extended)

```json
{
  "version": 3,
  "profile": "30m_deepseek-v4-flash_thinking_v3",
  "components": { "anti_fluff_policy": "hard", "length_policy": "explicit_budget_first", "...": "..." },
  "length_control": { "repair_less_prompt_id": "shrink_merge_sections" },
  "changed_dimensions": ["length", "anti_fluff"],
  "composite": 0.71,
  "quality": 0.68,
  "faithfulness": 0.74,
  "concept_coverage": 0.66,
  "pass_rate": 0.9,
  "final_hit_rate": 0.82,
  "first_pass_hit_rate": 0.55,
  "mean_passes_used": 1.6,
  "mean_final_length_error_pct": 0.043,
  "length_score": 0.78,
  "cost": 0.42,
  "samples_scored": 40,
  "run_id": "...",
  "timestamp": "2026-09-11T00:00:00Z"
}
```