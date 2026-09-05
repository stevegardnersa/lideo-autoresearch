Feature: In-dialog run progress for "Run with options"

Spec: specs/dashboard-dialog-run-progress.md

WHAT
Jobs launched from the profile dialog's "Run with options…" action currently show only a static notice ("watch it stream in Run data") — dialog stays open, zero inline feedback. Add a live progress pane inside the dialog: status badge + elapsed timer, live stdout/stderr log tail (preferred), indeterminate spinner as the floor, terminal summary (Run ID / error / canceled), and View log / Open in Run data / Re-run / Close actions. Zero backend change — existing SSE stream already emits start/log/status events.

CONTEXT FOUND (read-only, no design_ref assets exist)
- Flow: prefill action → openRunProfilePrefill mounts run_candidate card into #dlgRunWidget → runToolSubmit POSTs /api/jobs → refreshJobs + expandJob + attachStreamIfPossible → static dialogNotice.
- SSE upstream: vite.config.js job manager emits start / log (batched ~20ms) / status {status, exitCode, error?, resultHints?} + ping keepalive; job._subs is a Set → multiple concurrent subscribers per job OK; Last-Event-ID replay on connect.
- Collision pitfall: RUN_STATE.es keyed by job id; attachStreamIfPossible skips if already keyed. Dialog pane must use dialog-scoped key (RUN_STATE.dlgEs) or row stream + dialog stream suppress each other.
- Cleanup: clearInlineRunWidget() (on dlgRunClose / openAddDialog / openEditDialog / closeDialog) wipes #dlgRunWidget — must also detach dialog stream. Job continues server-side regardless.
- updateToolGuards already disables dialog submit while same toolId queued/running; adds in-flight guard needed for POST window.
- Test harness stubs window.EventSource (settings.ui.test.mjs:160) recording to state.es — extend to capture listeners for fake log/status emission; existing dialog-submit test (line 848) needs pane assertions + notice copy update.

KEY SPEC DECISIONS
1. Pane states: pending-launch → queued (with queue position from RUN_STATE.jobs) → running (1s elapsed timer, autoscroll console w/ 400-line cap, stderr .con-err) → succeeded (Run ID, Open in explorer when runId hint) | failed (job.error or last 5 stderr lines) | canceled | interrupted; transient "reconnecting" on SSE error (EventSource auto-reconnects; detach only when last known status terminal).
2. Duplicate POST (200 duplicate:true) → pane in queued state on existing job id, "Already queued — identical job". 409 → error notice + "Open in Run data" link, no pane. failedFast → terminal pane directly, no EventSource.
3. Re-run resets pane for new job id (POST same args, clear console, restart timer, new stream).
4. Accessibility: status line in role="status" aria-live=polite (announce transitions once, never whole-pane re-render); log pre role="log"; timer aria-hidden; spinner aria-hidden; autoscroll honors prefers-reduced-motion; focus moves to first action button on terminal.
5. Performance: 1 EventSource per dialog run, closed on terminal/teardown; 400-line cap vs 2000 in Run data; per-log-event pre re-render acceptable at batched server cadence; no new polling.
6. Non-goals: no server/progress-event changes, no structured phase extraction from run_candidate.py, Run data tab untouched, other menu actions (run/judge/agent) unchanged.

TESTS REQUIRED
11 cases: pane renders running; log append incl. stderr wrapper; terminal detach + summary; failed error text + log link; failedFast no-ES; duplicate queued pane; two EventSources coexist; dlgRunClose closes stream; queue position line; in-flight submit guard; update existing submit test.