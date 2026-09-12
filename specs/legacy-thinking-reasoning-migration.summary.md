# Summary: Legacy Thinking/Plain Variants → New Reasoning Format + "None" Option

**Status:** Implemented — includes mandatory-reasoning model enforcement (phantom `_notthinking` profiles removed)

## Mapping answer (1:1)

| Legacy signal | New native format | 1:1 |
|---|---|---|
| `extra_body.thinking.type=enabled` | `reasoning={"enabled": true}` | Yes (handler already does this) |
| `extra_body.thinking.type=disabled` | `reasoning={"enabled": false}` | Yes (`stage_reasoning_effort` → `"none"`) |
| plain (no config) | no `reasoning` field (absent) | Yes, as "unset" |
| scalar `reasoning_effort=X` | `reasoning={"effort": X}` | Yes |
| scalar `reasoning_effort="none"` | `reasoning={"enabled": false}` | Yes (already collapsed) |

Not 1:1 — decisions: `DEFAULT_THINKING_EFFORT="high"` + `migrate_legacy_thinking` rename `_thinking → _effort-high` is an upgrade mapping (breaks resume); this spec picks key-stable `enabled:true` instead. Canonical "none" option = `reasoning={"enabled": false}` (NOT absent — provider default stays ON for reasoning models; NOT `{"effort":"none"}` — forwarded verbatim today, may 400).

## Scope

`data/candidates.json` is the source of truth (**95 profiles**; `candidate_spec.py` generated). All 78 legacy `extra_body.thinking.*` carriers migrated to native `reasoning` (0 remain); 41 `_notthinking` profiles. **Mandatory-reasoning model** `stepfun/step-3.7-flash` (probe `none_style="mandatory"`, efforts `["medium"]`): its provider rejects `reasoning.enabled=false` AND still reasons on a plain request, so the two phantom `step-3.7-flash_notthinking` profiles were **removed** — the family keeps `_effort-medium` only. 8 profiles stay hidden.

## Requirements

- **R1** Migrate all legacy profiles to native format, profile keys unchanged (resume-safe).
- **R2** Canonical none = `reasoning={"enabled": false}`: `_build_stage_config` emits it for `effort=="none"` (today emits nothing); probe the enabled-false form, record `none_style` = `"enabled_false"` / `"mandatory"` / `None`.
- **R3** Add `_notthinking` profiles for families lacking one **except mandatory-reasoning models**; invariant: every (time×model) family minus `step-3.7-flash` has exactly one none variant. Tooling refuses `effort="none"` on mandatory models (raises, never defaults); phantom step profiles removed.
- **R4** Normalisation consistency: collapse `reasoning={"effort":"none"}` → `enabled:false` in `reasoning_request_params` + handler; handler emits `reasoning.enabled` only when `thinking is not None`; cf_client stops defaulting plain payloads to `thinking=False`. **Mandatory guard:** disable intent on a registered mandatory model raises `ValueError` (`assert_disabling_reasoning_allowed`) on the direct path, `build_openrouter_request`, and CF client — never silently sends a default-effort request.
- **R5** Regenerate `candidate_spec.py`: `py_literal()` fixes JSON bool/null → Python literals; emits `MANDATORY_REASONING_MODELS` frozenset from capabilities and registers it via `register_mandatory_reasoning_models`.
- **R6** cf-client-smoke → **10 checks** (add effort-none dict collapse, plain → no reasoning field, mandatory-disable raises); new `tests/test_reasoning_migration.py` (offline invariants, naming, normalisation table, mandatory guards).

## Files touched

`tools/add_candidate.py` (builder + probe + `one_to_one` migrate mode + mandatory refusal), `data/candidates.json`, `core/reasoning.py` (+ mandatory registry/guard), `core/cf_client.py`, `cloud_function/handler.py`, `candidate_spec.py` (regenerated), `cloud_function/cf_client_smoke.py`, `tests/test_reasoning_migration.py`, `tools/gen_profile_literal.py`, `.notes/serverless-summarizer.md`, docstring in `core/reasoning.py`.

## Edge cases

Resume compat via stable keys; hidden luna effort-low/high/xhigh/max stay hidden; **mandatory reasoning (step) fails loudly everywhere** — probe refuses, tooling raises, request builders raise; none-effort keeps unscaled max_tokens (8192); `effort_style_label("none")=="notthinking"` preserves agent/dashboard `_thinking`/`_notthinking` filters; judge path unaffected.

## Acceptance

No `extra_body.thinking` anywhere; 95 profiles / 43 families, `_notthinking` for all but the two mandatory step families; `make test` + `make cf-client-smoke` (**10 checks**) green; probe records `enabled_false` for luna/nemotron and `mandatory` for step; migration tests pass offline.

Out of scope: adding `_thinking` variants to families lacking them; effort-tier upgrade renames (opt-in `--upgrade-efforts`); litellm gateway; dashboard/batch tool changes.