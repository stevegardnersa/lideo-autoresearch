# Spec: In-Dialog Run Progress for "Run with options"

**Feature:** When a job is launched from the profile dialog's "Run with options…" action, show live progress inside the dialog instead of a static "watch it stream in Run data" notice. Preferred indicator: live logs/status updates. Minimum acceptable: an animated progress indicator.

**Input needed:** None — complete behavior contract (no `design_ref/` assets exist; visual styling is left to the implementer using existing dashboard tokens/classes).

**Status:** Draft for implementation

---

## 1. Goals

1. **Progress visible at the point of launch.** The dialog that launched the job reports what the job is doing: queued, running (with live stdout/stderr log tail), succeeded, failed, or canceled — without the user navigating away to the Run data tab.
2. **Logs preferred, spinner as floor.** Render the live log tail as the primary indicator; a status badge + elapsed time as the summary; an animated indeterminate indicator only while there is nothing to show yet ("Waiting for output…").
3. **Zero backend change.** Everything required is already emitted by the existing job SSE stream (`start` / `log` / `status` events) — no new endpoints, no new event types.
4. **Clean lifecycle.** The dialog's EventSource is detached when the run reaches a terminal state or when the dialog/widget is cleared — no leaked connections, no stale DOM writes.

## 2. Non-Goals

- No server-side changes: no new `progress` event type, no structured phase/percentage metadata from `run_candidate.py`. Phase extraction (e.g. "profile 2/3") is future work; this spec derives activity from raw log lines.
- No changes to the Run data tab (`#settingsRunSection`) behavior, job rows, or its console — existing streaming there stays as-is.
- No progress pane for the other actions ("Run candidate now", "Re-judge", "Agent") that use `launchProfileJob`. They keep the current notice. The dialog pane is scoped to the "Run with options…" (`data-action="prefill"`) flow. Reuse of the pane component for `launchProfileJob` is allowed and encouraged, but not required by this spec.
- No keyboard-shortcut, no cross-tab/native-notification features.
- No re-architecture of the shared `toolCardHtml` / `wireToolCard` widget. The pane is an addition inside `#dlgRunForm`, below the widget.

## 3. Context: Existing System (must not regress)

- Trigger: profile dialog → candidate row "⋯" menu → **Run with options…** (`data-action="prefill"`, `settings.js` line ~345) → `openRunProfilePrefill(slug, tb)` mounts a `run_candidate` tool card into `#dlgRunWidget` (`settings.js` line ~557).
- Submit: user tunes options, clicks **Run script** → `runToolSubmit(card, tool)` (`settings.js` line 1119):
  1. Validates args locally + `toolNeedsKey`/`OPENROUTER_API_KEY` guard.
  2. `POST /api/jobs` with `{ toolId: 'run_candidate', args }` (+ `confirm` if destructive — not the case here).
  3. On success: `refreshJobs()`, then if `res.job.status` is `running`/`queued` → `expandJob(res.job.id)` + `attachStreamIfPossible(res.job.id)` (expands the Run data row and streams it).
  4. If launched from the dialog (`inDialog`, card inside `#dlgRunForm`): `setDialogNotice("Launched … — watch it stream in Run data.")`.
- Current gap: the dialog stays open above a static notice. No inline indication that the job progressed, finished, or failed. The job row expansion happens on a different tab the user is not looking at.
- SSE upstream (`vite.config.js` job manager): `emit(job, 'start', …)`, `emit(job, 'log', { stream, text })` (batched, ~20 ms flush), `emit(job, 'status', { status, exitCode, error?, resultHints? })`, `: ping` keepalive (~15 s). Logs are also mirrored to `artifacts/jobs/<id>.log`. Server supports multiple concurrent SSE subscribers per job (`job._subs` is a `Set`).
- Stream helpers today (`settings.js`): `attachStreamIfPossible` (line 1453) keys the EventSource by job id in `RUN_STATE.es`; `appendConsole` (line 1252) appends parsed log events into the job row's `.console-pre`; `announceConsole` (line 1488) updates the row badge + `aria-live` announce.
- **Collision pitfall:** `RUN_STATE.es` is keyed by job id and `attachStreamIfPossible` skips if `RUN_STATE.es.has(job)`. The dialog pane must use a **separate key namespace** (e.g. `dlg-<jobId>`) or its own map — otherwise the dialog stream and the Run data row stream for the same job would silently suppress each other.
- Cleanup contract: `clearInlineRunWidget()` (line 528) runs on `#dlgRunClose`, `openAddDialog`, `openEditDialog`, `closeDialog`. It wipes `#dlgRunWidget` HTML. The dialog stream must be detached there.
- Dialog notice: `setDialogNotice(msg, tone)` writes into `#dlgNotice`.

## 4. Behavior Contract

### 4.1 DOM structure (behavioral hooks; styling left to implementer)

Inside `#dlgRunForm`, below `#dlgRunWidget`, add a progress region that is hidden (`cm-hidden`) until a job is launched from this dialog widget:

```
#dlgRunProgress (hidden by default, aria-live="polite" on the status line only)
├── .dlg-progress-head
│   ├── status badge (reuse statusBadgeHtml / .job-badge .st-* classes)
│   ├── status line text ("Queued — run harness busy, position 2", "Running — 1m 23s", "Succeeded — 4m 05s", ...)
│   └── actions: View log · Open in Run data · Re-run · (Close)
├── .dlg-progress-console (hidden until first log line or terminal state)
│   ├── toolbar: Auto-scroll checkbox (checked), Clear view
│   └── .console-wrap > pre.dlg-console-pre (role="log")
└── .dlg-progress-spinner (indeterminate, CSS-only; shown while queued/running and console empty)
```

- Reuse existing tokens/classes: `.job-badge`, `.st-queued/.st-running/.st-succeeded/.st-failed/.st-canceled`, `.con-err` (stderr), `.mini-btn`, `.console-wrap`, `.autoscroll-toggle`.
- Spinner: indeterminate CSS animation; `animation: none` under `prefers-reduced-motion` (fall back to static "Running…" text with pulsing dot or nothing animated).
- No new color scheme, no pixel-level design requirements — visual layer is implementer's choice within the existing dashboard look.

### 4.2 Submit → pane lifecycle (`runToolSubmit` in dialog context)

1. **In-flight guard:** while `POST /api/jobs` is pending, disable the dialog widget's `.tool-run-submit` (prevent double-submit). Existing `updateToolGuards` already disables submit while the same toolId has a queued/running job — that takes over after refresh; the in-flight guard covers the window before `refreshJobs()` runs.
2. **POST failure (network / 400 / 429 / 409):** show `showCardError`/`setDialogNotice` exactly as today. No pane, no stream. For 409 "already running — identical args" the message must additionally include an **Open in Run data** link so the user can jump to the existing job (409 body carries no job id; the Run data list is source of truth).
3. **Duplicate (HTTP 200, `res.job.duplicate === true`):** open the pane immediately in **queued** state for `res.job.id`, attach the dialog stream, and set the status line to "Already queued — identical job" + the job id short form. Do not fire a second POST.
4. **Fast-terminal job (`res.job.status` already `succeeded`/`failed`/`canceled`/`interrupted`, or `failedFast: true`):** render the pane directly in the terminal state (see 4.4) using `res.job`; **do not** open an EventSource. If the POST body was rejected (`failedFast`), show error tone in the status line.
5. **Normal launch (`running` or `queued`):**
   - Show pane; set badge + status line from `res.job.status`.
   - If queued: append queue position derived from `RUN_STATE.jobs` (index among `status === 'queued'`, 1-based) into the status line: "Queued — position 2 of 3 (run harness busy)". Position may be unavailable on the first render before `refreshJobs()` completes — see 4.6.
   - Start the elapsed timer (1 s interval, ticks "1m 23s" style reusing `durationStr` with `startedAt`; `aria-hidden="true"` on the timer text).
   - Open dialog EventSource `GET /api/jobs/<id>/stream`, tracked under a **dialog-scoped key** (e.g. `RUN_STATE.dlgEs.set(id, es)`), NOT `RUN_STATE.es`.
   - Keep the existing `expandJob` + `attachStreamIfPossible` calls for the Run data row untouched — both streams now coexist (server supports multiple subscribers).
   - Update the launch notice copy: `Launched job <code>abc12345…</code> — progress below. Open in <strong>Run data</strong> for the full job history.`

### 4.3 Stream event handling (dialog subscriber)

| SSE event | Dialog behavior |
|---|---|
| `start` (`{ jobId, pid }`) | Badge → Running; status line "Running — <elapsed>"; keep spinner only while console empty; start timer (if not already). |
| `log` (`{ stream, text }`) | Append to `pre.dlg-console-pre`; hide spinner; stderr lines wrapped with `.con-err` (reuse `consoleContentHtml` rendering rules); autoscroll to bottom if Auto-scroll checked and `prefers-reduced-motion` not set; cap buffer at **400 lines** (drop oldest); per-event re-render of the `<pre>` is acceptable at this volume. |
| `status` (`{ status, exitCode, error?, resultHints? }`) | Update badge. If terminal (`succeeded`/`failed`/`canceled`/`interrupted`): stop timer, detach dialog stream (close EventSource, delete dialog-scoped key), render terminal summary (4.4). Non-terminal status transitions (e.g. re-emitted running) just refresh badge/status line. |
| `onerror` | Transient: set status line suffix "Reconnecting…"; EventSource auto-reconnects — do **not** detach. Detach only if the subscriber's own latest known status (from `GET /api/jobs/:id` or POST response) is terminal, mirroring the existing `attachStreamIfPossible.onerror` rule. |
| keepalive `: ping` | Ignore (browser handles). |

- Guard: do not open a second dialog stream for a job that already has a live dialog stream (keyed check).
- Appending: reuse the same line-splitting/escaping semantics as `appendConsole`/`consoleContentHtml` (escape via `esc`, preserve `\n` joins).

### 4.4 Terminal states (pane summary)

- **Succeeded:** badge ✓ Succeeded; status line "Succeeded — <duration>"; if `resultHints.runId` present, show "Run ID: <runId>" line and a **Open in explorer** action (opens `/explorer.html`); if `resultHints.resultsTsvUpdated`, show "results table updated"; keep the log tail visible; actions: View log, Re-run, Close.
- **Failed:** badge ✕ Failed; status line shows `job.error` (from status event `error` field, or last 5 stderr lines when absent — mirror server-side fallback); log tail kept, stderr highlighted; actions: View log, Re-run, Close.
- **Canceled / Interrupted:** badge ■; status line "Canceled — <duration>"; console preserved; actions: View log, Re-run, Close (same for interrupted, label "Interrupted").
- **View log:** `<a href="/api/jobs/<id>/log" target="_blank" rel="noopener">`, same as the job row link.
- **Open in Run data:** switches the settings nav to the Run data section (`#settingsRunSection` active) and leaves the dialog open behind it — or closes the dialog if that is simpler to implement without layout regressions; the user must end up looking at the job row (which is already auto-expanded by `expandJob`). Requirement: the click lands the user on the job's expanded console with its stream attached.
- **Re-run:** `POST /api/jobs` with the same toolId + args (reuse the job-row `job-rerun` pattern, minus the destructive guard which does not apply to `run_candidate`); on success, reset the pane to the running/queued state for the new job id (clear console, restart timer, new stream). On failure, `setDialogNotice(error)`.
- **Close:** clears the pane and returns the dialog to the form state (`clearInlineRunWidget` semantics for pane only — widget form stays as-is, or full clear per implementer choice; the form must remain usable for another launch).

### 4.5 Cleanup / detach rules (must not leak)

1. Terminal status event → close + delete dialog-scoped stream entry immediately.
2. `clearInlineRunWidget()` → close + delete any live dialog stream entry (this function already runs on `#dlgRunClose`, `openAddDialog`, `openEditDialog`, `closeDialog`). Registration point: same place `dlgRunClose` is wired (`bindModal`, ~line 783).
3. `stopRunPolling()` (module teardown in tests) → also close dialog-scoped streams.
4. Clearing/wiping `#dlgRunWidget` while a stream lives must be impossible — the stream's append target (`pre.dlg-console-pre`) must be a sibling node owned by the pane, and the pane must be torn down by the same `clearInlineRunWidget` path that wipes the widget. If the pane node is missing at event time (defensive), drop the event silently.
5. Server-side job execution is unaffected by any client detach — closing the dialog never cancels the job.

### 4.6 Timing / race notes

- Queue position depends on `refreshJobs()`; `runToolSubmit` already awaits it after POST (line 1138). If position is still unknown (job not in `RUN_STATE.jobs` yet — e.g. list capped at 60), render "Queued — waiting for a free slot" and optionally recompute on the next `renderJobs()` while the pane is in queued state.
- `start` event may arrive before the dialog EventSource finishes opening (SSE replays from connection; events are buffered server-side per `_events` with `Last-Event-ID` replay — verified existing behavior in tests). Treat `start` as idempotent.
- Terminal status can arrive immediately after POST (very fast/instant jobs). The pane must render the terminal summary directly from the status event; the elapsed timer must not tick after terminal.

## 5. Copy (user-visible strings)

- Launch notice: `Launched job <code>abc12345…</code> — progress below. Open in <strong>Run data</strong> for the full job history.`
- Queued: `Queued — waiting for a free slot` / `Queued — position 2 of 3 (run harness busy)`
- Duplicate: `Already queued — identical job <code>abc12345…</code>`
- No output yet: `Waiting for output…`
- Reconnecting: `… · reconnecting`
- Terminals: `Succeeded — 4m 05s` · `Failed — 12s` · `Canceled — 2m 01s` · `Interrupted — 3m 10s`
- Failed error line: `job.error`, fallback `Exited with code <code>`.
- Re-run failure (dialog): `Re-run failed: <message>`

## 6. Accessibility

- Status line lives in `role="status"` / `aria-live="polite"` — announces state transitions (queued → running → succeeded/failed/canceled) once each. Status updates must be **text changes on the status line**, not whole-pane re-renders, to avoid re-announcement.
- Log `<pre>` uses `role="log"` with `aria-live="polite"` (matches the Run data console). Do not mirror every line into a live region — the status line is the announced signal; `role="log"` semantics cover log additions at the AT's discretion.
- Elapsed timer: `aria-hidden="true"` and visually part of the status line — no per-second announcements.
- Spinner: `aria-hidden="true"`; the adjacent status text ("Running — 1m 23s") is the accessible signal.
- Auto-scroll default ON; disable smooth scrolling entirely under `prefers-reduced-motion` (existing pattern in `appendConsole` line 1264 — reuse it).
- Focus: after a terminal state, move focus to the first action button (View log / Re-run) so keyboard users can continue without tabbing through the log. Never steal focus during running.
- Color is never the only indicator: badge icons (✓ ✕ ■ ⋯ ⏱) accompany status text (already the case in `STATUS_BADGE`).

## 7. Performance

- One EventSource per dialog run; closed on terminal/teardown. No polling added for the dialog (existing 2 s polling stays scoped to the Run data section visibility).
- Elapsed timer: `setInterval` 1 s, cleared on terminal/detach.
- Log buffer capped at 400 lines in the dialog pane (vs 2000 in Run data) — re-rendering the `<pre>` per log event is acceptable at this cap and typical harness output volume (server already batches to ~20 ms frames).
- No layout thrash: the pane occupies its own flex box inside the existing `has-run-form` two-column layout; the console area has a bounded height (implementer choice, e.g. `max-height: 240px`) so long jobs don't grow the dialog unboundedly.

## 8. Edge Cases (exhaustive)

| Case | Behavior |
|---|---|
| Double-click Run script | Second POST blocked by in-flight disabled submit; if it still fires (race), server dedupe returns the same queued job (`duplicate: true`) — pane shows "Already queued". |
| 409 already running (identical args) | Error notice with **Open in Run data** link; no pane. |
| 429 rate limit / queue full | Existing error path (`showCardError`), no pane. |
| Missing `OPENROUTER_API_KEY` | Existing pre-submit guard, no POST, no pane. |
| Job fails before any log line | `status: failed` event → summary with error tail; spinner replaced by terminal rendering. |
| Job succeeds with no output lines | Console shows the empty/`Waiting for output…` placeholder until terminal; summary still renders Run ID/hints from status event. |
| Dialog closed mid-run | Dialog stream closed (4.5); job keeps running server-side; Run data row (auto-expanded at submit) keeps its own stream. |
| Dialog closed after terminal | No stream to close; pane torn down by `clearInlineRunWidget`. |
| Dialog + Run data row both streaming same job | Two EventSources, distinct keys — both receive events (server `_subs` Set). No cross-suppression (4.2 collision fix). |
| SSE drops mid-run | Status line "… · reconnecting"; auto-reconnect; detach only when last known status terminal. |
| Tab hidden / throttled timers | Timer may lag; recompute elapsed from `Date.now() - startedAt` on each tick and on visibility restore — display drifts are acceptable but must never go backwards. |
| Job terminal between POST response and stream open | Status event (or replay) renders terminal summary directly; timer never starts ticking post-terminal. |
| `refreshJobs()` fails right after POST | Pane still functional — it relies on the POST response + SSE, not the job list. Queue position line simply omitted. |
| Very chatty log (e.g. OpenRouter retries) | 400-line cap + batched server frames keep the DOM bounded. |
| Widget re-mounted for a different profile while pane shown | Same `clearInlineRunWidget` path (called by `openRunProfilePrefill` line 560) tears the pane down first. |

## 9. Test Plan (`dashboard/tests/settings.ui.test.mjs`, jsdom harness)

Harness already stubs `window.EventSource` (line 160) recording URLs into `state.es` with no-op `addEventListener`/`close`. Extend the stub to capture listeners so tests can emit fake `log`/`status` events (mirror pattern used by the run-data stream test at line 599).

New tests:

1. **Dialog submit renders progress pane in running state** — after POST with `state.jobsPost` = running job, assert `#dlgRunProgress` visible, badge text "Running", timer node present, no notice-only outcome.
2. **Log events append into dialog console** — emit `{stream:'stdout', text:'Running profile: x'}`; assert `pre.dlg-console-pre` contains the line; emit stderr line; assert `.con-err` wrapper.
3. **Terminal status detaches dialog stream and renders summary** — emit `status` `{status:'succeeded', resultHints:{runId:'r1'}}`; assert `close()` called on the dialog EventSource, "Succeeded" text, "Run ID: r1", spinner hidden.
4. **Failed run shows error** — emit `status` `{status:'failed', error:'boom'}`; assert error text and View log link href `/api/jobs/<id>/log`.
5. **`failedFast` POST → terminal pane, no EventSource** — `state.jobsPost` = `{job:{status:'failed', failedFast:true}, failedFast:true}`; assert `state.es` empty and pane shows failure.
6. **Duplicate POST → queued pane + stream to existing id** — `state.jobsPost` = `{job:{id:'j1',status:'queued'}, duplicate:true}`; assert "Already queued" and one EventSource to `/api/jobs/j1/stream`.
7. **Dialog stream + row stream coexist** — after dialog submit, `state.es` contains both `/api/jobs/<id>/stream` entries (length 2): one from `attachStreamIfPossible`, one dialog-scoped.
8. **clearInlineRunWidget closes dialog stream** — click `#dlgRunClose` while running; assert `close()` called and pane hidden; dialog remains open (existing behavior).
9. **Queue position line** — with one other queued job in `state.jobs`, assert "position 2".
10. **Double-submit guarded** — submit button disabled while POST in flight (assert via stub that resolves after a tick).
11. **Update existing test** "inline run form submits its own job from the dialog widget" (line 848): the notice assertion `/Launched run_candidate/` may stay, but the test must additionally assert the pane appears, and the notice copy change (5) is reflected where asserted.

Server-side: no changes → `tests/api.jobs.test.mjs` untouched.

## 10. Implementation Hints (non-normative)

- New module-level state: `RUN_STATE.dlgEs = new Map()` (or key prefix `dlg-<id>` on the existing map — choose the option with the least churn; the requirement is no key collision with `attachStreamIfPossible`).
- A single `attachDlgStream(jobId)` + `detachDlgStream(jobId)` pair mirroring the existing helpers, plus `resetDlgPane(job)` for re-run.
- Render helper for the pane reuses `statusBadgeHtml`, `consoleContentHtml`-equivalent, `durationStr`, `esc`, and the `.mini-btn` / `.console-*` CSS already present — no new design system.
- `aria-expanded`/disabled handling on Re-run/Close follows the job-row button patterns (`renderJobs`, line 1344+).