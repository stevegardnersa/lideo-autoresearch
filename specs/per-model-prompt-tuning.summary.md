# Summary: Per-Model Prompt Tuning

**Spec:** `specs/per-model-prompt-tuning.md`

## What

New **Prompts** tab (enables the disabled Settings nav placeholder) plus a registry job tool `tune_prompt` (`autoresearch/tune_prompt.py`). Select a model → select its profile → search prompt variants → adopt the best for that model. Reuses the autoresearch loop; never mutates `candidate_spec.py`.

Default/shared chapter prompt (same components on every profile) is the baseline; per-model overrides land in `data/optimized_prompts/<perm_key>.json` and are applied at runtime by `candidate_spec.get_candidate()` via the permutation store.

## Objective — hit word target, minimise passes

Extended `evaluate` parsing of run manifests adds `final_hit_rate`, `first_pass_hit_rate`, `mean_final_length_error_pct`, `mean_passes_used`, `mean_extra_passes`, `pass_cap_rate`. Default `length_score` (maximize):

```
0.55*final_hit_rate + 0.20*first_pass_hit_rate
+ 0.15*(1 - mean_final_err/0.25)
- 0.10*(mean_extra_passes/(max_passes-1))
- 0.10*pass_cap_rate
```

Eligibility guard: variant must keep `mean_faithfulness ≥ 0.70`, `mean_concept_coverage ≥ 0.60`, and not worsen hard-fail rate by > 0.05. Otherwise baseline retained (`adoptedVersion: 0`). Scoring gates/tolerance are fixed and untunable (anti-gaming). Ties: fewest extra passes → highest faithfulness → earliest version.

## Tuning surface

Existing 7 prompt dimensions + new `length` dimension (`length_policy`: `explicit_budget_first`, `outline_then_fill`, `verbatim_target_echo`) + advanced repair-policy overrides (`repair_more`/`repair_less`, stored as a `length_control` overlay). Search space needs no notes; without notes it cycles length-correlated dimensions.

## Backend

- `SCRIPT_REGISTRY` entry `tune_prompt` (group `run`, `runtimeClass: llm`) with args model, candidate, bench, objective, mode, max-iter, max-variants, quality floors, include-notes, dry-run, json-out.
- CLI forwards Cloud Function args like `agent.py`; writes permutation + JSON + markdown report (`artifacts/autoresearch/`).
- New read APIs: `GET /api/optimized-prompts`, `GET /api/optimized-prompts/:key`, `POST /api/optimized-prompts/:key/revert` (pointer reset, non-destructive).
- Permutation history entries gain length metrics + optional `length_control`; `_apply_permutation_overrides`/`resolve_variant` apply both. Old files stay readable.

## Notes question — answer

Yes, but secondary, and the tool must **not** require them. Notes map to 7 qualitative dimensions and decide which dims to perturb; the length/pass target is already measured deterministically from manifests, so the tuner can hill-climb with zero human input. Notes help by (1) adding `length`/`passes` dimension slugs + auto-tag/keyword entries so "first draft 40% over" becomes a signal, (2) diagnosing why length misses (steers which qualitative dim to move), (3) providing rationale for quality-guard rejections. Ship metric-first; add `length`/`passes` dims + optional tuning brief.

## Key risks / decisions

- `length_policy` needs a new `LENGTH_POLICIES` map in `candidate_spec.py`; overlay is ignored until added.
- Existing `agent.py` exits with zero notes — `tune_prompt.py` must fall back to metric-only.
- Baselines measured with fixed scoring config so variants are comparable.
- No parallel evaluations; one LLM job at a time (existing guard).

## Testing

Objective math on fixture manifests, eligibility guard, adoption/revert via `get_candidate`, metric-only (empty notes) run, notes mixing, API shapes, tool-registry presence, edge cases (baseline failure, all-variant failure, old manifest), all offline via mock generator.
