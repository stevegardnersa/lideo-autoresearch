# Spec: Pick Closest-to-Target Pass in Length-Controlled Generation

**Feature:** Fix pass selection in `run_length_controlled_stage` so that when a stage exits — whether because a pass landed inside the target word-count range or because the pass budget (`max_passes`) ran out — the returned summary is the pass whose visible word count is *closest* to `target_words`, not merely the last pass generated.

**Input needed:** None — complete behavior contract.

**Status:** Draft for implementation

---

## 1. Goals

1. **Best-pass selection.** The `StageRun.summary_md` returned by `run_length_controlled_stage` is the pass with the minimum `|visible_word_count(pass.md) − target_words|` among all passes executed for that stage.
2. **Applies to every exit path.** Selection happens on (a) in-range break and (b) `max_passes` exhaustion.
3. **No behavior regression.** Cost accounting, `passes_used`, repair directions, `first_pass_summary_md` semantics, and the LLM call sequence are unchanged.
4. **Resume-safe.** A run resumed from a checkpoint still considers passes generated before the interruption.

## 2. Non-Goals

- No change to *when* passes are generated (same loop, same repair prompts, same exit conditions — the loop still stops as soon as a pass is in range).
- No change to `visible_word_range`, `LengthControlConfig` defaults (`max_passes=5`, `tolerance_pct=0.05`; book profiles `/0.08`), or repair strategies.
- No change to `extractive_mock_summary` (mock path builds to target deterministically) or to the `disable_composer` path.
- No change to scoring formulas themselves (`length_error_pct`, `length_accuracy`, hard-fail gates) — only the input summary fed to them.
- No change to `first_pass_summary_md` (must remain the first pass's text, used for first-pass length metrics).

## 3. Context: Existing System

- File: `core/run_candidate.py`, function `run_length_controlled_stage` (line ~735).
- Callers (all three stage kinds go through this one function):
  - Book/intro stage — `stage_kind="chapter"`, call at line ~975.
  - Chapter summary stage — `stage_kind="chapter"`, call at line ~1242.
  - Composer stage — `stage_kind="composer"`, call at line ~1364.
- Loop shape (current):
  1. Initial pass (if `passes_used <= 0` or no `summary_md`) → `summary_md = result.summary_md`, `passes_used = 1`.
  2. `while passes_used < max_passes`: if `low <= visible_word_count(summary_md) <= high` → `break`; else generate a repair pass (direction = `"more"` if below `low`, `"less"` if above `high`) that overwrites `summary_md`, increments `passes_used`, appends to `responses`, accumulates cost.
  3. Return `StageRun(summary_md=summary_md, …)`.
- **Bug:** `summary_md` is overwritten per pass and the last value is returned unconditionally. On `max_passes` exhaustion the final repair can be *worse* than an earlier pass (e.g. pass 1 = 501 words off target, final repair overshoots to 900 words off). The stage then returns the worse summary, and downstream scoring (`deterministic_metrics`, hard-fail gate `length_outside_hard_tolerance` at `scoring.py:542`) evaluates the wrong text.
- Checkpoint/resume: `checkpoint_callback` persists `stage_state` with `summary_md` (latest pass), `first_pass_summary_md`, `passes_used`, cost fields, `raw_responses`. Resume (`resume_state`) restores these. A selection fix that only tracks "best so far" in memory would lose the best pass across a resume — checkpoint schema must carry it.
- Downstream consumers of the returned `summary_md`: run manifest `output_words`, chapter/composer rows (lines ~1024, 1048, 1196, 1278, 1461), `sample.summary_md` fed to `score_dataset`.

## 4. Required Behavior

### 4.1 Selection rule

After the loop terminates (either exit path), select the pass `p` minimizing

```
distance(p) = |visible_word_count(p.summary_md) − target_words|
```

and return `p.summary_md` as `StageRun.summary_md`. The in-range exit is a strict subset: any in-range pass has `distance ≤ tolerance_delta` (where `tolerance_delta = max(1, round(target_words * tolerance_pct))` from `visible_word_range`), and every pass *preceding* an in-range pass was out-of-range with `distance ≥ tolerance_delta + 1` — so a uniform minimum-distance selection is always safe there and also satisfies the requested "check previous runs when within range".

### 4.2 Tie-breaking

- If two or more passes have identical distance, keep the **earliest** pass among the tied ones (strict `<` comparison when updating the best candidate). This is deterministic and independent of run/timestamp variation.
- A pass with `distance == 0` (exact target hit) immediately wins; later passes cannot beat it, but generation still continues per the existing loop rules (the loop breaks on the in-range check anyway).

### 4.3 Pass tracking

- Maintain the running best alongside the current summary:
  - `best_summary_md`: the pass with minimum distance seen so far (tie → earlier pass).
  - `best_distance`: `|visible_word_count(best_summary_md) − target_words|`, recomputed after each pass.
- The *current* summary remains the loop's working variable: repair prompts and mock input use the latest pass (`current_summary_md`), exactly as today. Selection changes nothing about how the *next* pass is generated.
- After each pass (initial and each repair), update `best_summary_md` if `distance(pass) < best_distance`.

### 4.4 Unchanged fields

- `passes_used`: total passes executed (not the index of the best pass).
- `generation_cost` / `uncached_generation_cost`: all passes billed; sums unchanged.
- `raw_responses`: all passes' raw responses retained, in order.
- `first_pass_summary_md`: first pass's text, unchanged.
- Loop exit conditions: unchanged (`break` when current pass in range; `while passes_used < max_passes`).

## 5. Checkpoint / Resume Contract

The `stage_state` dict written by `checkpoint_callback` (and restored from `resume_state`) gains two fields:

| Field | Type | Meaning |
|---|---|---|
| `summary_md` | `str` | Latest pass (working variable for repair continuation) — unchanged semantics |
| `best_summary_md` | `str` **new** | Best-distance pass among passes executed so far |
| `best_distance` | `int` **new** | `|visible_word_count(best_summary_md) − target_words|` at checkpoint time (optional if recomputed on restore; recomputing avoids trusting stale state) |

Restore rules:
- On resume, `summary_md` (latest), `first_pass_summary_md`, `passes_used`, costs, and `raw_responses` restore exactly as today.
- `best_summary_md` restores if present; if absent (checkpoint written by an older version), initialize it from the restored `summary_md` so a pre-existing checkpoint still yields correct behavior for the remaining passes. `best_distance` may be recomputed from `best_summary_md` on restore rather than stored (cheap: one `visible_word_count` call).
- When the restored `best_summary_md` is empty/absent and `summary_md` is empty, the initial-generation branch runs as today and populates both.
- `stage_state` keys used by downstream progress rendering (e.g. `progress["composer_stage_run"]`, `serialize_stage_run`) are unaffected — the extra keys live inside `stage_state` only.

## 6. Downstream Effects

- `StageRun.summary_md` is now the best-distance pass → `output_words` in run rows and manifests report the selected pass's count (matching the text, since `output_words` is recomputed from `summary_md` via `visible_word_count` — no double-count to fix).
- `deterministic_metrics` scores the best pass: `final_length_error_pct` and `final_length_accuracy` improve (never worse) vs. the last-pass behavior. The hard-fail length gate (`scoring.py:542`) can flip from fail to pass in `max_passes`-exhaustion cases; that is the intended outcome.
- Judge inputs unchanged in mechanism: judging still reads `summary.summary_md`, which is now the best pass.
- No visible UI/format changes (specs/dashboard-* untouched).

## 7. Edge Cases

1. **Single pass** (pass 1 already in range): `best_summary_md == summary_md`; behavior identical to today.
2. **max_passes exhaustion, last pass worst**: regression case — earlier closer pass wins. Covered by 4.1.
3. **max_passes exhaustion, last pass best**: last pass wins; identical to today.
4. **Distance tie across out-of-range passes** (e.g. pass 1 at −200 words, pass 2 at +200 words): earliest pass (pass 1) selected — deterministic.
5. **In-range exit**: current pass selected; per 4.1 a previous pass can never be strictly closer, and ties would require a previous in-range pass (impossible — the loop would have broken sooner).
6. **Resume mid-loop**: best pass from before interruption preserved via 5.
7. **Resume with old-format checkpoint**: `best_summary_md` initialized from restored `summary_md`.
8. **Zero/empty pass text** (`visible_word_count` returns 0 for empty): distance = `target_words` (large, so such a pass only wins if every other pass is also degenerate) — same degenerate-input behavior as today, no special handling.
9. **Mock mode / `disable_composer`**: bypass this function; unchanged.

## 8. Performance

- One extra `visible_word_count` per pass (reuse the count already computed in the loop's range check where possible; the loop already calls it at the top of each iteration — hoist and reuse the value for both the range check and the best-distance update).
- No additional LLM calls, no extra network round trips. Memory: one extra string reference per stage (best pass) plus optional `best_distance` int.

## 9. Testing Requirements

1. **max-passes regression**: target such that no pass lands in range; pass 1 closer than the final pass → returned `summary_md` equals pass 1's text; `passes_used` equals `max_passes`; cost equals sum of all passes.
2. **In-range exit**: pass 2 in range → returned is pass 2; a (hypothetically closer) earlier out-of-range pass is not selected — assert the in-range pass's distance is ≤ any earlier out-of-range pass's distance.
3. **Tie**: two passes at ±equal distance, neither in range → earliest selected.
4. **Exact hit**: a pass at exactly `target_words` selected over any earlier/later pass.
5. **Resume**: checkpoint after pass 1 (best), resume, pass 2 worse, budget exhausted → pass 1 returned; also test resume *without* `best_summary_md` in the checkpoint (old-format compatibility).
6. **first_pass stability**: `first_pass_summary_md` equals the initial pass's text in all of the above.
7. **Downstream**: `output_words` equals `visible_word_count(summary_md)` of the selected pass; `final_length_accuracy` for the max-passes case does not decrease vs. last-pass behavior on the same fixture.

All tests must run offline against the mock generator (`invoke_generation` with `mock_source_md`) to avoid LLM calls.