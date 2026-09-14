# Summary: Revised System Prompt (Target-Aware) + Prompt-Version Routing

Combines `specs/target-aware-length-control.md` and the pasted book-summarisation pipeline brief into one contract, then adds a prompt-version registry with `local` / `production` slots so a new prompt can be tested without changing what the Cloud Function serves.

## Core design decision

The **system prompt is target-independent and versioned**; all numbers (`W_chapter_target`, `[low,high]`, `S_target`) stay in the per-request user prompt. This is what makes passing a `prompt_version` id to the CF meaningful — the same version text is valid for every chapter/target.

## Canonical length math (consolidated)

`words_per_minute=200`; `W_target` = 6,000 (30m) / 12,000 (60m), capped by source-ratio; per-chapter `W_chapter_target` from the existing allocator; `S_target = round(W_chapter_target/18)`; brief's `T_max = round(W×1.35×1.15)`. Effective cap reconciles with the shipped `visible_max_tokens`: `min(stage.max_tokens, max(visible_max_tokens(...), T_max))`. For all current profiles `visible_max_tokens` dominates, so shipped behavior is preserved.

## Revised prompt v2

New `length_policy` component dimension (`explicit_budget_first` default; also `outline_then_fill`, `verbatim_target_echo`, `sentence_budget_strict`). System prompt = existing component tables + `length_policy` + JSON output contract. User prompt adds `Target sentences: {S_target}` when a length policy is active. Repair prompts keep the shipped numeric delta and add a sentence-budget clause. Composer gets the same treatment.

## Prompt-version registry

New `core/prompt_registry.py`; file `data/prompts/registry.json` (+ `cloud_function/prompts/registry.json` snapshot). Versions are immutable `{stage}-p{N}` with component overlay + `sha256[:16]` fingerprint. Slots per stage: `local` and `production`. First load seeds `chapter-p1` (legacy baseline) and `chapter-p2` (v2), both slots at `p1` — shipping changes nothing until an operator assigns v2. Precedence: profile baseline ← permutation override ← registry version.

## Local vs production routing

- Local/direct runs default to slot `local`; CF runs default to `production`.
- Per-run override: `--prompt-version`, `--prompt-slot`, `--allow-prompt-override` (+ env fallbacks).
- CF request adds `prompt_version`, `prompt_fingerprint`, `prompt_stage`, `system_prompt_override`, `allow_prompt_override`. Resolution: override → known registry version → unknown+opt-in inline override → incoming `system_prompt` → bundled production default. Unknown version without opt-in = 422.
- Response `meta` adds `prompt_registry_sha`, `prompt_version`, `prompt_fingerprint`, `prompt_source`, `prompt_warning`; run manifest records them.
- **Test vs prod:** to test a brand-new prompt on the live CF, send `prompt_version` + `allow_prompt_override=true` with the rendered text; no redeploy. Promotion = `Set production` → `tools/sync_prompts.py` → existing deploy.

## Settings > Prompts UI

Enables the disabled nav item. Slot assignment panel (per-stage `local`/`production` selects, Save, drift badge `In sync`/`Not deployed`/`Never reported`); version table (preview, set local/production, clone, delete); create-version dialog with live rendered preview + fingerprint; test panel launch; plus the per-model tuning subsection from `per-model-prompt-tuning.md`.

## APIs

`GET/POST /api/prompts`, `GET /api/prompts/options`, `GET/PUT/DELETE /api/prompts/:id`, `POST /api/prompts/slots`, `POST /api/prompts/:id/preview`, `POST /api/prompts/sync`. JSON, same-origin, atomic writes, `409 registry_changed` on stale slot writes, `409 version_in_use` on delete.

## Explorer Controls filter (§21)

Adds a **System prompt** toggle group to the explorer Controls sidebar, directly above the existing `Show` and `Reasoning` fields (`dashboard/index.html`). Buttons are built from the distinct `prompt_version` values present in loaded run manifests, reusing the `Reasoning` toggle pattern (`judge-toggle` / `.jt-off`, `renderPromptVersionToggles`). **Legacy runs (empty `prompt_version`) are treated as the v1 baseline `chapter-p1`** (`LEGACY_PROMPT_VERSION`), so they group with v1 rather than forming an `unversioned` bucket; legacy preference keys (bare id or empty) are migrated by `migratePromptVisibility()` to `version:<normalized-id>`. Clicking hides those runs from the scatter, tables, and `filteredCount`; filters compose AND with search/run/judge/reasoning. Visibility persists via `promptVersionVisibility` in `scatter_explorer_state`. Registry-independent — works on historical runs; a legacy-only run set renders the single `chapter-p1` toggle with unchanged plots. Scope-aware grouping (model-scoped versions collapsing into one aggregate toggle) is in `specs/prompts-ui-model-scoped-editing.md`.

## Evaluation

Adds per-pass `latency_seconds` + `mean_latency_seconds`; keeps cost, `final_length_error_pct`, faithfulness/coverage. Brief's 0.88/0.80 thresholds exposed as optional `strict-v1` scoring profile selectable via `--scoring-profile` (`auto` default unchanged; `strict-v1_deterministic` disables semantic gates), resolved gate key recorded in the run manifest; defaults stay 0.70/0.60 and gate 0.10. `results.tsv`/manifests gain `prompt_version` + fingerprint columns.

## Acceptance

V1 render byte-identical; v2 tests without deploy both locally and live on CF; production changes only on explicit promote+sync+deploy; unknown version 422; legacy callers unaffected; shipped length-control acceptance still passes. Tests: `tests/test_prompt_registry.py`, `tests/test_cf_prompt_version.py`. Out of scope: gate/threshold changes, auto-promotion, scoring re-runs.
