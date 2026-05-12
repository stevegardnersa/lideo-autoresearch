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
    const paddedChapterId = String(chapterId).padStart(3, '0')
    const bookResponse = await fetch(`/data/books/${bookId}/book.json`)
    if (!bookResponse.ok) return 'Book manifest not found.'
    const bookData = await bookResponse.json()
    
    const chapters = bookData.chapters || []
    const chapter = chapters.find(c => c.chapter_id === paddedChapterId)
    if (!chapter || !chapter.source_path) return `Chapter ${paddedChapterId} not found in manifest.`

    const response = await fetch(`/data/books/${bookId}/${chapter.source_path}`)
    if (!response.ok) return 'Original chapter file not found.'
    return await response.text()
  } catch (e) {
    console.error('Failed to load original chapter:', e)
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

let currentManifest = null

function calculateReadingGrade(text) {
  const clean = text.replace(/```.*?```/gs, ' ').replace(/`.*?`/g, ' ').replace(/<.*?>/g, ' ')
  const words = clean.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g) || []
  const sentences = clean.split(/[.!?]+/).filter(s => s.trim().length > 0)
  
  if (words.length === 0 || sentences.length === 0) return 0
  
  const wordCount = words.length
  const sentenceCount = sentences.length
  
  const syllableCount = words.reduce((acc, word) => {
    const w = word.toLowerCase().replace(/[^a-z]/g, '')
    if (!w) return acc
    if (w.length <= 3) return acc + 1
    const vowelGroups = w.match(/[aeiouy]+/g) || []
    let count = vowelGroups.length
    if (w.endsWith('e') && !w.endsWith('le') && !w.endsWith('ye') && count > 1) count--
    if (w.endsWith('ed') && count > 1 && !w.endsWith('ted') && !w.endsWith('ded')) count--
    return acc + Math.max(1, count)
  }, 0)
  
  const grade = 0.39 * (wordCount / sentenceCount) + 11.8 * (syllableCount / wordCount) - 15.59
  return Math.max(0, grade)
}

async function renderOriginal(sample) {
  const content = document.getElementById('originalContent')
  const bookId = sample.book_id || sample.item_key.split(':')[0]
  const chapterId = sample.chapter_id || sample.item_key.split(':')[1]

  const text = await loadChapterOriginal(bookId, chapterId)
  content.innerHTML = marked.parse(text)

  // Word counts
  document.getElementById('targetWords').textContent = (sample.target_words || '-').toLocaleString()
  const summaryText = sample.summary_md || sample.first_pass_summary_md || ''
  const summaryWords = summaryText.trim().split(/\s+/).filter(Boolean).length
  document.getElementById('actualWords').textContent = summaryWords.toLocaleString()
  
  const originalWords = text.trim().split(/\s+/).filter(Boolean).length
  document.getElementById('originalWords').textContent = originalWords.toLocaleString()
  
  const originalGrade = calculateReadingGrade(text)
  document.getElementById('originalGrade').textContent = `G${originalGrade.toFixed(1)}`

  // Technical metrics
  document.getElementById('genCost').textContent = sample.generation_cost ? `$${sample.generation_cost.toFixed(3)}` : '-'
  document.getElementById('passesUsed').textContent = sample.passes_used || '-'

  // Join with scores from manifest
  const score = (currentManifest?.sample_scores || []).find(s => s.sample_id === sample.sample_id)
  if (score) {
    document.getElementById('qualityScore').textContent = (score.quality || 0).toFixed(2)
    document.getElementById('utilityScore').textContent = (score.utility || 0).toFixed(2)
    document.getElementById('faithScore').textContent = (score.resolved_faithfulness || 0).toFixed(2)
    document.getElementById('conceptScore').textContent = (score.resolved_concept_coverage || 0).toFixed(2)
    document.getElementById('readabilityScore').textContent = (score.deterministic?.readability_band || 0).toFixed(2)
    
    const summaryGrade = calculateReadingGrade(summaryText)
    document.getElementById('gradeLevel').textContent = `G${summaryGrade.toFixed(1)}`

    const statusEl = document.getElementById('failStatus')
    if (score.hard_fail) {
      statusEl.textContent = 'FAIL'
      statusEl.className = 'metric-value status-fail'
    } else {
      statusEl.textContent = 'PASS'
      statusEl.className = 'metric-value status-pass'
    }
  } else {
    ['qualityScore', 'utilityScore', 'faithScore', 'conceptScore', 'readabilityScore', 'gradeLevel', 'failStatus'].forEach(id => {
      const el = document.getElementById(id)
      if (el) el.textContent = '-'
    })
    document.getElementById('failStatus').className = 'metric-value'
  }

  // Genre subtext
  const trace = sample.trace || {}
  const genreMacro = trace.genre_macro || ''
  const genreMicro = trace.genre_micro || ''
  document.getElementById('genreSubtext').textContent = 
    genreMacro ? `${genreMacro.replace(/_/g, ' ')} · ${genreMicro.replace(/_/g, ' ')}` : ''
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

  currentManifest = await loadRunManifest(runId)
  if (!currentManifest) {
    document.getElementById('runSubtitle').textContent = 'Run not found: ' + runId
    return
  }

  const runManifest = currentManifest.run_manifest || {}
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