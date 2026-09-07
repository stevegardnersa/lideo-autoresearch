import { defineConfig } from 'vite'
import { readdirSync, readFileSync, writeFileSync, existsSync, appendFileSync, mkdirSync, statSync, unlinkSync } from 'fs'
import { join, extname } from 'path'
import { fileURLToPath } from 'url'
import { randomUUID } from 'crypto'
import { execFile, spawn } from 'child_process'
import https from 'https'

const VITE_DIR = fileURLToPath(new URL('.', import.meta.url))
const REPO_ROOT = join(VITE_DIR, '..')
const CANDIDATES_PATH = join(REPO_ROOT, 'data', 'candidates.json')
const PYTHON = process.env.PYTHON || 'python3'

function stripQuery(url) {
  const i = url.indexOf('?')
  return i >= 0 ? url.slice(0, i) : url
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function slugOfModel(model) {
  const parts = String(model).split('/')
  return parts[parts.length - 1]
}

function timeBudgetOf(key) {
  return String(key).split('_')[0] || '30m'
}

const EFFORT_ORDER = ['thinking', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

// Reasoning takes ~fraction of the token budget; mirrors core/reasoning.py.
const EFFORT_FRACTION = {
  none: 0.0, minimal: 0.10, low: 0.20, medium: 0.50, high: 0.80, xhigh: 0.95, max: 0.95,
}
const BASE_MAX_TOKENS = 8192
const MAX_TOKEN_CAP = 163840

function effortOf(key) {
  const k = String(key)
  if (k.endsWith('_thinking')) return 'thinking'
  if (k.endsWith('_notthinking')) return 'none'
  const m = k.match(/_(effort-[a-z]+)$/)
  return m ? m[1].replace(/^effort-/, '') : 'plain'
}

function keyFor(tb, slug, effort) {
  if (effort === 'thinking') return `${tb}_${slug}_thinking`
  if (effort === 'none') return `${tb}_${slug}_notthinking`
  if (!effort || effort === 'plain') return `${tb}_${slug}_plain`
  return `${tb}_${slug}_effort-${effort}`
}

function effortDefaultMaxTokens(effort) {
  const f = EFFORT_FRACTION[effort] || 0
  if (f <= 0) return BASE_MAX_TOKENS
  return Math.min(Math.ceil(BASE_MAX_TOKENS / (1 - f)), MAX_TOKEN_CAP)
}

function specStyle(spec) {
  const stage = spec && spec.chapter_stage
  return (stage && stage.extra_body && stage.extra_body.thinking) ? 'legacy' : 'new'
}

function clearReasoningKeys(stage) {
  if (!stage) return
  if (stage.extra_body) {
    delete stage.extra_body.thinking
    if (Object.keys(stage.extra_body).length === 0) delete stage.extra_body
  }
  delete stage.reasoning
  delete stage.reasoning_effort
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = ''
    req.on('data', c => { body += c })
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {})
      } catch (e) {
        reject(new Error('Invalid JSON body'))
      }
    })
    req.on('error', reject)
  })
}

function sendJson(res, statusCode, payload) {
  res.statusCode = statusCode
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(payload))
}

function runPythonAsync(args, stdin, cwd = REPO_ROOT) {
  return new Promise((resolve) => {
    const child = execFile(PYTHON, args, {
      cwd,
      maxBuffer: 64 * 1024 * 1024,
      timeout: 300000,
    }, (err, stdout, stderr) => {
      resolve({
        ok: !err,
        exitCode: err ? (err.code ?? -1) : 0,
        stdout: stdout || '',
        stderr: stderr || '',
        message: err ? (err.message || String(err)) : '',
      })
    })
    try {
      if (stdin) child.stdin.end(stdin)
      else child.stdin.end()
    } catch { /* stdin already closed */ }
  })
}

function regenerateSpecPy(pythonRunner) {
  return pythonRunner(['tools/gen_profile_literal.py'])
}

function readCandidates(candidatesPath) {
  return JSON.parse(readFileSync(candidatesPath, 'utf-8'))
}

function writeCandidates(candidatesPath, data) {
  writeFileSync(candidatesPath, JSON.stringify(data, null, 2) + '\n')
}

function makeCtx(opts = {}) {
  const repoRoot = opts.repoRoot || REPO_ROOT
  return {
    repoRoot,
    candidatesPath: opts.candidatesPath || CANDIDATES_PATH,
    runsDir: opts.runsDir || join(process.cwd(), 'runs'),
    dataDir: opts.dataDir || join(process.cwd(), 'data'),
    pythonRunner: opts.pythonRunner || ((args, stdin) => runPythonAsync(args, stdin, repoRoot)),
    autoTag: opts.autoTag || autoTagWithLLM,
    jobManager: opts.jobManager || null,
    jobsDir: opts.jobsDir || null,
    scriptRegistry: opts.scriptRegistry || null,
  }
}

// ── Keyword-based tag inference (fallback when tags empty) ─────

const DIMENSION_SLUGS = ['style','detail','qualifier','structure','example','terminology','anti_fluff']

const DIMENSION_KEYWORDS = {
  style: ['tone','voice','compression','dense','wordy','concise','verbose','readable','readability','pace','pacing','write','writing','feels','sounds'],
  detail: ['detail','mechanism','concept','balance','depth','deep','surface','coverage','covered','missing detail','enough detail','too much detail'],
  qualifier: ['qualifier','caveat','exception','nuance','hedging','certainty','uncertain','qualified','absolute','limit','limitation','scope','tradeoff','trade-off'],
  structure: ['structure','heading','section','bullet','organization','organised','organized','cluster','theme','outline','scan','heading','subhead','subsection','layout','flow','paragraph'],
  example: ['example','anecdote','illustration','instance','case','sparse','few examples','too many examples','explanatory','decorative'],
  terminology: ['term','terminology','glossary','gloss','jargon','technical','vocabulary','word choice','source terms','defined','definition'],
  anti_fluff: ['fluff','fluffy','filler','repetition','repetitive','repeats','padding','waste','extra','unnecessary','bland','generic','surface-level','shallow']
}

function inferTags(text) {
  const lower = text.toLowerCase()
  const tags = new Set()
  for (const [dim, words] of Object.entries(DIMENSION_KEYWORDS)) {
    for (const w of words) {
      if (lower.includes(w)) { tags.add(dim); break }
    }
  }
  return [...tags]
}

// ── LLM auto-tag (OpenCode Go API) ─────────────────────────────

const OPCODE_BASE = process.env.OPENCODE_BASE_URL || 'https://zen.openai.azure.com'
const OPCODE_KEY = process.env.OPENCODE_API_KEY || ''
const OPCODE_MODEL = 'opencode-go/deepseek-v4-flash'

async function autoTagWithLLM(text) {
  if (!OPCODE_KEY) {
    console.warn('[autotag] OPENCODE_API_KEY not set — using keyword fallback')
    return { tags: inferTags(text), sentiment: 0, source: 'keyword' }
  }

  const prompt = `You are a prompt optimization classifier. Given a human reviewer's note about an LLM-generated book chapter summary, classify it.

Output ONLY valid JSON with these keys:
- tags: array of strings from ["style","detail","qualifier","structure","example","terminology","anti_fluff"] — which prompt dimension(s) the note addresses
- sentiment: float from -1.0 (strong negative) to 1.0 (strong positive) — whether the note says the current prompt setting works well (+) or poorly (-)

HUMAN NOTE: ${text.slice(0, 500)}

Return JSON only.`

  const body = JSON.stringify({
    model: OPCODE_MODEL,
    messages: [
      { role: 'system', content: 'You output valid JSON only. No markdown, no explanation.' },
      { role: 'user', content: prompt }
    ],
    temperature: 0.1,
    max_tokens: 256,
    response_format: { type: 'json_object' }
  })

  return new Promise((resolve) => {
    try {
      const req = https.request(`${OPCODE_BASE}/v1/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${OPCODE_KEY}`,
          'User-Agent': 'autoresearch-auto-tag/1.0'
        },
        timeout: 15000
      }, (resp) => {
        let data = ''
        resp.on('data', chunk => { data += chunk })
        resp.on('end', () => {
          try {
            const parsed = JSON.parse(data)
            const content = parsed?.choices?.[0]?.message?.content || ''
            let result
            try {
              result = JSON.parse(content)
            } catch {
              // Sometimes LLM wraps in markdown code fences
              const jsonMatch = content.match(/\{[\s\S]*\}/)
              if (jsonMatch) result = JSON.parse(jsonMatch[0])
            }
            if (result && Array.isArray(result.tags)) {
              result.tags = result.tags.filter(t => DIMENSION_SLUGS.includes(t))
              result.source = 'llm'
              if (typeof result.sentiment !== 'number') result.sentiment = 0
              resolve(result)
            } else {
              resolve({ tags: inferTags(text), sentiment: 0, source: 'keyword_fallback_parse' })
            }
          } catch (e) {
            console.warn(`[autotag] LLM parse failed: ${e.message}`)
            resolve({ tags: inferTags(text), sentiment: 0, source: 'keyword_fallback_parse' })
          }
        })
      })
      req.on('timeout', () => {
        req.destroy()
        resolve({ tags: inferTags(text), sentiment: 0, source: 'keyword_fallback_timeout' })
      })
      req.on('error', () => {
        resolve({ tags: inferTags(text), sentiment: 0, source: 'keyword_fallback_error' })
      })
      req.write(body)
      req.end()
    } catch {
      resolve({ tags: inferTags(text), sentiment: 0, source: 'keyword_fallback_exception' })
    }
  })
}

function readAndSendNotes(notesPath, res) {
  if (!existsSync(notesPath)) {
    res.setHeader('Content-Type', 'application/json')
    res.end('[]')
    return
  }
  try {
    const raw = readFileSync(notesPath, 'utf-8')
    const lines = raw.trim().split('\n').filter(Boolean)
    const notes = []
    for (const line of lines) {
      try { notes.push(JSON.parse(line)) } catch { /* skip malformed */ }
    }
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(notes))
  } catch (e) {
    res.statusCode = 500
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ ok: false, error: e.message }))
  }
}

function serveStaticFile(rootDir, urlPath) {
  const safePath = stripQuery(urlPath).replace(/^\/+/, '')
  const filePath = join(rootDir, safePath)
  if (!existsSync(filePath)) return null
  try {
    const content = readFileSync(filePath, 'utf-8')
    const ext = extname(filePath)
    const mimeTypes = {
      '.json': 'application/json',
      '.jsonl': 'application/jsonl',
      '.txt': 'text/plain',
      '.csv': 'text/csv',
      '.tsv': 'text/tab-separated-values',
      '.md': 'text/markdown',
      '.html': 'text/html',
      '.css': 'text/css',
      '.js': 'application/javascript',
    }
    return { content, contentType: mimeTypes[ext] || 'application/octet-stream' }
  } catch {
    return null
  }
}

// ── Models API ─────────────────────────────────────────────────

function collectRunStats(models, runsDir) {
  try {
    const dirs = readdirSync(runsDir, { withFileTypes: true })
    for (const dir of dirs) {
      if (!dir.isDirectory()) continue
      const files = readdirSync(join(runsDir, dir.name)).filter(f => f.endsWith('.json') && !f.endsWith('.state.json'))
      for (const f of files) {
        let run
        try {
          run = JSON.parse(readFileSync(join(runsDir, dir.name, f), 'utf-8'))
        } catch { continue }
        const rm = run && run.run_manifest
        const model = rm && rm.chapter_model
        if (!model || !models[model]) continue
        const m = models[model]
        m.runs_count = (m.runs_count || 0) + 1
        if (rm.profile) m._testedProfiles.add(rm.profile)
        const created = rm.created_at_utc
        if (created && (!m.last_tested || created > m.last_tested)) m.last_tested = created
        const score = run.dataset_score && typeof run.dataset_score.mean_quality === 'number'
          ? run.dataset_score.mean_quality
          : null
        if (score == null) continue
        const isLlm = !!(rm.judge_model && rm.judge_version_resolved && String(rm.judge_version_resolved).includes('judge-absolute-v1'))
        const bucket = isLlm ? 'best_quality_llm' : 'best_quality_det'
        if (m[bucket] == null || score > m[bucket]) m[bucket] = score
      }
    }
  } catch { /* runs dir not found */ }
}

function buildModelsIndex(ctx) {
  const data = readCandidates(ctx.candidatesPath)
  const models = {}
  const profiles = data.profiles || {}
  // Registered models (data.models) stay in the list even when they have zero
  // profiles — e.g. after every variant was removed in the editor.
  const registered = data.models || {}
  for (const model of Object.keys(registered)) {
    if (!model) continue
    const reg = registered[model] || {}
    models[model] = models[model] || {
      model,
      profiles: [],
      runs_count: 0,
      last_tested: null,
      best_quality_det: null,
      best_quality_llm: null,
      provider_route: reg.provider_route || null,
      registered: true,
      _testedProfiles: new Set(),
    }
  }
  for (const [key, spec] of Object.entries(profiles)) {
    const model = spec && spec.chapter_stage && spec.chapter_stage.model
    if (!model) continue
    let m = models[model]
    if (!m) {
      m = models[model] = {
        model,
        profiles: [],
        runs_count: 0,
        last_tested: null,
        best_quality_det: null,
        best_quality_llm: null,
        provider_route: null,
        registered: false,
        _testedProfiles: new Set(),
      }
    }
    m.profiles.push({
      slug: key,
      time_budget: timeBudgetOf(key),
      thinking: effortOf(key) === 'thinking',
      effort: effortOf(key),
      style: specStyle(spec),
      status: 'pending',
      temperature: (spec.chapter_stage && typeof spec.chapter_stage.temperature === 'number') ? spec.chapter_stage.temperature : null,
      max_tokens: (spec.chapter_stage && typeof spec.chapter_stage.max_tokens === 'number') ? spec.chapter_stage.max_tokens : null,
      provider_route: (spec.chapter_stage && spec.chapter_stage.provider) ? JSON.stringify(spec.chapter_stage.provider) : '',
    })
  }
  collectRunStats(models, ctx.runsDir)
  for (const m of Object.values(models)) {
    for (const p of m.profiles) {
      if (m._testedProfiles.has(p.slug)) p.status = 'tested'
    }
    delete m._testedProfiles
  }
  return Object.values(models).sort((a, b) => a.model.localeCompare(b.model))
}

function parseAddOutput(stdout) {
  const created = []
  const re = /Will create profile: (\S+)/g
  let mm
  while ((mm = re.exec(stdout)) !== null) created.push(mm[1])

  const probe = { schema: null, thinking: null, notthinking: null, efforts: null, effort_style: null }
  const lines = String(stdout).split('\n')
  for (const line of lines) {
    const t = line.trim()
    if (t.startsWith('Probing JSON schema support')) {
      probe.schema = !/ \[[^\]]*\]/.test(t.split('...')[1] || '')
      continue
    }
    const styleM = t.match(/^Probing effort tiers \(([a-z_]+)\):$/)
    if (styleM) {
      probe.effort_style = styleM[1]
      probe.efforts = probe.efforts || []
      continue
    }
    const effortM = t.match(/^effort '([a-z][\w-]*)' \((\w+)\)\.\.\. (\w+)/)
    if (effortM) {
      probe.efforts = probe.efforts || []
      if (effortM[3] === 'ok') probe.efforts.push(effortM[1])
      continue
    }
    const ogProbe = t.match(/^Probing (thinking|non-thinking) mode\.\.\./)
    if (ogProbe) {
      probe[ogProbe[1] === 'thinking' ? 'thinking' : 'notthinking'] = !(/ \[[^\]]*\]/.test(t.split('...')[1] || ''))
      continue
    }
    const pricingM = t.match(/^PRICING\s+(\{.*\})$/)
    if (pricingM) {
      try {
        const p = JSON.parse(pricingM[1])
        if (p && typeof p === 'object' && Array.isArray(p.tiers)) probe.pricing = p
      } catch (e) { /* ignore malformed pricing */ }
    }
  }
  return { created, probe }
}

function buildDefaultSpec(model, thinking, schemaOk) {
  const extraBody = { thinking: { type: thinking ? 'enabled' : 'disabled' } }
  const stage = {
    model,
    temperature: 0.2,
    seed: 42,
    max_tokens: 8192,
    format_mode: 'markdown_sections',
    context_mode: 'chapter_plus_toc_and_meta',
    prompt_components: {
      system_style: 'dense_faithful',
      detail_policy: 'mechanisms_first',
      qualifier_policy: 'strict',
      structure_policy: 'heading_aware',
      example_policy: 'explanatory_only',
      terminology_policy: 'keep_source_terms',
      anti_fluff_policy: 'hard',
    },
    provider: null,
    extra_body: JSON.parse(JSON.stringify(extraBody)),
    use_json_schema: !!schemaOk,
  }
  return {
    chapter_stage: stage,
    composer_stage: { ...stage, format_mode: 'markdown_sections', context_mode: 'chapter_plus_toc_and_meta' },
  }
}

// Apply a requested effort tier to a spec. The effort name is the canonical
// tier ('thinking', 'none', 'minimal'..'max'); the key suffix is derived by
// keyFor. New-style templates get the recommended reasoning API; legacy
// templates fall back to the extra_body thinking param (scaled budget) so the
// model's config style stays internally consistent.
function applyEffortConfig(nSpec, effort, templateStyle) {
  const newStyle = templateStyle !== 'legacy'
  for (const st of [nSpec.chapter_stage, nSpec.composer_stage]) {
    clearReasoningKeys(st)
    const maxT = effortDefaultMaxTokens(effort)
    if (effort === 'thinking') {
      if (newStyle) {
        st.reasoning = { effort: 'high' }
      } else {
        st.extra_body = Object.assign(st.extra_body || {}, { thinking: { type: 'enabled' } })
      }
      st.max_tokens = maxT
    } else if (effort === 'none') {
      // plain request: no reasoning params at all
    } else if (newStyle) {
      st.reasoning = { effort }
      st.max_tokens = maxT
    } else {
      st.extra_body = Object.assign(st.extra_body || {}, { thinking: { type: 'enabled' } })
      st.max_tokens = maxT
    }
  }
}

// ── Reasoning-variant deletion cascade (run files + logs + results.tsv) ──

const CASCADE_TOOLS = new Set(['run_candidate', 'judge_existing', 'agent'])

// Runs from the repo root cwd the python scripts share; CLI defaults resolve
// runs/ artifacts/jobs/ and results.tsv relative to that same cwd.
function cascadeCliArgs(key, dryRun) {
  const args = ['tools/add_candidate.py', '--remove-profile', key, '--out', 'data/candidates.json']
  if (dryRun) args.push('--dry-run')
  return args
}

function parseCascadeOutput(stdout) {
  const grab = (re) => { const m = stdout.match(re); return m ? parseInt(m[1], 10) : 0 }
  const runRemoved = grab(/Removed (\d+) run file\(s\)/)
  const logsRemoved = grab(/Removed (\d+) job log\(s\)/)
  const rowsRemoved = grab(/Removed (\d+) results\.tsv row\(s\)/)
  return {
    runFiles: runRemoved || grab(/Found (\d+) run file\(s\)/),
    logs: logsRemoved || grab(/Found (\d+) job log\(s\)/),
    activeLogs: grab(/Skipped (\d+) active job log\(s\)/),
    resultRows: rowsRemoved || grab(/Found (\d+) results\.tsv row\(s\)/),
  }
}

function activeJobsForKey(ctx, getJobs, key) {
  const jm = (typeof getJobs === 'function' && getJobs()) || ctx.jobManager
  if (!jm || !jm.jobs) return 0
  let n = 0
  for (const j of [...jm.jobs.values(), ...(jm.history ? jm.history.values() : [])]) {
    if (j.status !== 'running' && j.status !== 'queued') continue
    const a = j.args || {}
    if (j.toolId === 'agent') {
      if (a.candidate === key) n++
    } else if (CASCADE_TOOLS.has(j.toolId) && a.profile === key) {
      n++
    }
  }
  return n
}

function applyProfileEdit(data, body) {
  const { old_model, new_model } = body
  if (!old_model) return { error: 'old_model is required' }
  const profiles = data.profiles || {}
  const registered = data.models || {}
  // A registered-but-variant-less model can still be edited: its dialog lets the
  // user tick cells and re-create variants via the create list below.
  const knownModel = registered[old_model] !== undefined
  const modelKeys = Object.keys(profiles).filter(k => profiles[k] && profiles[k].chapter_stage && profiles[k].chapter_stage.model === old_model)
  if (modelKeys.length === 0 && !knownModel) return { error: `no profiles found for model ${old_model}` }

  const target = new_model && new_model !== old_model ? new_model : old_model
  const hasRoute = 'provider_route' in body
  const edits = body.edits || []
  const allEdits = {}
  for (const e of edits) {
    if (!e || !e.key || !modelKeys.includes(e.key)) {
      return { error: `unknown profile in edits: ${e && e.key}` }
    }
    allEdits[e.key] = e
  }

  const newProfiles = {}
  const removed = []
  const renamed = []
  const updated = []
  const superseded = []

  for (const oldKey of modelKeys) {
    const edit = allEdits[oldKey] || { keep: true }
    const spec = profiles[oldKey]
    if (edit.keep === false) {
      removed.push(oldKey)
      continue
    }
    const tb = (edit.time_budget && ['30m', '60m'].includes(edit.time_budget)) ? edit.time_budget : timeBudgetOf(oldKey)
    const effort = edit.effort && EFFORT_ORDER.includes(edit.effort) ? edit.effort : effortOf(oldKey)
    const finalKey = keyFor(tb, slugOfModel(target), effort)

    if (finalKey !== oldKey) {
      if (newProfiles[finalKey]) return { error: `would create duplicate profile key ${finalKey}` }
      if (profiles[finalKey] !== undefined && !modelKeys.includes(finalKey)) {
        return { error: `would overwrite existing profile ${finalKey}` }
      }
    }

    const nSpec = JSON.parse(JSON.stringify(spec))
    nSpec.chapter_stage.model = target
    nSpec.composer_stage.model = target
    if (effort !== effortOf(oldKey)) applyEffortConfig(nSpec, effort, specStyle(spec))

    if (typeof edit.temperature === 'number' && isFinite(edit.temperature)) {
      nSpec.chapter_stage.temperature = edit.temperature
      nSpec.composer_stage.temperature = edit.temperature
    }
    if (typeof edit.max_tokens === 'number' && isFinite(edit.max_tokens)) {
      nSpec.chapter_stage.max_tokens = edit.max_tokens
      nSpec.composer_stage.max_tokens = edit.max_tokens
    }
    if (hasRoute) {
      nSpec.chapter_stage.provider = body.provider_route
      nSpec.composer_stage.provider = body.provider_route
    }

    const nameMatch = String(spec.name || '').match(/_v(\d+)$/)
    const vNum = nameMatch ? nameMatch[1] : '1'
    nSpec.profile = finalKey
    nSpec.name = `${finalKey}_v${vNum}`

    newProfiles[finalKey] = nSpec
    if (finalKey !== oldKey) {
      renamed.push(`${oldKey} -> ${finalKey}`)
      superseded.push(oldKey)
    } else {
      updated.push(finalKey)
    }
  }

  const outProfiles = {}
  for (const [k, v] of Object.entries(profiles)) {
    if (newProfiles[k] !== undefined) continue
    if (removed.includes(k) || superseded.includes(k)) continue
    outProfiles[k] = v
  }
  for (const [k, v] of Object.entries(newProfiles)) outProfiles[k] = v

  // New variants requested for this model (checkbox turned on for an absent effort x budget cell)
  const createdVariants = []
  const createList = Array.isArray(body.create) ? body.create : []
  for (const c of createList) {
    if (!c || !['30m', '60m'].includes(c.time_budget)) return { error: 'create variant must specify a valid time_budget' }
    const effort = c.effort && EFFORT_ORDER.includes(c.effort) ? c.effort : 'none'
    const finalKey = keyFor(c.time_budget, slugOfModel(target), effort)
    if (newProfiles[finalKey] !== undefined || outProfiles[finalKey] !== undefined) {
      return { error: `variant already exists: ${finalKey}` }
    }
    let template = modelKeys.map(k => outProfiles[k]).find(p => p && effortOf(p.profile) === effort)
    if (!template) template = modelKeys.map(k => outProfiles[k]).find(p => p)
    const templateStyle = template ? specStyle(template) : 'new'
    const appliedEffort = effort
    const nSpec = template
      ? JSON.parse(JSON.stringify(template))
      : buildDefaultSpec(target, effort === 'thinking', data._defaultSchemaOk !== false)
    nSpec.chapter_stage.model = target
    nSpec.composer_stage.model = target
    applyEffortConfig(nSpec, appliedEffort, templateStyle)
    if (!template && effort !== 'thinking' && effort !== 'none') {
      nSpec.chapter_stage.max_tokens = effortDefaultMaxTokens(effort)
      nSpec.composer_stage.max_tokens = effortDefaultMaxTokens(effort)
    }
    if (typeof c.temperature === 'number' && isFinite(c.temperature)) {
      nSpec.chapter_stage.temperature = c.temperature
      nSpec.composer_stage.temperature = c.temperature
    }
    if (typeof c.max_tokens === 'number' && isFinite(c.max_tokens)) {
      nSpec.chapter_stage.max_tokens = c.max_tokens
      nSpec.composer_stage.max_tokens = c.max_tokens
    }
    if (hasRoute) {
      nSpec.chapter_stage.provider = body.provider_route
      nSpec.composer_stage.provider = body.provider_route
    }
    nSpec.profile = finalKey
    nSpec.name = `${finalKey}_v1`
    outProfiles[finalKey] = nSpec
    createdVariants.push(finalKey)
  }

  // Keep the model in the registry whenever it exists (was added or still has
  // profiles) so a model whose last variant is removed does not vanish from the
  // list. Follow a rename to the target name.
  const outModels = { ...(data.models || {}) }
  if (modelKeys.length > 0 || knownModel) {
    delete outModels[old_model]
    const priorRoute = knownModel ? (registered[old_model] && registered[old_model].provider_route) || null : null
    outModels[target] = {
      ...(outModels[target] || {}),
      provider_route: priorRoute || (outModels[target] && outModels[target].provider_route) || null,
      updated_at: new Date().toISOString(),
    }
    if (!outModels[target].added_at) outModels[target].added_at = outModels[target].updated_at
  }

  return {
    data: { ...data, profiles: outProfiles, models: outModels },
    plan: { removed, renamed, updated, created: createdVariants, model: old_model !== target ? `${old_model} -> ${target}` : null },
  }
}

async function handleModelsApi(req, res, ctx, getJobs = () => null) {
  const url = stripQuery(req.url)

  if (req.method === 'GET' && url === '/api/models') {
    try {
      const models = buildModelsIndex(ctx)
      sendJson(res, 200, { ok: true, count: models.length, models })
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  if (req.method === 'POST' && url === '/api/models/probe') {
    let body
    try {
      body = await readBody(req)
    } catch (e) {
      sendJson(res, 400, { ok: false, error: e.message })
      return true
    }
    const model = String(body.model || '').trim()
    if (!model) {
      sendJson(res, 400, { ok: false, error: 'model is required' })
      return true
    }
    const tb = Array.isArray(body.time_budget) && body.time_budget.length
      ? body.time_budget.filter(t => ['30m', '60m'].includes(t))
      : ['30m', '60m']
    const args = ['tools/add_candidate.py', '--model-full', model, '--dry-run', '--out', 'data/candidates.json']
    args.push('--time-budget', ...tb)
    try {
      const r = await ctx.pythonRunner(args)
      const parsed = parseAddOutput(r.stdout)
      if (r.ok) {
        sendJson(res, 200, { ok: true, model, probe: parsed.probe, created: parsed.created, compatible: parsed.created.length > 0 })
      } else {
        sendJson(res, 200, {
          ok: false,
          model,
          probe: parsed.probe,
          created: parsed.created,
          compatible: parsed.created.length > 0,
          error: (r.stderr || r.message || 'probe failed').trim().split('\n').slice(-3).join(' '),
        })
      }
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  if (req.method === 'POST' && url === '/api/models') {
    let body
    try {
      body = await readBody(req)
    } catch (e) {
      sendJson(res, 400, { ok: false, error: e.message })
      return true
    }
    const model = String(body.model || '').trim()
    if (!model) {
      sendJson(res, 400, { ok: false, error: 'model is required' })
      return true
    }
    const tb = Array.isArray(body.time_budget) && body.time_budget.length
      ? body.time_budget.filter(t => ['30m', '60m'].includes(t))
      : ['30m', '60m']
    const args = ['tools/add_candidate.py', '--model-full', model]
    args.push('--time-budget', ...tb)
    args.push('--out', 'data/candidates.json')
    try {
      const r = await ctx.pythonRunner(args, 'n\n')
      if (!r.ok) {
        sendJson(res, 500, { ok: false, error: (r.stderr || r.message).trim() })
        return true
      }
      const added = []
      const skipped = []
      for (const line of r.stdout.split('\n')) {
        const am = line.match(/^\s*Added: (\S+)/)
        if (am) added.push(am[1])
        const sm = line.match(/^\s*Skipped \(already exists\): (\S+)/)
        if (sm) skipped.push(sm[1])
      }
      if (added.length > 0) {
        await regenerateSpecPy(ctx.pythonRunner)
      }
      // Register the model on disk so it keeps appearing in the list even after
      // all of its variants are later removed.
      const regData = readCandidates(ctx.candidatesPath)
      if (regData && typeof regData === 'object') {
        const regModels = regData.models || (regData.models = {})
        if (!regModels[model]) {
          regModels[model] = {
            added_at: new Date().toISOString(),
            provider_route: null,
          }
          writeCandidates(ctx.candidatesPath, regData)
        }
      }
      sendJson(res, 200, { ok: true, added, skipped, model })
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  if (req.method === 'PUT' && url === '/api/models') {
    let body
    try {
      body = await readBody(req)
    } catch (e) {
      sendJson(res, 400, { ok: false, error: e.message })
      return true
    }
    try {
      const data = readCandidates(ctx.candidatesPath)
      const result = applyProfileEdit(data, body)
      if (result.error) {
        sendJson(res, 400, { ok: false, error: result.error })
        return true
      }
      const removedKeys = result.plan.removed || []

      // Confirmation gate: a keep:false edit without confirm mutates nothing.
      if (removedKeys.length > 0 && body.confirm !== true) {
        const impact = []
        let anyActive = false
        for (const key of removedKeys) {
          const r = await ctx.pythonRunner(cascadeCliArgs(key, true))
          if (!r.ok) {
            sendJson(res, 500, { ok: false, error: (r.stderr || r.message || 'preflight failed').trim() })
            return true
          }
          const c = parseCascadeOutput(r.stdout)
          const activeJobs = activeJobsForKey(ctx, getJobs, key)
          if (activeJobs > 0) anyActive = true
          impact.push({ key, runFiles: c.runFiles, logs: c.logs, resultRows: c.resultRows, activeJobs })
        }
        sendJson(res, 409, {
          ok: false,
          code: anyActive ? 'active_jobs' : 'confirmation_required',
          impact,
        })
        return true
      }

      // Confirmed deletion: variant keys with an active job are kept, not removed.
      let skippedActive = []
      if (removedKeys.length > 0 && body.confirm === true) {
        skippedActive = removedKeys.filter(k => activeJobsForKey(ctx, getJobs, k) > 0)
        if (skippedActive.length > 0) {
          body.edits = (body.edits || []).filter(e => !skippedActive.includes(e.key))
        }
      }

      const confirmed = applyProfileEdit(data, body)
      if (confirmed.error) {
        sendJson(res, 400, { ok: false, error: confirmed.error })
        return true
      }
      writeCandidates(ctx.candidatesPath, confirmed.data)
      await regenerateSpecPy(ctx.pythonRunner)

      let removedRuns = 0
      let removedLogs = 0
      let removedResultRows = 0
      for (const key of confirmed.plan.removed || []) {
        const r = await ctx.pythonRunner(cascadeCliArgs(key))
        if (!r.ok) {
          sendJson(res, 500, { ok: false, error: (r.stderr || r.message || 'cascade failed').trim() })
          return true
        }
        const c = parseCascadeOutput(r.stdout)
        removedRuns += c.runFiles
        removedLogs += c.logs
        removedResultRows += c.resultRows
      }

      const plan = {
        ...confirmed.plan,
        removedRuns,
        removedLogs,
        removedResultRows,
      }
      if (skippedActive.length > 0) plan.skippedActive = skippedActive
      sendJson(res, 200, { ok: true, plan })
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  if (req.method === 'DELETE' && url === '/api/models') {
    let body
    try {
      body = await readBody(req)
    } catch (e) {
      sendJson(res, 400, { ok: false, error: e.message })
      return true
    }
    const model = String(body.model || '').trim()
    if (!model) {
      sendJson(res, 400, { ok: false, error: 'model is required' })
      return true
    }
    const slug = slugOfModel(model)
    const pattern = escapeRegExp(slug)
    try {
      const r = await ctx.pythonRunner(['tools/add_candidate.py', '--remove', pattern, '--out', 'data/candidates.json'])
      if (!r.ok) {
        sendJson(res, 500, { ok: false, error: (r.stderr || r.message).trim() })
        return true
      }
      const removedProfiles = []
      for (const line of r.stdout.split('\n')) {
        const pm = line.match(/^  - (\S+)/)
        if (pm) removedProfiles.push(pm[1])
      }
      const rm = r.stdout.match(/Removed (\d+) run file\(s\)/)
      const lm = r.stdout.match(/Removed (\d+) job log\(s\)/)
      const tm = r.stdout.match(/Removed (\d+) results\.tsv row\(s\)/)
      // Full delete also unregisters the model so it no longer lists as empty.
      const regData = readCandidates(ctx.candidatesPath)
      if (regData && typeof regData === 'object' && regData.models && regData.models[model]) {
        delete regData.models[model]
        writeCandidates(ctx.candidatesPath, regData)
      }
      sendJson(res, 200, {
        ok: true,
        model,
        pattern,
        removedProfiles,
        removedRuns: rm ? parseInt(rm[1], 10) : 0,
        removedLogs: lm ? parseInt(lm[1], 10) : 0,
        removedResultRows: tm ? parseInt(tm[1], 10) : 0,
      })
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  return false
}

// ── Python script runner (dashboard jobs) ─────────────────────

const MODEL_PATTERN = '^[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+$'

function arg(name, label, o = {}) {
  return { name, label, type: 'text', required: false, ...o }
}

const JOB_LIMITS = {
  maxQueue: 10,
  logCapBytes: 20 * 1024 * 1024,
  sseBufferCap: 10000,
  sseKeepaliveMs: 15000,
  cancelGraceMs: 10000,
  logPruneDays: 30,
  historyDropDays: 7,
  rateLimitPerHour: 20,
  bodyMaxBytes: 64 * 1024,
  stringMaxBytes: 2000,
  notesMax: 500,
}

const SCRIPT_REGISTRY = [
  {
    id: 'build_rubrics',
    group: 'corpus',
    title: 'Build rubrics',
    description: 'Build frozen source-derived rubrics for every book and chapter under artifacts/.',
    script: 'tools/build_rubrics.py',
    runtimeClass: 'write',
    outputs: ['artifacts/book_rubrics', 'artifacts/rubrics'],
    args: [
      arg('books-root', 'Books root', { type: 'path', default: 'data/books', hint: 'Corpus directory containing book folders.' }),
      arg('artifacts-root', 'Artifacts root', { type: 'path', default: 'artifacts', hint: 'Where rubric JSON files are written.' }),
    ],
    presets: [
      { id: 'default', label: 'Default roots', args: { 'books-root': 'data/books', 'artifacts-root': 'artifacts' } },
    ],
  },
  {
    id: 'build_bench',
    group: 'corpus',
    title: 'Build benchmark splits',
    description: 'Build benchmark splits (development/gate/holdout) from a book corpus.',
    script: 'tools/build_bench.py',
    runtimeClass: 'write',
    outputs: ['bench/*.jsonl', 'bench/splits.json'],
    args: [
      arg('books-root', 'Books root', { type: 'path', default: 'data/books' }),
      arg('bench-dir', 'Bench directory', { type: 'path', default: 'bench' }),
      arg('dev-books', 'Development books', { type: 'int', default: 10, min: 1, max: 1000 }),
      arg('gate-books', 'Gate books', { type: 'int', default: 4, min: 0, max: 1000, advanced: true }),
      arg('holdout-books', 'Holdout books', { type: 'int', default: 4, min: 0, max: 1000, advanced: true }),
      arg('chapters-per-dev-book', 'Chapters per dev book', { type: 'int', default: 4, min: 1, max: 100, advanced: true }),
      arg('seed', 'Random seed', { type: 'int', default: 42, advanced: true }),
      arg('split-mode', 'Split mode', { type: 'enum', default: 'balanced_genre', choices: ['balanced_genre', 'random'], advanced: true }),
      arg('stratify-field', 'Stratify field', { type: 'text', default: 'genre_macro', pattern: '^[A-Za-z_]+$', advanced: true }),
    ],
    presets: [
      { id: 'default', label: 'Default (10/4/4)', args: { 'dev-books': 10, 'gate-books': 4, 'holdout-books': 4, seed: 42 } },
      { id: 'smoke', label: 'Smoke (1/1/1)', args: { 'dev-books': 1, 'gate-books': 1, 'holdout-books': 1, seed: 42 } },
    ],
  },
  {
    id: 'corpus_report',
    group: 'corpus',
    title: 'Corpus report',
    description: 'Audit the corpus composition for genre-aware benchmarking.',
    script: 'tools/corpus_report.py',
    runtimeClass: 'instant',
    outputs: [],
    args: [
      arg('books-root', 'Books root', { type: 'path', default: 'data/books' }),
    ],
  },
  {
    id: 'add_candidate',
    group: 'candidates',
    title: 'Add candidate model',
    description: 'Auto-probe a model and append candidate profile(s) to data/candidates.json.',
    script: 'tools/add_candidate.py',
    runtimeClass: 'write',
    outputs: ['data/candidates.json'],
    args: [
      arg('model-full', 'Model ID', { type: 'text', required: true, pattern: '^[a-z0-9-]+/[a-z0-9.\\-]+$', placeholder: 'deepseek/deepseek-v4-flash' }),
      arg('time-budget', 'Time budgets', { type: 'enum', multiple: true, default: ['30m', '60m'], choices: ['30m', '60m'] }),
      arg('dry-run', 'Dry run (probe only, no writes)', { type: 'bool', default: false }),
      arg('provider-route', 'Provider route (JSON)', { type: 'json', advanced: true, hint: '{"order":["deepseek"]} — keys limited to only/order/allow/avoid.' }),
    ],
    presets: [
      { id: 'both', label: '30m + 60m', args: { 'time-budget': ['30m', '60m'] } },
      { id: '30m', label: '30m only', args: { 'time-budget': ['30m'] } },
      { id: '60m', label: '60m only', args: { 'time-budget': ['60m'] } },
    ],
  },
  {
    id: 'gen_profile_literal',
    group: 'candidates',
    title: 'Regenerate candidate_spec.py',
    description: 'Regenerate the Profile literal + PROFILE_CANDIDATES in candidate_spec.py from data/candidates.json.',
    script: 'tools/gen_profile_literal.py',
    runtimeClass: 'write',
    outputs: ['candidate_spec.py'],
    args: [],
  },
  {
    id: 'snapshot_catalog',
    group: 'candidates',
    title: 'Snapshot OpenRouter catalog',
    description: 'Snapshot the current OpenRouter model catalog and derived pricing table.',
    script: 'tools/snapshot_catalog.py',
    runtimeClass: 'instant',
    outputs: ['snapshots/catalog/*.json', 'snapshots/pricing/*.json'],
    args: [
      arg('api-key-env', 'API key env var', { type: 'text', fixed: true, default: 'OPENROUTER_API_KEY', ui: false }),
    ],
  },
  {
    id: 'run_candidate',
    group: 'run',
    title: 'Run candidate on a benchmark',
    description: 'Run the frozen benchmark harness against a candidate profile (chapter fast, gate, or holdout).',
    script: 'core/run_candidate.py',
    runtimeClass: 'llm',
    outputs: ['runs/<bench>/<run_id>/*', 'results.tsv (when --write-results)'],
    args: [
      arg('bench', 'Benchmark', { type: 'text', required: true, bench: true, hint: 'Name from bench/ (e.g. chapter_fast) or a bench/*.jsonl path.' }),
      arg('profile', 'Profile', { type: 'text', required: true, pattern: '^[A-Za-z0-9_.\\-]+$', default: 'all', hint: "Profile key, or 'all' with a --time filter." }),
      arg('time', 'Time budget', { type: 'enum', default: 'all', choices: ['all', '30m', '60m'] }),
      arg('judge-model', 'Judge model', { type: 'text', pattern: MODEL_PATTERN, toggle: true, default: 'openai/gpt-5.4-mini', placeholder: 'openai/gpt-5.4-mini', hint: 'When enabled the LLM judge scores the run. Uncheck to score deterministically without calling a judge model.' }),
      arg('mock', 'Mock (no API calls)', { type: 'bool', default: false }),
      arg('write-results', 'Write results.tsv', { type: 'bool', default: false }),
      arg('max-samples', 'Max samples (0 = all)', { type: 'int', default: 0, min: 0, advanced: true }),
      arg('run-id', 'Explicit run id', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$', advanced: true }),
      arg('resume', 'Resume run id', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$', advanced: true }),
      arg('wait-for-credits', 'Wait for credits on 402', { type: 'bool', default: false, advanced: true }),
      arg('notes', 'Notes', { type: 'text', advanced: true, max: 500 }),
    ],
    presets: [
      { id: 'smoke', label: 'Smoke (mock)', args: { bench: 'chapter_fast', profile: 'all', time: 'all', mock: true, 'write-results': true, 'max-samples': 4 } },
      { id: '30m-all', label: '30m all profiles', args: { bench: 'chapter_fast', profile: 'all', time: '30m', 'write-results': true } },
      { id: '60m-all', label: '60m all profiles', args: { bench: 'chapter_fast', profile: 'all', time: '60m', 'write-results': true } },
    ],
  },
  {
    id: 'judge_existing',
    group: 'run',
    title: 'Re-judge existing runs (LLM)',
    description: 'Re-run an LLM judge on existing runs, writing separate .llmj files without modifying originals.',
    script: 'core/judge_existing.py',
    runtimeClass: 'llm',
    outputs: ['runs/<bench>/*__llmj_*'],
    args: [
      arg('bench', 'Benchmark', { type: 'text', required: true, bench: true }),
      arg('judge-model', 'Judge model', { type: 'text', required: true, pattern: MODEL_PATTERN, default: 'openai/gpt-5.4-mini', placeholder: 'openai/gpt-5.4-mini' }),
      arg('profile', 'Profile filter', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$' }),
      arg('run-id', 'Run id', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$', advanced: true }),
      arg('max-samples', 'Max samples (0 = all)', { type: 'int', default: 0, min: 0, advanced: true }),
      arg('force-overwrite', 'Force overwrite', { type: 'bool', default: false, advanced: true }),
      arg('dry-run', 'Dry run (enumerate only)', { type: 'bool', default: false, advanced: true }),
    ],
  },
  {
    id: 'agent',
    group: 'run',
    title: 'Autoresearch agent',
    description: 'Optimize prompt components from human notes: read notes, generate variants, run benchmarks, write a report.',
    script: 'autoresearch/agent.py',
    runtimeClass: 'llm',
    outputs: ['candidate_spec.py edits'],
    args: [
      arg('model', 'Base model', { type: 'text', pattern: '^[A-Za-z0-9_.\\-/]*$' }),
      arg('budget', 'Time budget', { type: 'enum', choices: ['30m', '60m'] }),
      arg('thinking', 'Thinking mode', { type: 'enum', choices: ['thinking', 'notthinking'], advanced: true }),
      arg('candidate', 'Candidate hint', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$' }),
      arg('mode', 'Search mode', { type: 'enum', default: 'auto', choices: ['hill_climb', 'grid_search', 'auto'] }),
      arg('max-iter', 'Max iterations', { type: 'int', default: 5, min: 1, max: 50, advanced: true }),
      arg('max-variants', 'Max variants', { type: 'int', default: 12, min: 1, max: 50, advanced: true }),
      arg('stage', 'Pipeline stage', { type: 'enum', default: 'chapter', choices: ['chapter', 'composer'], advanced: true }),
      arg('dry-run', 'Dry run', { type: 'bool', default: false }),
    ],
  },
  {
    id: 'leaderboard',
    group: 'maintenance',
    title: 'Leaderboard',
    description: 'Show overall and slice-based leaderboards from results.tsv and run artifacts.',
    script: 'tools/leaderboard.py',
    runtimeClass: 'instant',
    outputs: [],
    args: [
      arg('bench', 'Bench filter', { type: 'text', bench: true }),
      arg('profile', 'Profile filter', { type: 'text', pattern: '^[A-Za-z0-9_.\\-]*$' }),
      arg('model-contains', 'Model contains', { type: 'text' }),
      arg('sort-by', 'Sort by', { type: 'enum', default: 'mean_utility', choices: ['mean_utility', 'mean_quality', 'mean_faithfulness', 'mean_concept_coverage', 'mean_generation_cost', 'hard_fail_rate', 'mean_passes_used'] }),
      arg('top', 'Top N', { type: 'int', default: 10, min: 1, max: 100 }),
      arg('slice-field', 'Slice field', { type: 'enum', advanced: true, choices: ['genre_macro', 'none'] }),
      arg('slice-value', 'Slice value', { type: 'text', advanced: true }),
    ],
  },
  {
    id: 'reset_benchmark',
    group: 'maintenance',
    title: 'Reset benchmark',
    description: 'Clear runs, results, candidates, snapshots and PROFILE_CANDIDATES to start fresh. Irreversible.',
    script: 'reset_benchmark.py',
    runtimeClass: 'write',
    destructive: true,
    confirmPhrase: 'RESET',
    stdin: 'y\n',
    outputs: [],
    args: [],
  },
]

const GROUP_LABELS = {
  corpus: 'Corpus validation',
  candidates: 'Candidates',
  run: 'Run harness',
  maintenance: 'Analysis & maintenance',
}

function getScriptRegistry() {
  return JSON.parse(JSON.stringify(SCRIPT_REGISTRY))
}

function stableStringify(v) {
  const sort = (o) => {
    if (Array.isArray(o)) return o.map(sort)
    if (o && typeof o === 'object') {
      return Object.fromEntries(Object.keys(o).sort().map(k => [k, sort(o[k])]))
    }
    return o
  }
  return JSON.stringify(sort(v))
}

const SECRET_LINE_RE = /(sk-[A-Za-z0-9]{16,}|(?:OPENROUTER_API_KEY|OPENROUTER_MANAGEMENT_KEY|GOOGLE_BOOKS_API_KEY)=[^\s]+)/g

function scrubSecrets(text) {
  text = String(text)
  if (!text) return text
  if (!SECRET_LINE_RE.test(text)) return text
  SECRET_LINE_RE.lastIndex = 0
  return text.replace(SECRET_LINE_RE, (m) =>
    m.includes('=') ? `${m.split('=')[0]}=[REDACTED]` : '[REDACTED]',
  )
}

function resolveArgValue(value, isBench) {
  const s = String(value)
  if (isBench) {
    if (/^[A-Za-z0-9_.\-]+$/.test(s)) return { ok: true, value: s }
    const parts = s.split('/').filter(Boolean)
    if (parts.some(p => p === '..' || p === '.' || p.includes('\\'))) {
      return { ok: false, error: 'bench path must not contain . or .. or backslashes' }
    }
    if (parts[0] === 'bench' && parts.length >= 2 && /\.jsonl?$/.test(parts[parts.length - 1])) {
      return { ok: true, value: s }
    }
    return { ok: false, error: 'bench must be a bare name or a bench/*.jsonl path (no escapes)' }
  }
  if (s.startsWith('/')) return { ok: false, error: 'absolute paths are not allowed' }
  const parts = s.split('/')
  if (parts.some(p => p === '..' || p.includes('\\'))) {
    return { ok: false, error: 'path must stay inside the repo (no ..)' }
  }
  return { ok: true, value: s }
}

function validateProviderRoute(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false
  const allowed = new Set(['only', 'order', 'allow', 'avoid'])
  const keys = Object.keys(v)
  if (keys.length === 0) return true
  if (!keys.every(k => allowed.has(k))) return false
  for (const k of keys) {
    const list = v[k]
    if (!Array.isArray(list)) return false
    if (!list.every(x => typeof x === 'string' && x.length > 0)) return false
  }
  return true
}

function validateJobArgs(tool, rawArgs) {
  const errors = {}
  const argMap = new Map(tool.args.map(a => [a.name, a]))
  for (const k of Object.keys(rawArgs || {})) {
    if (!argMap.has(k)) errors[k] = 'unknown argument'
  }
  const out = {}
  for (const a of tool.args) {
    if (a.fixed) {
      out[a.name] = a.default ?? ''
      continue
    }
    let v = rawArgs ? rawArgs[a.name] : undefined
    const present = v !== undefined && v !== null
    if (!present || v === '' || (Array.isArray(v) && v.length === 0)) {
      if (a.required && !(a.type === 'bool')) {
        if (!errors[a.name]) errors[a.name] = `${a.label} is required`
        continue
      }
      if (a.toggle) {
        out[a.name] = ''
        continue
      }
      out[a.name] = a.multiple ? (a.default ? [...a.default] : []) : (a.default ?? '')
      if (a.type === 'bool') out[a.name] = !!a.default
      continue
    }
    if (a.multiple) {
      const arr = Array.isArray(v) ? v : [v]
      if (a.type === 'enum') {
        for (const item of arr) {
          if (!a.choices.includes(String(item))) {
            errors[a.name] = `${a.label} must be one of ${a.choices.join(', ')}`
            break
          }
        }
      }
      if (!errors[a.name]) out[a.name] = arr.map(String)
      continue
    }
    switch (a.type) {
      case 'bool': {
        const b = v === true || v === 1 || v === '1' || v === 'true'
        if (v !== true && v !== false && v !== 1 && v !== 0 && v !== '1' && v !== '0' && v !== 'true' && v !== 'false') {
          errors[a.name] = `${a.label} must be a boolean`
          break
        }
        out[a.name] = b
        break
      }
      case 'int': {
        const n = Number(v)
        if (!Number.isInteger(n)) {
          errors[a.name] = `${a.label} must be an integer`
          break
        }
        if ((a.min !== undefined && n < a.min) || (a.max !== undefined && n > a.max)) {
          errors[a.name] = a.min !== undefined && a.max !== undefined
            ? `${a.label} must be between ${a.min} and ${a.max}`
            : a.min !== undefined ? `${a.label} must be at least ${a.min}` : `${a.label} must be at most ${a.max}`
          break
        }
        out[a.name] = n
        break
      }
      case 'enum': {
        if (!a.choices.includes(String(v))) {
          errors[a.name] = `${a.label} must be one of ${a.choices.join(', ')}`
          break
        }
        out[a.name] = String(v)
        break
      }
      case 'json': {
        let parsed
        try {
          parsed = typeof v === 'string' ? JSON.parse(v) : v
        } catch {
          errors[a.name] = `${a.label} is not valid JSON`
          break
        }
        if (!validateProviderRoute(parsed)) {
          errors[a.name] = 'route keys must be a subset of only/order/allow/avoid with string arrays'
          break
        }
        out[a.name] = parsed
        break
      }
      case 'path': {
        const r = resolveArgValue(v, !!a.bench)
        if (!r.ok) {
          errors[a.name] = r.error
          break
        }
        out[a.name] = v
        break
      }
      default: {
        if (a.bench) {
          const r = resolveArgValue(v, true)
          if (!r.ok) {
            errors[a.name] = r.error
            break
          }
        }
        const s = String(v)
        if (s.length > JOB_LIMITS.stringMaxBytes) {
          errors[a.name] = `${a.label} is too long`
          break
        }
        if (a.max !== undefined && s.length > a.max) {
          errors[a.name] = `${a.label} must be at most ${a.max} characters`
          break
        }
        if (a.pattern && !new RegExp(a.pattern).test(s)) {
          errors[a.name] = `${a.label} has invalid characters`
          break
        }
        out[a.name] = s
      }
    }
  }
  return { ok: Object.keys(errors).length === 0, args: out, fieldErrors: errors }
}

function buildArgv(tool, args) {
  const argv = [tool.script]
  for (const a of tool.args) {
    const val = args[a.name]
    if (val === undefined || val === null || val === '') continue
    if (a.type === 'bool') {
      if (val) argv.push(`--${a.name}`)
      continue
    }
    if (a.multiple) {
      const arr = Array.isArray(val) ? val : [val]
      if (arr.length) argv.push(`--${a.name}`, ...arr.map(String))
      continue
    }
    if (a.type === 'json') {
      argv.push(`--${a.name}`, JSON.stringify(val))
      continue
    }
    argv.push(`--${a.name}`, String(val))
  }
  return argv
}

function summarize(j) {
  const out = {
    id: j.id,
    toolId: j.toolId,
    status: j.status,
    exitCode: j.exitCode,
    createdAt: j.createdAt,
    startedAt: j.startedAt,
    finishedAt: j.finishedAt,
    error: j.error || undefined,
    resultHints: j.resultHints || {},
  }
  return out
}

const liveChildren = new Set()

function killAllChildren() {
  for (const child of [...liveChildren]) {
    try {
      if (child && child.kill) child.kill('SIGTERM')
    } catch { /* ignore */ }
  }
  const hard = setTimeout(() => {
    for (const child of [...liveChildren]) {
      try {
        if (child && child.kill) child.kill('SIGKILL')
      } catch { /* ignore */ }
    }
  }, JOB_LIMITS.cancelGraceMs)
  if (hard.unref) hard.unref()
}

process.once('SIGINT', killAllChildren)
process.once('SIGTERM', killAllChildren)

function createJobManager(opts = {}) {
  const repoRoot = opts.repoRoot || REPO_ROOT
  const jobsDir = opts.jobsDir || join(REPO_ROOT, 'artifacts', 'jobs')
  const registry = opts.scriptRegistry || getScriptRegistry()
  const limits = opts.jobLimits || JOB_LIMITS
  const verifyScripts = opts.verifyScripts !== false
  const spawnFn = opts.spawn || ((argv, o) => spawn(PYTHON, argv, o))
  const pythonRunner = opts.pythonRunner || ((args) => runPythonAsync(args, undefined, repoRoot))
  const registryById = new Map(registry.map(t => [t.id, t]))

  const jobs = new Map()
  const history = new Map()
  const writableQueue = []
  let activeWritable = 0
  let recentCreations = []

  mkdirSync(jobsDir, { recursive: true })
  restoreHistory()
  pruneLogs()

  function pruneLogs() {
    const cutoff = Date.now() - limits.logPruneDays * 864e5
    let files = []
    try {
      files = readdirSync(jobsDir).filter(f => f.endsWith('.log'))
    } catch {
      return
    }
    for (const f of files) {
      try {
        const st = statSync(join(jobsDir, f))
        if (st.mtimeMs < cutoff) unlinkSync(join(jobsDir, f))
      } catch { /* ignore */ }
    }
  }

  function restoreHistory() {
    const cutoff = Date.now() - limits.historyDropDays * 864e5
    let files = []
    try {
      files = readdirSync(jobsDir).filter(f => f.endsWith('.log'))
    } catch {
      return
    }
    for (const f of files) {
      let content
      try {
        content = readFileSync(join(jobsDir, f), 'utf-8')
      } catch {
        continue
      }
      if (content.includes('[job] finished')) continue
      const metaMatch = content.match(/\[job\] meta: (\{[\s\S]*?\})/)
      if (!metaMatch) continue
      let meta
      try {
        meta = JSON.parse(metaMatch[1])
      } catch {
        continue
      }
      if (!meta || typeof meta.jobId !== 'string') continue
      const createdAt = meta.createdAt || null
      if (createdAt && new Date(createdAt).getTime() < cutoff) continue
      history.set(meta.jobId, {
        id: meta.jobId,
        toolId: meta.toolId,
        args: {},
        status: 'interrupted',
        exitCode: null,
        pid: null,
        createdAt,
        startedAt: meta.startedAt || null,
        finishedAt: meta.finishedAt || null,
        logPath: join(jobsDir, f),
        cancelRequested: false,
        error: 'interrupted by server restart — re-run from the dashboard',
        resultHints: {},
        _history: true,
      })
    }
  }

  function appendJobMeta(job, payload) {
    try {
      appendFileSync(job.logPath, `[job] ${payload.kind}: ${JSON.stringify(payload)}\n`)
    } catch { /* non-fatal */ }
  }

  function ensurePing(job) {
    if (job._subs.size > 0 && job._pingTimer == null) {
      job._pingTimer = setInterval(() => {
        for (const r of [...job._subs]) {
          if (!r.writableEnded && !r.destroyed) r.write(': ping\n\n')
        }
      }, limits.sseKeepaliveMs)
      if (job._pingTimer.unref) job._pingTimer.unref()
    }
  }

  function emit(job, type, data) {
    const ev = { seq: job._seq++, type, data }
    job._events.push(ev)
    while (job._events.length > limits.sseBufferCap) job._events.shift()
    if (job._events.length > 0) job._dropped = job._events[0].seq
    const frame = `id: ${ev.seq}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`
    for (const r of [...job._subs]) {
      if (!r.writableEnded && !r.destroyed) r.write(frame)
    }
    ensurePing(job)
  }

  function closeSubs(job) {
    if (job._pingTimer) {
      clearInterval(job._pingTimer)
      job._pingTimer = null
    }
    for (const r of [...job._subs]) {
      if (!r.writableEnded) r.end()
    }
    job._subs.clear()
  }

  function appendToLog(job, line) {
    try {
      appendFileSync(job.logPath, `${line}\n`)
      const st = statSync(job.logPath)
      if (st.size > limits.logCapBytes) trimLog(job.logPath)
    } catch { /* non-fatal */ }
  }

  function trimLog(path) {
    try {
      const data = readFileSync(path)
      const start = data.indexOf(10, Math.floor(data.length / 2))
      if (start < 0) return
      writeFileSync(path, Buffer.concat([Buffer.from('[log truncated]\n'), data.slice(start + 1)]))
    } catch { /* non-fatal */ }
  }

  function addLogLine(job, stream, line) {
    const clean = scrubSecrets(line)
    if (stream === 'stdout') {
      job._stdoutTail.push(clean)
      if (job._stdoutTail.length > 1000) job._stdoutTail.shift()
    } else {
      job._stderrTail.push(clean)
      if (job._stderrTail.length > 500) job._stderrTail.shift()
    }
    job._batch[stream].push(clean)
    appendToLog(job, clean)
  }

  function scheduleBatch(job) {
    if (job._batchTimer) return
    if (job._batch.stdout.length === 0 && job._batch.stderr.length === 0) return
    job._batchTimer = setTimeout(() => {
      job._batchTimer = null
      flushBatch(job)
    }, 20)
  }

  function flushBatch(job) {
    for (const stream of ['stdout', 'stderr']) {
      if (job._batch[stream].length) {
        const text = job._batch[stream].join('\n')
        job._batch[stream] = []
        emit(job, 'log', { stream, text })
      }
    }
  }

  function flushPartial(job) {
    for (const stream of ['stdout', 'stderr']) {
      if (job._partial[stream]) {
        addLogLine(job, stream, job._partial[stream])
        job._partial[stream] = ''
      }
    }
    flushBatch(job)
  }

  function onData(job, stream, chunk) {
    const text = chunk.toString()
    job._partial[stream] += text
    const lines = job._partial[stream].split('\n')
    job._partial[stream] = lines.pop()
    for (const line of lines) addLogLine(job, stream, line)
    scheduleBatch(job)
  }

  function deriveResultHints(job) {
    const tool = job._tool
    if (!tool) return {}
    const hints = {}
    if (tool.id === 'run_candidate') {
      if (job.status === 'succeeded') {
        hints.bench = String(job.args.bench || '')
        hints.resultsTsvUpdated = !!job.args['write-results']
        const m = job._stdoutTail.join('\n').match(/Run ID: (\S+)/)
        if (m) hints.runId = m[1]
      }
    } else if (tool.id === 'gen_profile_literal' || tool.id === 'agent') {
      hints.specPyChanged = job.status === 'succeeded'
    } else if (tool.id === 'snapshot_catalog' && job.status === 'succeeded') {
      try {
        hints.snapshotsCreated = readdirSync(join(repoRoot, 'snapshots', 'catalog'))
          .map(f => `snapshots/catalog/${f}`)
          .sort()
          .slice(-10)
      } catch {
        hints.snapshotsCreated = []
      }
    }
    return hints
  }

  function finalize(job, status, opts = {}) {
    job.status = status
    job.finishedAt = new Date().toISOString()
    job.resultHints = deriveResultHints(job)
    appendJobMeta(job, { kind: 'finished', status, exitCode: job.exitCode, canceled: job.cancelRequested })
    if (!opts.deferEmit) {
      emit(job, 'status', {
        status,
        exitCode: job.exitCode,
        error: job.error || undefined,
        resultHints: job.resultHints,
      })
      closeSubs(job)
    }
  }

  function failJob(job, error) {
    job.status = 'failed'
    job.exitCode = null
    job.error = error
    finalize(job, 'failed')
    if (job._writable) {
      activeWritable = Math.max(0, activeWritable - 1)
      maybeStart()
    }
  }

  function onClose(job, code, signal) {
    if (job._batchTimer) {
      clearTimeout(job._batchTimer)
      job._batchTimer = null
    }
    if (job._child) liveChildren.delete(job._child)
    flushPartial(job)
    job.exitCode = code == null ? null : code
    const killed = job._killed !== null || job._killFlag || (code == null && signal != null && job.cancelRequested)
    let status
    if (killed || job.cancelRequested) status = 'canceled'
    else status = code === 0 ? 'succeeded' : 'failed'
    if (status === 'canceled' && job.exitCode == null) job.error = 'killed'
    if (status === 'failed') {
      job.error = job._stderrTail.slice(-5).join(' ') || job._spawnError ||
        (code == null && signal ? `killed (${signal})` : `exit code ${code}`)
    }
    const regenWanted = job.toolId === 'add_candidate' && status === 'succeeded' && !job.args['dry-run'] &&
      job._tool && job._tool.runtimeClass !== 'instant'
    finalize(job, status, { deferEmit: regenWanted })
    if (regenWanted) {
      pythonRunner(['tools/gen_profile_literal.py']).then(() => {
        job.resultHints.specPyChanged = true
        emit(job, 'status', {
          status, exitCode: job.exitCode, error: job.error || undefined, resultHints: job.resultHints,
        })
        closeSubs(job)
      }).catch(() => {
        emit(job, 'status', {
          status, exitCode: job.exitCode, error: job.error || undefined, resultHints: job.resultHints,
        })
        closeSubs(job)
      })
    }
    if (job._writable) {
      activeWritable = Math.max(0, activeWritable - 1)
      maybeStart()
    }
  }

  function startJob(job) {
    const tool = job._tool
    const script = tool.script
    if (verifyScripts && !existsSync(join(repoRoot, script))) {
      failJob(job, `script not found: ${script}`)
      return
    }
    const argv = buildArgv(tool, job.args)
    let child
    try {
      child = spawnFn(argv, { cwd: repoRoot, env: { ...process.env, PYTHONUNBUFFERED: '1' } })
    } catch (e) {
      failJob(job, (e && e.message) || String(e))
      return
    }
    job._child = child
    job.pid = (child && child.pid) || null
    job.status = 'running'
    job.startedAt = new Date().toISOString()
    liveChildren.add(child)
    emit(job, 'start', { jobId: job.id, pid: job.pid })
    appendJobMeta(job, { kind: 'meta', jobId: job.id, toolId: tool.id, createdAt: job.createdAt, startedAt: job.startedAt })
    if (child.stdout) child.stdout.on('data', (c) => onData(job, 'stdout', c))
    if (child.stderr) child.stderr.on('data', (c) => onData(job, 'stderr', c))
    if (child.on) {
      child.on('error', (e) => { job._spawnError = (e && e.message) || String(e) })
      child.on('close', (code, signal) => onClose(job, code, signal))
    }
    try {
      if (child.stdin) {
        if (tool.stdin) child.stdin.end(tool.stdin)
        else child.stdin.end()
      }
    } catch { /* stdin already closed */ }
  }

  function maybeStart() {
    if (activeWritable > 0) return
    while (activeWritable === 0 && writableQueue.length > 0) {
      const job = writableQueue.shift()
      if (job.status === 'canceled') continue
      job._writable = true
      activeWritable = 1
      startJob(job)
      break
    }
  }

  function pruneCreations() {
    const cutoff = Date.now() - 3600 * 1000
    recentCreations = recentCreations.filter(t => t >= cutoff)
  }

  function create({ toolId, args, confirm }) {
    const tool = registryById.get(toolId)
    if (!tool) return { status: 404, body: { ok: false, error: `unknown tool: ${toolId}` } }
    if (verifyScripts && !existsSync(join(repoRoot, tool.script))) {
      const job = makeFailedJob(tool, args, `script not found: ${tool.script}`)
      return { status: 201, body: { ok: true, job: summarize(job), failedFast: true } }
    }
    const v = validateJobArgs(tool, args || {})
    if (!v.ok) return { status: 400, body: { ok: false, error: 'invalid arguments', fieldErrors: v.fieldErrors } }
    if (tool.destructive && confirm !== (tool.confirmPhrase || 'RESET')) {
      return { status: 400, body: { ok: false, error: 'confirmation required' } }
    }
    const norm = stableStringify(v.args)
    const dup = [...jobs.values()].find(j => j.toolId === toolId && j.status === 'queued' && stableStringify(j.args) === norm)
    if (dup) return { status: 200, body: { ok: true, job: summarize(dup), duplicate: true } }
    const running = [...jobs.values()].find(j => j.toolId === toolId && j.status === 'running' && stableStringify(j.args) === norm)
    if (running) return { status: 409, body: { ok: false, error: 'already running — identical args for this tool' } }
    if (tool.runtimeClass !== 'instant') {
      const queuedCount = [...jobs.values()].filter(j => j.status === 'queued').length
      if (queuedCount >= limits.maxQueue) return { status: 409, body: { ok: false, error: 'queue full' } }
    }
    pruneCreations()
    if (recentCreations.length >= limits.rateLimitPerHour) {
      return { status: 429, body: { ok: false, error: 'rate limit exceeded: too many jobs in the past hour' } }
    }
    recentCreations.push(Date.now())
    const id = randomUUID()
    const now = new Date().toISOString()
    const job = {
      id,
      toolId,
      args: v.args,
      status: 'queued',
      exitCode: null,
      pid: null,
      createdAt: now,
      startedAt: null,
      finishedAt: null,
      logPath: join(jobsDir, `${id}.log`),
      cancelRequested: false,
      error: undefined,
      resultHints: {},
      _history: false,
      _tool: tool,
      _child: null,
      _events: [],
      _seq: 0,
      _subs: new Set(),
      _dropped: 0,
      _partial: { stdout: '', stderr: '' },
      _batch: { stdout: [], stderr: [] },
      _batchTimer: null,
      _pingTimer: null,
      _killTimer: null,
      _stdoutTail: [],
      _stderrTail: [],
      _spawnError: '',
      _killed: null,
      _killFlag: false,
      _writable: tool.runtimeClass !== 'instant',
      ensurePing: () => ensurePing(job),
    }
    jobs.set(id, job)
    appendJobMeta(job, { kind: 'meta', jobId: id, toolId, createdAt: now })
    if (tool.runtimeClass === 'instant') startJob(job)
    else if (activeWritable === 0 && writableQueue.length === 0) {
      job._writable = true
      activeWritable = 1
      startJob(job)
    } else {
      writableQueue.push(job)
    }
    return { status: 201, body: { ok: true, job: summarize(job) } }
  }

  function makeFailedJob(tool, args, error) {
    const id = randomUUID()
    const now = new Date().toISOString()
    const v = validateJobArgs(tool, args || {})
    const job = {
      id,
      toolId: tool.id,
      args: v.ok ? v.args : (args || {}),
      status: 'failed',
      exitCode: null,
      pid: null,
      createdAt: now,
      startedAt: null,
      finishedAt: now,
      logPath: join(jobsDir, `${id}.log`),
      cancelRequested: false,
      error,
      resultHints: {},
      _history: false,
      _tool: tool,
      _events: [],
      _seq: 0,
      _subs: new Set(),
      _dropped: 0,
      _partial: { stdout: '', stderr: '' },
      _batch: { stdout: [], stderr: [] },
      _batchTimer: null,
      _pingTimer: null,
      _killTimer: null,
      _stdoutTail: [],
      _stderrTail: [],
      _writable: false,
    }
    jobs.set(id, job)
    appendJobMeta(job, { kind: 'meta', jobId: id, toolId: tool.id, createdAt: now })
    appendJobMeta(job, { kind: 'finished', status: 'failed', exitCode: null })
    return job
  }

  function list({ status, toolId, limit } = {}) {
    const cap = Math.min(Math.max(parseInt(limit, 10) || 50, 1), 200)
    const cutoff = Date.now() - limits.historyDropDays * 864e5
    const rows = [...jobs.values(), ...[...history.values()].filter(h => !h.createdAt || new Date(h.createdAt).getTime() >= cutoff)]
      .filter(j => !status || j.status === status)
      .filter(j => !toolId || j.toolId === toolId)
    rows.sort((a, b) => {
      const ap = a.status === 'running' || a.status === 'queued' ? 0 : 1
      const bp = b.status === 'running' || b.status === 'queued' ? 0 : 1
      if (ap !== bp) return ap - bp
      const at = a.finishedAt || a.createdAt || ''
      const bt = b.finishedAt || b.createdAt || ''
      return bt.localeCompare(at)
    })
    return rows.slice(0, cap).map(j => summarize(j))
  }

  function get(id) {
    const j = jobs.get(id) || history.get(id)
    if (!j) return null
    return {
      id: j.id,
      toolId: j.toolId,
      args: j.args || {},
      status: j.status,
      exitCode: j.exitCode,
      pid: j.pid,
      createdAt: j.createdAt,
      startedAt: j.startedAt,
      finishedAt: j.finishedAt,
      logPath: j.logPath,
      cancelRequested: !!j.cancelRequested,
      error: j.error || undefined,
      resultHints: j.resultHints || {},
    }
  }

  function cancel(id) {
    const job = jobs.get(id)
    if (!job) return { status: 404, body: { ok: false, error: 'unknown job' } }
    if (job.status !== 'queued' && job.status !== 'running') {
      return { status: 409, body: { ok: false, error: 'job already finished' } }
    }
    if (job.status === 'queued') {
      const idx = writableQueue.indexOf(job)
      if (idx >= 0) writableQueue.splice(idx, 1)
      job.status = 'canceled'
      job.finishedAt = new Date().toISOString()
      emit(job, 'status', { status: 'canceled', exitCode: null, error: 'canceled before start' })
      closeSubs(job)
      return { status: 200, body: { ok: true, job: summarize(job) } }
    }
    job.cancelRequested = true
    emit(job, 'cancel', { cancelRequested: true })
    try {
      if (job._child && job._child.kill) job._child.kill('SIGTERM')
    } catch { /* ignore */ }
    if (job._killTimer == null && !job._history) {
      job._killTimer = setTimeout(() => {
        job._killTimer = null
        if (job.status === 'running') {
          job._killFlag = true
          try {
            if (job._child && job._child.kill) job._child.kill('SIGKILL')
          } catch { /* ignore */ }
        }
      }, limits.cancelGraceMs)
      if (job._killTimer.unref) job._killTimer.unref()
    }
    return { status: 200, body: { ok: true, canceled: true } }
  }

  function clearFinished() {
    const ago = Date.now() - 3600 * 1000
    let removed = 0
    for (const [id, j] of [...jobs]) {
      const terminal = ['succeeded', 'failed', 'canceled', 'interrupted'].includes(j.status)
      const finishedTs = j.finishedAt ? new Date(j.finishedAt).getTime() : null
      if (terminal && finishedTs != null && finishedTs < ago) {
        jobs.delete(id)
        if (j._killTimer) clearTimeout(j._killTimer)
        if (j._pingTimer) clearInterval(j._pingTimer)
        try {
          if (j.logPath && existsSync(j.logPath)) unlinkSync(j.logPath)
        } catch { /* ignore */ }
        removed++
      }
    }
    return removed
  }

  function shutdown() {
    for (const j of jobs.values()) {
      if (j._killTimer) clearTimeout(j._killTimer)
      if (j._pingTimer) clearInterval(j._pingTimer)
      if (j.status === 'running') {
        if (j._child && j._child.kill) j._child.kill('SIGKILL')
      }
    }
  }

  return { jobs, history, registryById, create, list, get, cancel, clearFinished, shutdown }
}

function frameOf(ev) {
  return `id: ${ev.seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev.data)}\n\n`
}

function isTerminalStatus(status) {
  return ['succeeded', 'failed', 'canceled', 'interrupted'].includes(status)
}

function handleJobStream(req, res, id, jobManager) {
  const job = jobManager.jobs.get(id) || jobManager.history.get(id)
  if (!job) {
    sendJson(res, 404, { ok: false, error: 'unknown job' })
    return
  }
  const hdr = req.headers['last-event-id'] != null ? req.headers['last-event-id'] : req.headers['last-event-id']
  let last = Number(hdr)
  if (!Number.isInteger(last) || last < 0) last = -1
  const events = job._events || []
  const dropped = job._dropped || 0
  if (last + 1 < dropped) {
    res.statusCode = 410
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ ok: false, error: 'stream expired' }))
    return
  }
  if (isTerminalStatus(job.status) && last + 1 >= events.length) {
    res.statusCode = 410
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ ok: false, error: 'stream expired' }))
    return
  }
  res.statusCode = 200
  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache')
  res.setHeader('Connection', 'keep-alive')
  if (res.flushHeaders) res.flushHeaders()
  for (let i = 0; i < events.length; i++) {
    if (events[i].seq > last) res.write(frameOf(events[i]))
  }
  if (isTerminalStatus(job.status)) {
    res.end()
    return
  }
  const onClose = () => {
    if (!res.writableEnded) res.end()
  }
  job._subs.add(res)
  if (job.ensurePing) job.ensurePing()
  res.on('close', () => {
    job._subs.delete(res)
    req.removeListener('close', onClose)
  })
  req.on('close', onClose)
}

function readBodyCapped(req, limit) {
  return new Promise((resolve, reject) => {
    let body = ''
    let size = 0
    req.on('data', (c) => {
      size += c.length
      if (size > limit) {
        reject(new Error('request body too large'))
        req.destroy()
        return
      }
      body += c
    })
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {})
      } catch {
        reject(new Error('Invalid JSON body'))
      }
    })
    req.on('error', reject)
  })
}

function scanRequestHandler(ctx) {
  let jobManager
  const getJobs = () => {
    if (!jobManager) {
      jobManager = ctx.jobManager || createJobManager({
        repoRoot: ctx.repoRoot,
        jobsDir: ctx.jobsDir || undefined,
        scriptRegistry: ctx.scriptRegistry || undefined,
      })
    }
    return jobManager
  }
  return (req, res, next) => {
    if (req.url.startsWith('/api/models') || req.url.startsWith('/api/models/probe')) {
      handleModelsApi(req, res, ctx, getJobs).then(done => { if (!done) next() })
      return
    }

    if (req.url === '/api/registry') {
      sendJson(res, 200, { ok: true, groups: GROUP_LABELS, tools: getScriptRegistry() })
      return
    }

    if (req.url === '/api/env-check') {
      const missingKeys = ['OPENROUTER_API_KEY', 'OPENROUTER_MANAGEMENT_KEY', 'GOOGLE_BOOKS_API_KEY']
        .filter(k => !process.env[k])
      sendJson(res, 200, { ok: true, missingKeys })
      return
    }

    if (req.url === '/bench-list') {
      const benches = []
      try {
        for (const f of readdirSync(join(ctx.repoRoot, 'bench'))) {
          if (f.endsWith('.jsonl')) benches.push(f.replace(/\.jsonl$/, ''))
        }
      } catch { /* bench dir missing */ }
      try {
        for (const d of readdirSync(ctx.runsDir)) benches.push(d)
      } catch { /* runs dir missing */ }
      sendJson(res, 200, { ok: true, benches: [...new Set(benches)].sort() })
      return
    }

    const jobsPathOnly = stripQuery(req.url)
    if (jobsPathOnly === '/api/jobs' || jobsPathOnly.startsWith('/api/jobs/')) {
      const jm = getJobs()
      const pathOnly = jobsPathOnly
      const parsed = pathOnly.match(/^\/api\/jobs(?:\/([^/]+))?(\/(?:stream|log|cancel))?$/)
      const id = parsed && parsed[1]
      const sub = parsed && parsed[2]

      if (req.method === 'POST' && !id) {
        readBodyCapped(req, JOB_LIMITS.bodyMaxBytes).then(body => {
          const r = jm.create({
            toolId: body && body.toolId,
            args: body && body.args,
            confirm: body && body.confirm,
          })
          sendJson(res, r.status, r.body)
        }).catch(e => {
          sendJson(res, 400, { ok: false, error: e.message })
        })
        return
      }
      if (req.method === 'DELETE' && !id) {
        const removed = jm.clearFinished()
        sendJson(res, 200, { ok: true, removed })
        return
      }
      if (req.method === 'GET' && !id) {
        const q = new URL(req.url, 'http://localhost').searchParams
        sendJson(res, 200, {
          ok: true,
          jobs: jm.list({
            status: q.get('status') || undefined,
            toolId: q.get('toolId') || undefined,
            limit: q.get('limit') || undefined,
          }),
        })
        return
      }
      if (req.method === 'GET' && id && sub === '/stream') {
        handleJobStream(req, res, id, jm)
        return
      }
      if (req.method === 'GET' && id && sub === '/log') {
        const j = jm.get(id)
        if (!j) {
          sendJson(res, 404, { ok: false, error: 'unknown job' })
          return
        }
        if (!existsSync(j.logPath)) {
          sendJson(res, 404, { ok: false, error: 'log pruned' })
          return
        }
        res.setHeader('Content-Type', 'text/plain')
        res.setHeader('Content-Disposition', `attachment; filename="${id}.log"`)
        res.end(readFileSync(j.logPath, 'utf-8'))
        return
      }
      if (req.method === 'POST' && id && sub === '/cancel') {
        const r = jm.cancel(id)
        sendJson(res, r.status, r.body)
        return
      }
      if (req.method === 'GET' && id && !sub) {
        const j = jm.get(id)
        if (!j) {
          sendJson(res, 404, { ok: false, error: 'unknown job' })
          return
        }
        sendJson(res, 200, { ok: true, job: j })
        return
      }
    }

    const runsDir = ctx.runsDir
    const dataDir = ctx.dataDir

    if (req.url === '/runs-list') {
      const allFiles = []
      try {
        const dirs = readdirSync(runsDir, { withFileTypes: true })
        for (const dir of dirs) {
          if (dir.isDirectory()) {
            const subDir = join(runsDir, dir.name)
            const files = readdirSync(subDir).filter(f => f.endsWith('.json') || f.endsWith('.jsonl'))
            for (const file of files) {
              allFiles.push(`${dir.name}/${file}`)
            }
          }
        }
      } catch (e) {
        // runs dir not found
      }
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify(allFiles))
      return
    }

    if (req.url.startsWith('/runs/')) {
      const relativePath = req.url.slice('/runs/'.length)
      const result = serveStaticFile(runsDir, relativePath)
      if (!result) {
        res.statusCode = 404
        res.end('Not found')
        return
      }
      res.setHeader('Content-Type', result.contentType)
      res.end(result.content)
      return
    }

    if (req.url.startsWith('/data/')) {
      const relativePath = req.url.slice('/data/'.length)
      const result = serveStaticFile(dataDir, relativePath)
      if (!result) {
        res.statusCode = 404
        res.end('Not found')
        return
      }
      res.setHeader('Content-Type', result.contentType)
      res.end(result.content)
      return
    }

    // ── Chapter notes API ──────────────────────────────────
    const notesPath = join(ctx.dataDir, 'chapter_notes.jsonl')

    if (req.url === '/notes' && req.method === 'GET') {
      readAndSendNotes(notesPath, res)
      return
    }

    if (req.url === '/notes/all' && req.method === 'GET') {
      readAndSendNotes(notesPath, res)
      return
    }

    if (req.url === '/notes' && req.method === 'POST') {
      let body = ''
      req.on('data', chunk => { body += chunk })
      req.on('end', async () => {
        try {
          const note = JSON.parse(body)
          note.id = note.id || randomUUID()
          note.timestamp = note.timestamp || new Date().toISOString()

          // ── Auto-tag when user left tags empty ──────────────
          if (!note.tags || note.tags.length === 0) {
            const text = (note.text || '').trim()
            if (text) {
              const result = await ctx.autoTag(text)
              note.tags = result.tags
              note.sentiment = result.sentiment
              note.auto_tag_source = result.source
            } else {
              note.tags = []
              note.sentiment = 0
              note.auto_tag_source = 'empty_text'
            }
          }

          appendFileSync(notesPath, JSON.stringify(note) + '\n', 'utf-8')
          res.setHeader('Content-Type', 'application/json')
          res.statusCode = 201
          res.end(JSON.stringify({ ok: true, id: note.id, tags: note.tags, auto_tag_source: note.auto_tag_source }))
        } catch (e) {
          res.statusCode = 400
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ ok: false, error: e.message }))
        }
      })
      return
    }

    next()
  }
}

function createScanPlugin(opts) {
  const ctx = makeCtx(opts)
  return {
    name: 'scan-runs',
    configureServer(server) {
      server.middlewares.use(scanRequestHandler(ctx))
    },
    configurePreviewServer(server) {
      server.middlewares.use(scanRequestHandler(ctx))
    },
  }
}

function scanRunsPlugin() {
  return createScanPlugin()
}

export { makeCtx, scanRequestHandler, createScanPlugin, scanRunsPlugin, getScriptRegistry, createJobManager, JOB_LIMITS, SCRIPT_REGISTRY, killAllChildren, liveChildren }
export default defineConfig({
  root: '.',
  plugins: [scanRunsPlugin()],
  server: {
    port: 3001,
    allowedHosts: ['steves-macbook-pro.tailfa97f9.ts.net']
  }
})