# Chapter length + readability tuning (luna-30m, chapter_fast-v3)

**Status:** experiment complete; candidate shipped to **local** slot (`chapter-p14`). Production still on `chapter-p3`.

## Problem

The shipped readable prompt (`chapter-p3`: `plain_readable` + `sentence_split_hard` + 13 words/sentence)
keeps FK under 10 but **overshoots the word target badly** — roughly 2x the dense baseline.

## Method

Offline A/B on `bench/chapter_fast-v3.jsonl` (8 chapters, 30m luna, thinking off), varying only
chapter prompt components. Metrics: mean absolute length error vs `target_words`, count within the
10% hard length tolerance, and Flesch-Kincaid grade (deterministic `scoring.readability_metrics`).

New components added to make the levers testable (all in `candidate_spec.py`):

| Component | Meaning |
|---|---|
| `target_sentence_words` | Max/average sentence length (FK lever) and fallback words-per-sentence |
| `sentence_target_words` | Words-per-sentence used only to derive the sentence budget S |
| `sentence_budget_scale` | Multiplier on S (e.g. `0.9`) |
| `length_aim` | First-pass aim: `low` (low edge) or a fraction like `0.90` (aim at 0.90×target) |
| `length_policy=sentence_primary` | New policy: sentence count is the hard constraint |

## Results

| version | components | meanErr | within hard | mean FK | max FK |
|---|---|---|---|---|---|
| chapter-p2 | dense, `explicit_budget_first` | 13.1% | 5/8 | 14.4 | 17.1 |
| **chapter-p3 (shipped)** | readable, `sentence_split_hard`, 13w/s | **19.4% / 21.8%** | **2/8, 2/8** | 8.8–9.0 | 10.1 |
| chapter-p4 | `sentence_primary`, 11w/s | 12.9% | 4/8 | (dense) | — |
| chapter-p5 | `sentence_primary`, 11w/s, ×0.9 | 14.0% | 4/8 | (dense) | — |
| chapter-p6 | dense + `length_aim=low` | 9.3% | 5/8 | 14.8 | 17.7 |
| chapter-p9 | readable, `explicit_budget_first`, 13w/s | 15.6% | 2/8 | 9.23 | 10.3 |
| chapter-p10 | = p9 + `length_aim=0.90` | 7.2% | 7/8 | 9.28 | 10.6 |
| chapter-p11 | = p9 + `length_aim=low` | 12.9% | 6/8 | 9.19 | 10.2 |
| **chapter-p12** | **= p10 + 12w/s** | **8.1 / 10.2 / 11.3%** | **7/8, 6/8, 4/8** | **9.1–9.3** | 9.8–10.3 |
| chapter-p13 | = p10 + 11w/s | 10.7% | 5/8 | 8.98 | 9.9 |

p3 was re-run on the same bench to confirm the overshoot is stable (19.4% and 21.8% on two runs),
so the p3 → p12 improvement is real, not a single-run artifact. p12 was run three times
(8.1 / 10.2 / 11.3%).

## Findings

1. **`sentence_split_hard` (p3) is the overshoot driver.** The model writes close to S sentences but
   with more words per sentence than the target, so S × words-per-sentence exceeds the target.
   Switching to `explicit_budget_first` (word-first) cut error from ~20% to ~15% with no other change (p9).
2. **Aiming the first pass below target is the biggest single lever.** `length_aim=0.90` on top of p9
   took error from 15.6% to 7.2% and hard-pass rate from 2/8 to 7/8 (p10).
3. **`sentence_primary` (S as the hard constraint) does not work** with this model — it overshoots
   because sentence length is not actually capped (p4/p5). Refuted.
4. **`length_aim=low` (low edge, target−5%) is weaker than `0.90`** (p11 vs p10).
5. **A 12-word sentence cap keeps FK < 10** while retaining the length gain (p12); 11 words is tighter
   than needed and starts costing length accuracy (p13).

## Recommended change

Chapter version `chapter-p14` = `plain_readable` + `explicit_budget_first` + `target_sentence_words=12`
+ `length_aim=0.90` (behaviorally identical to `chapter-p12`; assigned to the local slot).

- Length error ~10% vs ~20% for the shipped p3; hard-tolerance pass rate ~2–3x.
- FK mean ~9.2, at or just below 10 (occasional 10.0–10.3 outliers).

If strict FK < 10 on every chapter is required, fall back to `target_sentence_words=11` (p13): FK max
9.9, at the cost of ~1–2% length error.

## Caveats

- n=8 chapters; single model/profile (luna 30m, no thinking). Re-validate on a larger bench before
  production promotion.
- `plain_readable`'s system text still says "14 to 16 words per sentence" while the budget cap is 12;
  the measured FK used that exact text, so it was left unchanged.

## Reproduce

```
python3 core/run_candidate.py --bench bench/chapter_fast-v3.jsonl \
  --profile 30m_gpt-5.6-luna_notthinking --prompt-version chapter-p3  --prompt-stage chapter
python3 core/run_candidate.py --bench bench/chapter_fast-v3.jsonl \
  --profile 30m_gpt-5.6-luna_notthinking --prompt-version chapter-p14 --prompt-stage chapter
```
