import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { scanRequestHandler, makeCtx } from '../vite.config.js'

// ── fixtures ──────────────────────────────────────────────────

function stage(model, temp, maxT, thinking) {
  return {
    model,
    temperature: temp,
    seed: 42,
    max_tokens: maxT,
    format_mode: 'markdown_sections',
    context_mode: 'chapter_plus_toc_and_meta',
    prompt_components: {
      system_style: 'dense_faithful',
      detail_policy: 'mechanisms_first',
    },
    extra_body: { thinking: { type: thinking ? 'enabled' : 'disabled' } },
    use_json_schema: true,
  }
}

function p(slug, model, temp, maxT, provider) {
  const thinking = slug.endsWith('_thinking')
  const cs = stage(model, temp, maxT, thinking)
  const comp = stage(model, temp, maxT, thinking)
  if (provider) { cs.provider = provider; comp.provider = provider }
  return {
    name: `${slug}_v1`,
    profile: slug,
    chapter_stage: cs,
    composer_stage: comp,
    composer_mode: 'ladder',
    length_control: 'max',
    budget_allocator: 'fixed',
    disable_composer: false,
    notes: 'n',
  }
}

function baseCandidates() {
  return {
    version: 2,
    profiles: {
      '30m_deepseek-v4-flash_thinking': p('30m_deepseek-v4-flash_thinking', 'deepseek/deepseek-v4-flash', 0.2, 8192),
      '60m_deepseek-v4-flash_thinking': p('60m_deepseek-v4-flash_thinking', 'deepseek/deepseek-v4-flash', 0.2, 8192),
      '30m_deepseek-v4-flash_notthinking': p('30m_deepseek-v4-flash_notthinking', 'deepseek/deepseek-v4-flash', 0.0, 8192, { order: ['deepseek'] }),
      '60m_qwen3.6-plus_thinking': p('60m_qwen3.6-plus_thinking', 'qwen/qwen3.6-plus', 0.4, 4096),
    },
  }
}

const CONFIRM_JSON = 'output_format tanl; typography minimal; distilled dense; all internal reasoning about source; no editorializing.'

function runFixture(model, quality, judgeVersion, createdAt) {
  return {
    dataset_score: { mean_quality: quality },
    run_manifest: {
      chapter_model: model,
      judge_model: 'litellm:schemaparams',
      judge_version_resolved: judgeVersion,
      created_at_utc: createdAt,
    },
    findings_gathered: { model_output: CONFIRM_JSON },
  }
}

function writeRunsFixture(runsDir) {
  mkdirSync(join(runsDir, 'alpha'), { recursive: true })
  writeFileSync(join(runsDir, 'alpha', 'r1_det.json'), JSON.stringify(runFixture('deepseek/deepseek-v4-flash', 0.712, 'judge-deterministic-llmj-rubric-v1', '2026-05-01T10:00:00Z')))
  writeFileSync(join(runsDir, 'alpha', 'r2_det.json'), JSON.stringify(runFixture('deepseek/deepseek-v4-flash', 0.745, 'judge-deterministic-llmj-rubric-v1', '2026-05-02T10:00:00Z')))
  writeFileSync(join(runsDir, 'alpha', 'r3_llm.json'), JSON.stringify(runFixture('deepseek/deepseek-v4-flash', 0.902, 'litellm://judge-absolute-v1/default', '2026-05-03T10:00:00Z')))
  writeFileSync(join(runsDir, 'alpha', 'other.model.json'), JSON.stringify(runFixture('qwen/qwen3.6-plus', 0.555, 'judge-deterministic-llmj-rubric-v1', '2026-04-20T10:00:00Z')))
}

// ── harness ───────────────────────────────────────────────────

let tmp
let runsDir
let dataDir
let candidatesPath

beforeEach(() => {
  tmp = realpathSync(mkdtempSync(join(tmpdir(), 'mm-api-')))
  runsDir = join(tmp, 'runs')
  dataDir = join(tmp, 'data')
  candidatesPath = join(dataDir, 'candidates.json')
  mkdirSync(dataDir, { recursive: true })
  writeRunsFixture(runsDir)
  writeCandidatesFile(baseCandidates())
})

function writeCandidatesFile(data) {
  writeFileSync(candidatesPath, JSON.stringify(data, null, 2) + '\n')
}

function freshCandidates() {
  return JSON.parse(readFileSync(candidatesPath, 'utf-8'))
}

function readNotes() {
  const p = join(dataDir, 'chapter_notes.jsonl')
  try {
    return readFileSync(p, 'utf-8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l))
  } catch { return [] }
}

function fakeRunner() {
  const calls = []
  const defaultImpl = async (args) => {
    const joined = args.join(' ')
    if (args[0] === 'tools/gen_profile_literal.py') return { ok: true, stdout: 'regenerated\n', stderr: '' }
    if (joined.includes('--dry-run')) {
      return {
        ok: true,
        stdout: [
          'Probing JSON schema support... ✓',
          'Probing thinking mode... ✓',
          'Probing non-thinking mode... [UNSUPPORTED]',
          'Will create profile: 30m_newmodel_thinking',
          'Will create profile: 60m_newmodel_thinking',
        ].join('\n'),
        stderr: '',
      }
    }
    if (joined.includes('--remove')) {
      return {
        ok: true,
        stdout: 'Removing profiles matching deepseek-v4-flash\n  - 30m_deepseek-v4-flash_thinking\n  - 60m_deepseek-v4-flash_thinking\n  - 30m_deepseek-v4-flash_notthinking\nRemoved 3 run file(s)\n',
        stderr: '',
      }
    }
    return {
      ok: true,
      stdout: ['  Added: 30m_newmodel_thinking_v1', '  Added: 30m_newmodel_notthinking_v1'].join('\n'),
      stderr: '',
    }
  }
  let impl = defaultImpl
  const fn = async (args, stdin) => {
    calls.push({ args, stdin })
    return impl(args, stdin)
  }
  return { fn, calls, setImpl(i) { impl = i } }
}

async function startServer(overrides = {}) {
  const runner = fakeRunner()
  const ctx = makeCtx({
    candidatesPath,
    runsDir,
    dataDir,
    pythonRunner: runner.fn,
    autoTag: async (text) => ({ tags: reduce(text), sentiment: 0, source: 'stub' }),
    ...overrides,
  })
  const server = http.createServer(scanRequestHandler(ctx))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  return { server, base, runner }
}

function reduce(text) {
  const lower = text.toLowerCase()
  return lower.includes('structure') ? ['structure'] : []
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

test.afterEach(() => {
  rmSync(tmp, { recursive: true, force: true })
})

// ── GET /api/models ───────────────────────────────────────────

test('GET /api/models indexes models with run aggregation', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models')
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.equal(body.count, 2)
    assert.equal(body.models.length, 2)

    const deepseek = body.models.find(m => m.model === 'deepseek/deepseek-v4-flash')
    assert.ok(deepseek)
    assert.equal(deepseek.runs_count, 3)
    assert.equal(deepseek.last_tested, '2026-05-03T10:00:00Z')
    assert.equal(deepseek.best_quality_det, 0.745)
    assert.equal(deepseek.best_quality_llm, 0.902)
    assert.equal(deepseek.profiles.length, 3)
    assert.ok(deepseek.profiles.every(pf => pf.status === 'tested'))

    const qwen = body.models.find(m => m.model === 'qwen/qwen3.6-plus')
    assert.ok(qwen)
    assert.equal(qwen.runs_count, 1)
    assert.equal(qwen.best_quality_det, 0.555)
    assert.equal(qwen.best_quality_llm, null)
    assert.ok(qwen.profiles.every(pf => pf.status === 'tested'))
  } finally {
    await closeServer(server)
  }
})

test('GET /api/models serializes provider route and pending status', async () => {
  writeCandidatesFile({ version: 2, profiles: { '30m_deepseek-v4-flash_notthinking': p('30m_deepseek-v4-flash_notthinking', 'deepseek/deepseek-v4-flash', 0.0, 8192, { order: ['deepseek'] }) } })
  const { server, base } = await startServer()
  try {
    const { body } = await call(base, '/api/models')
    assert.equal(body.count, 1)
    const m = body.models[0]
    assert.equal(m.runs_count, 3)
    assert.equal(m.profiles[0].status, 'tested')
    assert.equal(m.profiles[0].provider_route, JSON.stringify({ order: ['deepseek'] }))
    assert.equal(m.profiles[0].temperature, 0.0)
    assert.equal(m.profiles[0].max_tokens, 8192)
  } finally {
    await closeServer(server)
  }
})

test('GET /api/models handles empty registry', async () => {
  writeCandidatesFile({ version: 2, profiles: {} })
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models')
    assert.equal(status, 200)
    assert.equal(body.count, 0)
    assert.deepEqual(body.models, [])
  } finally {
    await closeServer(server)
  }
})

// ── POST /api/models/probe ────────────────────────────────────

test('POST probe returns capability flags and created list', async () => {
  const { server, base, runner } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models/probe', {
      method: 'POST',
      body: { model: 'newcorp/newmodel', time_budget: ['30m', '60m'] },
    })
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.equal(body.model, 'newcorp/newmodel')
    assert.equal(body.compatible, true)
    assert.deepEqual(body.probe, { schema: true, thinking: true, notthinking: false })
    assert.deepEqual(body.created, ['30m_newmodel_thinking', '60m_newmodel_thinking'])

    const probeCall = runner.calls.find(c => c.args.includes('--dry-run'))
    assert.ok(probeCall, 'should invoke add_candidate.py --dry-run')
    assert.ok(probeCall.args.includes('tools/add_candidate.py'))
    assert.ok(probeCall.args.includes('--model-full'))
    assert.ok(probeCall.args.includes('--time-budget'))
    assert.ok(probeCall.args.includes('30m'))
    assert.ok(probeCall.args.includes('60m'))
    assert.equal(probeCall.stdin, undefined)
  } finally {
    await closeServer(server)
  }
})

test('POST probe defaults time budgets to 30m+60m', async () => {
  const { server, base, runner } = await startServer()
  try {
    await call(base, '/api/models/probe', { method: 'POST', body: { model: 'x/y' } })
    const probeCall = runner.calls.find(c => c.args.includes('--dry-run'))
    assert.ok(probeCall.args.includes('30m'))
    assert.ok(probeCall.args.includes('60m'))
  } finally {
    await closeServer(server)
  }
})

test('POST probe filters invalid budgets and reports runner failure without 500', async () => {
  const { server, base, runner } = await startServer()
  runner.setImpl(async (args) => {
    if (args.includes('--dry-run')) return { ok: false, stdout: '', stderr: 'boom: no such model', message: 'exec failed' }
    return { ok: true, stdout: '', stderr: '' }
  })
  try {
    const { status, body } = await call(base, '/api/models/probe', {
      method: 'POST',
      body: { model: 'x/y', time_budget: ['30m', '999m', '60m'] },
    })
    assert.equal(status, 200)
    assert.equal(body.ok, false)
    assert.ok(body.error.includes('boom'))
    assert.equal(body.compatible, false)
    assert.deepEqual(body.probe, { schema: null, thinking: null, notthinking: null }, 'no probe output on runner failure')
  } finally {
    await closeServer(server)
  }
})

test('POST probe rejects missing model', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models/probe', { method: 'POST', body: {} })
    assert.equal(status, 400)
    assert.equal(body.ok, false)
    assert.match(body.error, /model is required/)
  } finally {
    await closeServer(server)
  }
})

test('POST probe rejects invalid JSON body', async () => {
  const { server, base } = await startServer()
  try {
    const r = await fetch(`${base}/api/models/probe`, { method: 'POST', body: '{not json' })
    assert.equal(r.status, 400)
  } finally {
    await closeServer(server)
  }
})

// ── POST /api/models (add) ────────────────────────────────────

test('POST /api/models invokes add script and returns added list', async () => {
  const { server, base, runner } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models', {
      method: 'POST',
      body: { model: 'newcorp/newmodel', time_budget: ['30m'] },
    })
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.deepEqual(body.added, ['30m_newmodel_thinking_v1', '30m_newmodel_notthinking_v1'])
    assert.deepEqual(body.skipped, [])
    assert.equal(body.model, 'newcorp/newmodel')

    const addCall = runner.calls.find(c => c.args.includes('--model-full') && c.args.includes('--out'))
    assert.ok(addCall)
    assert.ok(addCall.args.includes('--time-budget'))
    assert.ok(addCall.args.includes('30m'))
    assert.equal(addCall.stdin, 'n\n')

    const genCall = runner.calls.find(c => c.args[0] === 'tools/gen_profile_literal.py')
    assert.ok(genCall, 'should regenerate candidate_spec.py after adding')
  } finally {
    await closeServer(server)
  }
})

test('POST /api/models reports skipped profiles and skips regen', async () => {
  const { server, base, runner } = await startServer()
  runner.setImpl(async (args) => {
    if (args.includes('--model-full')) return { ok: true, stdout: '  Skipped (already exists): 30m_newmodel_thinking_v1\n', stderr: '' }
    return { ok: true, stdout: '', stderr: '' }
  })
  try {
    const { body } = await call(base, '/api/models', { method: 'POST', body: { model: 'x/y' } })
    assert.equal(body.ok, true)
    assert.deepEqual(body.added, [])
    assert.deepEqual(body.skipped, ['30m_newmodel_thinking_v1'])
    assert.ok(!runner.calls.some(c => c.args[0] === 'tools/gen_profile_literal.py'))
  } finally {
    await closeServer(server)
  }
})

test('POST /api/models propagates script failure as 500', async () => {
  const { server, base, runner } = await startServer()
  runner.setImpl(async (args) => {
    if (args.includes('--model-full')) return { ok: false, stdout: '', stderr: 'WRAPPER CRASHED', message: 'EACCES' }
    return { ok: true, stdout: '', stderr: '' }
  })
  try {
    const { status, body } = await call(base, '/api/models', { method: 'POST', body: { model: 'x/y' } })
    assert.equal(status, 500)
    assert.match(body.error, /WRAPPER CRASHED/)
  } finally {
    await closeServer(server)
  }
})

// ── PUT /api/models (edit) ────────────────────────────────────

test('PUT rewrites candidates.json when bumping temperature', async () => {
  const { server, base, runner } = await startServer()
  try {
    const edits = [
      { key: '30m_deepseek-v4-flash_thinking', keep: true, temperature: 0.9, max_tokens: 8192 },
      { key: '60m_deepseek-v4-flash_thinking', keep: true, temperature: 0.2, max_tokens: 8192 },
      { key: '30m_deepseek-v4-flash_notthinking', keep: true, temperature: 0.0, max_tokens: 8192 },
    ]
    const { status, body } = await call(base, '/api/models', {
      method: 'PUT',
      body: { old_model: 'deepseek/deepseek-v4-flash', new_model: 'deepseek/deepseek-v4-flash', edits, create: [] },
    })
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.deepEqual(body.plan.updated, ['30m_deepseek-v4-flash_thinking', '60m_deepseek-v4-flash_thinking', '30m_deepseek-v4-flash_notthinking'])
    assert.deepEqual(body.plan.removed, [])
    assert.equal(body.plan.model, null)

    const onDisk = freshCandidates()
    assert.equal(onDisk.profiles['30m_deepseek-v4-flash_thinking'].chapter_stage.temperature, 0.9)
    assert.equal(onDisk.profiles['30m_deepseek-v4-flash_thinking'].composer_stage.temperature, 0.9)

    const genCall = runner.calls.find(c => c.args[0] === 'tools/gen_profile_literal.py')
    assert.ok(genCall, 'edit must trigger spec regeneration')
  } finally {
    await closeServer(server)
  }
})

test('PUT renames model across all profiles', async () => {
  const { server, base } = await startServer()
  try {
    const edits = ['30m_deepseek-v4-flash_thinking', '60m_deepseek-v4-flash_thinking', '30m_deepseek-v4-flash_notthinking']
      .map(key => ({ key, keep: true, temperature: null, max_tokens: null }))
    const { status, body } = await call(base, '/api/models', {
      method: 'PUT',
      body: { old_model: 'deepseek/deepseek-v4-flash', new_model: 'deepseek-v4-flash-turbo', edits, create: [] },
    })
    assert.equal(status, 200)
    assert.ok(body.plan.renamed.length === 3)

    const onDisk = freshCandidates()
    assert.equal(onDisk.profiles['30m_deepseek-v4-flash_thinking'], undefined)
    const newKey = Object.keys(onDisk.profiles).find(k => k.includes('deepseek-v4-flash-turbo'))
    assert.ok(newKey)
    assert.equal(onDisk.profiles[newKey].chapter_stage.model, 'deepseek-v4-flash-turbo')
    assert.equal(onDisk.profiles[newKey].composer_stage.model, 'deepseek-v4-flash-turbo')
  } finally {
    await closeServer(server)
  }
})

test('PUT creates a missing variant from create list', async () => {
  const { server, base } = await startServer()
  try {
    const edits = [{ key: '60m_qwen3.6-plus_thinking', keep: true, temperature: 0.4, max_tokens: 4096 }]
    const { body } = await call(base, '/api/models', {
      method: 'PUT',
      body: {
        old_model: 'qwen/qwen3.6-plus',
        new_model: 'qwen/qwen3.6-plus',
        edits,
        create: [{ time_budget: '30m', thinking: false }],
      },
    })
    assert.equal(body.ok, true)
    assert.deepEqual(body.plan.created, ['30m_qwen3.6-plus_notthinking'])
    const onDisk = freshCandidates()
    const key = Object.keys(onDisk.profiles).find(k => k === '30m_qwen3.6-plus_notthinking')
    assert.ok(key)
    assert.equal(onDisk.profiles[key].chapter_stage.model, 'qwen/qwen3.6-plus')
    assert.equal(onDisk.profiles[key].chapter_stage.extra_body.thinking.type, 'disabled')
  } finally {
    await closeServer(server)
  }
})

test('PUT removes variants marked keep=false', async () => {
  const { server, base } = await startServer()
  try {
    const edits = [
      { key: '30m_deepseek-v4-flash_thinking', keep: true, temperature: 0.2, max_tokens: 8192 },
      { key: '60m_deepseek-v4-flash_thinking', keep: false },
      { key: '30m_deepseek-v4-flash_notthinking', keep: true, temperature: 0.0, max_tokens: 8192 },
    ]
    const { body } = await call(base, '/api/models', {
      method: 'PUT',
      body: { old_model: 'deepseek/deepseek-v4-flash', new_model: 'deepseek/deepseek-v4-flash', edits, create: [] },
    })
    assert.equal(body.ok, true)
    assert.deepEqual(body.plan.removed, ['60m_deepseek-v4-flash_thinking'])
    const onDisk = freshCandidates()
    assert.equal(onDisk.profiles['60m_deepseek-v4-flash_thinking'], undefined)
    assert.ok(onDisk.profiles['30m_deepseek-v4-flash_thinking'])
  } finally {
    await closeServer(server)
  }
})

test('PUT rejects unknown profile key', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models', {
      method: 'PUT',
      body: { old_model: 'deepseek/deepseek-v4-flash', new_model: 'deepseek/deepseek-v4-flash', edits: [{ key: 'nope_missing_thinking', keep: true }], create: [] },
    })
    assert.equal(status, 400)
    assert.match(body.error, /unknown profile/)
  } finally {
    await closeServer(server)
  }
})

test('PUT rejects overwriting a foreign profile on rename', async () => {
  const { server, base } = await startServer()
  try {
    const edits = ['30m_deepseek-v4-flash_thinking', '60m_deepseek-v4-flash_thinking', '30m_deepseek-v4-flash_notthinking']
      .map(key => ({ key, keep: true, temperature: 0.2, max_tokens: 8192 }))
    // renaming to qwen/qwen3.6-plus would move 60m_deepseek-v4-flash_thinking -> 60m_qwen3.6-plus_thinking,
    // which already exists and belongs to another model
    const { status, body } = await call(base, '/api/models', {
      method: 'PUT',
      body: { old_model: 'deepseek/deepseek-v4-flash', new_model: 'qwen/qwen3.6-plus', edits, create: [] },
    })
    assert.equal(status, 400)
    assert.match(body.error, /would overwrite existing profile/)
  } finally {
    await closeServer(server)
  }
})

// ── DELETE /api/models ────────────────────────────────────────

test('DELETE builds escaped pattern and parses removal output', async () => {
  const { server, base, runner } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models', { method: 'DELETE', body: { model: 'deepseek/deepseek-v4-flash' } })
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.equal(body.pattern, 'deepseek-v4-flash')
    assert.deepEqual(body.removedProfiles, ['30m_deepseek-v4-flash_thinking', '60m_deepseek-v4-flash_thinking', '30m_deepseek-v4-flash_notthinking'])
    assert.equal(body.removedRuns, 3)

    const removeCall = runner.calls.find(c => c.args.includes('--remove'))
    assert.ok(removeCall)
    assert.ok(removeCall.args.includes('--remove'))
    assert.ok(removeCall.args.includes('deepseek-v4-flash'))
  } finally {
    await closeServer(server)
  }
})

test('DELETE rejects missing model', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/models', { method: 'DELETE', body: {} })
    assert.equal(status, 400)
    assert.match(body.error, /model is required/)
  } finally {
    await closeServer(server)
  }
})

// ── notes (uses ctx.autoTag seam) ─────────────────────────────

test('POST /notes writes with stub auto-tag', async () => {
  const { server, base } = await startServer()
  try {
    const res = await fetch(`${base}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'structure feels scattered' }),
    })
    assert.equal(res.status, 201)
    const notes = readNotes()
    assert.equal(notes.length, 1)
    assert.deepEqual(notes[0].tags, ['structure'])
    assert.equal(notes[0].auto_tag_source, 'stub')
    assert.ok(notes[0].id)
    assert.ok(notes[0].timestamp)
  } finally {
    await closeServer(server)
  }
})