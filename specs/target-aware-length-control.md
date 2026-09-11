# Spec: Target-Aware Generation-Time Length Control (Visible-Word `max_tokens` Cap + Numeric Repair Prompts)

**Feature:** Bound chapter/composer generation at request time to a per-pass `max_tokens` derived from the *visible-word target* (not the effort-scaled 8192 base), and quantify repair prompts with the measured current word count and an exact cut/add target so the length-control loop converges inside `[low, high]` within `max_passes`.

**Input needed:** None — complete behavior contract.

**Status:** Draft for implementation

**Related spec:** `specs/run-length-best-pass-selection.md` (pass selection; orthogonal — this spec does not change when passes run, only how each pass is generated and how repair prompts are phrased). May be combined with a sibling spec in a later consolidation pass.

---

## 1. Goals

1. **Generation-time length bound.** Each generation pass requests an effective `max_tokens` = `visible_max_tokens(pass_target, effort, cap=stage.max_tokens)` so the visible output cannot exceed roughly `target_words × 1.6` (+ reasoning allowance) tokens' worth of words. Catastrophic overshoot (first pass up to 965% of target in current runs) becomes impossible; the model can no longer free-run to 10–26× the target.
2. **Convergent repair loop.** Repair prompts state the measured current word count and the exact number of words to cut (or add) so each repair lands measurably closer; the loop converges inside `[low, high]` within `max_passes` instead of burning all 5 passes and still ending 11–39% over.
3. **General (all profiles).** The cap and prompt change apply to every stage (`chapter`, composer) and every profile; no per-profile overrides, no new profile fields, no generated-file changes.
4. **Gate unchanged.** `hard_fail_reasons` keeps the unified length gate (`word_diff > 100 AND pct_error > max_final_length_error_pct`; `0.10` default). Generation improves; thresholds do not move.
5. **Resume-safe and mock-safe.** No checkpoint schema change (the cap is derived deterministically from `pass_target`/`stage`, never stored); the `client=None` mock path and the `regenerate_from_source` repair strategy are untouched.

## 2. Non-Goals

- **No gate/threshold changes:** `max_final_length_error_pct` stays `0.10`; the 100-word floor stays; `hard_tolerance_pct` stays `0.15` (unused by the gate today); `tolerance_pct` stays `0.08` for chapter/composer profiles.
- **No pass-budget change:** `max_passes` stays `5`; `max_truncation_retries` stays `1`.
- **No change to *when* passes run:** same loop, same exit conditions (`low <= words <= high` break, `max_passes` exhaustion), same best-pass selection already implemented by `run-length-best-pass-selection.md`.
- **No change to `scaled_max_tokens` semantics for profile generation:** `tools/add_candidate.py` continues to store effort-scaled `stage.max_tokens` (8192 base). The cap computed here is *per request* and overrides `stage.max_tokens` only inside `run_length_controlled_stage`.
- **No per-model/provider tuning, no prompt-component changes** other than the numeric delta block.
- **No change to `extractive_mock_summary`, the CF handler, or `core/cf_client.py`.**
- **No re-scoring or re-running of historical artifacts.**

## 3. Context: Existing System

### 3.1 Where the length problem lives

- `core/run_candidate.py::run_length_controlled_stage` (`:907`) runs every stage kind (chapter, composer). Its `generate_pass` (`:983`) calls `build_openrouter_request` (`:990`), which sets `"max_tokens": stage.max_tokens` (`candidate_spec.py:661`).
- `stage.max_tokens` is the **effort-scaled** value: `tools/add_candidate.py` stores `scaled_max_tokens(8192, effort)` (`core/reasoning.py:47`). For `gpt-5.6-luna` that is **9103** (effort-minimal, fraction 0.10) and **16384** (effort-medium, fraction 0.50).
- `chapter_fast-v3` targets are **353–1706 visible words**. At ≈1.4 tokens/word, the true output budget needed for the largest target is ~2,400 tokens; the request allows 9,103–16,384. **Headroom is 9.6–25.8×** — the model is never length-constrained at generation time.
- Repair loop (`:1036–1075`): when `words` leaves `[low, high]`, it renders `render_repair_user` (chapter, `candidate_spec.py:549`) or `render_composer_repair_user` (`core/run_candidate.py:769`) and re-generates. Neither prompt contains a word count or a numeric cut target — only "Target visible words: N. Acceptable range: L-H." plus an unquantified policy ("Shorten by removing repetition.").

### 3.2 Evidence (runs/booksum-v4, 2026-09-11, gpt-5.6-luna)

Four runs (deterministic + judged × effort-minimal + effort-medium), 8 samples each; identical failures regardless of judge — the gate is objective:

| sample | medium err% (best of 5 passes) | minimal err% (best of 5) |
|---|---|---|
| eifc:001 | +17.7% | — |
| eifc:006 | +16.4% | +20.5% |
| eifc:009 | +11.9% | +30.2% |
| gtd:013 | **+39.4%** | +28.0% |
| dht:003 | — | +18.1% |

- All failures are **over-length** (never under), all exhausted `max_passes=5`, and the best of the 5 passes still fails the gate. `hard_fail_rate = 0.5` per run; reason `length_outside_hard_tolerance` in every case.
- First-pass overshoot: +16% to +76% (luna). Other models are far worse: nemotron/mimo runs reach 97–965% overshoot.
- Per-pass trimming across the 4 repairs totals only ~5–20% (measured `first_pass → best_pass` word cut), so the loop cannot converge to the ±8% band within 5 passes.
- **Systemic, not a luna bug:** across all ~40 `chapter_fast` profiles the length-only hard-fail rate runs 0–100% (median ≈ 0.5); luna at 0.54 is mid-pack.

### 3.3 Why a tighter cap is safe

The truncation-retry path already exists (`generate_pass`, `:1007–1026`): if `result.truncated`, it retries at `attempt_target = pass_target × 0.7^retry_count` with an explicit "at most N visible words" instruction, up to `max_truncation_retries` (1). A tighter cap therefore cannot produce truncated JSON without a bounded, already-specified recovery.

### 3.4 Honest expectation for the reasoning allowance

`EFFORT_TOKEN_FRACTION` (`core/reasoning.py:25`) reserves 10–95% of the budget for reasoning tokens. The cap formula divides the visible budget by `(1 − fraction)`, so:

| target (words) | effort | effective cap (tokens) | ≈ max visible words | binds a real first pass? |
|---|---|---|---|---|
| 353 | minimal (0.10) | ~1,100 | ~780 | yes (first pass was 419) |
| 353 | medium (0.50) | ~2,380 | ~1,700 | yes |
| 1288 | minimal (0.10) | ~3,430 | ~2,450 | yes (medium first pass 2,267 → cut) |
| 1288 | medium (0.50) | ~6,170 | ~4,400 | no (2,267 < 4,400) |
| 1706 | medium (0.50) | ~8,000 | ~5,700 | no |

**Fix A's constraining power is bounded by the reasoning reservation**: at effort-medium it caps the catastrophic tail (965% overshoot, first passes > ~4,400 words) but cannot squeeze visible output below the reasoning allowance. **Fix B is the convergence lever** that drives 11–39% stragglers into the band. If post-fix re-runs still show stubborn 11–15% stragglers on medium, the follow-up is a per-effort headroom reduction or `max_passes` bump (evidence-driven, separate decision) — the gate stays `0.10`.

## 4. Required Behavior

### 4.1 Fix A — `visible_max_tokens` (target-aware cap)

**`core/reasoning.py`** — new constants and helper (reuses `effort_token_fraction` and the same `/(1−fraction)` math as `scaled_max_tokens`):

```python
TOKENS_PER_WORD = 1.4            # ~0.7 words/token for English markdown
JSON_OVERHEAD_TOKENS = 200       # response_format wrapper + markdown framing
DEFAULT_VISIBLE_HEADROOM = 1.6
DEFAULT_MIN_OUTPUT_TOKENS = 512

def visible_max_tokens(
    target_words: int,
    *,
    effort: Optional[str] = None,
    headroom: float = DEFAULT_VISIBLE_HEADROOM,
    cap: int = DEFAULT_MAX_TOKEN_CAP,
) -> int:
    """Per-request max_tokens derived from a visible-word target.

    visible_base = ceil(target_words * headroom) words, converted at
    TOKENS_PER_WORD + JSON_OVERHEAD_TOKENS, then inflated by the reasoning
    effort's token reservation (same /(1-fraction) as scaled_max_tokens).
    Clamped to [DEFAULT_MIN_OUTPUT_TOKENS, cap].
    """
    fraction = effort_token_fraction(effort)
    visible_base_words = max(1, math.ceil(float(target_words) * headroom))
    tokens = visible_base_words * TOKENS_PER_WORD + JSON_OVERHEAD_TOKENS
    if fraction > 0.0:
        tokens /= (1.0 - fraction)
    return int(min(cap, max(DEFAULT_MIN_OUTPUT_TOKENS, math.ceil(tokens))))
```

Behavior contract:
- Monotonic non-decreasing in `target_words` (for fixed effort/headroom).
- Never exceeds `cap`; never below `DEFAULT_MIN_OUTPUT_TOKENS` (512) — degenerate tiny targets still get a usable budget.
- Reasoning inflation matches `scaled_max_tokens` exactly for the same effort (`visible_max_tokens(t, effort=...)` uses the identical `1/(1−fraction)` divisor).
- `effort=None` or `"none"` → no inflation (fraction 0.0).
- No storage, no profile field, no manifest change.

**`candidate_spec.py::build_openrouter_request`** (`:641`) — add a per-call override:

```python
def build_openrouter_request(*, stage, system_prompt, user_prompt, schema_name,
                             use_json_schema: bool = True,
                             max_tokens: Optional[int] = None) -> Dict[str, object]:
    ...
    "max_tokens": stage.max_tokens if max_tokens is None else max_tokens,
```

**`core/run_candidate.py::run_length_controlled_stage.generate_pass`** (`:983`) — compute the effective cap once per pass and pass it through:

```python
from core.reasoning import stage_reasoning_effort, visible_max_tokens
...
effective_max_tokens = visible_max_tokens(
    pass_target,
    effort=stage_reasoning_effort(stage_config),
    cap=int(stage_config.max_tokens),
)
request = build_openrouter_request(
    stage=stage_config,
    system_prompt=system_prompt,
    user_prompt=retry_prompt,
    schema_name=spec.json_schema_name,
    use_json_schema=use_json_schema,
    max_tokens=effective_max_tokens,
)
```

- Truncation retries (`:1007–1026`) are **unchanged**: they lower `attempt_target` (0.7× per retry) and re-build the request with the same `effective_max_tokens` from the lowered `pass_target` (the loop calls `generate_pass(..., pass_target=attempt_target)` — recompute inside `generate_pass` from the passed `pass_target`, not a captured initial value).
- `stage_reasoning_effort(stage_config)` returns `None`/`"none"` for legacy plain/`_notthinking` stages → cap = `target×1.6×1.4 + 200` (no inflation), which is *tighter* than today's 8192 for every real target — exactly the intended effect for non-reasoning profiles.
- Mock path (`client=None`): the request is still built (harmless override) and `invoke_generation` ignores it — unchanged behavior.

### 4.2 Fix B — numeric, delta-quantified repair prompts

**`candidate_spec.py::render_repair_user`** (`:549`) — new optional kwarg:

```python
def render_repair_user(spec, *, source_md, current_summary_md, target_words,
                       direction, book_title="", chapter_title="",
                       current_word_count: Optional[int] = None) -> str:
```

When `current_word_count is not None`, append a numeric block **after** the target/range line and **before** the policy, exactly one of:

- `direction == "less"`: `f"Current summary is {current_word_count} visible words; target is {target_words} (range {low}-{high}). Cut approximately {max(0, current_word_count - high)} words so the final markdown lands inside [{low}, {high}]. Count the visible words of your output before finishing and adjust if needed."`
- `direction == "more"`: `f"Current summary is {current_word_count} visible words; target is {target_words} (range {low}-{high}). Add approximately {max(0, low - current_word_count)} words so the final markdown lands inside [{low}, {high}]. Count the visible words of your output before finishing and adjust if needed."`

When `current_word_count is None` (in-flight resumed stages, any legacy caller), the block is omitted and output is byte-identical to today.

**`core/run_candidate.py::render_composer_repair_user`** (`:769`) — same signature addition (`current_word_count: Optional[int] = None`) and the same delta block, inserted after the "Target visible words…" block (`:794`).

**Callers** (`run_length_controlled_stage`, `:1046–1067`): pass `current_word_count=words` in both branches (chapter and composer), where `words = visible_word_count(summary_md)` is already computed at `:1037`. The `regenerate_from_source` branch (`:1041–1043`) is untouched (it re-uses `initial_user_prompt`).

### 4.3 Tests — new `tests/test_length_control.py` (offline, no network)

1. **`visible_max_tokens` table** (assert exact expected ints; values are `ceil(ceil(target × headroom) × TOKENS_PER_WORD + JSON_OVERHEAD_TOKENS) / (1 − effort_fraction)`, i.e. the §4.1 formula):
   - `visible_max_tokens(353, effort="minimal", cap=9103) == 1102` (and `< 9103`).
   - `visible_max_tokens(353, effort="medium", cap=16384) == 1982` (and `< 16384`).
   - `visible_max_tokens(1288, effort="minimal", cap=9103) == 3429`.
   - `visible_max_tokens(1288, effort="medium", cap=16384) == 6171` (≤ cap; inflated vs `effort="none"` == 991).
   - Monotonicity: `visible_max_tokens(t+1) >= visible_max_tokens(t)` for t in 100..2000.
   - Cap/floor: `visible_max_tokens(10_000, cap=16384) == 16384`; `visible_max_tokens(10, effort="none") == 512` (floor); `visible_max_tokens(10_000_000, cap=16384) == 16384`.
   - `effort=None` and `effort="none"` produce identical values (no inflation).
2. **`build_openrouter_request` override:** `max_tokens` param replaces `stage.max_tokens`; omitted param keeps `stage.max_tokens` (byte-identical body otherwise).
3. **Repair prompts carry the delta:**
   - `render_repair_user(..., direction="less", current_word_count=2267)` with `target_words=1288` contains `"Current summary is 2267"` and `"Cut approximately 979"` (2267 − 1288×(1+0.08)=1391 → 2267−1391=876; assert the computed literal, not a hardcoded guess) and `"[1149, 1391]"`.
   - `direction="more"` variant contains `"Add approximately"`.
   - `current_word_count=None` → the delta block is absent (output identical to the current implementation).
   - `render_composer_repair_user` behaves identically.
4. **Integration (stub candidate module + FakeCloudFunctionClient, pattern from `tests/test_chapter_parallel.py`):** a stub `build_openrouter_request` records kwargs; run one stage with a mock client; assert every generated request carried `max_tokens == visible_max_tokens(pass_target, effort=stage_reasoning_effort(stage), cap=stage.max_tokens)` for each pass target, and that repair requests were built with `current_word_count` set.

`Makefile` `test:` target gains `$(PYTHON) tests/test_length_control.py`.

## 5. Edge Cases

- **Reasoning models (effort > none):** the cap is inflated by `1/(1−fraction)`; at high effort it may not bind typical overshoot — by design (starving reasoning corrupts output). Fix B carries convergence. No special-casing.
- **Tiny targets (< ~100 words):** `DEFAULT_MIN_OUTPUT_TOKENS` floor (512) keeps the request viable; gate's 100-word floor means a 300-word summary of a 60-word target is not hard-failed anyway.
- **Truncated output under the new cap:** existing retry path reissues at 0.7× `pass_target` with the "at most N visible words" instruction; `max_truncation_retries=1` unchanged. A cap derived from `pass_target` (not the original `target_words`) means retries re-derive a tighter cap automatically.
- **Resume mid-stage:** checkpoint schema unchanged (`stage_state` has no token fields). On resume the cap is recomputed from the resumed `pass_target`; repair prompts for already-started stages continue with `current_word_count=None` until the next pass, then carry the delta. No migration.
- **Legacy profiles / in-flight runs:** profiles with `extra_body.thinking` resolve `stage_reasoning_effort` → `None`/`"none"`; the tighter un-inflated cap applies (correct). In-flight runs pick up the new behavior only for passes generated after the code change; no artifact incompatibility (prompt text is not parsed downstream).
- **JSON schema overhead:** 200 tokens is a heuristic; providers that meter schema tokens differently are absorbed by the truncation retry path. No per-provider logic.
- **Composer stage:** shares `generate_pass` and the repair-prompt change; book benches get the same benefit. `retrieved_source_excerpts` block position in the composer prompt is unchanged.
- **Deterministic vs judged runs:** identical generation path; the gate decision is orthogonal (see `specs/deterministic-vs-judged-scoring-gates.md`).

## 6. Acceptance Criteria

- [ ] `tests/test_length_control.py` passes offline; `make test` green (all four suites).
- [ ] `visible_max_tokens` matches the §4.1 table exactly; monotonic; capped; floored.
- [ ] `build_openrouter_request(max_tokens=...)` override works; default path byte-identical.
- [ ] Repair prompts (chapter + composer) contain the measured count and the exact cut/add delta when `current_word_count` is provided; absent otherwise.
- [ ] Re-run of the four luna runs (`det/jud × medium/minimal` on `chapter_fast-v3`) yields `hard_fail_rate = 0.0` (all 8 samples pass) with mean `passes_used ≤ ~3` and no truncation-retry spike (`truncation_retries` ≈ 0–2 total across the run).
- [ ] First-pass overshoot on luna drops below ~40% (medium) / ~30% (minimal) without the cap; with the cap, first-pass is bounded by the effective cap's visible-word ceiling.
- [ ] Next `chapter_fast-v3` run of any verbose profile (nemotron, mimo) shows first-pass overshoot capped (no 97–965% cases).

## 7. Verification Plan (post-implementation)

1. Run `make test` (existing three suites + new `test_length_control.py`).
2. Re-run the four luna runs exactly as the baseline (`--bench chapter_fast-v3 --profile 30m_gpt-5.6-luna_effort-medium|minimal [--judge-model openai/gpt-5.4-mini] --chapter-concurrency 4`).
3. Compare against the baseline artifacts:
   - `hard_fail_rate` 0.5 → 0.0; failing samples (eifc:001/006/009, gtd:013, dht:003) now pass.
   - `mean_passes_used` drops (convergence in 2–3 passes instead of 5).
   - `mean_uncached_cost` does not increase (fewer passes ≳ token cap).
4. Grep run logs for `truncation_retry` events; expect ≈0–2 per run.
5. If a straggler remains at 11–15% on effort-medium, log it as a documented follow-up (per-effort headroom or `max_passes`), keeping the gate at `0.10`.

## 8. Out of Scope

- Gate/threshold changes (keep `max_final_length_error_pct=0.10`, 100-word floor).
- `max_passes` / `max_truncation_retries` changes.
- Per-profile or per-model overrides of the cap/headroom.
- Changing `scaled_max_tokens` or `tools/add_candidate.py` profile generation.
- Changes to `core/cf_client.py`, `cloud_function/`, `dashboard/`, or `scoring.py`.
- Re-scoring or re-running historical artifacts.
- Any change to best-pass selection (already shipped per `specs/run-length-best-pass-selection.md`).