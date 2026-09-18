import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { scanRequestHandler, makeCtx } from '../vite.config.js'

let tmp
let registryPath
let sweepDir
let jobsDir

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), 'prompts-api-'))
  registryPath = join(tmp, 'registry.json')
  sweepDir = join(tmp, 'base_sweep')
  jobsDir = join(tmp, 'jobs')
  mkdirSync(sweepDir, { recursive: true })
  mkdirSync(jobsDir, { recursive: true })
  process.env.PROMPT_REGISTRY_PATH = registryPath
  process.env.BASE_SWEEP_ARTIFACT_DIR = sweepDir
  process.env.BASE_SWEEP_JOBS_DIR = jobsDir
})

afterEach(() => {
  delete process.env.PROMPT_REGISTRY_PATH
  delete process.env.BASE_SWEEP_ARTIFACT_DIR
  delete process.env.BASE_SWEEP_JOBS_DIR
  rmSync(tmp, { recursive: true, force: true })
})

function writeSweepJob(jobId, { profile, bench, createdAt }) {
  writeFileSync(join(jobsDir, `${jobId}.log`),
    `[job] meta: {"kind":"meta","jobId":"${jobId}","toolId":"sweep_base_prompts","createdAt":"${createdAt}"}\n`
    + `  $ python core/run_candidate.py --bench ${bench} --profile ${profile}\n`)
}

function writeComparison(stem, { profile, bench, generatedAt, jobId = '' }) {
  writeFileSync(join(sweepDir, `${stem}.json`), JSON.stringify({
    profile, bench, generated_at: generatedAt, job_id: jobId, winner: 'sentence',
    rows: [{ slug: 'sentence', length_score: 0.9 }],
  }))
  writeFileSync(join(sweepDir, `${stem}.md`), `# Summary for ${profile}\n`)
}

async function startServer() {
  const ctx = makeCtx({}) // default pythonRunner runs tools/prompts_api.py from the repo root
  const server = http.createServer(scanRequestHandler(ctx))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  return { server, base }
}

async function closeServer(server) {
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

test('GET /api/prompts returns registry shape with production status', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/prompts')
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.ok(body.registry_sha)
    const ids = body.versions.map(v => v.id).sort()
    assert.deepEqual(ids, ['chapter-p1', 'chapter-p2', 'composer-p1'])
    assert.deepEqual(body.slots.chapter.general, { local: 'chapter-p1', production: 'chapter-p1' })
    assert.deepEqual(body.slots.chapter.models, {})
    assert.equal(body.production_status.length, 2)
    assert.equal(body.production_status[0].never_reported, true)
  } finally {
    await closeServer(server)
  }
})

test('GET /api/prompts/options exposes component dimensions', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/prompts/options')
    assert.equal(status, 200)
    assert.ok(body.dimensions.length_policy.includes('explicit_budget_first'))
    assert.ok(body.stage_keys.composer.includes('length_policy'))
  } finally {
    await closeServer(server)
  }
})

test('slot save round-trips and stale writes are rejected', async () => {
  const { server, base } = await startServer()
  try {
    const list = await call(base, '/api/prompts')
    const save = await call(base, '/api/prompts/slots', {
      method: 'POST',
      body: { stage: 'chapter', slot: 'local', version_id: 'chapter-p2', expected_sha: list.body.registry_sha },
    })
    assert.equal(save.status, 200)
    assert.equal(save.body.slots.chapter.general.local, 'chapter-p2')

    const after = await call(base, '/api/prompts')
    assert.equal(after.body.slots.chapter.general.local, 'chapter-p2')

    const stale = await call(base, '/api/prompts/slots', {
      method: 'POST',
      body: { stage: 'chapter', slot: 'local', version_id: 'chapter-p1', expected_sha: list.body.registry_sha },
    })
    assert.equal(stale.status, 409)
    assert.equal(stale.body.error, 'registry_changed')
  } finally {
    await closeServer(server)
  }
})

test('comparison runs list newest-first and expose their summary', async () => {
  writeComparison('old_run', { profile: '30m_a', bench: 'bench-v1', generatedAt: '2026-01-01T00:00:00Z' })
  writeComparison('new_run', {
    profile: '30m_b', bench: 'bench-v2', generatedAt: '2026-02-01T00:00:00Z', jobId: 'job-xyz',
  })

  const { server, base } = await startServer()
  try {
    const list = await call(base, '/api/prompts/comparisons')
    assert.equal(list.status, 200)
    assert.deepEqual(list.body.comparisons.map(c => c.id), ['new_run', 'old_run'])
    assert.equal(list.body.comparisons[0].job_id, 'job-xyz')
    assert.equal(list.body.comparisons[1].has_log, false)

    const one = await call(base, '/api/prompts/comparisons/new_run')
    assert.equal(one.status, 200)
    assert.equal(one.body.summary.profile, '30m_b')
    assert.match(one.body.markdown, /Summary for 30m_b/)

    const missing = await call(base, '/api/prompts/comparisons/nope')
    assert.equal(missing.status, 404)

    const index = await call(base, '/api/prompts')
    assert.deepEqual(index.body.comparisons.map(c => c.id), ['new_run', 'old_run'])
  } finally {
    await closeServer(server)
  }
})

test('a sweep artifact with no recorded job id is resolved to its job log', async () => {
  const jobId = 'abc12345-0000-0000-0000-000000000000'
  writeSweepJob(jobId, { profile: '30m_b', bench: 'bench-v2', createdAt: '2026-02-01T00:00:00.000Z' })
  writeComparison('resolved_run', { profile: '30m_b', bench: 'bench-v2', generatedAt: '2026-02-01T00:03:00Z' })
  writeComparison('other_run', { profile: '30m_c', bench: 'bench-v9', generatedAt: '2026-02-01T00:03:00Z' })

  const { server, base } = await startServer()
  try {
    const list = await call(base, '/api/prompts/comparisons')
    const resolved = list.body.comparisons.find(c => c.id === 'resolved_run')
    assert.equal(resolved.job_id, jobId)
    assert.equal(resolved.log_resolved, true)
    assert.equal(resolved.has_log, true)
    const unmatched = list.body.comparisons.find(c => c.id === 'other_run')
    assert.equal(unmatched.job_id, '')
    assert.equal(unmatched.has_log, false)

    const detail = await call(base, '/api/prompts/comparisons/resolved_run')
    assert.equal(detail.body.summary.job_id, jobId)
  } finally {
    await closeServer(server)
  }
})

test('unknown version slot write is rejected and in-use delete returns 409', async () => {
  const { server, base } = await startServer()
  try {
    const unknown = await call(base, '/api/prompts/slots', {
      method: 'POST',
      body: { stage: 'chapter', slot: 'local', version_id: 'chapter-nope' },
    })
    assert.equal(unknown.status, 404)

    const del = await call(base, '/api/prompts/chapter-p1', { method: 'DELETE' })
    assert.equal(del.status, 409)
    assert.equal(del.body.ok, false)
  } finally {
    await closeServer(server)
  }
})
