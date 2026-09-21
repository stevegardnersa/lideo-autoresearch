Cloud Function can now loop (generate → measure → repair) and self-correct word count. Opt-in via `auto_length`.

Spec: specs/cf-auto-length-loop.md

WHAT
Before: CF returned pass 1 verbatim. Loop + repair prompts + length config lived only in candidate_spec/core.run_candidate, unreachable from CF. Now: `POST /summarize {"auto_length": true, "target_words": N}` runs the same loop offline uses, returns the BEST pass (closest to target, earliest on ties), sums cost across passes, reports `meta.auto_length/passes_used/first_pass_visible_words/final_visible_words`. Default (no flag) unchanged: 1 pass.

HOW
- `core/length_control.py` — transport-agnostic loop (already extracted). Gained repair knobs (repair_target_mode, repair_sentence_delete, repair_min_reduction_pct, repair_more/less_prompt_id) + `LengthControlConfig.from_mapping()` (partial/unknown-safe) so a baked registry mapping can build it.
- `core/repair_prompt.py` (NEW) — single source of truth for repair text. `visible_word_range`, `sentence_budget`, `repair_numeric_delta`, `render_repair_prompt`, `chapter_repair_template`, `composer_repair_template`. No candidate_spec import.
- `candidate_spec.render_repair_user` + `core/run_candidate.render_composer_repair_user` now delegate to it (byte-identical output — parity-verified across all profiles).
- `core/prompt_registry.py` — version records gain optional `length_control` (loop knobs) + `repair_templates` ({more,less}); carried through create_version/revise_version; `_clean_mapping` drops blanks/non-scalars.
- `tools/bake_length_control.py` (NEW) — fills those two fields into every version that lacks them. Additive + idempotent, `--dry-run`/`--force`. Never rewrites prompt templates → fingerprints unchanged; registry_sha does change → run sync_prompts.py after.
- `cloud_function/handler.py` — `auto_length` flag; `_run_auto_length` wires `run_length_control`; config precedence request `length_control` > baked version > defaults; repair templates from version (fallback builder for un-baked versions); per-pass token cap via `_target_aware_max_tokens` (mirrors candidate_spec: visible_max_tokens, floored at target×1.35×1.15, capped by CF_MAX_TOKENS/8192). 400 `invalid_auto_length` if target_words<=0 or summary_md present.
- `tests/test_cf_auto_length.py` (NEW) + Makefile — offline: template parity, registry round-trip, shipped bundle baked, handler loop with FakeClient, override, validation.

KEY DECISIONS
1. One loop, two hosts: CF and offline run identical code; no CF fork.
2. CF does not import candidate_spec — config/templates baked offline into registry.
3. Best-pass return, not last pass (inherits run-length-best-pass-selection).
4. Opt-in: default path byte-identical; legacy un-baked versions still work via defaults + fallback template.
5. Security: auto_length/length_control are operator-controlled (cost amplification) — don't pass through from browser.

VERIFIED
- make test green; parity check old-vs-new = 0 mismatches.
- Live probes (deployed, chapter-p18, registry_sha a98e576d…): target 120 → 116 in band, 1 pass; target 350 → 320→335 in band, 3 passes; target 40 → 55→37, 5 passes (tight-target best-effort); target_words=0 → HTTP 400 invalid_auto_length.

Files: core/length_control.py, core/repair_prompt.py (new), core/prompt_registry.py, candidate_spec.py, core/run_candidate.py, cloud_function/handler.py, tools/bake_length_control.py (new), tests/test_cf_auto_length.py (new), Makefile, cloud_function/README.md, .notes/serverless-summarizer.md, data+cf prompts/registry.json.
