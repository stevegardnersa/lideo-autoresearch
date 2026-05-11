import { marked } from 'marked'
import './style.css'

function getRunIdFromUrl() {
  const params = new URLSearchParams(window.location.search)
  return params.get('run_id') || ''
}

async function loadRunManifest(runId) {
  try {
    const files = await fetch('/runs-list').then(r => r.json())
    const runFile = files.find(f => f.endsWith('.json') && !f.includes('/mock/') && !f.endsWith('.state.json') && f.includes(runId))
    if (!runFile) return null

    const response = await fetch(`/runs/${runFile}`)
    return await response.json()
  } catch (e) {
    console.error('Failed to load run manifest:', e)
    return null
  }
}

async function loadRunSamples(runId) {
  try {
    const files = await fetch('/runs-list').then(r => r.json())
    const sampleFile = files.find(f => f.endsWith('.samples.jsonl') && !f.includes('/mock/') && f.includes(runId))
    if (!sampleFile) return []

    const response = await fetch(`/runs/${sampleFile}`)
    const text = await response.text()
    const lines = text.trim().split('\n')
    return lines.map(line => {
      try {
        return JSON.parse(line)
      } catch {
        return null
      }
    }).filter(Boolean)
  } catch (e) {
    console.error('Failed to load samples:', e)
    return []
  }
}

async function loadChapterOriginal(bookId, chapterId) {
  try {
    const response = await fetch(`/data/books/${bookId}/original/${chapterId}.md`)
    if (!response.ok) return 'Original chapter not found.'
    return await response.text()
  } catch (e) {
    return 'Failed to load original chapter.'
  }
}

function populateChapterSelect(samples) {
  const select = document.getElementById('chapterSelect')
  select.innerHTML = '<option value="">Select a chapter...</option>'

  samples.forEach(sample => {
    if (!sample || !sample.item_key) return
    const option = document.createElement('option')
    option.value = sample.item_key
    const bookTitle = sample.book_id || sample.item_key.split(':')[0]
    const chapterNum = sample.chapter_id || sample.item_key.split(':')[1]
    option.textContent = `${bookTitle} - ${chapterNum}`
    select.appendChild(option)
  })
}

function renderSummary(sample) {
  const content = document.getElementById('summaryContent')
  const summary = sample.summary_md || sample.first_pass_summary_md || ''

  if (!summary) {
    content.innerHTML = '<div class="placeholder-text">No summary available for this chapter.</div>'
    return
  }

  let html = ''
  try {
    const parsed = JSON.parse(summary)
    if (typeof parsed === 'object' && parsed !== null) {
      html = parsed.summary || parsed.primary || JSON.stringify(parsed, null, 2)
    } else {
      html = parsed
    }
  } catch {
    html = summary
  }

  if (html.startsWith('{') || html.startsWith('[')) {
    html = '<pre class="json-content">' + html + '</pre>'
  }

  content.innerHTML = marked.parse(html)
}

async function renderOriginal(sample) {
  const content = document.getElementById('originalContent')
  const bookId = sample.book_id || sample.item_key.split(':')[0]
  const chapterId = String(sample.chapter_id || sample.item_key.split(':')[1]).padStart(3, '0')

  const text = await loadChapterOriginal(bookId, chapterId)
  content.innerHTML = marked.parse(text)
}

async function handleChapterSelect(samples) {
  const select = document.getElementById('chapterSelect')
  const selectedKey = select.value

  const sample = samples.find(s => s && s.item_key === selectedKey)
  if (!sample) return

  renderSummary(sample)
  await renderOriginal(sample)
}

document.addEventListener('DOMContentLoaded', async () => {
  const runId = getRunIdFromUrl()
  if (!runId) {
    document.getElementById('runSubtitle').textContent = 'No run_id provided.'
    return
  }

  const manifest = await loadRunManifest(runId)
  if (!manifest) {
    document.getElementById('runSubtitle').textContent = 'Run not found: ' + runId
    return
  }

  const runManifest = manifest.run_manifest || {}
  document.getElementById('runSubtitle').textContent =
    `${runManifest.candidate_name || runId} · ${runManifest.bench || runManifest.profile || ''}`

  const samples = await loadRunSamples(runId)
  if (samples.length === 0) {
    document.getElementById('runSubtitle').textContent += ' (no samples found)'
    return
  }

  populateChapterSelect(samples)

  document.getElementById('chapterSelect').addEventListener('change', () => {
    handleChapterSelect(samples)
  })
})