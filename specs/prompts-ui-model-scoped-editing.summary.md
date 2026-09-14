# Summary: Prompts UI — Larger Settings Modal, Create/Edit Dialog, Editable Templates, Model Scoping

**Spec:** `specs/prompts-ui-model-scoped-editing.md`

Four coupled changes to Settings > Prompts, extending the existing prompt-version registry (`system-prompt-v2-versioned-routing.md`) and coexisting with per-model tuning (`per-model-prompt-tuning.md`).

## 1. Settings modal fills viewport

CSS-only in `dashboard/settings.css`. Desktop `min(1240px, calc(100vw - 48px))`, height `min(88dvh, 1080px)`; tablet `calc(100vw - 32px)` / `min(90dvh, 900px)`; ≤599px full-bleed (`100vw`/`100dvh`, radius 0, horizontal nav). Two-column model dialog max-height `min(88dvh, 900px)`. Wide tables get `overflow-x:auto`. Header fixed, `.settings-pane` scrolls.

## 2. New/Edit version becomes a dialog

Remove the inline "New version" block at the bottom of the panel. Add `#promptVersionOverlay` (same `.settings-overlay`/`.settings-dialog` chrome) opened by a `+ New version` button in the Versions section header. Serves create/clone/edit. Fields: stage, label, scope, per-dimension component selects with inline "Edit text", system template, user template, live server-rendered preview (debounced 400 ms via `POST /api/prompts/preview-draft`), fingerprint + placeholder legend. **Edit is copy-on-write**: Save creates a new immutable version with `derived_from`, never mutates the source or repoints slots. Clone no longer uses `window.prompt`. Dirty-close confirm, focus trap, Esc/backdrop, busy guard, `fieldErrors` mapped to inputs.

## 3. Editable underlying prompt + dynamic placeholders

Version record gains: `component_text` (per-dimension text override; falls back to `candidate_spec` maps), `system_template` (authoritative when set; `{{components}}` expands the composed component text), `user_template` (replaces built-in user prompt). New `core/prompt_template.py` uses `{{name}}` regex substitution — never `str.format`, no expressions. Unknown placeholder → 400 `unknown_placeholder`; unbalanced delimiters → `invalid_template`; chapter user template must contain `{{source_md}}`, composer `{{chapter_summaries}}`. Allowlists preserve the target-independent system-prompt invariant (no word/target numbers in system). Missing value at render falls back to built-in + `prompt_warning`, never crashes a run. New `user_template_fingerprint`; registry SHA includes new fields. **Byte-identical guarantee** for versions with no override (regression-tested on seeded `chapter-p1`/`chapter-p2`/`composer-p1`).

## 4. Model-scoped prompts

Versions carry `scope: {kind: general|model, model?, profiles?}`. Slots nested by scope (`general` + `models[model_id]`), legacy flat slots normalized in memory. `set_slot` takes optional `model`; `scope_mismatch` blocks a model-specific version in the general slot (general versions may pin to a model slot). Resolution precedence, recorded as `prompt_source`: explicit override → permutation override (tuning) → registry model (profile-qualified beats model-wide) → registry general → built-in baseline. Resolution happens pre-dispatch (CF stateless); synced CF can resolve scoped ids from the bundled registry. Per-`(stage, model)` production drift in `production_status`.

**Explorer toggles (dashboard/main.js + promptFilters.js).** The System-prompt toggle group is scope-aware: general versions keep one toggle each keyed `version:<id>`, and every model-scoped version collapses into a single aggregate `scope:model` toggle (label `model-specific · N`) that hides only model-scoped runs. **Legacy runs and preferences are v1**: an empty `prompt_version` normalizes to `chapter-p1` (`LEGACY_PROMPT_VERSION`) so pre-registry runs group with v1 instead of an `unversioned` bucket, and `migratePromptVisibility()` rewrites legacy bare/empty preference keys to `version:<normalized-id>`.

## APIs (new/changed)

- `GET /api/prompts` adds `scope`, `model` drift, `effective` map, `?model=` filter.
- `GET /api/prompts/options` adds `placeholders` allowlists + `component_text` library.
- New `POST /api/prompts/preview-draft` (unsaved draft; never writes) and `POST /api/prompts/:id/revise` (copy-on-write).
- New `POST /api/prompts/resolve` (precedence debug).
- `PUT /:id` stays label/notes-only (409 on content keys). `POST /slots` gains `model`. `sync` copies scoped versions.
- New errors: `unknown_placeholder`, `unknown_model`, `unknown_profile`, `scope_mismatch`, `invalid_template`, `invalid_scope`.

## Key defaults / non-goals

Preserve existing behavior until opt-in; no scoring/gate/tolerance changes; no `candidate_spec.py` mutation; no auto-promotion; no auth/CORS change; placeholder substitution only (no scripting). Template caps 64 KB each / 256 KB body. Tests: `test_prompt_template.py`, extended registry/resolution/CF suites, dashboard dialog+scope tests, all offline. Rollback-safe: old readers ignore new keys.
