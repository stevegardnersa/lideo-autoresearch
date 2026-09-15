import { marked } from 'marked'
import './settings.js'
import { initRunWidget } from './settings.js'

const ORIGINAL_CHAPTER = '__original__'

let currentManifest = null
let allProfiles = []
let selectedChapterKey = ''
let paneSelectionsByTimeBudget = {}
let currentTimeBudget = '30m'

function getPaneSelection(side) {
  if (!paneSelectionsByTimeBudget[currentTimeBudget]) {
    paneSelectionsByTimeBudget[currentTimeBudget] = { left: null, right: ORIGINAL_CHAPTER }
  }
  return paneSelectionsByTimeBudget[currentTimeBudget][side]
}

function setPaneSelection(side, value) {
  if (!paneSelectionsByTimeBudget[currentTimeBudget]) {
    paneSelectionsByTimeBudget[currentTimeBudget] = { left: null, right: ORIGINAL_CHAPTER }
  }
  paneSelectionsByTimeBudget[currentTimeBudget][side] = value
}
let profileSamplesCache = {}
let originalTextCache = {}
let currentSamples = []
let currentRunId = ''
let currentJudgeType = ''
let anchorRuns = []
let refreshNotesHook = null
const manifestCache = new Map()

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

async function loadRunManifest(runId, judgeType) {
  try {
    const runFile = await findRunFile(runId, judgeType)
    if (!runFile) return null

    const response = await fetch(`/runs/${runFile}`)
    const manifest = await response.json()
    manifestCache.set(runFile, manifest)
    return manifest
  } catch (e) {
    console.error('Failed to load run manifest:', e)
    return null
  }
}

async function loadRunSamples(runId, judgeType) {
  try {
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
  const [allFiles, hiddenResp] = await Promise.all([
    fetch('/runs-list').then(r => r.json()),
    fetch('/api/models/hidden').then(r => r.json()).catch(() => ({ hidden: [] })),
  ])
  const hiddenProfiles = new Set(hiddenResp.hidden || [])
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
      if (hiddenProfiles.has(name)) continue
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

async function discoverRunsForCandidate(candidateName, judgeType) {
  if (!candidateName) return []
  const files = await fetch('/runs-list').then(r => r.json())
  const isLlm = judgeType.startsWith('LLM:')
  const jsonFiles = files.filter(f =>
    f.endsWith('.json') && !f.includes('/mock/') && !f.endsWith('.state.json') &&
    (isLlm ? f.includes('__llmj_') : !f.includes('__llmj_'))
  )

  const runs = []
  for (const file of jsonFiles) {
    try {
      let manifest = manifestCache.get(file)
      if (!manifest) {
        const resp = await fetch(`/runs/${file}`)
        manifest = await resp.json()
        manifestCache.set(file, manifest)
      }
      const rm = manifest.run_manifest || {}
      if (rm.candidate_name !== candidateName) continue
      runs.push({
        runId: rm.run_id || '',
        file,
        manifest,
        bench: rm.bench || '',
        createdAt: rm.created_at_utc || '',
      })
    } catch {
      // skip
    }
  }

  runs.sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || '') || (b.runId || '').localeCompare(a.runId || ''))
  return runs
}

function formatRunTimestamp(createdAt) {
  if (!createdAt) return ''
  const d = new Date(createdAt)
  if (isNaN(d.getTime())) return ''
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function runOptionLabel(run, anchorBench) {
  const ts = formatRunTimestamp(run.createdAt)
  const parts = []
  if (run.bench && run.bench !== anchorBench) parts.push(run.bench)
  parts.push(ts || run.runId || 'unknown run')
  return parts.join(' · ')
}

function populateRunSelect(anchorRun, runs) {
  const select = document.getElementById('runSelect')
  if (!select) return
  select.innerHTML = ''
  const anchorBench = anchorRun?.bench || ''

  const sorted = runs.slice().sort((a, b) => {
    if ((a.bench === anchorBench) !== (b.bench === anchorBench)) return a.bench === anchorBench ? -1 : 1
    return (b.createdAt || '').localeCompare(a.createdAt || '')
  })

  sorted.forEach(run => {
    const opt = document.createElement('option')
    opt.value = run.runId
    opt.textContent = runOptionLabel(run, anchorBench)
    select.appendChild(opt)
  })

  const hasSelection = sorted.some(r => r.runId === currentRunId)
  if (hasSelection) {
    select.value = currentRunId
  } else if (anchorRun) {
    const match = sorted.find(r => r.file === anchorRun.file)
    if (match) select.value = match.runId
  }

  updateRunSelectVisibility()
}

function updateUrlRunId(runId) {
  const url = new URL(window.location.href)
  url.searchParams.set('run_id', runId)
  window.history.replaceState({}, '', url)
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
    const prevSelection = getPaneSelection(side)
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
        setPaneSelection('left', firstActive.candidateName)
      } else {
        sel.value = ORIGINAL_CHAPTER
        setPaneSelection(side, ORIGINAL_CHAPTER)
      }
    } else {
      sel.value = ORIGINAL_CHAPTER
      setPaneSelection(side, ORIGINAL_CHAPTER)
    }
  })
}

function getProfileByCandidateName(name) {
  return allProfiles.find(p => p.candidateName === name) || null
}

function syncAnchorProfile(manifest, runFile) {
  const name = manifest?.run_manifest?.candidate_name
  if (!name) return
  const entry = {
    candidateName: name,
    runId: manifest.run_manifest?.run_id || '',
    file: runFile,
    manifest,
  }
  const idx = allProfiles.findIndex(p => p.candidateName === name)
  if (idx === -1) {
    allProfiles.push(entry)
  } else {
    allProfiles[idx] = entry
  }
  delete profileSamplesCache[name]
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

function getScoringGates(candidateName) {
  const prefix = (candidateName || '').split('_')[0]
  const gates = {
    '30m': { minFaith: 0.50, minConcept: 0.15 },
    '60m': { minFaith: 0.60, minConcept: 0.50 },
  }
  return gates[prefix] || { minFaith: 0.70, minConcept: 0.60 }
}

function renderPaneMetrics(side, sample, score, isOriginal, originalText, thresholds) {
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
  let actualLengthCls = '', faithFailCls = '', conceptFailCls = ''

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
      const reasons = score.hard_fail_reasons || []
      if (reasons.includes('length_outside_hard_tolerance')) actualLengthCls = 'metric-fail'
      if (reasons.includes('faithfulness_below_threshold')) faithFailCls = 'metric-fail'
      if (reasons.includes('concept_coverage_below_threshold')) conceptFailCls = 'metric-fail'
    } else {
      statusText = 'PASS'
      statusClass = 'status-pass'
    }
  }

  container.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">Target / Actual</div>
      <div class="metric-value"><span>${(sample.target_words || '-').toLocaleString()}</span> / <span class="${actualLengthCls}">${summaryWords.toLocaleString()}</span></div>
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
      <div class="metric-value"><span class="${faithFailCls}" title="threshold: ${thresholds?.minFaith ?? '?'}">${faithVal}</span> / <span class="${conceptFailCls}" title="threshold: ${thresholds?.minConcept ?? '?'}">${conceptVal}</span></div>
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
  const selection = getPaneSelection(side)
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
    renderPaneMetrics(side, null, null, true, text, null)
    return
  }

  const profile = getProfileByCandidateName(selection)
  if (!profile) {
    contentEl.innerHTML = '<div class="placeholder-text">Profile not found.</div>'
    document.getElementById(`${side}Metrics`).innerHTML = ''
    return
  }

  const thresholds = getScoringGates(profile.candidateName)
  const samples = await getProfileSamples(profile)
  const sample = getMatchingSample(samples, selectedChapterKey)
  if (!sample) {
    contentEl.innerHTML = '<div class="placeholder-text">No data for this chapter in selected profile.</div>'
    document.getElementById(`${side}Metrics`).innerHTML = ''
    return
  }

  const score = getScoreForSample(profile.manifest, sample.sample_id || sample.item_key)
  contentEl.innerHTML = renderSummaryHTML(sample)
  renderPaneMetrics(side, sample, score, false, '', thresholds)
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
  setPaneSelection(side, select.value)
  await renderPane(side)
}

async function applyActiveRun(run) {
  currentRunId = run.runId
  currentManifest = run.manifest

  const runManifest = run.manifest.run_manifest || {}
  const profileLabel = runManifest.candidate_name || run.runId

  syncAnchorProfile(run.manifest, run.file)

  currentSamples = await loadRunSamples(run.runId, currentJudgeType)
  if (currentSamples.length === 0) {
    document.getElementById('runSubtitle').textContent = `${profileLabel} (no samples found)`
    return
  }

  document.getElementById('runSubtitle').textContent = profileLabel

  selectedChapterKey = populateChapterSelect(currentSamples)

  await renderPane('left')
  await renderPane('right')
  await updateGenreSubtext()
}

async function handleRunChange(runId) {
  if (!runId || runId === currentRunId) return
  const run = anchorRuns.find(r => r.runId === runId)
  if (!run) return

  updateUrlRunId(run.runId)
  await applyActiveRun(run)
  if (refreshNotesHook) await refreshNotesHook()
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
    const prevVal = getPaneSelection(side)
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
        setPaneSelection('left', firstActive.candidateName)
      } else {
        sel.value = ORIGINAL_CHAPTER
        setPaneSelection(side, ORIGINAL_CHAPTER)
      }
    } else {
      sel.value = ORIGINAL_CHAPTER
      setPaneSelection(side, ORIGINAL_CHAPTER)
    }
  })

  updateRunSelectVisibility()

  await renderPane('left')
  await renderPane('right')
}

function updateRunSelectVisibility() {
  const group = document.getElementById('runSelect')?.closest('.run-select-group')
  if (!group) return
  const anchor = anchorRuns.find(r => r.runId === currentRunId)
  const anchorName = anchor?.manifest?.run_manifest?.candidate_name || ''
  const matchesBudget = anchorName.startsWith(currentTimeBudget + '_')
  group.style.display = (matchesBudget && anchorRuns.length > 1) ? '' : 'none'
}

document.addEventListener('DOMContentLoaded', async () => {
  initRunWidget()
  const { runId, judgeType } = getRunIdFromUrl()
  if (!runId) {
    document.getElementById('runSubtitle').textContent = 'No run_id provided.'
    return
  }

  currentRunId = runId
  currentJudgeType = judgeType
  currentManifest = await loadRunManifest(runId, judgeType)
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

  const profileName = runManifest.candidate_name
  if (profileName) {
    const matching = allProfiles.find(p => p.candidateName === profileName && !isProfileDisabled(p))
    if (matching) {
      setPaneSelection('left', profileName)
      document.getElementById('leftProfileSelect').value = profileName
    }
  }

  anchorRuns = await discoverRunsForCandidate(runManifest.candidate_name || '', currentJudgeType)
  if (anchorRuns.length === 0) {
    anchorRuns = [{
      runId: runManifest.run_id || runId,
      file: await findRunFile(runId, judgeType),
      manifest: currentManifest,
      bench: runManifest.bench || '',
      createdAt: runManifest.created_at_utc || '',
    }]
  }
  const anchorRun = anchorRuns.find(r => r.runId === currentRunId) || anchorRuns[0]
  populateRunSelect(anchorRun, anchorRuns)
  document.getElementById('runSelect').addEventListener('change', e => handleRunChange(e.target.value))

  await applyActiveRun(anchorRun)

  document.getElementById('chapterSelect').addEventListener('change', handleChapterChange)

  document.getElementById('leftProfileSelect').addEventListener('change', () => handleProfileChange('left'))
  document.getElementById('rightProfileSelect').addEventListener('change', () => handleProfileChange('right'))

  document.querySelectorAll('.time-pill').forEach(pill => {
    pill.addEventListener('click', () => handleTimeToggle(pill.dataset.time))
  })

  // ── Chapter notes ───────────────────────────────────────────
  let notesActiveTags = new Set()
  const notesToggle = document.getElementById('notesToggle')
  const notesBody = document.getElementById('notesBody')
  const notesList = document.getElementById('notesList')
  const notesTextarea = document.getElementById('notesTextarea')
  const notesSubmit = document.getElementById('notesSubmit')

  notesToggle.addEventListener('click', () => {
    const collapsed = notesBody.classList.toggle('collapsed')
    notesToggle.classList.toggle('collapsed', collapsed)
    notesToggle.innerHTML = collapsed ? '&#9654;' : '&#9660;'
  })

  document.querySelectorAll('#notesTagsRow .tag-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const tag = chip.dataset.tag
      if (notesActiveTags.has(tag)) {
        notesActiveTags.delete(tag)
        chip.classList.remove('active')
      } else {
        notesActiveTags.add(tag)
        chip.classList.add('active')
      }
    })
  })

  notesSubmit.addEventListener('click', async () => {
    const text = notesTextarea.value.trim()
    if (!text || !selectedChapterKey) return
    notesSubmit.disabled = true
    notesSubmit.textContent = 'Saving...'
    try {
      const leftSelection = getPaneSelection('left')
      const [bookId, chapterId] = selectedChapterKey.split(':')
      const hasManualTags = notesActiveTags.size > 0
      const note = {
        book_id: bookId,
        chapter_id: chapterId,
        item_key: selectedChapterKey,
        candidate_name: leftSelection && leftSelection !== ORIGINAL_CHAPTER ? leftSelection : null,
        tags: hasManualTags ? [...notesActiveTags] : [],
        text,
        timestamp: new Date().toISOString(),
      }
      const resp = await fetch('/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(note),
      })
      const saved = await resp.json()
      if (saved.ok && !hasManualTags && saved.tags && saved.tags.length > 0) {
        // Server auto-tagged the note — flash tags briefly
        console.log(`[notes] Auto-tagged via ${saved.auto_tag_source}:`, saved.tags)
      }
      notesTextarea.value = ''
      // Clear manual tag selections (actual tags are server-side when empty)
      notesActiveTags.clear()
      document.querySelectorAll('#notesTagsRow .tag-chip').forEach(chip => chip.classList.remove('active'))
      await loadAndRenderNotes()
    } catch (e) {
      console.error('Failed to save note:', e)
    } finally {
      notesSubmit.disabled = false
      notesSubmit.textContent = 'Add Note'
    }
  })

  async function loadAndRenderNotes() {
    if (!selectedChapterKey) {
      notesList.innerHTML = '<div class="placeholder-text">Select a chapter to see notes.</div>'
      return
    }
    try {
      const all = await fetch('/notes/all').then(r => r.json())
      const mine = all.filter(n => n.item_key === selectedChapterKey || (n.book_id && n.chapter_id && `${n.book_id}:${n.chapter_id}` === selectedChapterKey))
      if (mine.length === 0) {
        notesList.innerHTML = '<div class="placeholder-text">No notes for this chapter yet.</div>'
        return
      }
      mine.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))
      notesList.innerHTML = mine.map(n => {
        const tagsHTML = (n.tags || []).map(t => `<span class="note-tag">${t}</span>`).join('')
        const candidateHTML = n.candidate_name ? `<span class="note-candidate">${n.candidate_name}</span>` : ''
        const timeStr = n.timestamp ? new Date(n.timestamp).toLocaleString() : ''
        const sentimentBadge = (n.sentiment != null && n.sentiment !== 0)
          ? `<span class="note-sentiment ${n.sentiment < 0 ? 'neg' : 'pos'}">${n.sentiment < 0 ? '&#9660;' : '&#9650;'} ${Math.abs(n.sentiment).toFixed(2)}</span>` : ''
        const autoTagBadge = n.auto_tag_source && n.auto_tag_source !== 'empty_text'
          ? `<span class="note-auto-tag" title="Auto-tagged via ${n.auto_tag_source}">auto</span>` : ''
        return `<div class="note-item">
          <div class="note-meta">${candidateHTML}<span class="note-tags">${tagsHTML}</span>${sentimentBadge}${autoTagBadge}</div>
          <div class="note-text">${n.text}</div>
          <div class="note-time">${timeStr}</div>
        </div>`
      }).join('')
    } catch (e) {
      console.error('Failed to load notes:', e)
      notesList.innerHTML = '<div class="placeholder-text">Failed to load notes.</div>'
    }
  }

  refreshNotesHook = loadAndRenderNotes

  const origHandleChapterChange = handleChapterChange
  handleChapterChange = async function() {
    await origHandleChapterChange()
    await loadAndRenderNotes()
  }

  if (selectedChapterKey) {
    await loadAndRenderNotes()
  }
})