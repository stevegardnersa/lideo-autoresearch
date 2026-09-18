# Base Prompts + Model-Strategy Tuning

**Feature:** Establish a curated set of **base chapter prompts** (movable parts for optimizing model
strategy), align the shared readable style text with the sentence cap, add a dashboard-triggered
**base-prompt sweep** that ranks the bases per model, and let both the manual (Prompts tab) and
automatic (`autoresearch/`) tuning paths fork from a chosen base.

**Input needed:** None — complete behavior contract.

**Status:** Draft for implementation

**Depends on:**
- `specs/per-model-prompt-tuning.md` (metrics + ranking definition reused verbatim in §3)
- `specs/prompts-ui-model-scoped-editing.md` (Prompts-tab model-scoped version creation, `POST /api/prompts`)

---

## 1. Goals

1. **Three bases, labeled, resolvable.** A `sentence` base and two word-budget bases (`readable`,
   `readable-low`), each a normal prompt-registry version (`scope=general`), tagged with a
   `base: <slug>` label so tooling can enumerate and resolve them.
2. **Aligned readable text.** The shared `plain_readable` style text must stop hardcoding a sentence
   length that contradicts the per-version sentence cap; it should reference the request's
   words-per-sentence.
3. **Clean version history.** `p3`/`p14` and the `p4`–`p13` experiments are demoted to
   `experiment:` labels and hidden from pickers by default — no deletions, no dangling refs.
4. **Sweep exists and is deterministic.** One command / one button runs the bases on a bench, repeats
   N times, and ranks them per model using the existing metric stack.
5. **Tuning is base-aware.** Manual forking and the auto hill-climber can both start from a base and
   record which base was used.

## 2. Non-Goals

- No new metrics or scoring formulas — reuse `specs/per-model-prompt-tuning.md` §5.
- No automatic production promotion from the sweep (`--adopt` is the only opt-in, and it is explicit).
- No change to the versioning model, slot semantics, or `resolve_version` precedence.
- No per-model base *pinning* config in this spec (deferred).
- No deletion of any existing version.

---

## 3. Current State

Registry (`data/prompts/registry.json`), chapter stage:

| id | label | components |
|---|---|---|
| `chapter-p2` | v2 target-aware (default) | `length_policy=explicit_budget_first` |
| `chapter-p3` | v3 plain readable FK<10 (shipped) | `sentence_split_hard`, `plain_readable`, 13 w/s |
| `chapter-p4`…`chapter-p13` | vN `(test)` | length experiments |
| `chapter-p14` | v4 readable word-first + aim 0.90 + 12w/s | `explicit_budget_first`, `plain_readable`, 12 w/s, `length_aim=0.90` |

Slots: `local=chapter-p14`, `production=chapter-p3`. No `chapter-p15` exists yet.

**Shared-text dependency.** `chapter-p3` and `chapter-p14` (and every `plain_readable` version
`p3`, `p7`–`p14`) have **no `system_template`**, so `render_chapter_system`
(`candidate_spec.py`) re-composes them from `CHAPTER_SYSTEM_STYLES["plain_readable"]` at render
time. Each version also stores a rendered `template` snapshot and a `system_prompt_sha256`
(`core/prompt_registry.py:394`). Editing the shared text therefore:

- changes the rendered system prompt of **all** readable versions at once;
- leaves each version's stored `template` / `system_prompt_sha256` **stale**;
- does **not** change `registry_sha` (`compute_registry_sha` hashes `versions` + `slots` JSON only);
- makes historical run manifests (`chapter_system_prompt_sha256`, recorded via
  `core/versioning.py::build_prompt_hashes`) disagree with what the same version id now renders.

That is why §4 re-derives the bases rather than editing in place.

---

## 4. Design

### 4.1 Align the shared readable text

In `candidate_spec.py::CHAPTER_SYSTEM_STYLES`, replace the `plain_readable` sentence rule so it is
cap-referential instead of hardcoded "14 to 16":

> `SENTENCE RULE: A sentence is one idea. Split any sentence with more than one clause joined by`
> `and/but/so/because/while or by commas into separate sentences. Keep sentences at or below the`
> `words-per-sentence the request asks for; prefer 12 to 14 words.`

The rest of the `plain_readable` entry (word rule, fidelity rule) is unchanged.

### 4.2 Re-derive the base set (cutover)

All bases are created with `core.prompt_registry.revise_version` under the §4.1 text, `scope=general`,
and label `base: <slug>`:

| slug | action | id | components |
|---|---|---|---|
| `sentence` | create new | `chapter-p15` | `length_policy=sentence_primary`, `system_style=plain_readable`, `sentence_target_words=11`, `target_sentence_words=13` |
| `readable` | re-derive from `chapter-p3` | `chapter-p16` | `length_policy=sentence_split_hard`, `system_style=plain_readable`, `target_sentence_words=13` |
| `readable-low` | re-derive from `chapter-p14` | `chapter-p17` | `length_policy=explicit_budget_first`, `system_style=plain_readable`, `target_sentence_words=12`, `length_aim=0.90` |

Rules:

- **Copy-on-write.** Never mutate `p3`/`p14`; re-derive so the new versions' `template` snapshot and
  `system_prompt_sha256` reflect the aligned text.
- **Slots.** Repoint `local` → `chapter-p15` (`sentence`, the intended tuning default). Repoint
  `production` → `chapter-p16` (re-derived `readable`, semantically identical to `p3` with the aligned
  text). *This supersedes the earlier "hold production on `p3`" decision, which would leave production
  serving a stale-fingerprint version; it is an explicit review point.*
- **Demote, do not delete.** Relabel `p3`, `p14`, and `p4`–`p13` to `experiment: <original label>` and
  hide `experiment:`-labelled versions from the Prompts-tab pickers by default (still resolvable by id
  for reproducing past runs). Keep `p2` as the legacy default label.
- **Refresh the bundled copy.** `cloud_function/prompts/registry.json` holds its own snapshot with
  `system_prompt_sha256` values; regenerate it via `tools/sync_prompts.py` (or
  `POST /api/prompts/sync`) so the CF bundle matches the re-derived bases.
- **No new schema.** The `base: <slug>` convention lives in version `label` strings; version records
  and `scope` are unchanged.

### 4.3 Base registry helpers

In `tools/prompts_api.py`:

- `BASE_PROMPTS: dict[str, str]` — slug → label, e.g. `{"sentence": "base: sentence", ...}`.
- `list_base_prompts() -> list[dict]` — resolves each slug to its version record by label prefix
  (`base: <slug>`), returns `{slug, version_id, label, components}`; raises a clear error if a base
  slug resolves to zero or more than one version.

Rejected alternative: a formal `Bases → [version ids]` map in the registry. It would be more
explicit, but the `base:` label resolves with no schema change and keeps bases as ordinary versions
(editable, forkable, revertible). Tradeoff: label parsing is string-based and must reject ambiguity.

### 4.4 Sweep

`tools/sweep_base_prompts.py`

```
python3 tools/sweep_base_prompts.py --profile <profile> [--bench chapter_fast-v3]
    [--bases sentence,readable,readable-low] [--repeat 3] [--json-out <path>] [--adopt]
```

- **Iteration.** For each selected base slug, for each repeat `1..--repeat` (default **3**):
  `run_candidate(profile, chapter_prompt_version=<base version id>, bench=<bench>)`.
  `--repeat` uses independent runs so the ranking sees run-to-run variance.
- **Metrics.** Per run, reuse `specs/per-model-prompt-tuning.md` §5 exactly:
  - per-sample final-length hit (inside the ±5% soft range), first-pass hit, final length error,
    extra passes beyond the first, pass-cap hit, `hard_fail`;
  - plus faithfulness and concept scores from the judge where available.
- **Eligibility floors** (same spec): `min_faithfulness = 0.70`, `min_concept = 0.60`,
  `hard_fail_rate <= baseline_hard_fail_rate + 0.05`. A base that violates any floor is **ineligible**.
- **Ranking** (only eligible bases):
  `length_score = 0.55·final_hit_rate + 0.20·first_pass_hit_rate + 0.15·(1 − clamp01(mean_final_err / 0.25))`
  `− 0.10·clamp01(mean_extra_passes / 4) − 0.10·pass_cap_rate`
  (all rates computed per sample, then averaged over the repeats).
- **Tie-break:** lower `mean_extra_passes` → higher faithfulness → base order
  `sentence, readable, readable-low`.
- **Output.** Deterministic winner + full table.
  - `artifacts/autoresearch/base_sweep/<profile>_<bench>_<timestamp>.json` — ranked rows with all aggregates.
  - `artifacts/autoresearch/base_sweep/<profile>_<bench>_<timestamp>.md` — human summary + recommendation.
  - Each run is timestamped (numeric suffix on collision) so every sweep is preserved as its own
    comparison run; `--json-out`/`--md-out` override the paths. Artifacts carrying `--job-id`
    (set automatically for dashboard-launched sweeps) link back to their live job log.
- **`--adopt`** (opt-in): set the permutation store's base components for `profile`'s chapter stage
  to the winner (via the existing `set_current_best` path). Off by default; never touches slots.

### 4.5 Dashboard trigger

- `dashboard/vite.config.js` — add a `SCRIPT_REGISTRY` entry for `tools/sweep_base_prompts.py` with
  `group: "run"`, `runtimeClass: "llm"`, args schema for `profile`, `bench`, `bases`, `repeat`,
  `adopt`.
- `dashboard/settings.js` — a **Compare base prompts** button in the Prompts tab (`#promptsPanel`)
  that opens a small form (profile, bench, repeat) and dispatches via `POST /api/jobs`. The panel
  renders the `.md` artifact summary when the job completes (same pattern as existing run jobs).
- **Profile must be a valid profile key, not a provider/model id.** `run_candidate.py` takes profile
  keys like `30m_step-3.5-flash_effort-medium`; a bare `stepfun/step-3.5-flash` (or `30m_step-3.5-flash`)
  is unknown and aborts the run. The form therefore uses two selects — **Model** (provider/model ids
  from `data.models`) and **Profile** (profile keys from `data.profiles_by_model[model]`) — so only
  real keys are selectable. The provider/model id itself is not a valid `--profile` value.
- `run_candidate.py` and `tools/sweep_base_prompts.py` both validate the profile up front and exit
  with a `Unknown profile: <x>` message plus the valid keys for that model/budget, instead of a bare
  `KeyError` traceback.

### 4.6 Tuning integration

Both tuning paths start from a base:

- **Manual (Prompts tab).** A **Fork base** dialog action creates a model-scoped version with
  `POST /api/prompts {scope:{kind:"model",...}, derived_from:<base version id>}` — reuses the
  endpoint defined in `prompts-ui-model-scoped-editing.md`.
- **Automatic (`autoresearch/`).** `autoresearch/tune_prompt.py` and `agent.run_hill_climb` gain
  `--base <slug>`:
  - default when omitted: the sweep winner for the profile if an artifact exists, else the current
    default base;
  - seeds the baseline components from the resolved base version;
  - records `base_prompt: <slug>` in the permutation entry and the run report/adoption record.

Precedence is unchanged (`cli override → permutation → registry model → registry general`).

---

## 5. Tests

- **`tests/test_base_prompts.py`** (new):
  - exactly the three `base:` slugs resolve; `list_base_prompts()` returns 3 distinct version ids;
  - `sentence` resolves to `chapter-p15` with the exact expected components;
  - label-prefix edge cases: an `experiment: base: x`-style label and a missing base each raise the
    documented error;
  - ranking + eligibility on fixture manifests: an ineligible base (faithfulness below floor) is
    excluded; ties break by `mean_extra_passes` then faithfulness then base order.
- **`tests/test_sentence_budget.py`** (extend): render the `sentence` base and assert
  `write exactly 117 sentences` / `hard constraint` for a 1288-word target.
- **Drift guard** (new assertion, closes the §3 hazard): after the §4.1 library edit, assert each
  re-derived base's stored `system_prompt_sha256` equals the sha of a freshly rendered system prompt
  for that version.
- **Sweep end-to-end (mocked)**: stub `run_candidate` to emit fixture manifests; assert the JSON
  artifact shape (per-base rows, winner, ranking) and that `--repeat` is honored.

## 6. Files Touched

**New**
- `tools/sweep_base_prompts.py`
- `tests/test_base_prompts.py`
- `specs/base-prompts-and-model-strategy-tuning.md`
- `specs/base-prompts-and-model-strategy-tuning.summary.md`

**Modified**
- `candidate_spec.py` — `plain_readable` sentence rule (§4.1).
- `data/prompts/registry.json` — create `chapter-p15`; re-derive `chapter-p16`/`chapter-p17`;
  relabel `experiment:`; repoint `local`/`production` slots.
- `cloud_function/prompts/registry.json` — sync bundle shas.
- `tools/prompts_api.py` — `BASE_PROMPTS`, `list_base_prompts()`.
- `dashboard/vite.config.js` — `SCRIPT_REGISTRY` sweep entry.
- `dashboard/settings.js` — Prompts-tab **Compare base prompts** + fork-from-base.
- `autoresearch/tune_prompt.py`, `autoresearch/agent.py`, `autoresearch/optimizer.py` — `--base`
  seeding + `base_prompt` recording.
- `Makefile` — `sweep-bases` convenience target.

## 7. Acceptance Criteria

1. `list_base_prompts()` returns exactly `sentence`, `readable`, `readable-low` with `chapter-p15`,
   `chapter-p16`, `chapter-p17`.
2. `chapter-p15` matches the §4.2 component spec and, for `target_words=1288`, renders a 117-sentence
   hard-constraint instruction.
3. The §4.1 edit is applied; every re-derived base's stored `system_prompt_sha256` equals a fresh
   render (drift guard passes); `p3`/`p14` are relabeled `experiment:` and still resolvable by id.
4. `python3 tools/sweep_base_prompts.py --profile 30m_gpt-5.6-luna_notthinking` on `chapter_fast-v3`
   emits 3 ranked rows + a deterministic winner in both artifacts, with repeats honored.
5. The Prompts-tab **Compare base prompts** button runs the sweep and shows the summary. The form's
   **Model** and **Profile** selects only offer valid profile keys (e.g. picking
   `stepfun/step-3.5-flash` restricts Profile to `30m_step-3.5-flash_effort-*`/`_thinking` keys); an
   invalid profile exits with `Unknown profile: <x>` plus valid-key hints, not a traceback.
6. `--base <slug>` seeds the hill-climber and the base slug appears in the permutation/report record.
7. `cloud_function/prompts/registry.json` reflects the re-derived shas.
8. Existing test suites pass; new tests in §5 pass.
