import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  applyPromptFilter,
  groupPromptVersions,
  modelSpecificLabel,
  naturalPromptCompare,
  presentPromptVersions,
  promptVersionColor,
  promptVersionLabel,
  versionVisibilityKey,
  normalizePromptVersion,
  migratePromptVisibility,
  LEGACY_PROMPT_VERSION,
  MODEL_SCOPE_KEY,
} from '../promptFilters.js'

const RUNS = [
  { run_id: 'a', prompt_version: 'chapter-p1' },
  { run_id: 'b', prompt_version: 'chapter-p2' },
  { run_id: 'c', prompt_version: 'chapter-p10' },
  { run_id: 'd', prompt_version: '' },
]

// 17 — legacy (empty) runs are v1, so they fold into chapter-p1
test('presentPromptVersions treats legacy runs as the v1 baseline', () => {
  const present = presentPromptVersions(RUNS)
  assert.deepEqual(present, ['chapter-p1', 'chapter-p2', 'chapter-p10'])
  assert.equal(LEGACY_PROMPT_VERSION, 'chapter-p1')
  assert.equal(normalizePromptVersion(''), 'chapter-p1')
  assert.equal(normalizePromptVersion('chapter-p2'), 'chapter-p2')
  assert.equal(promptVersionLabel(''), 'chapter-p1')
  assert.equal(promptVersionLabel('chapter-p2'), 'chapter-p2')
})

test('naturalPromptCompare orders numeric suffixes', () => {
  assert.ok(naturalPromptCompare('chapter-p1', 'chapter-p2') < 0)
  assert.ok(naturalPromptCompare('chapter-p2', 'chapter-p10') < 0)
  assert.ok(naturalPromptCompare('chapter-p2', '') < 0)
})

test('promptVersionColor maps legacy to the v1 color and stays stable', () => {
  assert.equal(promptVersionColor('chapter-p1'), '#6b7280')
  assert.equal(promptVersionColor('chapter-p2'), '#10b981')
  assert.equal(promptVersionColor(''), '#6b7280')
  assert.equal(promptVersionColor('chapter-p99'), promptVersionColor('chapter-p99'))
})

// 18/19 — hiding filters exactly those runs; all-off yields empty
test('applyPromptFilter hides selected versions', () => {
  const hiddenOne = applyPromptFilter(RUNS, { 'chapter-p1': false })
  assert.deepEqual(hiddenOne.map(r => r.run_id), ['b', 'c'])

  const hiddenAll = applyPromptFilter(RUNS, { 'chapter-p1': false, 'chapter-p2': false, 'chapter-p10': false })
  assert.deepEqual(hiddenAll, [])
})

test('a legacy empty key hides the v1 runs', () => {
  const hidden = applyPromptFilter(RUNS, { '': false })
  assert.deepEqual(hidden.map(r => r.run_id), ['b', 'c'])
})

test('applyPromptFilter with no hidden keys returns all runs', () => {
  assert.deepEqual(applyPromptFilter(RUNS, {}).map(r => r.run_id), ['a', 'b', 'c', 'd'])
  assert.deepEqual(applyPromptFilter(RUNS, { 'chapter-p2': true }).map(r => r.run_id), ['a', 'b', 'c', 'd'])
})

// 21 — a legacy run set is all v1
test('legacy run set presents a single v1 bucket', () => {
  const legacy = [{ run_id: 'x', prompt_version: '' }, { run_id: 'y', prompt_version: '' }]
  const present = presentPromptVersions(legacy)
  assert.deepEqual(present, ['chapter-p1'])
})

// 22 — model-scoped versions collapse into one aggregate toggle group
test('groupPromptVersions splits general and model-specific, folding legacy to v1', () => {
  const present = ['chapter-p1', 'chapter-p2', 'chapter-p3', 'composer-p1', '']
  const scope = { 'chapter-p3': 'model', 'composer-p1': 'model', 'chapter-p1': 'general', 'chapter-p2': 'general' }
  const grouped = groupPromptVersions(present, scope)
  assert.deepEqual(grouped.general, ['chapter-p1', 'chapter-p2'])
  assert.deepEqual(grouped.modelSpecificIds, ['chapter-p3', 'composer-p1'])
  assert.equal(modelSpecificLabel(grouped.modelSpecificIds), 'model-specific \u00b7 2')
  assert.equal(versionVisibilityKey('chapter-p2'), 'version:chapter-p2')
  assert.equal(versionVisibilityKey(''), 'version:chapter-p1')
  assert.equal(MODEL_SCOPE_KEY, 'scope:model')
})

test('an unknown version id groups as general', () => {
  const grouped = groupPromptVersions(['chapter-p9'], {})
  assert.deepEqual(grouped.general, ['chapter-p9'])
  assert.deepEqual(grouped.modelSpecificIds, [])
})

// 23 — turning the aggregate off hides only model-scoped runs
test('the aggregate toggle hides runs whose version is model-scoped', () => {
  const runs = [
    { run_id: 'g', prompt_version: 'chapter-p1' },
    { run_id: 'm', prompt_version: 'chapter-p3' },
    { run_id: 'u', prompt_version: '' },
  ]
  const scope = { 'chapter-p1': 'general', 'chapter-p3': 'model' }
  const filtered = applyPromptFilter(runs, { [MODEL_SCOPE_KEY]: false }, scope)
  assert.deepEqual(filtered.map(r => r.run_id), ['g', 'u'])
})

test('a general version toggle still hides exactly that version', () => {
  const runs = [
    { run_id: 'g', prompt_version: 'chapter-p1' },
    { run_id: 'm', prompt_version: 'chapter-p3' },
  ]
  const filtered = applyPromptFilter(runs, { [versionVisibilityKey('chapter-p1')]: false }, { 'chapter-p1': 'general', 'chapter-p3': 'model' })
  assert.deepEqual(filtered.map(r => r.run_id), ['m'])
})

test('turning off every general version and the model scope yields empty', () => {
  const runs = [
    { run_id: 'g1', prompt_version: 'chapter-p1' },
    { run_id: 'g2', prompt_version: 'chapter-p2' },
    { run_id: 'm', prompt_version: 'chapter-p3' },
    { run_id: 'u', prompt_version: '' },
  ]
  const scope = { 'chapter-p1': 'general', 'chapter-p2': 'general', 'chapter-p3': 'model' }
  const filtered = applyPromptFilter(runs, {
    [versionVisibilityKey('chapter-p1')]: false,
    [versionVisibilityKey('chapter-p2')]: false,
    [MODEL_SCOPE_KEY]: false,
  }, scope)
  assert.deepEqual(filtered, [])
})

test('migration-shaped keys (bare id, empty string, prefixed) all normalize to v1', () => {
  const runs = [{ run_id: 'v1', prompt_version: '' }, { run_id: 'v1b', prompt_version: 'chapter-p1' }]
  const viaBare = applyPromptFilter(runs, { 'chapter-p1': false })
  const viaEmpty = applyPromptFilter(runs, { '': false })
  const viaPrefixed = applyPromptFilter(runs, { [versionVisibilityKey('')]: false })
  assert.deepEqual(viaBare, [])
  assert.deepEqual(viaEmpty, [])
  assert.deepEqual(viaPrefixed, [])
})

// 24 — legacy preference keys (bare id, empty string) migrate to v1
test('migratePromptVisibility canonicalizes keys and folds empty to v1', () => {
  const migrated = migratePromptVisibility({
    'chapter-p2': false,
    '': false,
    'scope:model': false,
    'version:chapter-p4': true,
  })
  assert.deepEqual(migrated, {
    'version:chapter-p2': false,
    'version:chapter-p1': false,
    'scope:model': false,
    'version:chapter-p4': true,
  })
  assert.equal(migrated['version:'], undefined)
})
