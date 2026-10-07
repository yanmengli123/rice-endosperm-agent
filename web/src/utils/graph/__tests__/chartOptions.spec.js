/**
 * 图谱洞察 option 构建器测试（裸 node 运行）：
 *   node src/utils/graph/__tests__/chartOptions.spec.js
 */
import assert from 'node:assert/strict'
import process from 'node:process'

import { buildSankeyOption } from '../chartOptions.js'

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`✓ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${name}\n  ${error?.stack || error}`)
  }
}

const theme = {
  isDark: false,
  lighten: (c) => c,
  largeScale: false,
  labelColor: '#333',
  arcBorderColor: '#fff',
  ribbonOpacity: 0.6,
  sankeyLinkOpacity: 0.4,
  heatRamp: ['#eee', '#7fc0d4', '#046a82'],
  tooltip: {}
}

test('二部投影：互指对不构成环——所有连线严格源侧→目标侧，两侧节点名唯一', () => {
  const data = {
    nodes: [
      { name: 'A', itemStyle: { color: '#111' }, drawnTotal: 3 },
      { name: 'B', itemStyle: { color: '#222' }, drawnTotal: 3 }
    ],
    directedLinks: [
      { source: 'A', target: 'B', value: 2, relationTypes: [] },
      { source: 'B', target: 'A', value: 1, relationTypes: [] },
      { source: 'A', target: 'C', value: 1, relationTypes: [] } // C 无节点（折叠后丢失）：应被跳过
    ]
  }
  const opt = buildSankeyOption(data, theme)
  const series = opt.series[0]
  const names = new Set(series.data.map((n) => n.name))
  // 展示身份与原类型名分离，互指双方各出现在两侧。
  assert.equal(series.data.length, 4)
  assert.equal(names.size, 4)
  const byId = new Map(series.data.map((n) => [n.name, n]))
  for (const link of series.links) {
    assert.equal(byId.get(link.source).side, 'source')
    assert.equal(byId.get(link.target).side, 'target')
    assert.equal(byId.get(link.source).typeName, link.sourceType)
    assert.equal(byId.get(link.target).typeName, link.targetType)
    assert.ok(names.has(link.source) && names.has(link.target))
  }
  // 端点无节点的连线被跳过（C 不在 nodes）
  assert.equal(series.links.length, 2)
})

test('大规模档降参：layoutIterations=3 且关闭动画', () => {
  const data = {
    nodes: [{ name: 'A', itemStyle: { color: '#111' }, drawnTotal: 1 }],
    directedLinks: [{ source: 'A', target: 'A' + '', value: 1, relationTypes: [] }]
  }
  const big = buildSankeyOption(data, { ...theme, largeScale: true })
  assert.equal(big.animation, false)
  assert.equal(big.series[0].layoutIterations, 3)
  const small = buildSankeyOption(data, theme)
  assert.equal(small.animation, true)
  assert.equal(small.series[0].layoutIterations, 32)
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
