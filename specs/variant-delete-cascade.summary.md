Feature: reasoning variant delete = full destructive cascade (run files + logs + results rows) behind an explicit confirmation gate.

Spec: specs/variant-delete-cascade.md

WHAT
Removing a reasoning variant (uncheck row in model Edit dialog + Save, or `--remove-profile`) currently deletes ONLY the candidates.json profile — silently, no file cleanup. Whole-model card delete already removes run files (regex) but leaves job logs and results.tsv rows behind, corrupting the leaderboard (FileNotFoundError at leaderboard.py:121). Fix: variant deletion cascades to all run artifacts, referencing job logs, and results.tsv rows — and always asks for confirmation first.

CONTEXT FOUND (verified in code)
- Variant key grammar (candidates.json): `<tb>_<slug>_thinking|notthinking|effort-<name>`; run id embeds key verbatim as `__<key>_v<N>` (+ `__llmj_<judge>` duplicates) — runs/booksum-v4/ filenames confirm.
- Front-end: collectEditPayload (settings.js:465) marks keep:false; save PUT /api/models → applyProfileEdit (vite.config.js:478) deletes profile + regenerates spec. No confirm, no file deletion.
- Model delete: handleDelete (settings.js:1365) 2-click arm → DELETE /api/models → add_candidate.py --remove regex → remove_candidates (add_candidate.py:596) deletes profiles + matching run files only. No logs, no results.tsv.
- Job logs artifacts/jobs/<uuid>.log: meta line has NO args (verified restoreHistory); run id appears only as stdout token `Run ID: <run_id>` (deriveResultHints greps it) → content-match rule for logs.
- results.tsv has run_id + run_artifact columns; leaderboard hard-fails when run_artifact missing.
- Confirmation precedent: reset_benchmark uses confirmPhrase 'RESET'; variant delete gets dedicated modal (stronger than 4s armed button).

KEY SPEC DECISIONS
1. Confirmation gate: PUT without confirm + keep:false edits → 409 {code:confirmation_required, impact:[{key,runFiles,logs,resultRows,activeJobs}]}, NOTHING mutated (preflight via add_candidate.py --remove-profile --dry-run). Confirm re-sends with confirm:true.
2. Matching rule: fixed-string `__<profile_key>_v` for run files (exact, slug dots safe, _v<N> covers re-runs/resumes, __llmj_* caught); run-id set collected BEFORE unlink; logs matched by `Run ID: <id>` per line; results.tsv rewritten excluding matching run_id rows (atomic temp+rename).
3. No partial damage: applyProfileEdit error → 400, cascade never runs. results.tsv rewrite failure → 500, profile left in place.
4. Active-job safety: running/queued run_candidate/judge_existing/agent with args.profile==key → variant refused (activeJobs>0, code active_jobs), skipped + reported in plan.skippedActive; other variants still deletable.
5. Whole-model DELETE extended for consistency: same log + results.tsv cascade (shares utility), 2-click confirm unchanged, response gains removedLogs/removedResultRows.
6. CLI: new --remove-profile <key> (exact, non-regex) + --dry-run preflight; existing --remove gains log+results cascade.
7. Out of scope: chapter_runs/*.csv (sample_id-keyed, unattributable), snapshots, bench files. plan.removed/renamed/updated/created shapes preserved (additive only) — no regression on existing PUT tests.
8. UI/a11y: modal role=dialog aria-modal, Escape=cancel, focus trap, per-variant impact table, "Delete permanently" vs Cancel, cancel restores unchecked rows, active variants marked ⛔ non-deletable.
9. Tests: offline fixtures; cover gate, 6-file+log+rows cascade, boundary (effort-minimal vs effort-minimal-x, thinking vs notthinking), active-job skip, zero-impact, applyProfileEdit error no-unlink, CLI dry-run, results.tsv atomicity, existing-PUT regression.

Files written:
- specs/variant-delete-cascade.md (main spec)
- specs/variant-delete-cascade.summary.md (this summary)

Not implemented: no code written. Contracts additive to existing API shapes; front-end fetch wiring and settings.ui.test.mjs expectations change per spec sections 4.6/10.