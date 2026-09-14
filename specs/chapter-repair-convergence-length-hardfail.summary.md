# Summary: Repair-less Convergence Fails on Over-Length Chapters (length hardfail)

## WHAT
Dense chapters whose first pass lands ~25%+ over `target_words` exhaust the repair-less pass budget and exit above hard length tolerance → hardfail `length_outside_hard_tolerance`. Verified on `getting-things-done-allen-2015:013` (target 1288, hard range [1159, 1417]):

- chapter-p2 (baseline): final 1784 (+38%), passes=5 → hardfail
- chapter-p6 (FK test): final 1753 (+36%), passes=5 → hardfail

Identical in both versions → **independent** of the FK<10 reading-grade work.

## WHY best-pass doesn't fix it
Best-pass selection (`core/run_candidate.py:1213-1233`) picks the closest *existing* pass. With **zero** in-range passes it can't help. The bug must be fixed **before** selection.

## ROOT CAUSE
Repair-less loop stalls at `early_stop_patience=1` / `stop_on_direction_flip`:
`repair_sentence_delete=True` is set but not enforced — the model makes word-level trims instead of deleting sentences, real per-pass reduction stays <8%, and one stalled pass (|Δ| ≤ epsilon) stops the loop while the summary is still +36% over.

## FIX (led: C+B, per decision)
- **C** — Enforce real sentence deletion (strengthen shrink prompt, primary mechanism = remove low-value sentences) + require per-pass min reduction `repair_min_reduction_pct`; scale requested cut to close gap within remaining budget.
- **B** — Keep issuing repair-less while `words > (1 + hard_pct) * target`; early-stop patience applies only after inside hard tolerance.
- **A** — fallback (noted): raise `early_stop_patience` 1→2-3, relax `stop_on_direction_flip` on less-side.
- **D** — surface (per decision): when over hard tolerance AND passes == max_passes, log **non-fatal** warning (logger + manifest field `length_convergence_warning`, e.g. sample_id/words/overshoot%). No hard-fail.

## SCOPE
Only the repair-less convergence path in `run_length_controlled_stage`. `scoring.py`, FK gate, other gates, best-pass selection, repair-more path: **unchanged**. FK<10 work (chapter-p5/p6) out of scope.

## TEST
Offline mock: seed +35% over first pass → assert continues past old patience-1 stall; under-cutter exits non-fatal with warning after full budget; sentence-deleting model converges in ≤ max_passes; in-range exit unaffected; repair-more untouched; warning field present on gtd:013-style runs, absent on converging runs.

## STATUS
Draft, not implemented. Spec: `specs/chapter-repair-convergence-length-hardfail.md`.