import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { JSDOM } from 'jsdom'

const MODELS_PAYLOAD = {
  ok: true,
  count: 2,
  models: [
    {
      model: 'deepseek/deepseek-v4-flash',
      runs_count: 3,
      last_tested: '2026-05-03T10:00:00Z',
      best_quality_det: 0.745,
      best_quality_llm: 0.902,
      profiles: [
        { slug: '30m_deepseek-v4-flash_thinking', time_budget: '30m', thinking: true, status: 'tested', temperature: 0.2, max_tokens: 8192, provider_route: '' },
        { slug: '60m_deepseek-v4-flash_notthinking', time_budget: '60m', thinking: false, status: 'pending', temperature: 0.0, max_tokens: 8192, provider_route: '{"order":["deepseek"]}' },
        { slug: '30m_deepseek-v4-flash_effort-high', time_budget: '30m', thinking: null, status: 'pending', temperature: 0.7, max_tokens: 40960, provider_route: '' },
        { slug: '60m_deepseek-v4-flash_effort-low', time_budget: '60m', thinking: null, status: 'pending', temperature: 0.3, max_tokens: 10240, provider_route: '' },
      ],
    },
    {
      model: 'qwen/qwen3.6-plus',
      runs_count: 1,
      last_tested: null,
      best_quality_det: 0.555,
      best_quality_llm: null,
      profiles: [
        { slug: '60m_qwen3.6-plus_thinking', time_budget: '60m', thinking: true, status: 'tested', temperature: 0.4, max_tokens: 4096, provider_route: '' },
      ],
    },
  ],
}

function json(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => JSON.parse(JSON.stringify(payload)),
  })
}

const RUN_REGISTRY = [
  {
    id: 'run_candidate',
    group: 'run',
    title: 'Run candidate',
    description: 'Run the frozen benchmark harness',
    script: 'core/run_candidate.py',
    runtimeClass: 'llm',
    outputs: ['runs/'],
    args: [
      { name: 'bench', label: 'Benchmark', type: 'text', required: true, bench: true, default: 'chapter_fast' },
      { name: 'profile', label: 'Profile', type: 'text', default: 'all' },
      { name: 'time', label: 'Time budget', type: 'enum', default: 'all', choices: ['all', '30m', '60m'] },
      { name: 'judge-model', label: 'Judge model', type: 'text', toggle: true, default: 'openai/gpt-5.4-mini', placeholder: 'openai/gpt-5.4-mini', pattern: '^[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+$' },
      { name: 'mock', label: 'Mock (no API calls)', type: 'bool', default: false },
      { name: 'max-samples', label: 'Max samples (0 = all)', type: 'int', default: 0, advanced: true },
    ],
    presets: [
      { id: 'smoke', label: 'Smoke', args: { bench: 'chapter_fast', profile: 'all', mock: true, 'max-samples': 4 } },
    ],
  },
  {
    id: 'reset_benchmark',
    group: 'maintenance',
    title: 'Reset benchmark',
    description: 'Contracts runs, results, candidates',
    script: 'reset_benchmark.py',
    runtimeClass: 'write',
    destructive: true,
    confirmPhrase: 'RESET',
    outputs: [],
    args: [],
  },
  {
    id: 'leaderboard',
    group: 'maintenance',
    title: 'Leaderboard',
    description: 'Show scoreboard',
    script: 'tools/leaderboard.py',
    runtimeClass: 'instant',
    outputs: [],
    args: [
      { name: 'top', label: 'Top N', type: 'int', default: 10 },
    ],
  },
]

function makeHarness() {
  const state = {
    calls: [],
    es: [],
    esInstances: [],
    modelsGet: MODELS_PAYLOAD,
    probe: {
      ok: true,
      compatible: true,
      model: 'p/q',
      probe: { schema: true, thinking: true, notthinking: true },
      created: ['30m_q_thinking', '60m_q_thinking'],
    },
    add: { ok: true, added: ['30m_q_thinking_v1', '30m_q_notthinking_v1'], skipped: [], model: 'p/q' },
    put: { ok: true, plan: { removed: [], renamed: [], updated: [], created: [], model: null } },
    putFn: null,
    del: { ok: true, removedProfiles: [], removedRuns: 0 },
    registry: {
      ok: true,
      groups: { corpus: 'Corpus validation', candidates: 'Candidates', run: 'Run harness', maintenance: 'Analysis & maintenance' },
      tools: RUN_REGISTRY,
    },
    env: { ok: true, missingKeys: [] },
    bench: { ok: true, benches: ['chapter_fast'] },
    jobs: {
      ok: true,
      jobs: [
        { id: 'j1', toolId: 'run_candidate', status: 'running', createdAt: '2026-09-05T10:00:00Z', startedAt: '2026-09-05T10:00:01Z', exitCode: null, resultHints: {} },
        { id: 'j2', toolId: 'build_bench', status: 'queued', createdAt: '2026-09-05T10:00:02Z', exitCode: null, resultHints: {} },
      ],
    },
    jobsPost: { ok: true, job: { id: 'j2', toolId: 'build_bench', status: 'queued', createdAt: '2026-09-05T10:00:02Z', exitCode: null, resultHints: {} } },
    cancel: { ok: true, canceled: true },
    clear: { ok: true, removed: 0 },
    jobDetail: () => ({ ok: true, job: { id: 'j3', toolId: 'run_candidate', args: { bench: 'chapter_fast', profile: 'all', mock: true }, status: 'succeeded', createdAt: '2026-09-05T09:00:00Z', exitCode: 0, resultHints: {} } }),
    postGate: null,
    htmlFallback: null,
    ls: new Map(),
    promptReturn: null,
    promptCalls: 0,
    registryFails: 0,
  }

  const dom = new JSDOM(`<!doctype html><html><head></head><body>
    <div class="page-header">
      <div class="header-right"><button data-settings-toggle title="Settings">gear</button></div>
    </div>
  </body></html>`)
  const { window } = dom

  window.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET'
    state.calls.push({
      url,
      method,
      body: opts.body ? JSON.parse(opts.body) : undefined,
    })
    if (url === '/api/models' && method === 'GET') return json(state.modelsGet)
    if (url === '/api/models/probe') return json(state.probe)
    if (url === '/api/models' && method === 'POST') return json(state.add)
    if (url === '/api/models' && method === 'PUT') {
      if (typeof state.putFn === 'function') {
        const r = state.putFn(opts, state)
        if (r) return r
      }
      return json(state.put)
    }
    if (url === '/api/models' && method === 'DELETE') return json(state.del)
    if (url === '/api/registry') {
      if (state.registryFails > 0) {
        state.registryFails--
        return json({ ok: false, error: 'not found' }, 404)
      }
      return json(state.registry)
    }
    if (url === '/api/env-check') return json(state.env)
    if (state.htmlFallback && url === state.htmlFallback) {
      return Promise.resolve({
        ok: false,
        status: 200,
        headers: { get: () => 'text/html' },
        json: async () => { throw new Error('not JSON') },
      })
    }
    if (url === '/bench-list') return json(state.bench)
    if (url.split('?')[0] === '/api/jobs' && method === 'GET') return json(state.jobs)
    if (url === '/api/jobs' && method === 'POST') {
      return state.postGate ? state.postGate.then(() => json(state.jobsPost)) : json(state.jobsPost)
    }
    if (url === '/api/jobs' && method === 'DELETE') return json(state.clear)
    if (method === 'POST' && /^\/api\/jobs\/[^/]+\/cancel$/.test(url)) return json(state.cancel)
    const detailMatch = url.match(/^\/api\/jobs\/([^/]+)$/)
    if (method === 'GET' && detailMatch) return json(state.jobDetail(detailMatch[1]))
    throw new Error(`unexpected fetch ${method} ${url}`)
  }

  window.EventSource = class {
    constructor(url) {
      this.url = url
      this.listeners = {}
      this.closed = false
      state.es.push(url)
      state.esInstances.push(this)
    }
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn)
    }
    emit(type, data) {
      for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) })
    }
    close() {
      this.closed = true
    }
  }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  window.prompt = (msg, def) => {
    state.promptCalls++
    return state.promptReturn
  }
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k) => state.ls.get(k) ?? null,
      setItem: (k, v) => state.ls.set(k, String(v)),
      removeItem: (k) => state.ls.delete(k),
    },
  })

  const doc = window.document
  const postRender = () => {
    const d = doc
    return {
      overlay: d.getElementById('settingsOverlay'),
      dialog: d.getElementById('modelDialogOverlay'),
      list: d.getElementById('modelsList'),
      addBtn: d.getElementById('modelAddBtn'),
      create: d.getElementById('dlgCreate'),
      modelInput: d.getElementById('dlgModel'),
      routeInput: d.getElementById('dlgProviderRoute'),
      tb30: d.getElementById('tb30'),
      tb60: d.getElementById('tb60'),
      preview: d.getElementById('dlgPreview'),
      error: d.getElementById('dlgError'),
      rows: () => Array.from(d.querySelectorAll('#dlgVariantRows tr')),
      close: d.getElementById('settingsClose'),
      dlgClose: d.getElementById('dlgClose'),
      dlgCancel: d.getElementById('dlgCancel'),
    }
  }

  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }))
  const setValue = (el, value) => {
    el.value = value
    el.dispatchEvent(new window.Event('input', { bubbles: true }))
  }

  return { window, doc, state, click, setValue, postRender }
}

let h
beforeEach(async () => {
  h = makeHarness()
  // wire globals before the (cached) module first evaluates / re-inits
  global.window = h.window
  global.document = h.window.document
  Object.defineProperty(global, 'navigator', {
    configurable: true,
    get: () => h.window.navigator,
  })
  global.fetch = h.window.fetch
  global.HTMLElement = h.window.HTMLElement
  global.HTMLInputElement = h.window.HTMLInputElement
  global.Node = h.window.Node
  global.NodeList = h.window.NodeList
  global.MouseEvent = h.window.MouseEvent
  global.Event = h.window.Event
  global.KeyboardEvent = h.window.KeyboardEvent
  // fresh module instance per test (query string busts the ESM cache) so initSettings
  // re-runs against this test's own DOM; do it AFTER globals are wired
  const specifier = `../settings.js?test=${Math.random().toString(36).slice(2)}`
  h.mod = await import(specifier)
  // modal must exist synchronously after import
  if (!h.doc.getElementById('settingsOverlay')) {
    throw new Error('initSettings did not create the settings modal on import')
  }
  // ensure closed state between tests
  h.postRender().overlay.classList.add('cm-hidden')
  h.postRender().dialog.classList.add('cm-hidden')
  h.state.calls.length = 0
})

// ── open / render ────────────────────────────────────────────

test('gear button opens settings and renders model cards', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  const els = postRender()
  assert.ok(!els.overlay.classList.contains('cm-hidden'), 'overlay visible')
  await new Promise(r => setImmediate(r))
  const cards = els.list.querySelectorAll('.model-card')
  assert.equal(cards.length, 2)
  assert.match(els.list.textContent, /deepseek\/deepseek-v4-flash/)
  assert.match(els.list.textContent, /qwen\/qwen3.6-plus/)
  const getCalls = h.state.calls.filter(c => c.method === 'GET' && c.url === '/api/models')
  assert.ok(getCalls.length >= 1, 'models fetched on open')
})

// ── add-model dialog ─────────────────────────────────────────

test('add dialog opens, create disabled until model id typed', async () => {
  const { doc, click, setValue, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  assert.ok(!els.dialog.classList.contains('cm-hidden'), 'dialog visible')
  assert.equal(els.modelInput.readOnly, false)
  assert.equal(els.create.disabled, true)
  assert.equal(els.create.textContent.trim(), 'Probe & preview')

  setValue(els.modelInput, 'plain-name')
  assert.equal(els.create.disabled, true, 'requires slash separation')
  setValue(els.modelInput, 'prov/model-x')
  assert.equal(els.create.disabled, false)
})

test('probe click hits /api/models/probe and reveals preview with confirm', async () => {
  const { doc, click, setValue, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  setValue(els.modelInput, 'prov/model-x')
  click(els.create)

  await new Promise(r => setImmediate(r))
  const probeCall = state.calls.find(c => c.method === 'POST' && c.url === '/api/models/probe')
  assert.ok(probeCall, 'probe called')
  assert.deepEqual(probeCall.body, { model: 'prov/model-x', time_budget: ['30m', '60m'] })

  assert.ok(!els.preview.classList.contains('cm-hidden'))
  assert.ok(els.preview.querySelector('.probe-ok'))
  assert.match(els.create.textContent, /Create 2 profiles/)
  assert.ok(els.create.classList.contains('dlg-btn-confirm'))
  assert.equal(els.create.disabled, false)
})

test('probe incompatibility disables create and shows error', async () => {
  const { doc, click, setValue, postRender, state } = h
  state.probe = { ok: true, compatible: false, model: 'x/y', probe: { schema: false, thinking: true, notthinking: false }, created: [], error: 'incompatible harness' }
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  setValue(els.modelInput, 'x/y')
  click(els.create)
  await new Promise(r => setImmediate(r))
  assert.ok(!els.error.classList.contains('cm-hidden'))
  assert.match(els.error.textContent, /incompatible/i)
  assert.equal(els.create.classList.contains('dlg-btn-confirm'), false)
})

test('confirm add posts /api/models then closes and refreshes', async () => {
  const { doc, click, setValue, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  setValue(els.modelInput, 'prov/model-x')
  click(els.create)
  await new Promise(r => setImmediate(r))
  click(els.create)
  await new Promise(r => setImmediate(r))

  const addCall = state.calls.find(c => c.method === 'POST' && c.url === '/api/models')
  assert.ok(addCall, 'add posted')
  assert.deepEqual(addCall.body, { model: 'prov/model-x', time_budget: ['30m', '60m'] })
  assert.ok(els.dialog.classList.contains('cm-hidden'), 'dialog closed after add')
  const getAfterAdd = state.calls.filter(c => c.method === 'GET' && c.url === '/api/models').length
  assert.ok(getAfterAdd >= 2, 'model list refreshed')
})

test('provider route is parsed and included in add payload', async () => {
  const { doc, click, setValue, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  setValue(els.modelInput, 'prov/model-x')
  setValue(els.routeInput, '{ "order": ["qwen"] }')
  click(els.create)
  await new Promise(r => setImmediate(r))
  click(els.create)
  await new Promise(r => setImmediate(r))
  const addCall = state.calls.find(c => c.method === 'POST' && c.url === '/api/models')
  assert.deepEqual(addCall.body.provider_route, { order: ['qwen'] })
})

test('invalid route JSON blocks probe in add mode', async () => {
  const { doc, click, setValue, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  click(postRender().addBtn)
  const els = postRender()
  setValue(els.modelInput, 'prov/model-x')
  setValue(els.routeInput, '{not json')
  click(els.create)
  await new Promise(r => setImmediate(r))
  assert.ok(!els.error.classList.contains('cm-hidden'))
  assert.match(els.error.textContent, /valid JSON/)
  assert.ok(!state.calls.some(c => c.url === '/api/models/probe'), 'probe must not fire')
})

// ── edit-model dialog ────────────────────────────────────────

test('edit dialog reflects existing profiles as checked rows', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  const firstCard = els.list.querySelector('.model-card')
  click(firstCard.querySelector('[data-action="edit"]'))
  await new Promise(r => setImmediate(r))

  assert.match(postRenderInfo().title, /Edit deepseek\/deepseek-v4-flash/)
  const rows = els.rows()
  assert.equal(rows.length, 14, 'canonic cells with no existing profile are hidden (16 - 2)')
  const checkedCells = rows
    .filter(r => r.querySelector('.vc-check').checked)
    .map(r => `${r.dataset.tb}_${r.dataset.effort}`)
    .sort()
  assert.deepEqual(checkedCells,
    ['30m_high', '30m_thinking', '60m_low', '60m_none'],
    'only existing profiles checked')
})

function postRenderInfo() {
  return {
    title: h.doc.getElementById('dlgTitle').textContent,
    create: h.doc.getElementById('dlgCreate'),
  }
}

function variantRemovalImpact(key, overrides = {}) {
  return { key, runFiles: 6, logs: 2, resultRows: 4, activeJobs: 0, ...overrides }
}

function uncheckRow(els, tb, effort) {
  const row = els.rows().find(r => r.dataset.tb === tb && r.dataset.effort === effort)
  const removeItem = row.querySelector('.vc-opt-item[data-action="remove"]')
  if (removeItem) h.click(removeItem)
  else {
    row.querySelector('.vc-check').checked = false
  }
  return row
}

test('variant removal gates on a confirmation dialog before re-sending with confirm', async () => {
  const { doc, click, state } = h
  const els = await openEdit()
  const key = '30m_deepseek-v4-flash_thinking'
  const removedRow = uncheckRow(els, '30m', 'thinking')
  assert.ok(removedRow.classList.contains('vc-to-remove'))

  state.putFn = (opts) => {
    const b = JSON.parse(opts.body)
    if (b.confirm === true) {
      return json({ ok: true, plan: { removed: [key], renamed: [], updated: [], created: [], model: null, removedRuns: 6, removedLogs: 2, removedResultRows: 4 } })
    }
    return json({ ok: false, code: 'confirmation_required', impact: [variantRemovalImpact(key)] }, 409)
  }

  click(els.create)
  await new Promise(r => setImmediate(r))

  const firstPut = state.calls.find(c => c.method === 'PUT' && c.url === '/api/models')
  assert.ok(firstPut, 'preflight PUT issued')
  assert.equal(firstPut.body.confirm, undefined, 'preflight carries no confirm flag')
  assert.equal(firstPut.body.edits.find(e => e.key === key).keep, false)

  const confirmOverlay = doc.getElementById('confirmDeleteOverlay')
  assert.ok(!confirmOverlay.classList.contains('cm-hidden'), 'confirmation dialog opens')
  const impactHtml = doc.getElementById('confirmImpact').textContent
  assert.ok(impactHtml.includes(key), 'impact table lists the variant')
  assert.ok(/6 run files/.test(impactHtml), 'run file count shown')
  assert.ok(/2 logs/.test(impactHtml), 'log count shown')
  assert.ok(/4 results rows/.test(impactHtml), 'results row count shown')
  const btn = doc.getElementById('confirmDeleteButton')
  assert.ok(btn.textContent.includes('Delete permanently'), 'danger button rendered')
  assert.ok(!btn.disabled)

  const putCountBefore = state.calls.filter(c => c.method === 'PUT' && c.url === '/api/models').length
  click(btn)
  await new Promise(r => setImmediate(r))
  await new Promise(r => setImmediate(r))

  const putsAfter = state.calls.filter(c => c.method === 'PUT' && c.url === '/api/models')
  assert.equal(putsAfter.length, putCountBefore + 1, 'confirmed PUT re-sent')
  assert.equal(putsAfter[putsAfter.length - 1].body.confirm, true, 'confirmed PUT carries confirm flag')
  assert.ok(confirmOverlay.classList.contains('cm-hidden'), 'confirmation dialog closes after confirm')
  assert.ok(els.dialog.classList.contains('cm-hidden'), 'edit dialog closes after confirm')
  const banner = doc.getElementById('runBanner')
  assert.ok(banner.textContent.includes('6 files') && banner.textContent.includes('2 logs') && banner.textContent.includes('4 results rows'), 'banner shows cascade totals')
})

test('canceling variant confirmation restores rows and never deletes', async () => {
  const { doc, click, state } = h
  const els = await openEdit()
  const key = '30m_deepseek-v4-flash_thinking'
  const removedRow = uncheckRow(els, '30m', 'thinking')

  state.putFn = () => json({ ok: false, code: 'confirmation_required', impact: [variantRemovalImpact(key)] }, 409)
  click(els.create)
  await new Promise(r => setImmediate(r))

  const confirmOverlay = doc.getElementById('confirmDeleteOverlay')
  assert.ok(!confirmOverlay.classList.contains('cm-hidden'))
  click(doc.getElementById('confirmDeleteCancel'))
  await new Promise(r => setImmediate(r))

  assert.ok(confirmOverlay.classList.contains('cm-hidden'), 'confirmation dialog closes on cancel')
  assert.ok(!els.dialog.classList.contains('cm-hidden'), 'edit dialog stays open on cancel')
  const restored = els.rows().find(r => r.dataset.tb === '30m' && r.dataset.effort === 'thinking')
  assert.equal(restored.querySelector('.vc-check').checked, true, 'checkbox re-checked')
  assert.ok(!restored.classList.contains('vc-to-remove'), 'removal styling cleared')
  const puts = state.calls.filter(c => c.method === 'PUT' && c.url === '/api/models')
  assert.equal(puts.length, 1, 'no confirmed PUT after cancel')
  assert.equal(puts[0].body.confirm, undefined)
  assert.ok(state.calls.every(c => !(c.method === 'PUT' && c.body && c.body.confirm === true)), 'never sent a confirmed delete')
})

test('variant with an active job is blocked and not deletable', async () => {
  const { doc, click, state } = h
  const els = await openEdit()
  const key = '30m_deepseek-v4-flash_thinking'
  uncheckRow(els, '30m', 'thinking')

  state.putFn = () => json({ ok: false, code: 'active_jobs', impact: [variantRemovalImpact(key, { activeJobs: 1 })] }, 409)
  click(els.create)
  await new Promise(r => setImmediate(r))

  const confirmOverlay = doc.getElementById('confirmDeleteOverlay')
  assert.ok(!confirmOverlay.classList.contains('cm-hidden'))
  const btn = doc.getElementById('confirmDeleteButton')
  assert.ok(btn.disabled, 'no deletable variants disables the delete button')
  assert.match(btn.textContent, /active jobs/i)
  assert.ok(/cannot delete — job still running/.test(doc.getElementById('confirmImpact').textContent))
  assert.ok(state.calls.every(c => !(c.method === 'PUT' && c.body && c.body.confirm === true)), 'never sends a delete for an active variant')
})

test('edit save builds PUT payload with locked-row removal and tier creation', async () => {
  const { doc, click, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  click(els.list.querySelector('.model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))

  const rows = els.rows()
  const lockedRow = rows.find(r => r.dataset.tb === '30m' && r.dataset.effort === 'thinking')
  assert.equal(lockedRow.dataset.locked, '1', 'canonic profile row is locked')
  click(lockedRow.querySelector('.vc-opt-item[data-action="remove"]'))
  assert.equal(lockedRow.querySelector('.vc-check').checked, false, 'Remove variant unchecks locked row')

  const tierRow = rows.find(r => r.dataset.tb === '60m' && r.dataset.effort === 'medium')
  tierRow.querySelector('.vc-check').checked = true
  tierRow.querySelector('.vc-temp').value = '0.66'
  click(els.create)
  await new Promise(r => setImmediate(r))

  const putCall = state.calls.find(c => c.method === 'PUT' && c.url === '/api/models')
  assert.ok(putCall, 'PUT issued')
  assert.equal(putCall.body.old_model, 'deepseek/deepseek-v4-flash')
  assert.equal(putCall.body.new_model, 'deepseek/deepseek-v4-flash')
  const removed = putCall.body.edits.find(e => e.key === '30m_deepseek-v4-flash_thinking')
  assert.equal(removed.keep, false)
  assert.ok(!putCall.body.edits.find(e => e.key === '60m_deepseek-v4-flash_notthinking'), 'kept locked baseline emits no edit')
  const created = putCall.body.create.find(e => e.time_budget === '60m' && e.effort === 'medium')
  assert.ok(created && created.temperature === 0.66, 'create carries temp')
  assert.ok(els.dialog.classList.contains('cm-hidden'), 'dialog closed after save')
})

test('locked canonic rows and hidden cells render correctly', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  click(els.list.querySelector('.model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))

  const rows = els.rows()
  assert.ok(!rows.some(r => r.dataset.tb === '60m' && r.dataset.effort === 'thinking'), 'absent 60m thinking hidden')
  assert.ok(!rows.some(r => r.dataset.tb === '30m' && r.dataset.effort === 'none'), 'absent 30m none hidden')

  const locked = rows.filter(r => r.dataset.locked === '1')
  assert.deepEqual(
    locked.map(r => `${r.dataset.tb}_${r.dataset.effort}`).sort(),
    ['30m_thinking', '60m_none'],
    'present canonic profiles are the locked rows',
  )
  for (const r of locked) {
    const check = r.querySelector('.vc-check')
    assert.equal(check.disabled, true, 'locked checkbox disabled')
    assert.equal(check.checked, true, 'locked checkbox stays checked')
    assert.ok(r.querySelector('.vc-temp').readOnly, 'locked temp read-only')
    assert.ok(r.querySelector('.vc-max').readOnly, 'locked max read-only')
    assert.match(r.querySelector('.vc-mode').dataset.tooltip || '', /cannot be modified/, 'locked mode cell carries explainer tooltip')
    const items = Array.from(r.querySelectorAll('.vc-opt-item'))
    const enabled = items.filter(i => !i.disabled)
    assert.deepEqual(enabled.map(i => i.dataset.action), ['remove'], 'only Remove variant enabled on locked row')
    assert.deepEqual(
      items.filter(i => i.disabled).map(i => i.dataset.action).sort(),
      ['agent', 'judge', 'prefill', 'run'],
      'run/prefill/judge/agent disabled',
    )
  }
  const editable = rows.filter(r => r.dataset.locked !== '1')
  assert.ok(editable.length > 0, 'non-canonic tiers stay editable')
  assert.ok(editable.every(r => !r.querySelector('.vc-check').disabled), 'editable rows have enabled checkboxes')
  assert.ok(editable.every(r => !r.querySelector('.vc-mode').dataset.tooltip), 'no tooltip on editable rows')
})

test('Remove variant on locked row toggles and can be restored', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  click(els.list.querySelector('.model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))

  const rows = els.rows()
  const row = rows.find(r => r.dataset.tb === '60m' && r.dataset.effort === 'none')
  const check = row.querySelector('.vc-check')
  const removeBtn = row.querySelector('.vc-opt-item[data-action="remove"]')
  assert.equal(check.checked, true)
  click(removeBtn)
  assert.equal(check.checked, false)
  assert.ok(row.classList.contains('vc-to-remove'))
  assert.equal(row.querySelector('.vc-status').textContent.trim(), 'will remove')
  click(removeBtn)
  assert.equal(check.checked, true)
  assert.ok(!row.classList.contains('vc-to-remove'))
  assert.equal(row.querySelector('.vc-status').textContent.trim(), 'pending')
})

test('edit rename sends new_model in payload', async () => {
  const { doc, click, setValue, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  click(els.list.querySelector('.model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))
  setValue(els.modelInput, 'deepseek-v4-flash-turbo')
  click(els.create)
  await new Promise(r => setImmediate(r))
  const putCall = state.calls.find(c => c.method === 'PUT' && c.url === '/api/models')
  assert.equal(putCall.body.new_model, 'deepseek-v4-flash-turbo')
})

// ── delete ───────────────────────────────────────────────────

test('delete requires confirmation then fires DELETE and refreshes', async () => {
  const { doc, click, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  const card = els.list.querySelector('.model-card')
  const delBtn = card.querySelector('[data-action="delete"]')
  click(delBtn)
  assert.equal(delBtn.textContent.trim(), 'Confirm delete?')
  assert.ok(delBtn.dataset.armed)

  const delBefore = state.calls.filter(c => c.method === 'DELETE').length
  click(delBtn)
  await new Promise(r => setImmediate(r))
  const delCall = state.calls.filter(c => c.method === 'DELETE')[delBefore]
  assert.deepEqual(delCall.body, { model: 'deepseek/deepseek-v4-flash' })
  const getAfter = state.calls.filter(c => c.method === 'GET' && c.url === '/api/models').length
  assert.ok(getAfter >= 2, 'refresh after delete')
})

// ── close paths ──────────────────────────────────────────────

test('close button hides settings overlay', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  const els = postRender()
  assert.ok(!els.overlay.classList.contains('cm-hidden'))
  click(els.close)
  assert.ok(els.overlay.classList.contains('cm-hidden'))
  click(els.overlay) // click outside
  click(doc.querySelector('[data-settings-toggle]'))
  click(els.overlay)
  assert.ok(els.overlay.classList.contains('cm-hidden'), 'overlay backdrop click closes')
})

test('Escape key closes dialog then overlay', async () => {
  const { doc, click, postRender } = h
  click(doc.querySelector('[data-settings-toggle]'))
  const els = postRender()
  click(els.addBtn)
  assert.ok(!els.dialog.classList.contains('cm-hidden'))
  doc.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  assert.ok(els.dialog.classList.contains('cm-hidden'), 'esc closes dialog')
  doc.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  assert.ok(els.overlay.classList.contains('cm-hidden'), 'esc closes overlay')
})

// ── run data section ─────────────────────────────────────────

const waitUntil = async (fn, timeout = 1500) => {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    if (fn()) return
    await new Promise(r => setTimeout(r, 5))
  }
  throw new Error('waitUntil timed out')
}

const openRun = async (h) => {
  h.click(h.doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  h.click(h.doc.querySelector('.settings-nav-item[data-section="run"]'))
  await waitUntil(() => h.doc.querySelectorAll('.tool-card').length === RUN_REGISTRY.length)
}

test('run tab renders tool cards, groups, and empty jobs state', async () => {
  const { doc, state } = h
  state.jobs.jobs = []
  await openRun(h)
  const cards = Array.from(doc.querySelectorAll('.tool-card'))
  assert.equal(cards.length, RUN_REGISTRY.length)
  const groups = Array.from(doc.querySelectorAll('.tool-group-label')).map(g => g.textContent)
  assert.ok(groups.includes('Run harness'))
  assert.ok(groups.includes('Analysis & maintenance'))

  const reset = doc.querySelector('.tool-card[data-tool="reset_benchmark"]')
  assert.ok(reset.querySelector('.tool-warning'), 'destructive warning shown')
  assert.ok(reset.querySelector('[data-confirm]'), 'confirm field shown')

  const benchField = doc.querySelector('.tool-card[data-tool="run_candidate"] [data-arg="bench"]')
  assert.ok(benchField.getAttribute('list') === 'benchList', 'bench input wired to datalist')

  assert.match(doc.getElementById('jobsList').textContent, /No runs yet/)
  assert.match(doc.getElementById('jobCount').textContent, /No runs/)
  assert.equal(state.es.length, 0, 'no event sources without expanded jobs')
})

test('preset fills the form then submit POSTs collected args', async () => {
  const { doc, click, state } = h
  await openRun(h)
  const card = doc.querySelector('.tool-card[data-tool="run_candidate"]')
  click(card.querySelector('.preset-chip[data-preset="smoke"]'))
  assert.equal(card.querySelector('[data-arg="bench"]').value, 'chapter_fast')
  assert.equal(card.querySelector('[data-arg="mock"]').checked, true)
  assert.equal(card.querySelector('[data-arg="max-samples"]').value, '4')

  click(card.querySelector('.tool-run-btn'))
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.calls.some(c => c.url === '/api/jobs' && c.method === 'POST'))
  const post = state.calls.find(c => c.url === '/api/jobs' && c.method === 'POST')
  assert.deepEqual(post.body, {
    toolId: 'run_candidate',
    args: { bench: 'chapter_fast', profile: 'all', time: 'all', mock: true, 'max-samples': 4 },
  })
  assert.ok(!('confirm' in post.body), 'no confirm field for non-destructive tool')
})

test('destructive run gates on typed RESET and posts confirm', async () => {
  const { doc, click, setValue, state } = h
  await openRun(h)
  const card = doc.querySelector('.tool-card[data-tool="reset_benchmark"]')
  click(card.querySelector('.tool-run-btn'))
  const submit = card.querySelector('.tool-run-submit')
  assert.ok(submit.disabled, 'submit disabled until RESET typed')
  const confirmEl = card.querySelector('[data-confirm]')
  setValue(confirmEl, 'resat')
  assert.ok(submit.disabled, 'wrong phrase still disabled')
  setValue(confirmEl, 'RESET')
  assert.ok(!submit.disabled, 'correct phrase enables')
  click(submit)
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'reset_benchmark'))
  const post = state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'reset_benchmark')
  assert.equal(post.body.confirm, 'RESET')
  assert.deepEqual(post.body.args, {})
})

test('job rows show status badges; cancel is two-step confirm', async () => {
  const { doc, click, state } = h
  await openRun(h)
  const runningRow = doc.querySelector('.job-row[data-job="j1"]')
  assert.ok(runningRow, 'running job rendered')
  const badge = runningRow.querySelector('.job-badge')
  assert.ok(badge.classList.contains('st-running'))
  assert.match(badge.textContent, /Running/)

  const cancelBtn = runningRow.querySelector('.job-cancel')
  click(cancelBtn)
  assert.equal(cancelBtn.textContent.trim(), 'Confirm cancel?')
  assert.equal(cancelBtn.dataset.armed, '1')
  click(cancelBtn)
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs/j1/cancel'))
  assert.ok(state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs/j1/cancel'))
})

test('re-run replays original args via detail fetch', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [
    {
      id: 'j3',
      toolId: 'run_candidate',
      status: 'succeeded',
      createdAt: '2026-09-05T09:00:00Z',
      finishedAt: '2026-09-05T09:05:00Z',
      exitCode: 0,
      resultHints: { bench: 'chapter_fast' },
    },
  ]
  await openRun(h)
  const row = doc.querySelector('.job-row[data-job="j3"]')
  click(row.querySelector('.job-rerun'))
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate'))
  const post = state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate')
  assert.deepEqual(post.body, { toolId: 'run_candidate', args: { bench: 'chapter_fast', profile: 'all', mock: true } })
})

test('expanded running job opens an EventSource stream', async () => {
  const { doc, click, state } = h
  await openRun(h)
  const row = doc.querySelector('.job-row[data-job="j1"]')
  click(row.querySelector('.job-row-head'))
  const consoleEl = row.querySelector('.job-console')
  assert.ok(!consoleEl.classList.contains('cm-hidden'), 'console expanded on click')
  await waitUntil(() => state.es.length === 1)
  assert.match(state.es[0], /\/api\/jobs\/j1\/stream/)
})

test('results-refreshed hint dispatches window event', async () => {
  const { doc, state } = h
  let fired = 0
  h.window.addEventListener('dashboard:results-refreshed', () => { fired++ })
  state.jobs.jobs = [
    {
      id: 'j4',
      toolId: 'run_candidate',
      status: 'succeeded',
      createdAt: '2026-09-05T08:00:00Z',
      finishedAt: '2026-09-05T08:01:00Z',
      exitCode: 0,
      resultHints: { resultsTsvUpdated: true },
    },
  ]
  await openRun(h)
  await waitUntil(() => fired === 1)
  assert.equal(fired, 1)
})

// ── per-profile quick actions (edit dialog ⋯ menus) ──────────

const openEdit = async () => {
  h.click(h.doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  h.click(h.doc.querySelector('#modelsList .model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))
  return h.postRender()
}

test('edit dialog shows ⋯ menu on existing profile rows only', async () => {
  const { doc, click } = h
  await openEdit(h)
  const rows = h.postRender().rows()
  const withMenu = rows.filter(r => r.querySelector('.vc-opt-btn'))
  assert.equal(withMenu.length, 4, 'menus only on existing profiles')
  assert.deepEqual(withMenu.map(r => r.querySelector('.vc-opt-btn').dataset.slug).sort(),
    ['30m_deepseek-v4-flash_effort-high', '30m_deepseek-v4-flash_thinking',
      '60m_deepseek-v4-flash_effort-low', '60m_deepseek-v4-flash_notthinking'])
  assert.ok(rows.every(r => {
    const m = r.querySelector('.vc-opt-menu')
    return !m || m.classList.contains('cm-hidden')
  }), 'menus closed initially')

  const locked = withMenu.find(r => r.dataset.locked === '1')
  const lockedBtn = locked.querySelector('.vc-opt-btn')
  click(lockedBtn)
  const lockedMenu = lockedBtn.closest('.vc-opt').querySelector('.vc-opt-menu')
  assert.ok(!lockedMenu.classList.contains('cm-hidden'), 'menu opens on ⋯ click')
  assert.deepEqual([...lockedMenu.querySelectorAll('.vc-opt-item')].map(i => i.dataset.action),
    ['run', 'prefill', 'judge', 'agent', 'remove'])
  assert.ok([...lockedMenu.querySelectorAll('.vc-opt-item')]
    .filter(i => i.dataset.action !== 'remove').every(i => i.disabled),
  'locked profile keeps run/prefill/judge/agent disabled')

  const plain = withMenu.find(r => r.dataset.locked !== '1')
  const plainBtn = plain.querySelector('.vc-opt-btn')
  click(plainBtn)
  const plainMenu = plainBtn.closest('.vc-opt').querySelector('.vc-opt-menu')
  assert.deepEqual([...plainMenu.querySelectorAll('.vc-opt-item')].map(i => i.dataset.action),
    ['run', 'prefill', 'judge', 'agent'])
  assert.ok([...plainMenu.querySelectorAll('.vc-opt-item')].every(i => !i.disabled),
    'editable profile keeps all actions enabled')
})

test('clicking outside the menu closes it', async () => {
  const { doc, click } = h
  await openEdit(h)
  const rows = h.postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn')).querySelector('.vc-opt-btn')
  click(btn)
  assert.ok(!btn.closest('.vc-opt').querySelector('.vc-opt-menu').classList.contains('cm-hidden'))
  click(doc.getElementById('dlgModel'))
  await new Promise(r => setImmediate(r))
  assert.ok(btn.closest('.vc-opt').querySelector('.vc-opt-menu').classList.contains('cm-hidden'), 'outside click closes menu')
})

test('Run candidate now posts run_candidate with profile args and shows notice', async () => {
  const { doc, click, postRender } = h
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  click(btn.closest('.vc-opt').querySelector('[data-action="run"]'))
  await waitUntil(() => h.state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'))
  const post = h.state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs')
  assert.deepEqual(post.body, {
    toolId: 'run_candidate',
    args: { bench: 'chapter_fast', profile: '30m_deepseek-v4-flash_effort-high', time: '30m', 'write-results': true },
  })
  const notice = doc.getElementById('dlgNotice')
  await waitUntil(() => !notice.classList.contains('cm-hidden'))
  assert.match(notice.textContent, /Launched run_candidate/)
  assert.ok(!doc.getElementById('modelDialogOverlay').classList.contains('cm-hidden'), 'dialog stays open after launch')
})

test('re-judge asks for judge model once then remembers it', async () => {
  const { doc, click, postRender, state } = h
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  state.promptReturn = 'openai/gpt-4o-mini'
  click(btn.closest('.vc-opt').querySelector('[data-action="judge"]'))
  await waitUntil(() => h.state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'))
  const post = h.state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs')
  assert.equal(state.promptCalls, 1)
  assert.deepEqual(post.body, {
    toolId: 'judge_existing',
    args: { bench: 'chapter_fast', profile: '30m_deepseek-v4-flash_effort-high', 'judge-model': 'openai/gpt-4o-mini' },
  })
  assert.equal(state.ls.get('mm.judgeModel'), 'openai/gpt-4o-mini')

  state.calls.length = 0
  h.click(doc.getElementById('dlgClose'))
  await openEdit(h)
  const rows2 = postRender().rows()
  const btn2 = rows2.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'low').querySelector('.vc-opt-btn')
  click(btn2)
  state.promptCalls = 0
  click(btn2.closest('.vc-opt').querySelector('[data-action="judge"]'))
  await waitUntil(() => h.state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'))
  assert.equal(state.promptCalls, 0, 'remembered judge model avoids second prompt')
  const post2 = h.state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs')
  assert.equal(post2.body.args['judge-model'], 'openai/gpt-4o-mini')
})

test('Run with options mounts inline run_candidate widget, keeps dialog open, judge off', async () => {
  const { doc, click, postRender, state } = h
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  const item = btn.closest('.vc-opt').querySelector('[data-action="prefill"]')
  assert.match(item.textContent, /Run with options/)
  click(item)

  const dialog = doc.getElementById('modelDialogOverlay')
  await waitUntil(() => doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]'))
  assert.ok(!dialog.classList.contains('cm-hidden'), 'dialog stays open')
  assert.ok(dialog.classList.contains('has-run-form'), 'dialog widened to two columns')
  assert.equal(dialog.dataset.runSlug, '30m_deepseek-v4-flash_effort-high')
  assert.ok(!doc.getElementById('dlgRunForm').classList.contains('cm-hidden'), 'run column visible')

  const card = doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]')
  assert.equal(card.querySelector('[data-arg="bench"]').value, 'chapter_fast')
  assert.equal(card.querySelector('[data-arg="profile"]').value, '30m_deepseek-v4-flash_effort-high')
  assert.equal(card.querySelector('[data-arg="time"]').value, '30m')
  const cb = card.querySelector('[data-toggle="judge-model"]')
  const input = card.querySelector('[data-arg="judge-model"]')
  assert.equal(cb.checked, false, 'judge starts off for a deterministic quick run')
  assert.ok(input.classList.contains('cm-hidden'), 'judge input hidden when unchecked')
  assert.ok(!card.querySelector('.tool-form').classList.contains('cm-hidden'), 'form opened for editing')
  const runTabCard = doc.querySelector('#toolList .mm-tool-widget[data-tool="run_candidate"]')
  assert.ok(runTabCard, 'run tab widget also present, same source renderer')

  const notice = doc.getElementById('dlgNotice')
  assert.ok(!notice.classList.contains('cm-hidden'), 'success notice shown after opening inline form')
  assert.match(notice.textContent, /Run script/)
})

test('CSS contract: has-run-form is keyed off the overlay, not the dialog root', async () => {
  const css = await import('node:fs').then(fs => fs.readFileSync(new URL('../settings.css', import.meta.url), 'utf-8'))
  assert.match(css, /#modelDialogOverlay\.has-run-form \.settings-dialog/,
    'width rule scoped to the overlay carrying the class')
  assert.match(css, /#modelDialogOverlay\.has-run-form \.dialog-body/,
    'flex layout scoped to the overlay carrying the class')
  assert.doesNotMatch(css, /\.settings-dialog\.has-run-form/,
    'no rule keys off the dialog root — the class lives on #modelDialogOverlay')
  assert.match(css, /#modelDialogOverlay\.has-run-form \.dlg-run/,
    'mobile media-query rule also overlay-scoped')
})

test('missing #dlgRunWidget (stale bundle) surfaces an error notice, never a silent throw', async () => {
  const { doc, click, postRender, state } = h
  await openEdit(h)
  const postsBefore = state.calls.filter(c => c.method === 'POST' && c.url === '/api/jobs').length
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  doc.getElementById('dlgRunWidget').outerHTML = ''
  click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))

  const notice = doc.getElementById('dlgNotice')
  await waitUntil(() => notice.classList.contains('dlg-notice-err'))
  assert.match(notice.textContent, /Run form is missing/)
  assert.ok(!doc.getElementById('modelDialogOverlay').classList.contains('has-run-form'),
    'no half-mounted state when the widget is absent')
  const postsAfter = state.calls.filter(c => c.method === 'POST' && c.url === '/api/jobs').length
  assert.equal(postsAfter, postsBefore, 'stale-bundle guard does not POST anything')
})

test('run tab and dialog render the same widget (single source)', async () => {
  const { doc, click, postRender, state } = h
  state.jobs.jobs = []
  await openRun(h)
  const runTab = doc.querySelector('#toolList .mm-tool-widget[data-tool="run_candidate"]')
  const rcTool = RUN_REGISTRY.find(t => t.id === 'run_candidate')

  const canon = (html) => html.replace(/>\s+</g, '><').trim()
  const pristine = h.mod.toolCardHtml(rcTool)
  const tabNormal = doc.createElement('div')
  tabNormal.innerHTML = pristine
  assert.equal(canon(runTab.outerHTML), canon(tabNormal.innerHTML),
    'run tab renders toolCardHtml unmodified')

  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))
  await waitUntil(() => doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]'))
  const dlg = doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]')

  const prefixed = h.mod.toolCardHtml(rcTool, { idPrefix: 'dlg-run-fld' })
  const dlgNormal = doc.createElement('div')
  dlgNormal.innerHTML = prefixed
  assert.equal(
    canon(dlgNormal.innerHTML).replaceAll('dlg-run-fld-', 'run-fld-'),
    canon(tabNormal.innerHTML).replaceAll('dlg-run-fld-', 'run-fld-'),
    'prefix is the only source difference between mounts',
  )

  const dlgArgs = [...dlg.querySelectorAll('[data-arg]')].map(e => e.getAttribute('data-arg')).join(',')
  const tabArgs = [...runTab.querySelectorAll('[data-arg]')].map(e => e.getAttribute('data-arg')).join(',')
  assert.equal(dlgArgs, tabArgs, 'identical field order')
  assert.ok(!dlg.innerHTML.includes('id="run-fld-'), 'dialog widget uses isolated ids')
  assert.ok(!runTab.innerHTML.includes('id="dlg-run-fld-'), 'run tab widget keeps default ids')
})

test('inline run form is cleared on dialog close and reopens clean', async () => {
  const { doc, click, postRender } = h
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))
  await waitUntil(() => doc.querySelector('#dlgRunWidget .mm-tool-widget'))
  assert.equal(doc.getElementById('modelDialogOverlay').dataset.runSlug, '30m_deepseek-v4-flash_effort-high')

  click(doc.getElementById('dlgClose'))
  assert.ok(doc.getElementById('modelDialogOverlay').classList.contains('cm-hidden'))
  assert.ok(!doc.getElementById('modelDialogOverlay').classList.contains('has-run-form'), 'run column collapsed on close')
  assert.ok(doc.getElementById('dlgRunForm').classList.contains('cm-hidden'), 'run column hidden')

  await openEdit(h)
  assert.equal(doc.getElementById('dlgRunWidget').innerHTML, '', 'stale widget cleared')
  assert.ok(doc.getElementById('modelDialogOverlay').classList.contains('has-run-form') === false)
})

test('inline run form submits its own job from the dialog widget', async () => {
  const { doc, click, postRender, state } = h
  state.jobs.jobs = []
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))
  await waitUntil(() => doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]'))
  const card = doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]')
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate'))
  const post = state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate')
  assert.deepEqual(post.body, {
    toolId: 'run_candidate',
    args: { bench: 'chapter_fast', profile: '30m_deepseek-v4-flash_effort-high', time: '30m', mock: false, 'max-samples': 0 },
  })
  assert.ok(!('judge-model' in post.body.args), 'judge omitted when toggle off')
  const notice = doc.getElementById('dlgNotice')
  await waitUntil(() => /Launched run_candidate/.test(notice.textContent))
  assert.match(notice.textContent, /Launched run_candidate/)
  assert.ok(!doc.getElementById('modelDialogOverlay').classList.contains('cm-hidden'), 'dialog stays open after submit')
  await waitUntil(() => !doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'))
  const badge = doc.getElementById('dlgProgBadge')
  assert.match(badge.textContent, /Queued/)
  const status = doc.getElementById('dlgProgStatus')
  assert.match(status.textContent, /Queued/)
})

// ── in-dialog run progress ──────────────────────────────────

const RUN_JOB_RUNNING = { id: 'j9', toolId: 'run_candidate', status: 'running', createdAt: '2026-09-05T09:00:00Z', startedAt: '2026-09-05T09:00:01Z', exitCode: null, resultHints: {} }

const openPrefill = async () => {
  await openEdit(h)
  const rows = h.postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  h.click(btn)
  h.click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))
  await waitUntil(() => h.doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]'))
  return h.doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]')
}

test('dialog submit renders progress pane for a running job', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => !doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'))
  assert.match(doc.getElementById('dlgProgBadge').textContent, /Running/)
  assert.match(doc.getElementById('dlgProgStatus').textContent, /Running/)
  const timerEl = doc.querySelector('#dlgRunProgress .dlg-elapsed')
  assert.ok(timerEl, 'elapsed timer node present')
})

test('dialog log events append into the in-dialog console', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.esInstances.length >= 1)
  const dlgEs = state.esInstances[state.esInstances.length - 1]
  dlgEs.emit('log', { stream: 'stdout', text: 'Running profile: x' })
  const pre = doc.querySelector('#dlgRunProgress .dlg-console-pre')
  assert.match(pre.textContent, /Running profile: x/)
  dlgEs.emit('log', { stream: 'stderr', text: 'boom' })
  const err = doc.querySelector('#dlgRunProgress .con-err')
  assert.ok(err && /boom/.test(err.textContent), 'stderr wrapped in .con-err')
  assert.ok(!doc.getElementById('dlgProgConsole').classList.contains('cm-hidden'), 'console visible after log line')
  assert.ok(doc.getElementById('dlgProgSpinner').classList.contains('cm-hidden'), 'spinner hidden once output arrives')
})

test('terminal status detaches dialog stream and renders summary', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.esInstances.length >= 1)
  const dlgEs = state.esInstances[state.esInstances.length - 1]
  dlgEs.emit('status', { status: 'succeeded', exitCode: 0, resultHints: { runId: 'r1', resultsTsvUpdated: true } })
  assert.ok(dlgEs.closed, 'dialog stream closed on terminal')
  assert.match(doc.getElementById('dlgProgBadge').textContent, /Succeeded/)
  assert.match(doc.getElementById('dlgProgStatus').textContent, /Succeeded/)
  assert.match(doc.getElementById('dlgProgHints').textContent, /Run ID: r1/)
  assert.ok(doc.getElementById('dlgProgSpinner').classList.contains('cm-hidden'), 'spinner hidden at terminal')
  const actions = doc.getElementById('dlgProgActions')
  assert.ok(actions.querySelector('.dlg-prog-rerun'), 'Re-run action shown')
  assert.match(actions.querySelector('.dlg-prog-explorer').textContent, /explorer/)
})

test('failed run shows error and log link in the dialog pane', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.esInstances.length >= 1)
  const dlgEs = state.esInstances[state.esInstances.length - 1]
  dlgEs.emit('status', { status: 'failed', exitCode: 1, error: 'boom' })
  assert.ok(dlgEs.closed, 'dialog stream closed on failure')
  assert.match(doc.getElementById('dlgProgStatus').textContent, /boom/)
  assert.match(doc.getElementById('dlgProgBadge').textContent, /Failed/)
  const logBtn = doc.querySelector('#dlgProgActions .dlg-prog-log')
  assert.ok(logBtn, 'view log button in failed action row')
  assert.equal(logBtn.dataset.job, 'j9')
  h.click(logBtn)
  await waitUntil(() => !doc.getElementById('logPanel').classList.contains('cm-hidden'))
  assert.equal(doc.getElementById('panelTitle').textContent, 'Run candidate')
  assert.match(state.es[state.es.length - 1], /\/api\/jobs\/j9\/stream/, 'panel stream opened from dialog view log')
})

test('failedFast POST renders terminal pane without opening a stream', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = {
    ok: true,
    failedFast: true,
    job: { id: 'jf', toolId: 'run_candidate', status: 'failed', failedFast: true, error: 'script not found: core/run_candidate.py', createdAt: '2026-09-05T09:00:00Z', finishedAt: '2026-09-05T09:00:00Z', exitCode: null, resultHints: {} },
  }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => !doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'))
  assert.equal(state.es.length, 0, 'no EventSource for fast-failed job')
  assert.match(doc.getElementById('dlgProgBadge').textContent, /Failed/)
  assert.match(doc.getElementById('dlgProgStatus').textContent, /script not found/)
})

test('duplicate POST shows already-queued pane and streams the existing job', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.jobsPost = {
    ok: true,
    duplicate: true,
    job: { id: 'j1', toolId: 'run_candidate', status: 'queued', createdAt: '2026-09-05T09:00:00Z', exitCode: null, resultHints: {} },
  }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.es.length === 1)
  assert.match(state.es[0], /\/api\/jobs\/j1\/stream/)
  assert.match(doc.getElementById('dlgProgStatus').textContent, /Already queued/)
})

test('dialog stream and run-data row stream coexist for the same job', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.es.length === 2)
  assert.ok(state.es.every(u => /\/api\/jobs\/j9\/stream/.test(u)), 'two distinct stream subscriptions for j9')
})

test('dlgRunClose detaches the dialog stream and hides the pane', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.esInstances.length >= 2)
  const dlgEs = state.esInstances[state.esInstances.length - 1]
  assert.ok(!dlgEs.closed, 'dialog stream live before close')
  click(doc.getElementById('dlgRunClose'))
  assert.ok(dlgEs.closed, 'dialog stream closed on run-form close')
  assert.ok(doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'), 'pane hidden')
  assert.ok(!doc.getElementById('modelDialogOverlay').classList.contains('cm-hidden'), 'dialog stays open')
})

test('queued pane shows queue position from the job list', async () => {
  const { doc, click, state } = h
  const q1 = { id: 'qa', toolId: 'run_candidate', status: 'queued', createdAt: '2026-09-05T09:00:00Z', exitCode: null, resultHints: {} }
  const q2 = { id: 'qb', toolId: 'run_candidate', status: 'queued', createdAt: '2026-09-05T09:00:01Z', exitCode: null, resultHints: {} }
  state.jobs.jobs = [q1, q2]
  state.jobsPost = { ok: true, job: q2 }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => /position 2/.test(doc.getElementById('dlgProgStatus').textContent))
})

test('run submit is disabled while the POST is in flight in the dialog', async () => {
  let resolveGate
  h.state.postGate = new Promise(r => { resolveGate = r })
  h.state.jobs.jobs = []
  h.state.jobsPost = { ok: true, job: { ...RUN_JOB_RUNNING } }
  const card = await openPrefill()
  const submit = card.querySelector('.tool-run-submit')
  h.click(submit)
  assert.equal(submit.disabled, true, 'submit disabled while POST pending')
  resolveGate()
  await waitUntil(() => !h.doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'))
})

test('api() surfaces a UI-banner fallback instead of a raw JSON parse failure', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.htmlFallback = '/api/jobs'
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => card.classList.contains('has-error') || /banner fallback/.test(card.textContent))
  assert.match(card.textContent, /banner fallback/, 'descriptive banner error on the card, not a SyntaxError')
  assert.equal(state.es.length, 0, 'no stream for non-job response')
  assert.ok(doc.getElementById('dlgRunProgress').classList.contains('cm-hidden'), 'no progress pane for non-job response')
})

// ── log side panel (live log viewer) ─────────────────────────

const panelStream = (h) => h.state.esInstances[h.state.esInstances.length - 1]

test('run-data view log opens the live log side panel', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  const row = doc.querySelector('.job-row[data-job="j9"]')
  assert.ok(row.querySelector('.job-log-link'), 'view log button rendered on row')
  click(row.querySelector('.job-log-link'))
  await waitUntil(() => !doc.getElementById('logPanel').classList.contains('cm-hidden'))
  assert.ok(!doc.getElementById('logPanelScrim').classList.contains('cm-hidden'), 'scrim visible')
  assert.equal(doc.getElementById('panelTitle').textContent, 'Run candidate')
  await waitUntil(() => state.es.some(u => /\/api\/jobs\/j9\/stream/.test(u)))
  assert.match(doc.getElementById('panelStatus').textContent, /Running/)
  const raw = doc.querySelector('.log-panel-raw')
  assert.equal(raw.getAttribute('href'), '/api/jobs/j9/log')
})

test('log panel appends streamed events with stderr highlight', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  const row = doc.querySelector('.job-row[data-job="j9"]')
  click(row.querySelector('.job-log-link'))
  await waitUntil(() => state.esInstances.length === 1)
  const es = panelStream(h)
  es.emit('log', { stream: 'stdout', text: 'profile 1/3' })
  const pre = doc.querySelector('.log-panel-pre')
  assert.match(pre.textContent, /profile 1\/3/)
  es.emit('log', { stream: 'stderr', text: 'retry warned' })
  assert.ok(pre.querySelector('.con-err') && /retry warned/.test(pre.querySelector('.con-err').textContent), 'stderr wrapped in .con-err')
  es.emit('start', { jobId: 'j9', pid: 42 })
  assert.match(doc.getElementById('panelStatus').textContent, /Running/)
})

test('terminal status event closes the panel stream but keeps panel open', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  click(doc.querySelector('.job-row[data-job="j9"] .job-log-link'))
  await waitUntil(() => state.esInstances.length === 1)
  const es = panelStream(h)
  es.emit('status', { status: 'succeeded', exitCode: 0, resultHints: { runId: 'r9' } })
  assert.ok(es.closed, 'panel stream closed at terminal')
  assert.match(doc.getElementById('panelStatus').textContent, /Succeeded/)
  assert.ok(!doc.getElementById('logPanel').classList.contains('cm-hidden'), 'panel stays open after terminal')
})

test('panel close button hides panel and detaches the stream', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  click(doc.querySelector('.job-row[data-job="j9"] .job-log-link'))
  await waitUntil(() => state.esInstances.length === 1)
  const es = panelStream(h)
  click(doc.getElementById('panelClose'))
  assert.ok(es.closed, 'panel stream detached on close')
  assert.ok(doc.getElementById('logPanel').classList.contains('cm-hidden'), 'panel hidden')
  assert.ok(doc.getElementById('logPanelScrim').classList.contains('cm-hidden'), 'scrim hidden')
  assert.ok(doc.getElementById('panelTitle').textContent === '', 'panel state reset')
})

test('Escape closes the log panel', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  click(doc.querySelector('.job-row[data-job="j9"] .job-log-link'))
  await waitUntil(() => !doc.getElementById('logPanel').classList.contains('cm-hidden'))
  doc.body.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  assert.ok(doc.getElementById('logPanel').classList.contains('cm-hidden'), 'panel closed via Esc')
})

test('queued job panel shows queue position header', async () => {
  const { doc, click, state } = h
  const q1 = { id: 'qa', toolId: 'run_candidate', status: 'queued', createdAt: '2026-09-05T09:00:00Z', exitCode: null, resultHints: {} }
  const q2 = { id: 'j9', toolId: 'run_candidate', status: 'queued', createdAt: '2026-09-05T09:00:01Z', exitCode: null, resultHints: {} }
  state.jobs.jobs = [q1, q2]
  await openRun(h)
  click(doc.querySelector('.job-row[data-job="j9"] .job-log-link'))
  await waitUntil(() => /position 2/.test(doc.getElementById('panelStatus').textContent))
})

test('dialog pane flags a running job with no output for 150s as stale', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  state.jobsPost = { ok: true, job: RUN_JOB_RUNNING }
  const card = await openPrefill()
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => /Running/.test(doc.getElementById('dlgProgStatus').textContent))
  h.mod.RUN_STATE.dlgLastOutputAt = new Date(Date.now() - 151_000).toISOString()
  const reconnectEl = doc.querySelector('#dlgRunProgress .dlg-reconnect')
  await waitUntil(() => /stale/.test(reconnectEl.textContent))
  assert.match(reconnectEl.textContent, /stale — no output/)
})

test('log panel flags a running job with no output for 150s as stale', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = [RUN_JOB_RUNNING]
  await openRun(h)
  click(doc.querySelector('.job-row[data-job="j9"] .job-log-link'))
  await waitUntil(() => /Running/.test(doc.getElementById('panelStatus').textContent))
  h.mod.RUN_STATE.panelLastOutputAt = new Date(Date.now() - 151_000).toISOString()
  const reconnectEl = doc.querySelector('.log-panel-reconnect')
  await waitUntil(() => /stale/.test(reconnectEl.textContent))
  assert.match(reconnectEl.textContent, /stale — no output/)
})

// ── judge-model toggle ───────────────────────────────────────

test('judge-model is a checkbox: default openai/gpt-5.4-mini, hidden input when unchecked', async () => {
  const { doc, setValue, state } = h
  state.jobs.jobs = []
  await openRun(h)
  const card = doc.querySelector('.tool-card[data-tool="run_candidate"]')
  const cb = card.querySelector('[data-toggle="judge-model"]')
  const input = card.querySelector('[data-arg="judge-model"]')
  assert.ok(cb.checked, 'checkbox enabled by default')
  assert.equal(input.value, 'openai/gpt-5.4-mini', 'input defaults to gpt-5.4-mini')
  assert.ok(!input.classList.contains('cm-hidden'), 'input visible when checked')

  cb.checked = false
  cb.dispatchEvent(new h.window.Event('change', { bubbles: true }))
  assert.ok(input.classList.contains('cm-hidden'), 'input hidden when unchecked')
  assert.equal(input.value, '', 'input cleared when unchecked')

  const payload = { toolId: 'run_candidate', args: { bench: 'chapter_fast', profile: 'all', time: 'all', mock: false } }
  h.click(card.querySelector('.tool-run-btn'))
  h.click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate'), 4000)
  const post = state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs' && c.body && c.body.toolId === 'run_candidate')
  assert.ok(!('judge-model' in post.body.args), 'unchecked judge does not send judge-model')
})

test('judge-model enabled with empty value blocks submit with a clear error', async () => {
  const { doc, setValue, click, state } = h
  state.jobs.jobs = []
  await openRun(h)
  const card = doc.querySelector('.tool-card[data-tool="run_candidate"]')
  const cb = card.querySelector('[data-toggle="judge-model"]')
  const input = card.querySelector('[data-arg="judge-model"]')
  cb.checked = true
  setValue(input, '')
  click(card.querySelector('.tool-run-btn'))
  click(card.querySelector('.tool-run-submit'))
  const err = card.querySelector('.tool-form-error')
  assert.ok(!err.classList.contains('cm-hidden'), 'error surfaced')
  assert.match(err.textContent, /required/)
  assert.ok(!state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'), 'no POST when judge enabled but empty')
})

test('judge-model validated against model pattern when enabled', async () => {
  const { doc, setValue, click, state } = h
  state.jobs.jobs = []
  await openRun(h)
  const card = doc.querySelector('.tool-card[data-tool="run_candidate"]')
  const input = card.querySelector('[data-arg="judge-model"]')
  setValue(input, 'spaces not allowed here')
  click(card.querySelector('.tool-run-btn'))
  click(card.querySelector('.tool-run-submit'))
  const err = card.querySelector('.tool-form-error')
  assert.ok(!err.classList.contains('cm-hidden'))
  assert.match(err.textContent, /invalid characters/)
  assert.ok(!state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'))

  setValue(input, 'openai/gpt-5.4-mini')
  click(card.querySelector('.tool-run-submit'))
  await waitUntil(() => state.calls.some(c => c.method === 'POST' && c.url === '/api/jobs'))
  const post = state.calls.find(c => c.method === 'POST' && c.url === '/api/jobs')
  assert.equal(post.body.args['judge-model'], 'openai/gpt-5.4-mini')
})

// ── stale-state recovery (no permanent latch on failed init) ─

test('run tab recovers after a transient /api/registry failure', async () => {
  const { doc, click, state } = h
  state.jobs.jobs = []
  state.registryFails = 1
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const runNav = () => doc.querySelector('.settings-nav-item[data-section="run"]')
  click(runNav())
  await waitUntil(() => doc.getElementById('toolList').innerHTML.includes('Failed to load tools'))
  assert.ok(doc.querySelectorAll('#toolList .tool-card').length === 0, 'no cards after failed registry')

  click(doc.querySelector('.settings-nav-item[data-section="models"]'))
  click(runNav())
  await waitUntil(() => doc.querySelectorAll('#toolList .tool-card').length === RUN_REGISTRY.length)
  assert.ok(doc.querySelectorAll('#toolList .tool-card').length === RUN_REGISTRY.length, 'registry retried and rendered')
})

test('Run with options menu still works after a failed run-tab init', async () => {
  const { doc, click, postRender, state } = h
  state.jobs.jobs = []
  state.registryFails = 1
  await openEdit(h)
  const rows = postRender().rows()
  const btn = rows.find(r => r.querySelector('.vc-opt-btn') && r.dataset.effort === 'high').querySelector('.vc-opt-btn')
  click(btn)
  click(btn.closest('.vc-opt').querySelector('[data-action="prefill"]'))
  await waitUntil(() => doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]'))
  const dialog = doc.getElementById('modelDialogOverlay')
  assert.ok(dialog.classList.contains('has-run-form'), 'menu recovered from failed registry via defensive refetch')
  assert.equal(dialog.dataset.runSlug, '30m_deepseek-v4-flash_effort-high')
  const card = doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="run_candidate"]')
  assert.equal(card.querySelector('[data-arg="profile"]').value, '30m_deepseek-v4-flash_effort-high')
})