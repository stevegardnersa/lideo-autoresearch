Bug: run_length_controlled_stage picks LAST pass, not CLOSEST-to-target pass.

Spec: specs/run-length-best-pass-selection.md

WHAT
When a length-controlled stage (book intro, chapter, composer) runs multiple repair passes, the returned StageRun.summary_md was always the last pass generated — even on max_passes exhaustion when an earlier pass was closer to target_words. Fix: after loop exit (both paths: in-range break, max_passes exhaustion), return the pass minimizing |visible_word_count − target_words|.

CONTEXT FOUND (verified in code)
- core/run_candidate.py:735 run_length_controlled_stage. Loop: initial pass → while passes_used < max_passes: if current in [low,high] break; else repair pass overwrites summary_md. Returns StageRun(summary_md=summary_md, …) — last pass wins unconditionally.
- Three callers, one shared function: book stage (line ~975), chapter stage (~1242), composer stage (~1364). All inherit the bug.
- Defaults: LengthControlConfig(max_passes=5, tolerance_pct=0.05); book profiles tolerance 0.08 / hard 0.15. visible_word_range delta = max(1, round(target*pct)).
- Downstream: returned summary_md → run rows output_words (lines 1024/1048/1196/1278/1461), sample.summary_md → deterministic_metrics (final_length_accuracy) + hard-fail gate length_outside_hard_tolerance (scoring.py:542). Selecting the closer pass can flip max-passes-exhaustion hard fails to passes — intended.
- Checkpoint/resume: stage_state persists summary_md (latest), first_pass_summary_md, passes_used, cost, raw_responses. In-memory-only best tracking would lose the best pass across resume → checkpoint schema must carry best_summary_md.

KEY SPEC DECISIONS
1. Selection rule: min |visible_word_count(pass.md) − target_words| over ALL passes, both exit paths. In-range exit is a strict subset: in-range distance ≤ delta, any earlier pass out-of-range ≥ delta+1, so uniform min-distance is safe there (also satisfies "check previous runs when within range").
2. Tie-break: keep EARLIEST pass among equal-distance (strict < replace). Deterministic.
3. Exact hit (distance 0): wins; loop still breaks per existing rules — no behavior change to generation.
4. Loop/generation unchanged: current summary is still the working variable for repair prompts + mock input; passes_used, cost (all passes billed), raw_responses order, first_pass_summary_md semantics all unchanged. Only final selection changes.
5. Resume contract: stage_state gains best_summary_md (+ optional best_distance; recomputable on restore). Old-format checkpoint (no best_summary_md) → initialize from restored summary_md.
6. Perf: reuse the visible_word_count already computed in the loop's range check for the distance update — one string reference + int extra per stage, zero extra LLM calls.
7. Tests: offline via mock generator; cover max-passes regression (earlier closer pass wins), in-range exit, tie → earliest, exact hit, resume with/without best_summary_md, first_pass stability, output_words matches selected pass.

Files written:
- specs/run-length-best-pass-selection.md (main spec)
- specs/run-length-best-pass-selection.summary.md (this summary)

Not implemented: no code written. scoring.py formulas, level control config, dashboard specs untouched. Applies only to core/run_candidate.py selection logic.