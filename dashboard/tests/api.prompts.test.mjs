import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { scanRequestHandler, makeCtx } from '../vite.config.js'

let tmp
let registryPath

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), 'prompts-api-'))
  registryPath = join(tmp, 'registry.json')
  process.env.PROMPT_REGISTRY_PATH = registryPath
})

afterEach(() => {
  delete process.env.PROMPT_REGISTRY_PATH
  rmSync(tmp, { recursive: true, force: true })
})

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
    assert.deepEqual(body.slots.chapter, { local: 'chapter-p1', production: 'chapter-p1' })
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
    assert.equal(save.body.slots.chapter.local, 'chapter-p2')

    const after = await call(base, '/api/prompts')
    assert.equal(after.body.slots.chapter.local, 'chapter-p2')

    const stale = await call(base, '/api/prompts/slots', {
      method: 'POST',
      body: { stage: 'chapter', slot: 'local', version_id: 'chapter-p1', expected_sha: list.body.registry_sha },
    })
    assert.equal(stale.status, 409)
    assert.equal(stale.body.error.includes('Registry changed'), true)
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
