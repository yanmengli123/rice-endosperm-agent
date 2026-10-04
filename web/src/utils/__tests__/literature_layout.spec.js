import assert from 'node:assert/strict'
import { Worker } from 'node:worker_threads'
import { reactive } from 'vue'
import { createLiteratureLayout } from '../literature_layout_engine.js'
import { createLiteratureLayoutRunner } from '../literature_layout_runner.js'

const nodes = Array.from({ length: 8 }, (_, index) => ({ rid: `p-${index}`, citations: index }))
const edges = nodes.slice(1).map((node, index) => ({ source: nodes[index].rid, target: node.rid }))
for (const algorithm of ['fa2', 'force3d']) {
  const engine = createLiteratureLayout(nodes, edges, { algorithm })
  const original = engine.positions()
  const positions = engine.tick(60)
  assert.equal(positions.length, nodes.length * 3)
  assert.ok(positions.every(Number.isFinite), `${algorithm} coordinates must be finite`)
  assert.notDeepEqual(positions, original, `${algorithm} must actually compute, not return a seed`)
  const withoutLinks = createLiteratureLayout(nodes, [], { algorithm })
  assert.notDeepEqual(positions, withoutLinks.tick(60), 'Citations influence the actual layout')
  if (algorithm === 'force3d')
    assert.ok(
      nodes.some((_, index) => Math.abs(positions[index * 3 + 2]) > 0.1),
      '3D must have nonzero depth'
    )
  else assert.ok(nodes.every((_, index) => positions[index * 3 + 2] === 0))
  engine.dispose()
  withoutLinks.dispose()
}
assert.ok(
  nodes.every((node) => node.x === undefined),
  'Numerical engine must not mutate canonical metadata'
)
assert.throws(() => createLiteratureLayout([{ rid: 'same' }, { rid: 'same' }], []), /重复/)
assert.throws(() => createLiteratureLayout([], [], { algorithm: 'invented' }), /不支持/)

const workers = [],
  results = [],
  errors = []
const runner = createLiteratureLayoutRunner(
  (data) => results.push(data),
  (error) => errors.push(error),
  () => {
    const worker = {
      postMessage(data) {
        structuredClone(data)
      },
      terminate() {
        this.terminated = true
      }
    }
    workers.push(worker)
    return worker
  }
)
runner.start(
  reactive(nodes.map((node) => ({ ...node, metadata: { tags: ['ignored'] } }))),
  reactive(edges),
  { algorithm: 'fa2' }
)
assert.equal(errors.length, 0, 'Vue proxies must be unwrapped before structured clone')
const stale = workers[0]
runner.start(nodes, edges, { algorithm: 'force3d' })
assert.equal(stale.terminated, true)
stale.onmessage({ data: { positions: new Float32Array(nodes.length * 3), done: true } })
assert.equal(results.length, 0, 'Terminated generations cannot overwrite a new layout')
workers[1].onmessage({ data: { positions: new Float32Array([NaN]), done: true } })
assert.equal(errors.length, 1, 'Malformed replies fail closed')
runner.start(nodes, edges, {})
runner.stop()
workers[2].onmessage({ data: { positions: new Float32Array(nodes.length * 3) } })
assert.equal(results.length, 0, 'Unmount/pause must suppress late updates')

// Execute the actual module worker in an isolated Node worker, not a browser.
const url = new URL('../../workers/literature_layout.worker.js', import.meta.url).href
for (const algorithm of ['fa2', 'force3d']) {
  const worker = new Worker(
    `const { parentPort } = require('node:worker_threads');
    globalThis.postMessage = (data, transfer) => parentPort.postMessage(data, transfer);
    import(${JSON.stringify(url)}).then(() => {
      parentPort.on('message', data => globalThis.onmessage({ data })); parentPort.postMessage({ ready: true });
    });`,
    { eval: true }
  )
  try {
    const result = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('Layout worker timed out')), 10000)
      worker.on('error', (error) => {
        clearTimeout(timeout)
        reject(error)
      })
      worker.on('message', (data) => {
        if (data.ready) worker.postMessage({ nodes, edges, options: { algorithm, iterations: 30 } })
        else if (data.error) {
          clearTimeout(timeout)
          reject(Error(data.error))
        } else if (data.done) {
          clearTimeout(timeout)
          resolve(data)
        }
      })
    })
    assert.equal(result.positions.length, nodes.length * 3)
    assert.ok(result.positions.every(Number.isFinite))
    assert.equal(result.completed, 30)
  } finally {
    await worker.terminate()
  }
}
console.log(
  'Actual ForceAtlas2/3D engines and module workers, proxy cloning and cancellation passed'
)
