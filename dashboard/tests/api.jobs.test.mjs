import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { EventEmitter } from 'node:events'
import { scanRequestHandler, makeCtx, createJobManager, JOB_LIMITS, killAllChildren, liveChildren } from '../vite.config.js'

// ── fixtures ──────────────────────────────────────────────────

function makeFakeSpawn() {
  const procs = []
  const spawnFn = (argv, opts) => {
    const p = new EventEmitter()
    p.pid = 9000 + procs.length
    p.argv = argv
    p.opts = opts
    p.stdin = { ended: null, end(s) { p.stdin.ended = s == null ? '' : s } }
    p.stdout = new EventEmitter()
    p.stderr = new EventEmitter()
    p.killSignal = null
    p.kill = (sig) => { p.killSignal = sig; return true }
    p.write = (stream, text) => p[stream].emit('data', Buffer.from(text))
    p.exit = (code, signal) => p.emit('close', code, signal)
    procs.push(p)
    return p
  }
  return { spawnFn, procs }
}

let tmp

beforeEach(() => {
  tmp = realpathSync(mkdtempSync(join(tmpdir(), 'mm-jobs-')))
  mkdirSync(join(tmp, 'data'), { recursive: true })
  mkdirSync(join(tmp, 'bench'), { recursive: true })
  writeFileSync(join(tmp, 'bench', 'chapter_fast.jsonl'), '')
  writeFileSync(join(tmp, 'data', 'candidates.json'), '{"version":2,"profiles":{}}\n')
})

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true })
})

async function startServer(opts = {}) {
  const fs = opts.spawn ? { procs: opts.procs || [], spawnFn: opts.spawn } : makeFakeSpawn()
  const jobManager = createJobManager({
    repoRoot: tmp,
    jobsDir: join(tmp, 'jobs'),
    verifyScripts: opts.verifyScripts || false,
    jobLimits: opts.jobLimits || { ...JOB_LIMITS },
    spawn: fs.spawnFn,
    pythonRunner: opts.pythonRunner || (async () => ({ ok: true, stdout: 'regen\n', stderr: '' })),
  })
  const ctx = makeCtx({
    repoRoot: tmp,
    dataDir: join(tmp, 'data'),
    runsDir: join(tmp, 'runs'),
    candidatesPath: join(tmp, 'data', 'candidates.json'),
    jobManager,
  })
  const server = http.createServer(scanRequestHandler(ctx))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  return { server, base, jobManager, procs: fs.procs, fs }
}

async function closeServer(server) {
  server.closeAllConnections?.()
  await new Promise(resolve => server.close(resolve))
}

async function call(base, path, { method, body } = {}) {
  const r = await fetch(`${base}${path}`, {
    method: method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  return { status: r.status, body: await r.json() }
}

async function streamText(res) {
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let out = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    out += dec.decode(value, { stream: true })
  }
  return out
}

const runArgs = () => ({ bench: 'chapter_fast', profile: 'all', time: 'all', mock: true, 'write-results': true, 'max-samples': 4 })

// ── registry validation ──────────────────────────────────────

test('GET /api/jobs honors query params (regression: ?limit=60)', async () => {
  const { server, base } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'add_candidate', args: { 'model-full': 'openai/gpt-4o' } } })
    assert.ok(a.body.job, 'add_candidate job created')
    const b = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    assert.ok(b.body.job, 'run_candidate job created')
    const limited = await call(base, '/api/jobs?limit=1')
    assert.equal(limited.status, 200)
    assert.equal(limited.body.ok, true)
    assert.ok(Array.isArray(limited.body.jobs), 'list response is JSON (not html fallback)')
    assert.equal(limited.body.jobs.length, 1, 'limit honored')
    const q = await call(base, '/api/jobs?limit=60')
    assert.equal(q.body.ok, true)
    assert.equal(q.body.jobs.length, 2, 'unbounded-by-default list (limit=60) returns both jobs')
    const statusFiltered = await call(base, '/api/jobs?status=queued&limit=60')
    assert.equal(statusFiltered.body.ok, true)
    assert.ok(statusFiltered.body.jobs.length >= 1 && statusFiltered.body.jobs.every(j => j.status === 'queued'), 'status filter applied')
  } finally {
    await closeServer(server)
  }
})

test('POST /api/jobs rejects unknown tool with 404', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'definitely_nope', args: {} } })
    assert.equal(status, 404)
    assert.match(body.error, /unknown tool/)
  } finally {
    await closeServer(server)
  }
})

test('POST /api/jobs returns per-field errors for bad args', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'add_candidate', args: {} } })
    assert.equal(status, 400)
    assert.ok(body.fieldErrors['model-full'])
    assert.match(body.error, /invalid arguments/)
  } finally {
    await closeServer(server)
  }
})

test('POST /api/jobs rejects bad int range and unknown key', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/jobs', {
      method: 'POST',
      body: { toolId: 'run_candidate', args: { ...runArgs(), 'max-samples': -3, hack: 'x' } },
    })
    assert.equal(status, 400)
    assert.ok(body.fieldErrors['max-samples'])
    assert.ok(body.fieldErrors['hack'])
  } finally {
    await closeServer(server)
  }
})

test('destructive tool requires typed confirmation', async () => {
  const { server, base, procs } = await startServer()
  try {
    const missing = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'reset_benchmark', args: {} } })
    assert.equal(missing.status, 400)
    assert.match(missing.body.error, /confirmation required/i)

    const wrong = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'reset_benchmark', args: {}, confirm: 'resat' },
    })
    assert.equal(wrong.status, 400)

    const ok = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'reset_benchmark', args: {}, confirm: 'RESET' },
    })
    assert.equal(ok.status, 201)
    assert.equal(ok.body.job.toolId, 'reset_benchmark')
    assert.equal(procs[0].stdin.ended, 'y\n', 'stdin feeds y\n after confirm')
  } finally {
    await closeServer(server)
  }
})

test('/api/registry exposes grouped tools', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/registry')
    assert.equal(status, 200)
    const ids = body.tools.map(t => t.id)
    for (const expected of ['build_rubrics', 'build_bench', 'corpus_report', 'add_candidate', 'gen_profile_literal', 'snapshot_catalog', 'run_candidate', 'judge_existing', 'agent', 'leaderboard', 'reset_benchmark']) {
      assert.ok(ids.includes(expected), `${expected} in registry`)
    }
    const reset = body.tools.find(t => t.id === 'reset_benchmark')
    assert.equal(reset.destructive, true)
    assert.equal(reset.confirmPhrase, 'RESET')
    const run = body.tools.find(t => t.id === 'run_candidate')
    assert.ok(run.presets.length > 0)
  } finally {
    await closeServer(server)
  }
})

// ── queue / serialization ─────────────────────────────────────

test('llm jobs serialize FIFO — second one queues until first finishes', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const b = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'judge_existing', args: { bench: 'chapter_fast', 'judge-model': 'openai/gpt-4o' } } })
    assert.equal(a.status, 201)
    assert.equal(b.status, 201)
    assert.equal(b.body.job.status, 'queued')
    assert.equal(procs.length, 1, 'only first job spawned')

    procs[0].exit(0)
    assert.equal(procs.length, 2, 'second job starts after first completes')
    assert.equal(procs[1].argv[0], 'core/judge_existing.py')

    procs[1].exit(0)
    const { body } = await call(base, `/api/jobs/${b.body.job.id}`)
    assert.equal(body.job.status, 'succeeded')
  } finally {
    await closeServer(server)
  }
})

test('instant jobs bypass the llm lock', async () => {
  const { server, base, procs } = await startServer()
  try {
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const lb = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'leaderboard', args: { top: 5 } } })
    assert.equal(lb.status, 201)
    assert.equal(procs.length, 2, 'leaderboard runs while run_candidate is active')
    assert.equal(procs[1].argv[0], 'tools/leaderboard.py')
  } finally {
    await closeServer(server)
  }
})

test('duplicate queued job returns existing id; running duplicate is 409', async () => {
  const { server, base, procs } = await startServer()
  try {
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const dupRunning = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    assert.equal(dupRunning.status, 409)
    assert.match(dupRunning.body.error, /already running/)

    const c = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'judge_existing', args: { bench: 'chapter_fast', 'judge-model': 'openai/gpt-4o' } } })
    assert.equal(c.body.job.status, 'queued')
    const dupQueued = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'judge_existing', args: { bench: 'chapter_fast', 'judge-model': 'openai/gpt-4o' } } })
    assert.equal(dupQueued.status, 200)
    assert.equal(dupQueued.body.job.id, c.body.job.id)
    assert.equal(dupQueued.body.duplicate, true)
    assert.equal(procs.length, 1)
  } finally {
    await closeServer(server)
  }
})

test('queue full rejections use 409', async () => {
  const { server, base } = await startServer({ jobLimits: { ...JOB_LIMITS, maxQueue: 1 } })
  try {
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'judge_existing', args: { bench: 'chapter_fast', 'judge-model': 'openai/gpt-4o' } } })
    const third = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'agent', args: { mode: 'auto' } } })
    assert.equal(third.status, 409)
    assert.match(third.body.error, /queue full/)
  } finally {
    await closeServer(server)
  }
})

// ── cancel ────────────────────────────────────────────────────

test('cancel removes a queued job before it starts', async () => {
  const { server, base, procs } = await startServer()
  try {
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const b = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'judge_existing', args: { bench: 'chapter_fast', 'judge-model': 'openai/gpt-4o' } } })
    const r = await call(base, `/api/jobs/${b.body.job.id}/cancel`, { method: 'POST' })
    assert.equal(r.status, 200)
    const detail = await call(base, `/api/jobs/${b.body.job.id}`)
    assert.equal(detail.body.job.status, 'canceled')
    assert.equal(procs.length, 1, 'canceled job never spawned')
    procs[0].exit(0)
  } finally {
    await closeServer(server)
  }
})

test('cancel running sends SIGTERM then cancelled on exit', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const r = await call(base, `/api/jobs/${a.body.job.id}/cancel`, { method: 'POST' })
    assert.equal(r.status, 200)
    assert.equal(procs[0].killSignal, 'SIGTERM')
    procs[0].exit(null, 'SIGTERM')
    const detail = await call(base, `/api/jobs/${a.body.job.id}`)
    assert.equal(detail.body.job.status, 'canceled')
    assert.equal(detail.body.job.error, 'killed')

    const again = await call(base, `/api/jobs/${a.body.job.id}/cancel`, { method: 'POST' })
    assert.equal(again.status, 409)
    assert.match(again.body.error, /already finished/)
  } finally {
    await closeServer(server)
  }
})

// ── SSE ───────────────────────────────────────────────────────

test('SSE streams start/log/status events in order then closes', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const id = a.body.job.id
    const res = await fetch(`${base}/api/jobs/${id}/stream`, { headers: { Accept: 'text/event-stream' } })
    assert.equal(res.status, 200)
    assert.equal(res.headers.get('content-type'), 'text/event-stream')

    procs[0].write('stdout', 'Preparing run...\nRun ID: 20260905t000000z__chapter_fast__mock\n')
    procs[0].write('stderr', 'warning line\n')
    procs[0].exit(0, null)

    const text = await streamText(res)
    const startIdx = text.indexOf('event: start')
    const logIdx = text.indexOf('event: log')
    const statusIdx = text.indexOf('event: status')
    assert.ok(startIdx >= 0, 'start event present')
    assert.ok(logIdx > startIdx, 'log after start')
    assert.ok(statusIdx > logIdx, 'status last')
    assert.match(text, /"stream":"stdout"/)
    assert.match(text, /"stream":"stderr"/)
    assert.match(text, /Run ID: 20260905t000000z__chapter_fast__mock/)
    assert.match(text, /"status":"succeeded"/)
    assert.ok(text.includes('id: 0') && text.includes('id: 1'), 'event ids present')
  } finally {
    await closeServer(server)
  }
})

test('Last-Event-ID replays from the requested sequence', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    const id = a.body.job.id
    procs[0].write('stdout', 'one\n')
    procs[0].write('stdout', 'two\n')
    procs[0].exit(0, null)

    const replay = await fetch(`${base}/api/jobs/${id}/stream`, { headers: { 'Last-Event-ID': '1' } })
    const text = await streamText(replay)
    assert.ok(text.includes('event: status'), 'replays status after seq 1')
    assert.ok(!text.includes('event: start'), 'does not replay start')
    assert.ok(!text.includes('event: log'), 'does not replay log')

    const expired = await fetch(`${base}/api/jobs/${id}/stream`, { headers: { 'Last-Event-ID': '99' } })
    assert.equal(expired.status, 410)
  } finally {
    await closeServer(server)
  }
})

test('partial line is flushed on process exit', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    procs[0].write('stdout', 'no trailing newline')
    procs[0].exit(0, null)
    const detail = await call(base, `/api/jobs/${a.body.job.id}`)
    assert.equal(detail.body.job.status, 'succeeded')
    const log = readFileSync(detail.body.job.logPath, 'utf-8')
    assert.ok(log.includes('no trailing newline'))
  } finally {
    await closeServer(server)
  }
})

// ── log files ─────────────────────────────────────────────────

test('log file is written and truncated with marker under the cap', async () => {
  const { server, base, procs } = await startServer({ jobLimits: { ...JOB_LIMITS, logCapBytes: 120 } })
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    for (let i = 0; i < 30; i++) procs[0].write('stdout', `chunk-line-${i} padding padding padding\n`)
    procs[0].exit(0, null)
    const detail = await call(base, `/api/jobs/${a.body.job.id}`)
    const log = readFileSync(detail.body.job.logPath, 'utf-8')
    assert.ok(log.includes('[log truncated]'), 'truncation marker present')
    assert.ok(log.includes('chunk-line-29'), 'tail retained')
  } finally {
    await closeServer(server)
  }
})

test('secrets are scrubbed from the log file', async () => {
  const { server, base, procs } = await startServer()
  try {
    const a = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    procs[0].write('stdout', 'key sk-ABCDEFGHIJKLMNOPQRSTUV leak\n')
    procs[0].write('stdout', 'OPENROUTER_API_KEY=supersecretvalue\n')
    procs[0].exit(0, null)
    const detail = await call(base, `/api/jobs/${a.body.job.id}`)
    const log = readFileSync(detail.body.job.logPath, 'utf-8')
    assert.ok(!log.includes('ABCDEFGHIJKLMNOPQRSTUV'))
    assert.ok(!log.includes('supersecretvalue'))
    assert.ok(log.includes('[REDACTED]'))
  } finally {
    await closeServer(server)
  }
})

// ── env / bench / path confinement ───────────────────────────

test('/api/env-check returns missing keys only (presence, never values)', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/env-check')
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.ok(Array.isArray(body.missingKeys))
    for (const key of ['OPENROUTER_API_KEY', 'OPENROUTER_MANAGEMENT_KEY', 'GOOGLE_BOOKS_API_KEY']) {
      assert.equal(body.missingKeys.includes(key), !process.env[key], `${key} missing iff unset`)
    }
    assert.ok(!body.missingKeys.some(k => String(k).includes('=')))
  } finally {
    await closeServer(server)
  }
})

test('GET /bench-list derives benches from bench/ and runs/', async () => {
  const { server, base, jobManager } = await startServer()
  try {
    mkdirSync(join(tmp, 'runs', 'booksum-v4'), { recursive: true })
    writeFileSync(join(tmp, 'bench', 'mock.jsonl'), '')
    const { status, body } = await call(base, '/bench-list')
    assert.equal(status, 200)
    assert.ok(body.benches.includes('chapter_fast'))
    assert.ok(body.benches.includes('mock'))
    assert.ok(body.benches.includes('booksum-v4'))
  } finally {
    await closeServer(server)
  }
})

test('path confinement rejects escaping bench values', async () => {
  const { server, base } = await startServer()
  try {
    const esc = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'run_candidate', args: { ...runArgs(), bench: '../etc/passwd' } },
    })
    assert.equal(esc.status, 400)
    assert.ok(esc.body.fieldErrors.bench)

    const ok = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'run_candidate', args: { ...runArgs(), bench: 'bench/chapter_fast.jsonl' } },
    })
    assert.equal(ok.status, 201)
  } finally {
    await closeServer(server)
  }
})

test('script-not-found fails fast with a failed job', async () => {
  const { server, base } = await startServer({ verifyScripts: true })
  try {
    const r = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    assert.equal(r.status, 201)
    assert.equal(r.body.job.status, 'failed')
    assert.match(r.body.job.error, /script not found/)
  } finally {
    await closeServer(server)
  }
})

// ── judge-model toggle ───────────────────────────────────────

test('registry run_candidate judge-model is an optional toggle defaulting to gpt-5.4-mini', async () => {
  const { server, base } = await startServer()
  try {
    const { body } = await call(base, '/api/registry')
    const run = body.tools.find(t => t.id === 'run_candidate')
    const judge = run.args.find(a => a.name === 'judge-model')
    assert.ok(judge, 'judge-model arg present')
    assert.equal(judge.toggle, true, 'rendered as checkbox toggle')
    assert.equal(judge.default, 'openai/gpt-5.4-mini', 'default judge model')
    assert.equal(judge.required, false, 'judge is optional on run_candidate')
  } finally {
    await closeServer(server)
  }
})

test('run_candidate without judge-model spawns without --judge-model', async () => {
  const { server, base, procs } = await startServer()
  try {
    const r = await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    assert.equal(r.status, 201)
    assert.ok(!procs[0].argv.includes('--judge-model'), 'argv omits judge flag for deterministic run')
    assert.ok(!procs[0].argv.includes('openai'), 'no stray judge arg')
    assert.equal(procs[0].opts.env.PYTHONUNBUFFERED, '1', 'job stdout unbuffered so the live log streams promptly')
    assert.ok(procs[0].opts.env.OPENROUTER_API_KEY, 'inherits process env')
  } finally {
    await closeServer(server)
  }
})

test('run_candidate with judge-model passes it and validates the pattern', async () => {
  const { server, base, procs } = await startServer()
  try {
    const ok = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'run_candidate', args: { ...runArgs(), 'judge-model': 'openai/gpt-5.4-mini' } },
    })
    assert.equal(ok.status, 201)
    const i = procs[0].argv.indexOf('--judge-model')
    assert.ok(i >= 0, 'judge flag present')
    assert.equal(procs[0].argv[i + 1], 'openai/gpt-5.4-mini')

    const bad = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'run_candidate', args: { ...runArgs(), 'judge-model': 'has spaces' } },
    })
    assert.equal(bad.status, 400)
    assert.ok(bad.body.fieldErrors['judge-model'])
  } finally {
    await closeServer(server)
  }
})

test('run_candidate spawn processes are torn down on server shutdown (no orphan runs)', async () => {
  const { server, base, procs } = await startServer()
  try {
    await call(base, '/api/jobs', { method: 'POST', body: { toolId: 'run_candidate', args: runArgs() } })
    assert.equal(procs.length, 1)
    const child = procs[0]
    assert.equal(child.killSignal, null)
    killAllChildren()
    assert.equal(child.killSignal, 'SIGTERM', 'owned child gets SIGTERM on shutdown')
  } finally {
    liveChildren.clear()
    await closeServer(server)
  }
})

test('add_candidate runs a follow-up gen_profile_literal regen on success', async () => {
  let regenCalled = false
  const { server, base, procs } = await startServer({
    pythonRunner: async (args) => {
      if (args[0] === 'tools/gen_profile_literal.py') regenCalled = true
      return { ok: true, stdout: 'regen\n', stderr: '' }
    },
  })
  try {
    const a = await call(base, '/api/jobs', {
      method: 'POST', body: { toolId: 'add_candidate', args: { 'model-full': 'newcorp/newmodel', 'time-budget': ['30m'] } },
    })
    procs[0].exit(0, null)
    let detail = { body: { job: { status: 'running' } } }
    for (let i = 0; i < 50 && detail.body.job.status === 'running'; i++) {
      await new Promise(r => setTimeout(r, 10))
      detail = await call(base, `/api/jobs/${a.body.job.id}`)
    }
    assert.equal(regenCalled, true, 'regen invoked after add_candidate success')
    assert.equal(detail.body.job.status, 'succeeded')
    assert.equal(detail.body.job.resultHints.specPyChanged, true)
  } finally {
    await closeServer(server)
  }
})