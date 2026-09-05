const api = (path, opts = {}) => fetch(path, {
  headers: { 'Content-Type': 'application/json' },
  ...opts,
}).then(r => r.json()).then(j => {
  if (!j || j.ok === false) {
    let msg = (j && j.error) || `request failed (${path})`
    if (j && j.fieldErrors) {
      const first = Object.values(j.fieldErrors)[0]
      if (first) msg = first
    }
    throw new Error(msg)
  }
  return j
})

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function fmtQuality(v) {
  return v == null ? '–' : v.toFixed(3)
}

function fmtDate(iso) {
  if (!iso) return 'never'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toISOString().slice(0, 10)
}

const GEAR_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`

const MODAL_TEMPLATE = `
<div class="settings-overlay cm-hidden" id="settingsOverlay">
  <div class="settings-modal">
    <header class="settings-modal-header">
      <div class="settings-modal-title">Settings</div>
      <button class="settings-close" id="settingsClose" aria-label="Close settings">&times;</button>
    </header>
    <div class="settings-body">
      <nav class="settings-nav">
        <button class="settings-nav-item active" data-section="models">Models</button>
        <button class="settings-nav-item" data-section="run">Run data</button>
        <button class="settings-nav-item" data-section="upcoming" disabled title="Coming soon">Prompts</button>
        <button class="settings-nav-item" data-section="upcoming" disabled title="Coming soon">Judges</button>
      </nav>
      <div class="settings-pane">
        <section class="settings-section active" id="settingsModelsSection">
          <div class="models-head">
            <div>
              <div class="settings-section-title">Models</div>
              <div class="settings-section-sub">Candidate profiles each model is run through. Probes the model live before adding.</div>
            </div>
            <button class="model-add-btn" id="modelAddBtn">+ Add model</button>
          </div>
          <div class="models-list" id="modelsList">
            <div class="placeholder-text">Loading models...</div>
          </div>
        </section>
        <section class="settings-section" id="settingsUpcomingSection">
          <div class="placeholder-text">This settings page is coming soon.</div>
        </section>
        <section class="settings-section" id="settingsRunSection">
          <div class="runs-head">
            <div>
              <div class="settings-section-title">Run data</div>
              <div class="settings-section-sub">Run every corpus, candidate, harness, and maintenance script — no CLI. Long jobs stream live output.</div>
            </div>
          </div>
          <div class="run-banner cm-hidden" id="runBanner" role="alert"></div>
          <div class="run-workbench">
            <div class="tool-column">
              <div id="toolList"><div class="placeholder-text">Loading tools...</div></div>
            </div>
            <div class="job-column">
              <div class="job-head">
                <div class="job-count" id="jobCount">No runs</div>
                <div class="job-head-actions">
                  <button class="mini-btn" id="jobRefresh">Refresh</button>
                  <button class="mini-btn" id="jobClear">Clear finished</button>
                </div>
              </div>
              <div class="job-list" id="jobsList"></div>
              <div class="job-offline cm-hidden" id="jobOffline">Cannot reach dashboard server &mdash; is <code>npm run dev</code> running on :3001?</div>
            </div>
          </div>
          <datalist id="benchList"></datalist>
        </section>
      </div>
    </div>
  </div>
</div>

<div class="settings-overlay cm-hidden" id="modelDialogOverlay">
  <div class="settings-dialog" role="dialog" aria-modal="true" data-run-slug="">
    <header class="settings-modal-header">
      <div class="settings-modal-title" id="dlgTitle">Add model</div>
      <button class="settings-close" id="dlgClose" aria-label="Close dialog">&times;</button>
    </header>
    <div class="dialog-body">
      <div class="dlg-left">
        <div class="field">
          <label for="dlgModel">Model ID</label>
          <input id="dlgModel" type="text" placeholder="provider/model   e.g. qwen3.8-v2-max" spellcheck="false" autocomplete="off" />
          <div class="field-hint">Provider slug and model slug separated by a slash, matching your router config.</div>
        </div>
        <div class="field">
          <label for="dlgProviderRoute">Provider route <span class="field-optional">optional</span></label>
          <textarea id="dlgProviderRoute" rows="2" spellcheck="false" placeholder='{"order":["qwen"]}'></textarea>
          <div class="field-hint">JSON routing config applied to chapter and composer stages.</div>
        </div>
        <div class="field" id="dlgBudgetsField">
          <label>Time budgets</label>
          <div class="cb-row">
            <label class="cb"><input type="checkbox" id="tb30" checked /> 30m</label>
            <label class="cb"><input type="checkbox" id="tb60" checked /> 60m</label>
          </div>
        </div>
        <div class="field cm-hidden" id="dlgVariantsField">
          <div class="field-label-row">
            <label>Variants</label>
            <span class="field-hint">Effort tier per time budget. Check to keep a profile, uncheck to remove it. Checking an absent row creates it.</span>
          </div>
          <table class="variant-table">
            <thead><tr><th></th><th>Time</th><th>Effort</th><th>temp</th><th>max tokens</th><th>status</th><th></th></tr></thead>
            <tbody id="dlgVariantRows"></tbody>
          </table>
        </div>
        <div class="dlg-preview cm-hidden" id="dlgPreview"></div>
        <div class="dlg-notice cm-hidden" id="dlgNotice"></div>
        <div class="dlg-error cm-hidden" id="dlgError"></div>
      </div>
      <aside class="dlg-run cm-hidden" id="dlgRunForm" aria-label="Run this profile with options">
        <div class="dlg-run-head">
          <span class="dlg-run-title" id="dlgRunTitle">Run with options</span>
          <button type="button" class="dlg-run-close" id="dlgRunClose" aria-label="Remove run form">&times;</button>
        </div>
        <div class="dlg-run-widget" id="dlgRunWidget"></div>
      </aside>
    </div>
    <footer class="dialog-footer">
      <button class="dlg-btn dlg-btn-ghost" id="dlgCancel">Cancel</button>
      <button class="dlg-btn dlg-btn-primary" id="dlgCreate" disabled>Create</button>
    </footer>
  </div>
</div>`

const EFFORT_ORDER = ['thinking', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const EFFORT_LABEL = { thinking: 'think', none: 'plain' }
const BUDGETS = ['30m', '60m']
const EFFORT_FRACTION = { none: 0, minimal: 0.1, low: 0.2, medium: 0.5, high: 0.8, xhigh: 0.95, max: 0.95 }

function effortOf(key) {
  const k = String(key)
  if (k.endsWith('_thinking')) return 'thinking'
  if (k.endsWith('_notthinking')) return 'none'
  const m = k.match(/_(effort-[a-z]+)$/)
  return m ? m[1].replace(/^effort-/, '') : 'plain'
}

function effortLabel(effort) {
  return EFFORT_LABEL[effort] || effort
}

function effortDefaultMaxTokens(effort) {
  const f = EFFORT_FRACTION[effort] || 0
  if (f <= 0) return DEFAULTS.max_tokens
  return Math.min(Math.ceil(DEFAULTS.max_tokens / (1 - f)), 163840)
}

function modelCells(m) {
  const byCell = new Map()
  for (const p of m.profiles || []) {
    const e = effortOf(p.slug)
    if (EFFORT_ORDER.includes(e)) byCell.set(`${p.time_budget}_${e}`, p)
  }
  const cells = []
  for (const tb of BUDGETS) {
    for (const ef of EFFORT_ORDER) {
      const p = byCell.get(`${tb}_${ef}`)
      if (p) cells.push({ tb, effort: ef, profile: p })
    }
  }
  return cells
}

function renderModelsPage(models) {
  const list = document.getElementById('modelsList')
  if (!models.length) {
    list.innerHTML = '<div class="placeholder-text">No models registered yet. Add your first model to start probing capabilities.</div>'
    return
  }
  list.innerHTML = models.map(m => {
    const chips = modelCells(m).map(({ tb, effort, profile: p }) => {
      const statusCls = p.status === 'tested' ? 'is-tested' : 'is-pending'
      const statusTxt = p.status === 'tested' ? 'tested' : 'pending'
      return `<span class="profile-chip ${statusCls}" title="${esc(p.slug)}">
        <span class="pc-budget ${tb === '60m' ? 'is-60' : ''}">${tb}</span><span class="pc-mode">${effortLabel(effort)}</span>
        <span class="pc-status">${statusTxt}</span>
      </span>`
    }).join('')
    const tested = m.profiles.some(p => p.status === 'tested')
    const det = fmtQuality(m.best_quality_det)
    const llm = fmtQuality(m.best_quality_llm)
    return `<div class="model-card" data-model="${esc(m.model)}">
      <div class="model-row">
        <div class="model-dot"></div>
        <div class="model-main">
          <div class="model-name">${esc(m.model)}</div>
          <div class="model-sub">${m.profiles.length} profile${m.profiles.length === 1 ? '' : 's'} &middot; ${m.runs_count} run${m.runs_count === 1 ? '' : 's'} &middot; last tested ${fmtDate(m.last_tested)}</div>
        </div>
        <div class="model-stats">
          <div class="ms-col"><span class="ms-k">det Q</span><span class="ms-v ${tested ? '' : 'ms-muted'}">${det}</span></div>
          <div class="ms-col"><span class="ms-k">LLM Q</span><span class="ms-v ${tested ? '' : 'ms-muted'}">${llm}</span></div>
        </div>
        <div class="model-actions">
          <button class="mini-btn" data-action="edit">Edit</button>
          <button class="mini-btn mini-btn-danger" data-action="delete">Delete</button>
        </div>
      </div>
      <div class="model-chip-row">${chips}</div>
    </div>`
  }).join('')
}

async function loadModels() {
  const list = document.getElementById('modelsList')
  try {
    const { models } = await api('/api/models')
    renderModelsPage(models)
  } catch (e) {
    list.innerHTML = `<div class="placeholder-text">Failed to load models: ${esc(e.message)}</div>`
  }
}

function setPreview(html) {
  const el = document.getElementById('dlgPreview')
  if (!html) { el.classList.add('cm-hidden'); el.innerHTML = ''; return }
  el.innerHTML = html
  el.classList.remove('cm-hidden')
}

function setError(msg) {
  const el = document.getElementById('dlgError')
  const notice = document.getElementById('dlgNotice')
  if (notice) { notice.classList.add('cm-hidden'); notice.innerHTML = '' }
  if (!msg) { el.classList.add('cm-hidden'); el.innerHTML = ''; return }
  el.innerHTML = `<strong>${esc(msg)}</strong>`
  el.classList.remove('cm-hidden')
}

function setDialogNotice(msg, tone) {
  const el = document.getElementById('dlgNotice')
  const err = document.getElementById('dlgError')
  if (!msg) {
    if (el) { el.classList.add('cm-hidden'); el.innerHTML = '' }
    return
  }
  if (err) { err.classList.add('cm-hidden'); err.innerHTML = '' }
  el.classList.remove('cm-hidden')
  el.classList.toggle('dlg-notice-err', tone === 'error')
  el.innerHTML = msg
}

function probeResultHtml(model, probe, created) {
  const box = (ok, label) => `<span class="probe-badge ${ok ? 'probe-ok' : 'probe-fail'}">${ok ? '\u2713' : '\u2717'} ${esc(label)}</span>`
  const schemaLine = probe.schema == null ? '' : box(!!probe.schema, 'JSON schema')
  const effortChips = (probe.efforts && probe.efforts.length)
    ? `<span class="probe-badge probe-ok">efforts: ${probe.efforts.map(e => esc(e === 'none' ? 'none (plain)' : e)).join(', ')}</span>`
    : ''
  const legacyLine = (probe.thinking != null || probe.notthinking != null)
    ? `${box(probe.thinking === true, 'legacy thinking')}${box(probe.notthinking === true, 'legacy non-thinking')}`
    : ''
  const profiles = created.length
    ? `<div class="probe-profiles">${created.map(c => `<span class="probe-profile">${esc(c)}</span>`).join('')}</div>`
    : '<div class="probe-none">No candidate profiles would be created.</div>'
  return `<div class="probe-results">${[schemaLine, effortChips, legacyLine].filter(Boolean).join('')}</div>
  <div class="probe-create-label">${created.length ? `Will create ${created.length} profile${created.length === 1 ? '' : 's'} for ${esc(model)}:` : ''}</div>${profiles}`
}

const DEFAULTS = { temperature: 0.2, max_tokens: 8192 }

function openAddDialog() {
  const overlay = document.getElementById('modelDialogOverlay')
  const title = document.getElementById('dlgTitle')
  const createBtn = document.getElementById('dlgCreate')
  clearInlineRunWidget()
  title.textContent = 'Add model'
  document.getElementById('dlgModel').value = ''
  document.getElementById('dlgModel').readOnly = false
  document.getElementById('dlgProviderRoute').value = ''
  document.getElementById('dlgBudgetsField').classList.remove('cm-hidden')
  document.getElementById('tb30').checked = true
  document.getElementById('tb60').checked = true
  document.getElementById('dlgVariantsField').classList.add('cm-hidden')
  setPreview('')
  setError('')
  setDialogNotice('')
  createBtn.textContent = 'Probe & preview'
  createBtn.classList.remove('dlg-btn-confirm')
  createBtn.disabled = true
  overlay.dataset.mode = 'add'
  overlay.classList.remove('cm-hidden')
  document.getElementById('dlgModel').focus()
}

function parseRoute(raw) {
  const v = (raw || '').trim()
  if (!v) return undefined
  try { return JSON.parse(v) } catch { return undefined }
}

function variantRowHtml({ tb, effort, existing }) {
  const temp = existing && existing.temperature != null ? existing.temperature : DEFAULTS.temperature
  const maxT = existing && existing.max_tokens != null ? existing.max_tokens : effortDefaultMaxTokens(effort)
  const status = existing ? (existing.status === 'tested' ? 'tested' : 'pending') : 'not created'
  const opt = existing
    ? `<button type="button" class="vc-opt-btn" data-slug="${esc(existing.slug)}" aria-haspopup="menu" aria-expanded="false" aria-label="Options for ${esc(existing.slug)}" title="Profile options">\u22EF</button>
      <div class="vc-opt-menu cm-hidden" role="menu">
        <button type="button" class="vc-opt-item" data-action="run" role="menuitem">Run candidate now</button>
        <button type="button" class="vc-opt-item" data-action="prefill" role="menuitem">Run with options\u2026</button>
        <button type="button" class="vc-opt-item" data-action="judge" role="menuitem">Re-judge (LLM)\u2026</button>
        <button type="button" class="vc-opt-item" data-action="agent" role="menuitem">Autoresearch agent\u2026</button>
      </div>`
    : ''
  return `<tr data-tb="${tb}" data-effort="${effort}" data-existing="${existing ? '1' : '0'}">
    <td><input type="checkbox" class="vc-check" ${existing ? 'checked' : ''} /></td>
    <td class="vc-tb">${tb}</td>
    <td class="vc-mode">${effortLabel(effort)}</td>
    <td><input type="number" step="0.1" min="0" max="2" class="vc-temp" value="${temp}" /></td>
    <td><input type="number" step="256" min="0" class="vc-max" value="${maxT}" /></td>
    <td class="vc-status ${existing ? 'vc-has' : 'vc-none'}">${status}</td>
    <td class="vc-opt">${opt}</td>
  </tr>`
}

function openEditDialog(model) {
  const overlay = document.getElementById('modelDialogOverlay')
  const title = document.getElementById('dlgTitle')
  const createBtn = document.getElementById('dlgCreate')
  clearInlineRunWidget()
  overlay.dataset.editingModel = JSON.stringify(model)
  title.textContent = `Edit ${model.model}`
  document.getElementById('dlgModel').value = model.model
  document.getElementById('dlgModel').readOnly = false
  const route = (model.profiles[0] && model.profiles[0].provider_route) || ''
  document.getElementById('dlgProviderRoute').value = route
  document.getElementById('dlgBudgetsField').classList.add('cm-hidden')
  document.getElementById('dlgVariantsField').classList.remove('cm-hidden')

  const specs = new Map()
  model.profiles.forEach(p => specs.set(`${p.time_budget}_${effortOf(p.slug)}`, p))
  const rows = [].concat(...BUDGETS.map(tb => EFFORT_ORDER.map(ef => variantRowHtml({
    tb,
    effort: ef,
    existing: specs.get(`${tb}_${ef}`),
  })))).join('')
  document.getElementById('dlgVariantRows').innerHTML = rows

  setPreview('')
  setError('')
  setDialogNotice('')
  createBtn.textContent = 'Save changes'
  createBtn.classList.add('dlg-btn-confirm')
  createBtn.disabled = false
  overlay.dataset.mode = 'edit'
  overlay.classList.remove('cm-hidden')
  refreshProfileMeta()
  document.getElementById('dlgModel').focus()
}

function collectEditPayload(model) {
  const rows = Array.from(document.querySelectorAll('#dlgVariantRows tr'))
  const edits = []
  const create = []
  const existingByKey = new Map(model.profiles.map(p => [`${p.time_budget}_${effortOf(p.slug)}`, p]))
  for (const tr of rows) {
    const tb = tr.dataset.tb
    const effort = tr.dataset.effort
    const checked = tr.querySelector('.vc-check').checked
    const temp = parseFloat(tr.querySelector('.vc-temp').value)
    const maxT = parseInt(tr.querySelector('.vc-max').value, 10)
    const existing = existingByKey.get(`${tb}_${effort}`)
    const entry = { time_budget: tb, effort }
    if (isFinite(temp)) entry.temperature = temp
    if (isFinite(maxT)) entry.max_tokens = maxT
    if (existing) {
      entry.key = existing.slug
      entry.keep = checked
      edits.push(entry)
    } else if (checked) {
      create.push(entry)
    }
  }
  return { edits, create }
}

// ── per-profile quick actions (edit dialog ⋯ menus) ──────────

const PROFILE_META = new Map()
let prefJudgeModel = ''
let rememberedJudgeModel = ''

function storageGet(key) {
  try {
    const v = window.localStorage.getItem(key)
    return v == null ? '' : v
  } catch {
    return rememberedJudgeModel
  }
}

function storageSet(key, value) {
  rememberedJudgeModel = value
  try { window.localStorage.setItem(key, value) } catch { /* opaque origin */ }
}

async function refreshProfileMeta() {
  try {
    const { jobs } = await api('/api/jobs?limit=60')
    const runJobs = jobs.filter(j => ['run_candidate', 'judge_existing', 'agent'].includes(j.toolId)).slice(0, 12)
    const details = await Promise.all(runJobs.map(j =>
      api(`/api/jobs/${j.id}`).then(d => d.job).catch(() => null),
    ))
    for (const job of details) {
      if (!job || !job.args) continue
      const a = job.args
      if (a['judge-model'] && !prefJudgeModel) prefJudgeModel = a['judge-model']
      if (a.profile && a.profile !== 'all') {
        const meta = PROFILE_META.get(a.profile) || {}
        if (a.bench) meta.lastBench = a.bench
        if (a['judge-model']) meta.lastJudgeModel = a['judge-model']
        PROFILE_META.set(a.profile, meta)
      }
    }
  } catch { /* meta is best-effort */ }
}

function benchFor(slug) {
  const meta = PROFILE_META.get(slug)
  return (meta && meta.lastBench) || RUN_STATE.benches[0] || 'chapter_fast'
}

function buildProfileArgs(slug, tb, extra = {}) {
  return { bench: benchFor(slug), profile: slug, time: tb || 'all', ...extra }
}

function closeProfileMenus() {
  document.querySelectorAll('#dlgVariantRows .vc-opt-btn').forEach(b => b.setAttribute('aria-expanded', 'false'))
  document.querySelectorAll('#dlgVariantRows .vc-opt-menu').forEach(m => m.classList.add('cm-hidden'))
}

function profileMenuAction(slug, action, row) {
  const tb = row && row.dataset.tb ? row.dataset.tb : 'all'
  closeProfileMenus()
  if (action === 'run') {
    launchProfileJob('run_candidate', buildProfileArgs(slug, tb, { 'write-results': true }), `run_candidate on <code>${esc(benchFor(slug))}</code> for <code>${esc(slug)}</code>`)
    return
  }
  if (action === 'prefill') {
    openRunProfilePrefill(slug, tb)
    return
  }
  if (action === 'judge') {
    let judgeModel = storageGet('mm.judgeModel') || prefJudgeModel
    if (!judgeModel) {
      let entered = null
      try { entered = window.prompt('Judge model (e.g. openai/gpt-4o):', '') } catch { entered = null }
      if (!entered || !entered.trim()) return
      judgeModel = entered.trim()
      storageSet('mm.judgeModel', judgeModel)
    }
    launchProfileJob('judge_existing', {
      bench: benchFor(slug),
      'judge-model': judgeModel,
      profile: slug,
    }, `re-judge of <code>${esc(slug)}</code> on <code>${esc(benchFor(slug))}</code> with <code>${esc(judgeModel)}</code>`)
    return
  }
  if (action === 'agent') {
    launchProfileJob('agent', {
      budget: tb === '60m' ? '60m' : '30m',
      candidate: slug,
      mode: 'auto',
      stage: 'chapter',
    }, `autoresearch agent for <code>${esc(slug)}</code> (${tb === '60m' ? '60m' : '30m'} budget)`)
  }
}

function launchProfileJob(toolId, args, label) {
  if (RUN_STATE.missingKeys.includes('OPENROUTER_API_KEY')) {
    setDialogNotice('Missing <code>OPENROUTER_API_KEY</code> — set it in <code>dashboard/.env</code> and restart the dev server, then run again.', 'error')
    return
  }
  api('/api/jobs', { method: 'POST', body: JSON.stringify({ toolId, args }) })
    .then(res => {
      const shortId = res.job && res.job.id ? esc(res.job.id.slice(0, 8)) : ''
      setDialogNotice(`Launched <strong>${esc(toolId)}</strong>: ${label}. Job id <code>${shortId}…</code> — watch it stream in <strong>Run data</strong>.`)
      refreshJobs().catch(() => {})
    })
    .catch(e => setDialogNotice(esc(e.message), 'error'))
}

function clearInlineRunWidget() {
  const dialog = document.getElementById('modelDialogOverlay')
  if (!dialog) return
  dialog.classList.remove('has-run-form')
  dialog.dataset.runSlug = ''
  const holder = document.getElementById('dlgRunForm')
  const widget = document.getElementById('dlgRunWidget')
  if (widget) widget.innerHTML = ''
  if (holder) {
    holder.classList.add('cm-hidden')
    if (document.getElementById('dlgRunTitle')) document.getElementById('dlgRunTitle').innerHTML = 'Run with options'
  }
}

async function ensureRunData() {
  if (RUN_STATE.activeStarted && RUN_TOOLS_BY_ID.has('run_candidate')) return
  await initRunData()
  if (RUN_TOOLS_BY_ID.has('run_candidate')) return
  // defensive refetch: tolerates a latched-stale page session (pre-fix bundle)
  try {
    const reg = await api('/api/registry')
    RUN_STATE.tools = reg.tools || []
    RUN_TOOLS_BY_ID.clear()
    for (const t of RUN_STATE.tools) RUN_TOOLS_BY_ID.set(t.id, t)
    renderTools()
    RUN_STATE.activeStarted = RUN_TOOLS_BY_ID.size > 0
  } catch { /* still unavailable */ }
}

async function openRunProfilePrefill(slug, tb) {
  const dialog = document.getElementById('modelDialogOverlay')
  await ensureRunData()
  clearInlineRunWidget()
  if (!RUN_TOOLS_BY_ID.has('run_candidate')) {
    setDialogNotice('Run data failed to load — check the dev server and try again.', 'error')
    return
  }
  const tool = RUN_TOOLS_BY_ID.get('run_candidate')
  const widget = document.getElementById('dlgRunWidget')
  const holder = document.getElementById('dlgRunForm')
  if (!widget || !holder) {
    setDialogNotice('Run form is missing — hard refresh (&#8984;&#8679;R) to load the latest dashboard, then try again.', 'error')
    return
  }
  widget.innerHTML = toolCardHtml(tool, { idPrefix: 'dlg-run-fld' })
  const card = widget.querySelector('.mm-tool-widget')
  wireToolCard(card, tool)
  fillFormFromArgs(card, tool, buildProfileArgs(slug, tb))
  const form = card.querySelector('.tool-form')
  form.classList.remove('cm-hidden')
  if (form.scrollIntoView) form.scrollIntoView({ block: 'nearest' })
  document.getElementById('dlgRunTitle').innerHTML = `Run <code>${esc(slug)}</code> with options`
  dialog.dataset.runSlug = slug
  dialog.classList.add('has-run-form')
  holder.classList.remove('cm-hidden')
  setDialogNotice('Tune any option below, then hit <strong>Run script</strong>. The job queues behind other harness runs and streams live in <strong>Run data</strong>.')
  updateToolGuards()
}

async function handleProbeForAdd(model, route) {
  const createBtn = document.getElementById('dlgCreate')
  const budgets = []
  if (document.getElementById('tb30').checked) budgets.push('30m')
  if (document.getElementById('tb60').checked) budgets.push('60m')
  if (!budgets.length) { setError('Select at least one time budget.'); return }
  createBtn.disabled = true
  createBtn.textContent = 'Probing live...'
  setError('')
  try {
    const res = await api('/api/models/probe', {
      method: 'POST',
      body: JSON.stringify({ model, time_budget: budgets }),
    })
    if (!res.compatible) {
      setError(res.error || 'Model failed capability probes — incompatible with this harness.')
      setPreview(probeResultHtml(model, res.probe, res.created))
      createBtn.textContent = 'Probe & preview'
      return
    }
    setPreview(probeResultHtml(model, res.probe, res.created))
    createBtn.textContent = `Create ${res.created.length} profile${res.created.length === 1 ? '' : 's'}`
    createBtn.classList.add('dlg-btn-confirm')
    createBtn.disabled = false
  } catch (e) {
    setError(e.message)
    createBtn.textContent = 'Probe & preview'
  }
}

async function confirmAdd(model, route) {
  const budgets = []
  if (document.getElementById('tb30').checked) budgets.push('30m')
  if (document.getElementById('tb60').checked) budgets.push('60m')
  const createBtn = document.getElementById('dlgCreate')
  createBtn.disabled = true
  createBtn.textContent = 'Creating...'
  try {
    const body = { model }
    if (budgets.length) body.time_budget = budgets
    if (route) body.provider_route = route
    const res = await api('/api/models', { method: 'POST', body: JSON.stringify(body) })
    if (!res.added.length && !res.skipped.length) setError('Nothing added — model may already exist.')
    else {
      closeDialog()
      await loadModels()
    }
  } catch (e) {
    setError(e.message)
    createBtn.disabled = false
    createBtn.textContent = 'Create profiles'
  }
}

async function confirmEdit(model) {
  const overlay = document.getElementById('modelDialogOverlay')
  const createBtn = document.getElementById('dlgCreate')
  const rawModel = document.getElementById('dlgModel').value.trim()
  if (!rawModel) { setError('Model ID is required.'); return }
  const route = parseRoute(document.getElementById('dlgProviderRoute').value)
  if (document.getElementById('dlgProviderRoute').value.trim() && !route) { setError('Provider route is not valid JSON.'); return }
  const { edits, create } = collectEditPayload(model)
  createBtn.disabled = true
  createBtn.textContent = 'Saving...'
  try {
    const body = {
      old_model: model.model,
      new_model: rawModel,
      edits,
      create,
    }
    if (route !== undefined) body.provider_route = route
    await api('/api/models', { method: 'PUT', body: JSON.stringify(body) })
    closeDialog()
    await loadModels()
  } catch (e) {
    setError(e.message)
    createBtn.disabled = false
    createBtn.textContent = 'Save changes'
  }
}

function closeDialog() {
  clearInlineRunWidget()
  document.getElementById('modelDialogOverlay').classList.add('cm-hidden')
}

async function handleDelete(row) {
  const model = row.dataset.model
  const btn = row.querySelector('[data-action="delete"]')
  if (!btn.dataset.armed) {
    btn.dataset.armed = '1'
    btn.textContent = 'Confirm delete?'
    btn.classList.add('armed')
    setTimeout(() => {
      delete btn.dataset.armed
      btn.textContent = 'Delete'
      btn.classList.remove('armed')
    }, 4000)
    return
  }
  btn.textContent = 'Deleting...'
  try {
    const res = await api('/api/models', {
      method: 'DELETE',
      body: JSON.stringify({ model }),
    })
    await loadModels()
  } catch (e) {
    btn.textContent = 'Delete'
    delete btn.dataset.armed
    btn.classList.remove('armed')
  }
}

async function handleModelsListClick(e) {
  const btn = e.target.closest('button[data-action]')
  if (!btn) return
  const card = e.target.closest('.model-card')
  const model = card.dataset.model
  const action = btn.dataset.action
  if (action === 'edit') {
    const { models } = await api('/api/models')
    const m = models.find(x => x.model === model)
    if (m) openEditDialog(m)
  } else if (action === 'delete') {
    handleDelete(card)
  }
}

function wireGearButtons() {
  const open = () => {
    const overlay = document.getElementById('settingsOverlay')
    if (overlay) overlay.classList.remove('cm-hidden')
    loadModels()
  }
  document.querySelectorAll('[data-settings-toggle]').forEach(btn => {
    btn.addEventListener('click', open)
  })
}

function hideOverlay(overlay) {
  overlay.classList.add('cm-hidden')
  stopRunPolling()
}

function activateSection(section) {
  document.querySelectorAll('.settings-nav-item').forEach(n => n.classList.toggle('active', n.dataset.section === section))
  const cap = section[0].toUpperCase() + section.slice(1)
  document.querySelectorAll('.settings-section').forEach(s => {
    s.classList.toggle('active', s.id === `settings${cap}Section`)
  })
  if (section === 'models') loadModels()
  else if (section === 'run') initRunData()
}

function handleVariantMenuClick(e) {
  const btn = e.target.closest('.vc-opt-btn')
  if (btn) {
    e.stopPropagation()
    const menuEl = btn.closest('.vc-opt').querySelector('.vc-opt-menu')
    const open = !menuEl.classList.contains('cm-hidden')
    closeProfileMenus()
    if (!open) {
      menuEl.classList.remove('cm-hidden')
      btn.setAttribute('aria-expanded', 'true')
    }
    return
  }
  const item = e.target.closest('.vc-opt-item')
  if (item) {
    e.stopPropagation()
    const row = item.closest('tr')
    const slug = item.closest('.vc-opt').querySelector('.vc-opt-btn').dataset.slug
    profileMenuAction(slug, item.dataset.action, row)
  }
}

function closeProfileMenusIfOutside(e) {
  if (e.target.closest && e.target.closest('.vc-opt')) return
  closeProfileMenus()
}

function bindModal() {
  const overlay = document.getElementById('settingsOverlay')
  const dialogOverlay = document.getElementById('modelDialogOverlay')

  document.getElementById('settingsClose').addEventListener('click', () => {
    hideOverlay(overlay)
  })
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) hideOverlay(overlay)
  })

  document.getElementById('dlgClose').addEventListener('click', closeDialog)
  document.getElementById('dlgCancel').addEventListener('click', closeDialog)
  document.getElementById('dlgRunClose').addEventListener('click', clearInlineRunWidget)
  dialogOverlay.addEventListener('click', (e) => {
    if (e.target === dialogOverlay) closeDialog()
  })

  document.querySelector('.settings-nav').addEventListener('click', (e) => {
    const item = e.target.closest('.settings-nav-item')
    if (!item || item.disabled) return
    activateSection(item.dataset.section)
  })

  document.getElementById('dlgVariantRows').addEventListener('click', handleVariantMenuClick)
  document.addEventListener('click', closeProfileMenusIfOutside)

  document.getElementById('modelAddBtn').addEventListener('click', openAddDialog)
  document.getElementById('modelsList').addEventListener('click', handleModelsListClick)

  document.getElementById('jobRefresh').addEventListener('click', () => refreshJobs().catch(() => {}))
  document.getElementById('jobClear').addEventListener('click', async () => {
    try {
      const { removed } = await api('/api/jobs', { method: 'DELETE' })
      if (removed) setBanner(`Cleared ${removed} finished run${removed === 1 ? '' : 's'}.`)
      await refreshJobs()
    } catch (e) {
      setBanner(`Clear failed: ${esc(e.message)}`)
    }
  })

  window.addEventListener('dashboard:candidates-refreshed', () => {
    const modelSec = document.getElementById('settingsModelsSection')
    if (modelSec && modelSec.classList.contains('active')) loadModels()
  })
  document.getElementById('dlgModel').addEventListener('input', (e) => {
    const btn = document.getElementById('dlgCreate')
    const mode = dialogOverlay.dataset.mode
    const has = e.target.value.includes('/') && e.target.value.split('/')[0] && e.target.value.split('/')[1]
    if (mode === 'add') {
      btn.disabled = !has
      if (btn.textContent !== 'Probing live...' && btn.textContent !== 'Creating...') {
        btn.textContent = has ? 'Probe & preview' : 'Probe & preview'
        btn.classList.remove('dlg-btn-confirm')
      }
    }
  })

  document.getElementById('dlgCreate').addEventListener('click', async () => {
    const mode = dialogOverlay.dataset.mode
    const rawModel = document.getElementById('dlgModel').value.trim()
    if (!rawModel) { setError('Model ID is required.'); return }
    const model = rawModel.replace(/^\/+|\/+$/g, '')
    const btn = document.getElementById('dlgCreate')
    const rawRouteValue = document.getElementById('dlgProviderRoute').value
    const invalidRoute = rawRouteValue.trim() && !parseRoute(rawRouteValue)
    if (invalidRoute) {
      setError('Provider route is not valid JSON.')
      return
    }
    if (mode === 'add') {
      if (btn.classList.contains('dlg-btn-confirm')) {
        await confirmAdd(model, parseRoute(rawRouteValue))
      } else {
        await handleProbeForAdd(model)
      }
    } else if (mode === 'edit') {
      let editingModel
      try { editingModel = JSON.parse(dialogOverlay.dataset.editingModel || 'null') } catch { editingModel = null }
      if (!editingModel) { setError('Lost edit context — reopen the model.'); return }
      await confirmEdit(editingModel)
    }
  })

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return
    if (!dialogOverlay.classList.contains('cm-hidden')) closeDialog()
    else if (!overlay.classList.contains('cm-hidden')) hideOverlay(overlay)
  })
}

// ── Run data section (python script runner) ───────────────────

const RUN_STATE = {
  tools: null,
  benches: [],
  missingKeys: [],
  jobs: [],
  pollTimer: null,
  es: new Map(),
  logs: new Map(),
  expandedJobs: new Set(),
  hintedJobs: new Set(),
  activeStarted: false,
}

const RUN_TOOLS_BY_ID = new Map()
const GROUP_ORDER = ['corpus', 'candidates', 'run', 'maintenance']

const RUN_NEEDS_KEY = {
  add_candidate: true,
  run_candidate: true,
  judge_existing: true,
  agent: true,
  snapshot_catalog: true,
}

const STATUS_BADGE = {
  queued: ['\u22EF', 'Queued', 'st-queued'],
  running: ['\u23F1', 'Running', 'st-running'],
  succeeded: ['\u2713', 'Succeeded', 'st-succeeded'],
  failed: ['\u2715', 'Failed', 'st-failed'],
  canceled: ['\u25A0', 'Canceled', 'st-canceled'],
  interrupted: ['\u25A0', 'Interrupted', 'st-canceled'],
}

function durationStr(startIso, endIso) {
  if (!startIso) return ''
  const end = endIso ? new Date(endIso) : new Date()
  const ms = end.getTime() - new Date(startIso).getTime()
  if (isNaN(ms) || ms < 0) return ''
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) +
    ' ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function statusBadgeHtml(status) {
  const [icon, label, cls] = STATUS_BADGE[status] || ['?', status, 'st-queued']
  return `<span class="job-badge ${cls}"><span aria-hidden="true">${icon}</span> ${label}</span>`
}

function toolById(id) {
  return RUN_TOOLS_BY_ID.get(id)
}

function groupLabel(id) {
  const map = { corpus: 'Corpus validation', candidates: 'Candidates', run: 'Run harness', maintenance: 'Analysis & maintenance' }
  return map[id] || id
}

function fieldHtml(a, idPrefix = 'run-fld') {
  const req = a.required ? ' <span class="req-star" aria-hidden="true">*</span>' : ''
  const label = `<label for="${idPrefix}-${a.name}" ${a.required ? 'aria-required="true"' : ''}>${esc(a.label)}${req}</label>`
  const hint = a.hint ? `<div class="field-hint">${esc(a.hint)}</div>` : ''
  const wrap = (inner) => `<div class="field run-field ${a.advanced ? 'run-advanced' : ''}" data-group="${a.group || 'main'}">${inner}</div>`
  switch (a.type) {
    case 'bool':
      return wrap(`<label class="cb"><input type="checkbox" data-arg="${a.name}" id="${idPrefix}-${a.name}" ${a.default ? 'checked' : ''} /> ${esc(a.label)}</label>`)
    case 'enum':
      if (a.multiple) {
        const opts = (a.choices || []).map(c =>
          `<label class="cb"><input type="checkbox" data-arg="${a.name}" value="${esc(c)}" ${(a.default || []).includes(c) ? 'checked' : ''} /> ${esc(c)}</label>`,
        ).join('')
        return wrap(`${label}<div class="cb-row">${opts}</div>${hint}`)
      }
      return wrap(`${label}<select id="${idPrefix}-${a.name}" data-arg="${a.name}">${
        (a.choices || []).map(c => `<option value="${esc(c)}" ${a.default === c ? 'selected' : ''}>${esc(c)}</option>`).join('')
      }</select>${hint}`)
    case 'int':
      return wrap(`${label}<input id="${idPrefix}-${a.name}" data-arg="${a.name}" type="number" step="1" ${a.min != null ? `min="${a.min}"` : ''} ${a.max != null ? `max="${a.max}"` : ''} value="${esc(a.default != null ? a.default : '')}" />${hint}`)
    case 'json':
      return wrap(`${label}<textarea id="${idPrefix}-${a.name}" data-arg="${a.name}" rows="2" spellcheck="false" placeholder="${esc(a.placeholder || a.hint || '{}')}"></textarea>${hint}`)
    default:
      if (a.toggle) {
        const enabled = a.default != null && a.default !== ''
        return wrap(`${label}
          <div class="cb judge-tgl"><input type="checkbox" id="${idPrefix}-${a.name}-toggle" data-toggle="${a.name}" ${enabled ? 'checked' : ''} /><label for="${idPrefix}-${a.name}-toggle">Enable LLM judge</label></div>
          <input id="${idPrefix}-${a.name}" data-arg="${a.name}" type="text" list="${a.bench ? 'benchList' : ''}" spellcheck="false" placeholder="${esc(a.placeholder || '')}" value="${esc(a.default != null ? String(a.default) : '')}" class="judge-model-fld" />
          ${hint}`)
      }
      return wrap(`${label}<input id="${idPrefix}-${a.name}" data-arg="${a.name}" type="text" list="${a.bench ? 'benchList' : ''}" spellcheck="false" placeholder="${esc(a.placeholder || '')}" value="${esc(a.default != null ? String(a.default) : '')}" />${hint}`)
  }
}

function toolCardHtml(tool, opts = {}) {
  const idPrefix = opts.idPrefix || 'run-fld'
  const visible = tool.args.filter(a => (a.fixed && a.ui === false) ? false : !a.advanced)
  const advanced = tool.args.filter(a => a.advanced)
  const hasAdvanced = advanced.some(a => !(a.fixed && a.ui === false))
  const destructive = tool.destructive
  const warning = destructive
    ? `<div class="tool-warning" role="alert"><strong>Deletes artifacts/runs, results.tsv, data/candidates.json, bench/book_gate.jsonl and snapshots. Irreversible.</strong></div>`
    : ''
  const confirmField = destructive
    ? `<div class="field run-field"><label for="${idPrefix}-confirm-${tool.id}">Type <code>RESET</code> to continue <span class="req-star">*</span></label>
        <input id="${idPrefix}-confirm-${tool.id}" data-confirm type="text" autocapitalize="off" spellcheck="false" placeholder="RESET" />
        <div class="field-hint">Case-sensitive. The server re-checks this value before running.</div></div>`
    : ''
  const presets = (tool.presets || []).length
    ? `<div class="tool-presets"><span class="tp-label">Quick presets</span>${tool.presets.map(p =>
        `<button type="button" class="preset-chip" data-preset="${esc(p.id)}">${esc(p.label)}</button>`).join('')}</div>`
    : ''
  const advancedToggle = hasAdvanced
    ? `<button type="button" class="mini-btn run-advanced-toggle" aria-expanded="false">Show advanced</button>`
    : ''
  return `<div class="tool-card mm-tool-widget" data-tool="${esc(tool.id)}">
    <div class="tool-card-head">
      <span class="tool-dot" aria-hidden="true"></span>
      <div class="tool-card-main">
        <div class="tool-card-title">${esc(tool.title)}</div>
        <div class="tool-card-desc">${esc(tool.description)}</div>
        <div class="tool-card-class">${esc(tool.runtimeClass)}</div>
      </div>
      <button type="button" class="mini-btn tool-run-btn">Run</button>
    </div>
    ${presets}
    <div class="tool-form cm-hidden">
      ${warning}
      ${visible.map(a => fieldHtml(a, idPrefix)).join('')}
      ${hasAdvanced ? `<div class="run-advanced-block cm-hidden">${advanced.map(a => fieldHtml(a, idPrefix)).join('')}</div>` : ''}
      ${advancedToggle}
      ${confirmField}
      <div class="tool-form-actions">
        <button type="button" class="dlg-btn dlg-btn-primary tool-run-submit" ${destructive ? 'disabled' : ''}>Run script</button>
        <span class="tool-lock-hint cm-hidden">Run harness busy — queued runs start automatically</span>
      </div>
      <div class="tool-form-error cm-hidden" role="alert"></div>
    </div>
  </div>`
}

function fillFormFromArgs(card, tool, args) {
  for (const a of tool.args) {
    if (a.fixed && a.ui === false) continue
    const value = args[a.name]
    const el = card.querySelector(`[data-arg="${a.name}"]`)
    if (!el) continue
    if (a.type === 'bool') { el.checked = !!value; continue }
    if (a.multiple) {
      const vals = toArray(value)
      card.querySelectorAll(`[data-arg="${a.name}"]`).forEach(cb => { cb.checked = vals.includes(cb.value) })
      continue
    }
    if (el.tagName === 'TEXTAREA') { el.value = value != null ? JSON.stringify(value) : ''; continue }
    if (el.tagName === 'SELECT') {
      if (value != null && value !== '') el.value = String(value)
      continue
    }
    const enabled = value != null && value !== ''
    if (a.toggle) {
      const cb = card.querySelector(`[data-toggle="${a.name}"]`)
      if (cb) cb.checked = enabled
      el.classList.toggle('cm-hidden', !enabled)
      el.value = enabled ? String(value) : ''
      continue
    }
    el.value = value != null ? String(value) : ''
  }
}

function applyPreset(card, tool, preset) {
  fillFormFromArgs(card, tool, preset.args || {})
}

function toArray(v) {
  if (v == null) return []
  return Array.isArray(v) ? v : [v]
}

function showCardError(card, msg) {
  const el = card.querySelector('.tool-form-error')
  el.innerHTML = `<strong>${esc(msg)}</strong>`
  el.classList.remove('cm-hidden')
}

function clearCardError(card) {
  const el = card.querySelector('.tool-form-error')
  el.classList.add('cm-hidden')
  el.innerHTML = ''
}

function collectArgValues(card, tool) {
  const values = {}
  const errors = []
  for (const a of tool.args) {
    if (a.fixed && a.ui === false) continue
    const el = card.querySelector(`[data-arg="${a.name}"]`)
    if (!el) continue
    if (a.type === 'bool') { values[a.name] = el.checked; continue }
    if (a.multiple) {
      values[a.name] = [...card.querySelectorAll(`[data-arg="${a.name}"]`)].filter(c => c.checked).map(c => c.value)
      continue
    }
    if (el.tagName === 'TEXTAREA') {
      const raw = (el.value || '').trim()
      if (!raw) { values[a.name] = a.default ?? ''; continue }
      try {
        values[a.name] = JSON.parse(raw)
      } catch {
        errors.push(`${a.label} is not valid JSON`)
      }
      continue
    }
    const raw = (el.value || '').trim()
    if (a.type === 'int') {
      if (raw === '') { values[a.name] = a.default ?? ''; continue }
      const n = Number(raw)
      if (!Number.isInteger(n)) { errors.push(`${a.label} must be an integer`); continue }
      values[a.name] = n
      continue
    }
    if (el.tagName === 'SELECT') { values[a.name] = el.value; continue }
    if (a.toggle) {
      const cb = card.querySelector(`[data-toggle="${a.name}"]`)
      if (cb && !cb.checked) {
        delete values[a.name]
        continue
      }
      if (!raw) {
        errors.push(`${a.label} is required when the judge is enabled`)
        continue
      }
    }
    const s = String(raw)
    if (a.pattern && s && !new RegExp(a.pattern).test(s)) {
      errors.push(`${a.label} has invalid characters`)
      continue
    }
    values[a.name] = s
  }
  return { values, errors }
}

function toolNeedsKey(tool, values) {
  if (!RUN_NEEDS_KEY[tool.id]) return false
  if (tool.id === 'run_candidate' && values.mock) return false
  return true
}

async function runToolSubmit(card, tool) {
  clearCardError(card)
  const { values, errors } = collectArgValues(card, tool)
  if (errors.length) { showCardError(card, errors.join('; ')); return }
  const missingReq = tool.args.find(a => a.required && a.type !== 'bool' && (values[a.name] == null || values[a.name] === ''))
  if (missingReq) { showCardError(card, `${missingReq.label} is required`); return }
  if (tool.destructive && card.querySelector('[data-confirm]').value !== tool.confirmPhrase) {
    showCardError(card, `Type ${tool.confirmPhrase} to continue`)
    return
  }
  if (toolNeedsKey(tool, values) && RUN_STATE.missingKeys.includes('OPENROUTER_API_KEY')) {
    showCardError(card, 'Missing OPENROUTER_API_KEY — this run needs it. Set it in .env and restart the dev server.')
    return
  }
  const body = { toolId: tool.id, args: values }
  if (tool.destructive) body.confirm = card.querySelector('[data-confirm]').value
  const inDialog = !!card.closest('#dlgRunForm')
  try {
    const res = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) })
    await refreshJobs()
    if (res.job && (res.job.status === 'running' || res.job.status === 'queued')) {
      expandJob(res.job.id)
      attachStreamIfPossible(res.job.id)
    }
    if (inDialog) {
      const shortId = res.job && res.job.id ? res.job.id.slice(0, 8) : ''
      setDialogNotice(`Launched <strong>${esc(tool.id)}</strong>. Job id <code>${esc(shortId)}…</code> — watch it stream in <strong>Run data</strong>.`)
    }
  } catch (e) {
    showCardError(card, e.message)
  }
}

function wireToolCard(card, tool) {
  card.querySelector('.tool-run-btn').addEventListener('click', () => {
    const form = card.querySelector('.tool-form')
    const wasHidden = form.classList.contains('cm-hidden')
    form.classList.toggle('cm-hidden')
    if (wasHidden) {
      const first = card.querySelector('.run-field input, .run-field select, .run-field textarea')
      if (card.querySelector('[data-confirm]')) card.querySelector('[data-confirm]').focus()
      else if (first) first.focus()
    }
  })
  card.querySelector('.tool-run-submit').addEventListener('click', () => runToolSubmit(card, tool))
  const advToggle = card.querySelector('.run-advanced-toggle')
  if (advToggle) {
    advToggle.addEventListener('click', (e) => {
      const block = card.querySelector('.run-advanced-block')
      const open = block.classList.toggle('cm-hidden') === false
      e.target.setAttribute('aria-expanded', open ? 'true' : 'false')
    })
  }
  if (card.querySelector('[data-confirm]')) {
    card.querySelector('[data-confirm]').addEventListener('input', (e) => {
      const ok = e.target.value === tool.confirmPhrase
      card.querySelector('.tool-run-submit').disabled = !ok
    })
  }
  card.querySelectorAll('.preset-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const preset = (tool.presets || []).find(p => p.id === chip.dataset.preset)
      if (preset) applyPreset(card, tool, preset)
    })
  })

  card.querySelectorAll('[data-toggle]').forEach(cb => {
    const input = card.querySelector(`[data-arg="${cb.dataset.toggle}"]`)
    if (!input) return
    const sync = () => {
      const on = cb.checked
      input.classList.toggle('cm-hidden', !on)
      if (!on) input.value = ''
    }
    cb.addEventListener('change', sync)
    sync()
  })
  return card
}

function renderTools() {
  const list = document.getElementById('toolList')
  const tools = RUN_STATE.tools
  if (!tools) { list.innerHTML = '<div class="placeholder-text">Loading tools...</div>'; return }
  list.innerHTML = GROUP_ORDER.map(group => {
    const items = tools.filter(t => t.group === group)
    if (!items.length) return ''
    return `<div class="tool-group">
      <div class="tool-group-label">${esc(groupLabel(group))}</div>
      ${items.map(toolCardHtml).join('')}
    </div>`
  }).join('')

  list.querySelectorAll('.tool-card').forEach(card => {
    wireToolCard(card, RUN_TOOLS_BY_ID.get(card.dataset.tool))
  })
  updateToolGuards()
}

function updateToolGuards() {
  const runningToolIds = new Set(RUN_STATE.jobs.filter(j => j.status === 'running' || j.status === 'queued').map(j => j.toolId))
  const llmRunning = RUN_STATE.jobs.some(j => j.status === 'running' && toolById(j.toolId) && toolById(j.toolId).runtimeClass === 'llm')
  document.querySelectorAll('.mm-tool-widget').forEach(card => {
    const tool = RUN_TOOLS_BY_ID.get(card.dataset.tool)
    if (!tool) return
    const btn = card.querySelector('.tool-run-btn')
    const submit = card.querySelector('.tool-run-submit')
    const hintEl = card.querySelector('.tool-lock-hint')
    const dot = card.querySelector('.tool-dot')
    const myActive = runningToolIds.has(tool.id)
    const locked = llmRunning && tool.runtimeClass !== 'instant'
    const disabled = myActive || locked
    if (btn) {
      btn.disabled = disabled
      if (disabled) btn.title = myActive ? 'This tool already has a queued or running job' : 'Run harness busy — queued runs start automatically'
      else btn.removeAttribute('title')
    }
    if (submit && !tool.destructive) submit.disabled = disabled
    if (hintEl) hintEl.classList.toggle('cm-hidden', !locked)
    if (dot) dot.classList.toggle('tool-dot-active', myActive)
  })
}

function consoleContentHtml(job) {
  const logs = RUN_STATE.logs.get(job) || []
  if (!logs.length) return ''
  return logs.map(l =>
    l.stream === 'stderr'
      ? `<span class="con-err">${esc(l.text)}</span>`
      : esc(l.text),
  ).join('\n')
}

function appendConsole(job, ev) {
  const logs = RUN_STATE.logs.get(job) || []
  logs.push(ev)
  if (logs.length > 2000) logs.splice(0, logs.length - 2000)
  RUN_STATE.logs.set(job, logs)
  const row = document.querySelector(`.job-row[data-job="${job}"]`)
  if (!row) return
  const pre = row.querySelector('.console-pre')
  if (!pre) return
  pre.innerHTML = consoleContentHtml(job)
  const wrap = row.querySelector('.console-wrap')
  const autoscroll = row.querySelector('.autoscroll-toggle')
  if (wrap && autoscroll && autoscroll.checked && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    wrap.scrollTop = wrap.scrollHeight
  }
}

function rollupJobRow(job) {
  const tool = toolById(job.toolId)
  const title = tool ? tool.title : job.toolId
  const isExpanded = RUN_STATE.expandedJobs.has(job.id)
  const badge = statusBadgeHtml(job.status)
  const actions = []
  if (job.status === 'queued' || job.status === 'running') {
    actions.push(`<button type="button" class="mini-btn job-cancel" data-job="${job.id}" aria-pressed="false">Cancel</button>`)
  }
  if (['succeeded', 'failed', 'canceled', 'interrupted'].includes(job.status)) {
    actions.push(`<button type="button" class="mini-btn job-rerun" data-job="${job.id}">Re-run</button>`)
  }
  if (job.status === 'succeeded' && job.resultHints && (job.resultHints.bench || job.resultHints.runId)) {
    actions.push(`<button type="button" class="mini-btn job-explorer" data-job="${job.id}">Open in explorer</button>`)
  }
  actions.push(`<a class="mini-btn job-log-link" href="/api/jobs/${encodeURIComponent(job.id)}/log" target="_blank" rel="noopener">view log</a>`)
  const hintLine = job.resultHints && (job.resultHints.specPyChanged || job.resultHints.snapshotsCreated)
    ? `<div class="job-hints">${job.resultHints.specPyChanged ? 'candidate_spec.py changed' : ''}${job.resultHints.snapshotsCreated && job.resultHints.snapshotsCreated.length ? `snapshots: ${esc(job.resultHints.snapshotsCreated.length)} created` : ''}</div>`
    : ''
  const consoleArea = `<div class="job-console cm-hidden">
      <div class="console-toolbar">
        <label class="cb autoscroll-wrap"><input type="checkbox" class="autoscroll-toggle" checked /> Auto-scroll</label>
        <button type="button" class="mini-btn console-clear">Clear view</button>
        <span class="console-announce" aria-live="polite" aria-atomic="true"></span>
      </div>
      <div class="console-wrap"><pre class="console-pre" role="log"></pre></div>
    </div>`
  return `<div class="job-row" data-job="${job.id}">
    <div class="job-row-head" role="button" tabindex="0">
      <div class="job-main">${badge}<span class="job-title">${esc(title)}</span></div>
      <div class="job-meta">${fmtTime(job.createdAt)}${job.finishedAt ? ' · ' + durationStr(job.createdAt, job.finishedAt) : ''}</div>
    </div>
    <div class="job-actions">${actions.join('')}</div>
    ${hintLine}
    ${consoleArea}
  </div>`
}

function renderJobs() {
  const list = document.getElementById('jobsList')
  if (!list) return
  const count = document.getElementById('jobCount')
  const active = RUN_STATE.jobs.filter(j => j.status === 'running').length
  const queued = RUN_STATE.jobs.filter(j => j.status === 'queued').length
  const parts = []
  if (active) parts.push(`${active} running`)
  if (queued) parts.push(`${queued} queued`)
  count.textContent = parts.length ? parts.join(' · ') : (RUN_STATE.jobs.length ? `${RUN_STATE.jobs.length} run${RUN_STATE.jobs.length === 1 ? '' : 's'}` : 'No runs')

  if (!RUN_STATE.jobs.length) {
    list.innerHTML = '<div class="placeholder-text">No runs yet. Pick a tool on the left to run your first script.</div>'
    updateToolGuards()
    return
  }
  list.innerHTML = RUN_STATE.jobs.map(rollupJobRow).join('')

  list.querySelectorAll('.job-row-head').forEach(head => {
    head.addEventListener('click', () => {
      const row = head.closest('.job-row')
      const id = row.dataset.job
      const consoleEl = row.querySelector('.job-console')
      const open = consoleEl.classList.toggle('cm-hidden') === false
      if (open) {
        RUN_STATE.expandedJobs.add(id)
        const pre = row.querySelector('.console-pre')
        pre.innerHTML = consoleContentHtml(id)
        if (RUN_STATE.logs.get(id) && RUN_STATE.logs.get(id).length === 0) pre.textContent = 'Waiting for output…\n'
        attachStreamIfPossible(id)
      } else {
        RUN_STATE.expandedJobs.delete(id)
        detachStream(id)
      }
    })
  })

  list.querySelectorAll('.job-cancel').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.dataset.armed) {
        btn.dataset.armed = ''
        btn.setAttribute('aria-pressed', 'false')
        btn.textContent = 'Canceling…'
        try {
          await api(`/api/jobs/${btn.dataset.job}/cancel`, { method: 'POST' })
          await refreshJobs()
        } catch (e) {
          btn.textContent = 'Cancel'
          setBanner(`Cancel failed: ${esc(e.message)}`)
        }
      } else {
        btn.dataset.armed = '1'
        btn.setAttribute('aria-pressed', 'true')
        btn.textContent = 'Confirm cancel?'
      }
    })
  })

  list.querySelectorAll('.job-rerun').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.job
      try {
        const { job } = await api(`/api/jobs/${id}`)
        const tool = toolById(job.toolId)
        const body = { toolId: job.toolId, args: job.args || {} }
        if (tool && tool.destructive) return
        await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) })
        await refreshJobs()
      } catch (e) {
        setBanner(`Re-run failed: ${esc(e.message)}`)
      }
    })
  })

  list.querySelectorAll('.job-explorer').forEach(btn => {
    btn.addEventListener('click', () => {
      if (window.open) window.open('/explorer.html')
    })
  })

  list.querySelectorAll('.console-clear').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.job-row')
      const id = row.dataset.job
      const logs = RUN_STATE.logs.get(id) || []
      logs.length = 0
      RUN_STATE.logs.set(id, logs)
      row.querySelector('.console-pre').textContent = ''
    })
  })

  updateToolGuards()
}

function setBanner(msg) {
  const el = document.getElementById('runBanner')
  if (!el) return
  el.innerHTML = msg
  el.classList.remove('cm-hidden')
}

function clearBanner() {
  document.getElementById('runBanner').classList.add('cm-hidden')
}

async function refreshJobs() {
  if (!document.getElementById('jobsList')) {
    stopRunPolling()
    return
  }
  try {
    const res = await api('/api/jobs?limit=60')
    RUN_STATE.jobs = res.jobs || []
    clearOffline()
    notifyAllHints()
    renderJobs()
  } catch (e) {
    setOffline()
    throw e
  }
}

function setOffline() {
  document.getElementById('jobOffline').classList.remove('cm-hidden')
  document.getElementById('jobsList').classList.add('run-offline-hide')
}

function clearOffline() {
  document.getElementById('jobOffline').classList.add('cm-hidden')
  document.getElementById('jobsList').classList.remove('run-offline-hide')
}

function expandJob(id) {
  const row = document.querySelector(`.job-row[data-job="${id}"]`)
  if (row) {
    RUN_STATE.expandedJobs.add(id)
    const consoleEl = row.querySelector('.job-console')
    if (consoleEl) {
      consoleEl.classList.remove('cm-hidden')
      const pre = row.querySelector('.console-pre')
      pre.innerHTML = consoleContentHtml(id)
      if (!RUN_STATE.logs.get(id) || RUN_STATE.logs.get(id).length === 0) pre.textContent = 'Waiting for output…\n'
    }
  }
}

function attachStreamIfPossible(job) {
  if (typeof window.EventSource === 'undefined' || RUN_STATE.es.has(job)) return
  const jobRec = RUN_STATE.jobs.find(j => j.id === job)
  if (!jobRec || (jobRec.status !== 'running' && jobRec.status !== 'queued')) return
  const es = new window.EventSource(`/api/jobs/${job}/stream`)
  RUN_STATE.es.set(job, es)
  es.addEventListener('log', (e) => {
    try { appendConsole(job, JSON.parse(e.data)) } catch { /* ignore */ }
  })
  es.addEventListener('status', (e) => {
    const status = JSON.parse(e.data || '{}')
    announceConsole(job, status)
    notifyStatusHints(job, status)
    if (status.status && ['succeeded', 'failed', 'canceled', 'interrupted'].includes(status.status)) {
      detachStream(job)
      refreshJobs().catch(() => {})
    }
  })
  es.addEventListener('cancel', () => {
    announceConsole(job, { status: 'canceled' })
  })
  es.onerror = () => {
    const rec = RUN_STATE.jobs.find(j => j.id === job)
    if (rec && rec.status !== 'running' && rec.status !== 'queued') detachStream(job)
  }
}

function detachStream(job) {
  const es = RUN_STATE.es.get(job)
  if (es) {
    es.close()
    RUN_STATE.es.delete(job)
  }
}

function announceConsole(job, status) {
  const row = document.querySelector(`.job-row[data-job="${job}"]`)
  if (!row) return
  const ann = row.querySelector('.console-announce')
  if (!ann) return
  const [icon, label] = STATUS_BADGE[status.status] || ['', status.status]
  ann.textContent = `${icon} ${label}`
  const badgeEl = row.querySelector('.job-badge')
  if (badgeEl && status.status) badgeEl.outerHTML = statusBadgeHtml(status.status)
}

function notifyStatusHints(job, status) {
  const hints = status.resultHints
  if (!hints) return
  dispatchHints(job, hints)
}

function notifyAllHints() {
  for (const j of RUN_STATE.jobs) {
    if (j.resultHints) dispatchHints(j.id, j.resultHints)
  }
}

function dispatchHints(job, hints) {
  if (RUN_STATE.hintedJobs.has(job)) return
  if (hints.resultsTsvUpdated) {
    RUN_STATE.hintedJobs.add(job)
    window.dispatchEvent(new window.Event('dashboard:results-refreshed'))
  }
  if (hints.specPyChanged) {
    RUN_STATE.hintedJobs.add(job)
    window.dispatchEvent(new window.Event('dashboard:candidates-refreshed'))
  }
}

// Loads the script registry + bench/env/jobs and wires the run tab.
// `activeStarted` is only latched once registry loads: a failed initRunData
// (e.g. the dev server predating /api/registry) retries on the next trigger
// instead of leaving the run-data feature dead until a full reload.
async function initRunData() {
  if (RUN_STATE.activeStarted) return
  let toolsLoaded = false
  try {
    const reg = await api('/api/registry')
    RUN_STATE.tools = reg.tools || []
    for (const t of RUN_STATE.tools) RUN_TOOLS_BY_ID.set(t.id, t)
    renderTools()
    toolsLoaded = RUN_TOOLS_BY_ID.has('run_candidate') || RUN_TOOLS_BY_ID.size > 0
  } catch (e) {
    document.getElementById('toolList').innerHTML = `<div class="placeholder-text">Failed to load tools: ${esc(e.message)}</div>`
  }
  if (!toolsLoaded) return
  RUN_STATE.activeStarted = true
  try {
    const bl = await api('/bench-list')
    const dl = document.getElementById('benchList')
    dl.innerHTML = (bl.benches || []).map(b => `<option value="${esc(b)}"></option>`).join('')
    RUN_STATE.benches = bl.benches || []
  } catch { /* bench list is optional */ }
  try {
    const env = await api('/api/env-check')
    RUN_STATE.missingKeys = env.missingKeys || []
  } catch { RUN_STATE.missingKeys = [] }
  await refreshJobs().catch(() => {})
  if (RUN_STATE.pollTimer == null) {
    const timer = setInterval(() => {
      const sec = document.getElementById('settingsRunSection')
      if (sec && sec.classList.contains('active')) {
        refreshJobs().catch(() => {})
      }
    }, 2000)
    if (timer && typeof timer.unref === 'function') timer.unref()
    RUN_STATE.pollTimer = timer
  }
}

function stopRunPolling() {
  if (RUN_STATE.pollTimer) {
    clearInterval(RUN_STATE.pollTimer)
    RUN_STATE.pollTimer = null
  }
  for (const job of [...RUN_STATE.es.keys()]) detachStream(job)
}

function initSettings() {
  if (document.getElementById('settingsOverlay')) return
  const wrap = document.createElement('div')
  wrap.innerHTML = MODAL_TEMPLATE
  document.body.appendChild(wrap)
  wireGearButtons()
  bindModal()
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSettings)
  } else {
    initSettings()
  }
}

export { initSettings, GEAR_SVG, MODAL_TEMPLATE, toolCardHtml }