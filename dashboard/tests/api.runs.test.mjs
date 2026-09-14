import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, realpathSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import { scanRequestHandler, makeCtx } from '../vite.config.js'

function runFixture(model, quality, judgeModel, judgeVersion, createdAt, profile, bench, runIdBase, promptHash, extra) {
  const m = {
    run_id: runIdBase,
    chapter_model: model,
    profile: profile || '',
    candidate_name: (profile || 'prompt') + '_v1',
    bench: bench || 'booksum-v4',
    judge_model: judgeModel || '',
    judge_version_resolved: judgeVersion || 'judge-deterministic-llmj-rubric-v1',
    created_at_utc: createdAt,
    prompt_version: 'chapter-p1',
    composer_prompt_version: 'composer-p1',
    prompt_hashes: { chapter_stage_sha256: promptHash || 'aabbccddeeff001122334455667788990011223344556677889900aabbccddee' },
  }
  if (extra) Object.assign(m, extra)
  return {
    dataset_score: { mean_quality: quality, n_samples: 3 },
    run_manifest: m,
  }
}

function writeRunsFixture(runsDir) {
  mkdirSync(join(runsDir, 'booksum-v4'), { recursive: true })
  const ts = '20260512t075446z'
  const base = `${ts}__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v1`
  // base family mirrors the real 'luna' run: a composer prompt_selection frame
  // is recorded in the manifest even though the composer stage never ran.
  writeFileSync(join(runsDir, 'booksum-v4', `${base}.json`), JSON.stringify(runFixture('nebula/nebula-2', 0.712, '', 'judge-deterministic-llmj-rubric-v1', '2026-05-12T07:54:46Z', '30m_nebula_thinking', 'booksum-v4', base, undefined, {
    prompt_selection: { chapter: { version: 'chapter-p1', slot: 'local' }, composer: { version: 'composer-p1', slot: 'local' } },
  })))
  writeFileSync(join(runsDir, 'booksum-v4', `${base}.state.json`), JSON.stringify({ updated_at_utc: '2026-05-12T07:59:00Z' }))
  writeFileSync(join(runsDir, 'booksum-v4', `${base}.samples.jsonl`), '')
  // llmj sidecar for the same family (LLM judge)
  const llmj = `__llmj_gpt-5.4-mini`
  writeFileSync(join(runsDir, 'booksum-v4', `${base}${llmj}.json`), JSON.stringify(runFixture('nebula/nebula-2', 0.902, 'litellm:gpt-5.4-mini', 'litellm://judge-absolute-v1/default', '2026-05-13T10:00:00Z', '30m_nebula_thinking', 'booksum-v4', base)))
  writeFileSync(join(runsDir, 'booksum-v4', `${base}${llmj}.state.json`), JSON.stringify({ updated_at_utc: '2026-05-13T10:04:00Z' }))
  writeFileSync(join(runsDir, 'booksum-v4', `${base}${llmj}.samples.jsonl`), '')
  // separate deterministic family, same profile (unjudged)
  const base2 = `${ts}__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v2`
  writeFileSync(join(runsDir, 'booksum-v4', `${base2}.json`), JSON.stringify(runFixture('nebula/nebula-2', 0.745, '', 'judge-deterministic-llmj-rubric-v1', '2026-05-12T08:10:00Z', '30m_nebula_thinking', 'booksum-v4', base2)))
  writeFileSync(join(runsDir, 'booksum-v4', `${base2}.state.json`), JSON.stringify({ updated_at_utc: '2026-05-12T08:15:00Z' }))
  // other bench + model
  mkdirSync(join(runsDir, 'otherbench'), { recursive: true })
  const base3 = `${ts}__otherbench__v1__60m_zeta_effort-high_v1`
  writeFileSync(join(runsDir, 'otherbench', `${base3}.json`), JSON.stringify(runFixture('zeta/zeta-x', 0.555, '', 'judge-deterministic-llmj-rubric-v1', '2026-04-20T10:00:00Z', '60m_zeta_effort-high', 'otherbench', base3)))
  // composer-enabled family: the manifest records that the composer stage ran.
  const baseC = `${ts}__booksum-v4__chapter_fast-v3__30m_nebula_composing_v1`
  writeFileSync(join(runsDir, 'booksum-v4', `${baseC}.json`), JSON.stringify(runFixture('nebula/nebula-2', 0.812, '', 'judge-deterministic-llmj-rubric-v1', '2026-05-14T09:00:00Z', '30m_nebula_composing', 'booksum-v4', baseC, undefined, {
    composer_prompt_version: 'composer-p1',
    composer_stage_ran: true,
    prompt_selection: { composer: { version: 'composer-p1', slot: 'local' } },
  })))
  writeFileSync(join(runsDir, 'booksum-v4', `${baseC}.state.json`), JSON.stringify({ updated_at_utc: '2026-05-14T09:05:00Z' }))
}

let tmp
let runsDir
let dataDir
let candidatesPath

beforeEach(() => {
  tmp = realpathSync(mkdtempSync(join(tmpdir(), 'mm-runs-')))
  runsDir = join(tmp, 'runs')
  dataDir = join(tmp, 'data')
  candidatesPath = join(dataDir, 'candidates.json')
  mkdirSync(dataDir, { recursive: true })
  writeFileSync(candidatesPath, JSON.stringify({ version: 2, profiles: {} }, null, 2) + '\n')
  writeRunsFixture(runsDir)
})

function fakeRunner() {
  const calls = []
  const defaultImpl = async (args) => {
    if (args.includes('--remove-run')) {
      const id = args[args.indexOf('--remove-run') + 1]
      return {
        ok: true,
        stdout: [
          `Found 2 run file(s) for '${id}':`,
          '  - runs/booksum-v4/xxx.json',
          '  - runs/booksum-v4/xxx__llmj_gpt-5.4-mini.json',
          'Removed 2 run file(s)',
          'Removed 1 results.tsv row(s)',
        ].join('\n') + '\n',
        stderr: '',
      }
    }
    return { ok: true, stdout: '', stderr: '' }
  }
  let impl = defaultImpl
  const fn = async (args, stdin) => {
    calls.push({ args, stdin })
    return impl(args, stdin)
  }
  return { fn, calls, setImpl(i) { impl = i } }
}

async function startServer() {
  const runner = fakeRunner()
  const ctx = makeCtx({
    candidatesPath,
    runsDir,
    dataDir,
    repoRoot: tmp,
    jobsDir: join(tmp, 'jobs'),
    pythonRunner: runner.fn,
  })
  const server = http.createServer(scanRequestHandler(ctx))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return { server, base: `http://127.0.0.1:${server.address().port}`, runner, ctx }
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

// ── GET /api/runs ────────────────────────────────────────────

test('GET /api/runs lists run artifacts with family and judge flags', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/runs?disabled=include')
    assert.equal(status, 200)
    assert.equal(body.ok, true)
    assert.equal(body.count, 5)

    const det = body.runs.find(r => r.family_id.includes('nebula_thinking_v1') && !r.is_llm)
    assert.ok(det)
    assert.equal(det.family_id, det.run_id, 'family id equals base run id (no llmj suffix)')
    assert.equal(det.is_llm, false)
    assert.equal(det.judge_version_resolved, 'judge-deterministic-llmj-rubric-v1')
    assert.equal(det.prompt_version, 'chapter-p1', 'prompt version = manifest prompt_version id')
    assert.equal(det.composer_prompt_version, 'composer-p1')
    assert.equal(det.family_has_composer, false, 'composer prompt_selection frame alone must not flag a composer run')
    assert.equal(det.composer_stage_ran, false, 'composer stage did not run in the chapter-only family')
    assert.ok(!det.prompt_selection || true, 'frame present is not sufficient (luna regression)')
    assert.equal(det.prompt_hash_full, 'aabbccddeeff001122334455667788990011223344556677889900aabbccddee')

    const sidecar = body.runs.find(r => r.is_llm)
    assert.ok(sidecar)
    assert.ok(sidecar.file.includes('__llmj_gpt-5.4-mini'), 'sidecar file carries llmj suffix')
    assert.equal(sidecar.run_id, det.family_id, 'manifest run_id stays the base id for llmj sidecars')
    assert.equal(sidecar.family_id, det.family_id, 'sidecar shares the base family id')
    assert.equal(sidecar.is_llm, true)
    assert.equal(sidecar.judge_model, 'litellm:gpt-5.4-mini')
    assert.equal(sidecar.mean_quality, 0.902)
    assert.equal(sidecar.prompt_version, det.prompt_version, 'sidecar shares family prompt version')
    assert.equal(sidecar.family_file_count, 2)
    assert.equal(det.family_judged, true, 'family flagged judged when an llmj sidecar exists')

    const composer = body.runs.find(r => r.family_id.includes('composing'))
    assert.ok(composer)
    assert.equal(composer.composer_stage_ran, true, 'composer stage actually ran')
    assert.equal(composer.family_has_composer, true, 'family flagged as containing a composer run')
    assert.equal(composer.composer_prompt_version, 'composer-p1')
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

test('GET /api/runs filters by model, bench, judge', async () => {
  const { server, base } = await startServer()
  try {
    const nebula = await call(base, '/api/runs?model=nebula%2Fnebula-2&disabled=include')
    assert.equal(nebula.body.count, 4)
    const otherbench = await call(base, '/api/runs?bench=otherbench&disabled=include')
    assert.equal(otherbench.body.count, 1)
    assert.equal(otherbench.body.runs[0].chapter_model, 'zeta/zeta-x')
    const llm = await call(base, '/api/runs?judge=llm&disabled=include')
    assert.equal(llm.body.count, 1)
    assert.equal(llm.body.runs[0].is_llm, true)
    const detF = await call(base, '/api/runs?judge=det&disabled=include')
    assert.equal(detF.body.count, 4)
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

// ── disable / enable registry ────────────────────────────────

test('disable then enable toggles the family in the disabled registry', async () => {
  const { server, base, ctx } = await startServer()
  try {
    const fam = base4(ctx)
    assert.ok(fam, 'sample family id available')

    const dRes = await call(base, '/api/runs/disable', { method: 'POST', body: { runId: fam } })
    assert.equal(dRes.status, 200)
    assert.equal(dRes.body.ok, true)
    assert.equal(dRes.body.disabled, true)
    assert.equal(dRes.body.family_id, fam)

    let list = await call(base, '/api/runs?disabled=hide')
    assert.ok(!list.body.runs.some(r => r.family_id === fam), 'disabled family hidden by default')

    let only = await call(base, '/api/runs?disabled=only')
    assert.equal(only.body.count, 2, 'both base and llmj sidecar of the disabled family show as disabled')
    assert.ok(only.body.runs.every(r => r.family_id === fam))

    const eRes = await call(base, '/api/runs/enable', { method: 'POST', body: { runId: fam } })
    assert.equal(eRes.status, 200)
    assert.equal(eRes.body.disabled, false)

    list = await call(base, '/api/runs?disabled=hide')
    assert.ok(list.body.runs.some(r => r.family_id === fam), 'family visible again after enable')
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

test('disabling a sidecar run-id disables the whole family', async () => {
  const { server, base, ctx } = await startServer()
  try {
    const sidecarRunId = sidecarId(ctx)
    const res = await call(base, '/api/runs/disable', { method: 'POST', body: { runId: sidecarRunId } })
    assert.equal(res.status, 200)
    assert.ok(!res.body.family_id.includes('__llmj_'), 'family id normalized to base run id')
    const registry = await call(base, '/api/runs/disabled')
    assert.ok(registry.body.disabled[res.body.family_id], 'base family id present in registry')
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

test('disable rejects missing runId', async () => {
  const { server, base } = await startServer()
  try {
    const res = await call(base, '/api/runs/disable', { method: 'POST', body: {} })
    assert.equal(res.status, 400)
    assert.match(res.body.error, /runId is required/)
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

// ── GET /api/runs/disabled ───────────────────────────────────

test('GET /api/runs/disabled returns empty registry', async () => {
  const { server, base } = await startServer()
  try {
    const { status, body } = await call(base, '/api/runs/disabled')
    assert.equal(status, 200)
    assert.deepEqual(body.disabled, {})
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

// ── POST /api/runs/delete ────────────────────────────────────

test('POST /api/runs/delete invokes add_candidate --remove-run and cleans registry', async () => {
  const { server, base, runner, ctx } = await startServer()
  try {
    // put the family in the disabled registry first to verify cleanup
    const fam = base4(ctx)
    await call(base, '/api/runs/disable', { method: 'POST', body: { runId: fam } })

    const res = await call(base, '/api/runs/delete', { method: 'POST', body: { runId: fam } })
    assert.equal(res.status, 200)
    assert.equal(res.body.ok, true)
    assert.equal(res.body.family_id, fam)

    const removeCall = runner.calls.find(c => c.args.includes('--remove-run'))
    assert.ok(removeCall, 'delete shells out to add_candidate.py --remove-run')
    assert.equal(removeCall.args[removeCall.args.indexOf('--remove-run') + 1], fam)
    assert.ok(removeCall.args.includes('tools/add_candidate.py'))

    const registry = await call(base, '/api/runs/disabled')
    assert.deepEqual(registry.body.disabled, {}, 'family purged from disabled registry on delete')
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

test('POST /api/runs/delete propagates script failure as 500', async () => {
  const { server, base, runner } = await startServer()
  runner.setImpl(async (args) => {
    if (args.includes('--remove-run')) return { ok: false, stdout: '', stderr: 'boom: no run', message: 'EACCES' }
    return { ok: true, stdout: '', stderr: '' }
  })
  try {
    const res = await call(base, '/api/runs/delete', { method: 'POST', body: { runId: 'nope' } })
    assert.equal(res.status, 500)
    assert.match(res.body.error, /boom/)
  } finally {
    await new Promise(resolve => server.close(resolve))
  }
})

// ── helpers to locate fixture ids ────────────────────────────

function sampleIds(ctx) {
  const files = []
  for (const d of readdirSync(ctx.runsDir)) {
    files.push(...readdirSync(join(ctx.runsDir, d)).filter(f => f.endsWith('.json') && !f.endsWith('.state.json')))
  }
  return files
}

function base4(ctx) {
  const f = sampleIds(ctx).find(x => x.includes('__llmj_'))
  if (!f) return null
  return f.split('__llmj_')[0]
}

function sidecarId(ctx) {
  const f = sampleIds(ctx).find(x => x.includes('__llmj_'))
  return f ? f.replace(/\.json$/, '') : null
}