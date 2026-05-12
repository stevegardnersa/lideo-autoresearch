import './style.css'

const COLOR_MAP = {
  'deepseek/deepseek-v4-pro': '#f97316',
  'deepseek/deepseek-v4-flash': '#fb923c',
  'deepseek/deepseek-v3.2': '#ea580c',
  'minimax/minimax-m2.7': '#22c55e',
  'openai/gpt-5-mini': '#6366f1',
  'openai/gpt-4o': '#8b5cf6',
  'google/gemini-3.1-pro-preview': '#14b8a6',
  'google/gemini-3-flash-preview': '#2dd4bf',
  'anthropic/claude-opus-4': '#0ea5e9',
  'anthropic/claude-sonnet-4': '#38bdf8',
  'anthropic/claude-sonnet-4.6': '#7dd3fc',
  'xiaomi/mimo-v2.5-pro': '#ec4899',
  'xiaomi/mimo-v2-flash': '#f472b6',
}

function getColor(key, value) {
  if (key === 'model' && COLOR_MAP[value]) return COLOR_MAP[value]
  let h = 0
  for (let i = 0; i < value.length; i++) h = ((h << 5) - h) + value.charCodeAt(i) | 0
  return `hsl(${Math.abs(h) % 360}, 55%, 48%)`
}

function getProvider(model) {
  if (!model) return 'unknown'
  return model.split('/')[0]
}

let runs = []
let currentMode = 'price_quality'
let activeFilters = {}
let showLabels = false
let highlightQuadrant = true
let fixedYRange = false
let selectedRunId = null

const numericFields = [
  'mean_quality', 'mean_utility', 'mean_faithfulness', 'mean_concept_coverage',
  'mean_final_length_error_pct', 'mean_first_pass_length_error_pct', 'mean_passes_used',
  'mean_generation_cost', 'mean_uncached_cost', 'hard_fail_rate', 'n_genre_macros',
  'genre_macro_spread_utility'
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

async function loadRuns() {
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
          n_samples: score.n_samples || 0,
          hard_fail_rate: score.hard_fail_rate ?? 0,
          mean_quality: score.mean_quality ?? 0,
          mean_utility: score.mean_utility ?? 0,
          mean_faithfulness: score.mean_faithfulness ?? 0,
          mean_concept_coverage: score.mean_concept_coverage ?? 0,
          mean_final_length_error_pct: score.mean_final_length_error_pct ?? 0,
          mean_first_pass_length_error_pct: score.mean_first_pass_length_error_pct ?? 0,
          mean_passes_used: score.mean_passes_used ?? 0,
          mean_uncached_cost: score.mean_uncached_cost ?? score.mean_uncached_generation_cost ?? 0,
          mean_generation_cost: score.mean_generation_cost ?? 0,
          worst_genre_macro: score.worst_genre_macro?.slice_value || '',
          n_genre_macros: score.n_genre_macros || 0,
          genre_macro_spread_utility: score.genre_macro_spread_utility || 0,
          file: file
        }
        runs.push(run)
      } catch (e) {
        console.warn(`Failed to load ${file}:`, e)
      }
    }

    runs.sort((a, b) => a.run_id.localeCompare(b.run_id))
    populateRunSelect()
    populateSelects()
    renderChart()
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
    option.value = run.run_id
    option.textContent = `${run.candidate_name || run.run_id} (${run.bench || run.benchmark_version || 'unknown'})`
    select.appendChild(option)
  })
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
    filtered = filtered.filter(r => r.run_id === selectedRun)
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
    circle.setAttribute('class', 'point')
    circle.dataset.runId = d.run.run_id
    circle.addEventListener('mouseenter', e => showTooltip(e, d))
    circle.addEventListener('mouseleave', hideTooltip)
    circle.addEventListener('click', () => selectRun(d.run.run_id, true))
    g.appendChild(circle)

    if (showLabels) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text')
      text.setAttribute('x', xScale(d.x) + sizeScale(d.size) + 3)
      text.setAttribute('y', yScale(d.y) + 4)
      text.setAttribute('class', 'tick-label')
      text.textContent = (d.run.candidate_name || d.run.run_id).substring(0, 12)
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
  tooltip.innerHTML = `
    <strong>${run.candidate_name || run.run_id}</strong>
    <div>X: ${d.x.toFixed(4)} (${document.getElementById('xSelect').value})</div>
    <div>Y: ${d.y.toFixed(4)} (${document.getElementById('ySelect').value})</div>
    <div>Bubble: ${d.size.toFixed(2)}</div>
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

function selectRun(runId, openExplorer = false) {
  selectedRunId = runId
  const run = runs.find(r => r.run_id === runId)
  if (!run) return

  if (openExplorer) {
    window.open(`/explorer.html?run_id=${encodeURIComponent(runId)}`, '_blank')
  }

  const card = document.getElementById('detailCard')
  card.innerHTML = `
    <div class="detail-title">${run.candidate_name || run.run_id}</div>
    <div class="detail-meta">${run.bench || run.profile || ''} · ${run.n_samples} samples</div>
    <div class="kv">
      <div>run_id</div><div class="mono">${run.run_id}</div>
      <div>profile</div><div>${run.profile}</div>
      <div>bench</div><div>${run.bench}</div>
      <div>model</div><div>${run.model}</div>
      <div>mean_quality</div><div>${run.mean_quality.toFixed(4)}</div>
      <div>mean_utility</div><div>${run.mean_utility.toFixed(4)}</div>
      <div>mean_faithfulness</div><div>${run.mean_faithfulness.toFixed(4)}</div>
      <div>mean_concept_coverage</div><div>${run.mean_concept_coverage.toFixed(4)}</div>
      <div>mean_passes_used</div><div>${run.mean_passes_used.toFixed(2)}</div>
      <div>mean_generation_cost</div><div>${run.mean_generation_cost.toFixed(6)}</div>
      <div>hard_fail_rate</div><div>${run.hard_fail_rate.toFixed(2)}</div>
    </div>
    <button class="btn" id="explorerBtn" style="margin-top:12px;width:100%">Open in Run Explorer</button>
  `
  document.getElementById('explorerBtn')?.addEventListener('click', () => {
    window.open(`/explorer.html?run_id=${encodeURIComponent(runId)}`, '_blank')
  })
  document.querySelectorAll('.point').forEach(p => {
    p.classList.toggle('active', p.dataset.runId === runId)
  })
}

function renderLegend(data, colorField) {
  const legend = document.getElementById('legend')
  legend.innerHTML = ''

  const uniqueValues = [...new Set(data.map(d => d.colorVal))]
  uniqueValues.slice(0, 15).forEach(val => {
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
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => updateMode(tab.dataset.mode))
  })

  ;['xSelect', 'ySelect', 'sizeSelect', 'colorSelect'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', renderChart)
  })

  document.getElementById('labelToggle')?.addEventListener('change', e => {
    showLabels = e.target.checked
    renderChart()
  })

  document.getElementById('quadToggle')?.addEventListener('change', e => {
    highlightQuadrant = e.target.checked
    renderChart()
  })

  document.getElementById('fixedYRange')?.addEventListener('change', e => {
    fixedYRange = e.target.checked
    renderChart()
  })

  document.getElementById('searchInput')?.addEventListener('input', renderChart)
  document.getElementById('runSelect')?.addEventListener('change', renderChart)

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

  loadRuns()
})
