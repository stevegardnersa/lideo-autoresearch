# Spec: Reasoning-Variant Deletion Cascade (run files + logs) with Confirmation

**Feature:** Make deleting a reasoning variant destructive and complete: remove the variant profile from `data/candidates.json`, delete **all** of its run artifacts (every extension, judge duplicates included) across every `runs/<bench>/` directory, delete the dashboard job logs that reference the deleted runs, and remove the corresponding `results.tsv` rows — and **require an explicit confirmation step** before any of it happens.

**Input needed:** None — complete behavior contract, verified against current code.

**Status:** Draft for implementation

---

## 1. Goals

1. **Variant delete = full cascade.** Removing a reasoning variant (unchecking a profile row in the model Edit dialog and saving, or `tools/add_candidate.py --remove-profile <key>`) permanently deletes:
   - the profile entry in `data/candidates.json`,
   - every run artifact of that variant across all `runs/<bench>/` directories (`.json`, `.samples.jsonl`, `.state.json`, and `__llmj_<judge>` duplicates),
   - every dashboard job log in `artifacts/jobs/*.log` that references any of the deleted run ids,
   - every `results.tsv` row whose `run_id` is among the deleted run ids.
2. **Confirmation is mandatory.** No run file, log, or result row is deleted without an explicit user confirmation. Zero-impact variants still confirm (profile removal is itself destructive), with lighter copy.
3. **No orphaned data.** After a variant delete, no dashboard surface (leaderboard, run explorer, Run data panel, `results.tsv` consumers) references a deleted run artifact.
4. **Safe against active jobs.** A variant with a running or queued `run_candidate` / `judge_existing` / `agent` job cannot be deleted; the deletion is refused with a clear conflict response.

## 2. Non-Goals

- No change to *creation* or *editing* of variants — only the removal path.
- No change to the whole-model card delete flow's trigger UX (its existing 2-click "Confirm delete?" arm remains); this spec *extends its backend cascade* (logs + results.tsv) so it uses the same shared cleanup utility.
- No cleanup of `runs/<bench>/chapter_runs/*.csv`: those files are keyed by `sample_id` (not run id) and shared across runs of a bench; they cannot be attributed to a variant by filename and are explicitly out of scope.
- No change to how `keep: false` payloads are shaped by `collectEditPayload` (front-end payload format stays as-is; only the save flow and the server handler change).
- No change to snapshot files (`snapshots/catalog`, `snapshots/pricing`), `bench/*.jsonl`, rubrics, or `candidate_spec.py` regeneration behavior (it still regenerates after profile changes, exactly as today).
- No change to the "Remove variant" menu item availability (locked canonical rows still use it; unlocked rows still uncheck inline).

## 3. Context: Existing System (verified)

- Variant keys in `data/candidates.json`: `"<time_budget>_<model-slug>_thinking"`, `"<time_budget>_<model-slug>_notthinking"`, `"<time_budget>_<model-slug>_effort-<name>"` (e.g. `30m_deepseek-v4-flash_effort-minimal`). Keys are unique; `name` field is `<key>_v<digits>`.
- Run id grammar (from `runs/booksum-v4/` and `core/run_candidate.py`): `<ts>__<bench>__<model-full-with-slash-as-__>__<profile_key>_v<digits>` and the same stem with `__llmj_<judge>` appended for LLM-judged duplicates. The profile key appears **verbatim** in the stem, always preceded by `__` and followed by `_v`.
- Run artifacts per run (names as on disk):
  - `<run_id>.json` (manifest),
  - `<run_id>.samples.jsonl`,
  - `<run_id>.state.json` (resume state),
  - `<run_id>__llmj_<judge>.json` / `.samples.jsonl` / `.state.json` (re-judge pass writes these, per `core/judge_existing.py` outputs).
  A `--resume` re-run writes to the same run id stem (`_v<N>` numbering differs), so the prefix match `__<profile_key>_v` covers both initial and resumed runs.
- Variant removal today — front-end: `collectEditPayload` (dashboard/settings.js ~465) marks a row `keep: false` when its checkbox is unchecked; on "Save changes" the dialog PUTs to `/api/models` with `edits`. Server: `applyProfileEdit` (dashboard/vite.config.js ~478) deletes the profile key from `data.candidates.json` and regenerates `candidate_spec.py`. **No run files, no logs, no results.tsv rows are touched, and there is no confirmation.**
- Whole-model delete today: `handleDelete` (settings.js ~1365) 2-click arms "Confirm delete?" then `DELETE /api/models` → `tools/add_candidate.py --remove <slug-regex>` → `remove_candidates` (tools/add_candidate.py ~596) deletes profiles matching the slug regex **plus** every file in `runs/<bench>/` whose name matches the regex (any extension). **Does not delete `artifacts/jobs/*.log` and does not touch `results.tsv`.**
- Job logs: `artifacts/jobs/<uuid>.log`. Each log contains `[job] meta:` lines (jobId/toolId/createdAt — **args are not persisted there**) and streamed stdout. `core/run_candidate.py` prints `Run ID: <run_id>` on stdout (that token is what `deriveResultHints` greps for in vite.config.js), so the run id string always appears inside the job log of the run that produced it.
- `results.tsv` (repo root) has a `run_id` column and a `run_artifact` column pointing at the run manifest. `tools/leaderboard.py` raises `FileNotFoundError` when a row's `run_artifact` path is missing (verified at leaderboard.py ~121-123). Deleting run files without removing rows corrupts the leaderboard — this is the orphan-failure the cascade must prevent.
- Destructive-confirmation precedent: `reset_benchmark` job requires `confirmPhrase: 'RESET'` (vite.config.js); the model-card delete uses the armed-button pattern. The variant delete gets a dedicated confirmation dialog (Section 7) because it is file-destructive.

## 4. Required Behavior

### 4.1 Confirmation gate (front-end)

- When the user clicks **Save changes** in the model Edit dialog and `collectEditPayload` produced any `keep: false` edit:
  1. The dialog does **not** PUT immediately.
  2. The UI opens a confirmation dialog (Section 7) listing every variant being removed with its impact (run file count, log count, results-row count — computed by the server preflight, see 4.3).
  3. Only after the user confirms does the UI re-send the PUT with `confirm: true`.
  4. If the user cancels, nothing is deleted and the edit dialog stays open with the rows restored to their previous state (uncheck is rolled back).
- The `Save changes` button must remain disabled while the preflight is in flight; a failure of the preflight (network error, server error) surfaces as an error notice and does **not** proceed to a deletion.

### 4.2 No silent deletion anywhere

- The variant-removal path is the only path that deletes run artifacts. It must always go through the confirmation gate of 4.1.
- The whole-model card delete keeps its existing armed 2-click confirmation (already present); its backend cascade is extended per 4.5 so logs and results.tsv rows are cleaned with the same utility. No other code path deletes run files.

### 4.3 Server preflight (impact computation)

- `PUT /api/models` behavior when the payload contains any `keep: false` edit and `confirm` is absent/false:
  - **No mutation occurs.** The profiles are not removed, no file is deleted.
  - Response `409 Conflict`, body:
    ```json
    {
      "ok": false,
      "code": "confirmation_required",
      "impact": [
        {
          "key": "30m_deepseek-v4-flash_effort-minimal",
          "runFiles": 6,
          "logs": 2,
          "resultRows": 4,
          "activeJobs": 0
        }
      ]
    }
    ```
  - `runFiles` counts every artifact matched by 4.4's file rule. `logs` counts job logs matched by 4.4's log rule. `resultRows` counts `results.tsv` rows for matched run ids. `activeJobs` counts running/queued `run_candidate` / `judge_existing` / `agent` jobs whose `args.profile` (or `args.candidate` for the agent job) equals the variant key.
  - If `activeJobs > 0` for any key, the response additionally carries `"code": "active_jobs"` (still HTTP 409) and the UI must refuse to delete that variant until the job is canceled/cleared; other variants in the same save may still be confirmed and deleted (the confirmed PUT then applies only the deletable ones — the server drops the blocked keys from the edit set and reports them in `plan.skippedActive`).
  - The impact list only includes keys that exist as profiles (`applyProfileEdit` validation of `edits` still runs first, and its errors still take precedence).

### 4.4 Cascade matching rules (shared utility)

A single function (implemented in `tools/add_candidate.py`, used by both the PUT handler and the existing `--remove` path) removes the artifacts of one variant key:

1. **Run files.** For every directory under `runs/`, for every file whose name contains the fixed string `__<profile_key>_v` (the key is matched as a literal string, **not** as a regex — model slugs contain `.`), delete the file. This covers:
   - initial runs `<run_id>.json|.samples.jsonl|.state.json`,
   - all version-numbered re-runs (`_v1`, `_v2`, …) since `_v<digits>` follows the key,
   - all LLM-judged duplicates (`__llmj_<judge>` variants of the same stem),
   - resumed runs that share the stem.
   The `__` / `_v` boundary prevents cross-variant false positives (e.g. deleting `_thinking` files when removing the `_notthinking` variant, or `effort-minimal` matching `effort-minimal-x` — the latter cannot match `_v` immediately after the key).
2. **Collect deleted run ids.** The run id of a deleted file is its stem up to `_v<digits>` (i.e. the stem with the `__llmj_*` judge part, the `_v<digits>` part, and the extension removed). Collect the set across all deleted files.
3. **Job logs.** For every `*.log` in `artifacts/jobs/`, scan its lines for `Run ID: <run_id>` occurrences (`run_candidate` prints this token; verify per-line against the deleted-run-id set). Delete the log if any line's run id is in the set. Optionally short-circuit with the same regex used by `deriveResultHints`. Logs of running/queued jobs are handled per 4.3's `activeJobs` gate — never delete a log whose job is still active (the server refuses the variant first).
4. **results.tsv rows.** Rewrite `results.tsv` (same header, same order, tab-separated) excluding every row whose `run_id` is in the deleted-run-id set. If the resulting file is empty except the header, keep the header (schema stability); if the file does not exist, skip.
5. **Profile entry.** Delete the key from `candidates.json` profiles and regenerate `candidate_spec.py` — unchanged from today's `applyProfileEdit` / `remove_candidates` behavior.

Exact matching (fixed string for the key, `__`/`_v` boundaries for run files, run-id set membership for logs and results rows) is the contract. No glob approximation that could match a neighboring variant is acceptable.

### 4.5 Backend endpoints

- **`PUT /api/models`** (extended):
  - Request body: existing `{ old_model, edits, ... }` plus optional `confirm: boolean`.
  - If `confirm !== true` and any `keep: false` edit: preflight path (4.3), 409, nothing mutated.
  - If `confirm === true` (or no `keep: false` edits): existing behavior, plus:
    - for each `keep: false` key, run the cascade (4.4) **after** `applyProfileEdit` succeeds;
    - if a key has `activeJobs > 0`, skip its cascade and drop it from the applied edit set; report it in `plan.skippedActive: ["<key>", ...]`;
    - the `plan` object gains `removedRuns`, `removedLogs`, `removedResultRows` (aggregate counts) alongside the existing `removed` (profile keys), `renamed`, `updated`, `created`;
    - 200 response shape stays additive — existing front-end readers of `plan.removed` keep working.
  - If `applyProfileEdit` errors, 400 with the existing error shape, and **no** cascade runs (files must never be deleted when the profile edit itself failed).
- **`DELETE /api/models`** (extended, whole-model):
  - Keep the existing `--remove <slug-regex>` profile + run-file deletion.
  - Additionally run steps 2-4 of 4.4 for the runs matched by the slug regex: collect deleted run ids from the regex-matched files, delete referencing job logs (except active), delete referencing `results.tsv` rows.
  - Response body gains `removedLogs` and `removedResultRows` next to the existing `removedProfiles` / `removedRuns`.
  - The armed 2-click confirmation already present is the confirmation step for this endpoint; it is unchanged.
- **CLI** `tools/add_candidate.py`:
  - New flag `--remove-profile <profile-key>` (exact key, not regex) that runs the full cascade for one key, prints a machine-parseable summary honoring the existing output conventions:
    ```
    Removing 1 profile(s):
      - 30m_deepseek-v4-flash_effort-minimal
    Found 6 run file(s):
      - runs/booksum-v4/...json
      ...
    Removed 6 run file(s)
    Removed 2 job log(s)
    Removed 4 results.tsv row(s)
    ```
  - Honor `--dry-run`: print the same summary prefixed `(dry-run — no changes written)`, delete nothing. The server uses `--dry-run` for the preflight impact computation.
  - The existing `--remove` flag gains the same log + results.tsv cascade (steps 2-4) while keeping its regex semantics.
  - Never delete a log of an active job (server-side gate in the PUT path; CLI prints `Skipped N active job log(s):` instead — active-job refusal is a server/UI concern, CLI is best-effort).

### 4.6 Front-end flow (model Edit dialog, settings.js)

- On save with removals: call `PUT /api/models` **without** `confirm`.
- Response `409 code === "confirmation_required"`: render confirmation dialog with the impact table (4.3 payload). Confirm button label reflects totals, e.g. `Delete 3 variants, 14 files, 4 logs, 9 results rows`. On confirm, re-send the same PUT with `confirm: true`. On success: close dialog, reload models, show a banner with the cascade totals (mirroring the existing `Cleared N finished runs` banner pattern).
- Response `409 code === "active_jobs"` (or per-key `activeJobs > 0` rows in impact): mark those rows non-deletable in the confirmation dialog ("cannot delete while a run job is active"), keep their checkboxes restored; the user may confirm the remaining variants only.
- Canceling the confirmation dialog: restore all unchecked rows (`vc-to-remove` cleared, checkboxes re-checked), dialog stays open, no network delete.
- Confirmation dialog itself must be non-trivial (not the 4-second armed button): explicit Cancel and Delete buttons, dialog modal with visible impact summary, focus managed, `Escape` = cancel, no auto-dismiss timer.

## 5. Data Model / Entity Relationships

- **Variant** (`data/candidates.json` → key): `"<tb>_<slug>_<style>"`; `name` = key + `_v<N>`.
- **Run artifact** (`runs/<bench>/<run_id>.<ext>`): run id embeds the variant key at a fixed offset (`__<key>_v`). One run id family = up to 6 files (3 core + 3 `__llmj_<judge>`); more with re-runs/resumes.
- **Job log** (`artifacts/jobs/<uuid>.log`): relates to runs only via content token `Run ID: <run_id>` (args are not in the meta line — verified).
- **results.tsv row**: `run_id` column keys 1:1 to a run id family.
- Deletion order matters: profile → run files (collect ids) → logs → results rows. Run-id collection must happen **before** any unlink so the log/results matching has the full id set.

## 6. Auth & Permissions

- No new auth surface. Same as existing dashboard endpoints: the Vite dev server plugin, local single-user tool. The cascade must not elevate anything — it only deletes files inside the repo-owned `runs/`, `artifacts/jobs/`, `results.tsv`, `data/candidates.json`, exactly like the current `--remove` path does.
- Path safety: the pattern-driven deletion only ever targets files inside `runs/<bench>/` subdirectories and `artifacts/jobs/*.log`; the run-id token extracted for log/tsv matching must be validated against `^[A-Za-z0-9_.\-]+$` before use as a search term (it is derived from filenames, but a corrupted filename must not inject a path).

## 7. Confirmation Dialog (UX + a11y)

- Opened above the edit dialog; overlay + dialog (consistent with `.cm-hidden` conventions).
- Content:
  - Title: `Delete reasoning variants permanently?`
  - Body: one line per variant (`<key>`), each with its impact (`6 run files · 2 logs · 4 results rows`), styled as a table/list; variants with `activeJobs > 0` shown with `⛔ cannot delete — job still running` and excluded from confirmation.
  - Irreversibility warning: run files, logs, and results rows cannot be recovered.
  - Buttons: `Cancel` (focus on open) and `Delete permanently` (danger styling).
- A11y: `role="dialog"` + `aria-modal="true"`; `aria-labelledby` on the title; focus moves into the dialog on open and returns to `Save changes` on cancel; `Escape` cancels; no focus escape into the page behind while open.
- Empty-impact copy: if a variant has 0 run files / 0 logs / 0 rows, its line reads `no run data — profile only`, but confirmation is still required and performed.
- Error paths: preflight network failure → error notice in the edit dialog, nothing deleted; confirm-time failure → error notice, removal row state restored, no partial-confirm retry without user re-confirming.

## 8. Edge Cases

1. **Multiple variants removed in one save** → one dialog, one impact table, one confirmed PUT.
2. **Mixed rename + remove in one save** → renames behave exactly as today; only `keep: false` keys cascade.
3. **Variant has never been run** → impact all zero; confirmation shows "profile only", deletion removes just the profile.
4. **Re-run versioning** (`_v2`… generated by `--resume` or rerun) → all match `__<key>_v`, all deleted.
5. **LLM-judged duplicates** (`__llmj_<judge>`) → share the stem match, deleted with the family.
6. **`effort-minimal` vs `effort-minimal-x`** (hypothetical) → `__<key>_v` boundary makes the match exact; `x` variant untouched.
7. **Slug containing `.`** (e.g. `gpt-5.4-mini`) → fixed-string match, never regex-interpreted.
8. **Active job on the variant** → refused pre-delete (`activeJobs`), UI blocks that variant; other variants still deletable.
9. **`applyProfileEdit` fails** → 400, cascade never runs.
10. **Concurrent write to results.tsv** → read-parse-rewrite must be atomic (write temp + rename); a failed rewrite aborts before unlinking run files is acceptable only if the whole cascade reports an error and the profile removal is rolled back — simpler contract: cascade order = compute ids → delete run files → delete logs → rewrite results.tsv → remove profile → regenerate spec; if the results.tsv rewrite fails, report 500 and leave profile in place (files are already gone; the profile then has no runs — acceptable, and the error tells the user to retry).
11. **results.tsv missing entirely** → skip step 4 silently (leaderboard shows nothing to corrupt).
12. **Log already pruned** (30-day prune) → nothing to delete; counts reflect the logs actually present.
13. **Old-format logs** (no `Run ID:` line, e.g. canceled-before-start) → not matched; harmless.
14. **Deleting the last variant of a model** → model card disappears after reload (same as today's profile removal); run/log/row cascade still completes.
15. **`chapter_runs/*.csv`** → intentionally untouched (Non-Goals), files remain but are not referenced by any surviving run id.

## 9. Performance

- Preflight and delete scan exactly the same files: `runs/<bench>/` (flat listing per bench) and `artifacts/jobs/*.log` (line scan for `Run ID: <id>`).
- Log scan is O(lines × deleted-run-ids); regex per line with a single alternation of the id set is fine (log cap is 20 MB).
- results.tsv rewrite is a single pass; rows are typically hundreds, not millions.
- No new LLM calls, no new network round trips beyond the one extra preflight PUT per save-with-removals (which is the confirmation gate itself).

## 10. Testing Requirements

1. **Confirmation gate (UI)**: unchecking a variant with N run files then Save → first PUT has no `confirm`, asserts `409 code confirmation_required`; dialog shows correct counts; Cancel restores rows and performs no second PUT; Confirm sends `confirm: true` and a success banner shows totals.
2. **Cascade completeness (server)**: fixture with one variant run (all 6 artifacts: 3 core + 3 `__llmj_*`), a job log containing `Run ID: <id>`, and 2 results.tsv rows for that id → confirmed PUT removes profile, all 6 files, the log, both rows; a non-matching neighboring variant (e.g. same model, `_thinking` vs `_notthinking`) untouched.
3. **Boundary matching**: `effort-minimal` removal does not touch `effort-minimal-x` (constructed fixture); `_v2` re-run and `__llmj_` duplicates are caught by the `__<key>_v` rule.
4. **Active-job refusal**: queued `run_candidate` job with `args.profile == key` → preflight reports `activeJobs: 1`, confirmed PUT skips that key and reports `plan.skippedActive`.
5. **Zero-impact variant**: no runs/logs/rows → confirmation still shown, "profile only" copy, cascade deletes just the profile.
6. **applyProfileEdit early error** → 400, assert no file unlinked (fixture snapshot of `runs/` before/after).
7. **CLI**: `--remove-profile <key> --dry-run` prints counts and deletes nothing; same without `--dry-run` deletes per counts; `--remove <regex>` now also prints `Removed N job log(s)` and `Removed N results.tsv row(s)`.
8. **results.tsv atomicity**: row-rewrite leaves header intact on empty result; concurrent-append fixture (row added between read and write) resolved by temp+rename.
9. **Regression**: existing PUT edit tests (rename/temperature/route) assert `plan` still contains `removed/renamed/updated/created` and that no cascade keys appear when no `keep: false` edits exist.
10. **Manual dashboard check**: delete a variant with runs from the Edit dialog — confirm modal appears, counts match `ls runs/<bench>/ | grep <key>` and `grep -l "Run ID:" artifacts/jobs/*.log`, files actually disappear, leaderboard still renders (no FileNotFoundError).

All server-side tests must run offline (no LLM calls — the cascade never touches models; fixtures are fabricated files).