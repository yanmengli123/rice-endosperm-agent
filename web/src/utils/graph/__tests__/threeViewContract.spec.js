import assert from 'node:assert/strict'
import process from 'node:process'
import { applyTypeLimit, buildChordData } from '../chartAggregations.js'
import { buildHeatmapOption, buildSankeyOption } from '../chartOptions.js'
import { formatSankeyTooltip } from '../chartTooltips.js'

const node = (id, type) => ({ id, type, node_kind: 'entity' })
const edge = (id, source_id, target_id, type = 'REGULATES') => ({
  id,
  triple_id: id,
  source_id,
  target_id,
  type,
  edge_kind: 'relation'
})
const theme = {
  isDark: false,
  lighten: (c) => c,
  largeScale: false,
  labelColor: '#333',
  arcBorderColor: '#fff',
  ribbonOpacity: 0.6,
  sankeyLinkOpacity: 0.4,
  heatRamp: ['#fff', '#000']
}
let failed = 0,
  passed = 0
function test(name, fn) {
  try {
    fn()
    passed++
    console.log(`✓ ${name}`)
  } catch (e) {
    failed++
    console.error(`✗ ${name}\n${e.stack}`)
  }
}

test('孤立类型保留，实体数、内部与跨类型关系可以对账', () => {
  const data = buildChordData(
    {
      nodes: [node('a', 'A'), node('b', 'B'), node('c', 'A'), node('z', 'Isolated')],
      edges: [
        edge('ab', 'a', 'b'),
        edge('ba', 'b', 'a', 'INHIBITS'),
        edge('aa', 'a', 'c'),
        edge('ab', 'a', 'b')
      ]
    },
    { maxTypes: 0 }
  )
  assert.equal(data.meta.entityNodes, 4)
  assert.equal(data.meta.typeCount, 3)
  assert.equal(data.nodes.find((n) => n.name === 'Isolated').entityCount, 1)
  assert.equal(
    data.directedLinks.reduce((n, l) => n + l.value, 0),
    3
  )
  assert.equal(data.meta.drawnRelationEdges, 3)
  assert.equal(
    data.directedLinks.find((l) => l.source === 'A' && l.target === 'A').semantics[0].edges[0]
      .triple_id,
    'aa'
  )
})

test('有向类型流只保留该方向谓词与原始事实身份', () => {
  const data = buildChordData(
    {
      nodes: [node('a', 'A'), node('b', 'B')],
      edges: [edge('ab', 'a', 'b', 'ACTIVATES'), edge('ba', 'b', 'a', 'INHIBITS')]
    },
    { maxTypes: 0 }
  )
  const link = data.directedLinks.find((l) => l.source === 'A')
  assert.deepEqual(
    link.semantics.map((s) => s.type),
    ['ACTIVATES']
  )
  assert.deepEqual(
    link.semantics.flatMap((s) => s.edges.map((e) => e.triple_id)),
    ['ab']
  )
  assert.equal(link.directed, true)
})

test('流向展示 ID 不与零宽字符业务类型碰撞，节点计数可读', () => {
  const data = buildChordData(
    {
      nodes: [node('a', 'A'), node('b', 'A\u200b')],
      edges: [edge('ab', 'a', 'b'), edge('ba', 'b', 'a')]
    },
    { maxTypes: 0 }
  )
  const series = buildSankeyOption(data, theme).series[0]
  assert.equal(new Set(series.data.map((n) => n.name)).size, 4)
  assert.equal(series.data.length, 4)
  assert.ok(series.data.some((n) => n.typeName === 'A\u200b'))
  const tip = formatSankeyTooltip({ dataType: 'node', data: series.data[0] })
  assert.ok(!tip.includes('undefined'))
  assert.ok(tip.includes('1 条'))
  for (const link of series.links) {
    assert.ok(series.data.find((n) => n.name === link.source).side === 'source')
    assert.ok(series.data.find((n) => n.name === link.target).side === 'target')
    assert.equal(link.semantics.length, 1)
  }
})

test('流向包含类型内部关系，矩阵对角线只计一次', () => {
  const data = buildChordData(
    { nodes: [node('a', 'A'), node('b', 'A')], edges: [edge('aa', 'a', 'b')] },
    { maxTypes: 0 }
  )
  const sankey = buildSankeyOption(data, theme).series[0]
  const matrix = buildHeatmapOption(data, theme).series[0]
  assert.equal(sankey.links.length, 1)
  assert.equal(sankey.links[0].value, 1)
  assert.deepEqual(
    matrix.data.map((c) => c.value),
    [[0, 0, 1]]
  )
  assert.equal(matrix.data[0].semantics[0].edges[0].triple_id, 'aa')
})

test('矩阵行源列目标，反向格保留各自独立的谓词与事实', () => {
  const data = buildChordData(
    {
      nodes: [node('a', 'A'), node('b', 'B')],
      edges: [edge('ab', 'a', 'b', 'ACTIVATES'), edge('ba', 'b', 'a', 'INHIBITS')]
    },
    { maxTypes: 0 }
  )
  const matrix = buildHeatmapOption(data, theme)
  const forward = matrix.series[0].data.find((c) => c.source === 'A')
  assert.deepEqual(forward.value, [1, 0, 1])
  assert.deepEqual(forward.relationTypes, [{ type: 'ACTIVATES', count: 1 }])
  assert.equal(forward.semantics[0].edges[0].triple_id, 'ab')
})

test('Top-N 损益含内部关系；矩阵大规模窗口保留全量稀疏数据', () => {
  const nodes = Array.from({ length: 60 }, (_, i) => node(`n${i}`, `T${i}`))
  const edges = nodes.map((n, i) => edge(`t${i}`, n.id, nodes[(i + 1) % nodes.length].id))
  edges.push(edge('internal', 'n59', 'n59'))
  const full = buildChordData({ nodes, edges }, { maxTypes: 0 })
  const limited = applyTypeLimit(full, 10)
  assert.equal(
    limited.meta.drawnRelationEdges + limited.meta.omittedRelationEdges,
    full.meta.relationEdges
  )
  const matrix = buildHeatmapOption(full, theme)
  assert.equal(matrix.series[0].data.length, 61)
  assert.ok(matrix.dataZoom.some((z) => z.xAxisIndex === 0))
  assert.ok(matrix.dataZoom.some((z) => z.yAxisIndex === 0))
  assert.equal(matrix.xAxis.data.length, 60)
})
test('密集流向的节点间距有界，不挤成负高度或重叠标签', () => {
  const data = buildChordData(
    {
      nodes: Array.from({ length: 50 }, (_, i) => node(`n${i}`, `Type${i}`)),
      edges: Array.from({ length: 50 }, (_, i) => edge(`e${i}`, `n${i}`, `n${i}`))
    },
    { maxTypes: 0 }
  )
  const series = buildSankeyOption(data, theme).series[0]
  assert.ok(series.nodeGap * 49 <= 160)
  assert.equal(series.label.show, false)
  assert.equal(series.links.length, 50)
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
