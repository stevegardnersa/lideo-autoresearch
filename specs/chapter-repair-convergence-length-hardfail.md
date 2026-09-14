# Spec: Repair-less Convergence Fails on Over-Length Chapters (length hardfail)

**Feature:** Dense chapters — those whose first pass lands roughly 25%+ over `target_words` — exhaust the repair-less pass budget (`max_passes`) and exit above the hard length tolerance, hard-failing `length_outside_hard_tolerance`. Fix the repair-less convergence path so over-length chapters either converge into tolerance or surface clearly (non-fatal).

**Input needed:** None — complete behavior contract.

**Status:** Draft for implementation

---

## 1. Goals

1. **Converge, don't stall.** A chapter whose best repair-less pass is still over `(1 + hard_tolerance_pct) * target_words` keeps getting repair-less passes until either (a) it lands inside hard tolerance or (b) the pass budget is exhausted — not cut short by early-stop patience.
2. **Real sentence deletion.** `repair_sentence_delete` must actually remove low-value sentences (not just word-level trims), and each repair pass must make measurable progress toward target.
3. **Explicit surface, not silent best-pass.** When a stage exits over hard tolerance *and* used the full pass budget, log a clear, non-fatal warning identifying the chapter and the residual overshoot. Do **not** hard-fail.
4. **Preserve the FK<10 work.** This change is strictly independent of the reading-grade work (chapter-p5/p6). No overlap in code paths or config.

## 2. Non-Goals

- No change to pass *selection* (best-pass selection at `core/run_candidate.py:1213-1233` stays as-is).
- No change to `scoring.py` formulas, the FK/readability gate, or any other gate.
- No change to the over-short / repair-more path (only repair-less).
- No hard-fail behavior introduced.

---

## 3. Evidence

Repro: `bench/chapter_fast-v3-grade4.jsonl`, item `getting-things-done-allen-2015:013` (the `getting-things-done` ch.13).

| Version | target_words | hard range (±10%) | final words | passes_used | result |
|---|---|---|---|---|---|
| chapter-p2 (baseline) | 1288 | [1159, 1417] | 1784 (+38%) | 5 | hardfail `length_outside_hard_tolerance` |
| chapter-p6 (FK test) | 1288 | [1159, 1417] | 1753 (+36%) | 5 | hardfail `length_outside_hard_tolerance` |

- p2 and p6 fail **identically** → the bug is independent of the reading-grade prompt change.
- All 5 passes stay above hard tolerance → best-pass selection (already implemented) cannot help: it only picks the closest *existing* pass. Whatever fix lands must act **before** selection.

## 4. Root Cause (verified in code)

`LengthControlConfig` defaults relevant to the less-side (in `candidate_spec.py`):

- `max_passes = 5`
- `early_stop_patience = 1`
- `early_stop_epsilon = 3`
- `repair_min_reduction_pct = 0.08`
- `repair_sentence_delete = True`
- `stop_on_direction_flip = True`

Loop behavior (`core/run_candidate.py` `run_length_controlled_stage`):

1. Initial pass produces a summary ~35-40% over target (dense chapters over-write; the model packs content, not trims).
2. Repair-less passes ask the model to shrink toward target. The model under-cuts: real per-pass reduction stays well below `repair_min_reduction_pct` (here <8% needed per pass to converge 1753→1417 is ~20% total; the model makes word-level trims instead of deleting sentences).
3. Early-stop fires at `patience=1`: one pass with `|prev_words - curr_words| <= epsilon` stops the loop.
4. With `stop_on_direction_flip=True`, a stalled/direction-flipped pass also exits.
5. Exit → summary still > hard tolerance → `hard_fail_reasons = ['length_outside_hard_tolerance']`.

`repair_sentence_delete=True` is passed but not enforced: the prompt permits sentence deletion yet the model does word-level trims, so reduction stalls.

## 5. Fix (C+B combined, led)

### C — Enforce real sentence deletion + per-pass minimum reduction

- In the repair-less pass, after the model returns a new draft, recompute `visible_word_count`.
- If the draft did not reduce by at least `repair_min_reduction_pct` of the prior pass's word count **and** it is still over hard tolerance, treat the pass as non-progress: do **not** apply early-stop; instead re-issue another repair-less pass.
- Strengthen the `repair_sentence_delete` shrink prompt so deletion of low-value sentences is explicitly the primary mechanism (remove sentences whose removal doesn't injure the summary's key claims), with a target per-pass cut sized to close the gap within remaining budget:
  `requested_cut = ceil(overshoot / passes_remaining)`.

### B — Keep repairing while over hard tolerance (scaled cut-up)

- Replace the `patience=1`-based stop for the less-side with a hard overshoot condition:
  - while `passes_used < max_passes` **and** `words > (1 + hard_tolerance_pct) * target_words`: continue issuing repair-less passes.
  - Early-stop patience applies only when already inside hard tolerance (i.e., after the hard-tolerance gate passes, feed early-stop for approaching soft range).
- Scale the requested cut upward as overshoot persists (flag to prompt "previous pass did not shrink enough; cut more aggressively").

### A — Fallback / noted (not primary)

- Raise `early_stop_patience` from 1 to 2-3, and/or relax `stop_on_direction_flip` on the less-side. Kept as a secondary knob in the same config; not the led mechanism.

### D — Explicit surface (non-fatal), per decision

- After the stage exits: if `words > (1 + hard_tolerance_pct) * target_words` **and** `passes_used >= max_passes`, log a warning (level=warning, run logger + run manifest field e.g. `length_convergence_warning`) naming `sample_id`, `target_words`, `final_words`, residual overshoot pct.
- This is informational only. It does **not** alter hard_fail, scoring, or best-pass selection.
- Rationale: makes convergence stalls visible in dashboards/run manifests instead of masked by best-pass selection, without breaking existing runs.

## 6. Test Plan

Offline via mock generator (same harness used for best-pass spec):

1. Seed first pass at +35% over target (mirrors gtd:013). Assert the loop keeps repairing past the old patience-1 stall point.
2. With a model that under-cuts each pass, assert the run still exits non-hard-failed (log present) after full budget when convergence is impossible.
3. With a model that deletes sentences, assert convergence into hard range within `max_passes`.
4. Assert normal in-range exit is unaffected (no extra passes when a pass lands inside tolerance).
5. Assert repair-more / over-short path untouched.
6. Assert the non-fatal warning field is present on gtd:013-style runs and absent on converging runs.

## 7. Out of Scope / Independence

- FK<10 reading-grade work (chapter-p5/p6: `plain_readable`, `sentence_split_hard`, `target_sentence_words`) is fully separate. This spec does not touch prompt templates, system styles, or sentence budgets.
- `scoring.py`, FK gate, and all other gates unchanged.
- Best-pass selection logic unchanged.