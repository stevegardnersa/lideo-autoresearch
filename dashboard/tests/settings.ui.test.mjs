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

function makeHarness() {
  const state = {
    calls: [],
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
    throw new Error(`unexpected fetch ${method} ${url}`)
  }

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
  assert.equal(rows.length, 4, 'one row per time/mode combo')
  const checks = rows.map(r => r.querySelector('.vc-check').checked)
  assert.deepEqual(checks, [true, false, false, true], 'only existing profiles checked')
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
  const firstRow = rows.find(r => r.dataset.job === '30m_true')
  firstRow.querySelector('.vc-check').checked = false
  rows.find(r => r.dataset.job === '60m_false').querySelector('.vc-temp').value = '0.66'
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