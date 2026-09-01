import './style.css'
import './settings.js'

const COLOR_MAP = {
  'deepseek/deepseek-v4-pro': '#f97316',
  'deepseek/deepseek-v4-flash': '#fb923c',
  'deepseek/deepseek-v3.2': '#ea580c',
  'minimax/minimax-m2.7': '#22c55e',
  'openai/gpt-5-mini': '#000000',
  'openai/gpt-4o': '#000000',
  'openai/gpt-oss-120b': '#000000',
  'google/gemini-3.1-pro-preview': '#14b8a6',
  'google/gemini-3-flash-preview': '#2dd4bf',
  'anthropic/claude-opus-4': '#0ea5e9',
  'anthropic/claude-sonnet-4': '#38bdf8',
  'anthropic/claude-sonnet-4.6': '#7dd3fc',
  'xiaomi/mimo-v2.5-pro': '#ec4899',
  'xiaomi/mimo-v2-flash': '#f472b6',
}

const JUDGE_COLOR_MAP = {
  'LLM:gpt-4o': '#8b5cf6',
  'LLM:claude-sonnet-4': '#38bdf8',
  'LLM:claude-sonnet-4.6': '#7dd3fc',
  'LLM:claude-opus-4': '#0ea5e9',
  'LLM:gemini-3.1-pro-preview': '#14b8a6',
  'LLM:gemini-3-flash-preview': '#2dd4bf',
  'LLM:deepseek-v4-pro': '#f97316',
  'LLM:deepseek-v4-flash': '#fb923c',
  'LLM:deepseek-v3.2': '#ea580c',
  'LLM:gpt-5-mini': '#6366f1',
  'LLM:minimax-m2.7': '#22c55e',
}

function getColor(key, value) {
  if (key === 'model' && COLOR_MAP[value]) return COLOR_MAP[value]
  if (key === 'judge_type' && JUDGE_COLOR_MAP[value]) return JUDGE_COLOR_MAP[value]
  let h = 0
  for (let i = 0; i < value.length; i++) h = ((h << 5) - h) + value.charCodeAt(i) | 0
  return `hsl(${Math.abs(h) % 360}, 55%, 48%)`
}

function getProvider(model) {
  if (!model) return 'unknown'
  return model.split('/')[0]
}

const STORAGE_KEY = 'scatter_explorer_state'

let runs = []
let currentMode = 'price_quality'
let activeFilters = {}
let showLabels = false
let highlightQuadrant = true
let fixedYRange = false
let selectedRunId = null
let selectedJudgeType = ''
let judgeVisibility = { deterministic: true, LLM: true }
let explicitFieldChanges = { xField: false, yField: false, sizeField: false, colorField: false, labelField: false }

function loadPersistedState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function savePersistedState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
  }
}

function persistState() {
  const currentSaved = loadPersistedState()
  savePersistedState({
    version: 2,
    currentMode,
    selectedRunId,
    selectedJudgeType,
    searchText: document.getElementById('searchInput')?.value || '',
    xField: document.getElementById('xSelect')?.value || '',
    yField: document.getElementById('ySelect')?.value || '',
    sizeField: document.getElementById('sizeSelect')?.value || '',
    colorField: document.getElementById('colorSelect')?.value || '',
    labelField: document.getElementById('labelSelect')?.value || '',
    showLabels,
    highlightQuadrant,
    fixedYRange,
    judgeVisibility,
    activeFilters
  })
}

const numericFields = [
  'mean_quality', 'mean_utility', 'mean_faithfulness', 'mean_concept_coverage',
  'mean_final_length_error_pct', 'mean_first_pass_length_error_pct', 'mean_passes_used',
  'mean_generation_cost', 'mean_uncached_cost', 'mean_llm_judge_cost', 'mean_total_cost', 'hard_fail_rate', 'n_genre_macros',
  'genre_macro_spread_utility', 'avg_time_per_chapter_seconds'
]

const modeDefaults = {
  price_quality: { x: 'mean_generation_cost', y: 'mean_quality', size: 'mean_passes_used' },
  price_faithfulness: { x: 'mean_generation_cost', y: 'mean_faithfulness', size: 'mean_passes_used' },
  custom: { x: 'mean_quality', y: 'mean_utility', size: 'mean_passes_used' }
}

async function findRunJsonFiles() {
  try {
    const response = await fetch('/runs-list')
    if (!response.ok) throw new Error('Failed to fetch runs list')
    const files = await response.json()
    return files.filter(f => f.endsWith('.json') && !f.includes('/mock/') && !f.endsWith('.state.json'))
  } catch {
    return []
  }
}

async function loadRuns(doRender = true) {
  try {
    const files = await findRunJsonFiles()
    runs = []

    for (const file of files) {
      try {
        const response = await fetch(`/runs/${file}`)
        if (!response.ok) continue
        const data = await response.json()
        const manifest = data.run_manifest || {}
        const score = data.dataset_score || {}

        let updatedAtUtc = null
        try {
          const statePath = file.replace('.json', '.state.json')
          const stateRes = await fetch(`/runs/${statePath}`)
          if (stateRes.ok) {
            const stateData = await stateRes.json()
            updatedAtUtc = stateData.updated_at_utc ? new Date(stateData.updated_at_utc) : null
          }
        } catch (e) {
          console.warn(`State fetch error ${file}:`, e.message)
        }

        const createdAt = manifest.created_at_utc ? new Date(manifest.created_at_utc) : null
        const nSamples = score.n_samples || 0
        let avgTimePerChapterSeconds = null
        if (createdAt && updatedAtUtc && nSamples > 0) {
          const secs = (updatedAtUtc - createdAt) / 1000 / nSamples
          avgTimePerChapterSeconds = (isNaN(secs) || !isFinite(secs)) ? null : secs
        }

        const judgeVersion = manifest.judge_version_resolved || ''
        const isLlmJudged = !!(manifest.judge_model && judgeVersion.includes('judge-absolute-v1'))
        const judgeModelSlug = isLlmJudged ? manifest.judge_model.split('/').pop() : ''

        const run = {
          run_id: manifest.run_id || file.replace('.json', ''),
          profile: manifest.profile || '',
          bench: manifest.bench || '',
          candidate_name: manifest.candidate_name || '',
          chapter_model: manifest.chapter_model || '',
          composer_model: manifest.composer_model || '',
          benchmark_version: manifest.benchmark_version || '',
          provider: getProvider(manifest.chapter_model),
          model: manifest.chapter_model || '',
          judge_model: manifest.judge_model || '',
          judge_version_resolved: judgeVersion,
          is_llm_judged: isLlmJudged,
          judge_type: isLlmJudged ? `LLM:${judgeModelSlug}` : 'deterministic',
          n_samples: score.n_samples || 0,
          hard_fail_rate: score.hard_fail_rate ?? 0,
          mean_quality: score.mean_quality ?? 0,
          mean_utility: score.mean_utility ?? 0,
          mean_faithfulness: score.mean_faithfulness ?? 0,
          mean_concept_coverage: score.mean_concept_coverage ?? 0,
          mean_final_length_error_pct: score.mean_final_length_error_pct ?? 0,
          mean_first_pass_length_error_pct: score.mean_first_pass_length_error_pct ?? 0,
          mean_passes_used: score.mean_passes_used ?? 0,
          mean_uncached_cost: score.mean_uncached_cost ?? score.mean_uncached_generation_cost ?? manifest.mean_uncached_cost ?? 0,
          mean_generation_cost: manifest.mean_generation_cost ?? score.mean_generation_cost ?? 0,
          mean_llm_judge_cost: manifest.mean_llm_judge_cost ?? 0,
          mean_total_cost: manifest.mean_total_cost ?? 0,
          worst_genre_macro: score.worst_genre_macro?.slice_value || '',
          n_genre_macros: score.n_genre_macros || 0,
          genre_macro_spread_utility: score.genre_macro_spread_utility || 0,
          avg_time_per_chapter_seconds: avgTimePerChapterSeconds,
          file: file
        }
        runs.push(run)
      } catch (e) {
        console.warn(`Failed to load ${file}:`, e)
      }
    }

    runs.sort((a, b) => {
      const idA = `${a.run_id}__${a.judge_type}`
      const idB = `${b.run_id}__${b.judge_type}`
      return idA.localeCompare(idB)
    })
    populateRunSelect()
    populateSelects()
    if (doRender) renderChart()
  } catch (e) {
    console.error('Failed to load runs:', e)
  }
}

function populateRunSelect() {
  const select = document.getElementById('runSelect')
  select.innerHTML = ''

  const allOption = document.createElement('option')
  allOption.value = ''
  allOption.textContent = `All runs (${runs.length})`
  select.appendChild(allOption)

  runs.forEach(run => {
    const option = document.createElement('option')
    option.value = `${run.run_id}__${run.judge_type}`
    const judgeLabel = run.is_llm_judged ? ' [LLM]' : ' [det]'
    option.textContent = `${run.candidate_name || run.run_id}${judgeLabel} (${run.bench || run.benchmark_version || 'unknown'})`
    select.appendChild(option)
  })
}

function getRunKey(run) {
  return `${run.run_id}__${run.judge_type}`
}

function findRunByKey(runId, judgeType) {
  return runs.find(r => r.run_id === runId && r.judge_type === judgeType)
}

function populateSelects() {
  const xSelect = document.getElementById('xSelect')
  const ySelect = document.getElementById('ySelect')
  const sizeSelect = document.getElementById('sizeSelect')

  const fields = ['', ...numericFields]

    ;[xSelect, ySelect, sizeSelect].forEach((select, idx) => {
      const currentVal = select.value
      select.innerHTML = ''
      fields.forEach(f => {
        const opt = document.createElement('option')
        opt.value = f
        opt.textContent = f || '(none)'
        select.appendChild(opt)
      })
      if (idx === 0) select.value = modeDefaults[currentMode].x
      if (idx === 1) select.value = modeDefaults[currentMode].y
      if (idx === 2) select.value = modeDefaults[currentMode].size
    })
}

function getFilteredRuns() {
  let filtered = [...runs]

  const search = document.getElementById('searchInput').value.toLowerCase()
  if (search) {
    filtered = filtered.filter(r =>
      (r.candidate_name || '').toLowerCase().includes(search) ||
      (r.bench || '').toLowerCase().includes(search) ||
      (r.profile || '').toLowerCase().includes(search) ||
      (r.run_id || '').toLowerCase().includes(search)
    )
  }

  const selectedRun = document.getElementById('runSelect').value
  if (selectedRun) {
    const [rid, jtype] = selectedRun.split('__')
    filtered = filtered.filter(r => r.run_id === rid && r.judge_type === jtype)
  }

  if (!judgeVisibility.LLM) {
    filtered = filtered.filter(r => !r.judge_type.startsWith('LLM'))
  }
  if (!judgeVisibility.deterministic) {
    filtered = filtered.filter(r => r.judge_type !== 'deterministic')
  }

  return filtered
}

function renderChart() {
  const svg = document.getElementById('chart')
  const filtered = getFilteredRuns()
  const filteredCount = document.getElementById('filteredCount')
  if (filteredCount) filteredCount.textContent = `${filtered.length} runs`

  const xField = document.getElementById('xSelect').value || 'mean_generation_cost'
  const yField = document.getElementById('ySelect').value || 'mean_quality'
  const sizeField = document.getElementById('sizeSelect').value || 'mean_passes_used'
  const colorField = document.getElementById('colorSelect').value || 'provider'
  const labelField = document.getElementById('labelSelect').value || 'candidate_name'

  svg.innerHTML = ''

  const data = filtered.map(r => ({
    run: r,
    x: r[xField] ?? 0,
    y: r[yField] ?? 0,
    size: sizeField ? (r[sizeField] ?? 1) : 1,
    colorVal: r[colorField] || 'unknown'
  })).filter(d => d.x !== undefined && d.y !== undefined && !activeFilters[colorField]?.includes(d.colorVal))

  if (data.length === 0) return

  const xMin = Math.min(...data.map(d => d.x))
  const xMax = Math.max(...data.map(d => d.x))
  const yMin = fixedYRange ? 0 : Math.min(...data.map(d => d.y))
  const yMax = fixedYRange ? 1 : Math.max(...data.map(d => d.y))

  const xPad = (xMax - xMin) * 0.1 || 0.1
  const yPad = (yMax - yMin) * 0.1 || 0.1

  const margin = { top: 20, right: 20, bottom: 50, left: 60 }
  const width = 980 - margin.left - margin.right
  const height = 620 - margin.top - margin.bottom

  const xScale = v => margin.left + ((v - (xMin - xPad)) / (xMax - xMin + 2 * xPad)) * width
  const yScale = v => margin.top + (1 - (v - (yMin - yPad)) / (yMax - yMin + 2 * yPad)) * height

  const xDataMin = xMin - xPad
  const xDataMax = xMax + xPad
  const yDataMin = yMin - yPad
  const yDataMax = yMax + yPad

  document.getElementById('xRange').textContent = `${xMin.toFixed(4)} – ${xMax.toFixed(4)}`
  document.getElementById('yRange').textContent = `${yMin.toFixed(4)} – ${yMax.toFixed(4)}`

  const g = document.createElementNS('http://www.w3.org/2000/svg', 'g')

  const gridStep = Math.max(0.01, (xDataMax - xDataMin) / 8)
  const xGridStart = Math.ceil(xDataMin / gridStep) * gridStep
  for (let x = xGridStart; x <= xDataMax + 1e-9; x += gridStep) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line')
    line.setAttribute('x1', xScale(x))
    line.setAttribute('x2', xScale(x))
    line.setAttribute('y1', margin.top)
    line.setAttribute('y2', height + margin.top)
    line.setAttribute('class', 'grid-line')
    g.appendChild(line)

    const tick = document.createElementNS('http://www.w3.org/2000/svg', 'text')
    tick.setAttribute('x', xScale(x))
    tick.setAttribute('y', height + margin.top + 14)
    tick.setAttribute('class', 'tick-label')
    tick.setAttribute('text-anchor', 'middle')
    tick.textContent = x.toFixed(3)
    g.appendChild(tick)
  }

  const yGridStep = Math.max(0.01, (yDataMax - yDataMin) / 6)
  const yGridStart = Math.ceil(yDataMin / yGridStep) * yGridStep
  for (let y = yGridStart; y <= yDataMax + 1e-9; y += yGridStep) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line')
    line.setAttribute('x1', margin.left)
    line.setAttribute('x2', width + margin.left)
    line.setAttribute('y1', yScale(y))
    line.setAttribute('y2', yScale(y))
    line.setAttribute('class', 'grid-line')
    g.appendChild(line)

    const tick = document.createElementNS('http://www.w3.org/2000/svg', 'text')
    tick.setAttribute('x', margin.left - 6)
    tick.setAttribute('y', yScale(y) + 4)
    tick.setAttribute('class', 'tick-label')
    tick.setAttribute('text-anchor', 'end')
    tick.textContent = y.toFixed(3)
    g.appendChild(tick)
  }

  const xAxis = document.createElementNS('http://www.w3.org/2000/svg', 'line')
  xAxis.setAttribute('x1', margin.left)
  xAxis.setAttribute('x2', width + margin.left)
  xAxis.setAttribute('y1', height + margin.top)
  xAxis.setAttribute('y2', height + margin.top)
  xAxis.setAttribute('class', 'axis-line')
  g.appendChild(xAxis)

  const yAxis = document.createElementNS('http://www.w3.org/2000/svg', 'line')
  yAxis.setAttribute('x1', margin.left)
  yAxis.setAttribute('x2', margin.left)
  yAxis.setAttribute('y1', margin.top)
  yAxis.setAttribute('y2', height + margin.top)
  yAxis.setAttribute('class', 'axis-line')
  g.appendChild(yAxis)

  const xLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  xLabel.setAttribute('x', margin.left + width / 2)
  xLabel.setAttribute('y', height + margin.top + 40)
  xLabel.setAttribute('class', 'axis-label')
  xLabel.setAttribute('text-anchor', 'middle')
  xLabel.textContent = xField
  g.appendChild(xLabel)

  const xTickMin = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  xTickMin.setAttribute('x', margin.left)
  xTickMin.setAttribute('y', height + margin.top + 22)
  xTickMin.setAttribute('class', 'tick-label')
  xTickMin.setAttribute('text-anchor', 'middle')
  xTickMin.textContent = xDataMin.toFixed(3)
  g.appendChild(xTickMin)

  const xTickMax = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  xTickMax.setAttribute('x', margin.left + width)
  xTickMax.setAttribute('y', height + margin.top + 22)
  xTickMax.setAttribute('class', 'tick-label')
  xTickMax.setAttribute('text-anchor', 'middle')
  xTickMax.textContent = xDataMax.toFixed(3)
  g.appendChild(xTickMax)

  const yLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  yLabel.setAttribute('x', 15)
  yLabel.setAttribute('y', margin.top + height / 2)
  yLabel.setAttribute('class', 'axis-label')
  yLabel.setAttribute('text-anchor', 'middle')
  yLabel.setAttribute('transform', `rotate(-90, 15, ${margin.top + height / 2})`)
  yLabel.textContent = yField
  g.appendChild(yLabel)

  const yTickMin = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  yTickMin.setAttribute('x', margin.left - 8)
  yTickMin.setAttribute('y', height + margin.top)
  yTickMin.setAttribute('class', 'tick-label')
  yTickMin.setAttribute('text-anchor', 'end')
  yTickMin.textContent = yDataMin.toFixed(3)
  g.appendChild(yTickMin)

  const yTickMax = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  yTickMax.setAttribute('x', margin.left - 8)
  yTickMax.setAttribute('y', margin.top + 4)
  yTickMax.setAttribute('class', 'tick-label')
  yTickMax.setAttribute('text-anchor', 'end')
  yTickMax.textContent = yDataMax.toFixed(3)
  g.appendChild(yTickMax)

  const sizeMax = Math.max(...data.map(d => d.size), 1)
  const sizeScale = s => 5 + (s / sizeMax) * 25

  if (highlightQuadrant && xField.includes('cost') && (yField === 'mean_quality' || yField === 'mean_faithfulness')) {
    const quad = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
    const xMid = (xMin + xMax) / 2
    const yMid = (yMin + yMax) / 2
    quad.setAttribute('x', xScale(xDataMin))
    quad.setAttribute('y', yScale(yDataMax))
    quad.setAttribute('width', xScale(xMid) - xScale(xDataMin))
    quad.setAttribute('height', yScale(yMid) - yScale(yDataMax))
    quad.setAttribute('class', 'quad')
    g.appendChild(quad)
  }

  data.forEach(d => {
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle')
    circle.setAttribute('cx', xScale(d.x))
    circle.setAttribute('cy', yScale(d.y))
    circle.setAttribute('r', sizeScale(d.size))
    circle.setAttribute('fill', getColor(colorField, d.colorVal))
    if (d.run.is_llm_judged) {
      circle.style.stroke = '#8b5cf6'
      circle.style.strokeWidth = '3'
    }
    circle.setAttribute('class', 'point')
    circle.dataset.runId = d.run.run_id
    circle.dataset.judgeType = d.run.judge_type
    circle.addEventListener('mouseenter', e => showTooltip(e, d))
    circle.addEventListener('mouseleave', hideTooltip)
    circle.addEventListener('click', () => selectRun(d.run.run_id, d.run.judge_type, false))
    circle.addEventListener('dblclick', () => selectRun(d.run.run_id, d.run.judge_type, true))
    g.appendChild(circle)

    if (showLabels) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text')
      text.setAttribute('x', xScale(d.x) + sizeScale(d.size) + 3)
      text.setAttribute('y', yScale(d.y) + 4)
      text.setAttribute('class', 'tick-label')
      text.textContent = (d.run[labelField] || d.run.candidate_name || d.run.run_id).substring(0, 12)
      g.appendChild(text)
    }
  })

  svg.appendChild(g)
  const allData = filtered.map(r => ({
    run: r,
    x: r[xField] ?? 0,
    y: r[yField] ?? 0,
    size: sizeField ? (r[sizeField] ?? 1) : 1,
    colorVal: r[colorField] || 'unknown'
  })).filter(d => d.x !== undefined && d.y !== undefined)
  renderLegend(allData, colorField)
}

function showTooltip(e, d) {
  const tooltip = document.getElementById('tooltip')
  const plotWrap = document.getElementById('chart').parentElement
  const rect = plotWrap.getBoundingClientRect()
  const run = d.run
  const judgeTag = run.is_llm_judged
    ? `<span style="background:#8b5cf6;color:white;border-radius:3px;padding:1px 6px;font-size:11px">LLM:${run.judge_model.split('/').pop()}</span>`
    : `<span style="background:#6b7280;color:white;border-radius:3px;padding:1px 6px;font-size:11px">det</span>`
  tooltip.innerHTML = `
    <strong>${run.candidate_name || run.run_id}</strong> ${judgeTag}
    <div>X: ${d.x.toFixed(4)} (${document.getElementById('xSelect').value})</div>
    <div>Y: ${d.y.toFixed(4)} (${document.getElementById('ySelect').value})</div>
    <div>Bubble: ${d.size.toFixed(2)} (${document.getElementById('sizeSelect').value})</div>
    ${run.avg_time_per_chapter_seconds != null ? `<div>avg_time/chap: ${run.avg_time_per_chapter_seconds.toFixed(1)}s</div>` : ''}
    ${run.mean_generation_cost > 0 ? `<div>gen_cost: ${run.mean_generation_cost.toFixed(4)}</div>` : ''}
    ${run.mean_llm_judge_cost > 0 ? `<div>judge_cost: ${run.mean_llm_judge_cost.toFixed(4)}</div>` : ''}
    ${run.mean_total_cost > 0 ? `<div>total_cost: ${run.mean_total_cost.toFixed(4)}</div>` : ''}
    <div style="margin-top:6px;color:#9ca3af">${run.bench || run.profile || ''}</div>
  `
  tooltip.style.display = 'block'

  const pointY = e.clientY - rect.top
  const tooltipH = tooltip.offsetHeight || 100
  const plotBottom = rect.height

  const offsetTop = pointY + tooltipH > plotBottom ? -tooltipH : 12
  const offsetLeft = 12

  tooltip.style.left = (e.clientX - rect.left + offsetLeft) + 'px'
  tooltip.style.top = (e.clientY - rect.top + offsetTop) + 'px'
}

function hideTooltip() {
  document.getElementById('tooltip').style.display = 'none'
}

function selectRun(runId, judgeType = '', openExplorer = false) {
  selectedRunId = runId
  selectedJudgeType = judgeType
  const run = runs.find(r => r.run_id === runId && r.judge_type === judgeType)
  if (!run) return

  if (openExplorer) {
    window.open(`/explorer.html?run_id=${encodeURIComponent(runId)}&judge_type=${encodeURIComponent(judgeType)}`, '_blank')
  }

  const card = document.getElementById('detailCard')
  const judgeTag = run.is_llm_judged
    ? `<span style="background:#8b5cf6;color:white;border-radius:3px;padding:1px 6px;font-size:11px;vertical-align:middle">LLM:${run.judge_model.split('/').pop()}</span>`
    : `<span style="background:#6b7280;color:white;border-radius:3px;padding:1px 6px;font-size:11px;vertical-align:middle">deterministic</span>`
  card.innerHTML = `
    <div class="detail-title">${run.candidate_name || run.run_id} ${judgeTag}</div>
    <div class="detail-meta">${run.bench || run.profile || ''} · ${run.n_samples} samples</div>
    <div class="kv">
      <div>run_id</div><div class="mono">${run.run_id}</div>
      <div>profile</div><div>${run.profile}</div>
      <div>bench</div><div>${run.bench}</div>
      <div>model</div><div>${run.model}</div>
      <div>judge</div><div>${run.judge_type}${run.judge_model ? ' · ' + run.judge_model.split('/').pop() : ''}</div>
      <div>mean_quality</div><div>${run.mean_quality.toFixed(4)}</div>
      <div>mean_utility</div><div>${run.mean_utility.toFixed(4)}</div>
      <div>mean_faithfulness</div><div>${run.mean_faithfulness.toFixed(4)}</div>
      <div>mean_concept_coverage</div><div>${run.mean_concept_coverage.toFixed(4)}</div>
      <div>mean_passes_used</div><div>${run.mean_passes_used.toFixed(2)}</div>
      <div>mean_generation_cost</div><div>${run.mean_generation_cost.toFixed(6)}</div>
      ${run.mean_llm_judge_cost > 0 ? `<div>mean_llm_judge_cost</div><div>${run.mean_llm_judge_cost.toFixed(6)}</div>` : ''}
      ${run.mean_total_cost > 0 ? `<div>mean_total_cost</div><div>${run.mean_total_cost.toFixed(6)}</div>` : ''}
      <div>hard_fail_rate</div><div>${run.hard_fail_rate.toFixed(2)}</div>
      ${run.avg_time_per_chapter_seconds != null ? `<div>avg_time_per_chapter_seconds</div><div>${run.avg_time_per_chapter_seconds.toFixed(1)}s</div>` : ''}
    </div>
    <button class="btn" id="explorerBtn" style="margin-top:12px;width:100%">Open in Run Explorer</button>
  `
  document.getElementById('explorerBtn')?.addEventListener('click', () => {
    window.open(`/explorer.html?run_id=${encodeURIComponent(run.run_id)}&judge_type=${encodeURIComponent(run.judge_type)}`, '_blank')
  })
  document.querySelectorAll('.point').forEach(p => {
    p.classList.toggle('active', p.dataset.runId === runId && p.dataset.judgeType === run.judge_type)
  })
}

function renderLegend(data, colorField) {
  const legend = document.getElementById('legend')
  legend.innerHTML = ''

  const uniqueValues = [...new Set(data.map(d => d.colorVal))]
  uniqueValues.forEach(val => {
    const item = document.createElement('button')
    item.className = 'legend-item' + (activeFilters[colorField]?.includes(val) ? ' off' : '')
    item.innerHTML = `<span class="swatch" style="background:${getColor(colorField, val)}"></span>${val}`
    item.addEventListener('click', () => {
      if (!activeFilters[colorField]) activeFilters[colorField] = []
      const idx = activeFilters[colorField].indexOf(val)
      if (idx >= 0) activeFilters[colorField].splice(idx, 1)
      else activeFilters[colorField].push(val)
      item.classList.toggle('off')
      renderChart()
      persistState()
    })
    legend.appendChild(item)
  })
}

function updateMode(mode) {
  currentMode = mode
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode))

  const defaults = modeDefaults[mode] || modeDefaults.custom
  document.getElementById('xSelect').value = defaults.x
  document.getElementById('ySelect').value = defaults.y
  document.getElementById('sizeSelect').value = defaults.size

  const titles = {
    price_quality: 'Price vs. Quality',
    price_faithfulness: 'Price vs. Faithfulness',
    custom: 'Custom'
  }
  document.getElementById('panelTitle').textContent = titles[mode]
  document.getElementById('panelSub').textContent = `X: ${defaults.x} · Y: ${defaults.y} · Bubble size: ${defaults.size}`

  renderChart()
  persistState()
}

let savedState = null

document.addEventListener('DOMContentLoaded', () => {
  savedState = loadPersistedState()

  if (savedState) {
    currentMode = savedState.currentMode || 'price_quality'
    selectedRunId = savedState.selectedRunId || null
    selectedJudgeType = savedState.selectedJudgeType || ''
    showLabels = savedState.showLabels ?? false
    highlightQuadrant = savedState.highlightQuadrant ?? true
    fixedYRange = savedState.fixedYRange ?? false
    judgeVisibility = savedState.judgeVisibility ?? { deterministic: true, LLM: true }
    activeFilters = savedState.activeFilters || {}
    explicitFieldChanges = { xField: false, yField: false, sizeField: false, colorField: false, labelField: false }

    document.getElementById('searchInput').value = savedState.searchText || ''
    if (savedState.selectedRunId && savedState.selectedJudgeType) {
      document.getElementById('runSelect').value = `${savedState.selectedRunId}__${savedState.selectedJudgeType}`
    }
    document.getElementById('labelToggle').checked = showLabels
    document.getElementById('quadToggle').checked = highlightQuadrant
    document.getElementById('fixedYRange').checked = fixedYRange

    document.querySelectorAll('.judge-toggle').forEach(btn => {
      const judge = btn.dataset.judge
      const isActive = judgeVisibility[judge] ?? true
      btn.classList.toggle('jt-off', !isActive)
      btn.classList.toggle('active', isActive)
    })

    document.querySelectorAll('.tab').forEach(t => {
      t.classList.toggle('active', t.dataset.mode === currentMode)
    })

    const titles = {
      price_quality: 'Price vs. Quality',
      price_faithfulness: 'Price vs. Faithfulness',
      custom: 'Custom'
    }
    document.getElementById('panelTitle').textContent = titles[currentMode] || 'Custom'
  }

  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      updateMode(tab.dataset.mode)
      persistState()
    })
  })

    ;['xSelect', 'ySelect', 'sizeSelect', 'colorSelect', 'labelSelect'].forEach(id => {
      document.getElementById(id)?.addEventListener('change', e => {
        const fieldMap = { xSelect: 'xField', ySelect: 'yField', sizeSelect: 'sizeField', colorSelect: 'colorField', labelSelect: 'labelField' }
        const field = fieldMap[e.target.id]
        if (field) explicitFieldChanges[field] = true
        renderChart()
        persistState()
      })
    })

  document.getElementById('labelToggle')?.addEventListener('change', e => {
    showLabels = e.target.checked
    renderChart()
    persistState()
  })

  document.getElementById('quadToggle')?.addEventListener('change', e => {
    highlightQuadrant = e.target.checked
    renderChart()
    persistState()
  })

  document.getElementById('fixedYRange')?.addEventListener('change', e => {
    fixedYRange = e.target.checked
    renderChart()
    persistState()
  })

  document.querySelectorAll('.judge-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const judge = btn.dataset.judge
      const isCurrentlyEnabled = !btn.classList.contains('jt-off')
      btn.classList.toggle('jt-off', isCurrentlyEnabled)
      btn.classList.toggle('active', !isCurrentlyEnabled)
      judgeVisibility[judge] = !isCurrentlyEnabled
      renderChart()
      persistState()
    })
  })

  document.getElementById('searchInput')?.addEventListener('input', () => {
    renderChart()
    persistState()
  })
  document.getElementById('runSelect')?.addEventListener('change', () => {
    const val = document.getElementById('runSelect').value
    const [rid, jtype] = val ? val.split('__') : ['', '']
    selectedRunId = rid || null
    selectedJudgeType = jtype || ''
    selectRun(selectedRunId, selectedJudgeType)
    renderChart()
    persistState()
  })

  document.getElementById('downloadBtn')?.addEventListener('click', () => {
    const svg = document.getElementById('chart')
    const svgData = new XMLSerializer().serializeToString(svg)
    const blob = new Blob([svgData], { type: 'image/svg+xml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'chart.svg'
    a.click()
    URL.revokeObjectURL(url)
  })

  let isFullscreen = false
  document.getElementById('fullscreenBtn')?.addEventListener('click', () => {
    isFullscreen = !isFullscreen
    document.body.classList.toggle('fullscreen-mode', isFullscreen)
    document.getElementById('fullscreenBtn')?.classList.toggle('expanded', isFullscreen)
  })

  if (savedState) {
    loadRuns(false).then(() => {
      const titles = {
        price_quality: 'Price vs. Quality',
        price_faithfulness: 'Price vs. Faithfulness',
        custom: 'Custom'
      }
      document.getElementById('panelTitle').textContent = titles[currentMode] || 'Custom'

      document.getElementById('xSelect').value = savedState.xField || modeDefaults[currentMode].x
      document.getElementById('ySelect').value = savedState.yField || modeDefaults[currentMode].y
      document.getElementById('sizeSelect').value = savedState.sizeField || modeDefaults[currentMode].size
      document.getElementById('colorSelect').value = savedState.colorField || 'provider'
      document.getElementById('labelSelect').value = savedState.labelField || 'candidate_name'

      document.getElementById('panelSub').textContent = `X: ${document.getElementById('xSelect').value} · Y: ${document.getElementById('ySelect').value} · Bubble size: ${document.getElementById('sizeSelect').value}`

      renderChart()
      savedState = null
      explicitFieldChanges = { xField: false, yField: false, sizeField: false, colorField: false, labelField: false }
    })
  } else {
    loadRuns()
  }
})
