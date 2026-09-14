# Spec: Prompts UI — Larger Settings Modal, Create/Edit Version Dialog, Editable Templates, Model Scoping

**Feature:** Four coupled improvements to the Settings > Prompts surface:

1. The Settings modal should occupy substantially more of the viewport.
2. The "New version" form must move out of the bottom of the version list into its own modal dialog.
3. The underlying prompt text (component bodies + user-prompt template) must be **editable**, with first-class support for **dynamic `{{placeholder}}` variables** substituted at render time.
4. Prompt versions become **scoped** — either **general** (all models) or **model-specific**, so a prompt can be tuned for one model without changing what other models receive.

**Input needed:** None — complete behavior contract. No `design_ref/` assets exist in this repo; UI behavior and contracts, not pixels, are specified.

**Status:** Draft for implementation

**Related specs:**
- `specs/system-prompt-v2-versioned-routing.md` — prompt-version registry, local/production slots, `/api/prompts` endpoints, CF routing. This spec extends that registry; all existing invariants hold unless explicitly changed here.
- `specs/per-model-prompt-tuning.md` — the `tune_prompt` tool and permutation store. This spec adds the authoring/assignment counterpart (hand-authored model-scoped versions) and defines precedence between the two.

---

## 1. Goals

1. **Focused workspace.** Settings modal uses most of the viewport so the Prompts tables, editor, and preview are usable without scrolling a cramped 860×640 box.
2. **Create/edit in a dialog.** "New version" and "Edit version" open a dedicated modal (same dialog chrome as the model editor), never an inline form under the list. The version list gets a `+ New version` action in its section header.
3. **Editable prompt source.** Operators can edit (a) the text of each selected component and (b) the full system/user prompt templates, including dynamic variables. Edits are revisioned, validated, fingerprinted, and previewable before save.
4. **General vs model-specific.** A version declares a scope: `general` or `model`, optionally narrowed to one or more profile keys. Slot assignment, listing, filtering, resolution, and Cloud Function routing all become scope-aware, with the general slot preserved as the backward-compatible default.
5. **No silent breakage.** Existing registry files, existing run callers, existing permutation overrides, and the current shipped prompts continue to work byte-identically until an operator opts into a scoped version.

## 2. Non-Goals

- No visual redesign of the Models, Run data, or Judges tabs.
- No change to scoring formulas, gates, length tolerance, or the `tune_prompt` objective.
- No mutation of `candidate_spec.py` component maps on disk — per-version text overrides live in the registry.
- No auto-promotion to production; `Set production` + `Sync/Promote` stays explicit.
- No auth/CORS changes; the dashboard remains same-origin local dev.
- No free-form prompt scripting / arbitrary code execution in templates (placeholder substitution only).

---

## 3. Feature A — Settings modal fills the viewport

### 3.1 Current

`.settings-modal` (`dashboard/settings.css`) is `width: min(860px, calc(100vw - 48px)); max-height: min(72vh, 640px)`. The Changes are CSS-only in `.settings-modal`; `.settings-overlay` centering and `.settings-pane` internal scrolling stay.

### 3.2 Required behavior

- **Desktop (≥ 1024px):** width `min(1240px, calc(100vw - 48px))`, height `min(88dvh, 1080px)` (fallback `88vh` for browsers without `dvh`). The modal uses `display:flex; flex-direction:column` (already) so header stays fixed and `.settings-pane` scrolls.
- **Tablet (600–1023px):** width `calc(100vw - 32px)`, height `min(90dvh, 900px)`.
- **Narrow / mobile (≤ 599px):** width `100vw`, height `100dvh`, `border-radius: 0`; `.settings-nav` becomes a horizontal scrolling row above the pane (existing nav markup, new media-query layout) so vertical space is preserved for content.
- The two-column model dialog (`#modelDialogOverlay.has-run-form .settings-dialog`, width `min(920px, …)`) is unchanged except its own max-height is `min(88dvh, 900px)`.
- Long tables inside `.settings-pane` (Versions, Slots) get `overflow-x:auto` wrappers so wide action columns remain reachable at narrow widths; no content clipping.
- `max-height` must use `dvh` where supported so mobile browser chrome does not cut off the footer.

### 3.3 Acceptance

- At 1280×800 the modal is ≥ 1200px wide and ≥ 690px tall.
- Opening any tab shows a fixed header, independently scrolling pane, and no page-level scrollbar.
- At 375×667 the modal is full-bleed, nav is usable, and all Prompts actions remain reachable.

---

## 4. Feature B — Create / Edit version dialog

### 4.1 Current

`renderPrompts()` emits a third `.prompt-block` titled "New version" at the bottom of `#promptsPanel`, with `#promptNewStage`, `#promptNewLabel`, `#promptNewComponents`, `#promptNewTemplate`, and a `Create version` button. `createPromptVersion()` reads those fixed IDs. `clonePromptVersion()` uses `window.prompt` for the label.

### 4.2 Required behavior

- **Remove** the inline "New version" `.prompt-block` from the panel entirely.
- **Versions section header** becomes a flex row: title on the left, `+ New version` button on the right (reuse `.mini-btn` / `.model-add-btn` styling).
- **One reusable dialog** `#promptVersionOverlay` added to `MODAL_TEMPLATE` alongside `#modelDialogOverlay`, using the same `.settings-overlay` + `.settings-dialog` + `.settings-modal-header` chrome. It serves create, clone, and edit.
- Dialog fields:
  1. **Title** — `New version` / `Clone <id>` / `Edit <id>`.
  2. **Stage** — `chapter` | `composer`. Required. Disabled when editing an existing version or cloning (stage is fixed by source).
  3. **Label** — text, required, trimmed, max 120 chars. Prefill `v<N> experiment` for new, `"<src.label> (copy)"` for clone, existing label for edit.
  4. **Scope** — see §7: `General` | `Specific model`, plus a model picker and optional profile multi-select when specific.
  5. **Components** — one control per dimension from `GET /api/prompts/options` `stage_keys`; value `(inherit)` or a component id. Under each control, a collapsible **Edit text** textarea prefilled with the library text for the selected id (§6).
  6. **System template** — textarea, optional. Placeholder `Leave blank to render from components`. Supports `{{...}}` variables.
  7. **User template** — textarea, optional. Placeholder `Leave blank to use the built-in user prompt`. Supports `{{...}}` variables.
  8. **Live preview** — two panes (system prompt, user prompt) rendered server-side against a sample target (default `target_words: 1200`) via `POST /api/prompts/preview-draft`; shows fingerprint and a placeholder legend. Debounced 400 ms; renders on any field change; inline error if the draft is invalid.
  9. **Footer** — `Cancel` and `Save` (label `Create version` / `Save as new version` for edits). Save disabled while invalid or in flight.
- **Behavior:**
  - Open resets state; every open is a fresh draft (no leakage between opens).
  - `Cancel`, backdrop click, and `Esc` close without saving. If any field changed, a confirm is required before discarding.
  - **Edit is copy-on-write.** `Save` on an edit creates a new immutable version (`derived_from: <id>`) in the same stage and scope, then closes. It never mutates the source version's content (label/notes-only edits may keep using `PUT`). This preserves slot assignments and fingerprints.
  - **Clone** prefills from the source (components, component text, templates, scope) but always creates a new version and allows changing scope.
  - On success: close dialog, `loadPrompts()`, banner `Created <id>`; if the source was assigned to a slot, banner `Created <id> (derived from <src>)`.
  - `Esc` handling must not close the underlying Settings modal while the dialog is open (dialog overlay is topmost; Settings close is ignored until the dialog closes).
- **Clone no longer uses `window.prompt`.**

### 4.3 Validation (client + server, server authoritative)

| Field | Rule | Error surface |
| --- | --- | --- |
| Stage | required, one of `chapter`/`composer` | inline under field |
| Label | required, ≤ 120 chars | inline |
| Components | each value must be a known component id for the stage | inline |
| System/User template | `{{name}}` names must be in the stage allowlist (§6.3); balanced delimiters | inline, names listed |
| User template — chapter | must contain `{{source_md}}` if non-empty | inline (blocking) |
| User template — composer | must contain `{{chapter_summaries}}` if non-empty | inline (blocking) |
| Scope model | required when `Specific model`; must match a registered model id | inline |

Server returns `400` with `{ok:false, error, fieldErrors:{field:message}}`; the UI maps `fieldErrors` onto the corresponding inputs and keeps the dialog open.

---

## 5. Feature C — Editable underlying prompt

### 5.1 Components today

`candidate_spec.chapter_component_tables()` / `composer_component_tables()` map each dimension to a dict of `id → text`. A version stores only the selected `id`s; `_component_text()` concatenates the mapped text in dimension order. Operators can choose an id but cannot change a word of the underlying text, and there is no way to inject per-request values.

### 5.2 Editable layers

A registry version gains two new optional layers, both stored in `data/prompts/registry.json` and copied to the CF bundle by `sync`:

1. **`component_text`** — per-version text override for the selected component, keyed by dimension: `{ "<dimension>": "<full text>" }`. Only dimensions present here override the library text; unedited dimensions fall back to `candidate_spec` maps. Renderer: for each dimension, use `component_text[dim]` if present, else `table[selected_id]`; dimensions are appended in the stage's existing table order.
2. **`system_template`** — optional full replacement for the component-composed system prompt. When non-empty it is authoritative and `component_text`/`components` are used only to populate the preview legend.
3. **`user_template`** — optional full replacement for the built-in user prompt. When non-empty the renderer produces it via placeholder substitution; the built-in `render_chapter_user` / `render_composer_user` is not called.

`template` (the existing field) is retained as an alias: on read, if `system_template` is absent, use `template`. On write, new versions always emit `system_template`; `template` is still written for backward compatibility with older readers/CF snapshots.

### 5.3 Placeholder engine

New module `core/prompt_template.py`:

```
PLACEHOLDER_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/

extract_placeholders(text) -> list[str]        # in first-appearance order, de-duped
validate(text, allowlist) -> raises UnknownPlaceholder(names)
render(text, values) -> str                    # replaces known names; missing -> MissingValue
```

- Substitution is plain string replacement, not `str.format` — never evaluate expressions, never touch `{}` single braces.
- Literal `{{` cannot be represented; document this and reject unbalanced delimiters at save time.
- Unknown placeholder at create/save → `400` `unknown_placeholder` with the offending names and the stage allowlist.
- Missing value at render time → not possible for built-in call sites (values are always supplied); if it somehow occurs, log and fall back to the built-in template, record `prompt_warning` in the run manifest, and surface it in Settings > Prompts (do not crash a run).

### 5.4 Allowlist by stage

**System template** (target-independent invariant preserved): `book_title`, `chapter_title`, `toc_md`, `book_metadata`, `components`.

- `{{components}}` expands to the composed component text (ordered). This is the only way a system template can reuse the component selection while adding its own instructions.
- Target/word numbers are intentionally **not** allowed in the system template; they stay in the user prompt so one version is valid for every target.

**Chapter user template:** `book_title`, `chapter_title`, `toc_md`, `book_metadata`, `target_words`, `low`, `high`, `s_target`, `source_md`, `context_block`, `format_instructions`, `required_behavior`.

**Composer user template:** `book_title`, `chapter_summaries`, `target_words`, `low`, `high`, `s_target`, `format_instructions`.

`context_block`, `format_instructions`, and `required_behavior` expand to rendered sub-prompts matching the current built-in output for the spec's `context_mode` / `format_mode`.

### 5.5 Rendering integration

- `core/prompt_registry.py` exposes `resolve_prompt(stage, version_id=None, slot=None, model=None, profile=None) -> ResolvedPrompt` with fields `{system_prompt, user_prompt_renderer, version_id, fingerprint, source, placeholders}`.
- `render_chapter_system` / `render_composer_system` accept an optional resolved version: when the version has `system_template`, substitute `{{components}}` (from component text) and the system allowlist; otherwise render as today.
- `render_chapter_user` / `render_composer_user` gain a parallel path that, when the active version has `user_template`, substitutes the user allowlist with values computed exactly as the built-in does (`visible_word_range`, `sentence_budget`, `_context_block`, `FORMAT_INSTRUCTIONS`, source/title fields).
- **Byte-identical guarantee:** a version with no `component_text`, no `system_template`, and no `user_template` renders exactly the current output. Regression test asserts SHA equality against pre-change renders for the seeded `chapter-p1`/`chapter-p2` and `composer-p1` versions.

### 5.6 Fingerprint

`system_prompt_sha256` / `system_prompt_fingerprint` continue to fingerprint the **resolved system prompt**. New `user_template_fingerprint` is added (fingerprint of `user_template`, or `""` when built-in). `compute_registry_sha()` includes `component_text`, `system_template`, `user_template`, and `scope` so any content/scope change invalidates stale slot writes. Existing fields keep their meaning; old files remain readable (missing keys treated as absent).

---

## 6. Feature D — Model-scoped prompts

### 6.1 Version scope

Every version carries:

```json
"scope": { "kind": "general" }
"scope": { "kind": "model", "model": "deepseek/deepseek-v4-flash", "profiles": ["30m_deepseek-v4-flash_thinking_v1"] }
```

- `kind` ∈ `general` | `model`. Default `general` for all existing versions (read path injects `{kind:"general"}` when absent — never rewrites old files unless saved).
- `model` required when `kind=model`; must match a `GET /api/models` id. Server rejects unknown ids with `400 unknown_model`.
- `profiles` optional array of profile keys; empty/absent means the whole model. Profile entries must belong to the named model, else `400 unknown_profile`.
- Scope is part of identity for listing/assignment; **changing scope creates a new version** (via the edit dialog's copy-on-write save). `PUT /api/prompts/:id` still only edits `label`/`notes`.
- A version's `id` remains `{stage}-p{N}` (global counter). The UI shows a scope badge: `General` or the model id (profile suffix when narrowed).

### 6.2 Slot schema (extended, backward compatible)

Slots become nested by scope while old files keep working:

```json
"slots": {
  "chapter": {
    "general": { "local": "chapter-p2", "production": "chapter-p1" },
    "models": {
      "deepseek/deepseek-v4-flash": { "local": "chapter-p3", "production": null }
    }
  },
  "composer": { "general": { "local": "composer-p1", "production": "composer-p1" }, "models": {} }
}
```

- On read, a legacy `slots[stage] = {local, production}` is normalized in memory to `{general:{...}, models:{}}`. Files are not rewritten until the next save.
- `set_slot` accepts optional `model`; omitted/empty means the general scope. A model-scoped slot may only reference a version whose scope matches (general versions may be assigned to a model slot as an explicit pin; a model-specific version may **not** be assigned to the general slot — `400 scope_mismatch`).
- `production_status[]` entries gain `model` (`null` for general); drift is computed per `(stage, model)` from CF `meta.prompt_version` + `meta.model`.
- **Legacy runs and preferences are v1.** Runs stored before the registry existed carry `prompt_version: ''`; they used the v1 baseline prompt (the profile defaults), which is seeded as `chapter-p1`. The explorer normalizes the empty value to `chapter-p1` (`LEGACY_PROMPT_VERSION`) when building toggles and filtering, so legacy runs group with v1 instead of forming a separate `unversioned` bucket. Persisted `promptVersionVisibility` from before the scope-aware keys is migrated by `migratePromptVisibility()`: bare ids and the empty key are rewritten to `version:<normalized-id>` (empty → `version:chapter-p1`).

### 6.3 Resolution precedence (single source of truth)

Runtime resolution for a run with model `M`, profile `P`, stage `S`, slot scope:

1. **Explicit per-run override** — `--prompt-version` / CF `prompt_version` (+ `allow_prompt_override`) — unchanged from the prior spec.
2. **Permutation override** — `data/optimized_prompts/<perm_key>.json` adopted best (existing `per-model-prompt-tuning` behavior); highest non-explicit priority because it is empirically measured per profile.
3. **Registry model version** — the version assigned to `S.models[M]` (profile-qualified match wins over model-wide), for the active slot (`local` for local/direct runs, `production` for CF unless overridden).
4. **Registry general version** — `S.general[slot]`.
5. **Built-in profile baseline** — `candidate_spec` `PROFILE_CANDIDATES` components (current shipped behavior).

Rules:
- Resolution happens **before dispatch** (runner/`get_candidate`), because the CF is stateless; the resolved `prompt_version` is sent in the CF request. A synced CF can additionally resolve the id itself by looking it up in its bundled registry.
- `ResolvedPrompt.source` ∈ `override | permutation | registry_model | registry_general | baseline` and is recorded in the run manifest (`prompt_source`) and CF `meta`.
- The UI surfaces the effective version per model on the Prompts tab ("Effective: chapter-p3 — model scope").

### 6.4 Authoring path

- The version dialog (§4) has a **Scope** control; when `Specific model` is chosen, a model `<select>` (from `/api/models`) and an optional profile multi-select (from the selected model's profile keys) appear.
- Slots panel gains a **Scope** selector: `General` or a model, above the existing stage rows. Selecting a model shows that model's local/production selects; general rows are unchanged. Save posts `{stage, slot, model, version_id, expected_sha}`.
- Versions table gains a **Scope** column and a scope filter (`All | General | <model>`). Model-specific rows show a badge and their "Set local/production" buttons target that model's slot.
- "Preview" and "Smoke test" operate on the version regardless of scope; smoke test passes the resolved id.

---

## 7. Data model (registry version record)

```jsonc
{
  "id": "chapter-p3",
  "stage": "chapter",
  "label": "flash tuned v1",
  "scope": { "kind": "model", "model": "deepseek/deepseek-v4-flash", "profiles": [] },
  "components": { "system_style": "dense_faithful", "length_policy": "explicit_budget_first" },
  "component_text": { "system_style": "You write ... {{book_title}} ..." },
  "system_template": "…{{components}}…",
  "user_template": "Book: {{book_title}}\nTarget: {{target_words}} ({{low}}-{{high}})\n{{source_md}}",
  "template": "…",                       // legacy alias = system_template
  "system_prompt_sha256": "...",
  "system_prompt_fingerprint": "16hex",
  "user_template_fingerprint": "16hex",  // "" when built-in
  "created_at": "ISO-8601",
  "created_by": "operator",
  "derived_from": "chapter-p2",          // absent for originals
  "notes": "",
  "immutable": true
}
```

Invariants:
- Content-bearing fields (`components`, `component_text`, `system_template`, `user_template`, `scope`) are immutable after creation; any change is a new version.
- `id` is globally incremented per stage; revisioning a `chapter-p2` yields the next free `chapter-pN` and sets `derived_from`.
- Unknown keys are preserved on load/save (forward compatibility).

---

## 8. API contracts (delta)

All endpoints under `/api/prompts`, JSON, same-origin, atomic writes (`tempfile` + rename). Existing endpoints keep their shapes; changes are additive unless noted.

| Method | Path | Request | Response | Notes |
| --- | --- | --- | --- | --- |
| `GET` | `/api/prompts` | `?stage=&model=` | `{ok, registry_sha, versions:[…], slots:{…normalized…}, production_status:[{stage, model, selected_id, reported_id, reported_fingerprint, reported_at, in_sync}], effective:{chapter:{model, version_id, source}}, models:[…]}` | Adds `scope` to versions, `model` to status, `effective` map. `?model=` filters versions to that model + general. |
| `GET` | `/api/prompts/options` | — | `{ok, dimensions:{…}, stage_keys:{…}, placeholders:{chapter:{system:[…], user:[…]}, composer:{…}}, component_text:{chapter:{dim:{id:text}}, composer:{…}}}` | Adds placeholder allowlists and library text so the editor can prefill Edit-text areas. |
| `GET` | `/api/prompts/:id` | — | `{ok, version, rendered_system_prompt, rendered_user_prompt, system_prompt_sha256, fingerprint}` | Adds rendered user prompt. |
| `POST` | `/api/prompts` | `{stage, label, scope?, components?, component_text?, system_template?, user_template?, template?, notes?}` | `{ok, version}` 201 | Validates placeholders/scope; alias `template` accepted. |
| `POST` | `/api/prompts/preview-draft` | `{stage, components?, component_text?, system_template?, user_template?, scope?, target_words?, sample?}` | `{ok, system_prompt, user_prompt, fingerprint, placeholders, warnings:[…]}` | New. Renders an **unsaved** draft for the dialog preview; never writes. |
| `POST` | `/api/prompts/:id/revise` | `{label?, components?, component_text?, system_template?, user_template?, scope?}` | `{ok, version}` 201 | New. Copy-on-write edit; `derived_from=:id`; scope defaults to source. |
| `PUT` | `/api/prompts/:id` | `{label?, notes?}` | `{ok, version}` | Unchanged; 409 if content keys are present (use `/revise`). |
| `DELETE` | `/api/prompts/:id` | — | `{ok}` | 409 `version_in_use` if assigned to any slot (general or model). |
| `POST` | `/api/prompts/slots` | `{stage, slot, version_id, model?, expected_sha?}` | `{ok, slots, registry_sha}` | `model` omitted = general. 409 `registry_changed` on stale sha; 400 `scope_mismatch` / `unknown_model`. Returns the post-write `registry_sha` so a multi-slot save can thread it through sequential writes. |
| `POST` | `/api/prompts/:id/preview` | `{target_words, stage?, model?, slot?}` | `{ok, system_prompt, user_prompt, fingerprint}` | Now returns user prompt and can preview as resolved for a model. |
| `POST` | `/api/prompts/resolve` | `{stage, model, profile?, slot?}` | `{ok, version_id, source, fingerprint}` | New. Debug/verification of §6.3 precedence. |
| `POST` | `/api/prompts/sync` | `{stage?}` | `{ok, copied, production_version, registry_sha, scoped_versions}` | Copies all scoped versions + normalized slots into the CF bundle. |

Error codes: `unknown_prompt_version`, `unknown_placeholder`, `unknown_model`, `unknown_profile`, `scope_mismatch`, `registry_changed`, `version_in_use`, `invalid_template`, `invalid_scope` (each with a human message and, where field-specific, `fieldErrors`).

---

## 9. State flows

- **Prompts tab load:** skeleton → parallel `GET /api/prompts/options` (cached after first load) + `GET /api/prompts` + `GET /api/models` → render slots, versions, effective map. Any single request failing shows an inline error with `Retry`; sections backed by successful requests still render.
- **Dialog open:** fresh draft state → options fetched (or reused) → live preview request in flight shows `Rendering…`; success shows system/user panes + fingerprint; failure shows inline error but keeps Save enabled only when server validation was the failure (draft still validatable) — a transport failure disables Save until preview succeeds or a `Retry preview` is used.
- **Save in flight:** Save button `Saving…`, disabled; duplicate submit blocked (busy guard). Cancel/backdrop/Esc ignored until the request settles.
- **Save success:** close, refetch list, green banner with new id.
- **Save conflict:** `409 registry_changed` → banner `Registry changed elsewhere — reloading`, refetch, keep dialog open with the user's draft so nothing is lost; user re-saves.
- **Empty states:** no models → `Add a model on the Models tab first.` (scope picker disabled); no versions for a scope filter → `No versions in this scope.` with a `+ New version` shortcut; no scoped slot → select shows `(none)` / `(inherit general)`.
- **Offline / server down:** Prompts panel shows existing `Failed to load prompts` + Retry; dialog Save shows a transport error banner and remains retryable.

---

## 10. Edge cases

1. **Legacy registry file** (no `scope`, flat `slots`) → normalizes to general; renders identical; no file rewrite until a save.
2. **Version assigned to a model slot, then that model removed from `/api/models`** → version and slot are preserved (no destructive cleanup); UI shows the raw model id with a `model not registered` warning badge and resolution still works for that model string.
3. **Model-specific version assigned to the general slot** → rejected `400 scope_mismatch`; general slot may only hold general versions.
4. **Project-scoped (profile-qualified) vs model-wide** → when both exist for the same model+stage+slot, the profile-qualified version wins; if the run's profile is unknown, the model-wide version is used.
5. **Editing a version currently assigned to production** → copy-on-write creates a new unassigned version; production continues to serve the old version until explicitly `Set production` (never silently repoints).
6. **Deleting an in-use version** → blocked; UI explains which `stage.model.slot` references it.
7. **Deleting the last version for a scope** → allowed only if unassigned; the slot select falls back to `(none)` meaning "inherit general" for a model scope, or "use baseline" for the general scope.
8. **Unknown placeholder** → save rejected; preview highlights the token and lists the allowlist.
9. **Unbalanced `{{`** → `invalid_template`; no partial substitution.
10. **Template references `{{components}}` but no components selected** → warning `system_template uses {{components}} but no components are selected`; save allowed (renders empty block).
11. **User template for chapter missing `{{source_md}}`** → blocking validation error.
12. **Concurrent slot writes** with stale `expected_sha` → 409; UI reloads and preserves selection.
13. **`sync` with scoped versions** → all versions copied; CF resolves scoped ids independently; `production_status` per `(stage, model)`.
14. **Very long template** → cap at 64 KB per template (system + user each) and 256 KB per version body; `400 payload_too_large`.
15. **Whitespace-only template** → treated as absent (falls back to built-in), not as an empty prompt.
16. **`window.prompt` clone removed** → ensure no remaining call sites; clone always routes through the dialog.

---

## 11. Accessibility

- Dialogs: `role="dialog"`, `aria-modal="true"`, labelled by the dialog title (`aria-labelledby`). Focus moves to the first field on open; focus is trapped within the dialog; `Esc` closes; focus returns to the triggering button on close.
- The Settings modal already sets `aria-modal`; adding a nested dialog must make the underlying modal inert (`aria-hidden` on `#settingsOverlay` while the version dialog owns focus) and restore it on close.
- All form controls have associated `<label>`s; validation errors use `aria-describedby` and `aria-invalid="true"`, and the first invalid field receives focus on failed save.
- Placeholder legend is announced as descriptive text; preview panes use `<pre>` with `role="region"` and an accessible label (`System prompt preview` / `User prompt preview`).
- Scope badges and drift badges use `role="status"`; color is not the only signal (text label present).
- The `+ New version` button has an accessible name; scope filter is a labelled `<select>`.
- Keyboard: full tab order through dialog fields; `Tab` never escapes the dialog; table action buttons are reachable and have discernible names (`aria-label` includes version id for Clone/Delete).

---

## 12. Performance

- `GET /api/prompts` < 200 ms for ≤ 200 versions incl. previews stripped; `GET /api/prompts/options` < 150 ms and cached for the session (invalidate on `registry_changed`).
- Live preview: debounce 400 ms, cancel superseded requests (AbortController); server-side draft render < 250 ms for ≤ 64 KB templates.
- `POST /api/prompts/resolve` < 50 ms (in-memory lookup).
- `sync` copies the registry + CF snapshot atomically; no network calls to GCP.
- Settings modal open/close and nav switching ≤ 100 ms perceived; no layout thrash from resize (CSS only).

---

## 13. Testing

- `tests/test_prompt_template.py` — placeholder extraction order, validation/allowlist, unknown placeholders, unbalanced delimiters, missing values, `{{components}}` expansion, literal-brace handling.
- `tests/test_prompt_registry.py` (extend) — `component_text` override precedence, `system_template`/`user_template` rendering, byte-identical baseline for no-override versions, fingerprint includes new fields, legacy `template` alias, copy-on-write `revise`, scope create/validate, unknown model/profile rejection, slot normalization for legacy files, `scope_mismatch`.
- `tests/test_prompt_resolution.py` — §6.3 precedence matrix (override > permutation > registry model+profile > registry model > registry general > baseline); profile-qualified beats model-wide; unknown profile falls back; `resolve` endpoint shape.
- `tests/test_cf_prompt_version.py` (extend) — scoped version resolution from the bundled registry, `prompt_source`/`prompt_warning` in `meta`, unknown scoped id → 422 (unless inline override), sync round-trip.
- Dashboard tests (`dashboard/tests/`) — dialog open/reset/close, validation mapping from `fieldErrors`, busy guard, copy-on-write edit does not mutate source, scope filter, scope selector saves the right slot, drift per model, and explorer toggles: legacy (empty) runs and legacy preference keys normalize to v1 (`chapter-p1`); the aggregate `scope:model` toggle hides only model-scoped runs.
- Regression: seeded `chapter-p1`/`chapter-p2`/`composer-p1` render SHA unchanged (system and user) before/after.
- All backend tests run offline with the mock generator; no API spend.

---

## 14. Migration & rollout

1. Ship read-path normalization first (legacy files → general scope, legacy slots → general). No writes change until an operator saves.
2. Existing CF callers unaffected: they send an optional `prompt_version`; the bundled registry still resolves general ids. Scoped ids resolve only if present in the synced bundle.
3. Existing permutation overrides keep their priority; a `tune_prompt` run continues to affect only its own profile. (Optional follow-up, out of scope: `tune_prompt --promote-registry` to publish an adopted result as a model-scoped version.)
4. `data/prompts/registry.json` and `cloud_function/prompts/registry.json` are written atomically by `sync`; a failed sync leaves the old files intact.
5. Rollback: revert the dashboard bundle; registry files remain valid under the old reader because extra keys (`scope`, `component_text`, `user_template`) are ignored by the previous `load_registry`/CF resolution path.
