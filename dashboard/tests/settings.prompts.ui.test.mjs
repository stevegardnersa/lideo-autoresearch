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
  placeholders: {
    chapter: { system: ['book_title', 'components'], user: ['book_title', 'target_words', 'source_md'] },
    composer: { system: ['book_title', 'components'], user: ['book_title', 'chapter_summaries'] },
  },
  component_text: {
    chapter: { system_style: { dense_faithful: 'DENSE LIBRARY TEXT' }, detail_policy: {}, length_policy: {} },
    composer: { system_style: {}, length_policy: {} },
  },
  models: ['deepseek/deepseek-v4-flash'],
  dimension_help: {
    system_style: 'Overall voice and identity of the summarizer.',
    detail_policy: 'How much explanatory depth to keep.',
    length_policy: 'How the word/sentence budget is communicated.',
  },
  profiles_by_model: { 'deepseek/deepseek-v4-flash': ['30m_deepseek-v4-flash_thinking'] },
}

const COMPARISONS = [
  { id: '30m_b_bench-v2_20260201T000000Z', profile: '30m_b', bench: 'bench-v2', winner: 'readable', generated_at: '2026-02-01T00:00:00Z', job_id: 'job-xyz', has_log: true },
  { id: '30m_a_bench-v1_20260101T000000Z', profile: '30m_a', bench: 'bench-v1', winner: 'sentence', generated_at: '2026-01-01T00:00:00Z', job_id: '', has_log: false },
]

const VERSIONS = [
  { id: 'chapter-p1', stage: 'chapter', label: 'v1 baseline (profile defaults)', scope: { kind: 'general' }, components: {}, system_prompt_fingerprint: '2906d9c5015f7f21', created_at: '2026-09-12T00:00:00Z' },
  { id: 'chapter-p2', stage: 'chapter', label: 'v2 target-aware (default)', scope: { kind: 'general' }, components: { length_policy: 'explicit_budget_first' }, system_prompt_fingerprint: 'ce79c4d35f92de9a', created_at: '2026-09-12T00:00:00Z' },
  { id: 'composer-p1', stage: 'composer', label: 'v1 baseline (profile defaults)', scope: { kind: 'general' }, components: {}, system_prompt_fingerprint: '95516b89af43c275', created_at: '2026-09-12T00:00:00Z' },
]

const DEPLOY_TOOL = {
  id: 'deploy_cf',
  group: 'candidates',
  title: 'Deploy Cloud Function',
  description: 'Sync the prompt registry into the bundle, run gcloud functions deploy, and smoke-test.',
  script: 'tools/deploy_cf.py',
  runtimeClass: 'write',
  outputs: ['data/prompts/cf_last_report.json'],
  args: [
    { name: 'project', label: 'GCP project', type: 'text', hint: 'GCP project id. Defaults to env or gcloud config.' },
    { name: 'region', label: 'Region', type: 'text', default: 'us-central1', hint: 'Cloud Functions region.' },
    { name: 'function', label: 'Function name', type: 'text', default: 'summarize' },
    { name: 'memory', label: 'Memory', type: 'enum', default: '1024Mi', choices: ['512Mi', '1024Mi'] },
    { name: 'timeout', label: 'Timeout (seconds)', type: 'int', default: 3600, min: 60, max: 3600 },
    { name: 'debug-log-bodies', label: 'Log request/response bodies', type: 'bool', default: false, hint: 'Master gate for CF body logging (sets CF_DEBUG_LOG_BODIES).' },
    { name: 'confirm', label: 'Confirmed (required to deploy)', type: 'bool', default: true, fixed: true, advanced: true, ui: false },
    { name: 'no-sync', label: 'Skip bundle sync', type: 'bool', default: false, advanced: true },
    { name: 'dry-run', label: 'Dry run', type: 'bool', default: false, advanced: true },
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

function makeHarness() {
  const state = { calls: [], listStatus: 200, lastCreate: null, lastRevise: null, lastSlot: null, promptCalls: 0, slotSha: 'sha-1', slotWrites: 0 }
  const dom = new JSDOM(`<!doctype html><html><head></head><body>
    <div class="page-header"><div class="header-right"><button data-settings-toggle>gear</button></div></div>
  </body></html>`, { url: 'http://localhost/' })
  const { window } = dom
  window.confirm = () => true
  window.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET'
    state.calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : undefined })
    if (url === '/api/prompts/options') return json(OPTIONS)
    if (url === '/api/prompts/preview-draft' && method === 'POST') {
      return json({ ok: true, system_prompt: 'SYS PREVIEW', user_prompt: 'USER PREVIEW', fingerprint: 'fp-1', warnings: [], placeholders: { system: [], user: [] } })
    }
    if (url === '/api/prompts' && method === 'POST') {
      state.lastCreate = state.calls[state.calls.length - 1].body
      return json({ ok: true, version: { id: 'chapter-p9', stage: 'chapter', label: 'new' } }, 201)
    }
    if (/\/api\/prompts\/[^/]+\/revise$/.test(url) && method === 'POST') {
      state.lastRevise = state.calls[state.calls.length - 1].body
      return json({ ok: true, version: { id: 'chapter-p10', stage: 'chapter', label: 'edited' } }, 201)
    }
    if (url === '/api/prompts/slots' && method === 'POST') {
      const body = state.calls[state.calls.length - 1].body
      state.lastSlot = body
      if (state.slotSha && body.expected_sha && body.expected_sha !== state.slotSha) {
        return json({ ok: false, error: 'registry_changed', code: 409, message: 'Registry changed since it was loaded; reload and retry.' }, 409)
      }
      state.slotWrites += 1
      state.slotSha = `sha-${state.slotWrites + 1}`
      return json({ ok: true, slots: {}, registry_sha: state.slotSha })
    }
    const cmpMatch = url.match(/^\/api\/prompts\/comparisons\/(.+)$/)
    if (cmpMatch && method === 'GET') {
      const comp = COMPARISONS.find(c => c.id === decodeURIComponent(cmpMatch[1]))
      if (!comp) return json({ ok: false, error: 'unknown_comparison', code: 404 }, 404)
      return json({
        ok: true,
        id: comp.id,
        summary: comp,
        markdown: `# Base prompt sweep — ${comp.profile}\n\nRecommendation: adopt base \`${comp.winner}\`.`,
      })
    }
    if (/^\/api\/jobs\/[^/]+$/.test(url) && method === 'GET') {
      const id = url.split('/').pop()
      return json({
        ok: true,
        job: { id, toolId: 'sweep_base_prompts', status: 'finished', startedAt: '2026-02-01T00:00:00Z', finishedAt: '2026-02-01T00:05:00Z', logPath: `/tmp/${id}.log` },
      })
    }
    if (url === '/api/prompts' && method === 'GET') {
      if (state.listStatus !== 200) return json({ ok: false, error: 'boom' }, state.listStatus)
      return json({
        ok: true,
        registry_sha: 'sha-1',
        versions: VERSIONS,
        comparisons: COMPARISONS,
        models: OPTIONS.models,
        slots: {
          chapter: { general: { local: 'chapter-p1', production: 'chapter-p1' }, models: {} },
          composer: { general: { local: 'composer-p1', production: 'composer-p1' }, models: {} },
        },
        production_status: [
          { stage: 'chapter', model: '', selected_id: 'chapter-p1', reported_id: '', reported_fingerprint: '', reported_at: '', in_sync: false, never_reported: true },
          { stage: 'composer', model: '', selected_id: 'composer-p1', reported_id: '', reported_fingerprint: '', reported_at: '', in_sync: false, never_reported: true },
        ],
      })
    }
    if (url === '/api/prompts/sync' && method === 'POST') return json({ ok: true, copied: 'cloud_function/prompts/registry.json', production_version: 'chapter-p1', registry_sha: 'sha-1' })
    if (url === '/api/registry' && method === 'GET') return json({ ok: true, tools: [DEPLOY_TOOL] })
    if (url === '/api/jobs?limit=60' && method === 'GET') {
      const job = state.lastJob
      return json({ ok: true, jobs: job ? [{ id: 'job-deploy-1', toolId: job.toolId, status: 'queued' }] : [] })
    }
    if (url === '/api/jobs' && method === 'POST') {
      state.lastJob = state.calls[state.calls.length - 1].body
      return json({ job: { id: 'job-deploy-1', toolId: 'deploy_cf', status: 'queued' } }, 201)
    }
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

test('Deploy opens a params modal with hints and posts deploy_cf on submit', async () => {
  await openPrompts()
  const deployBtn = h.doc.querySelector('[data-action="deploy"]')
  assert.ok(deployBtn)
  h.click(deployBtn)
  await flush()
  await flush()
  const overlay = h.doc.getElementById('modelDialogOverlay')
  assert.equal(overlay.classList.contains('cm-hidden'), false, 'modal opens')
  assert.equal(overlay.classList.contains('deploy-form'), true, 'deploy-form mode')

  const card = h.doc.querySelector('#dlgRunWidget .mm-tool-widget[data-tool="deploy_cf"]')
  assert.ok(card, 'deploy card rendered in the modal')
  assert.ok(card.querySelector('[data-arg="project"]'), 'project param')
  assert.ok(card.querySelector('[data-arg="debug-log-bodies"]'), 'debug-log-bodies param')
  assert.match(card.textContent, /CF_DEBUG_LOG_BODIES/, 'debug-log hint shown')
  assert.ok(card.querySelector('.field-hint'), 'field hints rendered')
  assert.equal(card.querySelector('[data-arg="confirm"]'), null, 'internal confirm arg is hidden')

  const submit = card.querySelector('.tool-run-submit')
  assert.equal(submit.textContent, 'Deploy')
  h.click(submit)
  await flush()
  await flush()

  const jobCalls = h.state.calls.filter(c => c.url === '/api/jobs' && c.method === 'POST')
  assert.equal(jobCalls.length, 1)
  assert.equal(jobCalls[0].body.toolId, 'deploy_cf')
  assert.equal(overlay.classList.contains('deploy-mode'), true, 'progress mode after submit')
})

test('Prompts section shows an error with Retry when the API fails', async () => {
  h.state.listStatus = 500
  await openPrompts()
  const panel = h.doc.getElementById('promptsPanel')
  assert.match(panel.textContent, /Failed to load prompts/)
  assert.ok(h.doc.getElementById('promptsRetry'))
})

const flush = async () => { await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)) }
const wait = (ms) => new Promise(r => setTimeout(r, ms))

function setValue(doc, id, value) {
  const el = doc.getElementById(id)
  el.value = value
  el.dispatchEvent(new h.window.Event('input', { bubbles: true }))
  el.dispatchEvent(new h.window.Event('change', { bubbles: true }))
}

test('+ New version opens the dialog and previews a server-rendered draft', async () => {
  await openPrompts()
  const btn = h.doc.querySelector('[data-action="new-version"]')
  assert.ok(btn, '+ New version button exists')
  h.click(btn)
  await flush()
  await wait(450)
  const overlay = h.doc.getElementById('promptVersionOverlay')
  assert.equal(overlay.classList.contains('cm-hidden'), false)
  assert.equal(h.doc.getElementById('pvSystemPreview').textContent, 'SYS PREVIEW')
  assert.equal(h.doc.getElementById('pvUserPreview').textContent, 'USER PREVIEW')
  setValue(h.doc, 'pvLabel', 'my experiment')
  await flush()
  h.click(h.doc.getElementById('pvSave'))
  await flush()
  assert.ok(h.state.lastCreate, 'create called')
  assert.equal(h.state.lastCreate.label, 'my experiment')
  assert.equal(overlay.classList.contains('cm-hidden'), true)
  assert.match(h.doc.getElementById('promptsBanner').textContent, /Created chapter-p9/)
})

test('Edit is copy-on-write and locks the stage', async () => {
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="edit"][data-id="chapter-p2"]'))
  await flush()
  await wait(450)
  assert.equal(h.doc.getElementById('pvStage').disabled, true)
  assert.equal(h.doc.getElementById('pvLabel').value, 'v2 target-aware (default)')
  h.click(h.doc.getElementById('pvSave'))
  await flush()
  assert.ok(h.state.lastRevise, 'revise called')
  assert.equal(h.state.lastRevise.label, 'v2 target-aware (default)')
  assert.equal(h.state.lastCreate, null, 'create not used for an edit')
})

test('Clone opens the dialog without window.prompt', async () => {
  let prompted = false
  h.window.prompt = () => { prompted = true; return 'x' }
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="clone"][data-id="chapter-p2"]'))
  await flush()
  assert.equal(prompted, false)
  assert.equal(h.doc.getElementById('promptVersionOverlay').classList.contains('cm-hidden'), false)
  assert.match(h.doc.getElementById('pvLabel').value, /\(copy\)/)
})

test('specific-model scope posts a scoped version', async () => {
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  const kind = h.doc.getElementById('pvScopeKind')
  kind.value = 'model'
  kind.dispatchEvent(new h.window.Event('change', { bubbles: true }))
  await flush()
  assert.equal(h.doc.getElementById('pvScopeExtra').classList.contains('cm-hidden'), false)
  assert.ok(h.doc.querySelector('#pvScopeModel option[value="deepseek/deepseek-v4-flash"]'))
  h.doc.getElementById('pvScopeModel').value = 'deepseek/deepseek-v4-flash'
  setValue(h.doc, 'pvLabel', 'flash tuned')
  h.click(h.doc.getElementById('pvSave'))
  await flush()
  assert.deepEqual(h.state.lastCreate.scope, { kind: 'model', model: 'deepseek/deepseek-v4-flash' })
})

test('client validation blocks a chapter user template without source_md', async () => {
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  setValue(h.doc, 'pvLabel', 'bad template')
  setValue(h.doc, 'pvUserTemplate', '{{book_title}} only')
  await flush()
  h.click(h.doc.getElementById('pvSave'))
  await flush()
  assert.equal(h.state.lastCreate, null)
  const err = h.doc.getElementById('pvUserTemplateError')
  assert.equal(err.classList.contains('cm-hidden'), false)
  assert.match(err.textContent, /source_md/)
})

test('Esc closes the version dialog but keeps settings open', async () => {
  h.click(h.doc.querySelector('[data-settings-toggle]'))
  await flush()
  assert.equal(h.doc.getElementById('settingsOverlay').classList.contains('cm-hidden'), false)
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  const overlay = h.doc.getElementById('promptVersionOverlay')
  assert.equal(overlay.classList.contains('cm-hidden'), false)
  h.doc.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  await flush()
  assert.equal(overlay.classList.contains('cm-hidden'), true)
  assert.equal(h.doc.getElementById('settingsOverlay').classList.contains('cm-hidden'), false)
})

test('component dimensions render help tooltips', async () => {
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  const field = h.doc.querySelector('#pvComponents .field[data-dim="system_style"]')
  assert.ok(field, 'system_style field rendered')
  const help = field.querySelector('.prompt-help')
  assert.ok(help, 'help icon rendered')
  assert.equal(help.getAttribute('title'), 'Overall voice and identity of the summarizer.')
  assert.match(help.getAttribute('aria-label'), /voice and identity/)
  const detail = h.doc.querySelector('#pvComponents .field[data-dim="detail_policy"] .prompt-help')
  assert.equal(detail.getAttribute('title'), 'How much explanatory depth to keep.')
})

test('closing an untouched version dialog does not prompt to discard', async () => {
  let confirmCalls = 0
  h.window.confirm = () => { confirmCalls += 1; return true }
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  h.click(h.doc.getElementById('pvCancel'))
  await flush()
  assert.equal(confirmCalls, 0, 'no discard confirm when nothing changed')
  assert.equal(h.doc.getElementById('promptVersionOverlay').classList.contains('cm-hidden'), true)
})

test('closing an edited version dialog prompts to discard', async () => {
  let confirmCalls = 0
  h.window.confirm = () => { confirmCalls += 1; return true }
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  setValue(h.doc, 'pvLabel', 'changed this')
  await flush()
  h.click(h.doc.getElementById('pvCancel'))
  await flush()
  assert.equal(confirmCalls, 1, 'discard confirm shown once a field changed')
})

test('reverting a field back to its original value does not prompt to discard', async () => {
  let confirmCalls = 0
  h.window.confirm = () => { confirmCalls += 1; return true }
  await openPrompts()
  h.click(h.doc.querySelector('[data-action="new-version"]'))
  await flush()
  await wait(450)
  const original = h.doc.getElementById('pvLabel').value
  setValue(h.doc, 'pvLabel', 'temporary edit')
  await flush()
  setValue(h.doc, 'pvLabel', original)
  await flush()
  h.click(h.doc.getElementById('pvCancel'))
  await flush()
  assert.equal(confirmCalls, 0, 'no discard confirm after reverting the only change')
})

test('Save writes only the changed slot and reports saved', async () => {
  await openPrompts()
  setValue(h.doc, 'promptLocal_chapter', 'chapter-p2')
  const save = h.doc.querySelector('[data-action="save-slot"][data-stage="chapter"]')
  assert.ok(save)
  assert.equal(save.disabled, false, 'Save enabled after a slot change')
  h.click(save)
  await flush()
  const posts = h.state.calls.filter(c => c.url === '/api/prompts/slots' && c.method === 'POST')
  assert.equal(posts.length, 1, 'only the changed local slot is written')
  assert.equal(posts[0].body.slot, 'local')
  assert.equal(posts[0].body.version_id, 'chapter-p2')
  assert.equal(posts[0].body.expected_sha, 'sha-1')
  assert.match(h.doc.getElementById('promptsBanner').textContent, /Saved chapter slots/)
})

test('Save of both slots threads the registry sha between writes', async () => {
  await openPrompts()
  setValue(h.doc, 'promptLocal_chapter', 'chapter-p2')
  setValue(h.doc, 'promptProd_chapter', 'chapter-p2')
  h.click(h.doc.querySelector('[data-action="save-slot"][data-stage="chapter"]'))
  await flush()
  const posts = h.state.calls.filter(c => c.url === '/api/prompts/slots' && c.method === 'POST')
  assert.equal(posts.length, 2)
  assert.deepEqual(posts.map(p => p.body.slot), ['local', 'production'])
  assert.equal(posts[0].body.expected_sha, 'sha-1')
  assert.equal(posts[1].body.expected_sha, 'sha-2', 'second write uses the sha returned by the first')
  assert.match(h.doc.getElementById('promptsBanner').textContent, /Saved chapter slots/)
})

test('a stale registry sha surfaces a reload banner instead of a hard error', async () => {
  await openPrompts()
  h.state.slotSha = 'sha-99'
  setValue(h.doc, 'promptLocal_chapter', 'chapter-p2')
  h.click(h.doc.querySelector('[data-action="save-slot"][data-stage="chapter"]'))
  await flush()
  assert.match(h.doc.getElementById('promptsBanner').textContent, /Registry changed elsewhere/)
  assert.match(h.doc.getElementById('promptsPanel').textContent, /chapter-p1/)
})

test('Base prompt comparisons render below Versions and open a summary drawer', async () => {
  await openPrompts()
  const blocks = [...h.doc.querySelectorAll('#promptsPanel .prompt-block')]
  const titles = blocks.map(b => (b.querySelector('.settings-section-title') || {}).textContent)
  assert.equal(titles.indexOf('Base prompt comparisons'), titles.indexOf('Versions') + 1, titles.join(' | '))

  const rows = h.doc.querySelectorAll('#promptsPanel .comparison-row')
  assert.equal(rows.length, 2)
  assert.equal(rows[0].dataset.id, '30m_b_bench-v2_20260201T000000Z')
  assert.match(rows[1].textContent, /sentence/)

  h.click(rows[1])
  await flush()
  const panel = h.doc.getElementById('logPanel')
  assert.equal(panel.classList.contains('cm-hidden'), false)
  assert.match(panel.textContent, /30m_a · bench-v1/)
  assert.match(panel.textContent, /Recommendation: adopt base/)
  assert.match(h.doc.querySelector('.log-panel-status').textContent, /Comparison summary/)
  // No job log for this run, so the summary toggle stays hidden.
  assert.equal(h.doc.getElementById('panelSummary').classList.contains('cm-hidden'), true)
})

test('comparison run with a job log opens the log by default with a Summary toggle', async () => {
  await openPrompts()
  const rows = h.doc.querySelectorAll('#promptsPanel .comparison-row')
  h.click(rows[0])
  await flush()
  const panel = h.doc.getElementById('logPanel')
  assert.equal(panel.classList.contains('cm-hidden'), false)
  assert.match(panel.textContent, /30m_b · bench-v2/)

  const toggle = h.doc.getElementById('panelSummary')
  assert.equal(toggle.classList.contains('cm-hidden'), false)
  assert.equal(toggle.textContent, 'Summary')

  h.click(toggle)
  await flush()
  assert.match(panel.textContent, /Recommendation: adopt base/)
  assert.match(h.doc.querySelector('.log-panel-status').textContent, /Comparison summary/)
  assert.equal(toggle.textContent, 'Live log')

  h.click(toggle)
  await flush()
  assert.equal(toggle.textContent, 'Summary')
})
