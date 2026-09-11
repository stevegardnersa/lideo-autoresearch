# Legacy Thinking/Plain Variants → New Reasoning Format + "None" Option

**Status:** Proposed spec (no code changes in this commit)
**Scope:** `data/candidates.json`, `candidate_spec.py` (generated), `tools/add_candidate.py`, `core/reasoning.py`, `core/cf_client.py`, `cloud_function/handler.py`, `cloud_function/cf_client_smoke.py`, tests
**Source of truth:** `data/candidates.json` (92 profiles; `candidate_spec.py` is generated from it via `tools/gen_profile_literal.py`)

---

## 1. Problem

Two parameter styles coexist (see `core/reasoning.py` docstring):

- **Legacy:** `extra_body={"thinking": {"type": "enabled"|"disabled"}}` — litellm-era wrapper. Still used by 78 of 92 profiles.
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

- **92 profiles / 43 (time×model) families; 84 visible + 8 hidden.**
- Legacy `extra_body.thinking.enabled` → **40 profiles** (`_thinking`).
- Legacy `extra_body.thinking.disabled` → **36 profiles** (`_notthinking`).
- Plain (no reasoning config) → **2 profiles**: `30m_muse-glimmer-30b_notthinking`, `60m_muse-glimmer-30b_notthinking` — note the **name says notthinking but the config is empty** (provider default). Migrate to `reasoning={"enabled": false}` to match the name (behavior change from "provider default" to forced-off; intentional).
- Already native → **14 profiles** (muse-glimmer effort-medium ×2, nemotron-3.5-lightning effort-medium/minimal, step-3.7-flash effort-medium ×2, gpt-5.6-luna effort-* — the latter 8 are the hidden low/high/xhigh/max variants; keep them hidden).
- **Families missing a none/`_notthinking` variant (5 to add):**
  | Family | Existing variants | Add |
  |---|---|---|
  | `30m_gpt-5.6-luna` | effort-high/low/max/medium/minimal/xhigh | `30m_gpt-5.6-luna_notthinking` |
  | `60m_gpt-5.6-luna` | effort-high/low/max/medium/minimal/xhigh | `60m_gpt-5.6-luna_notthinking` |
  | `30m_nemotron-3.5-lightning` | effort-medium, effort-minimal | `30m_nemotron-3.5-lightning_notthinking` |
  | `30m_step-3.7-flash` | effort-medium | `30m_step-3.7-flash_notthinking` |
  | `60m_step-3.7-flash` | effort-medium | `60m_step-3.7-flash_notthinking` |

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
- Probe: `_probe_effort` for `effort == "none"` must probe `{"reasoning": {"enabled": false}}` (today it probes a plain request). Record acceptance in `capabilities["efforts"]` as `"none"` only when the enabled-false form is accepted; keep a separate `capabilities["none_style"]` (`"enabled_false"` vs `"plain"`) for models that reject it.

### R3 — Add the none option to every family
Add the 5 profiles from §3 via the `add_candidates`/`add_candidate.py` flow (effort `"none"`), then regenerate. Resulting invariant: **every (time×model) family has exactly one `_notthinking` profile**, and that profile's both stages carry `reasoning={"enabled": false}`.

### R4 — Normalisation consistency across transports
- `core/reasoning.py::reasoning_request_params`: `reasoning={"effort": "none"}` → `{"reasoning": {"enabled": False}}` (collapse; today forwarded verbatim).
- `cloud_function/handler.py`: same collapse for the `reasoning` dict branch; and emit `payload["reasoning"] = {"enabled": ...}` **only when `thinking` is not None** (today `thinking` defaults `False`, so plain requests get reasoning forced off).
- `core/cf_client.py`: stop defaulting `body["thinking"] = False` when the payload carries no signal (no `extra_body.thinking`, no explicit `thinking` arg). Pass `thinking=None` through → handler omits the reasoning field → plain stays absent on both transports.
- `core/reasoning.py::stage_reasoning_effort`: already returns `"none"` for `reasoning.enabled is False` and for `reasoning.effort == "none"`; add a unit test pinning `reasoning={"effort": "none"}` → `"none"` (it already does, via the `effort` branch — make it explicit).

### R5 — Regenerate `candidate_spec.py`
Run `tools/gen_profile_literal.py`: Profile `Literal` gains the 5 new keys; every stage drops `extra_body={"thinking": …}` (emitted as `extra_body=None`); `reasoning` dicts emitted verbatim. No hand-edits to the generated file.

### R6 — Tests and smoke
- `cloud_function/cf_client_smoke.py` — extend to **9 checks**:
  1. scoring-only round trip
  2. stub generation
  3. `reasoning_effort="high"` → `reasoning={"effort": "high"}` (exists)
  4. `reasoning={"effort": "high"}` forwarded verbatim (exists)
  5. `thinking=False` → `reasoning={"enabled": false}` (exists)
  6. `reasoning_effort="none"` → `reasoning={"enabled": false}` (exists)
  7. `thinking=True` → `reasoning={"enabled": true}` (exists)
  8. **new** `reasoning={"effort": "none"}` → `reasoning={"enabled": false}`
  9. **new** plain payload (no reasoning/thinking/extra_body) → **no `reasoning` field in stub body**
- New `tests/test_reasoning_migration.py` (offline, no network):
  - Every visible profile in `candidate_spec.PROFILE_CANDIDATES` has `extra_body` empty/None and exactly one of `reasoning`/`reasoning_effort` per stage (or neither, but never `extra_body.thinking`).
  - Every (time×model) family has a `_notthinking` profile; its stages carry `reasoning.enabled is False`.
  - `stage_reasoning_effort` table: `enabled:false → "none"`, `{"effort": "none"} → "none"`, absent → `None`, `{"effort": "minimal"} → "minimal"`.
  - `reasoning_request_params({"effort": "none"}) == {"reasoning": {"enabled": False}}`.
  - `effort_style_label("none") == "notthinking"`, `profile_name_for("30m", "gpt-5.6-luna", "none") == "30m_gpt-5.6-luna_notthinking"`.
- `make test` and `make cf-client-smoke` green.

---

## 5. Implementation steps (ordered)

1. **`tools/add_candidate.py`**
   - `_build_stage_config`: emit `reasoning={"enabled": False}` for `effort == "none"` (R2).
   - `_probe_effort`: probe enabled-false form for `"none"`; record `none_style` (R2).
   - `migrate_legacy_thinking`: add a `keep_keys`/`one_to_one` mode (default for this migration): `_thinking → reasoning={"enabled": true}` and `_notthinking → reasoning={"enabled": false}`, **keys unchanged**; existing upgrade mode (effort tiers + rename) stays opt-in via `--upgrade-efforts`.
2. **`data/candidates.json`**: run the 1:1 migration (all 78 legacy + 2 plain) + add the 5 none profiles (R1, R3). Preserve `hidden` flags and `capabilities` entries (append `none_style` results per model).
3. **`core/reasoning.py`**: `reasoning_request_params` collapse (R4); docstring updated — legacy `extra_body` no longer produced by the profile set ("kept byte-identical" note removed; legacy support retained only for back-compat of in-flight stages).
4. **`core/cf_client.py`**: `thinking=None` pass-through for plain payloads (R4).
5. **`cloud_function/handler.py`**: collapse `reasoning={"effort":"none"}`; emit `reasoning.enabled` only when `thinking is not None` (R4).
6. **`tools/gen_profile_literal.py`** → regenerate `candidate_spec.py` (R5). No generator change needed (it already serialises `reasoning`/`reasoning_effort` verbatim).
7. **`cloud_function/cf_client_smoke.py`** + **`tests/test_reasoning_migration.py`** (R6).
8. **`.notes/serverless-summarizer.md` + `core/reasoning.py` docstring**: update the reasoning-control note (native-only; "none" = `enabled:false`).

---

## 6. Edge cases

- **Resume compatibility:** profile keys are stable (R1), so `--resume <run_id> --profile <legacy-name>` keeps working; old run artifacts are untouched (stage configs only shape new requests). The CF-handler change (emit `reasoning.enabled` only when `thinking is not None`) affects only requests that previously carried an implicit `thinking=False` — in-flight legacy runs that relied on `extra_body.thinking.disabled` still send the explicit signal via cf_client, so no behavior change for them.
- **Hidden profiles:** luna effort-low/high/xhigh/max stay hidden after regeneration; the new luna `_notthinking` profiles are visible.
- **Provider acceptance:** `reasoning.enabled: false` must be probed (R2). If a model rejects it, fall back to plain (absent) for that family's none variant and record `none_style="plain"` — documented deviation, flagged in the manifest note.
- **`max_tokens`:** none-effort stages keep unscaled `8192` (fraction 0.0) — unchanged.
- **Manifest/dashboard labels:** `effort_style_label("none") == "notthinking"` and profile names are preserved, so `_thinking`/`_notthinking` filters in the agent and dashboard keep working.
- **Judge stages:** the judge path (`judge=True`) never carries reasoning config — unaffected.
- **`data/candidates.json` vs generated py drift:** regenerate immediately after JSON edits; add a CI-style assertion (R6) that the generated file matches the JSON for reasoning fields.

---

## 7. Acceptance criteria

- [ ] `grep -r "extra_body.*thinking\|thinking.*extra_body" data/candidates.json candidate_spec.py` → no matches.
- [ ] `data/candidates.json` has exactly 97 profiles (92 + 5 new); 8 remain hidden; every family has a `_notthinking` profile.
- [ ] `candidate_spec.py` Profile `Literal` contains the 5 new keys; import succeeds; `make test` green.
- [ ] `make cf-client-smoke` prints `cf-client-smoke OK (9 checks)`.
- [ ] Probe run for gpt-5.6-luna / nemotron-3.5-lightning / step-3.7-flash shows `reasoning.enabled:false` accepted (or records `none_style="plain"` fallback with a manifest note).
- [ ] `tests/test_reasoning_migration.py` passes offline.

---

## 8. Out of scope

- Adding `_thinking` (enabled) variants to families that lack them (muse-glimmer, luna, nemotron, step) — not requested.
- Upgrade migration (`_thinking → effort-high` with rename) — remains opt-in via `--upgrade-efforts`.
- litellm gateway / direct-OpenRouter routing — already fixed in a prior change.
- Touching `tools/batch_summarize.py`, `dashboard/`, or run artifacts.