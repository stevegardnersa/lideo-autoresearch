import { defineConfig } from 'vite'
import { readdirSync, readFileSync, writeFileSync, existsSync, appendFileSync } from 'fs'
import { join, extname } from 'path'
import { fileURLToPath } from 'url'
import { randomUUID } from 'crypto'
import { execFile } from 'child_process'
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
      if (m.runs_count > 0) p.status = 'tested'
    }
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

function applyProfileEdit(data, body) {
  const { old_model, new_model } = body
  if (!old_model) return { error: 'old_model is required' }
  const profiles = data.profiles || {}
  const modelKeys = Object.keys(profiles).filter(k => profiles[k] && profiles[k].chapter_stage && profiles[k].chapter_stage.model === old_model)
  if (modelKeys.length === 0) return { error: `no profiles found for model ${old_model}` }

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

  return {
    data: { ...data, profiles: outProfiles },
    plan: { removed, renamed, updated, created: createdVariants, model: old_model !== target ? `${old_model} -> ${target}` : null },
  }
}

async function handleModelsApi(req, res, ctx) {
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
      writeCandidates(ctx.candidatesPath, result.data)
      await regenerateSpecPy(ctx.pythonRunner)
      sendJson(res, 200, { ok: true, plan: result.plan })
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
      const removedRuns = []
      for (const line of r.stdout.split('\n')) {
        const pm = line.match(/^  - (\S+)/)
        if (pm) removedProfiles.push(pm[1])
      }
      const rm = r.stdout.match(/Removed (\d+) run file\(s\)/)
      if (rm) removedRuns.push(parseInt(rm[1], 10))
      sendJson(res, 200, { ok: true, model, pattern, removedProfiles, removedRuns: removedRuns[0] || 0 })
    } catch (e) {
      sendJson(res, 500, { ok: false, error: e.message })
    }
    return true
  }

  return false
}

function scanRequestHandler(ctx) {
  return (req, res, next) => {
    if (req.url.startsWith('/api/models') || req.url.startsWith('/api/models/probe')) {
      handleModelsApi(req, res, ctx).then(done => { if (!done) next() })
      return
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

export { makeCtx, scanRequestHandler, createScanPlugin, scanRunsPlugin }
export default defineConfig({
  root: '.',
  plugins: [scanRunsPlugin()],
  server: {
    port: 3001,
    allowedHosts: ['steves-macbook-pro.tailfa97f9.ts.net']
  }
})