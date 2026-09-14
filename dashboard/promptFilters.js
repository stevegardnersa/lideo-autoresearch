// Pure helpers for the explorer's system-prompt-version toggles (§21).
// Kept free of DOM access so they can be unit-tested without a browser.

export const PROMPT_VERSION_COLOR = {
  'chapter-p1': '#6b7280',
  'chapter-p2': '#10b981',
}

// Runs recorded before the prompt registry existed used the v1 baseline prompt
// (the profile defaults), which is seeded in the registry as chapter-p1. Treat
// the empty legacy value as v1 everywhere, so those runs and any saved
// preferences that used the empty key group with chapter-p1 instead of forming
// a separate bucket.
export const LEGACY_PROMPT_VERSION = 'chapter-p1'

export function normalizePromptVersion(value) {
  return value || LEGACY_PROMPT_VERSION
}

function hashColor(value) {
  let h = 0
  const s = String(value)
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h) + s.charCodeAt(i) | 0
  return `hsl(${Math.abs(h) % 360}, 55%, 48%)`
}

export function promptVersionColor(value) {
  const version = normalizePromptVersion(value)
  return PROMPT_VERSION_COLOR[version] || hashColor(version)
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
  const present = [...new Set((runs || []).map(r => normalizePromptVersion(r.prompt_version)))]
  present.sort(naturalPromptCompare)
  return present
}

export function promptVersionLabel(value) {
  return normalizePromptVersion(value)
}

// Visibility keys for the scope-aware toggles. General versions keep one toggle
// each (`version:<id>`); every model-scoped version collapses into the single
// `scope:model` toggle.
export const MODEL_SCOPE_KEY = 'scope:model'
export const VERSION_KEY_PREFIX = 'version:'

export function versionVisibilityKey(value) {
  return `${VERSION_KEY_PREFIX}${normalizePromptVersion(value)}`
}

// Legacy saved state keyed the toggles by bare version id (and used '' for
// pre-registry runs). Rewrite every entry to the canonical `version:<id>` key,
// folding the empty legacy key into the v1 baseline, so old preferences keep
// working and align with how legacy runs are grouped.
export function migratePromptVisibility(saved) {
  const out = {}
  for (const [key, value] of Object.entries(saved || {})) {
    if (key === MODEL_SCOPE_KEY) { out[key] = value; continue }
    const raw = key.startsWith(VERSION_KEY_PREFIX) ? key.slice(VERSION_KEY_PREFIX.length) : key
    out[versionVisibilityKey(raw)] = value
  }
  return out
}

// Split the present versions into individual general toggles and the
// model-scoped ids that back the single `scope:model` toggle. Legacy (v1)
// versions are already normalized to chapter-p1, so they group with general.
export function groupPromptVersions(present, versionScope = {}) {
  const general = []
  const modelSpecificIds = []
  const seen = new Set()
  for (const raw of present || []) {
    const value = normalizePromptVersion(raw)
    if (seen.has(value)) continue
    seen.add(value)
    if (versionScope[value] === 'model') modelSpecificIds.push(value)
    else general.push(value)
  }
  modelSpecificIds.sort(naturalPromptCompare)
  return { general, modelSpecificIds }
}

export function modelSpecificLabel(ids) {
  const n = (ids || []).length
  return n ? `model-specific \u00b7 ${n}` : 'model-specific'
}

// Returns the subset of runs whose prompt version is not explicitly hidden.
// Legacy callers may pass plain version ids as keys; new callers use the
// `version:<id>` / `scope:model` keys above. Empty ids and empty keys normalize
// to the v1 baseline so legacy runs and legacy preferences behave alike.
export function applyPromptFilter(runs, visibility, versionScope = {}) {
  const vis = visibility || {}
  const hiddenIds = new Set()
  let hideModelSpecific = false
  for (const key of Object.keys(vis)) {
    if (vis[key] !== false) continue
    if (key === MODEL_SCOPE_KEY) { hideModelSpecific = true; continue }
    const raw = key.startsWith(VERSION_KEY_PREFIX) ? key.slice(VERSION_KEY_PREFIX.length) : key
    hiddenIds.add(normalizePromptVersion(raw))
  }
  if (!hiddenIds.size && !hideModelSpecific) return [...(runs || [])]
  return (runs || []).filter(r => {
    const value = normalizePromptVersion(r.prompt_version)
    if (hiddenIds.has(value)) return false
    if (hideModelSpecific && versionScope[value] === 'model') return false
    return true
  })
}
