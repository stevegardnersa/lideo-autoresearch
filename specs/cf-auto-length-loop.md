# Spec: Cloud Function Automatic Length Control (`auto_length`) — Shared Loop + Registry-Baked Config

**Feature:** Let the deployed Cloud Function run the same generate → measure → repair loop the offline pipeline uses, so a single `POST /summarize` request can self-correct an over- or under-length summary instead of returning pass 1 verbatim. The loop, the repair-prompt rendering, and the loop/repair configuration are all shared with the autoresearch harness — the CF imports transport-agnostic `core` modules and reads a per-version baked config from the prompt registry, so it never imports the tuned `candidate_spec.py`.

**Input needed:** None — complete behavior contract. (A live probe against the deployed function is recorded in §8.)

**Status:** Implemented, deployed, live-probed.

**Related specs:**
- `specs/target-aware-length-control.md` — the generation-time `visible_max_tokens` cap and the numeric-delta repair prompts. This spec reuses that behavior inside the CF.
- `specs/run-length-best-pass-selection.md` — best-pass selection (closest to target, earliest on ties). This spec inherits it unchanged.
- `specs/chapter-repair-convergence-length-hardfail.md` — the convergence/termination rules the loop enforces.
- `specs/nextjs-cloud-function-production-prompt-concurrent-runs.md` — registry routing / production prompt versions.
- `specs/webapp-serverless-summariser-integration.md` — the app-side integration that this unblocks.

---

## 1. Goals

1. **Loop in the CF.** A request with `"auto_length": true` and `target_words > 0` runs the full length-control loop inside the function: first pass → measure visible words → repair pass with a measured delta → repeat until in band / exhausted / stopped. The returned `summary_md` is the **best pass** (closest visible-word count to target; earliest wins ties), not the last.
2. **One loop implementation.** The CF and `core/run_candidate.py::run_length_controlled_stage` execute the *same* loop code (`core.length_control.run_length_control`) and the *same* repair prompt rendering (`core.repair_prompt`). No CF-only fork of the convergence logic.
3. **CF stays decoupled from the tuned candidate file.** Loop config and repair templates are baked into the served version record (`length_control`, `repair_templates`) by an offline tool, so the function does not import `candidate_spec.py` to find out how to repair.
4. **Backward compatible.** Default behavior (no `auto_length`) is byte-identical to today: one generation, `passes_used = 1`, same response shape. Existing registry versions without baked fields fall back to built-in defaults. `core/run_candidate.py` behavior is unchanged (refactor only; repair prompts verified byte-identical).
5. **Offline-testable.** A new `tests/test_cf_auto_length.py` covers template parity, registry round-trip, the shipped bundle, and the handler loop with a fake client — no network.

## 2. Non-Goals

- **No change to the offline loop's behavior.** `run_length_controlled_stage` keeps its exit conditions, checkpoint schema, best-pass rule, and prompt text; the extraction into `core.length_control` is behavior-preserving (see §8 parity evidence).
- **No new gate/threshold.** `scoring.py` and the length hard-fail gate are untouched.
- **No automatic looping by default.** `auto_length` is opt-in per request; batch/benchmark callers are unaffected unless they set it.
- **No progress streaming / async job.** The loop runs synchronously inside one request; the function timeout must accommodate it (deploy uses `--timeout 3600`).
- **No web-app client changes.** How the Next.js side surfaces the longer call is a separate decision (see `specs/webapp-serverless-summariser-integration.md`); this spec only makes the CF capable of looping.
- **No per-provider repair tuning**, no `candidate_spec` profile generation changes, no re-scoring of historical artifacts.

## 3. Context: Existing System

Before this change:

- **The CF was single-pass.** `cloud_function/handler.py::run_summarize` built one payload, called `client.chat_completion`, and returned `passes_used = 1`. If the model overshot the target, the CF returned the overshoot.
- **The loop lived only offline.** `core/run_candidate.py::run_length_controlled_stage` owned pass accounting, the soft/hard tolerance bands, direction-flip stop, diminishing-returns early stop, truncation retry, and best-pass selection. `core/length_control.py` had already extracted the loop body, but its `LengthControlConfig` lacked the repair-targeting knobs and had no mapping constructor for a baked config.
- **Repair prompts lived in two places.** `candidate_spec.render_repair_user` (chapter) and `core/run_candidate.render_composer_repair_user` (composer) each built the numeric-delta block and the policy/format sections inline. The CF could not reuse either without importing the candidate module.
- **Length config lived in `candidate_spec`.** `LengthControlConfig` was a dataclass in the candidate file; the registry stored only prompt templates, model, reasoning, and components.

So the CF had neither the loop nor the repair instructions. This change supplies both from shared, spec-free modules and adds an offline bake step to carry the config into the registry.

## 4. Required Behavior

### 4.1 Shared modules

**`core/length_control.py`** — transport-agnostic loop, no candidate import.

```python
@dataclass
class LengthControlConfig:
    max_passes: int = 5
    tolerance_pct: float = 0.05
    hard_tolerance_pct: float = 0.10
    repair_strategy: str = "edit_existing"          # or "regenerate_from_source"
    repair_more_prompt_id: str = "expand_missing_detail"
    repair_less_prompt_id: str = "shrink_dedup_first"
    max_truncation_retries: int = 1
    repair_target_mode: str = "target"              # or "low"
    repair_sentence_delete: bool = False
    repair_min_reduction_pct: float = 0.0
    early_stop_patience: int = 0
    early_stop_epsilon: int = 0
    stop_on_direction_flip: bool = False
    accept_within_hard_tolerance: bool = False

    @classmethod
    def from_mapping(cls, raw) -> "LengthControlConfig": ...   # partial/unknown-safe

@dataclass(frozen=True)
class LengthControlResult:
    summary_md: str                 # BEST pass, not last
    first_pass_summary_md: str
    passes_used: int
    generation_cost: float
    uncached_generation_cost: float
    raw_responses: tuple
    pass_latency_seconds: tuple = ()

def run_length_control(*, generate, render_repair, word_range, initial_user_prompt,
                       target_words, length_control, resume_state=None,
                       checkpoint_callback=None, latency_accumulator=None,
                       visible_words=visible_word_count) -> LengthControlResult: ...
```

Contract:
- `generate(user_prompt, attempt_target, current_for_mock) -> GenerationResult` performs **one** completion. The loop owns pass accounting and truncation retries (a truncation retry re-issues at `pass_target × 0.7^retry` and does **not** consume a pass).
- `render_repair(direction, current_summary_md, target_words, current_word_count) -> str` builds the direction-specific repair prompt.
- Termination order per iteration: in soft band → in hard band (if enabled) → direction flip (if enabled) → diminishing returns (`early_stop_patience`/`early_stop_epsilon`) → `max_passes` exhaustion.
- Best-pass tracking (`min |visible_words(pass) − target|`, strict `<` keeps the earliest) is maintained across restored checkpoints via `best_summary_md`.
- `from_mapping` ignores unknown keys and falls back to field defaults on malformed values, so a registry record can never crash the loop.

**`core/repair_prompt.py`** — single source of truth for repair text; no `candidate_spec` import.

- `visible_word_range(target_words, tolerance_pct)` and `sentence_budget(target_words, words_per_sentence, scale)` — the same math as `candidate_spec`.
- `repair_numeric_delta(...)` — the measured current count plus the exact cut/add instruction, honoring `repair_target_mode` (`target`/`low`), `repair_min_reduction_pct`, `repair_sentence_delete`, and the optional sentence-budget note.
- `render_blocks(template, values)` — `{{placeholder}}` substitution via `core.prompt_template`, dropping blocks that render empty (this is how optional lines — book/chapter title, TOC, metadata, excerpts — stay conditional without template control flow).
- `render_repair_prompt(template, *, direction, target_words, current_summary_md, source_md, current_word_count=None, book_title, chapter_title, tolerance_pct, repair_target_mode, repair_min_reduction_pct, repair_sentence_delete, sentence_budget_note=None, extra_values=None)`.
- `chapter_repair_template(policy, format_instructions)` / `composer_repair_template(policy, format_instructions)` — build the static templates with the policy and format section inlined; only dynamic placeholders remain.

**Refactors to use them (behavior-preserving):**
- `candidate_spec.render_repair_user` now delegates to `core.repair_prompt` (same output bytes — verified, §8).
- `core/run_candidate.render_composer_repair_user` likewise delegates; the excerpt block is still emitted only when `retrieved_source_excerpts` is non-empty.

### 4.2 Registry schema additions

`core/prompt_registry.py` version records gain two **additive, optional** fields:

| Field | Type | Purpose |
|-------|------|---------|
| `length_control` | object (JSON scalars) | Loop/repair knobs for `auto_length`. Mirrors `LengthControlConfig`. |
| `repair_templates` | object `{ "more": str, "less": str }` | Repair templates with policy + format inlined. Rendered by `core.repair_prompt`. |

- `create_version(...)` and `revise_version(...)` accept `length_control=` and `repair_templates=` and carry them through copy-on-write (a revision inherits the source's values unless overridden).
- `_clean_mapping(raw)` normalizes the baked mapping: JSON scalars only, blank strings dropped, non-mappings → `{}`.
- Fields are emitted only when non-empty, so versions that predate this change are unchanged on disk.

### 4.3 Offline bake tool — `tools/bake_length_control.py`

Fills `length_control` + `repair_templates` into every version record that lacks them.

- Defaults come from `candidate_spec.LengthControlConfig()` and the candidate's `REPAIR_MORE_POLICIES` / `REPAIR_LESS_POLICIES` / `FORMAT_INSTRUCTIONS`, resolved per stage (composer vs chapter template).
- **Additive and idempotent:** only fills absent fields unless `--force`; never rewrites `template` / `user_template`, so prompt fingerprints are unchanged.
- `--dry-run` prints changes without writing.
- Writing the registry changes `registry_sha`, so the workflow is:

  ```bash
  python3 tools/bake_length_control.py          # local registry
  python3 tools/sync_prompts.py                 # stage the CF bundle
  ```

  The CF's `meta.prompt_registry_sha` then matches the bundle.

### 4.4 Cloud Function handler — `auto_length`

**Request:**

```jsonc
{
  "source_md": "...",
  "target_words": 400,
  "auto_length": true,
  "prompt_version": "chapter-p18",
  "length_control": { "max_passes": 3, "tolerance_pct": 0.08 }  // optional override
}
```

**Config resolution precedence** (`_resolve_length_control`): request `length_control` over baked version `length_control` → built-in `LengthControlConfig` defaults.

**Repair templates** (`_resolve_repair_templates`): the version's baked `{"more","less"}`. If absent (legacy version), `_fallback_repair_template` builds one from the default policy + a minimal format line, so `auto_length` still works on un-baked versions.

**Per-pass token cap** (`_target_aware_max_tokens`): mirrors `core/run_candidate` — `visible_max_tokens(attempt_target, effort, cap=CF_MAX_TOKENS or 8192)`, floored at `round(attempt_target × 1.35 × 1.15)` so a short sentence budget cannot starve the JSON payload, never above the cap. Effort is read from the version's baked `reasoning` (or request `reasoning` / `reasoning_effort`).

**Loop wiring** (`_run_auto_length`): builds the payload per pass via `_build_openrouter_payload(..., max_tokens=<cap>)`, calls `client.chat_completion`, aggregates token/cost totals, and calls `core.length_control.run_length_control` with `word_range=repair_prompt.visible_word_range` and `visible_words=scoring.visible_word_count`. `use_json_schema` still applies — the client parses the JSON and exposes `GenerationResult.summary_md`, which is what the loop measures.

**Response additions** (only when `auto_length` ran):

```jsonc
"meta": {
  "auto_length": true,
  "passes_used": 2,
  "first_pass_visible_words": 500,
  "final_visible_words": 350
}
```

`usage.generation_cost` / `usage.uncached_generation_cost` are the **sum across passes**; token counters are summed too. `passes_used` defaults to `1` on the non-`auto_length` path.

**Validation** (`invalid_auto_length`, HTTP 400):
- `auto_length` with `target_words <= 0`.
- `auto_length` together with `summary_md` (judge/scoring-only mode has nothing to generate).

### 4.5 Tests — `tests/test_cf_auto_length.py`

Offline, no network. Added to the `Makefile` `test:` target.

1. **Chapter template parity** — `core.repair_prompt` output equals `candidate_spec.render_repair_user` for both directions, with and without `current_word_count`.
2. **Composer template parity** — equals `core.run_candidate.render_composer_repair_user`.
3. **Registry round-trip** — `create_version`/`revise_version` persist and copy `length_control` + `repair_templates`.
4. **Shipped bundle** — the production chapter version carries baked fields and its `less` template renders with runtime values.
5. **Handler loop** — `FakeClient([500, 350])` with `target_words=353` runs 2 passes, returns the closer pass, aggregates cost, reports meta, and the repair prompt carries the measured delta.
6. **Override** — request `length_control.max_passes=1` caps the loop at one call.
7. **Validation** — both `invalid_auto_length` cases raise 400.

## 5. Data Flow

```
tools/bake_length_control.py ──► data/prompts/registry.json (length_control, repair_templates)
                                          │
tools/sync_prompts.py ────────────────────┴──► cloud_function/prompts/registry.json
                                                        │
POST /summarize {auto_length:true, target_words}        ▼
  cloud_function/main.py ─► handler.run_summarize
                               │  _resolve_prompt(version)
                               │  _resolve_length_control(version, body)
                               │  _resolve_repair_templates(version)
                               ▼
                          _run_auto_length
                               │  generate(prompt, target, current) ─► client.chat_completion
                               │  render_repair(direction, …)        ─► core.repair_prompt
                               ▼
                     core.length_control.run_length_control
                               │  best pass + passes_used + summed cost
                               ▼
                          Response { summary, usage, meta.auto_length }
```

## 6. Edge Cases

- **No baked fields (legacy version):** built-in defaults + fallback repair template; loop still runs. Verified with a synthetic un-baked registry version.
- **Tight targets:** with the default `max_passes=5` and a target near the model's floor, the loop may exhaust passes and return the closest pass (possibly a few words outside the band) — best-pass semantics, not a failure. Raising `max_passes` or enabling `accept_within_hard_tolerance` in the baked config is the lever.
- **Truncation:** a truncated pass triggers the existing bounded retry (`max_truncation_retries=1`) at a lowered target; the token cap is re-derived per retry. Truncation does not consume a pass.
- **`use_json_schema`:** unchanged; the loop reads `summary_md` from the parsed result. A JSON-recovery failure still surfaces as before.
- **Reasoning models:** the cap is inflated by the effort reservation, matching the offline pipeline; `effort=None`/`"none"` gets the tighter un-inflated cap.
- **Composer stage:** the same loop path is used; the composer repair template's optional blocks (TOC, metadata, excerpts) drop when empty.
- **Cost:** every pass is a separate billed completion; `generation_cost` is the sum. Treat `auto_length` / `length_control` as operator-controlled (like `system_prompt_override`): a caller who sets `max_passes` high can amplify cost on the billed account, so don't pass them straight through from the browser.
- **Resume:** the CF loop is stateless per request (no checkpoint resume); `resume_state` support exists in `run_length_control` for the offline pipeline only.

## 7. Acceptance Criteria

- [x] `make test` green, including `tests/test_cf_auto_length.py`.
- [x] Chapter and composer repair prompts are byte-identical before/after the `core.repair_prompt` refactor (all profiles, with/without `current_word_count`).
- [x] `auto_length` runs the loop, returns the best pass, sums cost, and reports `passes_used` / first-pass / final word counts.
- [x] `auto_length` without `target_words` or with `summary_md` → HTTP 400 `invalid_auto_length`.
- [x] Default (no `auto_length`) path unchanged (`passes_used=1`, same response shape).
- [x] Baked `length_control` + `repair_templates` present on the production chapter version in the deployed bundle; `meta.prompt_registry_sha` matches the local bundle.
- [x] Live probe: in-range pass 1 returns unchanged; out-of-range first pass is repaired toward the band.

## 8. Verification (post-implementation, live)

`make test` passes offline. Local parity check (old vs new modules, all profiles) showed zero mismatches for chapter and composer repair prompts.

Live probes against the deployed function (`summarize-q2dzk4wjva-uc.a.run.app`, version `chapter-p18`, `prompt_registry_sha a98e576d…`):

| Probe | Target (band) | first → final | passes | cost | result |
|---|---|---|---|---|---|
| In-range pass 1 | 120 (114–126) | 116 → 116 | 1 | $0.00032 | in band |
| Negative (`target_words=0`) | — | — | — | — | HTTP 400 `invalid_auto_length` |
| Repair "less" | 40 (38–42) | 55 → 37 | 5 | $0.00131 | repaired, best-effort (1 under band after the 5-pass cap) |
| Repair "more" | 350 (332–368) | 320 → 335 | 3 | $0.00230 | in band |

The "more" and "less" probes confirm the live repair path issues the measured delta in both directions. The target-40 case is the documented tight-target behavior in §6.

## 9. Out of Scope

- Web-app transport for the longer call (sync route handler vs job/poll) — see `specs/webapp-serverless-summariser-integration.md`.
- Progress streaming from the CF.
- Gate/threshold changes; `scoring.py` untouched.
- Per-profile/per-model repair tuning beyond what the bake tool copies from `candidate_spec`.
- Changes to `tools/batch_summarize.py` (it does not set `auto_length`).
- Re-scoring or re-running historical artifacts.

## 10. Files Changed

| File | Change |
|------|--------|
| `core/length_control.py` | Added repair knobs to `LengthControlConfig`; added `from_mapping`. |
| `core/repair_prompt.py` | **New** — shared repair templates + rendering. |
| `core/prompt_registry.py` | `length_control` / `repair_templates` on version records; `_clean_mapping`; passthrough in `create_version` / `revise_version`. |
| `candidate_spec.py` | `render_repair_user` delegates to `core.repair_prompt`. |
| `core/run_candidate.py` | `render_composer_repair_user` delegates to `core.repair_prompt`; loop body delegates to `core.length_control`. |
| `cloud_function/handler.py` | `auto_length` flag, `_run_auto_length`, config/template resolution, target-aware cap, meta/validation. |
| `tools/bake_length_control.py` | **New** — idempotent bake migration. |
| `tests/test_cf_auto_length.py` | **New** — parity, registry, bundle, handler loop. |
| `Makefile` | `test:` runs `tests/test_cf_auto_length.py`. |
| `cloud_function/README.md` | API: `auto_length`, `length_control`, response meta, errors, security note. |
| `.notes/serverless-summarizer.md` | Architecture: loop components, request/response, bake workflow. |
| `data/prompts/registry.json`, `cloud_function/prompts/registry.json` | Baked fields (via `bake_length_control.py` + `sync_prompts.py`). |
