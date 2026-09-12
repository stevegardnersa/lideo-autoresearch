// Pure helpers for the explorer's system-prompt-version toggles (§21).
// Kept free of DOM access so they can be unit-tested without a browser.

export const PROMPT_VERSION_COLOR = {
  'chapter-p1': '#6b7280',
  'chapter-p2': '#10b981',
  unversioned: '#94a3b8',
}

function hashColor(value) {
  let h = 0
  const s = String(value)
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h) + s.charCodeAt(i) | 0
  return `hsl(${Math.abs(h) % 360}, 55%, 48%)`
}

export function promptVersionColor(value) {
  if (!value) return PROMPT_VERSION_COLOR.unversioned
  if (PROMPT_VERSION_COLOR[value]) return PROMPT_VERSION_COLOR[value]
  return hashColor(value)
}

export function naturalPromptCompare(a, b) {
  const rank = v => (v === '' ? 1 : 0)
  if (rank(a) !== rank(b)) return rank(a) - rank(b)
  const ma = String(a).match(/^([a-z]+)-p(\d+)$/)
  const mb = String(b).match(/^([a-z]+)-p(\d+)$/)
  if (ma && mb) {
    if (ma[1] !== mb[1]) return ma[1] < mb[1] ? -1 : 1
    return parseInt(ma[2], 10) - parseInt(mb[2], 10)
  }
  return String(a).localeCompare(String(b))
}

export function presentPromptVersions(runs) {
  const present = [...new Set((runs || []).map(r => r.prompt_version || ''))]
  present.sort(naturalPromptCompare)
  return present
}

export function promptVersionLabel(value) {
  return value || 'unversioned'
}

// Returns the subset of runs whose prompt_version is not explicitly hidden.
export function applyPromptFilter(runs, visibility) {
  const hidden = Object.keys(visibility || {}).filter(k => visibility[k] === false)
  if (!hidden.length) return [...(runs || [])]
  return (runs || []).filter(r => !hidden.includes(r.prompt_version || ''))
}
