import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { JSDOM } from 'jsdom'

const OPTIONS = {
  ok: true,
  dimensions: {
    system_style: ['dense_faithful', 'teacherly_precise'],
    detail_policy: ['balanced_dense', 'mechanisms_first'],
    length_policy: ['explicit_budget_first', 'outline_then_fill'],
  },
  stage_keys: {
    chapter: ['system_style', 'detail_policy', 'length_policy'],
    composer: ['system_style', 'length_policy'],
  },
}

const VERSIONS = [
  { id: 'chapter-p1', stage: 'chapter', label: 'v1 baseline (profile defaults)', components: {}, system_prompt_fingerprint: '2906d9c5015f7f21', created_at: '2026-09-12T00:00:00Z' },
  { id: 'chapter-p2', stage: 'chapter', label: 'v2 target-aware (default)', components: { length_policy: 'explicit_budget_first' }, system_prompt_fingerprint: 'ce79c4d35f92de9a', created_at: '2026-09-12T00:00:00Z' },
  { id: 'composer-p1', stage: 'composer', label: 'v1 baseline (profile defaults)', components: {}, system_prompt_fingerprint: '95516b89af43c275', created_at: '2026-09-12T00:00:00Z' },
]

function json(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => JSON.parse(JSON.stringify(payload)),
  })
}

function makeHarness() {
  const state = { calls: [], listStatus: 200 }
  const dom = new JSDOM(`<!doctype html><html><head></head><body>
    <div class="page-header"><div class="header-right"><button data-settings-toggle>gear</button></div></div>
  </body></html>`, { url: 'http://localhost/' })
  const { window } = dom
  window.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET'
    state.calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : undefined })
    if (url === '/api/prompts/options') return json(OPTIONS)
    if (url === '/api/prompts' && method === 'GET') {
      if (state.listStatus !== 200) return json({ ok: false, error: 'boom' }, state.listStatus)
      return json({
        ok: true,
        registry_sha: 'sha-1',
        versions: VERSIONS,
        slots: { chapter: { local: 'chapter-p1', production: 'chapter-p1' }, composer: { local: 'composer-p1', production: 'composer-p1' } },
        production_status: [
          { stage: 'chapter', selected_id: 'chapter-p1', reported_id: '', reported_fingerprint: '', reported_at: '', in_sync: false, never_reported: true },
          { stage: 'composer', selected_id: 'composer-p1', reported_id: '', reported_fingerprint: '', reported_at: '', in_sync: false, never_reported: true },
        ],
      })
    }
    if (url === '/api/prompts/sync' && method === 'POST') return json({ ok: true, copied: 'cloud_function/prompts/registry.json', production_version: 'chapter-p1', registry_sha: 'sha-1' })
    if (url === '/api/models/hidden') return json({ hidden: [] })
    throw new Error(`unexpected fetch ${method} ${url}`)
  }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  return { window, doc: window.document, state }
}

let h
beforeEach(async () => {
  h = makeHarness()
  global.window = h.window
  global.document = h.window.document
  global.fetch = h.window.fetch
  global.HTMLElement = h.window.HTMLElement
  global.HTMLInputElement = h.window.HTMLInputElement
  global.Node = h.window.Node
  global.NodeList = h.window.NodeList
  global.MouseEvent = h.window.MouseEvent
  global.Event = h.window.Event
  global.KeyboardEvent = h.window.KeyboardEvent
  const specifier = `../settings.js?test=${Math.random().toString(36).slice(2)}`
  h.mod = await import(specifier)
  h.click = (el) => el.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true, cancelable: true }))
})

afterEach(() => {
  const rs = h && h.mod && h.mod.RUN_STATE
  if (rs && rs.pollTimer) { clearInterval(rs.pollTimer); rs.pollTimer = null }
})

async function openPrompts() {
  const nav = h.doc.querySelector('[data-section="prompts"]')
  assert.ok(nav, 'Prompts nav item exists')
  assert.equal(nav.disabled, false)
  h.click(nav)
  await new Promise(r => setImmediate(r))
  await new Promise(r => setImmediate(r))
}

test('Prompts section renders slot assignment and version table', async () => {
  await openPrompts()
  const panel = h.doc.getElementById('promptsPanel')
  assert.match(panel.textContent, /chapter-p1/)
  assert.match(panel.textContent, /chapter-p2/)
  assert.match(panel.textContent, /composer-p1/)
  assert.match(panel.textContent, /Never reported/)
  const prod = h.doc.getElementById('promptProd_chapter')
  assert.equal(prod.value, 'chapter-p1')
  const local = h.doc.getElementById('promptLocal_chapter')
  assert.equal(local.value, 'chapter-p1')
  const table = panel.querySelector('table.prompt-table')
  assert.ok(table)
  assert.ok(table.querySelector('caption'))
})

test('Sync button calls the sync endpoint once and reports the result', async () => {
  await openPrompts()
  const syncBtn = h.doc.querySelector('[data-action="sync"]')
  assert.ok(syncBtn)
  h.click(syncBtn)
  await new Promise(r => setImmediate(r))
  await new Promise(r => setImmediate(r))
  const syncCalls = h.state.calls.filter(c => c.url === '/api/prompts/sync')
  assert.equal(syncCalls.length, 1)
  assert.match(h.doc.getElementById('promptsBanner').textContent, /Synced registry/)
})

test('Prompts section shows an error with Retry when the API fails', async () => {
  h.state.listStatus = 500
  await openPrompts()
  const panel = h.doc.getElementById('promptsPanel')
  assert.match(panel.textContent, /Failed to load prompts/)
  assert.ok(h.doc.getElementById('promptsRetry'))
})
