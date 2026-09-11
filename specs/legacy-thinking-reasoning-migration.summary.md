# Summary: Legacy Thinking/Plain Variants → New Reasoning Format + "None" Option

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

`data/candidates.json` is the source of truth (92 profiles; `candidate_spec.py` generated). 78 legacy profiles to migrate: 40 `_thinking`, 36 `_notthinking`, 2 plain muse-glimmer `_notthinking` (empty config — force `enabled:false` to match name). 5 families missing a none variant: gpt-5.6-luna (30m/60m), nemotron-3.5-lightning 30m, step-3.7-flash (30m/60m) → add 5 `_notthinking` profiles (97 total; 8 stay hidden).

## Requirements

- **R1** Migrate all 78 legacy profiles to native format, profile keys unchanged (resume-safe).
- **R2** Canonical none = `reasoning={"enabled": false}`: `_build_stage_config` emits it for `effort=="none"` (today emits nothing); probe the enabled-false form, record `none_style` fallback.
- **R3** Add 5 `_notthinking` profiles; invariant: every (time×model) family has exactly one none variant.
- **R4** Normalisation consistency: collapse `reasoning={"effort":"none"}` → `enabled:false` in `reasoning_request_params` + handler; handler emits `reasoning.enabled` only when `thinking is not None`; cf_client stops defaulting plain payloads to `thinking=False` (today CF forces reasoning-off where direct path leaves it unset).
- **R5** Regenerate `candidate_spec.py` (Profile Literal + 5 keys, `extra_body=None`, reasoning verbatim).
- **R6** cf-client-smoke → 9 checks (add effort-none dict collapse + plain → no reasoning field); new `tests/test_reasoning_migration.py` (offline invariants, naming, normalisation table).

## Files touched

`tools/add_candidate.py` (builder + probe + `--one-to-one` migrate mode), `data/candidates.json`, `core/reasoning.py`, `core/cf_client.py`, `cloud_function/handler.py`, `candidate_spec.py` (regenerated), `cloud_function/cf_client_smoke.py`, `tests/test_reasoning_migration.py`, `.notes/serverless-summarizer.md`, docstring in `core/reasoning.py`.

## Edge cases

Resume compat via stable keys; hidden luna effort-low/high/xhigh/max stay hidden; provider rejection of `enabled:false` → probe fallback `none_style="plain"` with manifest note; none-effort keeps unscaled max_tokens (8192); `effort_style_label("none")=="notthinking"` preserves agent/dashboard `_thinking`/`_notthinking` filters; judge path unaffected.

## Acceptance

No `extra_body.thinking` anywhere; 97 profiles / 43 families all with notthinking; `make test` + `make cf-client-smoke` (9 checks) green; probe shows enabled-false accepted (or documented fallback).

Out of scope: adding `_thinking` variants to families lacking them; effort-tier upgrade renames (opt-in `--upgrade-efforts`); litellm gateway; dashboard/batch tool changes.