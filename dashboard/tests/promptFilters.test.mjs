import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  applyPromptFilter,
  naturalPromptCompare,
  presentPromptVersions,
  promptVersionColor,
  promptVersionLabel,
} from '../promptFilters.js'

const RUNS = [
  { run_id: 'a', prompt_version: 'chapter-p1' },
  { run_id: 'b', prompt_version: 'chapter-p2' },
  { run_id: 'c', prompt_version: 'chapter-p10' },
  { run_id: 'd', prompt_version: '' },
]

// 17 — present versions are ordered naturally with unversioned last
test('presentPromptVersions orders versioned ids then unversioned', () => {
  const present = presentPromptVersions(RUNS)
  assert.deepEqual(present, ['chapter-p1', 'chapter-p2', 'chapter-p10', ''])
  assert.equal(promptVersionLabel(''), 'unversioned')
  assert.equal(promptVersionLabel('chapter-p2'), 'chapter-p2')
})

test('naturalPromptCompare orders numeric suffixes', () => {
  assert.ok(naturalPromptCompare('chapter-p1', 'chapter-p2') < 0)
  assert.ok(naturalPromptCompare('chapter-p2', 'chapter-p10') < 0)
  assert.ok(naturalPromptCompare('chapter-p2', '') < 0)
})

test('promptVersionColor is stable and uses seeded colors', () => {
  assert.equal(promptVersionColor('chapter-p1'), '#6b7280')
  assert.equal(promptVersionColor('chapter-p2'), '#10b981')
  assert.equal(promptVersionColor(''), '#94a3b8')
  assert.equal(promptVersionColor('chapter-p99'), promptVersionColor('chapter-p99'))
})

// 18/19 — hiding filters exactly those runs; all-off yields empty
test('applyPromptFilter hides selected versions', () => {
  const hiddenOne = applyPromptFilter(RUNS, { 'chapter-p1': false })
  assert.deepEqual(hiddenOne.map(r => r.run_id), ['b', 'c', 'd'])

  const hiddenAll = applyPromptFilter(RUNS, { 'chapter-p1': false, 'chapter-p2': false, 'chapter-p10': false, '': false })
  assert.deepEqual(hiddenAll, [])
})

test('applyPromptFilter with no hidden keys returns all runs', () => {
  assert.deepEqual(applyPromptFilter(RUNS, {}).map(r => r.run_id), ['a', 'b', 'c', 'd'])
  assert.deepEqual(applyPromptFilter(RUNS, { 'chapter-p2': true }).map(r => r.run_id), ['a', 'b', 'c', 'd'])
})

// 21 — legacy run set (all unversioned) offers no toggles
test('legacy run set has a single unversioned bucket', () => {
  const legacy = [{ run_id: 'x', prompt_version: '' }, { run_id: 'y', prompt_version: '' }]
  const present = presentPromptVersions(legacy)
  assert.deepEqual(present, [''])
  assert.equal(present.every(v => v === ''), true)
})
