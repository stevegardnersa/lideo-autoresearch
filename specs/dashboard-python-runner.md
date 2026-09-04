# Spec: Dashboard Python Script Runner + Dashboard README

**Feature:** Run every Python script (corpus validation, candidate management, run harness, judging, leaderboard, maintenance) from the web dashboard — no command line. Ships with a standalone `dashboard/README.md` documenting every tool, action, and workflow.

**Input needed:** None — this is the complete behavior contract.

**Status:** Draft for implementation

---

## 1. Goals

1. **Zero-CLI operation.** Every script the user currently runs from a terminal (`Makefile` targets + README commands) is reachable from the dashboard with the same defaults and flags.
2. **Live feedback.** Long-running scripts stream stdout/stderr into the browser in real time with status, cancel, and re-run.
3. **Safety.** Destructive scripts require explicit confirmation; args are validated server-side; no arbitrary shell input reaches the host.
4. **Documented.** `dashboard/README.md` explains every control in the interface and the workflow for each script group.

## 2. Non-Goals

- No arbitrary command execution from the UI (whitelist registry only).
- No replacement of the existing `/api/models` (add/probe/edit/delete candidate) endpoints — they already run `tools/add_candidate.py`, `tools/gen_profile_literal.py` and must keep working unchanged. The runner may share the underlying Python spawn helper but must not double-trigger `gen_profile_literal.py`.
- No new auth system (dashboard is localhost-only today; runner inherits the same trust model).
- No remote distribution of jobs; no cron/scheduling.

## 3. Context: Existing System (must not regress)

- Dashboard = plain HTML/JS Vite app, root `dashboard/`, dev server port **3001** (`vite.config.js`).
- `dashboard/vite.config.js` exports `createScanPlugin({ repoRoot, runsDir, dataDir, candidatesPath, pythonRunner, autoTag })`; middleware `scanRequestHandler(ctx)` mounted via `configureServer` **and** `configurePreviewServer`.
- Existing endpoints: `GET/POST/PUT/DELETE /api/models`, `POST /api/models/probe`, `GET /runs-list`, `GET /runs/*`, `GET /data/*`, `GET /notes`, `GET /notes/all`, `POST /notes`.
- `runPythonAsync(args, stdin, cwd)` exists: `execFile`, **buffered**, timeout **300 s**, `maxBuffer` 64 MB. Used for fast, idempotent ops (add_candidate, gen_profile_literal). **Do not reuse for long jobs** — it cannot stream and kills long runs at 5 minutes.
- Settings overlay (`settings.js` embedded in `index.html`) has nav sections: **Models** (active), **Run data** (disabled, "Coming soon"), **Prompts** (disabled), **Judges** (disabled). The "Run data" section is the designated home for this feature.
- Tests: `node --test` with jsdom; `tests/api.models.test.mjs` builds real HTTP fixtures around `makeCtx({ pythonRunner: fake })`; `tests/settings.ui.test.mjs` drives the DOM.

## 4. Architecture

```
Browser (index.html: settings overlay "Run data" tab)
   │  fetch POST /api/jobs   │  EventSource GET /api/jobs/:id/stream
   ▼                         ▼
vite middleware: scanRequestHandler(ctx)
   │
   ├── JobRegistry (in-memory Map, jobId → Job)
   ├── JobQueue (serial executor, max 1 running for "llm"/"write" class)
   ├── JobManager.attach() → child_process.spawn(PYTHON, args, { cwd: REPO_ROOT, env, stdio pipes })
   │      stdout/stderr → line-batched broadcast → SSE clients + append to log file
   └── ScriptRegistry (declarative tool specs; validation; destructive flags)
```

- `PYTHON` resolution: reuse existing `process.env.PYTHON || 'python3'`.
- Child processes run with `cwd = REPO_ROOT` (project root, `join(VITE_DIR, '..')`), same as today.
- Child environment: inherited `process.env` (so `OPENROUTER_API_KEY`, `OPENROUTER_MANAGEMENT_KEY`, `GOOGLE_BOOKS_API_KEY` flow through). The runner must never echo these values to the client or log files.

## 5. Script Registry (Server-Side Whitelist)

Every tool is declared in a `SCRIPT_REGISTRY` table. The UI renders forms from this table; the server validates argv against it. Fields per tool: `id`, `group`, `title`, `description` (from module docstring), `script` (path under REPO_ROOT), `args[]` (see schema below), `stdin` (static string to feed, e.g. `y\n` for confirm prompts), `destructive` (bool), `outputs[]` (artifacts produced, for post-run refresh hints), `runtimeClass` (see §7 Concurrency).

Groups (mirror the user's mental model and README sections):

### 5.1 Corpus validation
| id | script | args (defaults) | outputs |
|---|---|---|---|
| `build_rubrics` | `tools/build_rubrics.py` | `--books-root` (data/books, text), `--artifacts-root` (artifacts) | artifacts/book_rubrics, artifacts/rubrics |
| `build_bench` | `tools/build_bench.py` | `--books-root`, `--bench-dir` (bench), `--dev-books` (10, 1–1000), `--gate-books` (4), `--holdout-books` (4), `--chapters-per-dev-book` (4), `--seed` (42), `--split-mode` (enum: balanced_genre/random), `--stratify-field` (genre_macro) | bench/*.jsonl, bench/splits.json |
| `corpus_report` | `tools/corpus_report.py` | `--books-root` (data/books) | console report |

### 5.2 Candidates
| id | script | args | outputs |
|---|---|---|---|
| `add_candidate` | `tools/add_candidate.py` | `--model-full` (required, pattern `^[a-z0-9-]+/[a-z0-9.\-]+$`), `--time-budget` (multi, enum 30m/60m), `--dry-run`, `--provider-route` (JSON, validated parse) | data/candidates.json (+ auto regenerate spec.py via existing flow) |
| `gen_profile_literal` | `tools/gen_profile_literal.py` | none | candidate_spec.py (regenerated) |
| `snapshot_catalog` | `tools/snapshot_catalog.py` | `--api-key-env` (fixed OPENROUTER_API_KEY, not editable) | snapshots/catalog/*.json, snapshots/pricing/*.json |

### 5.3 Run harness
| id | script | args | outputs / runtime class |
|---|---|---|---|
| `run_candidate` | `core/run_candidate.py` | `--bench` (required; see §6.3 validation), `--profile` (required; pattern `^[A-Za-z0-9_.\-]+$`, allow `all`), `--time` (enum all/30m/60m), `--judge-model` (optional, model pattern), `--mock` (checkbox), `--write-results` (checkbox), `--max-samples` (int ≥ 0), `--run-id` (pattern), `--resume` (pattern), `--wait-for-credits` (checkbox), `--notes` (text, ≤ 500 chars) | runs/<bench>/<run_id>/*, results.tsv (when --write-results) — class `llm` |
| `judge_existing` | `core/judge_existing.py` | `--bench` (required), `--judge-model` (required pattern), `--run-id`, `--profile`, `--max-samples` (0=all), `--dry-run`, `--force-overwrite` | runs/<bench>/*__llmj_* — class `llm` |
| `agent` | `autoresearch/agent.py` | `--model` (optional pattern), `--budget` (enum 30m/60m), `--thinking` (enum thinking/notthinking), `--candidate` (text), `--mode` (enum hill_climb/grid_search/auto), `--max-iter` (int 1–50), `--max-variants` (int 1–50), `--dry-run`, `--stage` (enum chapter/composer) | candidate_spec.py edits — class `llm` |

### 5.4 Analysis & maintenance
| id | script | args | notes |
|---|---|---|---|
| `leaderboard` | `tools/leaderboard.py` | `--profile`, `--bench`, `--model-contains`, `--sort-by` (enum mean_utility/…), `--top` (int 1–100), `--slice-field` (enum from candidate_spec dimensions), `--slice-value` | fast, class `instant` |
| `reset_benchmark` | `reset_benchmark.py` | none | **destructive; requires typed confirmation** "RESET" in UI (§8); stdin fed `y\n` only after confirm; deletes artifacts/runs, results.tsv, data/candidates.json, bench/book_gate.jsonl, snapshots |

### 5.5 Existing fast endpoints (no change)
`POST /api/models`, `POST /api/models/probe`, `PUT /api/models`, `DELETE /api/models` continue to use `runPythonAsync` as-is with 300 s timeout.

**Arg schema (server-side validation)**, per arg: `{ name, label, type: 'text'|'enum'|'int'|'bool'|'json', required, default, min, max, choices[], pattern, hint, group }`. Server rejects unknown args, wrong types, out-of-range ints, non-matching patterns, non-enum values, unparseable JSON. Unknown keys → 400 with per-field errors.

## 6. Job Model

```ts
interface Job {
  id: string            // randomUUID()
  toolId: string        // registry key
  args: Record<string, unknown>   // sanitized, resolved argv
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled' | 'interrupted'
  exitCode: number | null
  pid: number | null
  createdAt: string     // ISO
  startedAt: string | null
  finishedAt: string | null
  logPath: string       // artifacts/jobs/<id>.log
  cancelRequested: boolean
  error?: string        // headline error (last lines of stderr)
  resultHints?: { bench?: string; runId?: string; resultsTsvUpdated?: boolean; specPyChanged?: boolean; snapshotsCreated?: string[] }
}
```

State machine: `queued → running → succeeded | failed | canceled`; `running → interrupted` (server shutdown); `queued → canceled` (cancel before start). Terminal states are final.

## 7. Concurrency & Queue

- **`runtimeClass`**: `llm` (run_candidate, judge_existing, agent), `write` (build_bench, build_rubrics, add_candidate, gen_profile_literal, reset_benchmark — mutate shared corpus files), `instant` (leaderboard, corpus_report, snapshot_catalog — read-only/fast, no file races with UI reads).
- **Rule:** one `llm` or `write` job runs at a time (single global lock for those classes — candidates.json / results.tsv / runs/ are single-writer). `instant` jobs are allowed to run while an `llm` job is active (they only read).
- Classification of a queued job: starts immediately if lock free; otherwise `queued`; queue is FIFO per class. `instant` jobs bypass queue.
- Max queue length: 10. Rejection: 409 `{ ok:false, error:'queue full' }`.

## 8. API Contract

All under `/api/jobs`. JSON body for POST. Errors always `{ ok:false, error, fieldErrors? }`.

### 8.1 `POST /api/jobs` — start job
Request: `{ toolId, args: { …form values… }, confirm?: 'RESET' }`
- Server resolves tool from registry; 404 if unknown.
- Validate args; 400 with `fieldErrors: { [arg]: message }`.
- If `tool.destructive`: require `confirm` string equal to the tool's `confirmPhrase` (`'RESET'`), else 400 `{ error: 'confirmation required' }`. Client must show typed-confirmation dialog (§10) before sending.
- Create job, enqueue, respond **201**:
```json
{ "ok": true, "job": { "id": "…", "status": "queued", "toolId": "run_candidate", "createdAt": "…" } }
```
- Note: for `add_candidate` non-dry-run the middleware keeps responsibility for the follow-up `gen_profile_literal.py` regeneration (existing behavior in `/api/models`). The jobs runner itself does not chain scripts.

### 8.2 `GET /api/jobs` — list
Query: `?limit=50&status=running` (optional filters: status, toolId). Response:
```json
{ "ok": true, "jobs": [ { "id","toolId","status","exitCode","createdAt","startedAt","finishedAt","error","resultHints" } ] }
```
Order: running/queued first, then newest-completed first. `limit` cap 200 server-side.

### 8.3 `GET /api/jobs/:id` — detail
Full job incl. `pid`, `logPath`, `cancelRequested`. 404 if unknown id.

### 8.4 `GET /api/jobs/:id/stream` — SSE
- Content-Type `text/event-stream`; `Cache-Control: no-cache`; keepalive comment `: ping` every **15 s**.
- Event types:
  - `event: start` → `{ jobId, pid }`
  - `event: log` → `{ stream: 'stdout'|'stderr', text: '<line-batch>' }` — multi-line batches, see §16 throttling
  - `event: status` → `{ status, exitCode, error?, resultHints? }` (sent on every transition; terminal events close the stream)
  - `event: cancel` → `{ cancelRequested: true }`
- Reconnect: client sends `Last-Event-ID` = last received event sequence number (monotonic per job, starting 0). Server replays buffered events after that seq (buffer kept in memory for running jobs, capped 10 000 events). Missing/expired → 410 `{ error: 'stream expired' }`, client falls back to `GET /api/jobs/:id` polling at 2 s while job is active.
- Non-JSON parseable lines from python (progress bars, partial lines like `\r`) are normalized: each complete line is one event payload; partial lines accumulate until newline, flushed 200 ms after last write.

### 8.5 `POST /api/jobs/:id/cancel`
- If `queued`: remove from queue → status `canceled`, `finishedAt` set, stream emits `status`.
- If `running`: set `cancelRequested`, then `child.kill()` (SIGTERM); after 10 s grace, SIGKILL. Scripts should exit on SIGTERM; if they don't, forced kill → status `canceled` with `exitCode = null`, `error = 'killed'`.
- Terminal job → 409 `{ error: 'job already finished' }`.

### 8.6 `GET /api/jobs/:id/log` — download full log
Text/plain file at `artifacts/jobs/<id>.log`. 404 if log pruned.

### 8.7 `DELETE /api/jobs` — clear completed
Removes finished jobs (status in terminal set, older than 1 h) from memory + deletes their `.log` files. Returns `{ ok:true, removed: N }`. Never touches running/queued.

## 9. Security

- **Whitelist only.** `SCRIPT_REGISTRY` is the sole argv source; user input can only fill declared args. No shell, no `exec`, no string interpolation into a command line — argv array passed directly to `spawn`.
- **Path confinement.** Any arg typed as `path` (books-root, bench-dir, artifacts-root, bench, run-id, resume, output) is resolved against `REPO_ROOT` and must remain inside `REPO_ROOT` (reject `..` segments and absolute paths outside root). `bench` additionally validated: if it names an existing file it must live under `bench/` or `runs/` listing (from `/bench-list` enum or filesystem glob `bench/*.jsonl` + `runs/*/` dirs); otherwise must match `^[A-Za-z0-9_.\-]+$` (no `/`, no `..`) so it cannot escape. Hmm — `--bench` does accept a path to a JSONL per `run_candidate` help ("or path to JSONL"); allow relative path under `bench/` only.
- **Secrets:** env inherited but never rendered; log scrubber masks anything matching `(sk-[A-Za-z0-9]{16,}|OPENROUTER_API_KEY=[^\s]+)` in streamed events and log files with `[REDACTED]`.
- No CORS changes; same-origin as today. Accept-header/body size limits: POST body ≤ 64 KB; JSON strings ≤ 2 KB each.
- Rate limiting: max 20 job creations per hour per server instance (local tool; prevents accidental runaway).

## 10. UI — Settings Overlay "Run data" Section

Enable the disabled nav item: `data-section="run"` (`Run data`), same styling as Models section.

### 10.1 Tool list (cards)
- Left column: grouped tool cards (Corpus validation / Candidates / Run harness / Analysis & maintenance). Each card: title, one-line description (registry `description`), status dot if currently running/queued.
- Click → expands inline form (accordion). Form fields auto-generated from registry `args[]`: text inputs, selects for enums, checkboxes for bools, `placeholder` shows default. Required args marked `*`.
- Every card has a **Run** primary button. Disabled while that tool has a queued/running job (tool-level guard) OR while any `llm`-class job runs (lock hint tooltip: "Run harness busy — queued runs start automatically").
- Advanced args hidden behind "Show advanced" toggle (judge_model, max_samples, run-id, resume, provider-route, split-mode, stratify-field, chapters-per-dev-book…). Defaults pre-filled from registry.
- Quick presets per tool (e.g. run_candidate: "Smoke (mock)", "30m all profiles", "60m all profiles", "Custom") — presets map to prefilled forms, same as README command examples (§13).

### 10.2 Destructive confirmation (reset_benchmark)
- Opening form shows warning panel: red border, "Deletes artifacts/runs, results.tsv, data/candidates.json, bench/book_gate.jsonl and snapshots. Irreversible."
- **Typed confirmation**: input "Type RESET to continue" + disabled Run until `confirm === 'RESET'` (case-sensitive). Server re-checks the same `confirm` field (§8.1). A11y: warning has `role="alert"`; focus moves into confirm input when form expands.

### 10.3 Job panel
- Right column, sticky: "Runs" list. Header: refresh button, "Clear finished" button (§8.7), live count badge ("1 running · 2 queued").
- Each job row: tool title, status badge (color + icon + text, §14), timestamps (created, finished + duration), link "view log" (opens `GET /api/jobs/:id/log` in new tab).
- Click row → expands **live console**: `<pre>` log pane (monospace, auto-scroll, "Auto-scroll" toggle, "Clear view" button — does not touch server log). `aria-live="polite"` region announcing status transitions only (not every line; prevents screen-reader flooding).
- Controls in expanded view: **Cancel** (visible when queued/running; two-step confirm in-button: first click turns into "Confirm cancel?"), **Re-run** (visible when terminal; POSTs same args again, no confirm unless destructive).
- Empty state: "No runs yet. Pick a tool on the left to run your first script."
- Offline/error-state banner (§12) when job fetch fails: "Cannot reach dashboard server — is `npm run dev` running on :3001?"

### 10.4 Post-run result integration (refresh hints)
- `resultHints.resultsTsvUpdated` (true when run_candidate with `--write-results` succeeded): trigger window event `dashboard:results-refreshed`; `main.js` re-fetches `/runs-list` + run files and redraws scatter (existing load path refactored into an `async function loadData()` — already the structure).
- `resultHints.specPyChanged` (gen_profile_literal, agent, add_candidate): dispatch `dashboard:candidates-refreshed` → Models section reloads list (existing `initSettings` fetch).
- `resultHints.bench`/`runId` (run_candidate): "Open in explorer" button appears on the finished job → `window.open('/explorer.html')` (explorer already lists runs; no change).
- Snapshot jobs (`snapshot_catalog`): success message lists created files with copy-to-clipboard.

## 11. Persistence & Retention

- Log files: `artifacts/jobs/<jobId>.log` (repo root artifacts dir), appended via stream piping, size cap **20 MB** (trim head, append `\n[log truncated]\n` marker).
- Job records: in-memory only for live jobs; on server startup, `GET /api/jobs` includes a compact history section parsed from existing log files (jobId, toolId, status=interrupted if no terminal marker, timestamps from first/last lines `[job] meta:` prefix lines that the middleware writes on start/finish). This gives restoration of "recent completed runs" without a DB.
- Prune: log files older than 30 days removed on startup; completed job meta dropped from history after 7 days.
- Retention constants in one exported object (`JOB_LIMITS`) for tests.

## 12. State Flows & Error Handling

| Scenario | Behavior |
|---|---|
| Script not found / missing arg handler | Job fails fast pre-spawn: status `failed`, error "script not found: …"; never queued |
| Python exits non-zero | `status: failed`, badge red, `exitCode` shown, error = last 5 stderr lines; log pane auto-expands showing tail; "view full log" link |
| Spawn error (ENOENT python3) | Same as above, error = spawn message; UI suggests `PYTHON=… make` env check |
| 402 insufficient credits mid-run | Log passes through; `run_candidate` handles pause/retry via `--wait-for-credits` if checked; job stays `running` (no client change) |
| Client disconnect mid-job | Job continues server-side; SSE reconnection with `Last-Event-ID` replays missed events (buffered §8.4); logs on disk anyway — no data loss |
| Server restart mid-job | Child killed by OS; on next start log history shows `interrupted`; user re-runs |
| Cancel requested | Status changes to `canceled` only after process exits (or SIGKILL after 10 s); terminal event closes stream |
| Network failure on POST | Client keeps form state, shows banner, "Retry" re-POSTs with same payload (idempotency: none needed — creating a new job is acceptable; duplicate detection: if identical toolId+args job is `queued`, respond with existing job id 200 instead of dup) |
| OpenRouter key missing | run_candidate/judge fail with clear stderr; UI pre-check: strings `OPENROUTER_API_KEY` unset → warn before launch ("Missing OPENROUTER_API_KEY — run needs it") via `GET /api/env-check` → `{ missingKeys: ['OPENROUTER_API_KEY'] }` (presence only, never value) |
| results.tsv being written while scatter reads | Existing `/runs/*` reads are per-file; results.tsv read errors → main.js shows stale-data notice, retries in 5 s (add: `fetch` failure on results.tsv → keep last known state + "Updating…" chip) |

## 13. `dashboard/README.md` — Required Content

Separate file at `dashboard/README.md` (new, committed). Document every tool/action. Structure:

1. **Overview** — dashboard purpose (benchmark explorer + model management + script runner).
2. **Quick start** — `npm install`, `npm run dev` (port 3001), `npm run build && npm run preview`, required env (`OPENROUTER_API_KEY` in repo-root `.env` sourced by the shell; note `PYTHON` override), browser open URL.
3. **Interface map** — three views: main scatter (index.html), explorer (explorer.html via "Explorer" link), settings overlay (gear). For each view list every control and its effect in a table:
   - Main: mode tabs (Price vs. Quality / Price vs. Faithfulness / Custom), run selector, search box, axis/size/color/label dropdowns, label toggle, quadrant highlight, fixed Y range, judge toggles (deterministic/LLM), download SVG, fullscreen, settings gear, detail card on point click.
   - Explorer: run file browser, chapter notes panel (add/auto-tag workflow via `/notes`), book/chapter source links.
   - Settings → Models: list, add-model dialog (probe preview = schema/thinking/non-thinking badges + created profiles), edit, delete, provider-route JSON.
4. **Run data section (new)** — full walkthrough: tool groups, per-tool form fields table (arg, type, default, meaning), presets, confirmation flow for reset, job panel states (queued/running/succeeded/failed/canceled), cancel/re-run, log viewer, refresh integration.
5. **Workflows mapped to old CLI** — table: "CLI command you ran before" → "Dashboard path now": one row per Makefile target and every README python3 example (§5 registry + §10.2 presets cover them: rubrics, bench, smoke, corpus-report, leaderboard, add_candidate variants, gen_profile_literal, run_candidate variants incl. mock/judge, judge_existing variants, reset_benchmark, snapshot_catalog, agent modes).
6. **API reference (server)** — brief table of all endpoints incl. new `/api/jobs/*`, notes that it is a local dev server, no auth.
7. **Tests** — `npm test`, `npm run test:api`, `npm run test:ui`.
8. **Troubleshooting** — common failures: missing key, queue full, port busy, python version mismatch, log truncated.

README must be generated/adjusted as part of this feature implementation; content is normative per this section.

## 14. Accessibility

- All controls reachable and operable by keyboard; visible focus rings matching existing style.
- Status never conveyed by color alone: badge = icon + text (e.g. "✓ Succeeded", "✕ Failed", "⏱ Running", "◼ Canceled", "⋯ Queued").
- Live console `role="log"` + `aria-live="polite"`; status transitions announced; line spam must not announce per-line.
- Forms: explicit `<label for>` per field; required marked with `aria-required` + `*`; error text `role="alert"` linked via `aria-describedby`.
- Confirmation dialogs: `role="dialog"` `aria-modal="true"`, focus trap, Esc closes, initial focus on confirm input.
- Cancel two-step: button `aria-pressed` reflects armed state.
- Reduce motion: auto-scroll console animations disabled via `prefers-reduced-motion` (CSS existing pattern).
- Target sizes ≥ 44 px for icon buttons (existing `.btn-icon`).

## 15. Performance

- Log streaming: batch lines; flush ≤ 50 events/sec per client; drop intermediate batches for slow consumers (always flush final batch + terminal event). Client renders log via one `<pre>` text node update per batch (no per-line DOM).
- Memory: middleware keeps per-job event buffer capped 10 000 events; log files written linearly; no full-file reads on stream (pipe).
- `GET /api/jobs` cost: O(jobs) single pass, no fs reads (history from in-memory map populated at startup once).
- SSE keepalive comment cheap; client uses single EventSource per running job, closed on terminal event.
- Main.js reload: full scatter redraw allowed (existing behavior), debounced 300 ms after `dashboard:results-refreshed`.

## 16. Edge Cases (additional)

- Partial output lines without trailing newline at process exit → flushed as final `log` event.
- stdout interleaving with stderr → two streams, each its own event; UI renders stderr in distinct tint (existing log color conventions).
- Job writes huge output fast (bench printables) → throttling + log-file trim cap (§11) prevents memory/disk blowup.
- Two tabs submit same run_candidate → duplicate suppression returns existing queued job id (§12 network-failure row); if already running, second submit 409 "already running" (same toolId + identical normalized args).
- Time zone: timestamps ISO-8601 UTC; UI renders local.
- `--provider-route` JSON: parse + validate keys ⊆ {only, order, allow, avoid} with string arrays; 400 on invalid.
- Non-ASCII paths/model IDs: argv passed as UTF-8 strings (execFile/spawn handle directly; no shell quoting hazard).
- `reset_benchmark` run while run/job writes results.tsv → queue lock (both `write` class) serializes; confirmation prevents accidental ordering.

## 17. Tests (extend existing suites)

- `tests/api.jobs.test.mjs` (node:test + `makeCtx({ pythonRunner })`): registry validation (unknown tool 404, field errors 400, destructive missing confirm 400), queue FIFO with fake runners (delayed resolves), cancel queued vs running, duplicate suppression, SSE stream events (start/log/status ordering + Last-Event-ID replay), log-file writing + truncation marker, `/env-check` presence-only response, path-confine rejection (`--bench ../etc/passwd` etc.).
- `tests/settings.ui.test.mjs`: Run data tab renders tool cards from registry fixture; form defaults; typed-confirm gates reset Run; job row states map to badges; cancel two-step; re-run POSTs same payload; empty state.
- Existing `api.models.test.mjs` fixtures remain green (no regression to /api/models).
- Manual smoke script (in README §8): `npm run test:api && npm run test:ui`, plus a `--mock` chapter_fast smoke run from UI verifying results.tsv refresh chip.

## 18. Implementation Notes

- All new code in `dashboard/vite.config.js` (registry + job manager + handlers) and `dashboard/settings.js` + `dashboard/index.html` (Run data section). Keep `vite.config.js` exports `{ makeCtx, scanRequestHandler, createScanPlugin, scanRunsPlugin }` backward-compatible; add `getScriptRegistry()`, `createJobManager()` exports for tests.
- New env check endpoint `GET /api/env-check` (presence of OPENROUTER_API_KEY / OPENROUTER_MANAGEMENT_KEY / GOOGLE_BOOKS_API_KEY as booleans) — used by pre-launch warn only.
- Bench list endpoint `GET /bench-list` → `{ benches: ['chapter_fast', 'book_gate', 'book_holdout', 'mock', …] }` derived from `bench/*.jsonl` + `runs/*/` dirs, for `--bench` select UI.
- Keep `runPythonAsync` untouched for /api/models; new `spawnJob` uses `child_process.spawn` for streaming.
- Ship `dashboard/README.md` per §13; link it from root `README.md` "Dashboard" section.