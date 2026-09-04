Feature: Dashboard Python Script Runner + dashboard/README.md

Spec: specs/dashboard-python-runner.md

WHAT
Run every repo Python script from the web dashboard (no CLI). Deliverable #2: standalone dashboard/README.md documenting all tools/actions. No design_ref assets exist — full behavior contract written.

CONTEXT FOUND
- Dashboard = plain JS Vite app (dashboard/), dev server :3001. vite.config.js exports scanRequestHandler(ctx) middleware via configureServer + configurePreviewServer; endpoints today: /api/models CRUD+probe, /runs-list, /runs/*, /data/*, /notes.
- runPythonAsync() exists but is BUFFERED execFile, 300s timeout — unsuitable for long runs. Feature needs new streaming spawn-based job manager.
- Settings overlay already has disabled "Run data" nav item ("coming soon") — designated home for feature.
- 12 CLI scripts to expose across 4 groups: corpus validation (build_rubrics, build_bench, corpus_report), candidates (add_candidate, gen_profile_literal, snapshot_catalog), run harness (run_candidate, judge_existing, autoresearch/agent), analysis/maintenance (leaderboard, reset_benchmark). reset_benchmark is destructive (stdin confirm prompt).
- Tests: node:test + jsdom, makeCtx({pythonRunner: fake}) pattern — extend for jobs.

KEY SPEC DECISIONS
1. Server-side SCRIPT_REGISTRY whitelist: declarative tool specs (id, group, script, arg schema, destructive flag, outputs, runtimeClass). UI forms auto-generated from registry. No arbitrary command execution; argv array direct to spawn; path confinement to REPO_ROOT; bench validation against bench/ + runs/ listing or safe pattern.
2. Job model + state machine: queued→running→succeeded|failed|canceled; running→interrupted (server death). Job record: id, toolId, status, exitCode, pid, timestamps, logPath, cancelRequested, error, resultHints.
3. Concurrency: global lock per runtimeClass — one llm (run_candidate, judge_existing, agent) or write (build_*, add_candidate, gen_profile_literal, reset) job at a time; instant (leaderboard, corpus_report, snapshot_catalog) bypasses. FIFO queue, max 10, 409 when full. Duplicate identical queued job → return existing id.
4. API: POST /api/jobs (validate → 201), GET /api/jobs, GET /api/jobs/:id, GET /api/jobs/:id/stream (SSE: start/log/status/cancel events, 15s keepalive, Last-Event-ID replay, 10k event buffer), POST cancel (SIGTERM→10s→SIGKILL), GET log download, DELETE finished. Plus GET /api/env-check (presence-only) and GET /bench-list.
5. Destructive safety: reset_benchmark requires UI typed "RESET" + server re-checks confirm field; stdin fed y\n only post-confirm.
6. Secrets: env inherited, log scrubber masks sk-… and key=value tokens; never rendered to client.
7. Persistence: logs to artifacts/jobs/<id>.log (20MB cap + truncate marker); job meta lines embedded; startup restores history (interrupted status); 30-day log prune, 7-day meta.
8. UI: enable "Run data" tab; left tool cards/accordion forms with presets (Smoke mock, 30m all, 60m all + per-tool defaults), advanced-args toggle; right sticky job panel with status badges, live console (role=log, aria-live=polite, auto-scroll toggle, clear-view), cancel two-step, re-run, "Open in explorer"; refresh integration via window events (dashboard:results-refreshed redraws scatter; dashboard:candidates-refreshed reloads Models) + resultHints (resultsTsvUpdated, specPyChanged, bench/runId).
9. Edge cases covered: 402 credits, client disconnect via SSE replay, server restart mid-job, missing API key pre-warn, partial lines flushed, stderr tinted, interleaved throttled ≤50 events/s, duplicate submits, non-ASCII paths.
10. A11y: not-color-alone badges (icon+text), focus traps, aria-required/describedby, prefers-reduced-motion, 44px targets.
11. README spec: 8-section normative outline (quick start, interface map with per-control tables for all 3 views + settings, Run data walkthrough, CLI→dashboard workflow mapping table covering every Makefile target + README python3 example, API reference, tests, troubleshooting).
12. Tests: new api.jobs.test.mjs + settings.ui.test.mjs extensions; existing api.models fixtures must stay green.

Files written (commit separately per instruction):
- specs/dashboard-python-runner.md (main spec)
- specs/dashboard-python-runner.summary.md (this summary)

Not implemented: no code written; underlying python scripts unchanged; /api/models flow untouched (no double gen_profile_literal).