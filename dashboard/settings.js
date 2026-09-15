const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const ct = res.headers && res.headers.get ? (res.headers.get('content-type') || '') : ''
  let j = null
  if (ct.includes('application/json')) {
    try {
      j = await res.json()
    } catch {
      j = null
    }
  }
  const httpBad = !res.ok
  const jsonBad = j === null
  const okFalse = j !== null && j.ok !== undefined && j.ok === false
  if (httpBad || jsonBad || okFalse) {
    let msg = (j && j.error) || `request failed (${path})`
    if (httpBad) msg = `HTTP ${res.status} — ${msg}`
    if (jsonBad && ct.includes('text/html')) {
      msg = `UI banner fallback for ${path} — expected JSON API (server route/query mismatch)`
    } else if (jsonBad) {
      msg = `No JSON response from ${path} (${ct || 'no content-type'})`
    }
    if (j && j.fieldErrors) {
      const first = Object.values(j.fieldErrors)[0]
      if (first) msg = first
    }
    const err = new Error(msg)
    err.detail = j
    throw err
  }
  return j
}

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

// The document this module instance was evaluated against. Timers from a
// previous instance (tests re-import with a fresh jsdom) must not touch a newer
// document, so async callbacks bail out once the global document moves on.
const SETTINGS_DOC = typeof document !== 'undefined' ? document : null

function settingsDocCurrent() {
  return SETTINGS_DOC == null || typeof document === 'undefined' || document === SETTINGS_DOC
}

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
        <button class="settings-nav-item" data-section="runs">Runs</button>
        <button class="settings-nav-item" data-section="run">Scripts</button>
        <button class="settings-nav-item" data-section="prompts">Prompts</button>
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
        <section class="settings-section" id="settingsRunsSection">
          <div class="runs-head">
            <div>
              <div class="settings-section-title">Runs</div>
              <div class="settings-section-sub">Every run artifact across benchmarks. Judge a single run with the LLM judge, soft-hide runs being kept for reference, or permanently delete a run family.</div>
            </div>
            <div class="runs-head-actions">
              <button class="mini-btn" id="runsRefresh">Refresh</button>
            </div>
          </div>
          <div class="run-banner cm-hidden" id="runsBanner" role="status"></div>
          <div class="runs-toolbar">
            <input class="runs-search" id="runsSearch" type="search" placeholder="Filter by model, profile, run id..." spellcheck="false" />
            <select class="runs-select" id="runsModelFilter"><option value="">all models</option></select>
            <select class="runs-select" id="runsJudgedFilter">
              <option value="all">any judge</option>
              <option value="llm">LLM-judged</option>
              <option value="det">deterministic</option>
            </select>
            <select class="runs-select" id="runsDisabledFilter">
              <option value="hide">exclude hidden</option>
              <option value="include">include hidden</option>
              <option value="only">hidden only</option>
            </select>
            <span class="runs-count" id="runsCount"></span>
          </div>
          <div class="runs-list-wrap">
            <table class="runs-table">
              <thead>
                <tr>
                  <th class="runs-check"><input type="checkbox" id="runsCheckAll" title="Select all visible" aria-label="Select all runs" /></th>
                  <th>run id</th>
                  <th>bench</th>
                  <th>profile</th>
                  <th>model</th>
                  <th>judge</th>
                  <th>prompt</th>
                  <th>quality</th>
                  <th>samples</th>
                  <th>state<span class="runs-th-tip" title="active = run family lives on disk; '· N files' = the base run plus its LLM-judge sidecar JSONs (one row per file). hidden = family soft-disabled in data/disabled_runs.json, excluded from leaderboard and model stats.">ⓘ</span></th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="runsList"><tr><td colspan="11"><div class="placeholder-text">Loading runs...</div></td></tr></tbody>
            </table>
          </div>
          <div class="runs-bulkbar">
            <span class="runs-bulkcount" id="runsBulkCount"></span>
            <button class="mini-btn" id="runsBulkDisable">Hide selected</button>
            <button class="mini-btn" id="runsBulkEnable">Show selected</button>
            <button class="mini-btn mini-btn-danger" id="runsBulkDelete">Delete selected</button>
          </div>
        </section>
        <section class="settings-section" id="settingsUpcomingSection">
          <div class="placeholder-text">This settings page is coming soon.</div>
        </section>
        <section class="settings-section" id="settingsPromptsSection">
          <div class="runs-head">
            <div>
              <div class="settings-section-title">Prompts</div>
              <div class="settings-section-sub">Versioned system prompts. Test locally or against the Cloud Function without changing what production serves.</div>
            </div>
          </div>
          <div class="run-banner cm-hidden" id="promptsBanner" role="status"></div>
          <div class="prompts-panel" id="promptsPanel">
            <div class="placeholder-text">Loading prompts...</div>
          </div>
        </section>
        <section class="settings-section" id="settingsRunSection">
          <div class="runs-head">
            <div>
              <div class="settings-section-title">Scripts</div>
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

<div class="log-panel-scrim cm-hidden" id="logPanelScrim"></div>
<aside class="log-panel cm-hidden" id="logPanel" role="dialog" aria-label="Run log" aria-hidden="true">
  <header class="log-panel-head">
    <div class="log-panel-title-row">
      <span class="job-badge" id="panelBadge">…</span>
      <span class="log-panel-title" id="panelTitle"></span>
    </div>
    <div class="log-panel-status-row">
      <span class="log-panel-status" id="panelStatus" role="status"><span class="panel-state-text"></span></span>
      <span class="log-panel-elapsed" aria-hidden="true"></span>
      <span class="log-panel-reconnect" aria-hidden="true"></span>
    </div>
  </header>
  <div class="log-panel-toolbar">
    <label class="cb autoscroll-wrap"><input type="checkbox" class="log-panel-autoscroll" checked /> Auto-scroll</label>
    <button type="button" class="mini-btn log-panel-clear">Clear view</button>
    <span class="log-panel-spacer"></span>
    <a class="mini-btn log-panel-raw" target="_blank" rel="noopener">raw log</a>
    <button type="button" class="mini-btn log-panel-close" id="panelClose" aria-label="Close log panel">&times;</button>
  </div>
  <div class="log-panel-body">
    <div class="log-panel-waiting cm-hidden" id="panelWaiting">Waiting for output&hellip;</div>
    <div class="console-wrap log-panel-console"><pre class="log-panel-pre" role="log"></pre></div>
  </div>
</aside>

<div class="run-toast-stack cm-hidden" id="runToastStack" aria-live="polite" aria-label="Active runs"></div>

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
            <span class="field-hint">Effort tier per time budget. Check to show a profile, uncheck to hide it (run data kept). Checking an absent row creates it. Use &#8943; &rarr; Remove variant to delete.</span>
          </div>
          <table class="variant-table">
            <thead><tr><th></th><th>Time</th><th>Effort</th><th>temp</th><th>max tokens</th><th class="vc-judged-col">judged</th><th>status</th><th></th></tr></thead>
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
        <div class="dlg-run-progress cm-hidden" id="dlgRunProgress">
          <div class="dlg-progress-head">
            <span class="dlg-progress-badge" id="dlgProgBadge"></span>
            <span class="dlg-progress-status" id="dlgProgStatus" role="status"><span class="dlg-state-text"></span></span>
            <span class="dlg-elapsed" aria-hidden="true"></span>
            <span class="dlg-reconnect" aria-hidden="true"></span>
          </div>
          <div class="dlg-progress-hints cm-hidden" id="dlgProgHints"></div>
          <div class="dlg-progress-actions" id="dlgProgActions"></div>
          <div class="dlg-progress-console cm-hidden" id="dlgProgConsole">
            <div class="console-toolbar">
              <label class="cb autoscroll-wrap"><input type="checkbox" class="dlg-autoscroll" checked /> Auto-scroll</label>
              <button type="button" class="mini-btn dlg-console-clear" id="dlgConClear">Clear view</button>
            </div>
            <div class="console-wrap"><pre class="dlg-console-pre" role="log"></pre></div>
          </div>
          <div class="dlg-progress-waiting cm-hidden" id="dlgProgWaiting">Waiting for output&hellip;</div>
          <div class="dlg-progress-spinner cm-hidden" id="dlgProgSpinner" aria-hidden="true"></div>
        </div>
      </aside>
    </div>
    <footer class="dialog-footer">
      <button class="dlg-btn dlg-btn-ghost" id="dlgCancel">Cancel</button>
      <button class="dlg-btn dlg-btn-primary" id="dlgCreate" disabled>Create</button>
    </footer>
  </div>
</div>

<div class="settings-overlay cm-hidden" id="promptVersionOverlay">
  <div class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="pvTitle">
    <header class="settings-modal-header">
      <div class="settings-modal-title" id="pvTitle">New version</div>
      <button class="settings-close" id="pvClose" aria-label="Close version dialog">&times;</button>
    </header>
    <div class="prompt-dialog-body">
      <div class="prompt-dialog-col">
        <div class="field">
          <label for="pvStage">Stage</label>
          <select id="pvStage"><option value="chapter">chapter</option><option value="composer">composer</option></select>
          <div class="prompt-field-error cm-hidden" id="pvStageError"></div>
        </div>
        <div class="field">
          <label for="pvLabel">Label</label>
          <input id="pvLabel" type="text" maxlength="120" placeholder="v3 experiment" />
          <div class="prompt-field-error cm-hidden" id="pvLabelError"></div>
        </div>
        <div class="field">
          <label for="pvScopeKind">Scope</label>
          <select id="pvScopeKind"><option value="general">General</option><option value="model">Specific model</option></select>
          <div class="cm-hidden" id="pvScopeExtra">
            <label for="pvScopeModel" class="cm-visually-hidden">Model</label>
            <select id="pvScopeModel"></select>
            <label for="pvScopeProfiles" class="cm-visually-hidden">Profiles</label>
            <select id="pvScopeProfiles" multiple size="4" aria-label="Profiles (optional)"></select>
            <div class="field-hint">Leave profiles empty to apply to the whole model.</div>
          </div>
          <div class="prompt-field-error cm-hidden" id="pvScopeError"></div>
        </div>
        <div class="field">
          <label>Components</label>
          <div id="pvComponents" class="prompt-components"></div>
        </div>
        <div class="field">
          <label for="pvSystemTemplate">System template <span class="field-optional">optional</span></label>
          <textarea id="pvSystemTemplate" rows="4" spellcheck="false" placeholder="Leave blank to render from components"></textarea>
          <div class="field-hint">Supports {{placeholder}} variables. Use {{components}} to inline the component selection.</div>
          <div class="prompt-field-error cm-hidden" id="pvSystemTemplateError"></div>
        </div>
        <div class="field">
          <label for="pvUserTemplate">User template <span class="field-optional">optional</span></label>
          <textarea id="pvUserTemplate" rows="4" spellcheck="false" placeholder="Leave blank to use the built-in user prompt"></textarea>
          <div class="prompt-field-error cm-hidden" id="pvUserTemplateError"></div>
        </div>
        <div class="dlg-notice cm-hidden" id="pvNotice"></div>
        <div class="dlg-error cm-hidden" id="pvError"></div>
      </div>
      <div class="prompt-dialog-col is-preview">
        <div class="settings-section-title">Preview</div>
        <div class="prompt-preview-panes">
          <div>
            <div class="prompt-preview-meta">System prompt</div>
            <pre class="prompt-preview-pane" id="pvSystemPreview" role="region" aria-label="System prompt preview">Rendering…</pre>
          </div>
          <div>
            <div class="prompt-preview-meta">User prompt</div>
            <pre class="prompt-preview-pane" id="pvUserPreview" role="region" aria-label="User prompt preview">Rendering…</pre>
          </div>
        </div>
        <div class="prompt-preview-meta" id="pvPreviewMeta"></div>
        <div class="prompt-placeholder-legend" id="pvLegend"></div>
        <div class="prompt-field-error cm-hidden" id="pvWarnings"></div>
      </div>
    </div>
    <footer class="dialog-footer">
      <button class="dlg-btn dlg-btn-ghost" id="pvCancel">Cancel</button>
      <button class="dlg-btn dlg-btn-primary" id="pvSave">Create version</button>
    </footer>
  </div>
</div>

<div class="settings-overlay cm-hidden" id="confirmDeleteOverlay">
  <div class="settings-dialog confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirmDeleteTitle">
    <header class="settings-modal-header">
      <div class="settings-modal-title" id="confirmDeleteTitle">Delete reasoning variants permanently?</div>
      <button class="settings-close" id="confirmDeleteClose" aria-label="Close confirmation">&times;</button>
    </header>
    <div class="dialog-body">
      <div class="confirm-impact" id="confirmImpact"></div>
      <div class="dlg-notice cm-hidden" id="confirmNotice"></div>
      <div class="dlg-error cm-hidden" id="confirmError"></div>
      <p class="confirm-warn"><strong>Irreversible.</strong> The selected variants' run files, job logs, and results.tsv rows will be permanently deleted.</p>
    </div>
    <footer class="dialog-footer">
      <button class="dlg-btn dlg-btn-ghost" id="confirmDeleteCancel">Cancel</button>
      <button class="dlg-btn dlg-btn-danger" id="confirmDeleteButton" disabled>Delete permanently</button>
    </footer>
  </div>
</div>`

const EFFORT_ORDER = ['thinking', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const EFFORT_LABEL = { thinking: 'think', none: 'off' }
const CANONIC_EFFORTS = new Set(['thinking'])
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
    if (p.hidden) continue
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
    const visible = m.profiles.filter(p => !p.hidden)
    const chipsHtml = modelCells(m).map(({ tb, effort, profile: p }) => {
      const statusCls = p.status === 'tested' ? 'is-tested' : 'is-pending'
      const statusTxt = p.status === 'tested' ? 'tested' : 'pending'
      return `<span class="profile-chip ${statusCls}" title="${esc(p.slug)}">
        <span class="pc-budget ${tb === '60m' ? 'is-60' : ''}">${tb}</span><span class="pc-mode">${effortLabel(effort)}</span>
        <span class="pc-status">${statusTxt}</span>
      </span>`
    }).join('')
    const tested = visible.some(p => p.status === 'tested')
    const chips = chipsHtml || '<span class="profile-chip is-empty" title="No reasoning variants selected">no variants selected &mdash; edit to add</span>'
    const det = fmtQuality(m.best_quality_det)
    const llm = fmtQuality(m.best_quality_llm)
    return `<div class="model-card" data-model="${esc(m.model)}">
      <div class="model-row">
        <div class="model-dot"></div>
        <div class="model-main">
          <div class="model-name">${esc(m.model)}</div>
          <div class="model-sub">${visible.length} profile${visible.length === 1 ? '' : 's'} &middot; ${m.runs_count} run${m.runs_count === 1 ? '' : 's'} &middot; last tested ${fmtDate(m.last_tested)}</div>
        </div>
        <div class="model-stats">
          <div class="ms-col"><span class="ms-k">det Q</span><span class="ms-v ${tested ? '' : 'ms-muted'}">${det}</span></div>
          <div class="ms-col"><span class="ms-k">LLM Q</span><span class="ms-v ${tested ? '' : 'ms-muted'}">${llm}</span></div>
        </div>
        <div class="model-actions">
          <button class="mini-btn" data-action="runs">Runs</button>
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
    refreshOpenVariantDialog(models)
  } catch (e) {
    list.innerHTML = `<div class="placeholder-text">Failed to load models: ${esc(e.message)}</div>`
  }
}

// ── Runs manager (per-run list/judge/hide/delete) ─────────────

const RUNS_STATE = { runs: [], models: [], filter: { q: '', model: '', judge: 'all', disabled: 'hide' }, preset: null }

function runsFamilyId(run) {
  const i = run.family_id.indexOf('__llmj_')
  return i >= 0 ? run.family_id.slice(0, i) : run.family_id
}

function setRunsBanner(msg, tone) {
  const b = document.getElementById('runsBanner')
  if (!b) return
  if (!msg) { b.classList.add('cm-hidden'); b.innerHTML = ''; return }
  b.innerHTML = msg
  b.classList.toggle('run-banner-err', tone === 'error')
  b.classList.remove('cm-hidden')
}

function runsFamilyOf(file) {
  let base = String(file).replace(/\.json$/, '')
  const slash = base.lastIndexOf('/')
  if (slash >= 0) base = base.slice(slash + 1)
  const i = base.indexOf('__llmj_')
  if (i >= 0) base = base.slice(0, i)
  return base
}

async function loadRunsManager() {
  const list = document.getElementById('runsList')
  try {
    const [modelsRes, runsRes] = await Promise.all([
      api('/api/models'),
      api(`/api/runs?disabled=${RUNS_STATE.filter.disabled}`),
    ])
    RUNS_STATE.models = (modelsRes.models || []).map(m => m.model).sort()
    RUNS_STATE.runs = runsRes.runs || []
    if (RUNS_STATE.preset && RUNS_STATE.preset.model) {
      RUNS_STATE.filter.model = RUNS_STATE.preset.model
      RUNS_STATE.preset = null
    }
    renderRunsManager()
  } catch (e) {
    list.innerHTML = `<div class="placeholder-text">Failed to load runs: ${esc(e.message)}</div>`
  }
}

function renderRunsManager() {
  const list = document.getElementById('runsList')
  const f = RUNS_STATE.filter
  const modelSel = document.getElementById('runsModelFilter')
  if (modelSel && modelSel.options.length !== RUNS_STATE.models.length + 1) {
    modelSel.innerHTML = '<option value="">all models</option>' + RUNS_STATE.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')
    modelSel.value = RUNS_STATE.models.includes(f.model) ? f.model : ''
  }
  const judgedSel = document.getElementById('runsJudgedFilter')
  if (judgedSel && judgedSel.value !== f.judge) judgedSel.value = f.judge
  const disabledSel = document.getElementById('runsDisabledFilter')
  if (disabledSel && disabledSel.value !== f.disabled) disabledSel.value = f.disabled
  const q = f.q.trim().toLowerCase()
  let out = RUNS_STATE.runs
  if (f.model) out = out.filter(r => r.chapter_model === f.model)
  if (f.judge === 'llm') out = out.filter(r => r.is_llm)
  if (f.judge === 'det') out = out.filter(r => !r.is_llm)
  if (f.disabled === 'hide') out = out.filter(r => !r.disabled)
  if (f.disabled === 'only') out = out.filter(r => r.disabled)
  if (q) {
    out = out.filter(r => (r.run_id + ' ' + r.profile + ' ' + r.chapter_model + ' ' + r.bench).toLowerCase().includes(q))
  }
  const count = document.getElementById('runsCount')
  if (count) count.textContent = `${out.length} of ${RUNS_STATE.runs.length} run${RUNS_STATE.runs.length === 1 ? '' : 's'}`
  const checkAll = document.getElementById('runsCheckAll')
  if (checkAll) checkAll.checked = false
  const bulk = document.getElementById('runsBulkCount')
  if (bulk) bulk.textContent = ''
  if (!out.length) {
    list.innerHTML = `<tr><td colspan="11"><div class="placeholder-text">No runs match the current filters.</div></td></tr>`
    return
  }
  list.innerHTML = out.slice().sort((a, b) => `${a.bench}/${a.run_id}`.localeCompare(`${b.bench}/${b.run_id}`))
    .map(r => {
      const famId = runsFamilyId(r)
      const judgeCell = r.is_llm ? `<span class="runs-badge runs-badge-llm" title="${esc(r.judge_model || '')} · ${esc(r.judge_version_resolved || '')}">LLM:${esc((r.judge_model || '').split('/').pop())}</span>`
        : (r.family_judged ? `<span class="runs-badge runs-badge-det" title="a ${esc(r.judge_model || 'LLM')} sidecar exists in this family">det + llm</span>` : `<span class="runs-badge runs-badge-det">deterministic</span>`)
      const stateCell = r.disabled
        ? `<span class="runs-state runs-state-hidden">hidden</span>`
        : (r.family_file_count > 1 ? `<span class="runs-state">active · ${r.family_file_count} files</span>` : `<span class="runs-state">active</span>`)
      return `<tr class="${r.disabled ? 'runs-row-hidden' : ''}">
        <td class="runs-check"><input type="checkbox" data-runs-check="${esc(famId)}" aria-label="Select run" /></td>
        <td class="runs-id" title="${esc(r.file)}"><code>${esc(runIdShort(r.run_id))}</code></td>
        <td>${esc(r.bench)}</td>
        <td>${esc(r.profile)}</td>
        <td>${esc(r.chapter_model)}</td>
        <td>${judgeCell}</td>
        <td class="runs-prompt" title="${r.prompt_hash_full ? `chapter prompt ${esc(r.prompt_hash_full)}` : ''}"><span>${r.prompt_version ? esc(r.prompt_version) : '<span class="muted">—</span>'}${r.composer_prompt_version && r.family_has_composer && r.composer_prompt_version !== r.prompt_version ? ` <span class="muted">· ${esc(r.composer_prompt_version)}</span>` : ''}</span></td>
        <td>${fmtQuality(r.mean_quality)}</td>
        <td>${r.n_samples}</td>
        <td>${stateCell}</td>
        <td class="runs-actions">
          <button class="mini-btn" data-run-action="judge" data-family="${esc(famId)}" data-bench="${esc(r.bench)}" ${r.is_llm ? '' : ''}>Judge</button>
          ${r.disabled
            ? `<button class="mini-btn" data-run-action="enable" data-family="${esc(famId)}">Show</button>`
            : `<button class="mini-btn" data-run-action="disable" data-family="${esc(famId)}">Hide</button>`}
          <button class="mini-btn mini-btn-danger" data-run-action="delete" data-family="${esc(famId)}">Delete</button>
        </td>
      </tr>`
    }).join('')
}

function runIdShort(id) {
  if (!id) return ''
  if (id.length <= 56) return id
  return `${id.slice(0, 28)}…${id.slice(-24)}`
}

function runsCheckedFamilies() {
  const fams = []
  document.querySelectorAll('#runsList input[data-runs-check]:checked').forEach(cb => fams.push(cb.dataset.runsCheck))
  return [...new Set(fams)]
}

async function runRunsAction(action, familyId, opts = {}) {
  if (action === 'judge') {
    const bench = opts.bench || ''
    const remembered = storageGet('mm.judgeModel') || prefJudgeModel || DEFAULT_JUDGE_MODEL
    let entered = null
    try { entered = window.prompt('Judge model (e.g. openai/gpt-5.4-mini):', remembered) } catch { entered = null }
    if (!entered || !entered.trim()) return
    const judgeModel = entered.trim()
    storageSet('mm.judgeModel', judgeModel)
    launchProfileJob('judge_existing', {
      bench,
      run_id: familyId,
      'judge-model': judgeModel,
    }, `LLM judge of run <code>${esc(runIdShort(familyId))}</code> on <code>${esc(bench)}</code> with <code>${esc(judgeModel)}</code>`)
    return
  }
  if (action === 'disable') {
    try { await api('/api/runs/disable', { method: 'POST', body: JSON.stringify({ runId: familyId }) }) }
    catch (e) { setRunsBanner(esc(e.message), 'error'); return }
  } else if (action === 'enable') {
    try { await api('/api/runs/enable', { method: 'POST', body: JSON.stringify({ runId: familyId }) }) }
    catch (e) { setRunsBanner(esc(e.message), 'error'); return }
  } else if (action === 'delete') {
    const ok = window.confirm(`Permanently delete run family?\n\n${runIdShort(familyId)}\n\nDeletes the run JSON, samples, state, and llmj sidecar files, plus its results.tsv rows. This cannot be undone.`)
    if (!ok) return
    try {
      await api('/api/runs/delete', { method: 'POST', body: JSON.stringify({ runId: familyId }) })
      setRunsBanner(`Deleted run family <code>${esc(runIdShort(familyId))}</code>.`)
    } catch (e) {
      setRunsBanner(esc(e.message), 'error')
      return
    }
  }
  await loadRunsManager()
}

async function runsBulkAction(action) {
  const fams = runsCheckedFamilies()
  if (!fams.length) { setRunsBanner('Select at least one run first.', 'error'); return }
  if (action === 'delete') {
    const ok = window.confirm(`Permanently delete ${fams.length} run famil${fams.length === 1 ? 'y' : 'ies'}?\n\nThis cannot be undone.`)
    if (!ok) return
  }
  for (const fam of fams) {
    try {
      if (action === 'disable') await api('/api/runs/disable', { method: 'POST', body: JSON.stringify({ runId: fam }) })
      else if (action === 'enable') await api('/api/runs/enable', { method: 'POST', body: JSON.stringify({ runId: fam }) })
      else if (action === 'delete') await api('/api/runs/delete', { method: 'POST', body: JSON.stringify({ runId: fam }) })
    } catch (e) {
      setRunsBanner(`${esc(action === 'delete' ? 'Delete' : action)} failed for <code>${esc(runIdShort(fam))}</code>: ${esc(e.message)}`, 'error')
      return
    }
  }
  await loadRunsManager()
  setRunsBanner(action === 'delete' ? `Deleted ${fams.length} run famil${fams.length === 1 ? 'y' : 'ies'}.` : '')
}

function bindRunsManager() {
  const section = document.getElementById('settingsRunsSection')
  if (!section || section.dataset.bound) return
  section.dataset.bound = '1'
  document.getElementById('runsRefresh').addEventListener('click', () => loadRunsManager().catch(() => {}))
  document.getElementById('runsSearch').addEventListener('input', (e) => { RUNS_STATE.filter.q = e.target.value; renderRunsManager() })
  document.getElementById('runsModelFilter').addEventListener('change', (e) => { RUNS_STATE.filter.model = e.target.value; renderRunsManager() })
  document.getElementById('runsJudgedFilter').addEventListener('change', (e) => { RUNS_STATE.filter.judge = e.target.value; renderRunsManager() })
  document.getElementById('runsDisabledFilter').addEventListener('change', (e) => {
    RUNS_STATE.filter.disabled = e.target.value
    loadRunsManager().catch(() => renderRunsManager())
  })
  document.getElementById('runsList').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-run-action]')
    if (!btn) return
    runRunsAction(btn.dataset.runAction, btn.dataset.family, { bench: btn.dataset.bench })
  })
  document.getElementById('runsCheckAll').addEventListener('change', (e) => {
    document.querySelectorAll('#runsList input[data-runs-check]').forEach(cb => { cb.checked = e.target.checked })
    updateRunsBulkCount()
  })
  document.getElementById('runsList').addEventListener('change', updateRunsBulkCount)
  document.getElementById('runsBulkDisable').addEventListener('click', () => runsBulkAction('disable'))
  document.getElementById('runsBulkEnable').addEventListener('click', () => runsBulkAction('enable'))
  document.getElementById('runsBulkDelete').addEventListener('click', () => runsBulkAction('delete'))
}

function updateRunsBulkCount() {
  const n = runsCheckedFamilies().length
  const el = document.getElementById('runsBulkCount')
  if (el) el.textContent = n ? `${n} selected` : ''
}

// Updates the judged/status/runs-dir cells of an open edit dialog in place, so a
// completed run refreshes the variant list without clobbering unsaved edits.
function refreshOpenVariantDialog(models) {
  const overlay = document.getElementById('modelDialogOverlay')
  if (!overlay || overlay.classList.contains('cm-hidden') || overlay.dataset.mode !== 'edit') return
  let editing = null
  try { editing = JSON.parse(overlay.dataset.editingModel || 'null') } catch { editing = null }
  if (!editing) return
  const fresh = models.find(m => m.model === editing.model)
  if (!fresh) return
  const byKey = new Map()
  for (const p of fresh.profiles || []) byKey.set(`${p.time_budget}_${effortOf(p.slug)}`, p)
  overlay.dataset.editingModel = JSON.stringify(fresh)
  for (const tr of overlay.querySelectorAll('#dlgVariantRows tr')) {
    if (tr.dataset.existing !== '1') continue
    const p = byKey.get(`${tr.dataset.tb}_${tr.dataset.effort}`)
    if (!p) continue
    const hidden = p.hidden === true
    const status = hidden ? 'hidden' : (p.status === 'tested' ? 'tested' : 'pending')
    tr.dataset.runsDir = p.runsDir || ''
    tr.dataset.origStatus = status
    const judgedCell = tr.querySelector('.vc-judged')
    if (judgedCell) {
      judgedCell.textContent = p.judged ? 'yes' : 'no'
      judgedCell.classList.toggle('vc-has', !!p.judged)
      judgedCell.classList.toggle('vc-none', !p.judged)
    }
    const statusCell = tr.querySelector('.vc-status')
    if (statusCell) {
      statusCell.textContent = status
      statusCell.classList.toggle('vc-hidden-status', hidden)
      statusCell.classList.toggle('vc-has', !hidden)
      statusCell.classList.toggle('vc-none', hidden)
    }
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
    ? `<span class="probe-badge probe-ok">efforts: ${probe.efforts.map(e => esc(e === 'none' ? 'none (off)' : e)).join(', ')}</span>`
    : ''
  const legacyLine = (probe.thinking != null || probe.notthinking != null)
    ? `${box(probe.thinking === true, 'legacy thinking')}${box(probe.notthinking === true, 'legacy non-thinking')}`
    : ''
  const profiles = created.length
    ? `<div class="probe-profiles">${created.map(c => `<span class="probe-profile">${esc(c)}</span>`).join('')}</div>`
    : '<div class="probe-none">No candidate profiles would be created.</div>'
  const pricing = renderPricing(probe && probe.pricing)
  return `<div class="probe-results">${[schemaLine, effortChips, legacyLine].filter(Boolean).join('')}</div>${pricing}
  <div class="probe-create-label">${created.length ? `Will create ${created.length} profile${created.length === 1 ? '' : 's'} for ${esc(model)}:` : ''}</div>${profiles}`
}

function renderPricing(pricing) {
  if (!pricing || !Array.isArray(pricing.tiers) || !pricing.tiers.length) return ''
  const fmt = (v) => (v == null || v === 0) ? '&ndash;' : `$${v}`
  const rows = [...pricing.tiers].sort((a, b) => a.min_context - b.min_context)
    .map(t => `<tr>
      <td>${t.min_context ? `${esc(t.min_context.toLocaleString())} tok` : '&ndash;'}</td>
      <td>${fmt(esc(t.input_cost_per_million))}</td>
      <td>${fmt(esc(t.output_cost_per_million))}</td>
      <td>${fmt(esc(t.cached_input_cost_per_million))}</td>
    </tr>`).join('')
  const ctxLine = pricing.context_length ? `<div class="pricing-ctx">context: ${esc(pricing.context_length.toLocaleString())} tokens</div>` : ''
  return `<div class="pricing-box">
    <div class="pricing-label">Pricing <span class="field-hint">per 1M tokens</span></div>
    ${ctxLine}
    <table class="pricing-table">
      <thead><tr><th>min context</th><th>input</th><th>output</th><th>cached input</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`
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

const LOCKED_MENU_ACTIONS = ['run', 'prefill', 'judge', 'agent']

function variantRowHtml({ tb, effort, existing, locked }) {
  const hidden = existing && existing.hidden === true
  const temp = existing && existing.temperature != null ? existing.temperature : DEFAULTS.temperature
  const maxT = existing && existing.max_tokens != null ? existing.max_tokens : effortDefaultMaxTokens(effort)
  const status = !existing ? 'not created' : hidden ? 'hidden' : (existing.status === 'tested' ? 'tested' : 'pending')
  const judged = !existing ? '\u2014' : (existing.judged ? 'yes' : 'no')
  const judgedCls = !existing ? 'vc-none' : (existing.judged ? 'vc-has' : 'vc-none')
  const numAttrs = locked ? ' readonly' : ''
  const runsDir = existing && existing.runsDir ? esc(existing.runsDir) : ''
  const opt = existing
    ? `<button type="button" class="vc-opt-btn" data-slug="${esc(existing.slug)}" aria-haspopup="menu" aria-expanded="false" aria-label="Options for ${esc(existing.slug)}" title="Profile options">\u22EF</button>
      <div class="vc-opt-menu cm-hidden" role="menu">
        ${LOCKED_MENU_ACTIONS.map(a => `<button type="button" class="vc-opt-item" data-action="${a}" role="menuitem"${locked ? ' disabled' : ''}>${{ run: 'Run candidate now', prefill: 'Run with options\u2026', judge: 'Re-judge (LLM)\u2026', agent: 'Autoresearch agent\u2026' }[a]}</button>`).join('')}
        <button type="button" class="vc-opt-item vc-opt-remove" data-action="remove" role="menuitem">Remove variant</button>
      </div>`
    : ''
  return `<tr data-tb="${tb}" data-effort="${effort}" data-existing="${existing ? '1' : '0'}"${locked ? ' data-locked="1"' : ''} data-orig-status="${status}" data-runs-dir="${runsDir}" class="${locked ? 'vc-locked' : ''}">
    <td><input type="checkbox" class="vc-check" ${existing && !hidden ? 'checked' : ''}${locked ? ' disabled' : ''} /></td>
    <td class="vc-tb">${tb}</td>
    <td class="vc-mode"${locked ? ` data-tooltip="Canonical effort profiles cannot be modified. Remove variant to delete this baseline."` : ''}>${effortLabel(effort)}</td>
    <td><input type="number" step="0.1" min="0" max="2" class="vc-temp" value="${temp}"${numAttrs} /></td>
    <td><input type="number" step="256" min="0" class="vc-max" value="${maxT}"${numAttrs} /></td>
    <td class="vc-judged ${judgedCls}">${judged}</td>
    <td class="vc-status ${hidden ? 'vc-hidden-status' : (existing ? 'vc-has' : 'vc-none')}">${status}</td>
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
  const route = (model.profiles[0] && model.profiles[0].provider_route) || model.provider_route || ''
  document.getElementById('dlgProviderRoute').value = route
  document.getElementById('dlgBudgetsField').classList.add('cm-hidden')
  document.getElementById('dlgVariantsField').classList.remove('cm-hidden')

  const specs = new Map()
  model.profiles.forEach(p => specs.set(`${p.time_budget}_${effortOf(p.slug)}`, p))
  const rows = [].concat(...BUDGETS.map(tb => EFFORT_ORDER.map(ef => {
    const existing = specs.get(`${tb}_${ef}`)
    if (CANONIC_EFFORTS.has(ef)) {
      if (!existing) return '' // absent cell: hidden entirely
      return variantRowHtml({ tb, effort: ef, existing, locked: true })
    }
    return variantRowHtml({ tb, effort: ef, existing })
  }))).filter(Boolean).join('')
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
    const removing = tr.classList.contains('vc-to-remove')
    const entry = { time_budget: tb, effort }
    if (isFinite(temp)) entry.temperature = temp
    if (isFinite(maxT)) entry.max_tokens = maxT
    if (existing) {
      if (tr.dataset.locked === '1') {
        if (!removing) continue // locked baseline kept as-is (cannot be hidden)
      }
      entry.key = existing.slug
      entry.keep = !removing
      if (!removing) entry.hidden = !checked
      edits.push(entry)
    } else if (checked) {
      create.push(entry)
    }
  }
  return { edits, create }
}

// ── per-profile quick actions (edit dialog ⋯ menus) ──────────

const PROFILE_META = new Map()
const DEFAULT_JUDGE_MODEL = 'openai/gpt-5.4-mini'
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
        if (job.toolId === 'run_candidate' && a.bench) meta.lastBench = a.bench
        if (a['judge-model']) meta.lastJudgeModel = a['judge-model']
        const concurrency = Number(a['chapter-concurrency'])
        if (Number.isInteger(concurrency) && concurrency > 1) meta.lastConcurrency = concurrency
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
  const meta = PROFILE_META.get(slug) || {}
  const args = { bench: benchFor(slug), profile: slug, time: tb || 'all', ...extra }
  const concurrency = Number(meta.lastConcurrency)
  if (Number.isInteger(concurrency) && concurrency > 1) args['chapter-concurrency'] = concurrency
  return args
}

function closeProfileMenus() {
  document.querySelectorAll('#dlgVariantRows .vc-opt-btn').forEach(b => b.setAttribute('aria-expanded', 'false'))
  document.querySelectorAll('#dlgVariantRows .vc-opt-menu').forEach(m => m.classList.add('cm-hidden'))
}

function toggleRowRemoval(row) {
  const statusEl = row.querySelector('.vc-status')
  if (!row) return
  const removing = row.classList.toggle('vc-to-remove')
  if (statusEl) statusEl.textContent = removing ? 'will remove' : (row.dataset.origStatus || '')
}

function profileMenuAction(slug, action, row) {
  const tb = row && row.dataset.tb ? row.dataset.tb : 'all'
  closeProfileMenus()
  if (action === 'remove') {
    if (row) toggleRowRemoval(row)
    return
  }
  if (LOCKED_MENU_ACTIONS.includes(action) && row && row.dataset.locked === '1') return
  if (action === 'run') {
    launchProfileJob('run_candidate', buildProfileArgs(slug, tb, { 'write-results': true }), `run_candidate on <code>${esc(benchFor(slug))}</code> for <code>${esc(slug)}</code>`)
    return
  }
  if (action === 'prefill') {
    openRunProfilePrefill(slug, tb)
    return
  }
  if (action === 'judge') {
    const remembered = storageGet('mm.judgeModel') || prefJudgeModel || DEFAULT_JUDGE_MODEL
    let entered = null
    try { entered = window.prompt('Judge model (e.g. openai/gpt-5.4-mini):', remembered) } catch { entered = null }
    if (!entered || !entered.trim()) return
    const judgeModel = entered.trim()
    storageSet('mm.judgeModel', judgeModel)
    const judgeBench = (row && row.dataset.runsDir) || benchFor(slug)
    launchProfileJob('judge_existing', {
      bench: judgeBench,
      'judge-model': judgeModel,
      profile: slug,
    }, `re-judge of <code>${esc(slug)}</code> on <code>${esc(judgeBench)}</code> with <code>${esc(judgeModel)}</code>`)
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
      setDialogNotice(`Launched <strong>${esc(toolId)}</strong>: ${label}. Job id <code>${shortId}…</code> — watch it stream in <strong>Scripts</strong>.`)
      refreshJobs().catch(() => {})
    })
    .catch(e => setDialogNotice(esc(e.message), 'error'))
}

function clearInlineRunWidget() {
  const dialog = document.getElementById('modelDialogOverlay')
  if (!dialog) return
  resetDlgPane()
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
    setDialogNotice('Scripts failed to load — check the dev server and try again.', 'error')
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
  setDialogNotice('Tune any option below, then hit <strong>Run script</strong>. The job queues behind other harness runs and streams live in <strong>Scripts</strong>.')
  updateToolGuards()
}

// ── in-dialog run progress (sidebar under the prefill widget) ──

const DLG_TERMINAL = ['succeeded', 'failed', 'canceled', 'interrupted']
const STALE_MS = 150_000

function dlgProg() {
  return document.getElementById('dlgRunProgress')
}

function dlgStatusEl() {
  return document.getElementById('dlgProgStatus')
}

function dlgActionsEl() {
  return document.getElementById('dlgProgActions')
}

function dlgBadgeEl() {
  return document.getElementById('dlgProgBadge')
}

function stopDlgTimer() {
  if (RUN_STATE.dlgTimer) {
    clearInterval(RUN_STATE.dlgTimer)
    RUN_STATE.dlgTimer = null
  }
}

function detachDlgStream(job) {
  if (job == null) return
  const es = RUN_STATE.dlgEs.get(job)
  if (es) {
    es.close()
    RUN_STATE.dlgEs.delete(job)
  }
}

function resetDlgPane() {
  stopDlgTimer()
  for (const job of [...RUN_STATE.dlgEs.keys()]) detachDlgStream(job)
  RUN_STATE.dlgLogs = []
  RUN_STATE.dlgJobId = null
  RUN_STATE.dlgStatus = ''
  RUN_STATE.dlgDuplicate = false
  RUN_STATE.dlgReconnecting = false
  RUN_STATE.dlgStartedAt = null
  RUN_STATE.dlgFinishedAt = null
  RUN_STATE.dlgError = ''
  RUN_STATE.dlgLastJob = null
  RUN_STATE.dlgLastOutputAt = null
  const p = dlgProg()
  if (!p) return
  p.classList.add('cm-hidden')
  for (const id of ['dlgProgBadge', 'dlgProgActions', 'dlgProgHints']) {
    const el = document.getElementById(id)
    if (el) el.innerHTML = ''
  }
  const statusEl = document.getElementById('dlgProgStatus')
  const stateText = statusEl && statusEl.querySelector('.dlg-state-text')
  if (stateText) stateText.textContent = ''
  for (const id of ['dlgProgConsole', 'dlgProgWaiting', 'dlgProgSpinner']) {
    const el = document.getElementById(id)
    if (el) el.classList.add('cm-hidden')
  }
  const pre = document.querySelector('#dlgRunProgress .dlg-console-pre')
  if (pre) pre.textContent = ''
}

function dlgQueuePosition() {
  if (!RUN_STATE.dlgJobId) return 0
  const queued = RUN_STATE.jobs.filter(j => j.status === 'queued')
  const idx = queued.findIndex(j => j.id === RUN_STATE.dlgJobId)
  return idx === -1 ? 0 : idx + 1
}

function dlgDurationText() {
  if (!RUN_STATE.dlgStartedAt) return ''
  return durationStr(RUN_STATE.dlgStartedAt, RUN_STATE.dlgFinishedAt || null)
}

function toolStaleLine(lastOutputAt) {
  if (!lastOutputAt) return ''
  const gap = Date.now() - new Date(lastOutputAt).getTime()
  if (gap < STALE_MS) return ''
  const mins = Math.floor(gap / 60_000)
  return ` · stale — no output for ${mins} m`
}

function renderDlgStatusLine() {
  const statusEl = dlgStatusEl()
  if (!statusEl) return
  const s = RUN_STATE.dlgStatus || ''
  let state = (STATUS_BADGE[s] && STATUS_BADGE[s][1]) || s
  if (s === 'queued') {
    if (RUN_STATE.dlgDuplicate) {
      const id = (RUN_STATE.dlgLastJob && RUN_STATE.dlgLastJob.id) || ''
      state = `Already queued — identical job ${id.slice(0, 8)}…`
    } else {
      const pos = dlgQueuePosition()
      const total = RUN_STATE.jobs.filter(j => j.status === 'queued').length
      state = pos > 0 ? `Queued — position ${pos} of ${total} (run harness busy)` : 'Queued — waiting for a free slot'
    }
  } else if (s === 'failed' && RUN_STATE.dlgError) {
    state += ` · ${RUN_STATE.dlgError}`
  }
  const stateText = statusEl.querySelector('.dlg-state-text')
  if (stateText) stateText.textContent = state
  const head = statusEl.parentElement
  if (!head) return
  const elapsed = head.querySelector('.dlg-elapsed')
  const reconnect = head.querySelector('.dlg-reconnect')
  if (elapsed) elapsed.textContent = dlgDurationText() ? ` — ${dlgDurationText()}` : ''
  if (reconnect) {
    const isStale = s === 'running' && RUN_STATE.dlgLastOutputAt != null
    if (RUN_STATE.dlgReconnecting) reconnect.textContent = ' · reconnecting'
    else if (isStale && Date.now() - new Date(RUN_STATE.dlgLastOutputAt).getTime() >= STALE_MS) {
      reconnect.textContent = toolStaleLine(RUN_STATE.dlgLastOutputAt)
    } else reconnect.textContent = ''
  }
}

function startDlgTimer() {
  stopDlgTimer()
  RUN_STATE.dlgTimer = setInterval(() => {
    if (!RUN_STATE.dlgStatus || DLG_TERMINAL.includes(RUN_STATE.dlgStatus)) {
      stopDlgTimer()
      return
    }
    if (RUN_STATE.dlgStatus !== 'queued') renderDlgStatusLine()
  }, 1000)
  if (RUN_STATE.dlgTimer && typeof RUN_STATE.dlgTimer.unref === 'function') RUN_STATE.dlgTimer.unref()
}

function syncDlgConsoleView() {
  const consoleBox = document.getElementById('dlgProgConsole')
  const waiting = document.getElementById('dlgProgWaiting')
  const spinner = document.getElementById('dlgProgSpinner')
  const has = RUN_STATE.dlgLogs.length > 0
  const active = !!RUN_STATE.dlgStatus && !DLG_TERMINAL.includes(RUN_STATE.dlgStatus)
  if (consoleBox) consoleBox.classList.toggle('cm-hidden', !has)
  if (waiting) waiting.classList.toggle('cm-hidden', has)
  if (spinner) spinner.classList.toggle('cm-hidden', has || !active)
}

function dlgConsoleHtml() {
  return RUN_STATE.dlgLogs.map(l =>
    l.stream === 'stderr' ? `<span class="con-err">${esc(l.text)}</span>` : esc(l.text),
  ).join('\n')
}

function appendDlgLog(ev) {
  RUN_STATE.dlgReconnecting = false
  RUN_STATE.dlgLastOutputAt = new Date().toISOString()
  RUN_STATE.dlgLogs.push(ev)
  if (RUN_STATE.dlgLogs.length > 400) RUN_STATE.dlgLogs.splice(0, RUN_STATE.dlgLogs.length - 400)
  const pre = document.querySelector('#dlgRunProgress .dlg-console-pre')
  const wrap = document.querySelector('#dlgRunProgress .console-wrap')
  const auto = document.querySelector('#dlgRunProgress .dlg-autoscroll')
  if (pre) pre.innerHTML = dlgConsoleHtml()
  if (wrap && auto && auto.checked && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    wrap.scrollTop = wrap.scrollHeight
  }
  syncDlgConsoleView()
}

function clearDlgConsole() {
  RUN_STATE.dlgLogs = []
  const pre = document.querySelector('#dlgRunProgress .dlg-console-pre')
  if (pre) pre.textContent = ''
  syncDlgConsoleView()
}

function handleDlgStatus(job, status) {
  if (!status || !status.status) return
  if (!RUN_STATE.dlgJobId || RUN_STATE.dlgJobId !== job) return
  const s = status.status
  RUN_STATE.dlgReconnecting = false
  RUN_STATE.dlgStatus = s
  if (status.error) RUN_STATE.dlgError = status.error
  if (!DLG_TERMINAL.includes(s)) RUN_STATE.dlgLastOutputAt = new Date().toISOString()
  if (DLG_TERMINAL.includes(s)) {
    stopDlgTimer()
    detachDlgStream(job)
    renderDlgTerminal({
      id: job,
      status: s,
      exitCode: status.exitCode,
      error: status.error || RUN_STATE.dlgError,
      resultHints: status.resultHints || {},
    })
    refreshJobs().catch(() => {})
    return
  }
  if (s === 'running' && !RUN_STATE.dlgStartedAt) {
    RUN_STATE.dlgStartedAt = new Date().toISOString()
    startDlgTimer()
  }
  const badge = dlgBadgeEl()
  if (badge) badge.innerHTML = statusBadgeHtml(s)
  renderDlgStatusLine()
  syncDlgConsoleView()
}

function renderDlgHints(resultHints) {
  const el = document.getElementById('dlgProgHints')
  if (!el) return
  const html = dlgHintsHtml(resultHints)
  el.innerHTML = html
  el.classList.toggle('cm-hidden', !html)
}

function dlgHintsHtml(resultHints) {
  if (!resultHints) return ''
  const parts = []
  if (resultHints.runId) parts.push(`Run ID: <code>${esc(resultHints.runId)}</code>`)
  if (resultHints.resultsTsvUpdated) parts.push('results table updated')
  return parts.length ? parts.join(' · ') : ''
}

function renderDlgActions(jobId, resultHints = {}) {
  const box = dlgActionsEl()
  if (!box) return
  const id = jobId || RUN_STATE.dlgJobId || ''
  const actions = []
  const terminal = DLG_TERMINAL.includes(RUN_STATE.dlgStatus)
  if (terminal && resultHints && (resultHints.bench || resultHints.runId)) {
    actions.push(`<button type="button" class="mini-btn dlg-prog-explorer">Open in explorer</button>`)
  }
  if (terminal) actions.push(`<button type="button" class="mini-btn dlg-prog-rerun">Re-run</button>`)
  if (id) {
    actions.push(`<button type="button" class="mini-btn dlg-prog-log" data-job="${encodeURIComponent(id)}">view log</button>`)
  }
  actions.push(`<button type="button" class="mini-btn dlg-prog-runs">Open in Scripts</button>`)
  actions.push(`<button type="button" class="mini-btn dlg-prog-close">Close</button>`)
  box.innerHTML = actions.join('')
  const rerun = box.querySelector('.dlg-prog-rerun')
  if (rerun) rerun.addEventListener('click', rerunDlgJob)
  const logBtn = box.querySelector('.dlg-prog-log')
  if (logBtn) logBtn.addEventListener('click', () => openLogPanel(decodeURIComponent(logBtn.dataset.job)))
  const explorer = box.querySelector('.dlg-prog-explorer')
  if (explorer) explorer.addEventListener('click', () => { if (window.open) window.open('/explorer.html') })
  const runs = box.querySelector('.dlg-prog-runs')
  if (runs) runs.addEventListener('click', openDlgInRunData)
  const close = box.querySelector('.dlg-prog-close')
  if (close) close.addEventListener('click', () => {
    resetDlgPane()
    setDialogNotice('')
  })
}

function renderDlgTerminal(job) {
  if (!job) return
  stopDlgTimer()
  RUN_STATE.dlgReconnecting = false
  RUN_STATE.dlgStatus = job.status || RUN_STATE.dlgStatus || 'failed'
  if (job.finishedAt) RUN_STATE.dlgFinishedAt = job.finishedAt
  else if (DLG_TERMINAL.includes(RUN_STATE.dlgStatus)) RUN_STATE.dlgFinishedAt = RUN_STATE.dlgFinishedAt || new Date().toISOString()
  if (job.error) RUN_STATE.dlgError = job.error
  if (RUN_STATE.dlgStatus === 'failed' && !RUN_STATE.dlgError && job.exitCode != null) {
    RUN_STATE.dlgError = `Exited with code ${job.exitCode}`
  }
  const badge = dlgBadgeEl()
  if (badge) badge.innerHTML = statusBadgeHtml(RUN_STATE.dlgStatus)
  renderDlgStatusLine()
  renderDlgHints(job.resultHints)
  renderDlgActions(job.id, job.resultHints)
  syncDlgConsoleView()
  const actions = dlgActionsEl()
  const first = actions && actions.querySelector('.mini-btn')
  if (first && first.focus) first.focus()
}

function attachDlgStream(job) {
  if (typeof window.EventSource === 'undefined' || RUN_STATE.dlgEs.has(job)) return
  const es = new window.EventSource(`/api/jobs/${job}/stream`)
  RUN_STATE.dlgEs.set(job, es)
  es.addEventListener('log', (e) => {
    try {
      const ev = JSON.parse(e.data || '{}')
      if (ev && ev.text) appendDlgLog({ stream: ev.stream, text: ev.text })
    } catch { /* ignore */ }
  })
  es.addEventListener('start', (e) => {
    if (RUN_STATE.dlgJobId !== job) return
    RUN_STATE.dlgStatus = 'running'
    RUN_STATE.dlgStartedAt = RUN_STATE.dlgStartedAt || new Date().toISOString()
    RUN_STATE.dlgLastOutputAt = new Date().toISOString()
    if (RUN_STATE.dlgStartedAt) startDlgTimer()
    const badge = dlgBadgeEl()
    if (badge) badge.innerHTML = statusBadgeHtml('running')
    renderDlgStatusLine()
    syncDlgConsoleView()
  })
  es.addEventListener('status', (e) => {
    let status = {}
    try { status = JSON.parse(e.data || '{}') } catch { return }
    handleDlgStatus(job, status)
  })
  es.addEventListener('cancel', () => handleDlgStatus(job, { status: 'canceled' }))
  es.onerror = () => {
    if (!RUN_STATE.dlgStatus || DLG_TERMINAL.includes(RUN_STATE.dlgStatus)) {
      detachDlgStream(job)
      return
    }
    RUN_STATE.dlgReconnecting = true
    renderDlgStatusLine()
  }
}

function openDialogProgress(job, toolId, args, opts = {}) {
  const p = dlgProg()
  if (!p) return
  p.classList.remove('cm-hidden')
  stopDlgTimer()
  for (const j of [...RUN_STATE.dlgEs.keys()]) detachDlgStream(j)
  RUN_STATE.dlgLogs = []
  RUN_STATE.dlgJobId = job.id
  RUN_STATE.dlgStatus = job.status || 'queued'
  RUN_STATE.dlgDuplicate = !!opts.duplicate
  RUN_STATE.dlgReconnecting = false
  RUN_STATE.dlgStartedAt = job.startedAt || null
  RUN_STATE.dlgFinishedAt = job.finishedAt || null
  RUN_STATE.dlgError = job.error || ''
  RUN_STATE.dlgLastOutputAt = null
  RUN_STATE.dlgLastJob = { id: job.id, toolId, args: args || (job.args || {}) }
  const pre = document.querySelector('#dlgRunProgress .dlg-console-pre')
  if (pre) pre.textContent = ''
  const badge = dlgBadgeEl()
  if (badge) badge.innerHTML = statusBadgeHtml(RUN_STATE.dlgStatus)
  renderDlgStatusLine()
  syncDlgConsoleView()
  if (DLG_TERMINAL.includes(RUN_STATE.dlgStatus)) {
    renderDlgTerminal(job)
    return
  }
  renderDlgActions(job.id)
  if (RUN_STATE.dlgStatus === 'running') startDlgTimer()
  attachDlgStream(job.id)
}

function handleDialogJobResult(res, toolId, args) {
  if (!res.job) return
  openDialogProgress(res.job, toolId, args, { duplicate: !!res.duplicate })
}

function openDlgInRunData() {
  activateSection('run')
  closeDialog()
}

async function rerunDlgJob() {
  const prev = RUN_STATE.dlgLastJob
  if (!prev || !prev.toolId) return
  const btn = document.querySelector('.dlg-prog-rerun')
  if (btn) btn.disabled = true
  resetDlgPane()
  try {
    const res = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ toolId: prev.toolId, args: prev.args }) })
    await refreshJobs()
    if (res.job && !DLG_TERMINAL.includes(res.job.status)) {
      handleDialogJobResult(res, prev.toolId, prev.args)
      setDialogNotice(`Re-running <strong>${esc(prev.toolId)}</strong> — progress below.`)
    } else if (res.job) {
      handleDialogJobResult(res, prev.toolId, prev.args)
      setDialogNotice(`Re-run <strong>${esc(prev.toolId)}</strong> did not start — see pane below.`, 'error')
    }
  } catch (e) {
    setDialogNotice(`Re-run failed: ${esc(e.message)}`, 'error')
  }
}

// ── log side panel (live log viewer, row + dialog "view log") ──

function panelEl() {
  return document.getElementById('logPanel')
}

function stopPanelTimer() {
  if (RUN_STATE.panelTimer) {
    clearInterval(RUN_STATE.panelTimer)
    RUN_STATE.panelTimer = null
  }
}

function detachPanelStream(job) {
  if (job == null) return
  const es = RUN_STATE.panelEs.get(job)
  if (es) {
    es.close()
    RUN_STATE.panelEs.delete(job)
  }
}

function panelDurationText() {
  if (!RUN_STATE.panelStartedAt) return ''
  return durationStr(RUN_STATE.panelStartedAt, RUN_STATE.panelFinishedAt || null)
}

function renderPanelHeader() {
  const p = panelEl()
  if (!p) return
  const s = RUN_STATE.panelStatus || ''
  let state = (STATUS_BADGE[s] && STATUS_BADGE[s][1]) || s || 'Opened'
  if (s === 'queued' && !RUN_STATE.panelFinishedAt) {
    state = RUN_STATE.panelQueuePos > 0
      ? `Queued — position ${RUN_STATE.panelQueuePos} of ${RUN_STATE.jobs.filter(j => j.status === 'queued').length}`
      : 'Queued — waiting for a free slot'
  } else if (s === 'failed') {
    const row = RUN_STATE.jobs.find(j => j.id === RUN_STATE.panelJobId)
    state += row && row.error ? ` · ${row.error}` : ''
  }
  const stateText = p.querySelector('.panel-state-text')
  if (stateText) stateText.textContent = state
  const elapsed = p.querySelector('.log-panel-elapsed')
  if (elapsed) elapsed.textContent = panelDurationText() ? ` — ${panelDurationText()}` : ''
  const reconnect = p.querySelector('.log-panel-reconnect')
  if (reconnect) {
    const isStale = s === 'running' && RUN_STATE.panelLastOutputAt != null
    if (RUN_STATE.panelReconnecting) reconnect.textContent = ' · reconnecting'
    else if (isStale && Date.now() - new Date(RUN_STATE.panelLastOutputAt).getTime() >= STALE_MS) {
      reconnect.textContent = toolStaleLine(RUN_STATE.panelLastOutputAt)
    } else reconnect.textContent = ''
  }
  const badge = document.getElementById('panelBadge')
  if (badge) badge.innerHTML = statusBadgeHtml(s)
  const raw = p.querySelector('.log-panel-raw')
  if (raw && RUN_STATE.panelJobId) raw.setAttribute('href', `/api/jobs/${encodeURIComponent(RUN_STATE.panelJobId)}/log`)
}

function startPanelTimer() {
  stopPanelTimer()
  RUN_STATE.panelTimer = setInterval(() => {
    if (!RUN_STATE.panelStatus || DLG_TERMINAL.includes(RUN_STATE.panelStatus)) {
      stopPanelTimer()
      return
    }
    renderPanelHeader()
  }, 1000)
  if (RUN_STATE.panelTimer && typeof RUN_STATE.panelTimer.unref === 'function') RUN_STATE.panelTimer.unref()
}

function panelConsoleHtml() {
  return RUN_STATE.panelLogs.map(l =>
    l.stream === 'stderr' ? `<span class="con-err">${esc(l.text)}</span>` : esc(l.text),
  ).join('\n')
}

function renderPanelConsole() {
  const p = panelEl()
  const pre = p && p.querySelector('.log-panel-pre')
  const wrap = p && p.querySelector('.log-panel-console')
  const waiting = document.getElementById('panelWaiting')
  const auto = p && p.querySelector('.log-panel-autoscroll')
  if (!pre) return
  const has = RUN_STATE.panelLogs.length > 0
  pre.innerHTML = panelConsoleHtml()
  if (!has && waiting) waiting.classList.remove('cm-hidden')
  if (has && waiting) waiting.classList.add('cm-hidden')
  if (wrap && auto && auto.checked && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    wrap.scrollTop = wrap.scrollHeight
  }
}

function appendPanelLog(ev) {
  RUN_STATE.panelReconnecting = false
  RUN_STATE.panelLastOutputAt = new Date().toISOString()
  RUN_STATE.panelLogs.push(ev)
  if (RUN_STATE.panelLogs.length > 1000) RUN_STATE.panelLogs.splice(0, RUN_STATE.panelLogs.length - 1000)
  renderPanelConsole()
}

function handlePanelStatus(job, status) {
  if (!status || !status.status || RUN_STATE.panelJobId !== job) return
  RUN_STATE.panelReconnecting = false
  RUN_STATE.panelStatus = status.status
  if (status.status === 'running' && !RUN_STATE.panelStartedAt) {
    RUN_STATE.panelStartedAt = new Date().toISOString()
    startPanelTimer()
  }
  if (!DLG_TERMINAL.includes(status.status)) RUN_STATE.panelLastOutputAt = new Date().toISOString()
  if (DLG_TERMINAL.includes(status.status)) {
    stopPanelTimer()
    detachPanelStream(job)
    RUN_STATE.panelFinishedAt = new Date().toISOString()
    renderPanelHeader()
    refreshJobs().catch(() => {})
    return
  }
  renderPanelHeader()
}

function attachPanelStream(job) {
  if (typeof window.EventSource === 'undefined' || RUN_STATE.panelEs.has(job)) return
  const p = panelEl()
  if (!p) return
  const es = new window.EventSource(`/api/jobs/${job}/stream`)
  RUN_STATE.panelEs.set(job, es)
  es.addEventListener('log', (e) => {
    try {
      const ev = JSON.parse(e.data || '{}')
      if (ev && ev.text) appendPanelLog({ stream: ev.stream, text: ev.text })
    } catch { /* ignore */ }
  })
  es.addEventListener('start', (e) => {
    if (RUN_STATE.panelJobId !== job) return
    RUN_STATE.panelStatus = 'running'
    RUN_STATE.panelStartedAt = RUN_STATE.panelStartedAt || new Date().toISOString()
    RUN_STATE.panelLastOutputAt = new Date().toISOString()
    startPanelTimer()
    renderPanelHeader()
  })
  es.addEventListener('status', (e) => {
    let status = {}
    try { status = JSON.parse(e.data || '{}') } catch { return }
    handlePanelStatus(job, status)
  })
  es.addEventListener('cancel', () => handlePanelStatus(job, { status: 'canceled' }))
  es.onerror = () => {
    if (!RUN_STATE.panelStatus || DLG_TERMINAL.includes(RUN_STATE.panelStatus)) {
      detachPanelStream(job)
      return
    }
    RUN_STATE.panelReconnecting = true
    renderPanelHeader()
  }
}

function closeLogPanel() {
  stopPanelTimer()
  if (RUN_STATE.panelJobId != null) detachPanelStream(RUN_STATE.panelJobId)
  RUN_STATE.panelJobId = null
  RUN_STATE.panelStatus = ''
  RUN_STATE.panelTitle = ''
  RUN_STATE.panelStartedAt = null
  RUN_STATE.panelFinishedAt = null
  RUN_STATE.panelReconnecting = false
  RUN_STATE.panelQueuePos = 0
  RUN_STATE.panelLogs = []
  RUN_STATE.panelLastOutputAt = null
  const p = panelEl()
  const scrim = document.getElementById('logPanelScrim')
  const pre = p && p.querySelector('.log-panel-pre')
  const titleEl = document.getElementById('panelTitle')
  if (p) {
    p.classList.add('cm-hidden')
    p.setAttribute('aria-hidden', 'true')
  }
  if (scrim) scrim.classList.add('cm-hidden')
  if (pre) pre.textContent = ''
  if (titleEl) titleEl.textContent = ''
  renderRunWidgets()
}

async function openLogPanel(jobId) {
  if (!jobId) return
  let rec = RUN_STATE.jobs.find(j => j.id === jobId)
  if (!rec) {
    try {
      const { job } = await api(`/api/jobs/${jobId}`)
      rec = job
    } catch { rec = null }
  }
  if (!rec) {
    setBanner(`Log unavailable: unknown job ${jobId}`)
    return
  }
  const p = panelEl()
  const scrim = document.getElementById('logPanelScrim')
  if (!p) return
  stopPanelTimer()
  if (RUN_STATE.panelJobId != null && RUN_STATE.panelJobId !== jobId) detachPanelStream(RUN_STATE.panelJobId)
  RUN_STATE.panelJobId = jobId
  RUN_STATE.panelStatus = rec.status || 'queued'
  RUN_STATE.panelStartedAt = rec.startedAt || null
  RUN_STATE.panelFinishedAt = rec.finishedAt || null
  RUN_STATE.panelReconnecting = false
  RUN_STATE.panelQueuePos = 0
  RUN_STATE.panelLogs = []
  RUN_STATE.panelLastOutputAt = null
  const tool = toolById(rec.toolId)
  RUN_STATE.panelTitle = tool ? tool.title : (rec.toolId || jobId.slice(0, 8))
  const titleEl = document.getElementById('panelTitle')
  if (titleEl) titleEl.textContent = RUN_STATE.panelTitle
  p.classList.remove('cm-hidden')
  p.setAttribute('aria-hidden', 'false')
  if (scrim) scrim.classList.remove('cm-hidden')
  if (RUN_STATE.panelStatus === 'queued') {
    const queued = RUN_STATE.jobs.filter(j => j.status === 'queued')
    const idx = queued.findIndex(j => j.id === jobId)
    if (idx !== -1) RUN_STATE.panelQueuePos = idx + 1
  }
  renderPanelHeader()
  renderPanelConsole()
  attachPanelStream(jobId)
  if (RUN_STATE.panelStatus === 'running') startPanelTimer()
  const closeBtn = document.getElementById('panelClose')
  if (closeBtn && closeBtn.focus) closeBtn.focus()
  const esc = (e) => {
    if (e.key === 'Escape') {
      closeLogPanel()
      document.removeEventListener('keydown', esc)
    }
  }
  document.addEventListener('keydown', esc)
  renderRunWidgets()
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
  const body = {
    old_model: model.model,
    new_model: rawModel,
    edits,
    create,
  }
  if (route !== undefined) body.provider_route = route
  const pending = await requestEditApply(model, body)
  if (pending.needConfirm) {
    openConfirmDelete(model, body, ((pending.error && pending.error.detail) || {}).impact || [])
  }
}

async function requestEditApply(model, body) {
  const createBtn = document.getElementById('dlgCreate')
  createBtn.disabled = true
  createBtn.textContent = 'Saving...'
  try {
    await api('/api/models', { method: 'PUT', body: JSON.stringify(body) })
    closeDialog()
    await loadModels()
    return { needConfirm: false }
  } catch (e) {
    if (e.detail && (e.detail.code === 'confirmation_required' || e.detail.code === 'active_jobs')) {
      return { needConfirm: true, error: e }
    }
    setError(e.message)
    createBtn.disabled = false
    createBtn.textContent = 'Save changes'
    return { needConfirm: false, error: e }
  }
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function restoreRemovalRows() {
  document.querySelectorAll('#dlgVariantRows tr.vc-to-remove').forEach(r => toggleRowRemoval(r))
}

function openConfirmDelete(model, body, impact) {
  const overlay = document.getElementById('confirmDeleteOverlay')
  const list = document.getElementById('confirmImpact')
  const btn = document.getElementById('confirmDeleteButton')
  const notice = document.getElementById('confirmNotice')
  const err = document.getElementById('confirmError')
  if (notice) { notice.classList.add('cm-hidden'); notice.innerHTML = '' }
  if (err) { err.classList.add('cm-hidden'); err.innerHTML = '' }

  const rowsHtml = impact.map(i => {
    const impactStr = (i.runFiles || i.logs || i.resultRows)
      ? `${plural(i.runFiles, 'run file')} · ${plural(i.logs, 'log')} · ${plural(i.resultRows, 'results row')}`
      : 'no run data — profile only'
    const blocked = i.activeJobs > 0
    return `<tr class="${blocked ? 'confirm-row-blocked' : ''}">
      <td><code>${esc(i.key)}</code></td>
      <td>${impactStr}${blocked ? ' <span class="confirm-blocked">⛔ cannot delete — job still running</span>' : ''}</td>
    </tr>`
  }).join('')
  list.innerHTML = `<table class="confirm-table"><tbody>${rowsHtml}</tbody></table>`

  const confirmable = impact.filter(i => i.activeJobs === 0)
  const totalRuns = confirmable.reduce((s, i) => s + (i.runFiles || 0), 0)
  const totalLogs = confirmable.reduce((s, i) => s + (i.logs || 0), 0)
  const totalRows = confirmable.reduce((s, i) => s + (i.resultRows || 0), 0)
  btn.dataset.impact = JSON.stringify(impact)
  btn.dataset.body = JSON.stringify(body)
  if (confirmable.length === 0) {
    btn.textContent = 'Nothing to delete (active jobs)'
    btn.disabled = true
  } else {
    const parts = [plural(confirmable.length, 'variant')]
    if (totalRuns) parts.push(plural(totalRuns, 'file'))
    if (totalLogs) parts.push(plural(totalLogs, 'log'))
    if (totalRows) parts.push(`${totalRows === 1 ? '1 results row' : `${totalRows} results rows`}`)
    btn.textContent = `Delete permanently (${parts.join(', ')})`
    btn.disabled = false
  }
  overlay.classList.remove('cm-hidden')
  document.getElementById('confirmDeleteCancel').focus()
}

function closeConfirmDelete() {
  document.getElementById('confirmDeleteOverlay').classList.add('cm-hidden')
  const createBtn = document.getElementById('dlgCreate')
  createBtn.disabled = false
  createBtn.textContent = 'Save changes'
  if (document.getElementById('confirmDeleteCancel').contains(document.activeElement) ||
      document.getElementById('confirmDeleteButton').contains(document.activeElement) ||
      document.getElementById('confirmDeleteClose').contains(document.activeElement)) {
    createBtn.focus()
  }
}

function cancelConfirmDelete() {
  restoreRemovalRows()
  closeConfirmDelete()
}

async function performConfirmedDelete(model, body) {
  const overlay = document.getElementById('confirmDeleteOverlay')
  const btn = document.getElementById('confirmDeleteButton')
  btn.disabled = true
  btn.textContent = 'Deleting...'
  const confirmedBody = { ...body, confirm: true }
  try {
    const res = await api('/api/models', { method: 'PUT', body: JSON.stringify(confirmedBody) })
    overlay.classList.add('cm-hidden')
    closeDialog()
    await loadModels()
    const plan = res.plan || {}
    const parts = []
    if (plan.removed && plan.removed.length) parts.push(plural(plan.removed.length, 'variant'))
    if (plan.removedRuns) parts.push(plural(plan.removedRuns, 'file'))
    if (plan.removedLogs) parts.push(plural(plan.removedLogs, 'log'))
    if (plan.removedResultRows) parts.push(plan.removedResultRows === 1 ? '1 results row' : `${plan.removedResultRows} results rows`)
    if (plan.skippedActive && plan.skippedActive.length) {
      setBanner(`Deleted ${parts.join(', ') || 'selected variants'}. Kept ${plan.skippedActive.length} variant${plan.skippedActive.length === 1 ? '' : 's'} with an active job.`)
    } else {
      setBanner(`Deleted ${parts.join(', ') || 'selected variants'}.`)
    }
  } catch (e) {
    overlay.classList.add('cm-hidden')
    restoreRemovalRows()
    document.getElementById('dlgCreate').disabled = false
    document.getElementById('dlgCreate').textContent = 'Save changes'
    setError(e.message)
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
  if (action === 'runs') {
    RUNS_STATE.preset = { model }
    activateSection('runs')
  } else if (action === 'edit') {
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
    bootstrapRunWidget()
  }
  document.querySelectorAll('[data-settings-toggle]').forEach(btn => {
    btn.addEventListener('click', open)
  })
}

function runJobsActive() {
  return (RUN_STATE.jobs || []).some(j => j.status === 'running' || j.status === 'queued')
}

function settingsOverlayHidden() {
  const o = document.getElementById('settingsOverlay')
  return !o || o.classList.contains('cm-hidden')
}

function hideOverlay(overlay) {
  overlay.classList.add('cm-hidden')
  // Keep monitoring while runs are in flight: closing the drawer must not stop
  // the live stream, so a job that finishes behind the drawer still refreshes
  // the scatter explorer, model list and variant list.
  if (!runJobsActive()) stopRunPolling()
}

function activateSection(section) {
  document.querySelectorAll('.settings-nav-item').forEach(n => n.classList.toggle('active', n.dataset.section === section))
  const cap = section[0].toUpperCase() + section.slice(1)
  document.querySelectorAll('.settings-section').forEach(s => {
    s.classList.toggle('active', s.id === `settings${cap}Section`)
  })
  if (section === 'models') loadModels()
  else if (section === 'runs') { bindRunsManager(); loadRunsManager().catch(() => {}) }
  else if (section === 'run') initRunData()
  else if (section === 'prompts') loadPrompts()
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
  closePromptMenus()
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
  const dlgConClear = document.getElementById('dlgConClear')
  if (dlgConClear) dlgConClear.addEventListener('click', clearDlgConsole)
  const panelClose = document.getElementById('panelClose')
  if (panelClose) panelClose.addEventListener('click', closeLogPanel)
  const panelClear = document.querySelector('.log-panel-clear')
  if (panelClear) panelClear.addEventListener('click', () => {
    RUN_STATE.panelLogs = []
    renderPanelConsole()
  })
  const panelScrim = document.getElementById('logPanelScrim')
  if (panelScrim) panelScrim.addEventListener('click', closeLogPanel)
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
    loadModels()
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
    const pvO = document.getElementById('promptVersionOverlay')
    if (pvO && !pvO.classList.contains('cm-hidden')) { closePromptVersionDialog(); return }
    const confirmO = document.getElementById('confirmDeleteOverlay')
    if (confirmO && !confirmO.classList.contains('cm-hidden')) { cancelConfirmDelete(); return }
    if (!dialogOverlay.classList.contains('cm-hidden')) closeDialog()
    else if (!overlay.classList.contains('cm-hidden')) hideOverlay(overlay)
  })

  const confirmOverlay = document.getElementById('confirmDeleteOverlay')
  document.getElementById('confirmDeleteCancel').addEventListener('click', cancelConfirmDelete)
  document.getElementById('confirmDeleteClose').addEventListener('click', cancelConfirmDelete)
  document.getElementById('confirmDeleteButton').addEventListener('click', async () => {
    let editingModel = null
    try { editingModel = JSON.parse(dialogOverlay.dataset.editingModel || 'null') } catch { /* none */ }
    const stored = document.getElementById('confirmDeleteButton').dataset.body
    if (!stored) return
    await performConfirmedDelete(editingModel, JSON.parse(stored))
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
  dlgEs: new Map(),
  dlgTimer: null,
  dlgLogs: [],
  dlgJobId: null,
  dlgStatus: '',
  dlgDuplicate: false,
  dlgReconnecting: false,
  dlgStartedAt: null,
  dlgFinishedAt: null,
  dlgError: '',
  dlgLastJob: null,
  dlgLastOutputAt: null,
  panelEs: new Map(),
  panelTimer: null,
  panelLogs: [],
  panelJobId: null,
  panelStatus: '',
  panelTitle: '',
  panelStartedAt: null,
  panelFinishedAt: null,
  panelReconnecting: false,
  panelQueuePos: 0,
  panelLastOutputAt: null,
  widgetEs: new Map(),
  widgetLogs: new Map(),
  widgetExpanded: new Set(),
  widgetTracked: new Set(),
  widgetPinned: new Set(),
  widgetDismissed: new Set(),
  widgetHandled: new Set(),
  widgetHideTimers: new Map(),
  widgetRenderKey: '',
  widgetSuccessTtlMs: 5000,
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

function textFieldHtml(a, idPrefix, extraClass = '') {
  const cls = extraClass ? ` class="${extraClass}"` : ''
  const base = `id="${idPrefix}-${a.name}" data-arg="${a.name}" type="text" spellcheck="false" autocomplete="off" placeholder="${esc(a.placeholder || '')}" value="${esc(a.default != null ? String(a.default) : '')}"`
  if (!a.bench) return `<input ${base}${cls} />`
  return `<div class="bench-combo">
    <input ${base} class="bench-combo-input" role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-autocomplete="none" />
    <button type="button" class="bench-combo-btn" data-bench-toggle aria-label="Show all benchmarks" aria-expanded="false" tabindex="-1">&#9662;</button>
    <div class="bench-combo-menu cm-hidden" data-bench-menu role="listbox" aria-label="Benchmarks"></div>
  </div>`
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
          ${textFieldHtml(a, idPrefix, 'judge-model-fld')}
          ${hint}`)
      }
      return wrap(`${label}${textFieldHtml(a, idPrefix)}${hint}`)
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
  const submitBtn = card.querySelector('.tool-run-submit')
  if (submitBtn) submitBtn.disabled = true
  try {
    const res = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) })
    // refreshJobs reconciles the floating widget and attaches its own live
    // stream for any newly running job — no per-row expansion needed.
    await refreshJobs()
    const resJob = res.job
    if (inDialog && resJob) {
      handleDialogJobResult(res, tool.id, values)
      if (DLG_TERMINAL.includes(resJob.status)) {
        setDialogNotice(`<strong>${esc(tool.title || tool.id)}</strong> failed to start: ${esc(resJob.error || 'unknown error')}`, 'error')
      } else if (res.duplicate) {
        setDialogNotice(`<strong>${esc(tool.id)}</strong> is already queued with these exact options — progress below.`)
      } else {
        setDialogNotice(`Launched <strong>${esc(tool.id)}</strong> — progress below. <strong>Scripts</strong> keeps the full history.`)
      }
    }
  } catch (e) {
    showCardError(card, e.message)
    if (inDialog && /already running/.test(e.message)) {
      setDialogNotice(`<strong>${esc(tool.title || tool.id)}</strong> is already running with identical options — the job is streaming in <strong>Scripts</strong>.`, 'error')
    }
  } finally {
    updateToolGuards()
  }
}

function benchComboOptions() {
  const benches = Array.isArray(RUN_STATE.benches) ? RUN_STATE.benches.slice() : []
  return benches.length ? benches : ['chapter_fast']
}

function closeBenchCombo(combo) {
  const menu = combo.querySelector('[data-bench-menu]')
  const input = combo.querySelector('.bench-combo-input')
  const btn = combo.querySelector('[data-bench-toggle]')
  if (menu) menu.classList.add('cm-hidden')
  if (input) input.setAttribute('aria-expanded', 'false')
  if (btn) btn.setAttribute('aria-expanded', 'false')
}

function closeAllBenchCombos(except) {
  document.querySelectorAll('.bench-combo').forEach(c => { if (c !== except) closeBenchCombo(c) })
}

function openBenchCombo(combo) {
  const menu = combo.querySelector('[data-bench-menu]')
  const input = combo.querySelector('.bench-combo-input')
  const btn = combo.querySelector('[data-bench-toggle]')
  if (!menu || !input) return
  closeAllBenchCombos(combo)
  const current = input.value.trim()
  menu.innerHTML = benchComboOptions().map(b => {
    const sel = b === current ? ' is-selected' : ''
    return `<button type="button" role="option" class="bench-combo-item${sel}" data-bench-option="${esc(b)}" aria-selected="${b === current}">${esc(b)}</button>`
  }).join('')
  menu.classList.remove('cm-hidden')
  input.setAttribute('aria-expanded', 'true')
  if (btn) btn.setAttribute('aria-expanded', 'true')
}

function benchMoveActive(combo, dir) {
  const items = Array.from(combo.querySelectorAll('.bench-combo-item'))
  if (!items.length) return
  const idx = items.findIndex(i => i.classList.contains('is-active'))
  let next = idx === -1 ? (dir > 0 ? 0 : items.length - 1) : idx + dir
  if (next < 0) next = items.length - 1
  if (next >= items.length) next = 0
  items.forEach(i => i.classList.remove('is-active'))
  items[next].classList.add('is-active')
  items[next].scrollIntoView({ block: 'nearest' })
}

function pickBenchOption(combo, value) {
  const input = combo.querySelector('.bench-combo-input')
  if (input) input.value = value
  closeBenchCombo(combo)
}

let BENCH_COMBO_DOC_WIRED = false

function wireBenchCombo(combo) {
  const input = combo.querySelector('.bench-combo-input')
  const btn = combo.querySelector('[data-bench-toggle]')
  const menu = combo.querySelector('[data-bench-menu]')
  if (!input || !menu) return
  if (!BENCH_COMBO_DOC_WIRED) {
    BENCH_COMBO_DOC_WIRED = true
    document.addEventListener('mousedown', (e) => { if (!e.target.closest('.bench-combo')) closeAllBenchCombos() })
  }
  const toggle = () => { menu.classList.contains('cm-hidden') ? openBenchCombo(combo) : closeBenchCombo(combo) }
  if (btn) btn.addEventListener('mousedown', (e) => { e.preventDefault(); toggle() })
  input.addEventListener('mousedown', () => { if (menu.classList.contains('cm-hidden')) openBenchCombo(combo) })
  input.addEventListener('click', () => { if (menu.classList.contains('cm-hidden')) openBenchCombo(combo) })
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (menu.classList.contains('cm-hidden')) openBenchCombo(combo)
      else benchMoveActive(combo, 1)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (menu.classList.contains('cm-hidden')) openBenchCombo(combo)
      else benchMoveActive(combo, -1)
    } else if (e.key === 'Enter') {
      const act = combo.querySelector('.bench-combo-item.is-active')
      if (act) { e.preventDefault(); pickBenchOption(combo, act.dataset.benchOption) }
    } else if (e.key === 'Escape') {
      closeBenchCombo(combo)
    }
  })
  menu.addEventListener('mousedown', (e) => e.preventDefault())
  menu.addEventListener('click', (e) => {
    const item = e.target.closest('[data-bench-option]')
    if (!item) return
    pickBenchOption(combo, item.dataset.benchOption)
    input.focus()
  })
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
  card.querySelectorAll('.bench-combo').forEach(wireBenchCombo)
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
  actions.push(`<button type="button" class="mini-btn job-log-link" data-job="${job.id}">view log</button>`)
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
  // Active/queued jobs float in the bottom-right widget; the section is the
  // finished-run history so the list stays readable regardless of what's live.
  const finished = RUN_STATE.jobs.filter(j => WIDGET_TERMINAL.includes(j.status))
  count.textContent = RUN_STATE.jobs.length ? `${finished.length} finished` : 'No runs'

  if (!finished.length) {
    list.innerHTML = RUN_STATE.jobs.length
      ? '<div class="placeholder-text">No finished runs yet — active runs show in the corner widget.</div>'
      : '<div class="placeholder-text">No runs yet. Pick a tool on the left to run your first script.</div>'
    updateToolGuards()
    return
  }
  list.innerHTML = finished.map(rollupJobRow).join('')

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

  list.querySelectorAll('.job-log-link').forEach(btn => {
    btn.addEventListener('click', () => openLogPanel(btn.dataset.job))
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
  if (!settingsDocCurrent()) return
  if (!document.getElementById('jobsList')) {
    stopRunPolling()
    return
  }
  try {
    const res = await api('/api/jobs?limit=60')
    RUN_STATE.jobs = res.jobs || []
    clearOffline()
    notifyAllHints()
    reconcileRunWidgets()
    syncWidgetStreams()
    renderJobs()
    renderRunWidgets()
    // Once the last run finishes behind a closed drawer, nothing is left to
    // monitor — shut the poll down instead of polling forever.
    if (settingsOverlayHidden() && !runJobsActive()) stopRunPolling()
    else ensureRunPolling()
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
  if (hints.resultsTsvUpdated || hints.runCompleted) {
    RUN_STATE.hintedJobs.add(job)
    window.dispatchEvent(new window.Event('dashboard:results-refreshed'))
  }
  if (hints.specPyChanged || hints.modelsChanged) {
    RUN_STATE.hintedJobs.add(job)
    window.dispatchEvent(new window.Event('dashboard:candidates-refreshed'))
  }
}

// ── floating run widget (bottom-right live cards) ──────────────
// Active/queued jobs live here instead of in the Scripts section, so a run
// stayed in view no matter which settings section (or page) is open. Finished
// jobs are history-only in the section; a card auto-hides on success and stays
// pinned on failure until dismissed.

const WIDGET_TAIL_LINES = 8
const WIDGET_TERMINAL = ['succeeded', 'failed', 'canceled', 'interrupted']
// The widget's open/dismissed state is persisted so every dashboard page and
// browser tab agrees: open a card on one tab and it opens on the others too.
const WIDGET_EXPANDED_KEY = 'mm.runWidget.expanded'
const WIDGET_DISMISSED_KEY = 'mm.runWidget.dismissed'
const WIDGET_STATE_CAP = 100

function readWidgetIdSet(key) {
  const raw = storageGet(key)
  if (!raw) return null
  try {
    const arr = JSON.parse(raw)
    if (Array.isArray(arr)) return new Set(arr.filter(x => typeof x === 'string'))
  } catch { /* ignore malformed */ }
  return null
}

function persistWidgetSets() {
  storageSet(WIDGET_EXPANDED_KEY, JSON.stringify([...RUN_STATE.widgetExpanded].slice(-WIDGET_STATE_CAP)))
  storageSet(WIDGET_DISMISSED_KEY, JSON.stringify([...RUN_STATE.widgetDismissed].slice(-WIDGET_STATE_CAP)))
}

function hydrateWidgetState() {
  const expanded = readWidgetIdSet(WIDGET_EXPANDED_KEY)
  if (expanded) RUN_STATE.widgetExpanded = expanded
  const dismissed = readWidgetIdSet(WIDGET_DISMISSED_KEY)
  if (dismissed) RUN_STATE.widgetDismissed = dismissed
}

function onWidgetStorage(e) {
  if (!settingsDocCurrent()) return
  if (e.key !== WIDGET_EXPANDED_KEY && e.key !== WIDGET_DISMISSED_KEY) return
  const next = readWidgetIdSet(e.key)
  if (!next) return
  if (e.key === WIDGET_EXPANDED_KEY) RUN_STATE.widgetExpanded = next
  else RUN_STATE.widgetDismissed = next
  renderRunWidgets(true)
}

function runToastStackEl() {
  return document.getElementById('runToastStack')
}

function logPanelOpen() {
  const p = document.getElementById('logPanel')
  return !!p && !p.classList.contains('cm-hidden')
}

function ensureRunPolling() {
  if (RUN_STATE.pollTimer != null) return
  const timer = setInterval(() => {
    if (!settingsDocCurrent()) { clearInterval(timer); RUN_STATE.pollTimer = null; return }
    const sec = document.getElementById('settingsRunSection')
    const sectionOpen = !!sec && sec.classList.contains('active') && !settingsOverlayHidden()
    if (runJobsActive() || sectionOpen) refreshJobs().catch(() => {})
  }, 2000)
  if (timer && typeof timer.unref === 'function') timer.unref()
  RUN_STATE.pollTimer = timer
}

function widgetVisibleJobs() {
  return RUN_STATE.jobs.filter(j =>
    RUN_STATE.widgetTracked.has(j.id) &&
    !RUN_STATE.widgetDismissed.has(j.id) &&
    ((j.status === 'running' || j.status === 'queued') || RUN_STATE.widgetPinned.has(j.id)),
  )
}

function reconcileRunWidgets() {
  for (const j of RUN_STATE.jobs) {
    if (j.status === 'running' || j.status === 'queued') {
      RUN_STATE.widgetTracked.add(j.id)
      RUN_STATE.widgetPinned.delete(j.id)
      RUN_STATE.widgetHandled.delete(j.id)
    } else if (RUN_STATE.widgetTracked.has(j.id) && !RUN_STATE.widgetHandled.has(j.id)) {
      RUN_STATE.widgetHandled.add(j.id)
      if (j.status === 'succeeded') scheduleWidgetAutoHide(j.id)
      else RUN_STATE.widgetPinned.add(j.id)
    }
  }
}

function scheduleWidgetAutoHide(id) {
  if (RUN_STATE.widgetHideTimers.has(id)) return
  const ttl = RUN_STATE.widgetSuccessTtlMs
  const timer = setTimeout(() => {
    if (!settingsDocCurrent()) return
    RUN_STATE.widgetHideTimers.delete(id)
    RUN_STATE.widgetDismissed.add(id)
    detachWidgetStream(id)
    RUN_STATE.widgetLogs.delete(id)
    RUN_STATE.widgetExpanded.delete(id)
    persistWidgetSets()
    renderRunWidgets(true)
  }, ttl)
  if (timer && typeof timer.unref === 'function') timer.unref()
  RUN_STATE.widgetHideTimers.set(id, timer)
}

function cancelWidgetAutoHide(id) {
  const t = RUN_STATE.widgetHideTimers.get(id)
  if (t) { clearTimeout(t); RUN_STATE.widgetHideTimers.delete(id) }
}

function widgetConsoleHtml(id) {
  const logs = RUN_STATE.widgetLogs.get(id) || []
  const expanded = RUN_STATE.widgetExpanded.has(id)
  const shown = expanded ? logs : logs.slice(-WIDGET_TAIL_LINES)
  return shown.map(l =>
    l.stream === 'stderr' ? `<span class="con-err">${esc(l.text)}</span>` : esc(l.text),
  ).join('\n')
}

function runToastHtml(job) {
  const tool = toolById(job.toolId)
  const title = tool ? tool.title : job.toolId
  const expanded = RUN_STATE.widgetExpanded.has(job.id)
  const pinned = RUN_STATE.widgetPinned.has(job.id)
  const active = job.status === 'running' || job.status === 'queued'
  const hasLogs = (RUN_STATE.widgetLogs.get(job.id) || []).length > 0
  const actions = []
  if (active) {
    actions.push(`<button type="button" class="mini-btn run-toast-cancel" data-job="${job.id}" aria-pressed="false">Cancel</button>`)
  } else {
    actions.push(`<button type="button" class="mini-btn run-toast-rerun" data-job="${job.id}">Re-run</button>`)
  }
  actions.push(`<button type="button" class="mini-btn run-toast-log" data-job="${job.id}">view log</button>`)
  return `<article class="run-toast ${expanded ? 'is-expanded' : ''} ${pinned ? 'is-pinned' : ''}" data-job="${job.id}">
    <header class="run-toast-head">
      <div class="run-toast-main">${statusBadgeHtml(job.status)}<span class="run-toast-title">${esc(title)}</span></div>
      <span class="run-toast-elapsed">${durationStr(job.createdAt, job.finishedAt)}</span>
      <button type="button" class="run-toast-expand mini-btn" data-job="${job.id}" aria-expanded="${expanded ? 'true' : 'false'}">${expanded ? 'Collapse' : 'Expand'}</button>
      <button type="button" class="run-toast-dismiss" data-job="${job.id}" aria-label="Dismiss run">\u00D7</button>
    </header>
    <div class="run-toast-console console-wrap"><pre class="run-toast-pre" role="log">${widgetConsoleHtml(job.id)}</pre></div>
    <div class="run-toast-waiting ${hasLogs ? 'cm-hidden' : ''}">Waiting for output&hellip;</div>
    <div class="run-toast-actions">${actions.join('')}</div>
  </article>`
}

function renderRunWidgetHeaders() {
  for (const card of document.querySelectorAll('.run-toast')) {
    const job = RUN_STATE.jobs.find(j => j.id === card.dataset.job)
    if (!job) continue
    const el = card.querySelector('.run-toast-elapsed')
    if (el) el.textContent = durationStr(job.createdAt, job.finishedAt)
  }
}

function wireRunToasts(stack) {
  stack.querySelectorAll('.run-toast-expand').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.job
      if (RUN_STATE.widgetExpanded.has(id)) RUN_STATE.widgetExpanded.delete(id)
      else RUN_STATE.widgetExpanded.add(id)
      persistWidgetSets()
      renderRunWidgets(true)
    })
  })
  stack.querySelectorAll('.run-toast-dismiss').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.job
      cancelWidgetAutoHide(id)
      RUN_STATE.widgetDismissed.add(id)
      RUN_STATE.widgetPinned.delete(id)
      detachWidgetStream(id)
      RUN_STATE.widgetLogs.delete(id)
      RUN_STATE.widgetExpanded.delete(id)
      persistWidgetSets()
      renderRunWidgets(true)
    })
  })
  stack.querySelectorAll('.run-toast-cancel').forEach(btn => {
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
  stack.querySelectorAll('.run-toast-rerun').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.job
      try {
        const { job } = await api(`/api/jobs/${id}`)
        const tool = toolById(job.toolId)
        if (tool && tool.destructive) return
        await api('/api/jobs', { method: 'POST', body: JSON.stringify({ toolId: job.toolId, args: job.args || {} }) })
        await refreshJobs()
      } catch (e) {
        setBanner(`Re-run failed: ${esc(e.message)}`)
      }
    })
  })
  stack.querySelectorAll('.run-toast-log').forEach(btn => {
    btn.addEventListener('click', () => { openLogPanel(btn.dataset.job).catch(() => {}) })
  })
}

function renderRunWidgets(force = false) {
  const stack = runToastStackEl()
  if (!stack) return
  const jobs = widgetVisibleJobs()
  const key = jobs
    .map(j => `${j.id}:${j.status}:${RUN_STATE.widgetExpanded.has(j.id) ? 'x' : 'c'}`)
    .join('|')
  if (force || key !== RUN_STATE.widgetRenderKey) {
    RUN_STATE.widgetRenderKey = key
    stack.innerHTML = jobs.map(runToastHtml).join('')
    wireRunToasts(stack)
  } else {
    renderRunWidgetHeaders()
  }
  stack.classList.toggle('cm-hidden', jobs.length === 0 || logPanelOpen())
}

function appendWidgetLog(id, ev) {
  const logs = RUN_STATE.widgetLogs.get(id) || []
  logs.push(ev)
  if (logs.length > 2000) logs.splice(0, logs.length - 2000)
  RUN_STATE.widgetLogs.set(id, logs)
  const card = document.querySelector(`.run-toast[data-job="${id}"]`)
  if (!card) return
  const pre = card.querySelector('.run-toast-pre')
  if (pre) pre.innerHTML = widgetConsoleHtml(id)
  const waiting = card.querySelector('.run-toast-waiting')
  if (waiting) waiting.classList.toggle('cm-hidden', logs.length > 0)
  const wrap = card.querySelector('.run-toast-console')
  if (wrap && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    wrap.scrollTop = wrap.scrollHeight
  }
}

function attachWidgetStream(job) {
  if (typeof window.EventSource === 'undefined' || RUN_STATE.widgetEs.has(job)) return
  const rec = RUN_STATE.jobs.find(j => j.id === job)
  if (!rec || rec.status !== 'running') return
  const es = new window.EventSource(`/api/jobs/${job}/stream`)
  RUN_STATE.widgetEs.set(job, es)
  es.addEventListener('log', (e) => {
    try { appendWidgetLog(job, JSON.parse(e.data)) } catch { /* ignore */ }
  })
  es.addEventListener('status', (e) => {
    let status = {}
    try { status = JSON.parse(e.data || '{}') } catch { return }
    notifyStatusHints(job, status)
    detachWidgetStream(job)
    refreshJobs().catch(() => {})
  })
  es.addEventListener('cancel', () => {
    detachWidgetStream(job)
    refreshJobs().catch(() => {})
  })
  es.onerror = () => {
    const rec2 = RUN_STATE.jobs.find(j => j.id === job)
    if (rec2 && rec2.status !== 'running' && rec2.status !== 'queued') detachWidgetStream(job)
  }
}

function detachWidgetStream(job) {
  const es = RUN_STATE.widgetEs.get(job)
  if (es) {
    es.close()
    RUN_STATE.widgetEs.delete(job)
  }
}

function syncWidgetStreams() {
  const want = new Set(RUN_STATE.jobs
    .filter(j => j.status === 'running' && !RUN_STATE.widgetDismissed.has(j.id))
    .map(j => j.id))
  for (const id of [...RUN_STATE.widgetEs.keys()]) {
    if (!want.has(id)) detachWidgetStream(id)
  }
  for (const id of want) attachWidgetStream(id)
}

async function bootstrapRunWidget() {
  try { await refreshJobs() } catch { /* offline banner already set */ }
  if (!settingsDocCurrent()) return
  if (runJobsActive()) {
    ensureRunPolling()
    initRunData().catch(() => {})
  }
}

// Called by every dashboard entrypoint (scatter + run explorer) so the floating
// widget is live on each page/tab, and its open/dismissed state is shared
// between tabs via localStorage + the `storage` event.
function initRunWidget() {
  hydrateWidgetState()
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('storage', onWidgetStorage)
  }
  return bootstrapRunWidget()
}

// Loads the script registry + bench/env/jobs and wires the run tab.
// `activeStarted` is only latched once registry loads: a failed initRunData
// (e.g. the dev server predating /api/registry) retries on the next trigger
// instead of leaving the run-data feature dead until a full reload.
async function initRunData() {
  if (!settingsDocCurrent()) return
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
  ensureRunPolling()
}

// ── Prompts section (prompt-version registry) ──────────────────
const PROMPTS_STATE = {
  data: null,
  options: null,
  pv: null,
  pvPreviewTimer: null,
  pvAbort: null,
  filter: 'all',
  slotScope: 'general',
}

function setPromptsBanner(msg, kind = '') {
  const el = document.getElementById('promptsBanner')
  if (!el) return
  if (!msg) { el.classList.add('cm-hidden'); el.textContent = ''; return }
  el.classList.remove('cm-hidden')
  el.textContent = msg
  el.dataset.kind = kind
}

function _promptVersionOptions(versions, stage) {
  return versions
    .filter(v => v.stage === stage)
    .map(v => `<option value="${esc(v.id)}">${esc(v.id)} — ${esc(v.label || '')}</option>`)
    .join('')
}

function _driftBadge(status) {
  if (!status) return '<span class="drift-badge" role="status">Never reported</span>'
  if (status.never_reported) return '<span class="drift-badge" role="status">Never reported</span>'
  if (status.in_sync) return '<span class="drift-badge drift-in-sync" role="status">&#10003; In sync</span>'
  const when = status.reported_at ? ` (${esc(String(status.reported_at).slice(0, 16))})` : ''
  return `<span class="drift-badge drift-stale" role="status">&#9888; Not deployed — CF reports ${esc(status.reported_id || '?')}${when}</span>`
}

function _stageSlots(data, stage) {
  const s = (data.slots || {})[stage] || {}
  if (s.general || s.models) {
    return {
      general: s.general || { local: null, production: null },
      models: s.models || {},
    }
  }
  return {
    general: { local: s.local || null, production: s.production || null },
    models: {},
  }
}

function _scopeOf(v) {
  return (v && v.scope) || { kind: 'general' }
}

function _scopeBadge(v, modelSet) {
  const scope = _scopeOf(v)
  const profiles = (scope.profiles || [])
  if (scope.kind !== 'model') return '<span class="scope-badge is-general" role="status">General</span>'
  const known = modelSet ? modelSet.has(scope.model) : true
  const cls = known ? 'scope-badge' : 'scope-badge is-warn'
  const title = known ? '' : ' title="model not registered"'
  const suffix = profiles.length ? ` &middot; ${profiles.map(esc).join(', ')}` : ''
  return `<span class="${cls}" role="status"${title}>${esc(scope.model)}${suffix}</span>`
}

function _modelSet() {
  const models = (PROMPTS_STATE.data || {}).models || (PROMPTS_STATE.options || {}).models || []
  return new Set(models)
}

const PROMPT_MENU_ITEMS = [
  ['preview', 'Preview'],
  ['edit', 'Edit'],
  ['set-local', 'Set local'],
  ['set-production', 'Set production'],
  ['clone', 'Clone'],
  ['smoke', 'Smoke test'],
]

function _promptRowMenu(v, assigned, setModel) {
  const locked = assigned.length > 0
  const items = PROMPT_MENU_ITEMS.map(([action, label]) => {
    const aria = action === 'set-local' || action === 'set-production' || action === 'clone'
      ? ` aria-label="${esc(label)} ${esc(v.id)}"` : ''
    return `<button type="button" class="vc-opt-item" data-action="${action}" data-id="${esc(v.id)}" data-model="${esc(setModel)}" role="menuitem"${aria}>${label}</button>`
  }).join('')
  return `<button type="button" class="vc-opt-btn" data-id="${esc(v.id)}" aria-haspopup="menu" aria-expanded="false" aria-label="Options for ${esc(v.id)}" title="Version options">\u22EF</button>
    <div class="vc-opt-menu cm-hidden" role="menu">
      ${items}
      <button type="button" class="vc-opt-item vc-opt-remove" data-action="delete" data-id="${esc(v.id)}" role="menuitem"${locked ? ' disabled title="Assigned to a slot"' : ''}>Delete</button>
    </div>`
}

function closePromptMenus() {
  document.querySelectorAll('#promptsPanel .vc-opt-btn').forEach(b => b.setAttribute('aria-expanded', 'false'))
  document.querySelectorAll('#promptsPanel .vc-opt-menu').forEach(m => m.classList.add('cm-hidden'))
}

function renderPrompts(data) {
  const panel = document.getElementById('promptsPanel')
  if (!panel) return
  const versions = data.versions || []
  const models = data.models || (PROMPTS_STATE.options || {}).models || []
  const modelSet = new Set(models)
  PROMPTS_STATE.data = data
  const activeScope = PROMPTS_STATE.slotScope || 'general'

  const slotRows = ['chapter', 'composer'].map(stage => {
    const stageSlots = _stageSlots(data, stage)
    const scopeSlots = activeScope === 'general'
      ? stageSlots.general
      : (stageSlots.models[activeScope] || { local: null, production: null })
    const status = (data.production_status || []).find(
      r => r.stage === stage && (r.model || '') === (activeScope === 'general' ? '' : activeScope)
    )
    const scopedVersions = versions.filter(v => {
      const scope = _scopeOf(v)
      return activeScope === 'general' ? scope.kind === 'general' : (scope.kind === 'general' || scope.model === activeScope)
    })
    const noneLabel = activeScope === 'general' ? '(use baseline)' : '(inherit general)'
    return `
      <tr>
        <td>${esc(stage)}</td>
        <td><label class="cm-visually-hidden" for="promptLocal_${esc(stage)}">local</label>
          <select id="promptLocal_${esc(stage)}" data-stage="${esc(stage)}" data-slot="local">
            <option value="">${noneLabel}</option>${_promptVersionOptions(scopedVersions, stage)}
          </select></td>
        <td><label class="cm-visually-hidden" for="promptProd_${esc(stage)}">production</label>
          <select id="promptProd_${esc(stage)}" data-stage="${esc(stage)}" data-slot="production">
            <option value="">${noneLabel}</option>${_promptVersionOptions(scopedVersions, stage)}
          </select></td>
        <td><button class="mini-btn" data-action="save-slot" data-stage="${esc(stage)}" data-model="${esc(activeScope === 'general' ? '' : activeScope)}" disabled>Save</button></td>
        <td>${_driftBadge(status)}</td>
      </tr>`
  }).join('')

  const filter = PROMPTS_STATE.filter
  const filtered = versions.filter(v => {
    const scope = _scopeOf(v)
    if (filter === 'all') return true
    if (filter === 'general') return scope.kind === 'general'
    return scope.model === filter
  })

  const versionRows = filtered.map(v => {
    const assigned = []
    for (const stage of Object.keys(data.slots || {})) {
      const s = _stageSlots(data, stage)
      for (const slot of ['local', 'production']) {
        if (s.general[slot] === v.id) assigned.push(`${stage}.general.${slot}`)
      }
      for (const [model, slots] of Object.entries(s.models)) {
        for (const slot of ['local', 'production']) {
          if ((slots || {})[slot] === v.id) assigned.push(`${stage}.${model}.${slot}`)
        }
      }
    }
    const lp = (v.components && v.components.length_policy) || '–'
    const scope = _scopeOf(v)
    const setModel = scope.kind === 'model' ? scope.model : ''
    return `
      <tr>
        <td>${esc(v.id)}</td>
        <td>${esc(v.label || '')}</td>
        <td>${esc(v.stage)}</td>
        <td>${_scopeBadge(v, modelSet)}</td>
        <td>${esc(lp)}</td>
        <td><span title="${esc(v.system_prompt_fingerprint || '')}">${esc((v.system_prompt_fingerprint || '').slice(0, 8))}</span></td>
        <td>${esc(assigned.join(', ') || '–')}</td>
        <td>${esc(fmtDate(v.created_at))}</td>
        <td class="vc-opt">${_promptRowMenu(v, assigned, setModel)}</td>
      </tr>`
  }).join('')

  const scopeOptions = [
    `<option value="general"${activeScope === 'general' ? ' selected' : ''}>General</option>`,
    ...models.map(m => `<option value="${esc(m)}"${activeScope === m ? ' selected' : ''}>${esc(m)}</option>`),
  ].join('')

  const filterOptions = [
    ['all', 'All'], ['general', 'General'], ...models.map(m => [m, m]),
  ].map(([val, label]) => `<option value="${esc(val)}"${filter === val ? ' selected' : ''}>${esc(label)}</option>`).join('')

  panel.innerHTML = `
    <div class="prompt-block">
      <div class="prompt-head-row">
        <div class="settings-section-title">Slots</div>
        <div class="prompt-head-actions">
          <label class="small" for="promptSlotScope">Scope
            <select id="promptSlotScope">${scopeOptions}</select>
          </label>
        </div>
      </div>
      <div class="prompt-table-wrap">
        <table class="prompt-table">
          <caption class="cm-visually-hidden">Prompt version slots per stage</caption>
          <thead><tr><th>Stage</th><th>local</th><th>production</th><th></th><th>Cloud Function drift</th></tr></thead>
          <tbody>${slotRows}</tbody>
        </table>
      </div>
      <div class="prompt-actions">
        <button class="mini-btn" data-action="sync">Sync / Promote to Cloud Function</button>
        <label class="small"><input type="checkbox" id="promptSmokeCf" /> Smoke tests use Cloud Function</label>
      </div>
    </div>
    <div class="prompt-block">
      <div class="prompt-head-row">
        <div class="settings-section-title">Versions</div>
        <div class="prompt-head-actions">
          <label class="small" for="promptScopeFilter">Filter
            <select id="promptScopeFilter">${filterOptions}</select>
          </label>
          <button class="model-add-btn" data-action="new-version">+ New version</button>
        </div>
      </div>
      <div class="prompt-table-wrap is-menu-safe">
        <table class="prompt-table">
          <caption class="cm-visually-hidden">Prompt registry versions</caption>
          <thead><tr><th>id</th><th>label</th><th>stage</th><th>scope</th><th>length_policy</th><th>fingerprint</th><th>slots</th><th>created</th><th>actions</th></tr></thead>
          <tbody>${versionRows || '<tr><td colspan="9" class="placeholder-text">No versions in this scope. <button class="mini-btn" data-action="new-version">+ New version</button></td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <div class="prompt-block">
      <div class="settings-section-title">Preview</div>
      <pre id="promptPreview" class="prompt-preview">Select Preview on a version.</pre>
      <pre id="promptPreviewUser" class="prompt-preview cm-hidden"></pre>
      <button class="mini-btn" data-action="copy-preview">Copy system prompt</button>
    </div>`

  for (const stage of ['chapter', 'composer']) {
    const stageSlots = _stageSlots(data, stage)
    const scopeSlots = activeScope === 'general'
      ? stageSlots.general
      : (stageSlots.models[activeScope] || { local: null, production: null })
    const local = document.getElementById(`promptLocal_${stage}`)
    const prod = document.getElementById(`promptProd_${stage}`)
    if (local) local.value = scopeSlots.local || ''
    if (prod) prod.value = scopeSlots.production || ''
    const row = local ? local.closest('tr') : null
    const save = row ? row.querySelector('[data-action="save-slot"]') : null
    const markDirty = () => { if (save) save.disabled = false }
    if (local) local.addEventListener('change', markDirty)
    if (prod) prod.addEventListener('change', markDirty)
  }
  const scopeSel = document.getElementById('promptSlotScope')
  if (scopeSel) scopeSel.addEventListener('change', () => {
    PROMPTS_STATE.slotScope = scopeSel.value
    renderPrompts(PROMPTS_STATE.data)
  })
  const filterSel = document.getElementById('promptScopeFilter')
  if (filterSel) filterSel.addEventListener('change', () => {
    PROMPTS_STATE.filter = filterSel.value
    renderPrompts(PROMPTS_STATE.data)
  })
}

async function loadPrompts() {
  const panel = document.getElementById('promptsPanel')
  if (!panel) return
  panel.innerHTML = '<div class="placeholder-text">Loading prompts...</div>'
  try {
    if (!PROMPTS_STATE.options) {
      PROMPTS_STATE.options = await api('/api/prompts/options')
    }
    const data = await api('/api/prompts')
    PROMPTS_STATE.data = data
    renderPrompts(data)
    setPromptsBanner('')
  } catch (e) {
    panel.innerHTML = `<div class="placeholder-text">Failed to load prompts: ${esc(e.message)}
      <button class="mini-btn" id="promptsRetry">Retry</button></div>`
    const btn = document.getElementById('promptsRetry')
    if (btn) btn.addEventListener('click', loadPrompts)
  }
}

async function savePromptSlot(stage, model) {
  const data = PROMPTS_STATE.data || {}
  const local = document.getElementById(`promptLocal_${stage}`).value
  const prod = document.getElementById(`promptProd_${stage}`).value
  const slots = _stageSlots(data, stage)
  const current = model ? (slots.models[model] || { local: null, production: null }) : slots.general
  const writes = []
  if (local !== (current.local || '')) writes.push({ slot: 'local', version_id: local })
  if (prod !== (current.production || '')) writes.push({ slot: 'production', version_id: prod })
  if (!writes.length) {
    setPromptsBanner(`No ${model ? model + ' ' : ''}${stage} slot changes.`)
    return
  }
  let sha = data.registry_sha || ''
  try {
    for (const write of writes) {
      const body = { stage, slot: write.slot, version_id: write.version_id, expected_sha: sha }
      if (model) body.model = model
      const res = await api('/api/prompts/slots', { method: 'POST', body: JSON.stringify(body) })
      if (res && res.registry_sha) sha = res.registry_sha
    }
  } catch (e) {
    if (e.detail && e.detail.error === 'registry_changed') {
      await loadPrompts()
      setPromptsBanner('Registry changed elsewhere — reloading. Reapply your selection.')
      return
    }
    throw e
  }
  await loadPrompts()
  setPromptsBanner(`Saved ${model ? model + ' ' : ''}${stage} slots.`)
}

// ── version create/edit dialog ─────────────────────────────────

function _pvModels() {
  const opts = PROMPTS_STATE.options || {}
  return opts.models || (PROMPTS_STATE.data || {}).models || []
}

function _pvProfilesByModel() {
  return (PROMPTS_STATE.options || {}).profiles_by_model || {}
}

function _pvLibText(stage, dim, id) {
  const lib = ((PROMPTS_STATE.options || {}).component_text || {})[stage] || {}
  return (lib[dim] && lib[dim][id]) || ''
}

function pvRenderScope() {
  const kind = document.getElementById('pvScopeKind').value
  document.getElementById('pvScopeExtra').classList.toggle('cm-hidden', kind !== 'model')
  const pv = PROMPTS_STATE.pv || {}
  const sel = document.getElementById('pvScopeModel')
  const current = (pv.scope && pv.scope.model) || sel.value || ''
  sel.innerHTML = _pvModels().map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')
  if (current) sel.value = current
  pvRenderProfiles()
}

function pvRenderProfiles() {
  const model = document.getElementById('pvScopeModel').value
  const pv = PROMPTS_STATE.pv || {}
  const selected = new Set((pv.scope && pv.scope.profiles) || [])
  const sel = document.getElementById('pvScopeProfiles')
  sel.innerHTML = (_pvProfilesByModel()[model] || [])
    .map(p => `<option value="${esc(p)}"${selected.has(p) ? ' selected' : ''}>${esc(p)}</option>`)
    .join('')
}

function pvRenderComponents() {
  const stage = document.getElementById('pvStage').value
  const opts = PROMPTS_STATE.options || {}
  const dims = opts.dimensions || {}
  const help = opts.dimension_help || {}
  const keys = (opts.stage_keys || {})[stage] || Object.keys(dims)
  const pv = PROMPTS_STATE.pv || {}
  const components = (pv.components && pv.stage === stage) ? pv.components : (pv.components || {})
  const componentText = (pv.component_text && pv.stage === stage) ? pv.component_text : (pv.component_text || {})
  const container = document.getElementById('pvComponents')
  container.innerHTML = keys.map(key => {
    const selectedId = components[key] || ''
    const override = componentText[key] || ''
    const libText = selectedId ? _pvLibText(stage, key, selectedId) : ''
    const tip = help[key] || ''
    const tipAttr = tip ? ` title="${esc(tip)}"` : ''
    const info = tip
      ? `<span class="prompt-help" role="img" aria-label="${esc(tip)}" tabindex="0"${tipAttr}>?</span>`
      : ''
    return `<div class="field" data-dim="${esc(key)}">
      <label for="pvComp_${esc(key)}"${tipAttr}>${esc(key)}${info}</label>
      <select id="pvComp_${esc(key)}" data-component="${esc(key)}"${tipAttr}><option value="">(inherit)</option>
        ${(dims[key] || []).map(id => `<option value="${esc(id)}"${selectedId === id ? ' selected' : ''}>${esc(id)}</option>`).join('')}
      </select>
      <details class="prompt-inline-editor">
        <summary>Edit text</summary>
        <textarea data-component-text="${esc(key)}" rows="3" spellcheck="false" placeholder="${esc(libText).slice(0, 120)}">${esc(override)}</textarea>
        <div class="field-hint">Override the component text for this version. Blank keeps the library text.</div>
      </details>
    </div>`
  }).join('')
}

function pvCollect() {
  const stage = document.getElementById('pvStage').value
  const components = {}
  document.querySelectorAll('#pvComponents select[data-component]').forEach(sel => {
    if (sel.value) components[sel.dataset.component] = sel.value
  })
  const componentText = {}
  document.querySelectorAll('#pvComponents textarea[data-component-text]').forEach(t => {
    if (t.value.trim()) componentText[t.dataset.componentText] = t.value
  })
  const kind = document.getElementById('pvScopeKind').value
  let scope = { kind: 'general' }
  if (kind === 'model') {
    scope = { kind: 'model', model: document.getElementById('pvScopeModel').value }
    const profiles = [...document.getElementById('pvScopeProfiles').selectedOptions].map(o => o.value)
    if (profiles.length) scope.profiles = profiles
  }
  return {
    stage,
    label: document.getElementById('pvLabel').value.trim(),
    components,
    component_text: componentText,
    system_template: document.getElementById('pvSystemTemplate').value,
    user_template: document.getElementById('pvUserTemplate').value,
    scope,
  }
}

function pvClientValidate(draft) {
  const errors = {}
  if (!draft.label) errors.label = 'Label is required'
  else if (draft.label.length > 120) errors.label = 'Label must be 120 characters or fewer'
  if (draft.scope.kind === 'model' && !draft.scope.model) errors.scope = 'Choose a model'
  const userT = draft.user_template.trim()
  if (userT) {
    const required = draft.stage === 'chapter' ? 'source_md' : 'chapter_summaries'
    if (!new RegExp(`\\{\\{\\s*${required}\\s*\\}\\}`).test(userT)) {
      errors.user_template = `Template must contain {{${required}}}`
    }
  }
  return errors
}

function pvClearErrors() {
  const map = { label: 'pvLabelError', scope: 'pvScopeError', system_template: 'pvSystemTemplateError', user_template: 'pvUserTemplateError' }
  for (const [field, id] of Object.entries(map)) {
    const el = document.getElementById(id)
    if (el) { el.textContent = ''; el.classList.add('cm-hidden') }
  }
  document.querySelectorAll('#pvComponents select, #pvLabel, #pvScopeModel, #pvSystemTemplate, #pvUserTemplate')
    .forEach(el => el.classList.remove('field-invalid'))
}

function pvApplyErrors(errors) {
  pvClearErrors()
  const map = { label: 'pvLabelError', scope: 'pvScopeError', system_template: 'pvSystemTemplateError', user_template: 'pvUserTemplateError' }
  let firstInvalid = null
  for (const [field, message] of Object.entries(errors || {})) {
    const id = map[field]
    const el = id ? document.getElementById(id) : null
    if (el) { el.textContent = message; el.classList.remove('cm-hidden') }
    const inputId = field === 'label' ? 'pvLabel' : field === 'scope' ? 'pvScopeModel' : `pv${field === 'system_template' ? 'SystemTemplate' : 'UserTemplate'}`
    const input = document.getElementById(inputId)
    if (input) { input.classList.add('field-invalid'); firstInvalid = firstInvalid || input }
  }
  if (firstInvalid) firstInvalid.focus()
}

function pvSnapshot() {
  try { return JSON.stringify(pvCollect()) } catch { return '' }
}

function pvSchedulePreview() {
  const pv = PROMPTS_STATE.pv
  if (!pv) return
  pv.dirty = typeof pv.baseline === 'string' && pvSnapshot() !== pv.baseline
  if (PROMPTS_STATE.pvPreviewTimer) clearTimeout(PROMPTS_STATE.pvPreviewTimer)
  document.getElementById('pvSystemPreview').textContent = 'Rendering…'
  PROMPTS_STATE.pvPreviewTimer = setTimeout(pvPreview, 400)
}

async function pvPreview() {
  const pv = PROMPTS_STATE.pv
  if (!pv) return
  const draft = pvCollect()
  const errors = pvClientValidate(draft)
  if (Object.keys(errors).length) { pvApplyErrors(errors); return }
  pvApplyErrors({})
  const meta = document.getElementById('pvPreviewMeta')
  const legend = document.getElementById('pvLegend')
  const warnEl = document.getElementById('pvWarnings')
  const notice = document.getElementById('pvNotice')
  if (PROMPTS_STATE.pvAbort) { try { PROMPTS_STATE.pvAbort.abort() } catch { /* noop */ } }
  const controller = new AbortController()
  PROMPTS_STATE.pvAbort = controller
  try {
    const res = await fetch('/api/prompts/preview-draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        stage: draft.stage,
        components: draft.components,
        component_text: draft.component_text,
        system_template: draft.system_template,
        user_template: draft.user_template,
        target_words: 1200,
      }),
      signal: controller.signal,
    })
    const body = await res.json()
    if (!res.ok || body.ok === false) {
      const fe = (body && body.fieldErrors) || {}
      if (Object.keys(fe).length) { pvApplyErrors(fe); return }
      throw new Error((body && body.error) || `HTTP ${res.status}`)
    }
    document.getElementById('pvSystemPreview').textContent = body.system_prompt || ''
    document.getElementById('pvUserPreview').textContent = body.user_prompt || ''
    meta.textContent = `fingerprint: ${body.fingerprint || ''}`
    const ph = body.placeholders || {}
    legend.textContent = `System variables: ${(ph.system || []).join(', ') || '(none)'} · User variables: ${(ph.user || []).join(', ') || '(none)'}`
    const warnings = body.warnings || []
    warnEl.textContent = warnings.join(' · ')
    warnEl.classList.toggle('cm-hidden', !warnings.length)
    notice.classList.add('cm-hidden')
    pv.transportError = false
    pv.previewOk = true
    setPvSaveState()
  } catch (e) {
    if (e && e.name === 'AbortError') return
    document.getElementById('pvSystemPreview').textContent = `Preview failed: ${e.message}`
    pv.transportError = true
    pv.previewOk = false
    setPvSaveState()
  }
}

function setPvSaveState() {
  const pv = PROMPTS_STATE.pv
  const btn = document.getElementById('pvSave')
  if (!pv || !btn) return
  const draft = pv.busy ? null : pvCollect()
  const clientErrors = draft ? pvClientValidate(draft) : {}
  const blocked = pv.busy || Object.keys(clientErrors).length > 0 || pv.transportError
  btn.disabled = blocked
  if (!pv.busy) btn.textContent = pv.mode === 'edit' ? 'Save as new version' : 'Create version'
}

async function pvSave() {
  const pv = PROMPTS_STATE.pv
  if (!pv || pv.busy) return
  const draft = pvCollect()
  const errors = pvClientValidate(draft)
  if (Object.keys(errors).length) { pvApplyErrors(errors); return }
  pv.busy = true
  const btn = document.getElementById('pvSave')
  btn.textContent = 'Saving…'
  btn.disabled = true
  const errEl = document.getElementById('pvError')
  errEl.classList.add('cm-hidden')
  try {
    let res
    if (pv.mode === 'edit') {
      res = await api(`/api/prompts/${encodeURIComponent(pv.sourceId)}/revise`, { method: 'POST', body: JSON.stringify(draft) })
    } else {
      res = await api('/api/prompts', { method: 'POST', body: JSON.stringify(draft) })
    }
    pv.dirty = false
    const newId = res.version && res.version.id
    const derivedFrom = pv.sourceId
    const wasEdit = pv.mode === 'edit'
    hidePromptVersionDialog()
    await loadPrompts()
    if (wasEdit) setPromptsBanner(`Created ${newId} (derived from ${derivedFrom}).`)
    else setPromptsBanner(`Created ${newId}.`)
  } catch (e) {
    const detail = e.detail || {}
    if (detail.fieldErrors) pvApplyErrors(detail.fieldErrors)
    if (detail.code === 409) {
      setPromptsBanner('Registry changed elsewhere — reloading.')
      try { await loadPrompts() } catch { /* noop */ }
    } else {
      errEl.textContent = e.message
      errEl.classList.remove('cm-hidden')
    }
    pv.busy = false
    setPvSaveState()
  }
}

async function openPromptVersionDialog(opts) {
  const source = opts.source || null
  if (!source && opts.mode !== 'create') {
    setPromptsBanner('Version not found — reloading prompts.')
    await loadPrompts()
    return
  }
  if (!PROMPTS_STATE.options) {
    try { PROMPTS_STATE.options = await api('/api/prompts/options') } catch { PROMPTS_STATE.options = {} }
  }
  const versions = (PROMPTS_STATE.data || {}).versions || []
  PROMPTS_STATE.pv = {
    mode: opts.mode || 'create',
    sourceId: source ? source.id : '',
    stage: source ? source.stage : 'chapter',
    components: source ? { ...(source.components || {}) } : {},
    component_text: source ? { ...(source.component_text || {}) } : {},
    scope: source ? { ...(source.scope || { kind: 'general' }) } : { kind: 'general' },
    dirty: false,
    busy: false,
    transportError: false,
    previewOk: false,
  }
  const pv = PROMPTS_STATE.pv
  document.getElementById('pvTitle').textContent = opts.mode === 'edit'
    ? `Edit ${source.id}`
    : opts.mode === 'clone' ? `Clone ${source.id}` : 'New version'
  const stageSel = document.getElementById('pvStage')
  stageSel.value = pv.stage
  stageSel.disabled = opts.mode === 'edit' || opts.mode === 'clone'
  document.getElementById('pvLabel').value = opts.mode === 'edit'
    ? (source.label || '')
    : opts.mode === 'clone' ? `${source.label || source.id} (copy)` : `v${versions.length + 1} experiment`
  document.getElementById('pvSystemTemplate').value = source ? (source.system_template || '') : ''
  document.getElementById('pvUserTemplate').value = source ? (source.user_template || '') : ''
  document.getElementById('pvScopeKind').value = pv.scope.kind || 'general'
  pvRenderScope()
  pvRenderComponents()
  pvClearErrors()
  document.getElementById('pvError').classList.add('cm-hidden')
  document.getElementById('pvNotice').classList.add('cm-hidden')
  document.getElementById('promptVersionOverlay').classList.remove('cm-hidden')
  const settings = document.getElementById('settingsOverlay')
  if (settings) settings.setAttribute('aria-hidden', 'true')
  document.getElementById('pvLabel').focus()
  setPvSaveState()
  pv.baseline = pvSnapshot()
  pv.dirty = false
  pvSchedulePreview()
}

function hidePromptVersionDialog() {
  document.getElementById('promptVersionOverlay').classList.add('cm-hidden')
  const settings = document.getElementById('settingsOverlay')
  if (settings) settings.removeAttribute('aria-hidden')
  if (PROMPTS_STATE.pvPreviewTimer) { clearTimeout(PROMPTS_STATE.pvPreviewTimer); PROMPTS_STATE.pvPreviewTimer = null }
  if (PROMPTS_STATE.pvAbort) { try { PROMPTS_STATE.pvAbort.abort() } catch { /* noop */ } PROMPTS_STATE.pvAbort = null }
  PROMPTS_STATE.pv = null
}

function closePromptVersionDialog(force = false) {
  const pv = PROMPTS_STATE.pv
  if (!pv) { hidePromptVersionDialog(); return }
  if (pv.busy) return
  if (!force && pv.dirty && !window.confirm('Discard unsaved changes to this version?')) return
  pv.dirty = false
  hidePromptVersionDialog()
}

async function previewPromptVersion(id) {
  const out = document.getElementById('promptPreview')
  const outUser = document.getElementById('promptPreviewUser')
  try {
    const res = await api(`/api/prompts/${encodeURIComponent(id)}/preview`, { method: 'POST', body: JSON.stringify({ target_words: 1200 }) })
    PROMPTS_STATE.previewText = res.system_prompt
    out.textContent = `fingerprint: ${res.fingerprint}\n\n${res.system_prompt}`
    if (outUser) {
      outUser.textContent = res.user_prompt || ''
      outUser.classList.toggle('cm-hidden', !res.user_prompt)
    }
  } catch (e) {
    out.textContent = `Preview failed: ${e.message}`
  }
}

async function deletePromptVersion(id) {
  if (!window.confirm(`Delete prompt version ${id}? This cannot be undone.`)) return
  try {
    await api(`/api/prompts/${encodeURIComponent(id)}`, { method: 'DELETE' })
    await loadPrompts()
  } catch (e) {
    setPromptsBanner(`Delete failed: ${e.message}`)
  }
}

async function syncPrompts() {
  try {
    const res = await api('/api/prompts/sync', { method: 'POST', body: JSON.stringify({}) })
    const scoped = res.scoped_versions ? ` (${res.scoped_versions} scoped)` : ''
    setPromptsBanner(`Synced registry ${res.registry_sha ? res.registry_sha.slice(0, 8) : ''}${scoped} to the Cloud Function bundle. Redeploy to take effect.`)
  } catch (e) {
    setPromptsBanner(`Sync failed: ${e.message}`)
  }
}

async function runPromptSmokeTest(id) {
  const useCf = !!(document.getElementById('promptSmokeCf') || {}).checked
  const args = {
    bench: 'chapter_fast',
    profile: 'all',
    time: 'all',
    'max-samples': 4,
    mock: !useCf,
    'prompt-version': id,
    'prompt-stage': 'both',
    'write-results': false,
  }
  if (useCf) args['allow-prompt-override'] = true
  try {
    await api('/api/jobs', { method: 'POST', body: JSON.stringify({ toolId: 'run_candidate', args }) })
    setPromptsBanner(`Smoke test started for ${id}. See Scripts for progress.`)
  } catch (e) {
    setPromptsBanner(`Smoke test failed to start: ${e.message}`)
  }
}

function _findVersion(id) {
  return ((PROMPTS_STATE.data || {}).versions || []).find(v => v.id === id) || null
}

function runPromptAction(action, id, model, stage) {
  try {
    if (action === 'save-slot') { savePromptSlot(stage, model).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'preview') { previewPromptVersion(id) }
    else if (action === 'edit') { openPromptVersionDialog({ mode: 'edit', source: _findVersion(id) }).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'new-version') { openPromptVersionDialog({ mode: 'create' }).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'set-local') { api('/api/prompts/slots', { method: 'POST', body: JSON.stringify({ stage: (_findVersion(id) || {}).stage, slot: 'local', version_id: id, model: model || undefined, expected_sha: PROMPTS_STATE.data.registry_sha }) }).then(loadPrompts).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'set-production') { api('/api/prompts/slots', { method: 'POST', body: JSON.stringify({ stage: (_findVersion(id) || {}).stage, slot: 'production', version_id: id, model: model || undefined, expected_sha: PROMPTS_STATE.data.registry_sha }) }).then(() => { setPromptsBanner(`Production now points at ${id} — run Sync/Promote to deploy it to the Cloud Function.`); return loadPrompts() }).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'clone') { openPromptVersionDialog({ mode: 'clone', source: _findVersion(id) }).catch(err => setPromptsBanner(err.message)) }
    else if (action === 'delete') { deletePromptVersion(id) }
    else if (action === 'smoke') { runPromptSmokeTest(id) }
    else if (action === 'sync') { syncPrompts() }
    else if (action === 'copy-preview') { if (PROMPTS_STATE.previewText) navigator.clipboard.writeText(PROMPTS_STATE.previewText) }
  } catch (err) {
    setPromptsBanner(err.message)
  }
}

function bindPromptsPanel() {
  const panel = document.getElementById('promptsPanel')
  if (!panel || panel.dataset.bound) return
  panel.dataset.bound = '1'
  panel.addEventListener('click', (e) => {
    const optBtn = e.target.closest('.vc-opt-btn')
    if (optBtn) {
      e.stopPropagation()
      const menuEl = optBtn.closest('.vc-opt').querySelector('.vc-opt-menu')
      const open = !menuEl.classList.contains('cm-hidden')
      closeProfileMenus()
      closePromptMenus()
      if (!open) {
        menuEl.classList.remove('cm-hidden')
        optBtn.setAttribute('aria-expanded', 'true')
      }
      return
    }
    const item = e.target.closest('.vc-opt-item')
    if (item) {
      e.stopPropagation()
      closePromptMenus()
      if (item.disabled) return
      runPromptAction(item.dataset.action, item.dataset.id, item.dataset.model, item.dataset.stage)
      return
    }
    const btn = e.target.closest('[data-action]')
    if (!btn) return
    runPromptAction(btn.dataset.action, btn.dataset.id, btn.dataset.model, btn.dataset.stage)
  })
}

function bindPromptVersionDialog() {
  const overlay = document.getElementById('promptVersionOverlay')
  if (!overlay) return
  document.getElementById('pvClose').addEventListener('click', () => closePromptVersionDialog())
  document.getElementById('pvCancel').addEventListener('click', () => closePromptVersionDialog())
  document.getElementById('pvSave').addEventListener('click', () => pvSave())
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closePromptVersionDialog() })
  document.getElementById('pvStage').addEventListener('change', () => { const pv = PROMPTS_STATE.pv; if (pv) { pv.stage = document.getElementById('pvStage').value; pvRenderComponents() } pvSchedulePreview() })
  document.getElementById('pvScopeKind').addEventListener('change', () => { pvRenderScope(); pvSchedulePreview() })
  document.getElementById('pvScopeModel').addEventListener('change', () => { pvRenderProfiles(); pvSchedulePreview() })
  document.getElementById('pvScopeProfiles').addEventListener('change', () => pvSchedulePreview())
  document.getElementById('pvComponents').addEventListener('change', (e) => {
    if (e.target.matches('select[data-component]')) {
      const dim = e.target.dataset.component
      const details = e.target.closest('.field')
      const ta = details ? details.querySelector('textarea[data-component-text]') : null
      const stage = document.getElementById('pvStage').value
      if (ta) ta.placeholder = _pvLibText(stage, dim, e.target.value).slice(0, 120)
    }
    pvSchedulePreview()
  })
  document.getElementById('pvComponents').addEventListener('input', () => pvSchedulePreview())
  for (const id of ['pvLabel', 'pvSystemTemplate', 'pvUserTemplate']) {
    document.getElementById(id).addEventListener('input', () => { pvSchedulePreview(); setPvSaveState() })
  }
}

function stopRunPolling() {
  if (RUN_STATE.pollTimer) {
    clearInterval(RUN_STATE.pollTimer)
    RUN_STATE.pollTimer = null
  }
  stopDlgTimer()
  stopPanelTimer()
  for (const job of [...RUN_STATE.es.keys()]) detachStream(job)
  for (const job of [...RUN_STATE.dlgEs.keys()]) detachDlgStream(job)
  for (const job of [...RUN_STATE.panelEs.keys()]) detachPanelStream(job)
  for (const job of [...RUN_STATE.widgetEs.keys()]) detachWidgetStream(job)
}

function initSettings() {
  if (document.getElementById('settingsOverlay')) return
  const wrap = document.createElement('div')
  wrap.innerHTML = MODAL_TEMPLATE
  document.body.appendChild(wrap)
  wireGearButtons()
  bindModal()
  bindRunsManager()
  bindPromptsPanel()
  bindPromptVersionDialog()
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSettings)
  } else {
    initSettings()
  }
}

export { initSettings, initRunWidget, GEAR_SVG, MODAL_TEMPLATE, toolCardHtml, RUN_STATE }