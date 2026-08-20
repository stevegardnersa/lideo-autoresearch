import { defineConfig } from 'vite'
import { readdirSync, readFileSync, existsSync, appendFileSync } from 'fs'
import { join, extname } from 'path'
import { randomUUID } from 'crypto'
import https from 'https'

function stripQuery(url) {
  const i = url.indexOf('?')
  return i >= 0 ? url.slice(0, i) : url
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

function scanRunsPlugin() {
  return {
    name: 'scan-runs',
    configureServer(server) {
      const runsDir = join(process.cwd(), 'runs')
      const dataDir = join(process.cwd(), 'data')

      server.middlewares.use((req, res, next) => {
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
        const notesPath = join(process.cwd(), 'data', 'chapter_notes.jsonl')

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
                  const result = await autoTagWithLLM(text)
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
      })
    }
  }
}

export default defineConfig({
  root: '.',
  plugins: [scanRunsPlugin()],
  server: {
    port: 3001
  }
})