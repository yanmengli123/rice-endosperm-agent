import assert from 'node:assert/strict'
import {
  canvasPoint,
  graphCameraPosition,
  graphGeometry,
  graphNodeRadius,
  graphNodeColor,
  graphNodeLabel
} from '../literature_graph.js'
const rect = { left: 20, top: 30, width: 450, height: 280 }
assert.deepEqual(canvasPoint(245, 170, rect), { x: 450, y: 280 })
assert.deepEqual(canvasPoint(245, 170, rect, { x: 50, y: 80, scale: 2 }), { x: 200, y: 100 })
assert.equal(canvasPoint(0, 0, { ...rect, width: 0 }), null)
assert.equal(canvasPoint(0, 0, rect, { x: 0, y: 0, scale: 0 }), null)
assert.deepEqual(graphCameraPosition('top'), [0, 330, 0.01])
assert.deepEqual(graphCameraPosition('bad'), [0, 75, 310])
const nodes = [{ rid: 'a' }, { rid: 'b' }, { rid: 'c' }]
for (const layout of ['circular', 'grid', 'random']) {
  const positions = graphGeometry(nodes, layout)
  assert.equal(positions.length, 3)
  assert.ok(positions.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y)))
  assert.deepEqual(positions, graphGeometry(nodes, layout), 'Reloading geometry is deterministic')
  assert.equal(nodes[0].x, undefined, 'Layout cannot mutate canonical network data')
}
assert.deepEqual(graphGeometry([], 'grid'), [])
assert.equal(graphNodeRadius({ citations: -10 }), 4)
assert.equal(graphNodeRadius({ citations: 1e10 }, { scaleAlgo: 'linear', sizeBase: 4 }), 40)
assert.equal(
  graphNodeRadius({ citations: 999 }, { scaleAlgo: 'uniform' }),
  graphNodeRadius({ citations: 0 }, { scaleAlgo: 'uniform' })
)
assert.notEqual(
  graphNodeColor({ quartile: 'Q1' }, {}, false),
  graphNodeColor({ quartile: 'Q1' }, {}, true)
)
assert.match(graphNodeColor({ impact_factor: 12 }, { colorMode: 'if' }), /^#[0-9a-f]{6}$/)
assert.notEqual(
  graphNodeColor({ cocitation_cluster: 0 }, { colorMode: 'cluster' }),
  graphNodeColor({}, { colorMode: 'cluster' })
)
assert.equal(graphNodeLabel({ rid: 'a', title: 'Title' }, 'selected', 'b'), '')
assert.equal(graphNodeLabel({ rid: 'a', year: 2024, citations: 9 }, 'year_cited'), '2024 · 9')
console.log('Graph responsive coordinates, transformed picking and camera presets passed')
