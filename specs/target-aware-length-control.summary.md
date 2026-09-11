# Summary: Target-Aware Generation-Time Length Control

## Problem (evidence)

All four luna runs (det/jud × effort-medium/minimal, chapter_fast-v3, 2026-09-11) fail 4/8 samples with `length_outside_hard_tolerance` — best-of-5-pass err +11.9% to +39.4%, all over-length, all 5 passes exhausted, judge irrelevant (objective gate). Root causes are generation-side, systemic across ~40 profiles (median length-only fail rate ≈ 0.5; nemotron/mimo first-pass overshoot 97–965%):

1. No generation-time length constraint: `stage.max_tokens` is effort-scaled to 9103/16384 (luna minimal/medium) while targets are 353–1706 words → 9.6–25.8× headroom.
2. Weak repair prompts: `render_repair_user`/`render_composer_repair_user` state only target + range; no measured word count or numeric cut target → only ~5–20% trim over 4 repairs, can't converge to ±8% band in 5 passes.

## Fix A — target-aware max_tokens cap

`core/reasoning.py` adds `visible_max_tokens(target_words, *, effort, headroom=1.6, cap)` + constants (`TOKENS_PER_WORD=1.4`, `JSON_OVERHEAD_TOKENS=200`, `DEFAULT_MIN_OUTPUT_TOKENS=512`): `ceil(target×1.6)×1.4 + 200`, inflated by `1/(1−effort_fraction)` (identical math to `scaled_max_tokens`), clamped to [512, cap]. `generate_pass` (run_candidate.py:983) computes it per pass from `pass_target` and `stage_reasoning_effort(stage_config)` and passes it via a new `max_tokens=None` override param on `build_openrouter_request` (candidate_spec.py:641). Truncation-retry path (0.7× pass_target, "at most N visible words", max 1 retry) unchanged and absorbs tight caps.

Honest expectation: the reasoning reservation bounds cap tightness — at effort-medium the cap is soft for mid targets (1288w → ~6170 tok won't bind a 2267w first pass); it kills the catastrophic tail, while convergence is Fix B's job. Effort none/legacy → un-inflated tight cap (target×1.6×1.4+200), the intended squeeze for non-reasoning profiles.

## Fix B — numeric repair prompts

Both `render_repair_user` (candidate_spec.py:549) and `render_composer_repair_user` (run_candidate.py:769) gain `current_word_count: Optional[int] = None`; when set, insert before the policy: "Current summary is {N} visible words; target {T} (range {L}-{H}). Cut approximately {N−H} (or Add approximately {L−N}) words so the final markdown lands inside [{L},{H}]. Count your output's words before finishing." Callers pass `current_word_count=words` (already computed at loop :1037). `None` → byte-identical to today (resume back-compat). `regenerate_from_source` branch untouched.

## Decisions locked (from plan review)

- General fix, all profiles; 1.6× headroom; gate stays 0.10 + 100-word floor; `max_passes` 5; no profile/JSON changes (cap derived, not stored — resume-safe, no checkpoint migration).

## Tests — new tests/test_length_control.py

Exact `visible_max_tokens` table (353 minimal=1101 <9103; 353 medium=2382; 1288 minimal=3430; 1288 medium=6173≤cap; monotonic; cap/floor 512; effort None=="none"); `build_openrouter_request` override + default byte-identity; repair prompts carry delta with computed cut literal + `[low,high]` band, absent when None, composer identical; stub-module integration asserting every pass request's `max_tokens` and `current_word_count`. `make test` gains the file.

## Acceptance

Luna re-runs (4) → hard_fail_rate 0.0, mean passes ≤~3, no truncation-retry spike, cost not up; next verbose-profile run caps overshoot (no 965% cases). If a medium straggler persists at 11–15%, log as evidence-driven follow-up (per-effort headroom or max_passes) — gate stays 0.10.

## Files

`core/reasoning.py`, `core/run_candidate.py`, `candidate_spec.py`, `tests/test_length_control.py`, `Makefile`. Out of scope: gates, max_passes, per-profile overrides, cf_client/cloud_function/dashboard/scoring, historical artifacts, best-pass selection (separate shipped spec).