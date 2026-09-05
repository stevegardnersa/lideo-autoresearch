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

function json(payload) {
  return Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(payload)) })
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
    if (url === '/api/models' && method === 'PUT') return json(state.put)
    if (url === '/api/models' && method === 'DELETE') return json(state.del)
    if (url === '/api/registry') return json(state.registry)
    if (url === '/api/env-check') return json(state.env)
    if (url === '/bench-list') return json(state.bench)
    if (url.split('?')[0] === '/api/jobs' && method === 'GET') return json(state.jobs)
    if (url === '/api/jobs' && method === 'POST') return json(state.jobsPost)
    if (url === '/api/jobs' && method === 'DELETE') return json(state.clear)
    if (method === 'POST' && /^\/api\/jobs\/[^/]+\/cancel$/.test(url)) return json(state.cancel)
    const detailMatch = url.match(/^\/api\/jobs\/([^/]+)$/)
    if (method === 'GET' && detailMatch) return json(state.jobDetail(detailMatch[1]))
    throw new Error(`unexpected fetch ${method} ${url}`)
  }

  window.EventSource = class {
    constructor(url) {
      this.url = url
      state.es.push(url)
    }
    addEventListener() {}
    close() {}
  }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })

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
  await import(specifier)
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
  assert.equal(rows.length, 16, 'one row per time/mode combo across all effort tiers')
  const checkedCells = rows
    .filter(r => r.querySelector('.vc-check').checked)
    .map(r => `${r.dataset.tb}_${r.dataset.effort}`)
    .sort()
  assert.deepEqual(checkedCells, ['30m_thinking', '60m_none'], 'only existing profiles checked')
})

function postRenderInfo() {
  return {
    title: h.doc.getElementById('dlgTitle').textContent,
    create: h.doc.getElementById('dlgCreate'),
  }
}

test('edit save builds PUT payload with removals and temp changes', async () => {
  const { doc, click, postRender, state } = h
  click(doc.querySelector('[data-settings-toggle]'))
  await new Promise(r => setImmediate(r))
  const els = postRender()
  click(els.list.querySelector('.model-card [data-action="edit"]'))
  await new Promise(r => setImmediate(r))

  const rows = els.rows()
  const removeRow = rows.find(r => r.dataset.tb === '30m' && r.dataset.effort === 'thinking')
  const tempRow = rows.find(r => r.dataset.tb === '60m' && r.dataset.effort === 'none')
  removeRow.querySelector('.vc-check').checked = false
  tempRow.querySelector('.vc-temp').value = '0.66'
  click(els.create)
  await new Promise(r => setImmediate(r))

  const putCall = state.calls.find(c => c.method === 'PUT' && c.url === '/api/models')
  assert.ok(putCall, 'PUT issued')
  assert.equal(putCall.body.old_model, 'deepseek/deepseek-v4-flash')
  assert.equal(putCall.body.new_model, 'deepseek/deepseek-v4-flash')
  assert.ok(Array.isArray(putCall.body.edits) && putCall.body.edits.length === 2)
  const removed = putCall.body.edits.find(e => e.key === '30m_deepseek-v4-flash_thinking')
  assert.equal(removed.keep, false)
  const temped = putCall.body.edits.find(e => e.key === '60m_deepseek-v4-flash_notthinking')
  assert.equal(temped.temperature, 0.66)
  assert.deepEqual(putCall.body.create, [])
  assert.ok(els.dialog.classList.contains('cm-hidden'), 'dialog closed after save')
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
    args: { bench: 'chapter_fast', profile: 'all', mock: true, 'max-samples': 4 },
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