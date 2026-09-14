# Spec: Revised System Prompt (Target-Aware) + Prompt-Version Routing (Local vs Cloud Function)

**Feature:** Consolidate the target-aware length-control contract and the book-summarisation pipeline contract into one **revised chapter/composer system prompt (v2)**, and add a **prompt-version registry with two selectable slots per stage — `local` and `production`** — managed from a new **Settings > Prompts** section. A system-prompt version can be tested locally (or against the Cloud Function via the `prompt_version` request field) without changing what production serves.

**Input needed:** None — complete behavior contract. The two questions at the end of the source pipeline spec are answered in §2.2.

**Status:** Draft for implementation

**Supersedes / consolidates:** `specs/target-aware-length-control.md` (generation-time cap + numeric repair deltas) and the pasted "Automated Book Summarisation & LLM Evaluation Pipeline" brief (reading-time math, sentence budget, token ceiling, prompt template, evaluation matrix). Length-control mechanics are now **shipped** in the tree (`core/reasoning.py::visible_max_tokens`, `candidate_spec.py::render_repair_user(current_word_count=...)`, `build_openrouter_request(max_tokens=...)`, `tests/test_length_control.py`); this spec treats them as baseline and layers the prompt-version system on top.

**Related specs:** `specs/per-model-prompt-tuning.md` (Prompts tab + permutation store — this spec extends that tab and defines precedence), `specs/run-length-best-pass-selection.md` (pass selection; orthogonal), `specs/webapp-serverless-summariser-integration.md` (CF transport).

---

## 1. Goals

1. **One revised prompt, target-aware.** A single chapter-stage system-prompt version (v2) that encodes static length discipline, while every concrete number (`target_words`, `[low, high]`, `S_target` sentence budget) stays in the per-request user prompt so the **system prompt is target-independent and therefore version-stable** (a hard prerequisite for routing a version id to the Cloud Function).
2. **Versioned prompts, two slots.** Every prompt is an immutable, fingerprintable version in a local registry. Each stage (`chapter`, `composer`) has two assignable slots:
   - **`local`** — used by direct/local runs (OpenRouter direct, mock, batch, tuning).
   - **`production`** — the version the Cloud Function serves by default.
3. **Test without touching production.** Selecting a new version into the `local` slot (or passing `--prompt-version` for a single run) exercises the new prompt while the `production` slot and the deployed function are untouched. Production only changes when the operator explicitly promotes a version.
4. **Version id travels to the Cloud Function.** CF requests carry `prompt_version` (+ fingerprint + override flag) so the same function supports both a stable production default and experimental versions/overrides, with the used version recorded in response meta for auditability.
5. **Drift is visible.** Settings > Prompts shows the version currently selected for production, the version the CF last reported, and whether they match.
6. **No regression to shipped length control.** `visible_max_tokens`, numeric repair deltas, best-pass selection, gates (`max_final_length_error_pct=0.10`, 100-word floor), `max_passes=5`, and `max_truncation_retries=1` are unchanged unless this spec says otherwise.
7. **Backward compatible.** Existing profiles, generated `candidate_spec.py`, run manifests, and CF callers that send only `system_prompt`/`user_prompt` keep working byte-for-byte when no version is selected.

## 2. Non-Goals

- No change to `scoring.py` gate thresholds or weights (the pipeline brief's stricter 0.88/0.80 thresholds are exposed as an **optional** scoring profile, not the default — §10.4).
- No change to `max_passes`, `max_truncation_retries`, tolerance values, or best-pass selection.
- No automatic promotion to production. Promotion is always an explicit operator action plus a deploy/sync step.
- No per-profile mutation of `candidate_spec.py` on disk (same invariant as `per-model-prompt-tuning.md`). The registry overlays at render time.
- No new auth provider; the dashboard stays localhost-only and the CF keeps its existing OIDC/`AUTH_MODE` transport.
- No change to `tools/add_candidate.py` profile generation or to `PROFILE_CANDIDATES` contents.
- No re-scoring of historical artifacts.
- No multimodal/PDF ingestion work beyond what already exists (§2.2).

### 2.1 Current system (context)

- **Prompt composition.** `StageConfig.prompt_components: Dict[str,str]` maps policy keys to option ids. `render_chapter_system(spec)` concatenates texts from `CHAPTER_SYSTEM_STYLES`, `DETAIL_POLICIES`, `QUALIFIER_POLICIES`, `STRUCTURE_POLICIES`, `EXAMPLE_POLICIES`, `TERMINOLOGY_POLICIES`, `ANTI_FLUFF_POLICIES`. `render_composer_system(spec)` does the same with composer tables. `render_chapter_user` already carries `Target visible words: N. Acceptable range: L-H.`
- **Per-profile overrides.** `autoresearch/permutation_store.py` persists `data/optimized_prompts/<perm_key>.json`; `candidate_spec.get_candidate()` calls `_apply_permutation_overrides` which updates `stage.prompt_components`. This is the **auto-tuned** layer.
- **Length control (shipped).** `core/run_candidate.py::run_length_controlled_stage` → `generate_pass` computes `visible_max_tokens(attempt_target, effort=stage_reasoning_effort(stage_config), cap=stage.max_tokens)` and passes `max_tokens=` to `build_openrouter_request`. Repair prompts carry `current_word_count` and an exact cut/add delta.
- **Local vs CF transport.** `make_client(args)` returns `CloudFunctionClient` when `FUNCTION_URL` is set (or `--function-url`), else `OpenRouterClient.from_env`. `CFClient.chat_completion(payload)` extracts `messages[0]` (system) and `messages[1]` (user) into `body["system_prompt"]`/`body["user_prompt"]` and posts to the function. `cloud_function/handler.py::_build_openrouter_payload` rebuilds the OpenRouter payload from those strings. The CF has **no prompt-version awareness today**; it uses exactly the strings it is given.
- **Dashboard.** Settings nav has a disabled `Prompts` placeholder (`settings.js`, `data-section="upcoming"`). `per-model-prompt-tuning.md` specifies enabling it with a tune list backed by `/api/optimized-prompts`. This spec adds the version registry and slot controls to the same tab.
- **Version metadata.** `core/versioning.py::build_prompt_hashes` records `chapter_system_prompt_sha256` / `composer_system_prompt_sha256` (full rendered text) plus per-function source hashes in every run manifest.

### 2.2 Answers to the pipeline brief's open questions

- **Raw book format:** canonical input is the existing per-book directory under `data/books/<slug>/` — `book.json` (already chapter-split), `toc.md`, `metadata.md`, `book.epub`. `run_candidate` consumes the chapter JSON/markdown; the pipeline does not need new PDF parsing in v2. If a flat TXT/EPUB is supplied, it is ingested through the existing book builder into the same `book.json` shape before the math engine runs.
- **Language/runtime:** **Python CLI** (this repo). The dashboard is a thin JS control surface that shells out to Python tools; no Node pipeline.

---

## 3. Canonical length math (consolidated)

This section makes the pipeline brief's math authoritative and reconciles it with the shipped `visible_max_tokens` cap. All rates share one constant so source and target use the same clock.

### 3.1 Reading-time and reduction metrics

- Baseline reading speed: `words_per_minute = 200` (already `BudgetAllocatorConfig.words_per_minute`).
- `W_total` = sum of visible words across all chapters (`BookContext.total_visible_words`).
- `W_target = minutes × 200` → **6,000** (30m) / **12,000** (60m), then capped by `min(W_target, round(W_total × max_summary_to_source_ratio))` (existing `final_book_target_words`).
- `R = W_target / W_total` (reduction factor; informational, `0 < R ≤ 0.9`).

### 3.2 Per-chapter targets (must match the existing allocator)

The shipped allocator (`allocate_chapter_targets`, `BudgetAllocatorConfig`) already distributes `W_target` across chapters by original share with min/max chapter share clamps and a 30m/60m stage multiplier. For each chapter `i`:

- `W_chapter_target_i` = allocator output for that chapter (integer, ≥ 1).
- `W_chapter_original_i` = visible words of the source chapter.
- Sentence budget: `S_target_i = max(1, round(W_chapter_target_i / 18))`, assuming the standard nonfiction sentence length of **18 words**.
- Request token ceiling (pipeline brief's `T_max`):
  `T_max_i = round(W_chapter_target_i × 1.35 × 1.15)` (1.35 word→token multiplier + 15% truncation buffer).

### 3.3 Effective generation cap (reconciled, single rule)

The shipped `visible_max_tokens(W, effort, headroom=1.6, cap=stage.max_tokens)` is the canonical cap. The pipeline brief's `T_max_i` is retained as a **truncation floor** so a short sentence budget can never starve the JSON payload. Effective per-pass cap:

```
base          = visible_max_tokens(attempt_target, effort=stage_reasoning_effort(stage), cap=stage.max_tokens)
t_max_floor   = round(attempt_target * 1.35 * 1.15)
effective_cap = min(stage.max_tokens, max(base, t_max_floor))     # never exceeds the profile budget
```

- `base` already inflates by `1/(1−effort_fraction)` for reasoning models; `t_max_floor` does not (it is a visible-output floor).
- `attempt_target` is the current pass target, so truncation retries (0.7×) re-derive both terms automatically — unchanged retry loop.
- For every profile currently in `chapter_fast-v3`, `base ≥ t_max_floor`, so v2 behavior equals the shipped cap. The floor only matters if a future profile sets a very tight `max_tokens`.

### 3.4 Hyperparameters

Global request defaults remain `temperature=0.1` for chapter/composer generation (low to minimize hallucination) and `top_p=0.9` where the provider supports it. **Note:** the repo's generated profiles currently store `temperature=0.2`; v2 does not silently rewrite them. The recommended profile default moves to `0.1`, applied through the existing profile generator only when a new profile is created, and configurable per profile in the Models dialog. `top_p` is omitted unless a profile sets it via `extra_body`/provider route (unused today).

---

## 4. Revised system prompt v2 (the actual prompt)

### 4.1 Design rule

> The **system prompt carries only target-independent rules and is versioned**; every number (`W_chapter_target`, `[low, high]`, `S_target`) is injected by `render_chapter_user` / `render_composer_user` at request time.

This is what makes `prompt_version` meaningful: the same version text is valid for every chapter and every target, so the CF can resolve it from an id.

### 4.2 New component dimension: `length_policy`

Add `LENGTH_POLICIES: Dict[str, str]` to `candidate_spec.py` and include it in `render_chapter_system` (key `length_policy`) and `render_composer_system`. Options:

- `explicit_budget_first` **(v2 default)** — *"Plan the entire summary against the stated word and sentence budgets before writing a single sentence. The request gives an exact target word count, an acceptable range, and a sentence budget. Obey the word range. Treat the sentence budget as the structural limit: never write more sentences than the budget. Before finishing, count your visible words and sentences and trim or expand to land inside the stated range."*
- `outline_then_fill` — *"Before writing, size each section so its words sum to the stated budget, then fill them in order while keeping a running word count."*
- `verbatim_target_echo` — *"Immediately before writing, restate the numeric target and the remaining word budget, then write to it."*
- `sentence_budget_strict` (pipeline-brief fidelity) — *"You will be given an exact sentence budget S. Write EXACTLY S sentences total. Do not exceed this sentence count under any circumstance. Prefer the target word count; the sentence count is the hard constraint."*

Profiles without `length_policy` render exactly as today (forward-compatible overlay, per `per-model-prompt-tuning.md` §6). The v2 default version sets `length_policy: explicit_budget_first`.

### 4.3 Revised chapter system prompt (template)

`render_chapter_system(spec, *, prompt_version=None)` resolves the effective component set
(`base(profile) ← permutation override ← registry version components`) and concatenates, in order:

```
{CHAPTER_SYSTEM_STYLES[system_style]}

{DETAIL_POLICIES[detail_policy]}

{QUALIFIER_POLICIES[qualifier_policy]}

{STRUCTURE_POLICIES[structure_policy]}

{EXAMPLE_POLICIES[example_policy]}

{TERMINOLOGY_POLICIES[terminology_policy]}

{ANTI_FLUFF_POLICIES[anti_fluff_policy]}

{LENGTH_POLICIES[length_policy]}

Output contract: respond only with JSON matching the summary_response schema; summary_md holds the summary markdown and estimated_visible_words holds the visible word count of summary_md.
```

When `render_chapter_system` is called with a registry version, the components are taken from the version (missing keys fall back to the profile). When no version is selected, output is byte-identical to today plus the new `length_policy` text (which the v1 baseline does not set, so v1 is unchanged).

### 4.4 Revised chapter user prompt (numeric contract)

`render_chapter_user` extends its existing target block to:

```
Target visible words: {W_chapter_target}. Acceptable range: {low}-{high}. Target sentences: {S_target}
(about 18 words each). Try to land inside the word range on the first pass; do not exceed {S_target} sentences.
```

- `low, high = visible_word_range(W_chapter_target, spec.length_control.tolerance_pct)` (existing, default `±8%` for chapter/composer).
- `S_target` is emitted only when the effective component set includes a `length_policy`; legacy profiles omit the sentence line (byte-identical).
- Existing required-behavior block, format instructions, context, and source markdown stay in the same positions.

### 4.5 Revised repair prompts

Keep the shipped numeric delta block (`current_word_count`) verbatim. On top of it, when `S_target` is available, append one sentence to the delta block: *"The sentence budget is {S_target}; adjust sentence count as well as word count."* `None` still omits the whole block (resume byte-compatibility).

### 4.6 Composer prompt

`render_composer_system` gains the same `length_policy` dimension (default `explicit_budget_first` for v2). `render_composer_user` already emits target + range; add the same `Target sentences: {S_target}` line for the composer's target. `render_composer_repair_user` gains the sentence clause. No structural change to `retrieved_source_excerpts` placement.

### 4.7 Model classes (evaluation matrix)

The pipeline brief's frontier/flash split maps onto existing profiles; v2 does not introduce a new model-config surface. The evaluation matrix is produced by running the same revised prompt across:

- **Frontier (accuracy):** the registered Anthropic/OpenAI-class profiles.
- **Flash (cost/speed):** the registered Google/DeepSeek-class profiles.

Matrix rows are produced by the existing `run_candidate` harness; the version id and fingerprint are recorded per run so results are attributable to a prompt version.

---

## 5. Prompt-version registry (data model)

### 5.1 New module

`core/prompt_registry.py` (pure stdlib file IO; no network). Public API:

```
fingerprint(text: str) -> str                       # sha256(text)[:16]
load_registry(path: Path | None = None) -> dict
save_registry(registry: dict, path: Path | None = None) -> None   # atomic tmp+replace
list_versions(stage: str | None = None) -> list[dict]
get_version(version_id: str) -> dict | None
create_version(*, stage, label, components, template=None, notes="") -> dict
set_slot(*, stage, slot, version_id) -> None        # slot in {"local","production"}
get_slot(*, stage, slot) -> str | None
resolve_components(*, stage, profile_components, prompt_version=None) -> dict
render_system(*, spec, stage_kind, prompt_version=None) -> tuple[str, str]  # (text, fingerprint)
```

Default registry path `data/prompts/registry.json` (env `PROMPT_REGISTRY_PATH` overrides; relative to cwd like `permutation_store.PROMO_DIR`).

### 5.2 Registry schema (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "registry_sha": "<sha256 of stable-json of versions+slots>",
  "updated_at": "2026-09-12T00:00:00Z",
  "versions": [
    {
      "id": "chapter-p2",
      "stage": "chapter",
      "label": "v2 target-aware (default)",
      "components": {
        "system_style": "dense_faithful",
        "detail_policy": "mechanisms_first",
        "qualifier_policy": "strict",
        "structure_policy": "heading_aware",
        "example_policy": "explanatory_only",
        "terminology_policy": "keep_source_terms",
        "anti_fluff_policy": "hard",
        "length_policy": "explicit_budget_first"
      },
      "template": null,
      "system_prompt_sha256": "<sha256 of render with baseline profile>",
      "system_prompt_fingerprint": "a1b2c3d4e5f60718",
      "created_at": "2026-09-12T00:00:00Z",
      "created_by": "operator",
      "notes": "Consolidates target-aware length control; static rules only.",
      "immutable": true
    }
  ],
  "slots": {
    "chapter":  { "local": "chapter-p2", "production": "chapter-p1" },
    "composer": { "local": "composer-p1", "production": "composer-p1" }
  }
}
```

- **Version ids** are `"{stage}-p{N}"` with `N` monotonic per stage. A content fingerprint (`sha256[:16]` of the rendered system prompt) is stored for drift comparison; it is not the identity.
- **Immutability.** `components`/`template` never change after creation. Editing content creates a new version (the UI "Save as new version" path). Only `label`/`notes` may be `PUT`.
- **Seeding.** On first load with no registry, `core/prompt_registry.py` seeds:
  - `chapter-p1` — the current shared baseline components (no `length_policy`), fingerprint of the v1 render.
  - `chapter-p2` — baseline components + `length_policy: explicit_budget_first` (the revised v2 prompt).
  - `composer-p1` — current composer baseline.
  - Slots: `local = production = p1` for both stages. v2 is available but **not** assigned, so shipping this change does not silently alter production output.
- **Ordering guarantee:** any deterministic render must depend only on (`components`, profile fallback order), so `system_prompt_sha256` is reproducible across machines.

### 5.3 Relationship to the permutation store

Three layers, resolved in this precedence (low → high):

1. **Profile baseline** — `StageConfig.prompt_components` from `candidate_spec.py`.
2. **Permutation override** — `data/optimized_prompts/<perm_key>.json`, auto-tuned per profile (existing `_apply_permutation_overrides`).
3. **Registry version overlay** — explicit slot/`--prompt-version` selection; its component keys replace layers 1–2 for those keys.

The registry does not read or write the permutation store. `get_prompt_override` continuing to work is unaffected.

---

## 6. Slot routing: local vs production

### 6.1 Resolution order

For a generation run, the effective chapter prompt version is:

1. `--prompt-version <id>` / `PROMPT_VERSION` env, if provided (single-run test override); else
2. `--prompt-slot <local|production>` (default `local` for direct runs) → `slots[stage][slot]`; else
3. none (legacy: rendered from profile + permutation components only).

For a **Cloud Function** run the client resolves version as in 1–2 (default slot `production` when `FUNCTION_URL` is set and no override is given, because CF is the production path), renders the system prompt locally as today, and additionally sends:
`prompt_version`, `prompt_fingerprint`, `prompt_stage`, and `allow_prompt_override` (§7.2).

### 6.2 Local run behavior

- `make_client` is unchanged; local runs use the `local` slot by default.
- `run_candidate` computes `(system_prompt, fingerprint)` via `core/prompt_registry.render_system` for chapter and composer, and passes `prompt_version` through to fetch/render.
- `mock=True` still builds the request; the mock generator ignores it. No behavioral change.
- `batch_summarize.py` gains the same flags and renders via the registry.

### 6.3 Production behavior

- The **production slot is a pointer in the registry**, not a deploy trigger. Changing it affects the next CF call only to the extent the CF registry knows that version (see §7).
- When `FUNCTION_URL` is set and no override is passed, the client targets `slots[stage].production`. It records the resolved id + fingerprint in the run manifest.
- The CF's bundled registry is a snapshot. If the client's production pointer names a version the CF does not know, and override is not allowed, the call fails with `422 unknown_prompt_version` (§7.3). This prevents silent fallback to a stale prompt.

---

## 7. Cloud Function contract

### 7.1 Bundled registry

- `cloud_function/prompts/registry.json` is a snapshot of `data/prompts/registry.json` copied at deploy/sync time. Path overridable via `PROMPT_REGISTRY_PATH`; production pointer overridable via `PROMPT_PRODUCTION_VERSION`.
- `tools/sync_prompts.py` copies the local registry into the bundle and stamps the production slot version. The dashboard "Promote to production" action calls it (or launches it as a job).
- The function loads the registry once at cold start (memoized) and records `registry_sha` in every response meta.

### 7.2 New request fields (`run_summarize` / `_build_openrouter_payload`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `prompt_stage` | `"chapter"\|"composer"` | `"chapter"` | Which registry stage to resolve. |
| `prompt_version` | string | `""` | Registry id. When set, CF resolves the system prompt from its bundled registry. |
| `prompt_fingerprint` | string | `""` | Client's `sha256[:16]` of the rendered system prompt. Used for verification/drift only. |
| `system_prompt_override` | string | `""` | Raw system prompt for experiments not present in the CF registry. Honored only when `allow_prompt_override` is true. |
| `allow_prompt_override` | bool | `false` | Explicit opt-in required for inline overrides. |

Resolution precedence in the CF:

1. `system_prompt_override` non-empty **and** `allow_prompt_override` true → use it; `prompt_source = "inline_override"`.
2. `prompt_version` known in the bundled registry → use its rendering; `prompt_source = "registry"`. If `prompt_fingerprint` is non-empty and differs from the version's fingerprint, set `prompt_warning = "fingerprint_mismatch"` but proceed with the registry text (registry is authoritative).
3. `prompt_version` set but unknown:
   - if `allow_prompt_override` true and the request's `system_prompt` is non-empty → use the incoming `system_prompt`, `prompt_source = "inline_override"`, `prompt_warning = "unknown_version"`;
   - else → HTTP **422** `{success:false, error:"unknown_prompt_version", allowed:[...ids]}`.
4. No `prompt_version`, no override → use incoming `system_prompt` (legacy behavior); if empty, use `slots[stage].production` from the bundled registry; `prompt_source = "request"` or `"production_default"` respectively.

The user prompt is **not** resolved by the CF: it is always the request's `user_prompt` (it embeds per-chapter numbers). The client should still send the rendered `system_prompt` for legacy compatibility; the CF prefers `prompt_version` when present. (This is what allows a new local version to be tested against the live function: send `prompt_version` + `allow_prompt_override` and the CF uses the passed text.)

### 7.3 Response meta (additive)

`cloud_function/handler.py::_meta_block()` extends to:

```json
{
  "scoring_version": "...",
  "handler_version": "...",
  "deployed_at_utc": "...",
  "prompt_registry_sha": "<sha>",
  "prompt_version": "chapter-p2",
  "prompt_fingerprint": "a1b2c3d4e5f60718",
  "prompt_source": "registry | inline_override | request | production_default",
  "prompt_warning": ""
}
```

`core/cf_client.py` records `last_meta` (already parsed) and `run_candidate` writes it under `run_manifest["cloud_function_meta"]` (already present at the call site) plus `run_manifest["prompt_version"]`.

### 7.4 Validation and errors

- Missing `source_md` / `model` → unchanged 400.
- `prompt_stage` not in `{chapter, composer}` → 400.
- Unknown `prompt_version` without override permission → 422 (above).
- `allow_prompt_override=true` but no `system_prompt_override` and unknown version and no incoming `system_prompt` → 422.
- Backwards compat: an old client sending only `system_prompt`/`user_prompt` sees identical behavior (`prompt_source="request"`).

---

## 8. Settings > Prompts (UI behavior contract)

Enable the disabled nav item (`settings.js`, `data-section="prompts"`) and add `#settingsPromptsSection`. No pixel spec; behavior only. It composes with the per-model tune list from `per-model-prompt-tuning.md` (tuning remains its own subsection).

### 8.1 Section layout

1. **Slot assignment panel (top).** Two rows (one per stage: Chapter, Composer), each with:
   - a `local` select (versions for that stage),
   - a `production` select (versions for that stage),
   - a `Save` button (disabled until dirty),
   - a drift badge: `In sync` when the selected production fingerprint equals the CF's last-reported `prompt_fingerprint` for that stage; `Not deployed` otherwise (with the last-reported id/fingerprint and timestamp).
   - Selecting the same version in both slots is allowed.
2. **Version list (table).** Columns: id, label, stage, `length_policy` (if any), fingerprint (abbreviated, full on title), assigned slots, created, actions.
   - Row actions: **Preview**, **Set local**, **Set production**, **Clone as new version**, **Delete** (disabled when assigned to any slot).
   - `Set production` writes the slot and shows the notice *"Production now points at `<id>` — run Sync/Promote to deploy it to the Cloud Function."* It does **not** deploy.
3. **Create version dialog.** Stage select; label; a component editor (one control per dimension, options from the maps exposed by a new `GET /api/prompts/options`); optional raw system-prompt template override; a live rendered preview showing the exact system prompt text and fingerprint; Save → `POST /api/prompts`.
4. **Test panel.** "Run smoke test with this version" launches `run_candidate` with `--prompt-version <id> --max-samples 4 --mock` (default) or, with a checkbox, against the Cloud Function (`--function-url` from env, `allow-prompt-override` implied). Shows the job streaming in Run data.
5. **Per-model tuning subsection** — rendered as specified by `per-model-prompt-tuning.md` §4.1 (unchanged).

### 8.2 States

- **Loading:** skeleton rows; slots disabled.
- **Empty (no registry):** seed (§5.2) then render; if seeding fails, inline error with Retry.
- **Error:** `GET /api/prompts` failure shows an inline error and a Retry; the per-model tune subsection still renders (independent request).
- **Offline / server down:** same offline banner pattern as Run data (`Cannot reach dashboard server`).
- **CF drift unknown:** if no CF run has been recorded, drift badge reads `Never reported` (neutral, not an error).
- **Save conflict:** two tabs save slots concurrently → the second gets `409 registry_changed` and must reload; no silent overwrite.

### 8.3 Accessibility

- Nav item is a real `<button>` with active/`aria-current` state.
- Selects have explicit `<label for>`; drift badges are text + icon (not color alone) and use `role="status"`.
- Version table uses real `<table>` headers and a caption.
- Destructive/irreversible-looking actions (Delete, Set production) use the existing two-step confirm pattern.
- Preview uses `<pre>` with a copy button; fingerprint is selectable text.
- Respect `prefers-reduced-motion` (existing console conventions).

---

## 9. API contracts (dashboard dev server)

All JSON, same-origin only (no CORS widening). Mutating endpoints inherit the existing local dashboard trust model.

| Method | Path | Request | Response |
|---|---|---|---|
| `GET` | `/api/prompts` | `?stage=` optional | `{ok, registry_sha, versions:[...], slots:{...}, production_status:[{stage, selected_id, reported_id, reported_fingerprint, reported_at, in_sync}]}` |
| `GET` | `/api/prompts/options` | — | `{ok, dimensions:{system_style:[...], detail_policy:[...], ..., length_policy:[...]}, stage_keys:{...}}` |
| `GET` | `/api/prompts/:id` | — | `{ok, version, rendered_system_prompt, system_prompt_sha256, fingerprint}` |
| `POST` | `/api/prompts` | `{stage, label, components, template?, notes?}` | `{ok, version}` (201 semantics; validation errors 400 with `fieldErrors`) |
| `PUT` | `/api/prompts/:id` | `{label?, notes?}` | `{ok, version}`; 409 if body attempts to change content |
| `DELETE` | `/api/prompts/:id` | — | `{ok}`; 409 `version_in_use` if assigned to any slot |
| `POST` | `/api/prompts/slots` | `{stage, slot, version_id}` | `{ok, slots}`; 409 `registry_changed` on stale write; 400 unknown version/stage/slot |
| `POST` | `/api/prompts/:id/preview` | `{target_words, stage?}` | `{ok, system_prompt, user_prompt, fingerprint}` (renders with the selected version against a sample target) |
| `POST` | `/api/prompts/sync` | `{stage?}` | `{ok, copied, production_version, registry_sha}` — runs `tools/sync_prompts.py` into the CF bundle; explicit, never automatic |

The dashboard reads `data/prompts/registry.json` directly (`ctx.dataDir`) for GETs and routes writes through `core/prompt_registry.py` via `pythonRunner` to keep atomic-write and validation logic in one place.

---

## 10. Evaluation & logging (consolidated)

### 10.1 Per-model-pass metrics

For every model pass, the pipeline records into the run manifest (extend existing `samples[]`/`usage`):

1. **Model** — OpenRouter id or CF-resolved model id.
2. **Latency** — wall-clock seconds from request to complete response. **New:** record `latency_seconds` per pass and `mean_latency_seconds` per dataset. Direct client measures around `chat_completion`; CF places it in `usage`/meta.
3. **Cost** — existing `generation_cost` / `uncached_generation_cost`.
4. **Length deviation** — `D = |W_actual − W_chapter_target| / W_chapter_target`, already `deterministic.final_length_error_pct` (and `first_pass_length_error_pct`).
5. **Prompt attribution** — `prompt_version`, `prompt_fingerprint`, `prompt_source`, `prompt_registry_sha`.

### 10.2 Accuracy benchmarking

- Faithfulness/alignment and concept-coverage are already produced by `scoring.py` (deterministic proxies) and optional LLM-judge (`core/judge.py`).
- The pipeline brief's targets (`faithfulness > 0.88`, `context recall/coverage > 0.80`) are exposed as an **optional named scoring profile** (`strict-v1`) selectable per run; the default gate profile keeps `min_faithfulness=0.70` / `min_concept_coverage=0.60` and `max_final_length_error_pct=0.10`. No default moves without a separate decision.
  - **Implemented as** `--scoring-profile {auto|default|strict-v1}` on `run_candidate.py` (default `auto` = unchanged budget-derived gates), backed by named `scoring_gates` entries in the benchmark manifest: `strict-v1` (judged: 0.88/0.80) and `strict-v1_deterministic` (semantic gates `null`, since deterministic proxies are not calibrated to the judged bar). Unknown names fail fast before any API spend. The resolved key is recorded as `run_manifest.scoring_gate_key`; `run_manifest.scoring_profile` records the request. New named profiles are merged into pre-existing on-disk manifests on load without overwriting operator tuning.

### 10.3 Test matrix output

`run_candidate` continues to write run manifests + `results.tsv`; each row gains `prompt_version` and `prompt_fingerprint` columns so the matrix can be grouped by prompt version (the brief's "CSV or database"). JSONL run manifests remain the detail store.

---

## 11. CLI & tooling

### 11.1 `run_candidate.py` / `batch_summarize.py` new flags

| Flag | Default | Meaning |
|---|---|---|
| `--prompt-version <id>` | `""` | Force a registry version for this run (both stages unless `--prompt-stage` given). |
| `--prompt-stage <chapter\|composer\|both>` | `both` | Which stage the override applies to. |
| `--prompt-slot <local\|production>` | `local` (CF runs: `production` when `FUNCTION_URL` set) | Slot to resolve when no explicit version. |
| `--allow-prompt-override` | false | Forward `allow_prompt_override=true` to the CF (enables inline test overrides). |

Env fallbacks: `PROMPT_VERSION`, `PROMPT_SLOT`, `PROMPT_PRODUCTION_VERSION`, `PROMPT_REGISTRY_PATH`.

### 11.2 Tools / registry entries

- `tools/sync_prompts.py` — copies `data/prompts/registry.json` → `cloud_function/prompts/registry.json`, validates the production version exists, prints a diff of ids/fingerprints. Registered in `SCRIPT_REGISTRY` (`id: sync_prompts`) so it runs from Run data.
- `autoresearch/tune_prompt.py` (per `per-model-prompt-tuning.md`) resolves + records the prompt version it started from; its adopted result can be exported **as a new registry version** ("Promote tuning result to version") in the Prompts tab.

### 11.3 Version-hash integration

`core/versioning.py::build_prompt_hashes` gains `prompt_version`, `prompt_fingerprint`, `prompt_source`, `prompt_registry_sha` inputs (kept out of `candidate_dict_sha256` to avoid invalidating every existing profile hash). Existing `chapter_system_prompt_sha256` continues to hash the actual rendered text and thereby changes when a version is selected — correct attribution.

---

## 12. State flows

- **Local test of a new prompt:**
  1. Create `chapter-p3` in the dialog → registry write.
  2. `Set local` → `slots.chapter.local = chapter-p3`.
  3. Run `run_candidate --bench chapter_fast --profile all` (current default slot `local`).
  4. Manifest records `chapter-p3` + fingerprint. Production function untouched; `slots.chapter.production` still `chapter-p1/p2`.
- **CF test of a new prompt without deploy:**
  1. Create version locally.
  2. Run with `--prompt-version chapter-p3 --allow-prompt-override` and `FUNCTION_URL` set.
  3. Client sends `prompt_version=chapter-p3`, `allow_prompt_override=true`, rendered `system_prompt`.
  4. CF logs `prompt_source="inline_override"` and `prompt_warning="unknown_version"`; uses the rendered text. No redeploy.
- **Promotion:**
  1. `Set production` → pointer in local registry.
  2. `Sync` → copies registry into CF bundle.
  3. Redeploy the function (existing deploy path). Until redeploy, CF reports the old `prompt_registry_sha`; the drift badge stays `Not deployed`.
  4. After redeploy, a smoke call reports the new fingerprint → `In sync`.
- **Revert:** `Set production` back to the prior version and Sync/redeploy. Local registry history is retained because content is immutable; pointer changes are the only mutation.

---

## 13. Edge cases

- **Unknown version id in CLI** → fail fast before any API spend, with the list of known ids for the stage.
- **Version assigned to a slot then deleted** → Delete returns 409; the UI requires reassignment first.
- **Registry file corrupt/unreadable** → loader returns an empty structure and logs a warning; the dashboard shows an error + Retry; no write occurs until a valid registry is loaded (mirrors permutation-store behavior).
- **Concurrent slot writes** → last-writer detection via `registry_sha` precondition; `409 registry_changed`, no partial write.
- **CF registry stale / version unknown in prod** → 422 (not silent fallback); CLI surfaces the allowed ids and the CF's `prompt_registry_sha`.
- **Fingerprint mismatch** (client rendered text differs from registry text) → CF proceeds with registry text + `prompt_warning="fingerprint_mismatch"`; run manifest records the warning.
- **Legacy caller** (no version fields) → byte-identical CF behavior; `prompt_source="request"`.
- **Reasoning profiles** → `visible_max_tokens` inflation unchanged; sentence-budget prompt does not alter the reasoning reservation.
- **Tiny targets** → `DEFAULT_MIN_OUTPUT_TOKENS` floor (512) and `t_max_floor` keep the request viable; `S_target` floors at 1.
- **Resume mid-stage** → prompt version is resolved at stage start and stored on the stage state only as a run-manifest field (not parsed for correctness); on resume, the same registry selection is re-resolved. If the slot changed between resume attempts, the manifest records the new version and a `prompt_version_changed_on_resume` warning. No checkpoint migration.
- **Composer-only version** → slots are independent per stage; selecting `chapter-p2` does not affect composer.
- **Mock runs** → cap and prompt rendering still occur; mock generator ignores them (unchanged).
- **Empty `data/prompts/`** → seeding writes the four baseline versions; if the filesystem is read-only, run proceeds with profile/permutation components only (`prompt_version=""`).

---

## 14. Auth & permissions

- Dashboard endpoints are same-origin localhost only; mutating prompt endpoints inherit the existing dev-server model. No credentials are stored in the registry.
- `POST /api/prompts/sync` and the CF deploy step are separate explicit actions; sync only writes bundle files and never calls GCP.
- CF `prompt_version` resolution never widens access; `allow_prompt_override` is an explicit per-request opt-in and is logged (`prompt_source`) so prod overrides are auditable.
- `data/prompts/registry.json` and `cloud_function/prompts/registry.json` contain no secrets and are safe to commit; `PROMPT_REGISTRY_PATH` is path config only.

---

## 15. Performance targets

- `GET /api/prompts` < 200 ms for ≤ 200 versions (single file read + parse).
- `render_system` is O(#components) and memoizable per `(profile, version)`; registry parsing once per process.
- CF cold-start registry load < 20 ms; no per-request disk read beyond the memoized registry.
- No LLM calls are triggered by loading the Prompts tab. The Test panel only launches an explicit job.
- Sync copies one small JSON file; no full repo scan.

---

## 16. Tests (offline, no network)

New `tests/test_prompt_registry.py`:

1. **Seeding**: empty dir → `chapter-p1`, `chapter-p2`, `composer-p1`; slots `local=production=p1`; `chapter-p2` includes `length_policy`.
2. **Fingerprint determinism**: same components → same `system_prompt_sha256` and `fingerprint` across repeated renders; different `length_policy` → different hash.
3. **Immutability**: `PUT` changing `components` → 409; label/notes allowed.
4. **Precedence**: registry overlay beats permutation override beats profile; missing version keys fall back to profile; no-version render is byte-identical to the current baseline (golden string), and profile-with-`length_policy` render matches the v2 template.
5. **Slot resolution**: `resolve_system(stage, prompt_version=...)`; explicit > slot > none; unknown explicit id raises before any request.
6. **V2 prompt content**: rendered v2 text contains the `explicit_budget_first` sentence; `render_chapter_user` with `length_policy` emits `Target sentences: {S}`; without it, no sentence line.
7. **Effective cap reconciliation**: `min(stage.max_tokens, max(visible_max_tokens(...), round(target*1.35*1.15)))` matches the shipped cap for representative `chapter_fast-v3` targets; never exceeds `stage.max_tokens`; respects truncation retry inputs.
8. **Latency field** appears per pass and aggregates as `mean_latency_seconds`.

New `tests/test_cf_prompt_version.py` (handler-level, mock OpenRouter client):

9. `prompt_version` known → CF uses registry text, `prompt_source="registry"`.
10. Unknown version + `allow_prompt_override=true` + `system_prompt` → inline override, warning `unknown_version`.
11. Unknown version without opt-in → 422 `unknown_prompt_version`.
12. No version fields → `prompt_source="request"`, legacy payload byte-identical.
13. Fingerprint mismatch → proceeds + `prompt_warning`.
14. Empty system prompt + no version → `production_default` from bundled registry.

Dashboard tests (existing harness):

15. `GET /api/prompts` shape + drift fields; slot save round-trip; stale write → 409; delete-in-use → 409.
16. Version table renders empty/loading/error/offline states; Sync button calls the tool once and shows result.

`Makefile` `test:` gains both new suites.

---

## 17. Acceptance criteria

- [ ] Registry seeds without touching existing behavior: with no `--prompt-version`/slot change, a profile run produces a system prompt byte-identical to pre-change (excluding the intentionally new `length_policy` only when a version selects it).
- [ ] `chapter-p2` renders the revised prompt (§4.3–4.4) with `explicit_budget_first`; its fingerprint is stable and recorded in the run manifest.
- [ ] Settings > Prompts exposes independent `local` and `production` selects per stage; Save persists; Delete-in-use and stale writes are rejected.
- [ ] A new version can be tested (a) locally via the `local` slot and (b) against the **live** Cloud Function via `--prompt-version` + `--allow-prompt-override`, without redeploying the function.
- [ ] Production default only changes after an explicit `Set production` + `Sync` + redeploy; until then the drift badge reads `Not deployed`.
- [ ] CF request/response carry `prompt_version`/`prompt_fingerprint`/`prompt_source`/`prompt_registry_sha`; unknown version without opt-in returns 422.
- [ ] Legacy callers (only `system_prompt`/`user_prompt`) are byte-compatible.
- [ ] Per-pass `latency_seconds` and dataset `mean_latency_seconds` are logged; run manifest rows are groupable by `prompt_version`.
- [ ] `make test` green including `tests/test_prompt_registry.py` and `tests/test_cf_prompt_version.py`.
- [ ] Shipped length control unaffected: the luna re-run acceptance from `target-aware-length-control.md` still holds (`hard_fail_rate 0.0`, mean passes ≤ ~3, no truncation spike).

---

## 18. Verification plan (post-implementation)

1. `make test` (existing suites + the two new files).
2. Golden-render check: v1 baseline hash unchanged; v2 hash recorded; diff the two system prompts to confirm the only addition is `length_policy` text.
3. Local A/B: run `chapter_fast-v3` for one flash and one frontier profile with `--prompt-slot local` where `local=chapter-p1`, then set `local=chapter-p2` and re-run. Compare `final_hit_rate`, `first_pass_hit_rate`, `mean_passes_used`, `mean_final_length_error_pct`, `mean_latency_seconds`, cost.
4. CF inline test: create `chapter-p99`, run one sample with `--prompt-version chapter-p99 --allow-prompt-override`; assert response `prompt_source="inline_override"` and unchanged `prompt_registry_sha`.
5. Promotion drill: `Set production → Sync → deploy → smoke`; assert drift goes `In sync` and the served fingerprint matches.
6. Legacy check: call the deployed CF with `cloud_function/sample_request.json` (no version fields) and diff the response against the pre-change baseline.
7. Confirm the deployed production prompt was never altered by steps 3–5 until the explicit promotion.

---

## 19. Out of scope

- Changing gate thresholds, weights, `max_passes`, `max_truncation_retries`, or best-pass selection.
- Auto-promotion, auto-sync, or auto-deploy.
- New model providers or profile generation changes.
- PDF/EPUB parsing beyond the existing book builder.
- Moving the default scoring thresholds to the pipeline brief's 0.88/0.80.
- Re-scoring or re-running historical artifacts.
- Multi-user accounts, RBAC, or remote dashboard hosting.

---

## 20. Open questions (needed before implementation, non-blocking for the contract)

1. **`top_p`**: keep omitted (recommended; `temperature=0.1` is the length/accuracy lever) or expose per profile? — recommended omitted.
2. **Latency source**: direct client wall-clock around `chat_completion` is the contract; if provider-reported latency is preferred, that is a follow-up that does not change this spec's fields.
3. **CF deploy mechanism**: whether `Sync` should also trigger a deploy command in the same job or only stage the bundle. This spec stages only; deploy stays the existing manual/skill step.

---

## 21. Explorer Controls: system-prompt-version toggles

Companion to §8. §8 is the *authoring/assignment* surface (Settings > Prompts). This section is the *analysis* surface: a toggle-button field in the **explorer Controls sidebar** (`dashboard/index.html`), sitting **above the existing `Show` field and therefore above `Reasoning`**, that filters the run scatter/plots by the system-prompt version each run used. It reuses the exact interaction model of the existing `Reasoning` toggles.

### 21.1 Placement (markup contract)

In `dashboard/index.html`, inside `<div class="controls">`, insert a new `.field` **between the `Fixed Y: 0–1` field (currently line 146–150) and the `Show` field (line 152–164)**, i.e. immediately above `Show` and `Reasoning`:

```html
<div class="field">
  <label class="small">System prompt</label>
  <div class="judge-toggles prompt-toggles" id="promptVersionToggles"></div>
</div>
```

- Keep the `Show` and `Reasoning` fields and their relative order unchanged below it.
- `id="promptVersionToggles"` is the only new DOM hook. `.prompt-toggles` reuses the existing `.judge-toggles`/`.reason-toggles` flex-wrap styling (no new CSS required; an optional `.prompt-toggles` rule may add `row-gap` consistent with `.reason-toggles`).

### 21.2 Data source

- `main.js` run loading maps each manifest to the run object. Add:
  `prompt_version: manifest.prompt_version || ''` (the primary/chapter-stage version recorded per §7.3, §11.3).
  Optional secondary field for detail only: `composer_prompt_version: manifest.composer_prompt_version || ''` (not used for toggling).
- The explorer is **registry-independent**: it never reads `data/prompts/registry.json`. Toggles reflect only versions actually present in the loaded run artifacts, so it works for historical runs before/after the registry exists.

### 21.3 Toggle rendering (`renderPromptVersionToggles()`)

Mirror `renderReasonToggles()` (main.js):

- Distinct present values = `[...new Set(runs.map(r => normalizePromptVersion(r.prompt_version)))]`. A run with no version is the **v1 baseline** (the profile defaults) and is treated as `chapter-p1`, not a separate `unversioned` bucket. `normalizePromptVersion('') === 'chapter-p1'` (`LEGACY_PROMPT_VERSION`).
- If `present.length === 0` (no runs), render nothing — same early-return behavior as `renderReasonToggles`. A legacy-only run set (all `''`) renders the single `chapter-p1` toggle, since those runs really did use v1.
- Ordering: natural-sorted ids (`chapter-p1 < chapter-p2 < chapter-p10`, then `composer-*`). No dependency on registry `created_at`.
- Each button:
  - `type="button"`, class `judge-toggle`, `data-prompt-version="<key>"`, `aria-pressed="true|false"`.
  - A `.jt-dot` colored by a stable hash of the (normalized) version string; define `PROMPT_VERSION_COLOR` for at least `chapter-p1` (`#6b7280`) and `chapter-p2` (`#10b981`) so the default seeded versions are visually distinct, falling back to hash for others. Legacy `''` resolves to the `chapter-p1` color.
  - Text label is the normalized version id, so the control is never color-only and a legacy run reads as `chapter-p1`.
  - Visible state driven by `promptVersionVisibility[key] ?? true`; off state adds `.jt-off` and removes `.active` (identical classes to the reasoning toggles).
  - Click: flip visibility, `renderChart()`, `persistState()`.
- Re-render on every run reload (called from the same place as `renderReasonToggles()` in `loadRuns`). Scope-aware grouping (model-scoped versions collapsing into one aggregate toggle) is specified in `specs/prompts-ui-model-scoped-editing.md`.

### 21.4 Filter semantics

Visibility keys are the canonical `version:<normalized-id>` (plus `scope:model` for the aggregate model group, §`prompts-ui-model-scoped-editing`). Legacy keys — bare ids and the empty `''` key — normalize to v1 (`chapter-p1`), so a run recorded before the registry and its saved preference are filtered by the same toggle:

```js
filtered = applyPromptFilter(filtered, promptVersionVisibility, versionScope)
```

- Filters compose with **AND**: search, run/bucket filter, `Show` (judge type), `Reasoning`, and the new system-prompt filter all apply together.
- `filteredCount` reflects the combined result; hiding all versions yields an empty chart (empty state), never an error.
- Hidden versions do **not** affect the run-picker options or the Y/X dropdowns; they only filter plotted runs.
- The filter also applies to the leaderboard/slice aggregation path that consumes `getFilteredRuns()` (main.js uses the same filtered set), so tables and plots stay consistent.

### 21.5 State persistence

- Add `let promptVersionVisibility = {}` alongside `reasoningVisibility`.
- `persistState()` includes `promptVersionVisibility` in the serialized `scatter_explorer_state`; `restoreState()` reads it through `migratePromptVisibility(savedState.promptVersionVisibility)`, which rewrites legacy bare/empty keys to `version:<normalized-id>` (empty → `version:chapter-p1`). Unknown/stale keys are harmless (a key for a version no longer present simply never matches).
- Because visibility defaults to `true` via `?? true`, a previously hidden version stays hidden across reloads exactly like reasoning.

### 21.6 Interaction with run manifests

- A run resumed across a prompt change (`prompt_version_changed_on_resume`, §13) is attributed to the **final** resolved version recorded in the manifest. The detail card may additionally show `composer_prompt_version` and the `prompt_fingerprint` (read-only, no toggle) when present.
- The toggle value is the registry id only; fingerprint/source/registry_sha are **not** toggle dimensions (too noisy). They remain available via the detail card and run table.
- `Cloud Function` inline-override runs (§6.3/§12) still record the effective `prompt_version`; if the id was unknown to the CF, the manifest carries the client id, so the toggle still groups them.

### 21.7 Accessibility & responsive

- Buttons are real `<button type="button">` with `aria-pressed`; toggling announces state changes.
- `aria-label` on the group: `role="group" aria-label="Filter runs by system prompt version"`.
- Text label present (never color-only); on narrow viewports the group wraps like `.reason-toggles`.
- Keyboard: buttons are tab-focusable and respond to Enter/Space; visible focus ring from existing styles.

### 21.8 Tests (extend the dashboard harness)

17. Given manifests with `chapter-p1`, `chapter-p2`, and `''`, the Controls sidebar renders **two** toggles (`chapter-p1`, `chapter-p2`) in natural order; the legacy `''` runs fold into `chapter-p1`. Each has `aria-pressed="true"`.
18. Clicking `chapter-p1` off removes those runs **and** the legacy `''` runs from the plotted/leaderboard set and adds `.jt-off`; `filteredCount` drops accordingly; clicking again restores them.
19. All-off → empty chart state, no exception, `filteredCount` = 0.
20. Visibility survives a reload via `persistState`/`restoreState`; a legacy preference keyed by `''` (or a bare id) is migrated and still hides the v1 runs.
21. Legacy run set (all `prompt_version === ''`) renders one `chapter-p1` toggle and plots unchanged; combined filters (search + judge + reasoning + prompt) AND correctly.

### 21.9 Acceptance (additions)

- [ ] Controls sidebar shows a **System prompt** toggle group directly above `Show`/`Reasoning`, populated from run manifests (not the registry); legacy (empty) runs are treated as the **v1** baseline `chapter-p1`.
- [ ] Toggling a version filters the scatter, tables, and `filteredCount`; composes with existing filters.
- [ ] State persists across reloads; legacy preference keys migrate to v1; a legacy-only run set renders the v1 toggle with unchanged output.
- [ ] `make test` dashboard suite covers §21.8 cases 17–21.
