const api = (path, opts = {}) => fetch(path, {
  headers: { 'Content-Type': 'application/json' },
  ...opts,
}).then(r => r.json()).then(j => {
  if (!j || j.ok === false) throw new Error((j && j.error) || `request failed (${path})`)
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
        <button class="settings-nav-item" data-section="upcoming" disabled title="Coming soon">Run data</button>
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
      </div>
    </div>
  </div>
</div>

<div class="settings-overlay cm-hidden" id="modelDialogOverlay">
  <div class="settings-dialog" role="dialog" aria-modal="true">
    <header class="settings-modal-header">
      <div class="settings-modal-title" id="dlgTitle">Add model</div>
      <button class="settings-close" id="dlgClose" aria-label="Close dialog">&times;</button>
    </header>
    <div class="dialog-body">
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
          <thead><tr><th></th><th>Time</th><th>Effort</th><th>temp</th><th>max tokens</th><th>status</th></tr></thead>
          <tbody id="dlgVariantRows"></tbody>
        </table>
      </div>
      <div class="dlg-preview cm-hidden" id="dlgPreview"></div>
      <div class="dlg-error cm-hidden" id="dlgError"></div>
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
  if (!msg) { el.classList.add('cm-hidden'); el.innerHTML = ''; return }
  el.innerHTML = `<strong>${esc(msg)}</strong>`
  el.classList.remove('cm-hidden')
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
  return `<tr data-tb="${tb}" data-effort="${effort}" data-existing="${existing ? '1' : '0'}">
    <td><input type="checkbox" class="vc-check" ${existing ? 'checked' : ''} /></td>
    <td class="vc-tb">${tb}</td>
    <td class="vc-mode">${effortLabel(effort)}</td>
    <td><input type="number" step="0.1" min="0" max="2" class="vc-temp" value="${temp}" /></td>
    <td><input type="number" step="256" min="0" class="vc-max" value="${maxT}" /></td>
    <td class="vc-status ${existing ? 'vc-has' : 'vc-none'}">${status}</td>
  </tr>`
}

function openEditDialog(model) {
  const overlay = document.getElementById('modelDialogOverlay')
  const title = document.getElementById('dlgTitle')
  const createBtn = document.getElementById('dlgCreate')
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
  createBtn.textContent = 'Save changes'
  createBtn.classList.add('dlg-btn-confirm')
  createBtn.disabled = false
  overlay.dataset.mode = 'edit'
  overlay.classList.remove('cm-hidden')
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

function bindModal() {
  const overlay = document.getElementById('settingsOverlay')
  const dialogOverlay = document.getElementById('modelDialogOverlay')

  document.getElementById('settingsClose').addEventListener('click', () => {
    overlay.classList.add('cm-hidden')
  })
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.add('cm-hidden')
  })

  document.getElementById('dlgClose').addEventListener('click', closeDialog)
  document.getElementById('dlgCancel').addEventListener('click', closeDialog)
  dialogOverlay.addEventListener('click', (e) => {
    if (e.target === dialogOverlay) closeDialog()
  })

  document.querySelector('.settings-nav').addEventListener('click', (e) => {
    const item = e.target.closest('.settings-nav-item')
    if (!item || item.disabled) return
    const section = item.dataset.section
    document.querySelectorAll('.settings-nav-item').forEach(n => n.classList.toggle('active', n === item))
    document.querySelectorAll('.settings-section').forEach(s => {
      s.classList.toggle('active', s.id === `settings${section[0].toUpperCase()}${section.slice(1)}Section`)
    })
    if (section === 'models') loadModels()
  })

  document.getElementById('modelAddBtn').addEventListener('click', openAddDialog)
  document.getElementById('modelsList').addEventListener('click', handleModelsListClick)
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
    else if (!overlay.classList.contains('cm-hidden')) overlay.classList.add('cm-hidden')
  })
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

export { initSettings, GEAR_SVG, MODAL_TEMPLATE }