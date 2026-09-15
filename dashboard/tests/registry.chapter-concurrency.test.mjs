import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SCRIPT_REGISTRY } from '../vite.config.js'

test('run_candidate exposes an opt-in chapter-concurrency arg', () => {
  const tool = SCRIPT_REGISTRY.find(t => t.id === 'run_candidate')
  assert.ok(tool, 'run_candidate tool missing')

  const concurrency = tool.args.find(a => a.name === 'chapter-concurrency')
  assert.ok(concurrency, 'chapter-concurrency arg missing')
  assert.equal(concurrency.type, 'int')
  assert.equal(concurrency.default, 1, 'default must stay sequential (opt-in)')
  assert.equal(concurrency.min, 1)
  assert.ok(concurrency.max >= 4, 'max should allow a useful degree of parallelism')
})

test('run_candidate ships a parallel preset that opts in to concurrency > 1', () => {
  const tool = SCRIPT_REGISTRY.find(t => t.id === 'run_candidate')
  const preset = (tool.presets || []).find(p => p.args && p.args['chapter-concurrency'] === 4)
  assert.ok(preset, 'parallel preset with chapter-concurrency 4 missing')
})
