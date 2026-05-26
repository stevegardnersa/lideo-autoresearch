import { marked } from 'marked'

const ORIGINAL_CHAPTER = '__original__'

let currentManifest = null
let allProfiles = []
let selectedChapterKey = ''
let paneSelection = { left: null, right: ORIGINAL_CHAPTER }
let currentTimeBudget = '30m'
let profileSamplesCache = {}
let originalTextCache = {}
let currentSamples = []

function getRunIdFromUrl() {
  const params = new URLSearchParams(window.location.search)
  return {
    runId: params.get('run_id') || '',
    judgeType: params.get('judge_type') || '',
    chapterKey: params.get('chapter_key') || '',
  }
}

async function findRunFile(runId, judgeType) {
  const files = await fetch('/runs-list').then(r => r.json())
  if (!runId) return null

  const isLlm = judgeType.startsWith('LLM:')
  const targetSuffix = isLlm ? '__llmj_' : '.json'

  const candidates = files.filter(f => {
    if (!f.endsWith('.json') || f.includes('/mock/') || f.endsWith('.state.json')) return false
    if (!f.includes(runId)) return false
    return !isLlm ? !f.includes('__llmj_') : f.includes('__llmj_')
  })

  return candidates[0] || null
}

async function loadRunManifest(runId) {
  try {
    const { runId: rid, judgeType } = getRunIdFromUrl()
    const runFile = await findRunFile(rid, judgeType)
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
    const { judgeType } = getRunIdFromUrl()
    const files = await fetch('/runs-list').then(r => r.json())
    if (!runId) return []

    const isLlm = judgeType.startsWith('LLM:')
    const candidates = files.filter(f => {
      if (!f.endsWith('.samples.jsonl') || f.includes('/mock/')) return false
      if (!f.includes(runId)) return false
      return !isLlm ? !f.includes('__llmj_') : f.includes('__llmj_')
    })

    return await fetchAndParseSamples(candidates[0])
  } catch (e) {
    console.error('Failed to load samples:', e)
    return []
  }
}

async function fetchAndParseSamples(sampleFile) {
  if (!sampleFile) return []
  try {
    const response = await fetch(`/runs/${sampleFile}`)
    const text = await response.text()
    const lines = text.trim().split('\n')
    return lines.map(line => {
      try { return JSON.parse(line) } catch { return null }
    }).filter(Boolean)
  } catch {
    return []
  }
}

async function discoverProfiles(timeBudget) {
  const allFiles = await fetch('/runs-list').then(r => r.json())
  const jsonFiles = allFiles.filter(f =>
    f.endsWith('.json') && !f.includes('/mock/') && !f.endsWith('.state.json') && !f.includes('__llmj_')
  )

  const profiles = []
  const seen = new Set()

  for (const file of jsonFiles) {
    try {
      const resp = await fetch(`/runs/${file}`)
      const manifest = await resp.json()
      const name = manifest.run_manifest?.candidate_name || ''
      if (!name.startsWith(timeBudget + '_')) continue
      if (seen.has(name)) continue
      seen.add(name)
      profiles.push({
        candidateName: name,
        runId: manifest.run_manifest?.run_id || '',
        file,
        manifest,
      })
    } catch {
      // skip
    }
  }

  profiles.sort((a, b) => a.candidateName.localeCompare(b.candidateName))
  return profiles
}

function getSamplesFile(manifestFile) {
  return manifestFile.replace(/\.json$/, '.samples.jsonl')
}

async function getProfileSamples(profile) {
  const key = profile.candidateName
  if (profileSamplesCache[key]) return profileSamplesCache[key]
  const samples = await fetchAndParseSamples(getSamplesFile(profile.file))
  profileSamplesCache[key] = samples
  return samples
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

function populateChapterSelect(samples) {
  const select = document.getElementById('chapterSelect')
  select.innerHTML = '<option value="">Select a chapter...</option>'
  const { chapterKey: urlChapterKey } = getRunIdFromUrl()

  let keyToSelect = ''
  samples.forEach(sample => {
    if (!sample || !sample.item_key) return
    const option = document.createElement('option')
    option.value = sample.item_key
    const bookTitle = sample.book_id || sample.item_key.split(':')[0]
    const chapterNum = sample.chapter_id || sample.item_key.split(':')[1]
    option.textContent = `${bookTitle} - ${chapterNum}`
    select.appendChild(option)
    if (sample.item_key === urlChapterKey) {
      keyToSelect = sample.item_key
    }
  })

  if (!keyToSelect && samples.length > 0) {
    keyToSelect = samples[0].item_key
  }

  if (keyToSelect) {
    select.value = keyToSelect
  }
  return select.value
}

function populatePaneSelects() {
  const leftSelect = document.getElementById('leftProfileSelect')
  const rightSelect = document.getElementById('rightProfileSelect')

  ;[leftSelect, rightSelect].forEach((sel, idx) => {
    const side = idx === 0 ? 'left' : 'right'
    const prevSelection = paneSelection[side]
    sel.innerHTML = ''
    const origOption = document.createElement('option')
    origOption.value = ORIGINAL_CHAPTER
    origOption.textContent = 'Original Chapter'
    sel.appendChild(origOption)
    allProfiles.forEach(p => {
      const opt = document.createElement('option')
      opt.value = p.candidateName
      opt.textContent = p.candidateName
      opt.disabled = isProfileDisabled(p)
      sel.appendChild(opt)
    })
    const prevStillValid = prevSelection && [...sel.options].some(o => o.value === prevSelection && !o.disabled)
    if (prevStillValid) {
      sel.value = prevSelection
    } else if (idx === 0) {
      const firstActive = allProfiles.find(p => !isProfileDisabled(p))
      if (firstActive) {
        sel.value = firstActive.candidateName
        paneSelection.left = firstActive.candidateName
      } else {
        sel.value = ORIGINAL_CHAPTER
        paneSelection[side] = ORIGINAL_CHAPTER
      }
    } else {
      sel.value = ORIGINAL_CHAPTER
      paneSelection[side] = ORIGINAL_CHAPTER
    }
  })
}

function getProfileByCandidateName(name) {
  return allProfiles.find(p => p.candidateName === name) || null
}

function getMatchingSample(samples, itemKey) {
  return (samples || []).find(s => s && s.item_key === itemKey) || null
}

function getScoreForSample(manifest, sampleId) {
  return (manifest?.sample_scores || []).find(s => s.sample_id === sampleId) || null
}

function isProfileDisabled(profile) {
  try {
    const raw = localStorage.getItem('scatter_explorer_state')
    if (!raw) return false
    const state = JSON.parse(raw)
    const filters = state.activeFilters || {}
    const chapterModel = profile.manifest?.run_manifest?.chapter_model || ''
    const provider = chapterModel.split('/')[0]
    if (filters['model']?.includes(chapterModel)) return true
    if (filters['provider']?.includes(provider)) return true
    return false
  } catch {
    return false
  }
}

function renderSummaryHTML(sample) {
  const summary = sample.summary_md || sample.first_pass_summary_md || ''
  if (!summary) return '<div class="placeholder-text">No summary available for this chapter.</div>'
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
  return marked.parse(html)
}

function renderPaneMetrics(side, sample, score, isOriginal, originalText) {
  const container = document.getElementById(`${side}Metrics`)
  if (!container) return

  if (isOriginal) {
    const words = originalText ? originalText.trim().split(/\s+/).filter(Boolean).length : 0
    const grade = originalText ? calculateReadingGrade(originalText) : 0
    container.innerHTML = `
      <div class="metric-card">
        <div class="metric-label">Original Words</div>
        <div class="metric-value">${words.toLocaleString()}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Original Grade</div>
        <div class="metric-value">G${grade.toFixed(1)}</div>
      </div>
    `
    return
  }

  if (!sample) {
    container.innerHTML = '<div class="metric-card"><div class="metric-label">No data</div><div class="metric-value">-</div></div>'
    return
  }

  const summaryText = sample.summary_md || sample.first_pass_summary_md || ''
  const summaryWords = summaryText.trim().split(/\s+/).filter(Boolean).length

  let qualityVal = '-', utilityVal = '-', faithVal = '-', conceptVal = '-', readabilityVal = '-'
  let statusText = '-', statusClass = ''
  let gradeVal = '-'

  if (score) {
    qualityVal = (score.quality || 0).toFixed(2)
    utilityVal = (score.utility || 0).toFixed(2)
    faithVal = (score.resolved_faithfulness || 0).toFixed(2)
    conceptVal = (score.resolved_concept_coverage || 0).toFixed(2)
    readabilityVal = (score.deterministic?.readability_band || 0).toFixed(2)
    const summaryGrade = calculateReadingGrade(summaryText)
    gradeVal = `G${summaryGrade.toFixed(1)}`
    if (score.hard_fail) {
      statusText = 'FAIL'
      statusClass = 'status-fail'
    } else {
      statusText = 'PASS'
      statusClass = 'status-pass'
    }
  }

  container.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">Target / Actual</div>
      <div class="metric-value"><span>${(sample.target_words || '-').toLocaleString()}</span> / <span>${summaryWords.toLocaleString()}</span></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Cost / Passes</div>
      <div class="metric-value"><span>${sample.generation_cost ? '$' + sample.generation_cost.toFixed(3) : '-'}</span> / <span>${sample.passes_used || '-'}</span></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Quality / Utility</div>
      <div class="metric-value"><span>${qualityVal}</span> / <span>${utilityVal}</span></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Faith / Concept</div>
      <div class="metric-value"><span>${faithVal}</span> / <span>${conceptVal}</span></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Summary Grade</div>
      <div class="metric-value"><span>${readabilityVal}</span> / <span>${gradeVal}</span></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Status</div>
      <div class="metric-value ${statusClass}">${statusText}</div>
    </div>
  `
}

async function renderPane(side) {
  const contentEl = document.getElementById(`${side}Content`)
  const selection = paneSelection[side]
  const isOriginal = selection === ORIGINAL_CHAPTER

  if (!selectedChapterKey) {
    contentEl.innerHTML = '<div class="placeholder-text">Select a chapter to view.</div>'
    document.getElementById(`${side}Metrics`).innerHTML = ''
    return
  }

  if (isOriginal) {
    if (!originalTextCache[selectedChapterKey]) {
      const parts = selectedChapterKey.split(':')
      originalTextCache[selectedChapterKey] = await loadChapterOriginal(parts[0], parts[1])
    }
    const text = originalTextCache[selectedChapterKey]
    contentEl.innerHTML = marked.parse(text)
    renderPaneMetrics(side, null, null, true, text)
    return
  }

  const profile = getProfileByCandidateName(selection)
  if (!profile) {
    contentEl.innerHTML = '<div class="placeholder-text">Profile not found.</div>'
    document.getElementById(`${side}Metrics`).innerHTML = ''
    return
  }

  const samples = await getProfileSamples(profile)
  const sample = getMatchingSample(samples, selectedChapterKey)
  if (!sample) {
    contentEl.innerHTML = '<div class="placeholder-text">No data for this chapter in selected profile.</div>'
    document.getElementById(`${side}Metrics`).innerHTML = ''
    return
  }

  const score = getScoreForSample(profile.manifest, sample.sample_id || sample.item_key)
  contentEl.innerHTML = renderSummaryHTML(sample)
  renderPaneMetrics(side, sample, score, false, '')
}

async function updateGenreSubtext() {
  const sample = currentSamples.find(s => s && s.item_key === selectedChapterKey)
  if (sample) {
    const trace = sample.trace || {}
    const genreMacro = trace.genre_macro || ''
    const genreMicro = trace.genre_micro || ''
    document.getElementById('genreSubtext').textContent =
      genreMacro ? `${genreMacro.replace(/_/g, ' ')} · ${genreMicro.replace(/_/g, ' ')}` : ''
  } else {
    document.getElementById('genreSubtext').textContent = ''
  }
}

async function handleChapterChange() {
  selectedChapterKey = document.getElementById('chapterSelect').value
  await renderPane('left')
  await renderPane('right')
  await updateGenreSubtext()
}

async function handleProfileChange(side) {
  const select = document.getElementById(`${side}ProfileSelect`)
  paneSelection[side] = select.value
  await renderPane(side)
}

async function handleTimeToggle(timeBudget) {
  currentTimeBudget = timeBudget
  document.querySelectorAll('.time-pill').forEach(pill => {
    pill.classList.toggle('active', pill.dataset.time === timeBudget)
  })

  allProfiles = await discoverProfiles(timeBudget)

  const leftSelect = document.getElementById('leftProfileSelect')
  const rightSelect = document.getElementById('rightProfileSelect')

  ;[leftSelect, rightSelect].forEach((sel, idx) => {
    const side = idx === 0 ? 'left' : 'right'
    const prevVal = paneSelection[side]
    sel.innerHTML = ''
    const origOption = document.createElement('option')
    origOption.value = ORIGINAL_CHAPTER
    origOption.textContent = 'Original Chapter'
    sel.appendChild(origOption)
    allProfiles.forEach(p => {
      const opt = document.createElement('option')
      opt.value = p.candidateName
      opt.textContent = p.candidateName
      opt.disabled = isProfileDisabled(p)
      sel.appendChild(opt)
    })
    const prevStillValid = prevVal && prevVal !== ORIGINAL_CHAPTER &&
      allProfiles.some(p => p.candidateName === prevVal && !isProfileDisabled(p))
    if (prevStillValid) {
      sel.value = prevVal
    } else if (idx === 0) {
      const firstActive = allProfiles.find(p => !isProfileDisabled(p))
      if (firstActive) {
        sel.value = firstActive.candidateName
        paneSelection.left = firstActive.candidateName
      } else {
        sel.value = ORIGINAL_CHAPTER
        paneSelection[side] = ORIGINAL_CHAPTER
      }
    } else {
      sel.value = ORIGINAL_CHAPTER
      paneSelection[side] = ORIGINAL_CHAPTER
    }
  })

  await renderPane('left')
  await renderPane('right')
}

document.addEventListener('DOMContentLoaded', async () => {
  const { runId, judgeType } = getRunIdFromUrl()
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

  const timeBudget = (runManifest.candidate_name || '').startsWith('60m_') ? '60m' : '30m'
  currentTimeBudget = timeBudget

  document.querySelectorAll('.time-pill').forEach(pill => {
    pill.classList.toggle('active', pill.dataset.time === timeBudget)
  })

  allProfiles = await discoverProfiles(timeBudget)

  populatePaneSelects()

  currentSamples = await loadRunSamples(runId)
  if (currentSamples.length === 0) {
    document.getElementById('runSubtitle').textContent = `${runManifest.candidate_name || runId} (no samples found)`
    return
  }

  document.getElementById('runSubtitle').textContent = runManifest.candidate_name || runId

  const selectedKey = populateChapterSelect(currentSamples)
  selectedChapterKey = selectedKey

  if (selectedKey) {
    await renderPane('left')
    await renderPane('right')
    await updateGenreSubtext()
  }

  document.getElementById('chapterSelect').addEventListener('change', handleChapterChange)

  document.getElementById('leftProfileSelect').addEventListener('change', () => handleProfileChange('left'))
  document.getElementById('rightProfileSelect').addEventListener('change', () => handleProfileChange('right'))

  document.querySelectorAll('.time-pill').forEach(pill => {
    pill.addEventListener('click', () => handleTimeToggle(pill.dataset.time))
  })
})