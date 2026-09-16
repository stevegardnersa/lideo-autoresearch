# Summary: Base Prompts + Model-Strategy Tuning

## WHAT
Establish three curated **base chapter prompts** as movable parts for model-strategy tuning, align the
shared readable style text with the sentence cap, add a dashboard-triggered sweep that ranks the bases
per model, and make both tuning paths base-aware.

Bases (all `scope=general`, labelled `base: <slug>`):

| slug | id | components |
|---|---|---|
| `sentence` | `chapter-p15` (new) | `sentence_primary`, `plain_readable`, `sentence_target_words=11`, `target_sentence_words=13` |
| `readable` | `chapter-p16` (re-derived from `p3`) | `sentence_split_hard`, `plain_readable`, 13 w/s |
| `readable-low` | `chapter-p17` (re-derived from `p14`) | `explicit_budget_first`, `plain_readable`, 12 w/s, `length_aim=0.90` |

Slots: `local` → `chapter-p15`; `production` → `chapter-p16`. `p3`/`p14`/`p4`–`p13` are relabelled
`experiment:` and hidden by default (no deletions).

## WHY
1. `plain_readable` hardcodes "14 to 16 words per sentence", which now contradicts the per-version
   sentence cap (`target_sentence_words` ∈ {12,13}) used to hold FK < 10.
2. There is no curated set of comparable base prompts, and no per-model way to pick the best one.
3. Tuning (manual and auto) has no notion of a starting base.

## KEY HAZARD THIS SPEC FIXES
`p3`/`p14` and every `plain_readable` version have **no `system_template`**, so they re-compose from
`CHAPTER_SYSTEM_STYLES["plain_readable"]` at render time. Editing that shared text changes the
rendered prompt of **all** readable versions at once and leaves their stored `template` /
`system_prompt_sha256` **stale** (`registry_sha` is unaffected — it hashes registry JSON only). So the
bases are **re-derived copy-on-write** (§4.2) under the aligned text, and a drift-guard test asserts
each re-derived base's stored sha equals a fresh render.

## DECISION OF NOTE
`production` moves from `p3` to the re-derived `chapter-p16`. This supersedes the earlier "hold
production on p3" choice, which would leave production serving a stale-fingerprint version. Flagged as
an explicit review point.

## SCOPE
- Sweep reuses `specs/per-model-prompt-tuning.md` §5 metrics, eligibility floors, and ranking verbatim.
- Dashboard: `SCRIPT_REGISTRY` + a **Compare base prompts** button in the Prompts tab, dispatched via
  `POST /api/jobs`.
- Tuning: manual "Fork base" uses `POST /api/prompts` (from `prompts-ui-model-scoped-editing.md`);
  auto adds `--base <slug>` to `autoresearch/tune_prompt.py` / `agent.run_hill_climb`, seeding from the
  base and recording `base_prompt`.
- No new metrics, no schema changes, no auto production promotion (`--adopt` opt-in only), no deletions.

## ARTIFACTS
`artifacts/autoresearch/base_sweep/<profile>_<bench>.json` + `.md` — ranked rows, eligibility, winner.

## TESTS
`tests/test_base_prompts.py` (new: resolution, ranking, eligibility, label edge cases);
`tests/test_sentence_budget.py` (extend: sentence base render); drift guard; mocked end-to-end sweep.

## STATUS
Draft for implementation. Spec: `specs/base-prompts-and-model-strategy-tuning.md`.
Depends on: `specs/per-model-prompt-tuning.md`, `specs/prompts-ui-model-scoped-editing.md`.
