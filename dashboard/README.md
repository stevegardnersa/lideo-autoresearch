# Dashboard

Vite dev server for the benchmark dashboard — model registry UI plus a
**Run data** panel that lets you run every Python script in the repo from the
browser. Long jobs stream live stdout/stderr over SSE.

## Start

```bash
cd dashboard
npm install
npm run dev      # http://localhost:3001, proxies /api/* to the repo root scripts
```

Requires `OPENROUTER_API_KEY` in `dashboard/.env` (or the shell) for anything
that calls OpenRouter: `add_candidate`, `run_candidate` (unless `--mock`),
`judge_existing`, `agent`, `snapshot_catalog`.

## Run data (python script runner)

The **Run data** tab lists every script from the registry grouped by purpose:

- **Corpus validation** — `build_rubrics`, `build_bench`, `corpus_report`
- **Candidates** — `add_candidate`, `gen_profile_literal`, `snapshot_catalog`
- **Run harness** — `run_candidate`, `judge_existing`, `agent`
- **Analysis & maintenance** — `leaderboard`, `reset_benchmark`

### Behavior

- **One long script at a time.** Corpus validation / candidates / run originate
  from the same Python env, so *writable* jobs (runtime class `write`/`llm`)
  serialize FIFO: the second job queues until the first finishes. `instant`
  jobs (report-style, read-only) bypass the lock and run immediately.
  The queue is capped at 10.
- **Per-profile quick actions.** In the *Models → Edit* dialog each existing
  profile row has a `⋯` menu: **Run candidate now** (launches immediately with
  the profile slug and last-known bench), **Run with options…** (opens the same
  run widget inline in the dialog — right-hand column, LLM judge off by
  default — so you can tweak bench, judge, and flags before submitting),
  **Re-judge (LLM)** (asks for a judge model once, remembers it), and
  **Autoresearch agent** (bounded by the profile's 30m/60m budget). Run and
  agent launch straight into the job queue; the inline form keeps the model
  dialog open so you can review what you submitted.
- **SSE** — click a running or queued job to expand it and stream live output.
  Reconnect uses `Last-Event-ID`, so refreshes don't lose output.
- **Hard limits** — logs are capped (20 MB, truncation marker), SSE buffers are
  capped (10k events → stream returns 410 and the UI polls instead), strings are
  length-limited, and value validation happens both client- and server-side.
- **Cancellation** — queued jobs are removed immediately; running jobs get
  `SIGTERM` then `SIGKILL` after 10 s. Cancel is two-step.
- **Secrets scrubbed** — anything matching `sk-…` longer than 15 chars or a
  known `KEY=…` assignment is redacted before it touches disk or SSE.
- **Path confinement** — `bench`/path args reject absolute paths and `..`;
  benchmark paths must be bare names or `bench/*.jsonl`.
- **Confirmation** — `reset_benchmark` (the destructive script) requires typing
  `RESET`, re-checked server-side before spawn.

### Job lifecycle

1. `POST /api/jobs` `{ toolId, args, confirm? }` → validates → queues.
2. Script spawns with `python3 <script> --<arg> <value> …` in the repo root.
3. `GET /api/jobs` lists jobs; `GET /api/jobs/<id>` returns one.
4. `GET /api/jobs/<id>/stream` opens SSE (`start`, `log`, `status`, `cancel`).
5. `GET /api/jobs/<id>/log` downloads the redacted log file.
6. `POST /api/jobs/<id>/cancel` cancels (queued removal or TERM→KILL).
7. `DELETE /api/jobs` clears finished jobs older than 1 h.

Job metadata lives in `artifacts/jobs/<id>.log` (git-ignored); finished logs
are pruned after 30 days.

## API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/registry` | GET | Script registry + group labels |
| `/api/jobs` | GET / POST / DELETE | List / create / clear-finished jobs |
| `/api/jobs/:id` | GET | Job detail |
| `/api/jobs/:id/stream` | GET | SSE event stream |
| `/api/jobs/:id/log` | GET | Redacted log download |
| `/api/jobs/:id/cancel` | POST | Cancel job |
| `/api/env-check` | GET | Which required env keys are missing |
| `/bench-list` | GET | Bench names from `bench/` and `runs/` |
| `/api/models` | GET/POST/PUT/DELETE | Model & profile registry |
| `/api/models/probe` | GET | Probe model capabilities |
| `/runs` | GET | Score run data used by the explorer |

## Tests

```bash
npm test              # whole suite (server + UI)
npm run test:api      # server endpoints only
npm run test:ui       # jsdom UI harness
```

`tests/api.jobs.test.mjs` uses a fake subprocess fixture with full argv capture,
so validation, serialization, cancellation, SSE ordering, log truncation, and
secret scrubbing are covered without touching real Python.