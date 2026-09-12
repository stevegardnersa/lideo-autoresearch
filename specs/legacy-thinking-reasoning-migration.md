# Legacy Thinking/Plain Variants → New Reasoning Format + "None" Option

**Status:** Implemented — includes mandatory-reasoning model enforcement (phantom `_notthinking` profiles removed)
**Scope:** `data/candidates.json`, `candidate_spec.py` (generated), `tools/add_candidate.py`, `tools/gen_profile_literal.py`, `core/reasoning.py`, `core/cf_client.py`, `cloud_function/handler.py`, `cloud_function/cf_client_smoke.py`, tests, `.notes/serverless-summarizer.md`
**Source of truth:** `data/candidates.json` (95 profiles; `candidate_spec.py` is generated from it via `tools/gen_profile_literal.py`)

---

## 1. Problem

Two parameter styles coexist (see `core/reasoning.py` docstring):

- **Legacy:** `extra_body={"thinking": {"type": "enabled"|"disabled"}}` — litellm-era wrapper. Previously used by 78 of 92 profiles (now fully migrated).
- **Native (recommended):** `reasoning={"effort": X}` (Option A), `reasoning={"effort": "none"}` / `reasoning={"enabled": bool}` (disable), or scalar `reasoning_effort=X` (Option B).

OpenRouter ignores `extra_body` entirely, so the handler must translate it. The translation exists (see §2) but is spread across `cf_client` → `handler` (CF path) and `reasoning_request_params` (direct path), with three inconsistencies:

1. **Canonical "none" is undefined.** `tools/add_candidate.py::_build_stage_config` emits **no reasoning field** for `effort == "none"` (provider default — reasoning stays ON for reasoning models like `gpt-5.6-luna`), while the runtime collapses legacy `thinking:disabled` and scalar `reasoning_effort="none"` to `reasoning={"enabled": false}` (forced OFF). A `_notthinking` profile must mean forced-off; today the two paths disagree.
2. **`reasoning={"effort": "none"}` is forwarded verbatim** (handler: `payload["reasoning"] = dict(reasoning)`; `reasoning_request_params` returns it unchanged) instead of collapsing to `{"enabled": false}`. OpenRouter may reject `effort: "none"` as an invalid tier.
3. **Plain (unset) requests get coerced to reasoning-off on the CF path.** `cf_client` defaults `thinking=False` for payloads with no signal → handler emits `reasoning.enabled=false`. The direct path sends no reasoning field for plain configs. Behavior diverges by transport.

Plus the requested gaps:

4. **Legacy signals must be migrated to native format** (all plain + thinking variants).
5. **Every variant family needs a "none" option** (a `_notthinking` sibling).

---

## 2. Mapping table (the 1:1 answer)

| Legacy / current signal | New native format | 1:1? | Where normalised today |
|---|---|---|---|
| `extra_body.thinking.type == "enabled"` | `reasoning={"enabled": true}` | **Yes** | handler: `thinking=True → {"enabled": True}`; smoke check 7 |
| `extra_body.thinking.type == "disabled"` | `reasoning={"enabled": false}` | **Yes** | handler: `thinking=False → {"enabled": False}`; `stage_reasoning_effort` → `"none"` |
| plain (no reasoning config) | no `reasoning` field (absent) | **Yes** (with "unset") | absent on direct path; **BUG** CF path coerces to `enabled=false` (see §4, R4) |
| scalar `reasoning_effort=X` | `reasoning={"effort": X}` | Yes | handler + `reasoning_request_params` |
| scalar `reasoning_effort="none"` | `reasoning={"enabled": false}` | Yes | handler + `reasoning_request_params` collapse |
| `reasoning={"effort": "none"}` | `reasoning={"enabled": false}` | Should be (canonicalise) | **BUG** forwarded verbatim today (§4, R4) |
| `reasoning={"enabled": bool}` | unchanged | Yes | verbatim |

**Not 1:1 — explicit decision points:**

- `DEFAULT_THINKING_EFFORT = "high"` in `core/reasoning.py` and `tools/add_candidate.py::migrate_legacy_thinking` map `_thinking → effort-high` **and rename the profile** to `..._effort-high`. That is an *upgrade* mapping (changes effort + breaks resume of in-flight runs keyed by the old profile name). **This spec chooses the 1:1 migration instead**: `_thinking` keeps its key and maps to `reasoning={"enabled": true}`. The effort-tier upgrade stays available as an explicit, opt-in follow-up (per model, via the existing `--migrate-legacy-thinking` tool).
- **Canonical "none" option** = `reasoning={"enabled": false}`. NOT absent (absent = provider default, may still reason) and NOT `{"effort": "none"}` (invalid tier for some providers). `stage_reasoning_effort` already returns `"none"` for `enabled:false`, and `effort_style_label("none") == "notthinking"` keeps profile naming stable.

---

## 3. Current inventory (`data/candidates.json`)

- **95 profiles / 43 (time×model) families; 87 visible + 8 hidden.**
- All 78 legacy `extra_body.thinking.*` carriers migrated to native `reasoning`; **0 profiles remain with `extra_body`** (verified by acceptance grep).
- **41 `_notthinking` profiles** (all families except the mandatory-reasoning `step-3.7-flash` 30m/60m — see §4, R3).
- **Mandatory-reasoning model:** `stepfun/step-3.7-flash` (`capabilities["none_style"] == "mandatory"`, efforts `["medium"]`). Its provider rejects `reasoning.enabled=false` and a plain request still reasons at default effort — a `_notthinking` profile is **impossible**, so the two phantom `step-3.7-flash_notthinking` profiles created during the first migration pass were **removed**. The family keeps only `_effort-medium` profiles.
- **Families that legitimately lack a `_notthinking` variant (2, both mandatory-reasoning):**
  | Family | Existing variants | Why no `_notthinking` |
  |---|---|---|
  | `30m_step-3.7-flash` | effort-medium | reasoning MANDATORY — cannot be disabled |
  | `60m_step-3.7-flash` | effort-medium | reasoning MANDATORY — cannot be disabled |

---

## 4. Requirements

### R1 — Migrate all legacy plain/thinking variants to native format
All 78 legacy profiles (40 `_thinking`, 36 `_notthinking`, 2 plain) in `data/candidates.json` lose `extra_body` and gain the native field:

- `_thinking` → `"reasoning": {"enabled": true}` (chapter + composer stage), **profile key unchanged**.
- `_notthinking` → `"reasoning": {"enabled": false}`, key unchanged.
- plain `_notthinking` (muse) → `"reasoning": {"enabled": false}`, key unchanged.
- `notes` updated: `mode` becomes `notthinking (effort=none)` / `thinking (enabled=true)` wording — drop "legacy-…" prefixes.
- Hidden flag preserved on hidden profiles.

### R2 — Canonical "none" = `reasoning={"enabled": false}` everywhere
- `tools/add_candidate.py::_build_stage_config`: `effort == "none"` emits `cfg["reasoning"] = {"enabled": False}` (replaces the current "emit nothing"). `max_tokens` scaling unchanged (`scaled_max_tokens(..., "none")` → base, fraction 0.0).
- Probe: `_probe_effort` for `effort == "none"` must probe `{"reasoning": {"enabled": false}}` (today it probes a plain request). Record acceptance in `capabilities["efforts"]` as `"none"` only when the enabled-false form is accepted; keep a separate `capabilities["none_style"]`:
  - `"enabled_false"` — model accepts `reasoning.enabled=false` → a real `_notthinking` profile is possible.
  - `"mandatory"` — model rejects `enabled=false` **and** still reasons on a plain request → `_notthinking` is impossible; `"none"` is **not** added to the accepted efforts, and no `_notthinking` profile may be created.
  - `None` — neither form accepted.

### R3 — None option per family, with mandatory-reasoning exclusion
Add `_notthinking` profiles for families that lack one **via the `add_candidates`/`add_candidate.py` flow (effort `"none"`)**, then regenerate. Resulting invariant: **every (time×model) family except mandatory-reasoning models has exactly one `_notthinking` profile**, and that profile's both stages carry `reasoning={"enabled": false}`.

**Mandatory-reasoning enforcement (added during implementation):**
- `tools/add_candidate.py::_resolve_efforts` refuses `requested=["none"]` (and never defaults to it) when `none_style in ("plain", "mandatory")` — raises instead of silently creating a profile that reasons at default effort.
- `_build_stage_config` raises if called with `effort="none"` and `none_style in ("plain", "mandatory")`.
- `migrate_legacy_thinking` (1:1 mode) raises when a `_notthinking` target belongs to a mandatory model — refuses to keep a phantom profile.
- The two phantom `step-3.7-flash_notthinking` profiles created in the first migration pass were **removed** from `data/candidates.json`; the step family is left with `_effort-medium` only.

### R4 — Normalisation consistency across transports
- `core/reasoning.py::reasoning_request_params`: `reasoning={"effort": "none"}` → `{"reasoning": {"enabled": False}}` (collapse; today forwarded verbatim). Also: **mandatory-reasoning guard** — a disable intent (`enabled:false`, `effort:"none"`, scalar `"none"`) on a registered mandatory model raises `ValueError` via `assert_disabling_reasoning_allowed` instead of silently sending a request that reasons at default effort.
- `cloud_function/handler.py`: same collapse for the `reasoning` dict branch; and emit `payload["reasoning"] = {"enabled": ...}` **only when `thinking` is not None** (today `thinking` defaults `False`, so plain requests get reasoning forced off).
- `core/cf_client.py`: stop defaulting `body["thinking"] = False` when the payload carries no signal (no `extra_body.thinking`, no explicit `thinking` arg). Pass `thinking=None` through → handler omits the reasoning field → plain stays absent on both transports. Add a client-side mirror of the mandatory guard (raises before the request leaves the process).
- `core/reasoning.py::stage_reasoning_effort`: already returns `"none"` for `reasoning.enabled is False` and for `reasoning.effort == "none"`; add a unit test pinning `reasoning={"effort": "none"}` → `"none"` (it already does, via the `effort` branch — make it explicit).

### R5 — Regenerate `candidate_spec.py`
Run `tools/gen_profile_literal.py`: Profile `Literal` gains the new keys; every stage drops `extra_body={"thinking": …}` (emitted as `extra_body=None`); `reasoning` dicts emitted verbatim. No hand-edits to the generated file. **Generator change:** `py_literal()` fixes JSON `true/false/null` → Python literals so emitted `reasoning={"enabled": False}` imports; the generator also emits a `MANDATORY_REASONING_MODELS` frozenset from `capabilities[].none_style == "mandatory"` and registers it via `register_mandatory_reasoning_models` (imported from `core.reasoning`).

### R6 — Tests and smoke
- `cloud_function/cf_client_smoke.py` — **10 checks**:
  1. scoring-only round trip
  2. stub generation
  3. `reasoning_effort="high"` → `reasoning={"effort": "high"}` (exists)
  4. `reasoning={"effort": "high"}` forwarded verbatim (exists)
  5. `thinking=False` → `reasoning={"enabled": false}` (exists)
  6. `reasoning_effort="none"` → `reasoning={"enabled": false}` (exists)
  7. `thinking=True` → `reasoning={"enabled": true}` (exists)
  8. **new** `reasoning={"effort": "none"}` → `reasoning={"enabled": false}`
  9. **new** plain payload (no reasoning/thinking/extra_body) → **no `reasoning` field in stub body**
  10. **new** mandatory-reasoning model (stepfun) — disabling reasoning raises client-side
- New `tests/test_reasoning_migration.py` (offline, no network):
  - Every visible profile in `candidate_spec.PROFILE_CANDIDATES` has `extra_body` empty/None and exactly one of `reasoning`/`reasoning_effort` per stage (or neither for a `_notthinking` of a mandatory model, but never `extra_body.thinking`).
  - Every (time×model) family except mandatory-reasoning models has a `_notthinking` profile; its stages never enable reasoning and never carry an effort tier. Mandatory models have **no** `_notthinking` profile and never carry `enabled=false`.
  - `stage_reasoning_effort` table: `enabled:false → "none"`, `{"effort": "none"} → "none"`, absent → `None`, `{"effort": "minimal"} → "minimal"`.
  - `reasoning_request_params({"effort": "none"}) == {"reasoning": {"enabled": False}}`; disabling on a mandatory model raises `ValueError`.
  - `build_openrouter_request` raises on a mandatory disable; emits `enabled=false` for a normal model.
  - `_resolve_efforts` refuses `requested=["none"]` on a mandatory model.
  - `effort_style_label("none") == "notthinking"`, `profile_name_for("30m", "gpt-5.6-luna", "none") == "30m_gpt-5.6-luna_notthinking"`.
- `make test` and `make cf-client-smoke` green.

---

## 5. Implementation steps (ordered)

1. **`tools/add_candidate.py`**
   - `_build_stage_config`: emit `reasoning={"enabled": False}` for `effort == "none"` (R2); raise for `none_style in ("plain", "mandatory")`.
   - `_probe_effort`: probe enabled-false form for `"none"`; record `none_style` = `"enabled_false"` / `"mandatory"` / `None` (R2). `_probe_plain` used only to distinguish "mandatory" from "rejects everything".
   - `_resolve_efforts`: refuse `requested=["none"]` on mandatory models (R3); no default `_notthinking`.
   - `migrate_legacy_thinking`: add `one_to_one` mode (default for this migration): `_thinking → reasoning={"enabled": true}` and `_notthinking → reasoning={"enabled": false}`, **keys unchanged**; `_effort-*` legacy carriers keep their effort tier from the name; raise on mandatory `_notthinking` targets. Existing upgrade mode (effort tiers + rename) stays opt-in via `--upgrade-efforts`. Add `_none_style_for` reading model capabilities.
2. **`data/candidates.json`**: run the 1:1 migration (all 78 legacy + 2 plain), add the missing `_notthinking` profiles for gpt-5.6-luna (30m/60m) and nemotron-3.5-lightning (30m), then **remove the two phantom `step-3.7-flash_notthinking` profiles** after probing confirmed step's reasoning is mandatory (R1, R3). Preserve `hidden` flags and `capabilities` entries (append `none_style` per model).
3. **`core/reasoning.py`**: `reasoning_request_params` collapse (R4); add `MANDATORY_REASONING_MODELS` registry + `register_mandatory_reasoning_models` + `assert_disabling_reasoning_allowed` guard (R4); docstring updated — legacy `extra_body` no longer produced by the profile set ("kept byte-identical" note removed; legacy support retained only for back-compat of in-flight stages).
4. **`core/cf_client.py`**: `thinking=None` pass-through for plain payloads (R4); client-side mandatory guard.
5. **`cloud_function/handler.py`**: collapse `reasoning={"effort":"none"}`; emit `reasoning.enabled` only when `thinking is not None` (R4).
6. **`tools/gen_profile_literal.py`** → regenerate `candidate_spec.py` (R5). `py_literal()` converts JSON bool/null to Python literals; emits `MANDATORY_REASONING_MODELS` frozenset from capabilities and registers it.
7. **`cloud_function/cf_client_smoke.py`** (10 checks) + **`tests/test_reasoning_migration.py`** (R6).
8. **`.notes/serverless-summarizer.md` + `core/reasoning.py` docstring**: update the reasoning-control note (native-only; "none" = `enabled:false`; mandatory models fail loudly).

---

## 6. Edge cases

- **Resume compatibility:** profile keys are stable (R1), so `--resume <run_id> --profile <legacy-name>` keeps working; old run artifacts are untouched (stage configs only shape new requests). The CF-handler change (emit `reasoning.enabled` only when `thinking is not None`) affects only requests that previously carried an implicit `thinking=False` — in-flight legacy runs that relied on `extra_body.thinking.disabled` still send the explicit signal via cf_client, so no behavior change for them.
- **Hidden profiles:** luna effort-low/high/xhigh/max stay hidden after regeneration; the new luna `_notthinking` profiles are visible.
- **Mandatory reasoning (stepfun/step-3.7-flash):** probing confirmed the provider rejects `reasoning.enabled: false` **and** reasons on a plain request. `none_style="mandatory"` is recorded, `"none"` is excluded from the accepted efforts, and the two phantom `_notthinking` profiles are removed. Any code path that would disable reasoning on the model raises `ValueError` (probe, tool, `reasoning_request_params`, `build_openrouter_request`, CF client) — a request that can only run at default effort is refused rather than silently mislabeled.
- **`max_tokens`:** none-effort stages keep unscaled `8192` (fraction 0.0) — unchanged.
- **Manifest/dashboard labels:** `effort_style_label("none") == "notthinking"` and profile names are preserved, so `_thinking`/`_notthinking` filters in the agent and dashboard keep working.
- **Judge stages:** the judge path (`judge=True`) never carries reasoning config — unaffected.
- **`data/candidates.json` vs generated py drift:** regenerate immediately after JSON edits; `tests/test_reasoning_migration.py` asserts the generated file matches the JSON for reasoning fields.

---

## 7. Acceptance criteria

- [x] `grep -r "extra_body.*thinking\|thinking.*extra_body" data/candidates.json candidate_spec.py` → no matches.
- [x] `data/candidates.json` has 95 profiles; 8 remain hidden; every family except the two mandatory-reasoning `step-3.7-flash` ones has a `_notthinking` profile.
- [x] `candidate_spec.py` Profile `Literal` contains the new keys; `MANDATORY_REASONING_MODELS` is emitted from capabilities; import succeeds; `make test` green.
- [x] `make cf-client-smoke` prints `cf-client-smoke OK (10 checks)`.
- [x] Probe run for gpt-5.6-luna / nemotron-3.5-lightning / step-3.7-flash records `none_style="enabled_false"` for the first two and `"mandatory"` for step (step's phantom `_notthinking` profiles removed).
- [x] `tests/test_reasoning_migration.py` passes offline.

## 8. Out of scope

- Adding `_thinking` (enabled) variants to families that lack them (muse-glimmer, luna, nemotron, step) — not requested.
- Upgrade migration (`_thinking → effort-high` with rename) — remains opt-in via `--upgrade-efforts`.
- litellm gateway / direct-OpenRouter routing — already fixed in a prior change.
- Touching `tools/batch_summarize.py`, `dashboard/`, or run artifacts.